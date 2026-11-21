#!/usr/bin/env python3
"""Offline calibration for uploaded whole-deck ratings.

Required fields: ``model``, ``scale``, ``rows[]`` with ``idSha256``,
``humanMean`` and ``baseline.score``. Rows may also include
``sourceGroupSha256`` from an independent trusted original-suite fingerprint.
If any row declares it, every row in both reports must provide it. Unrelated
metadata is ignored. Report output contains aggregates, file input hashes and
source-group audit metadata only; it never emits raw IDs, source fingerprints
or scores.
"""
from __future__ import annotations

import argparse, hashlib, json, math, os, random, re, statistics, sys, tempfile
from pathlib import Path
from typing import Any, Sequence

BOOTSTRAP_SAMPLES, BOOTSTRAP_SEED, CONFIDENCE = 2_000, 20_261_121, 0.95
SLOPES = (0.0, 0.25, 0.5, 0.75, 1.0)
METHODS = ("raw", "fixed_midpoint", "training_median", "training_residual_median", "monotone_slope")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SOURCE_GROUP_FIELD = "sourceGroupSha256"
Row = tuple[str, float, float, str | None]
SOURCES = [
    {"name": "scikit-learn DummyRegressor median", "url": "https://github.com/scikit-learn/scikit-learn/blob/1.7.2/sklearn/dummy.py#L580-L586", "note": "median uses np.median; this tool uses statistics.median"},
    {"name": "scikit-learn IsotonicRegression bounds", "url": "https://github.com/scikit-learn/scikit-learn/blob/1.7.2/sklearn/isotonic.py#L173-L174", "note": "upstream bounds fitted values; this tool borrows only clipping"},
    {"name": "scikit-learn GroupShuffleSplit group-first split", "url": "https://github.com/scikit-learn/scikit-learn/blob/1.7.2/sklearn/model_selection/_split.py#L2078-L2097", "note": "upstream permutes unique groups and then maps the group partition back to sample indices; this tool enforces the analogous original-suite boundary without performing a split"},
]


class InputError(ValueError):
    """An input cannot produce a trustworthy report."""


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{label} 必须是有限数字")
    number = float(value)
    if not math.isfinite(number):
        raise InputError(f"{label} 必须是有限数字")
    return number


def parse_report(raw: bytes, *, label: str) -> dict[str, Any]:
    """Read required fields and deliberately ignore unrelated metadata."""
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{label} 不是有效的 UTF-8 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise InputError(f"{label} 根对象必须是 JSON 对象")
    for key in ("model", "scale", "rows"):
        if key not in value:
            raise InputError(f"{label} 缺少字段 {key}")
    model = value["model"]
    if not isinstance(model, str) or not model.strip() or model != model.strip():
        raise InputError(f"{label} model 必须是无首尾空白的非空字符串")
    scale = _number(value["scale"], f"{label} scale")
    if scale <= 0:
        raise InputError(f"{label} scale 必须大于 0")
    if not isinstance(value["rows"], list) or not value["rows"]:
        raise InputError(f"{label} rows 必须是非空数组")
    rows: list[Row] = []
    seen: set[str] = set()
    seen_source_groups: set[str] = set()
    source_group_rows = 0
    for index, row in enumerate(value["rows"]):
        where = f"{label} rows[{index}]"
        if not isinstance(row, dict):
            raise InputError(f"{where} 必须是 JSON 对象")
        for key in ("idSha256", "humanMean", "baseline"):
            if key not in row:
                raise InputError(f"{where} 缺少字段 {key}")
        group = row["idSha256"]
        if not isinstance(group, str) or not SHA256_RE.fullmatch(group):
            raise InputError(f"{where}.idSha256 必须是 64 位十六进制 SHA-256")
        group = group.lower()
        if group in seen:
            raise InputError(f"{label} 存在重复分组 ID；idSha256 必须唯一")
        seen.add(group)
        source_group: str | None = None
        if SOURCE_GROUP_FIELD in row:
            candidate = row[SOURCE_GROUP_FIELD]
            if not isinstance(candidate, str) or not SHA256_RE.fullmatch(candidate):
                raise InputError(f"{where}.{SOURCE_GROUP_FIELD} 必须是 64 位十六进制 SHA-256")
            source_group = candidate.lower()
            if source_group in seen_source_groups:
                raise InputError(f"{label} 存在重复原始套件分组，{SOURCE_GROUP_FIELD} 必须在每个文件中最多对应一个代表")
            seen_source_groups.add(source_group)
            source_group_rows += 1
        human = _number(row["humanMean"], f"{where}.humanMean")
        if not 0 <= human <= scale:
            raise InputError(f"{where}.humanMean 必须在 [0, {scale:g}] 内")
        baseline = row["baseline"]
        if not isinstance(baseline, dict) or "score" not in baseline:
            raise InputError(f"{where}.baseline 必须包含 score")
        score = _number(baseline["score"], f"{where}.baseline.score")
        rows.append((group, human, score, source_group))
    if source_group_rows not in (0, len(rows)):
        raise InputError(f"{label} 的 {SOURCE_GROUP_FIELD} 必须在每一行提供，不能部分缺失")
    rows.sort(key=lambda item: item[0])
    return {
        "model": model,
        "scale": scale,
        "rows": tuple(rows),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "sourceGroupDeclared": source_group_rows > 0,
        "sourceGroupCount": len(seen_source_groups),
    }


