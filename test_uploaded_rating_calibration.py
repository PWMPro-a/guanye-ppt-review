from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "cloud"))
import verify_uploaded_rating_calibration as calibration  # noqa: E402


def gid(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def row(label: str, human: float, score: float, **extra) -> dict:
    value = {"idSha256": gid(label), "humanMean": human, "baseline": {"score": score}}
    value.update(extra)
    return value


def report(rows: list[dict], *, model: str = "model-a", scale: float = 5, **extra) -> dict:
    value = {"model": model, "scale": scale, "rows": rows}
    value.update(extra)
    return value


def parse(value: dict, label: str = "报告") -> dict:
    return calibration.parse_report(json.dumps(value).encode(), label=label)


def run(train: dict, holdout: dict, samples: int = 80):
    return calibration.evaluate(parse(train), parse(holdout), bootstrap_samples=samples)


def write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class ValidationTests(unittest.TestCase):
    def test_metadata_is_ignored_and_required_fields_are_validated(self):
        train = report(
            [
                row("1", 2, 2.2, humanCount=3, elapsedSeconds=1.2),
                row("2", 3, 3.1, assisted={"score": 4}),
            ],
            label="train",
            completedRequests=2,
        )
        train["rows"][0]["baseline"].update(images=["ignored"], elapsedSeconds=0.1)
        parsed = parse(train, "训练报告")
        self.assertEqual(parsed["model"], "model-a")
        self.assertEqual(parsed["scale"], 5.0)
        self.assertEqual(len(parsed["rows"]), 2)

        cases = [
            (report(train["rows"][:1], model="other"), "model 必须完全一致"),
            (report(train["rows"][:1], scale=10), "scale 必须完全一致"),
            (report([row("bad", 5.1, 3), row("ok", 3, 3)]), "humanMean 必须在"),
            (report([{"idSha256": "x", "humanMean": 3, "baseline": {"score": 3}}]), "idSha256"),
        ]
        for bad, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(calibration.InputError, message):
                calibration.evaluate(parsed, parse(bad, "留出报告"), bootstrap_samples=10)

    def test_duplicate_and_overlap_ids_are_rejected(self):
        duplicate = row("same", 3, 2.5)
        with self.assertRaisesRegex(calibration.InputError, "重复分组 ID"):
            parse(report([duplicate, dict(duplicate)]), "训练报告")

        shared = gid("shared")
        train = report([
            {"idSha256": shared, "humanMean": 3, "baseline": {"score": 2}},
            row("train-2", 4, 3.5),
        ])
        holdout = report([{"idSha256": shared.upper(), "humanMean": 3.5, "baseline": {"score": 3}}])
        with self.assertRaisesRegex(calibration.InputError, "重叠分组 ID"):
            calibration.evaluate(parse(train), parse(holdout), bootstrap_samples=10)

    def test_source_group_contract_rejects_partial_and_duplicate_representatives(self):
        source = gid("original-suite")
        partial = report([
            row("deck-a", 3, 2.5, sourceGroupSha256=source),
            row("deck-b", 4, 3.5),
        ])
        with self.assertRaisesRegex(calibration.InputError, "必须在每一行提供") as caught:
            parse(partial, "训练报告")
        self.assertNotIn(source, str(caught.exception))

        same_set = report([
            row("deck-c", 3, 2.5, sourceGroupSha256=source),
            row("deck-d", 4, 3.5, sourceGroupSha256=source.upper()),
        ])
        with self.assertRaisesRegex(calibration.InputError, "重复原始套件分组") as caught:
            parse(same_set, "训练报告")
        self.assertNotIn(source, str(caught.exception))

    def test_same_source_with_different_decks_cannot_cross_train_and_holdout(self):
        shared_source = gid("renamed-original")
        train = report([
            row("train-original-name", 3, 2.5, sourceGroupSha256=shared_source),
            row("train-other", 4, 3.5, sourceGroupSha256=gid("other-original")),
        ])
        holdout = report([
            row("holdout-renamed-copy", 3.5, 3, sourceGroupSha256=shared_source.upper()),
        ])
        with self.assertRaisesRegex(calibration.InputError, "重叠原始套件分组") as caught:
            calibration.evaluate(parse(train), parse(holdout), bootstrap_samples=10)
        self.assertNotIn(shared_source, str(caught.exception))

    def test_source_groups_must_be_present_on_both_sides(self):
        train = report([
            row("train-a", 3, 2.5, sourceGroupSha256=gid("source-a")),
            row("train-b", 4, 3.5, sourceGroupSha256=gid("source-b")),
        ])
        holdout = report([row("holdout-legacy", 3.5, 3)])
        with self.assertRaisesRegex(calibration.InputError, "必须同时提供"):
            calibration.evaluate(parse(train), parse(holdout), bootstrap_samples=10)

    def test_extrapolated_baseline_is_clipped(self):
        train = report([
            row("t1", 1, -8), row("t2", 2, 1), row("t3", 3, 2.5),
            row("t4", 4, 9), row("t5", 2.5, 3),
        ])
        holdout = report([row("h1", 1.5, -20), row("h2", 3, 2.5), row("h3", 4.5, 20)])
        result, predictions = run(train, holdout)
        for name in calibration.METHODS:
            self.assertTrue(all(0 <= value <= 5 for value in predictions[name]))
        self.assertGreater(result["methods"]["raw"]["clippedPredictions"], 0)
        self.assertGreater(result["methods"]["monotone_slope"]["clippedPredictions"], 0)
        self.assertTrue(result["validation"]["baselineOutsideScale"].startswith("accepted"))


class BehaviorTests(unittest.TestCase):
    def test_synthetic_underbias_is_corrected(self):
        train = report([row(f"t{i}", y, y - 1) for i, y in enumerate((1.5, 2, 2.5, 3, 3.5), 1)])
        holdout = report([row(f"h{i}", y, y - 1) for i, y in enumerate((1.75, 2.75, 3.75), 1)])
        result, _ = run(train, holdout)
        methods = result["methods"]
        self.assertAlmostEqual(methods["raw"]["mae"], 1.0)
        self.assertAlmostEqual(methods["raw"]["meanBias"], -1.0)
        self.assertAlmostEqual(methods["training_residual_median"]["mae"], 0.0)
        self.assertAlmostEqual(methods["monotone_slope"]["mae"], 0.0)
        self.assertEqual(result["slopeSelection"]["selected"]["slope"], 1.0)

    def test_holdout_labels_cannot_change_fit_and_report_is_repeatable(self):
        train = report([row(f"t{i}", y, score) for i, (y, score) in enumerate(
            ((1, 1.8), (2, 2.4), (3, 3.1), (4, 4.5), (2.5, 3)), 1
        )])
        low = report([row(f"h{i}", y, score) for i, (y, score) in enumerate(
            ((2.1, 2.5), (3.1, 3.5), (4.1, 4)), 1
        )])
        high = report([row(f"h{i}", y, score) for i, (y, score) in enumerate(
            ((1.1, 2.5), (4.9, 3.5), (2, 4)), 1
        )])
        first, _ = run(train, low)
        second, _ = run(train, high)
        again, _ = run(train, low)
        self.assertEqual(first["fittedParameters"], second["fittedParameters"])
        self.assertEqual(first["slopeSelection"], second["slopeSelection"])
        self.assertEqual(first, again)
        self.assertNotEqual(first["inputAudit"]["holdout"]["inputSha256"], second["inputAudit"]["holdout"]["inputSha256"])
        text = json.dumps(first, ensure_ascii=False)
        for item in train["rows"] + low["rows"]:
            self.assertNotIn(item["idSha256"], text)
        self.assertFalse(first["interpretation"]["automaticActivation"])
        self.assertIn("回顾性验证", first["interpretation"]["warning"])
        self.assertIn("新前瞻样本", first["interpretation"]["warning"])
        source_audit = first["sourceGroupAudit"]
        self.assertEqual(source_audit["status"], "source_groups_not_provided")
        self.assertIsNone(source_audit["declaredSourceGroupsDisjoint"])
        self.assertNotIn("originalSuiteIndependenceVerified", source_audit)
        self.assertIn("未验证原始套件独立性", source_audit["basis"])

    def test_source_group_audit_is_explicit_and_never_emits_hashes(self):
        train_sources = [gid("original-train-a"), gid("original-train-b")]
        holdout_source = gid("original-holdout")
        train = report([
            row("train-a", 2, 1.8, sourceGroupSha256=train_sources[0]),
            row("train-b", 3, 2.8, sourceGroupSha256=train_sources[1]),
        ])
        holdout = report([
            row("holdout-a", 4, 3.8, sourceGroupSha256=holdout_source),
        ])
        result, _ = run(train, holdout)
        audit = result["sourceGroupAudit"]
        self.assertEqual(audit["status"], "declared_source_groups_checked")
        self.assertIs(audit["declaredSourceGroupsDisjoint"], True)
        self.assertNotIn("originalSuiteIndependenceVerified", audit)
        self.assertNotIn("sourceGroupIndependenceVerified", result["interpretation"])
        self.assertEqual(audit["trainSourceGroups"], 2)
        self.assertEqual(audit["holdoutSourceGroups"], 1)
        self.assertFalse(audit["hashValuesEmitted"])
        self.assertEqual(result["validation"]["calibrationActivation"], "prohibited")
        self.assertFalse(result["interpretation"]["automaticActivation"])
        serialized = json.dumps(result, ensure_ascii=False)
        for source_hash in train_sources + [holdout_source]:
            self.assertNotIn(source_hash, serialized)


class CliTests(unittest.TestCase):
    def test_cli_module_writes_utf8_report_and_invalid_input_writes_nothing(self):
        train = report([row(f"t{i}", y, y - 0.2) for i, y in enumerate((2, 3, 4), 1)])
        holdout = report([row("h1", 2.5, 2.4), row("h2", 3.5, 3.6)])
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            train_path, holdout_path = folder / "train.json", folder / "holdout.json"
            output_path = folder / "result.json"
            write(train_path, train)
            write(holdout_path, holdout)
            command = [
                sys.executable, "-B", "-m", "cloud.verify_uploaded_rating_calibration",
                str(train_path), str(holdout_path), "--output", str(output_path),
            ]
            completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "")
            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8"))["status"], "completed")

            duplicate = row("dup", 3, 2.5)
            write(train_path, report([duplicate, dict(duplicate)]))
            bad_output = folder / "bad.json"
            failed = subprocess.run(command[:-1] + [str(bad_output)], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(failed.returncode, 2)
            self.assertFalse(bad_output.exists())
            self.assertIn("重复分组 ID", failed.stderr)

    def test_require_source_groups_rejects_legacy_input_without_output(self):
        train = report([row("t1", 2, 1.8), row("t2", 3, 2.8)])
        holdout = report([row("h1", 4, 3.8)])
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            train_path, holdout_path = folder / "train.json", folder / "holdout.json"
            output_path = folder / "result.json"
            write(train_path, train)
            write(holdout_path, holdout)
            completed = subprocess.run([
                sys.executable, "-B", "-m", "cloud.verify_uploaded_rating_calibration",
                str(train_path), str(holdout_path), "--require-source-groups",
                "--output", str(output_path),
            ], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(completed.returncode, 2)
            self.assertFalse(output_path.exists())
            self.assertIn("--require-source-groups", completed.stderr)
            self.assertIn("sourceGroupSha256", completed.stderr)

    def test_require_source_groups_accepts_complete_contract_and_suppresses_hashes(self):
        train_sources = [gid("strict-train-a"), gid("strict-train-b")]
        holdout_source = gid("strict-holdout")
        train = report([
            row("t1", 2, 1.8, sourceGroupSha256=train_sources[0]),
            row("t2", 3, 2.8, sourceGroupSha256=train_sources[1]),
        ])
        holdout = report([row("h1", 4, 3.8, sourceGroupSha256=holdout_source)])
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            train_path, holdout_path = folder / "train.json", folder / "holdout.json"
            output_path = folder / "result.json"
            write(train_path, train)
            write(holdout_path, holdout)
            completed = subprocess.run([
                sys.executable, "-B", "-m", "cloud.verify_uploaded_rating_calibration",
                str(train_path), str(holdout_path), "--require-source-groups",
                "--output", str(output_path),
            ], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            serialized = output_path.read_text(encoding="utf-8")
            result = json.loads(serialized)
            self.assertEqual(result["sourceGroupAudit"]["status"], "declared_source_groups_checked")
            self.assertIs(result["sourceGroupAudit"]["declaredSourceGroupsDisjoint"], True)
            for source_hash in train_sources + [holdout_source]:
                self.assertNotIn(source_hash, serialized)
                self.assertNotIn(source_hash, completed.stderr)

    def test_main_allows_atomic_replacement(self):
        train = report([row("t1", 2, 1.8), row("t2", 3, 2.8)])
        holdout = report([row("h1", 4, 3.8)])
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            train_path, holdout_path, output_path = folder / "t.json", folder / "h.json", folder / "o.json"
            write(train_path, train)
            write(holdout_path, holdout)
            for _ in range(2):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = calibration.main([str(train_path), str(holdout_path), "--output", str(output_path)])
                self.assertEqual(code, 0, stderr.getvalue())
            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8"))["status"], "completed")

    def test_output_must_not_overwrite_either_input_even_via_resolved_path(self):
        train = report([row("t1", 2, 1.8), row("t2", 3, 2.8)])
        holdout = report([row("h1", 4, 3.8)])
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            train_path, holdout_path = folder / "train.json", folder / "holdout.json"
            write(train_path, train)
            write(holdout_path, holdout)
            alias = folder / "alias"
            alias.mkdir()
            before = {train_path: train_path.read_bytes(), holdout_path: holdout_path.read_bytes()}
            targets = [
                train_path,
                alias / ".." / "train.json",
                holdout_path,
                alias / ".." / "holdout.json",
            ]
            for target in targets:
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    code = calibration.main([
                        str(train_path), str(holdout_path), "--output", str(target),
                    ])
                self.assertEqual(code, 2)
                self.assertIn("不能指向训练或留出报告输入文件", stderr.getvalue())
            self.assertEqual(train_path.read_bytes(), before[train_path])
            self.assertEqual(holdout_path.read_bytes(), before[holdout_path])

if __name__ == "__main__":
    unittest.main(verbosity=2)