def load_report(path: str, *, label: str) -> dict[str, Any]:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise InputError(f"无法读取 {label}：{exc}") from exc
    return parse_report(raw, label=label)


def validate_pair(train: dict[str, Any], holdout: dict[str, Any], *, require_source_groups: bool = False) -> None:
    if train["model"] != holdout["model"]:
        raise InputError("训练报告与留出报告的 model 必须完全一致")
    if train["scale"] != holdout["scale"]:
        raise InputError("训练报告与留出报告的 scale 必须完全一致")
    if len(train["rows"]) < 2:
        raise InputError("训练报告至少需要 2 个分组，才能做 LOO 选斜率")
    if set(row[0] for row in train["rows"]) & set(row[0] for row in holdout["rows"]):
        raise InputError("训练集与留出集存在重叠分组 ID")
    train_declares_sources = train["sourceGroupDeclared"]
    holdout_declares_sources = holdout["sourceGroupDeclared"]
    if require_source_groups and not (train_declares_sources and holdout_declares_sources):
        raise InputError(f"--require-source-groups 要求训练报告与留出报告每一行都提供 {SOURCE_GROUP_FIELD}")
    if train_declares_sources != holdout_declares_sources:
        raise InputError(f"训练报告与留出报告必须同时提供 {SOURCE_GROUP_FIELD}，不能只在一侧提供")
    if train_declares_sources and holdout_declares_sources:
        train_sources = {row[3] for row in train["rows"]}
        holdout_sources = {row[3] for row in holdout["rows"]}
        if train_sources & holdout_sources:
            raise InputError("训练集与留出集存在重叠原始套件分组")


def _clip(value: float, scale: float) -> float:
    return min(max(value, 0.0), scale)


def _median(values: Sequence[float]) -> float:
    return float(statistics.median(values))


def _clean(value: float) -> float:
    return 0.0 if abs(value) < 5e-13 else round(float(value), 10)


def _percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lower, upper = math.floor(pos), math.ceil(pos)
    if lower == upper:
        return ordered[lower]
    fraction = pos - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _interval(values: Sequence[float]) -> list[float]:
    alpha = (1 - CONFIDENCE) / 2
    return [_clean(_percentile(values, alpha)), _clean(_percentile(values, 1 - alpha))]


def _select_slope(train: Sequence[Row], scale: float) -> dict[str, Any]:
    candidates: list[dict[str, float]] = []
    for slope in SLOPES:
        errors = []
        for index, (_, human, score, _) in enumerate(train):
            fit_rows = train[:index] + train[index + 1 :]
            intercept = _median([item[1] - slope * item[2] for item in fit_rows])
            errors.append(abs(_clip(slope * score + intercept, scale) - human))
        intercept = _median([human - slope * score for _, human, score, _ in train])
        candidates.append({"slope": slope, "looMae": statistics.fmean(errors), "intercept": intercept})
    selected = candidates[0]
    for candidate in candidates[1:]:
        if candidate["looMae"] < selected["looMae"] - 1e-12:
            selected = candidate
    return {"selected": selected, "candidates": candidates}


def _fit(train: dict[str, Any], holdout: dict[str, Any]) -> tuple[dict[str, Any], dict[str, tuple[float, ...]], dict[str, int], dict[str, Any]]:
    scale, training_rows, holdout_rows = train["scale"], train["rows"], holdout["rows"]
    midpoint = scale / 2
    training_median = _median([row[1] for row in training_rows])
    residual_median = _median([row[1] - row[2] for row in training_rows])
    slope_fit = _select_slope(training_rows, scale)
    slope, intercept = slope_fit["selected"]["slope"], slope_fit["selected"]["intercept"]
    unclipped = {
        "raw": tuple(row[2] for row in holdout_rows),
        "fixed_midpoint": tuple(midpoint for _ in holdout_rows),
        "training_median": tuple(training_median for _ in holdout_rows),
        "training_residual_median": tuple(row[2] + residual_median for row in holdout_rows),
        "monotone_slope": tuple(slope * row[2] + intercept for row in holdout_rows),
    }
    predictions = {name: tuple(_clip(value, scale) for value in values) for name, values in unclipped.items()}
    clipped = {name: sum(value < 0 or value > scale for value in values) for name, values in unclipped.items()}
    params = {
        "fixedMidpoint": _clean(midpoint),
        "trainingMedian": _clean(training_median),
        "trainingResidualMedian": _clean(residual_median),
        "monotoneSlope": {"slope": _clean(slope), "intercept": _clean(intercept)},
    }
    selection = {
        "criterion": "training leave-one-out MAE", "onlyTrainingRowsUsed": True,
        "tieBreak": "lowest slope wins; differences <= 1e-12 are ties",
        "selected": {"slope": _clean(slope), "intercept": _clean(intercept), "looMae": _clean(slope_fit["selected"]["looMae"])},
        "candidates": [{"slope": _clean(item["slope"]), "looMae": _clean(item["looMae"]), "fullFitIntercept": _clean(item["intercept"])} for item in slope_fit["candidates"]],
    }
    return params, predictions, clipped, selection


def _metrics(holdout: dict[str, Any], predictions: dict[str, tuple[float, ...]], clipped: dict[str, int], samples: int) -> dict[str, Any]:
    targets = tuple(row[1] for row in holdout["rows"])
    raw_errors = tuple(abs(pred - target) for pred, target in zip(predictions["raw"], targets))
    bootstrap = {name: {"mae": [], "bias": [], "paired": []} for name in METHODS}
    rng = random.Random(BOOTSTRAP_SEED)
    for _ in range(samples):
        indices = [rng.randrange(len(targets)) for _ in targets]
        for name in METHODS:
            method_errors = [abs(predictions[name][i] - targets[i]) for i in indices]
            bootstrap[name]["mae"].append(statistics.fmean(method_errors))
            bootstrap[name]["bias"].append(statistics.fmean([predictions[name][i] - targets[i] for i in indices]))
            bootstrap[name]["paired"].append(statistics.fmean([method_errors[j] - raw_errors[i] for j, i in enumerate(indices)]))
    raw_mae = statistics.fmean(raw_errors)
    result = {}
    for name in METHODS:
        errors = [abs(pred - target) for pred, target in zip(predictions[name], targets)]
        bias = statistics.fmean([pred - target for pred, target in zip(predictions[name], targets)])
        paired = statistics.fmean([error - raw for error, raw in zip(errors, raw_errors)])
        result[name] = {
            "mae": _clean(statistics.fmean(errors)), "meanBias": _clean(bias),
            "maeCI95": _interval(bootstrap[name]["mae"]), "meanBiasCI95": _interval(bootstrap[name]["bias"]),
            "pairedAbsoluteErrorDifferenceVsRaw": {"mean": _clean(paired), "ci95": _interval(bootstrap[name]["paired"])},
            "maeImprovementVsRaw": _clean(raw_mae - statistics.fmean(errors)), "clippedPredictions": clipped[name],
        }
    return result


def _source_group_audit(train: dict[str, Any], holdout: dict[str, Any]) -> dict[str, Any]:
    declared = train["sourceGroupDeclared"] and holdout["sourceGroupDeclared"]
    if declared:
        basis = f"训练与留出报告全部行均提供 {SOURCE_GROUP_FIELD}，文件内代表唯一且两侧分组不相交，指纹来源由调用方声明。"
    else:
        basis = "输入仅提供 idSha256，未验证原始套件独立性，无法识别同一原始 PPT 改名上传后的不同 deck ID。"
    return {
        "status": "declared_source_groups_checked" if declared else "source_groups_not_provided",
        "basis": basis,
        "declaredSourceGroupsDisjoint": True if declared else None,
        "trainSourceGroups": train["sourceGroupCount"],
        "holdoutSourceGroups": holdout["sourceGroupCount"],
        "contract": "complete_unique_per_file_disjoint_across_sets",
        "hashValuesEmitted": False,
        "trustBoundary": f"调用方声明 {SOURCE_GROUP_FIELD} 来自独立可信原始套件，工具仅核验完整性、代表唯一性和跨集不相交，不证明指纹来源，也不从标题、路径或 deck id 推导。",
    }


def evaluate(train: dict[str, Any], holdout: dict[str, Any], *, bootstrap_samples: int = BOOTSTRAP_SAMPLES, require_source_groups: bool = False) -> tuple[dict[str, Any], dict[str, tuple[float, ...]]]:
    validate_pair(train, holdout, require_source_groups=require_source_groups)
    if bootstrap_samples < 1:
        raise InputError("bootstrap_samples 必须大于 0")
    params, predictions, clipped, selection = _fit(train, holdout)
    source_group_audit = _source_group_audit(train, holdout)
    report = {
        "status": "completed", "schemaVersion": 3, "model": train["model"],
        "scale": {"min": 0.0, "max": _clean(train["scale"])},
        "inputAudit": {
            "train": {"rows": len(train["rows"]), "inputSha256": train["sha256"], "sourceGroupDeclared": train["sourceGroupDeclared"], "sourceGroupCount": train["sourceGroupCount"], "baselineOutsideScale": sum(row[2] < 0 or row[2] > train["scale"] for row in train["rows"])},
            "holdout": {"rows": len(holdout["rows"]), "inputSha256": holdout["sha256"], "sourceGroupDeclared": holdout["sourceGroupDeclared"], "sourceGroupCount": holdout["sourceGroupCount"], "baselineOutsideScale": sum(row[2] < 0 or row[2] > holdout["scale"] for row in holdout["rows"])},
            "policy": f"required fields validated; optional {SOURCE_GROUP_FIELD} is all-or-none across rows and reports; unrelated metadata ignored; no deck ID or source-group overlap allowed",
        },
        "sourceGroupAudit": source_group_audit,
        "validation": {
            "duplicateGroupIds": "rejected", "splitOverlap": "rejected",
            "calibrationActivation": "prohibited",
            "modelMismatch": "rejected", "scaleMismatch": "rejected",
            "humanMeanOutsideScale": "rejected", "baselineOutsideScale": "accepted and clipped",
            "rawRowsEmitted": False,
        },
        "fittedParameters": params, "slopeSelection": selection,
        "methods": _metrics(holdout, predictions, clipped, bootstrap_samples),
        "bootstrap": {"samples": bootstrap_samples, "seed": BOOTSTRAP_SEED, "confidence": CONFIDENCE, "interval": "paired percentile with linear interpolation"},
        "interpretation": {
            "status": "retrospective_holdout",
            "warning": "该留出集在查看基线后用于评估，属于回顾性验证。即使指标改善，也必须用此前未查看基线、未参与参数选择的新前瞻样本复核后才能确认；本报告不自动生效，也不修改线上策略。",
            "requiresNewProspectiveSampleToConfirm": True, "automaticActivation": False,
        },
        "sources": {"dependencyAdded": False, "references": SOURCES},
    }
    return report, predictions


def _serialize(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"


def _write_atomic(path: Path, text: str) -> None:
    if not path.parent.is_dir():
        raise InputError(f"输出目录不存在：{path.parent}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="离线比较上传整套评级的校准方法；留出集只评估。")
    parser.add_argument("train_report", help="训练报告 JSON")
    parser.add_argument("holdout_report", help="留出报告 JSON")
    parser.add_argument("--output", help="原子写入 JSON；省略时输出到 stdout")
    parser.add_argument("--require-source-groups", action="store_true", help=f"要求两侧每一行都提供调用方声明的 {SOURCE_GROUP_FIELD}，缺失时拒绝且不输出")
    args = parser.parse_args(argv)
    try:
        inputs = {_canonical_path(args.train_report), _canonical_path(args.holdout_report)}
        if args.output and _canonical_path(args.output) in inputs:
            raise InputError("--output 不能指向训练或留出报告输入文件")
        report, _ = evaluate(
            load_report(args.train_report, label="训练报告"),
            load_report(args.holdout_report, label="留出报告"),
            require_source_groups=args.require_source_groups,
        )
        text = _serialize(report)
        _write_atomic(Path(args.output), text) if args.output else sys.stdout.write(text)
        return 0
    except (InputError, OSError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
