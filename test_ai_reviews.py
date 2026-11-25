"""Contract tests for cloud.ai_reviews without network or production services."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cloud import ai_reviews

WORKSPACE = "11111111-1111-4111-8111-111111111111"
JUDGE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
MEMBER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OUTSIDER = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


class Router:
    def __init__(self):
        self.routes = []

    def add(self, method, path, handler):
        self.routes.append((method, path, handler))


class FakeCtx:
    def __init__(self, user_id=MEMBER, workspace_id=WORKSPACE, role="reviewer", body=None, query=None, store=None):
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.role = role
        self.token = "never-log-this"
        self.body = body or {}
        self.query = query or {}
        self.store = store or FakeStore()
        self.select_calls = []
        self.rpc_calls = []

    def select(self, table, query=None, privileged=False):
        self.select_calls.append((table, query or {}, privileged))
        return self.store.select(table, query or {})

    def rpc(self, name, params=None, privileged=False):
        params = params or {}
        self.rpc_calls.append((name, params, privileged))
        return self.store.rpc(name, params, privileged=privileged)

    def storage_get(self, path):
        return self.store.storage_get(path)

    def storage_put(self, path, data, content_type):
        return self.store.storage_put(path, data, content_type)

    def chat_json(self, messages):
        return self.store.chat_json(messages)


class FakeStore:
    def __init__(self):
        self.members = {WORKSPACE: {JUDGE: "admin", MEMBER: "reviewer"}}
        self.workspaces = {WORKSPACE: {"id": WORKSPACE, "dedicated_judge_id": JUDGE, "settings": {
            "maxScore": 5, "dimensions": [{"id": "visual", "name": "视觉"}, {"id": "layout", "name": "排版"}]
        }}}
        self.settings = {WORKSPACE: {"workspace_id": WORKSPACE, "vision_weight": 40, "layout_weight": 30, "match_weight": 30,
            "human_threshold": 0.85, "model_hint": "gpt-6.2-sol", "reason_catalog": [{"code": "hierarchy", "label": "层级"}]}}
        self.decks = {
            "uploaded-1": {"id": "uploaded-1", "workspace_id": WORKSPACE, "source_kind": "uploaded", "data": {
                "id": "uploaded-1", "name": "上传", "title": "上传", "images": [
                    {"id": "p1", "name": "p1.png", "path": WORKSPACE + "/uploaded-1/p1.png"},
                    {"id": "p2", "name": "p2.png", "path": WORKSPACE + "/uploaded-1/p2.png"}]}},
            "generated-1": {"id": "generated-1", "workspace_id": WORKSPACE, "source_kind": "generated", "data": {
                "id": "generated-1", "name": "生成", "title": "生成", "generation": {"outline": "out", "style": "s", "pages": [], "references": []},
                "images": [{"id": "g1", "name": "g1.png", "path": WORKSPACE + "/generated-1/g1.png"}]}},
        }
        self.reviews = {}
        self.submissions = {}
        self.jobs = {}
        self.queue_deck_calls = []
        self.queue_job_calls = []
        self.queue_count_calls = []
        self.queue_counts_override = None
        self.records = {}
        self.matches = []
        self.decisions = {}
        self.entries = {}
        self.workspace_batch_calls = []
        self.workspace_batch_error = None
        self.generate_calls = 0
        self.confirm_calls = []
        self.model_messages = []
        self.calibrations = {}
        self.calibration_calls = []
        self.comparison_result = {
            "holdoutUnits": 0, "baseline": None, "assisted": None, "improvement": None,
            "byKind": {
                "uploaded": {"holdoutUnits": 0, "baseline": None, "assisted": None, "improvement": None},
                "generated": {"holdoutUnits": 0, "baseline": None, "assisted": None, "improvement": None},
            },
            "splitAlgorithm": "md5-first-byte-mod-5-v1", "holdoutBucket": 0,
        }
        self.comparison_calls = []
        self.comparison_error = None
        self.backfill_calls = []
        self.backfill_result = {
            "ok": True, "nextOffset": None, "decks": 0, "preparedUnits": 0, "totalDecks": 0,
        }
        self.backfill_error = None
        self.import_provenance_calls = []
        self.import_provenance_error = None
        self.legacy_add_calls = []
        self.storage = {
            WORKSPACE + "/uploaded-1/p1.png": b"png-p1",
            WORKSPACE + "/uploaded-1/p2.png": b"png-p2",
            WORKSPACE + "/generated-1/g1.png": b"png-g1",
        }

    def _scores_complete(self, submission):
        entry = submission.get("entry") or {}
        scores = entry.get("scores") if isinstance(entry, dict) else None
        if not isinstance(scores, dict):
            return False
        if str(submission.get("source_kind") or "uploaded") == "generated":
            expected = ["visual", "layout", "match"]
        else:
            expected = [str(dim.get("id")) for dim in self.workspaces[submission["workspace_id"]]["settings"].get("dimensions", []) if isinstance(dim, dict) and dim.get("id")]
        maximum = float(submission.get("max_score") or 1)
        for dimension in expected:
            value = scores.get(dimension)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
            number = float(value)
            if not math.isfinite(number) or number < 0 or number > maximum:
                return False
        return bool(expected)

    def select(self, table, query):
        query = dict(query or {})
        projection = query.pop("select", None)
        rows = []
        if table == "members":
            role = self.members.get(query.get("workspace_id"), {}).get(query.get("user_id"))
            if role:
                rows = [{"workspace_id": query["workspace_id"], "user_id": query["user_id"], "role": role}]
        elif table == "workspaces":
            row = self.workspaces.get(query.get("id"))
            if row:
                rows = [row]
        elif table == "ai_review_settings":
            row = self.settings.get(query.get("workspace_id"))
            if row:
                rows = [row]
        elif table == "decks":
            deck = self.decks.get(query.get("id"))
            if deck and deck["workspace_id"] == query.get("workspace_id"):
                rows = [deck]
            elif not query.get("id"):
                rows = [d for d in self.decks.values() if d["workspace_id"] == query.get("workspace_id")]
        elif table == "reviews":
            row = self.reviews.get((query.get("deck_id"), query.get("user_id")))
            if row:
                rows = [row]
            elif not query.get("user_id"):
                rows = [row for (deck_id, _), row in self.reviews.items() if deck_id == query.get("deck_id")]
        elif table == "ai_review_submissions":
            for sid, row in self.submissions.items():
                if all((sid if key == "id" else row.get(key)) == value for key, value in query.items()):
                    rows.append(dict(row, id=sid))
        elif table == "ai_review_records":
            for rid, row in self.records.items():
                candidate = dict(row, id=rid)
                if all(candidate.get(k) == v for k, v in query.items()):
                    rows.append(candidate)
        elif table == "ai_review_matches":
            rows = [m for m in self.matches if m.get("submission_id") == query.get("submission_id") or m.get("record_id") == query.get("record_id")]
        elif table == "ai_review_decisions":
            rows = [dict(row, id=did) for did, row in self.decisions.items() if row.get("submission_id") == query.get("submission_id")]
        elif table == "ai_review_jobs":
            if query.get("id"):
                row = self.jobs.get(query["id"])
                rows = [dict(row, id=query["id"])] if row else []
            else:
                rows = [dict(row, id=jid) for jid, row in self.jobs.items() if all(row.get(k) == v for k, v in query.items())]
        if projection and projection != "*":
            fields = [field.strip() for field in projection.split(",") if field.strip()]
            rows = [{field: row.get(field) for field in fields} for row in rows]
        return rows
    def rpc(self, name, params, privileged=False):
        if name == "ai_review_workspace_batch":
            self.workspace_batch_calls.append((dict(params), privileged))
            if self.workspace_batch_error is not None:
                raise self.workspace_batch_error
            workspace = params.get("w")
            actor = params.get("actor")
            deck = self.decks.get(params.get("p_deck_id"))
            if actor not in self.members.get(workspace, {}):
                raise PermissionError("请先加入团队")
            if not deck or deck.get("workspace_id") != workspace:
                raise ValueError("作品不存在")
            submitted = []
            records = {}
            decisions = []
            for sid in params.get("p_submission_ids") or []:
                submission = self.submissions.get(sid)
                if (not submission or submission.get("workspace_id") != workspace
                        or submission.get("deck_id") != deck["id"] or submission.get("human_user_id") != actor):
                    continue
                submitted.append(sid)
                if self._scores_complete(submission):
                    for match in self.matches:
                        if match.get("submission_id") != sid:
                            continue
                        record = self.records.get(match.get("record_id"))
                        if not record:
                            continue
                        if record.get("workspace_id") != workspace or record.get("deck_id") != submission.get("deck_id"):
                            continue
                        if match.get("variant") != record.get("variant"):
                            continue
                        if (record.get("scope"), record.get("unit_id"), record.get("deck_version"), record.get("rubric_version")) != (
                                submission.get("scope"), submission.get("unit_id"), submission.get("deck_version"), submission.get("rubric_version")):
                            continue
                        records.setdefault(match["record_id"], dict(record, id=match["record_id"]))
            for did, decision in self.decisions.items():
                sid = decision.get("submission_id")
                if sid not in submitted:
                    continue
                entry = self.entries.get(did)
                decisions.append(dict(decision, id=did,
                    _library_visible=bool(entry and entry.get("library_visible")),
                    _entry_id=entry.get("id") if entry else None))
            return {"records": list(records.values()), "decisions": decisions}
        if name == "ai_review_queue_decks":
            self.queue_deck_calls.append((dict(params), privileged))
            kind = params.get("p_kind")
            if kind not in ("uploaded", "generated"):
                raise ValueError("队列类型无效")
            after = str(params.get("p_after") or "")
            limit = int(params.get("p_limit") or 0)
            rows = []
            for deck in self.decks.values():
                if deck["workspace_id"] != params.get("w") or deck["source_kind"] != kind:
                    continue
                deck_id = str(deck["id"])
                if after and deck_id <= after:
                    continue
                data = deck.get("data") or {}
                rows.append({
                    "id": deck_id,
                    "title": data.get("title") or data.get("name") or deck_id,
                    "name": data.get("name") or deck_id,
                    "sourceKind": deck["source_kind"],
                })
            rows.sort(key=lambda row: str(row["id"]))
            page = rows[:limit]
            next_cursor = str(page[-1]["id"]) if len(rows) > len(page) and page else None
            return {"items": page, "nextCursor": next_cursor}
        if name == "ai_review_queue_counts":
            self.queue_count_calls.append((dict(params), privileged))
            if self.queue_counts_override is not None:
                return json.loads(json.dumps(self.queue_counts_override))
            result = {kind: {status: 0 for status in ai_reviews.QUEUE_STATUSES} for kind in ai_reviews.QUEUE_ORDER}
            for row in self.jobs.values():
                kind = str(row.get("source_kind") or "")
                status = str(row.get("status") or "")
                if row.get("workspace_id") == params.get("w") and kind in result and status in result[kind]:
                    result[kind][status] += 1
            return result
        if name == "ai_review_queue_jobs":
            self.queue_job_calls.append((dict(params), privileged))
            kind = params.get("p_kind")
            rows = []
            for job_id, row in self.jobs.items():
                if row.get("workspace_id") != params.get("w"):
                    continue
                if kind and row.get("source_kind") != kind:
                    continue
                rows.append(dict(row, id=job_id))
            rows.sort(key=lambda row: (
                str(row.get("source_kind") or ""), str(row.get("created_at") or ""), str(row.get("id") or ""),
            ))
            return rows[:50]
        if name == "ai_review_rubric":
            return {"maxScore": 5, "dimensions": self.workspaces[WORKSPACE]["settings"]["dimensions"], "weights": {"visual": 40, "layout": 30, "match": 30}}
        if name == "ai_review_rubric_version":
            return "rubric-v1"
        if name == "ai_review_deck_version":
            deck = self.decks[params["target_id"]]
            return "deck-v1:" + json.dumps(deck["data"], sort_keys=True, separators=(",", ":"))
        if name == "ai_review_prepare":
            actor = params["actor"]
            if actor != JUDGE:
                raise PermissionError("仅专职评委")
            payload = params["payload"]
            deck = self.decks[payload["deckId"]]
            scope = payload["scope"]
            ids = [""] if scope == "deck" else payload.get("unitIds", [])
            jobs = []
            for unit in ids:
                key = (payload["deckId"], scope, unit, "assisted")
                if key not in self.jobs:
                    jid = "job-" + str(len(self.jobs) + 1)
                    self.jobs[jid] = {"workspace_id": WORKSPACE, "deck_id": payload["deckId"], "source_kind": deck["source_kind"],
                        "scope": scope, "unit_id": unit, "variant": "assisted", "status": "queued", "deck_version": "deck-v1:" + json.dumps(deck["data"], sort_keys=True, separators=(",", ":")),
                        "rubric_version": "rubric-v1", "bucket": 1, "split": "calibration", "attempts": 0,
                        "available_at": "2026-11-21T00:00:00Z"}
                jobs.append({"id": next(jid for jid, row in self.jobs.items() if row["deck_id"] == payload["deckId"] and row["scope"] == scope and row["unit_id"] == unit), "variant": "assisted"})
            return {"ok": True, "deckId": payload["deckId"], "scope": scope, "jobs": jobs, "aiVisible": False}
        if name == "ai_review_claim_jobs":
            if params["actor"] != JUDGE:
                raise PermissionError("仅专职评委")
            claimed = []
            for jid, row in self.jobs.items():
                if row["status"] == "queued" and row["source_kind"] == params["p_kind"]:
                    row["status"] = "running"; row["attempts"] += 1
                    claimed.append({"id": jid, "deckId": row["deck_id"], "sourceKind": row["source_kind"], "scope": row["scope"],
                        "unitId": row["unit_id"], "position": 1, "variant": row["variant"], "bucket": row["bucket"], "split": row["split"],
                        "deckVersion": row["deck_version"], "rubricVersion": row["rubric_version"], "maxScore": 5, "attempts": row["attempts"]})
                    if len(claimed) >= params.get("p_limit", 4): break
            return claimed
        if name == "ai_review_finish_attempt":
            if params["actor"] != JUDGE:
                raise PermissionError("仅专职评委")
            job = self.jobs.get(params["target_job"])
            if not job:
                raise ValueError("任务不存在")
            if job["status"] != "running" or job["attempts"] != params.get("p_attempt"):
                return {"ok": False, "status": "lease_lost", "aiVisible": False}
            if params["p_status"] == "failed":
                job["status"] = "failed"
                job["error"] = params.get("p_error") or ""
                return {"ok": True, "status": "failed", "aiVisible": False}
            if params["p_status"] != "completed":
                raise ValueError("任务状态无效")
            rid = "record-" + str(len(self.records) + 1)
            self.records[rid] = {"job_id": params["target_job"], "submission_id": None, "workspace_id": WORKSPACE, "deck_id": job["deck_id"],
                "source_kind": job["source_kind"], "scope": job["scope"], "unit_id": job["unit_id"], "variant": job["variant"],
                "bucket": job["bucket"], "split": job["split"], "deck_version": job["deck_version"], "rubric_version": job["rubric_version"],
                "max_score": 5, "model_hint": "gpt-6.2-sol", "ai_result": params["p_result"]}
            job["status"] = "completed"
            job["error"] = None
            for sid, sub in self.submissions.items():
                if (sub["deck_id"], sub["scope"], sub["unit_id"], sub["deck_version"], sub["rubric_version"]) == (job["deck_id"], job["scope"], job["unit_id"], job["deck_version"], job["rubric_version"]):
                    self.matches.append({"submission_id": sid, "record_id": rid, "variant": job["variant"]})
            return {"ok": True, "status": "completed", "recordId": rid, "aiVisible": False}
        if name == "ai_review_submit":
            actor = params["actor"]; payload = params["payload"]; deck = self.decks[payload["deckId"]]
            self.generate_calls += 1
            sids = []
            for unit in payload["units"]:
                uid = unit.get("unitId", "")
                sid = "sub-" + str(len(self.submissions) + 1)
                self.submissions[sid] = {"workspace_id": WORKSPACE, "deck_id": payload["deckId"], "source_kind": deck["source_kind"], "scope": payload["scope"],
                    "unit_id": uid, "human_user_id": actor, "deck_version": "deck-v1:" + json.dumps(deck["data"], sort_keys=True, separators=(",", ":")),
                    "rubric_version": "rubric-v1", "human_version": "human-v1-" + str(len(self.submissions) + 1), "max_score": 5, "entry": {k: v for k, v in unit.items() if k not in ("unitId", "position")}}
                for rid, record in self.records.items():
                    if (record["deck_id"], record["scope"], record["unit_id"], record["deck_version"], record["rubric_version"]) == (payload["deckId"], payload["scope"], uid, self.submissions[sid]["deck_version"], "rubric-v1"):
                        self.matches.append({"submission_id": sid, "record_id": rid, "variant": record["variant"]})
                sids.append(sid)
            if deck["source_kind"] == "uploaded" and payload["scope"] == "page":
                self.reviews[(payload["deckId"], actor)] = {"workspace_id": WORKSPACE, "user_id": actor, "deck_id": payload["deckId"], "data": {"mode": "page", "pages": {u.get("unitId", ""): {k: v for k, v in u.items() if k not in ("unitId", "position")} for u in payload["units"]}, "scores": {}}}
            elif deck["source_kind"] == "uploaded":
                entry = {k: v for k, v in payload["units"][0].items() if k not in ("unitId", "position")}
                self.reviews[(payload["deckId"], actor)] = {"workspace_id": WORKSPACE, "user_id": actor, "deck_id": payload["deckId"], "data": dict(entry, mode="deck")}
            return {"ok": True, "submissionIds": sids, "aiVisible": True, "message": "已提交人工评分"}
        if name == "ai_review_confirm":
            actor = params["actor"]
            if actor != JUDGE:
                raise PermissionError("仅专职评委")
            payload = params["payload"]
            if not isinstance(payload.get("formalScores"), dict) or not payload["formalScores"]:
                raise ValueError("必须显式填写正式分")
            sid = payload["submissionId"]
            if sid not in self.submissions:
                raise ValueError("人工提交不存在")
            did = "decision-" + str(len(self.decisions) + 1)
            self.decisions[did] = {"submission_id": sid, "classification": payload.get("classification"),
                "reasons": payload.get("reasons") or [], "reason_other": payload.get("reasonOther") or "", "formal_scores": payload["formalScores"]}
            self.confirm_calls.append(payload)
            return {"ok": True, "decisionId": did, "status": "confirmed", "formalScores": payload["formalScores"], "aiVisible": True}
        if name == "ai_review_calibration":
            if params["actor"] != JUDGE:
                raise PermissionError("仅专职评委可以读取校准样本")
            self.calibration_calls.append(dict(params))
            key = (params["p_kind"], params["p_deck_id"], params["p_scope"])
            return self.calibrations.get(key, {
                "selectedSamples": 0, "selectedCorrections": 0, "samples": [], "corrections": [],
            })
        if name == "ai_review_backfill":
            self.backfill_calls.append((dict(params), privileged))
            if self.backfill_error is not None:
                raise self.backfill_error
            return dict(self.backfill_result)
        if name == "ai_review_comparison":
            self.comparison_calls.append((dict(params), privileged))
            if self.comparison_error is not None:
                raise self.comparison_error
            return self.comparison_result
        if name == "single_judge_ppt_training_samples":
            return [{"scope": params["p_scope"], "deckId": params.get("p_deck_id"), "pageId": params.get("p_page_id"), "labelScope": "deck-proxy", "scores": {"visual": 4, "layout": 3}}]
        if name == "single_judge_ppt_training_counts":
            return {"decks": 127, "pages": 983, "scoredDecks": 127, "scope": "deck/page/cover"}
        if name == "ai_review_training_bundle":
            raise AssertionError("mixed-track training bundle is no longer allowed")
        if name == "get_review_library":
            decks = []
            for deck in self.decks.values():
                if deck["source_kind"] == "uploaded":
                    decks.append({"id": deck["id"], "data": dict(deck["data"], sourceKind="uploaded", libraryScope="deck")})
            return {"schemaVersion": 1, "workspaceId": WORKSPACE, "decks": decks, "entries": []}
        if name == "ai_import_review_deck":
            self.import_provenance_calls.append((dict(params), privileged))
            if self.import_provenance_error is not None:
                raise self.import_provenance_error
            return None
        if name == "add_review_deck":
            self.legacy_add_calls.append((dict(params), privileged))
            return None
        if name == "ai_review_update_settings":
            self.settings[WORKSPACE].update({"vision_weight": params["payload"].get("visionWeight", 40), "layout_weight": params["payload"].get("layoutWeight", 30), "match_weight": params["payload"].get("matchWeight", 30), "human_threshold": params["payload"].get("humanThreshold", .85)})
            return {"visionWeight": 40, "layoutWeight": 30, "matchWeight": 30, "humanThreshold": .85}
        raise AssertionError("unexpected rpc " + name)

    def storage_get(self, path):
        if path not in self.storage:
            raise FileNotFoundError(path)
        return self.storage[path]

    def storage_put(self, path, data, content_type):
        self.storage[path] = data
        return {"path": path}

    def chat_json(self, messages):
        self.model_messages.append(messages)
        return {"scores": {"visual": 4, "layout": 3, "match": 4}, "classification": {"label": "模型分类"},
            "reasons": ["模型理由"], "summary": "预评", "confidence": 0.8}
class AIReviewContractTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.router = Router()
        ai_reviews.configure(self.router)

    def ctx(self, user=MEMBER, body=None, query=None):
        return FakeCtx(user_id=user, body=body, query=query or {}, store=self.store)

    def prepare_and_run(self, scope="page", units=None):
        body = {"deckId": "generated-1", "scope": scope}
        if scope == "page":
            body["unitIds"] = units or ["g1"]
        ai_reviews.handle_post(self.ctx(JUDGE, body, {"action": "prepare"}))
        result = ai_reviews.handle_post(self.ctx(JUDGE, {"kind": "generated", "drain": True, "limit": 8}, {"action": "run"}))
        self.assertEqual(result["failed"], 0)
        return result

    def _deck_hash(self, deck_id):
        return "deck-v1:" + json.dumps(self.store.decks[deck_id]["data"], sort_keys=True, separators=(",", ":"))

    def _seed_job(self, job_id, **overrides):
        deck_id = overrides.pop("deck_id", "generated-1")
        deck = self.store.decks[deck_id]
        row = {
            "workspace_id": WORKSPACE,
            "deck_id": deck_id,
            "source_kind": deck["source_kind"],
            "scope": "deck",
            "unit_id": "",
            "variant": "assisted",
            "status": "queued",
            "attempts": 0,
            "available_at": "2026-11-21T00:00:00Z",
            "deck_version": self._deck_hash(deck_id),
            "rubric_version": "rubric-v1",
            "created_at": "2026-11-21T00:00:00Z",
            "updated_at": "2026-11-21T00:00:00Z",
        }
        row.update(overrides)
        self.store.jobs[job_id] = row
        return job_id

    def _add_deck(self, deck_id, kind):
        self.store.decks[deck_id] = {
            "id": deck_id, "workspace_id": WORKSPACE, "source_kind": kind,
            "data": {"id": deck_id, "name": "名称 " + deck_id, "title": "标题 " + deck_id, "images": []},
        }
        return deck_id

    def _seed_workspace_batch_pages(self, count=60):
        deck_id = "generated-1"
        deck_version = self._deck_hash(deck_id)
        for index in range(count):
            suffix = f"{index:03d}"
            submission_id = "batch-sub-" + suffix
            record_id = "batch-record-" + suffix
            decision_id = "batch-decision-" + suffix
            unit_id = "batch-page-" + suffix
            created = f"2026-11-21T00:00:{index:02d}Z"
            self.store.submissions[submission_id] = {
                "workspace_id": WORKSPACE, "deck_id": deck_id, "source_kind": "generated",
                "scope": "page", "unit_id": unit_id, "position": index,
                "human_user_id": MEMBER, "deck_version": deck_version, "rubric_version": "rubric-v1",
                "human_version": "human-" + suffix, "max_score": 5,
                "entry": {"scores": {"visual": 3, "layout": 3, "match": 3}},
                "created_at": created,
            }
            self.store.records[record_id] = {
                "job_id": "job-" + suffix, "workspace_id": WORKSPACE, "deck_id": deck_id,
                "source_kind": "generated", "scope": "page", "unit_id": unit_id,
                "variant": "assisted", "bucket": 1, "split": "calibration",
                "deck_version": deck_version, "rubric_version": "rubric-v1", "max_score": 5,
                "model_hint": "gpt-6.2-sol", "ai_result": {"scores": {"visual": 4, "layout": 4, "match": 4}},
                "created_at": created,
            }
            self.store.matches.append({"submission_id": submission_id, "record_id": record_id, "variant": "assisted"})
            self.store.decisions[decision_id] = {
                "submission_id": submission_id, "ai_record_id": record_id, "status": "confirmed",
                "classification": None, "reasons": [], "reason_other": "", "formal_scores": {"visual": 4},
                "formal_total": 4, "score_ratio": 0.8, "expires_at": created, "summary": "",
                "created_at": created,
            }
            self.store.entries[decision_id] = {
                "id": "batch-entry-" + suffix,
                "workspace_id": WORKSPACE,
                "deck_id": deck_id,
                "decision_id": decision_id,
                "library_visible": index % 2 == 0,
            }

    def test_routes_are_fixed(self):
        self.assertEqual([(m, p) for m, p, _ in self.router.routes], [
            ("GET", "/api/ai/reviews"), ("POST", "/api/ai/reviews"), ("POST", "/api/ai/reviews/import"),
            ("GET", "/api/ai/training/ppt-counts"), ("GET", "/api/ai/training/ppt-samples")
        ])

    def test_deck_training_reads_all_pages_in_batches_without_sampling(self):
        images = []
        for index in range(1, 26):
            path = f"{WORKSPACE}/uploaded-1/page-{index}.png"
            images.append({"id": f"p{index}", "name": f"page-{index}.png", "path": path, "position": index})
            self.store.storage[path] = f"png-{index}".encode()
        self.store.decks["uploaded-1"]["data"]["images"] = images
        job = {"scope": "deck", "sourceKind": "uploaded", "deckId": "uploaded-1", "variant": "baseline"}
        result = ai_reviews._run_deck_full(
            self.ctx(JUDGE), job, self.store.decks["uploaded-1"], self.store.settings[WORKSPACE],
            self.store.workspaces[WORKSPACE]["settings"], None,
        )
        self.assertEqual(3, len(self.store.model_messages))
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(25, result["coverage"]["pagesExpected"])
        self.assertEqual(25, result["coverage"]["pagesEvaluated"])
        labels = [part["text"] for messages in self.store.model_messages for part in messages[1]["content"] if part.get("type") == "text"]
        self.assertTrue(any("第1页/25" in label for label in labels))
        self.assertTrue(any("第13页/25" in label for label in labels))
        self.assertTrue(any("第25页/25" in label for label in labels))
        tasks = [json.loads(messages[1]["content"][0]["text"]) for messages in self.store.model_messages]
        self.assertEqual([1, 13, 25], [task["pageCoverage"]["batchStart"] for task in tasks])

    def test_ppt_training_samples_supports_deck_page_and_cover_scopes(self):
        all_decks = ai_reviews.handle_ppt_training_samples(self.ctx(JUDGE, query={"scope": "deck", "limit": "2000"}))
        self.assertEqual(("deck", None, None), (all_decks["scope"], all_decks["deckId"], all_decks["pageId"]))
        self.assertEqual(1, all_decks["count"])
        page = ai_reviews.handle_ppt_training_samples(self.ctx(JUDGE, query={"scope": "page", "deckId": "uploaded-1", "pageId": "page-2"}))
        self.assertEqual(("page", "uploaded-1", "page-2"), (page["scope"], page["deckId"], page["pageId"]))
        cover = ai_reviews.handle_ppt_training_samples(self.ctx(JUDGE, query={"scope": "cover", "deckId": "uploaded-1"}))
        self.assertEqual("cover", cover["scope"])
        self.assertEqual("deck-proxy", cover["samples"][0]["labelScope"])
        self.assertEqual(127, ai_reviews.handle_ppt_training_counts(self.ctx(JUDGE))["decks"])
        with self.assertRaisesRegex(ValueError, "整套训练不接受"):
            ai_reviews.handle_ppt_training_samples(self.ctx(JUDGE, query={"scope": "deck", "deckId": "uploaded-1"}))

    def test_preeval_is_hidden_then_unlock_is_exact_granularity_and_reused(self):
        self.prepare_and_run("page", ["g1"])
        self.prepare_and_run("deck")
        self.assertEqual(len(self.store.records), 2)
        self.assertEqual(len(self.store.jobs), 2)
        before = ai_reviews.handle_get(self.ctx(MEMBER, query={"action": "workspace", "kind": "generated", "deckId": "generated-1"}))
        self.assertEqual(before["ai"], [])
        self.assertFalse(before["blind"]["aiVisible"])
        submit = ai_reviews.handle_post(self.ctx(MEMBER, {
            "deckId": "generated-1", "scope": "page", "units": [{"unitId": "g1", "position": 1, "scores": {"visual": 3, "layout": 3, "match": 3}, "note": "人工秘密"}],
        }, {"action": "submit"}))
        self.assertEqual(self.store.generate_calls, 1)
        self.assertEqual(len(self.store.jobs), 2, "submit must reuse pre-evaluation instead of creating jobs")
        after = ai_reviews.handle_get(self.ctx(MEMBER, query={"action": "workspace", "kind": "generated", "deckId": "generated-1"}))
        self.assertEqual(len(after["ai"]), 1)
        self.assertEqual(after["ai"][0]["scope"], "page")
        self.assertEqual(after["ai"][0]["unitId"], "g1")
        self.assertNotIn("deck", [row["scope"] for row in after["ai"]])
        prompt = json.dumps(self.store.model_messages, ensure_ascii=False)
        self.assertNotIn("人工秘密", prompt)
        self.assertNotIn('"scores":{"visual":3', prompt)

    def test_judge_only_prepare_run_and_formal_without_model_reason_leak(self):
        with self.assertRaises(PermissionError):
            ai_reviews.handle_post(self.ctx(MEMBER, {"deckId": "generated-1", "scope": "deck"}, {"action": "prepare"}))
        self.prepare_and_run("page", ["g1"])
        submission = ai_reviews.handle_post(self.ctx(MEMBER, {
            "deckId": "generated-1", "scope": "page", "units": [{"unitId": "g1", "position": 1, "scores": {"visual": 2}}]
        }, {"action": "submit"}))["submissionIds"][0]
        with self.assertRaises(PermissionError):
            ai_reviews.handle_post(self.ctx(MEMBER, {"submissionId": submission, "formalScores": {"visual": 4}}, {"action": "formal"}))
        with self.assertRaises(ValueError):
            ai_reviews.handle_post(self.ctx(JUDGE, {"submissionId": submission}, {"action": "formal"}))
        result = ai_reviews.handle_post(self.ctx(JUDGE, {"submissionId": submission, "formalScores": {"visual": 4, "layout": 4, "match": 4}}, {"action": "formal"}))
        self.assertTrue(result["ok"])
        decision = self.store.decisions[result["decisionId"]]
        self.assertIsNone(decision["classification"])
        self.assertEqual(decision["reasons"], [])
        self.assertNotIn("模型分类", json.dumps(decision, ensure_ascii=False))
        self.assertNotIn("模型理由", json.dumps(decision, ensure_ascii=False))

    def test_notes_only_does_not_unlock_and_status_uses_complete_scores(self):
        self.prepare_and_run("page", ["g1"])
        ai_reviews.handle_post(self.ctx(MEMBER, {
            "deckId": "generated-1", "scope": "page",
            "units": [{"unitId": "g1", "position": 1, "scores": {}, "note": "notes only"}],
        }, {"action": "submit"}))
        before = ai_reviews.handle_get(self.ctx(MEMBER, query={
            "action": "workspace", "kind": "generated", "deckId": "generated-1",
        }))
        self.assertEqual(before["ai"], [])
        self.assertFalse(before["submissions"][0]["scoresComplete"])

        ai_reviews.handle_post(self.ctx(MEMBER, {
            "deckId": "generated-1", "scope": "page",
            "units": [{"unitId": "g1", "position": 1, "scores": {"visual": 3, "layout": 3, "match": 3}}],
        }, {"action": "submit"}))
        after = ai_reviews.handle_get(self.ctx(MEMBER, query={
            "action": "workspace", "kind": "generated", "deckId": "generated-1",
        }))
        self.assertEqual(len(after["ai"]), 1)
        self.assertTrue(any(row["scoresComplete"] for row in after["submissions"]))

    def test_workspace_rejects_deck_from_wrong_queue_kind(self):
        with self.assertRaisesRegex(ValueError, '队列类型不匹配'):
            ai_reviews.handle_get(self.ctx(MEMBER, query={"action": "workspace", "kind": "uploaded", "deckId": "generated-1"}))

    def test_workspace_batch_collapses_sixty_page_detail_reads_to_one_rpc(self):
        self._seed_workspace_batch_pages(60)
        ctx = self.ctx(MEMBER, query={"action": "workspace", "kind": "generated", "deckId": "generated-1"})
        state = ai_reviews.handle_get(ctx)
        self.assertEqual(60, len(state["ai"]))
        self.assertEqual(60, len(state["decisions"]))
        self.assertEqual(1, len(self.store.workspace_batch_calls))
        params, privileged = self.store.workspace_batch_calls[0]
        self.assertTrue(privileged)
        self.assertEqual(60, len(params["p_submission_ids"]))
        self.assertEqual(60, len(set(params["p_submission_ids"])))
        detail_tables = {"ai_review_matches", "ai_review_records", "ai_review_decisions", "ai_generated_entries"}
        detail_selects = [call for call in ctx.select_calls if call[0] in detail_tables]
        self.assertEqual([], detail_selects)
        self.assertEqual(30, sum(bool(row["libraryVisible"]) for row in state["decisions"]))

        self.store.workspace_batch_error = ValueError("PGRST202 function not found")
        fallback_ctx = self.ctx(MEMBER, query={"action": "workspace", "kind": "generated", "deckId": "generated-1"})
        fallback = ai_reviews.handle_get(fallback_ctx)
        self.assertEqual(60, len(fallback["ai"]))
        fallback_detail_selects = [call for call in fallback_ctx.select_calls if call[0] in detail_tables]
        self.assertEqual(240, len(fallback_detail_selects))
        self.assertEqual(1, sum(name == "ai_review_workspace_batch" for name, _params, _privileged in fallback_ctx.rpc_calls))

    def test_workspace_batch_fallback_is_limited_to_missing_rpc(self):
        self._seed_workspace_batch_pages(1)
        for message in ("permission denied by database", "function public.ai_review_workspace_batch does not exist"):
            with self.subTest(message=message):
                self.store.workspace_batch_error = ValueError(message)
                ctx = self.ctx(MEMBER, query={"action": "workspace", "kind": "generated", "deckId": "generated-1"})
                with self.assertRaisesRegex(ValueError, message.split()[0]):
                    ai_reviews.handle_get(ctx)
                self.assertFalse(any(call[0] in {"ai_review_matches", "ai_review_records", "ai_review_decisions", "ai_generated_entries"} for call in ctx.select_calls))

    def test_workspace_uses_queue_pages_and_explicit_deck_without_full_scan(self):
        ctx = self.ctx(MEMBER, query={"action": "workspace", "kind": "generated"})
        state = ai_reviews.handle_get(ctx)
        self.assertEqual("generated-1", state["deck"]["id"])
        self.assertEqual(
            {"uploaded": ["uploaded-1"], "generated": ["generated-1"]},
            {track: [item["id"] for item in items] for track, items in state["queues"].items()},
        )
        self.assertEqual({"uploaded": None, "generated": None}, state["queueCursors"])
        self.assertEqual([
            ({"w": WORKSPACE, "p_kind": "uploaded", "p_after": None, "p_limit": 50}, True),
            ({"w": WORKSPACE, "p_kind": "generated", "p_after": None, "p_limit": 50}, True),
        ], self.store.queue_deck_calls)
        deck_calls = [call for call in ctx.select_calls if call[0] == "decks"]
        self.assertEqual([("decks", {"workspace_id": WORKSPACE, "id": "generated-1"}, True)], deck_calls)
        self.assertFalse(any(call[0] == "decks" and "id" not in call[1] for call in ctx.select_calls))

        explicit = ai_reviews.handle_get(self.ctx(MEMBER, query={
            "action": "workspace", "kind": "generated", "deckId": "generated-1",
        }))
        self.assertEqual("generated-1", explicit["deck"]["id"])

    def test_queue_decks_paginates_both_tracks_with_fixed_size_and_custom_cursor(self):
        for index in range(51):
            self._add_deck(f"custom/{index:03d}/α", "uploaded")
            self._add_deck(f"generated-custom/{index:03d}/α", "generated")

        uploaded = ai_reviews.handle_get(self.ctx(MEMBER, query={"action": "queue-decks", "kind": "uploaded"}))
        self.assertEqual({"kind", "items", "nextCursor"}, set(uploaded))
        self.assertEqual(50, len(uploaded["items"]))
        self.assertEqual("custom/000/α", uploaded["items"][0]["id"])
        self.assertEqual("custom/049/α", uploaded["nextCursor"])
        self.assertEqual({"id", "title", "name", "sourceKind"}, set(uploaded["items"][0]))
        empty_cursor = ai_reviews.handle_get(self.ctx(MEMBER, query={
            "action": "queue-decks", "kind": "uploaded", "cursor": "",
        }))
        self.assertEqual(uploaded, empty_cursor)
        self.assertIsNone(self.store.queue_deck_calls[-1][0]["p_after"])
        uploaded_next = ai_reviews.handle_get(self.ctx(MEMBER, query={
            "action": "queue-decks", "kind": "uploaded", "cursor": uploaded["nextCursor"],
        }))
        self.assertEqual(["custom/050/α", "uploaded-1"], [item["id"] for item in uploaded_next["items"]])
        self.assertIsNone(uploaded_next["nextCursor"])

        generated = ai_reviews.handle_get(self.ctx(MEMBER, query={"action": "queue-decks", "kind": "generated"}))
        self.assertEqual(50, len(generated["items"]))
        self.assertEqual("generated-custom/048/α", generated["nextCursor"])
        generated_next = ai_reviews.handle_get(self.ctx(MEMBER, query={
            "action": "queue-decks", "kind": "generated", "cursor": generated["nextCursor"],
        }))
        self.assertEqual(["generated-custom/049/α", "generated-custom/050/α"], [
            item["id"] for item in generated_next["items"]
        ])
        self.assertTrue(all(item["sourceKind"] == "generated" for item in generated_next["items"]))

        fixed = ai_reviews.handle_get(self.ctx(MEMBER, query={
            "action": "queue-decks", "kind": "uploaded", "cursor": "custom/049/α",
            "limit": "1", "workspace": "client-workspace", "w": "client-workspace",
        }))
        self.assertEqual({"w": WORKSPACE, "p_kind": "uploaded", "p_after": "custom/049/α", "p_limit": 50},
                         self.store.queue_deck_calls[-1][0])
        serialized = json.dumps(fixed, ensure_ascii=False)
        self.assertNotIn("scores", serialized)
        self.assertNotIn("counts", serialized)

    def test_queue_decks_rejects_bad_track_and_oversized_cursor_before_rpc(self):
        with self.assertRaisesRegex(ValueError, "队列类型无效"):
            ai_reviews.handle_get(self.ctx(MEMBER, query={"action": "queue-decks", "kind": "video"}))
        with self.assertRaisesRegex(ValueError, "游标过长"):
            ai_reviews.handle_get(self.ctx(MEMBER, query={
                "action": "queue-decks", "kind": "uploaded",
                "cursor": "x" * (ai_reviews.MAX_QUEUE_CURSOR_LENGTH + 1),
            }))
        self.assertEqual([], self.store.queue_deck_calls)

    def test_queue_decks_requires_exact_contract_item_fields(self):
        with mock.patch.object(ai_reviews, "_rpc", return_value={
            "items": [{"id": "legacy", "data": {"title": "旧标题", "name": "旧名称"}}],
            "nextCursor": None,
        }):
            with self.assertRaisesRegex(ValueError, "作品队列返回格式无效"):
                ai_reviews.handle_get(self.ctx(MEMBER, query={"action": "queue-decks", "kind": "uploaded"}))

    def test_queue_pages_are_workspace_and_member_scoped(self):
        self._add_deck("other-only", "uploaded")
        self.store.decks["other-only"]["workspace_id"] = "other-workspace"
        page = ai_reviews.handle_get(self.ctx(MEMBER, query={"action": "queue-decks", "kind": "uploaded"}))
        self.assertNotIn("other-only", [item["id"] for item in page["items"]])

        self.store.queue_deck_calls.clear()
        with self.assertRaises(PermissionError):
            ai_reviews.handle_get(self.ctx(OUTSIDER, query={"action": "queue-decks", "kind": "uploaded"}))
        self.assertEqual([], self.store.queue_deck_calls)
        with self.assertRaisesRegex(ValueError, "作品不存在"):
            ai_reviews.handle_get(self.ctx(MEMBER, query={
                "action": "workspace", "kind": "uploaded", "deckId": "other-only",
            }))
        self.assertEqual([], self.store.queue_deck_calls)

    def test_queue_state_uses_rpc_limits_and_returns_exact_large_counts(self):
        self.store.queue_counts_override = {
            "uploaded": {"queued": 2**53 + 1, "running": 20, "completed": 30, "failed": 40},
            "generated": {"queued": 2**53 + 3, "running": 21, "completed": 31, "failed": 41},
        }
        for index in range(56):
            kind = "uploaded" if index % 2 == 0 else "generated"
            deck_id = "uploaded-1" if kind == "uploaded" else "generated-1"
            self._seed_job(f"queue-{index:03d}", deck_id=deck_id)

        result = ai_reviews.handle_get(self.ctx(JUDGE, query={"action": "queue"}))
        self.assertEqual(self.store.queue_counts_override, result["counts"])
        self.assertEqual(50, len(result["items"]))
        self.assertFalse(result["aiVisible"])
        self.assertEqual({
            "id", "deck_id", "source_kind", "scope", "unit_id", "position", "variant", "status",
            "attempts", "bucket", "split", "deck_version", "rubric_version", "error",
        }, set(result["items"][0]))
        self.assertEqual([({"w": WORKSPACE, "p_kind": None}, True)], self.store.queue_job_calls)
        self.assertEqual([({"w": WORKSPACE}, True)], self.store.queue_count_calls)
        ctx = self.ctx(JUDGE, query={"action": "queue"})
        ai_reviews.handle_get(ctx)
        self.assertFalse(any(call[0] == "ai_review_jobs" for call in ctx.select_calls))

        job_calls = list(self.store.queue_job_calls)
        count_calls = list(self.store.queue_count_calls)
        with self.assertRaises(PermissionError):
            ai_reviews.handle_get(self.ctx(MEMBER, query={"action": "queue"}))
        self.assertEqual(job_calls, self.store.queue_job_calls)
        self.assertEqual(count_calls, self.store.queue_count_calls)

    def test_queue_counts_rejects_non_int_values_instead_of_coercing(self):
        base = {
            "uploaded": {"queued": 1, "running": 0, "completed": 0, "failed": 0},
            "generated": {"queued": 0, "running": 0, "completed": 0, "failed": 0},
        }
        ctx = self.ctx(JUDGE)
        for invalid in (True, 1.0, "1", -1):
            with self.subTest(invalid=invalid):
                self.store.queue_counts_override = json.loads(json.dumps(base))
                self.store.queue_counts_override["uploaded"]["queued"] = invalid
                with self.assertRaisesRegex(ValueError, "队列统计返回格式无效"):
                    ai_reviews._queue_counts(ctx)
        self.store.queue_counts_override = base
        result = ai_reviews._queue_counts(ctx)
        self.assertIs(type(result["uploaded"]["queued"]), int)
        self.assertEqual(1, result["uploaded"]["queued"])

    def test_default_generated_queue_deck_stays_blind_until_exact_submission(self):
        self.prepare_and_run("page", ["g1"])
        before = ai_reviews.handle_get(self.ctx(MEMBER, query={"action": "workspace", "kind": "generated"}))
        self.assertEqual("generated-1", before["deck"]["id"])
        self.assertEqual([], before["ai"])
        self.assertFalse(before["blind"]["aiVisible"])

        ai_reviews.handle_post(self.ctx(MEMBER, {
            "deckId": "generated-1", "scope": "page",
            "units": [{"unitId": "g1", "position": 1, "scores": {"visual": 3, "layout": 3, "match": 3}}],
        }, {"action": "submit"}))
        after = ai_reviews.handle_get(self.ctx(MEMBER, query={"action": "workspace", "kind": "generated"}))
        self.assertEqual(1, len(after["ai"]))
        self.assertEqual("g1", after["ai"][0]["unitId"])
        self.assertTrue(after["blind"]["aiVisible"])

    def test_pre_evaluation_status_uses_only_current_assisted_deck_job(self):
        self._seed_job("baseline-current", variant="baseline", status="completed")
        self._seed_job("page-current", scope="page", unit_id="g1", status="completed")
        self._seed_job("old-current", deck_version="deck-v1:old", status="completed")
        self._seed_job("old-rubric", rubric_version="rubric-v0", status="running")
        self._seed_job("wrong-kind", source_kind="uploaded", status="completed")
        self._seed_job("wrong-deck", deck_id="uploaded-1", status="completed")
        self._seed_job("other-workspace", workspace_id="other-workspace", status="running")
        self._seed_job(
            "target", status="queued", error="模型错误正文", model_hint="gpt-secret",
            ai_result={"scores": {"visual": 5}},
        )
        result = ai_reviews.handle_get(self.ctx(MEMBER, query={
            "action": "pre-evaluation-status", "kind": "generated", "deckId": "generated-1",
        }))
        self.assertEqual(result, {"preEvaluationStatus": "queued"})

    def test_uploaded_status_prefers_current_baseline_and_keeps_historical_fallback(self):
        target = {"deck_id": "uploaded-1", "source_kind": "uploaded"}
        query = {"action": "pre-evaluation-status", "kind": "uploaded", "deckId": "uploaded-1"}
        self._seed_job("legacy-assisted", **target, status="completed")
        self._seed_job("other-scoped-baseline", **target, scope="page", variant="baseline", status="running")
        self.assertEqual({"preEvaluationStatus": "completed"}, ai_reviews.handle_get(self.ctx(MEMBER, query=query)))
        self._seed_job("current-baseline", **target, variant="baseline", status="queued")
        self.assertEqual({"preEvaluationStatus": "queued"}, ai_reviews.handle_get(self.ctx(MEMBER, query=query)))

    def test_pre_evaluation_status_distinguishes_retry_from_terminal_failure(self):
        self._seed_job("status-job", status="failed", attempts=1, available_at="2026-11-21T02:00:00Z")
        query = {"action": "pre-evaluation-status", "kind": "generated", "deckId": "generated-1"}

        def current_status():
            return ai_reviews.handle_get(self.ctx(MEMBER, query=query))["preEvaluationStatus"]

        self.assertEqual(current_status(), "retry_scheduled")
        self.store.jobs["status-job"]["available_at"] = "infinity"
        self.assertEqual(current_status(), "failed")
        self.store.jobs["status-job"].update({"attempts": 3, "available_at": "2026-11-21T02:00:00Z"})
        self.assertEqual(current_status(), "failed")
        self.store.jobs["status-job"].update({"status": "running", "attempts": 2})
        self.assertEqual(current_status(), "running")
        self.store.jobs["status-job"]["status"] = "completed"
        self.assertEqual(current_status(), "completed")
        self.store.jobs["status-job"]["status"] = "queued"
        self.assertEqual(current_status(), "queued")
        del self.store.jobs["status-job"]
        self.assertEqual(current_status(), "not_queued")

    def test_pre_evaluation_status_handles_invalid_kind_and_wrong_deck(self):
        with self.assertRaisesRegex(ValueError, "队列类型无效"):
            ai_reviews.handle_get(self.ctx(MEMBER, query={
                "action": "pre-evaluation-status", "kind": "video", "deckId": "generated-1",
            }))
        for deck_id in ("missing", "uploaded-1"):
            result = ai_reviews.handle_get(self.ctx(MEMBER, query={
                "action": "pre-evaluation-status", "kind": "generated", "deckId": deck_id,
            }))
            self.assertEqual(result, {"preEvaluationStatus": "not_queued"})

    def test_pre_evaluation_status_preserves_text_deck_id_verbatim(self):
        deck_id = " custom/pre-eval α "
        self._add_deck(deck_id, "uploaded")
        self._seed_job("custom-status-job", deck_id=deck_id)
        ctx = self.ctx(MEMBER, query={
            "action": "pre-evaluation-status", "kind": "uploaded", "deckId": deck_id,
        })
        self.assertEqual({"preEvaluationStatus": "queued"}, ai_reviews.handle_get(ctx))
        deck_calls = [call for call in ctx.select_calls if call[0] == "decks"]
        self.assertEqual([("decks", {"workspace_id": WORKSPACE, "id": deck_id}, True)], deck_calls)
        self.assertNotIn(("decks", {"workspace_id": WORKSPACE, "id": deck_id.strip()}, True), deck_calls)

    def test_prepare_and_submit_preserve_contract_deck_ids(self):
        for deck_id in (" padded/prepare α ", "   ", "x" * 100):
            with self.subTest(deck_id=repr(deck_id)):
                self._add_deck(deck_id, "generated")
                prepared = ai_reviews.handle_post(self.ctx(JUDGE, {
                    "deckId": deck_id, "scope": "deck",
                }, {"action": "prepare"}))
                self.assertEqual(deck_id, prepared["deckId"])
                jobs = [row for row in self.store.jobs.values() if row["deck_id"] == deck_id]
                self.assertEqual(1, len(jobs))
                self.assertEqual(deck_id, jobs[0]["deck_id"])

                submitted = ai_reviews.handle_post(self.ctx(MEMBER, {
                    "deckId": deck_id, "scope": "deck",
                    "units": [{"scores": {"visual": 3, "layout": 3, "match": 3}}],
                }, {"action": "submit"}))
                self.assertTrue(submitted["ok"])
                submissions = [row for row in self.store.submissions.values() if row["deck_id"] == deck_id]
                self.assertEqual(1, len(submissions))
                self.assertEqual(deck_id, submissions[0]["deck_id"])

    def test_prepare_and_submit_reject_invalid_deck_id_contract(self):
        for action in ("prepare", "submit"):
            user = JUDGE if action == "prepare" else MEMBER
            base = {"scope": "deck", "units": [{"scores": {"visual": 3}}]}
            with self.subTest(action=action, deck_id="missing"):
                with self.assertRaisesRegex(ValueError, "需要 deckId"):
                    ai_reviews.handle_post(self.ctx(user, dict(base), {"action": action}))
            for deck_id in (None, "", 3, True, [], {}, "x" * 101):
                body = dict(base, deckId=deck_id)
                with self.subTest(action=action, deck_id=repr(deck_id)):
                    with self.assertRaisesRegex(ValueError, "需要 deckId"):
                        ai_reviews.handle_post(self.ctx(user, body, {"action": action}))

    def test_workspace_exposes_pre_evaluation_status_without_unlocking_results(self):
        self.prepare_and_run("deck")
        state = ai_reviews.handle_get(self.ctx(MEMBER, query={
            "action": "workspace", "kind": "generated", "deckId": "generated-1",
        }))
        self.assertEqual(state["preEvaluationStatus"], "completed")
        self.assertEqual(state["ai"], [])
        self.assertFalse(state["blind"]["aiVisible"])

    def test_backfill_uses_single_service_rpc_and_queue_only_contract(self):
        legacy_review = {
            "workspace_id": WORKSPACE, "user_id": MEMBER, "deck_id": "uploaded-1",
            "data": {"mode": "page", "pages": {"p1": {"scores": {"visual": 4}}}},
        }
        self.store.reviews[("uploaded-1", MEMBER)] = legacy_review
        self.store.backfill_result = {
            "ok": True, "nextOffset": 5, "decks": 1, "preparedUnits": 3, "totalDecks": 9,
        }
        ctx = self.ctx(JUDGE, {
            "kind": "uploaded", "offset": 2, "limit": 3,
            "w": "client-workspace", "actor": MEMBER,
        }, {"action": "backfill"})
        result = ai_reviews.handle_post(ctx)
        self.assertEqual({
            "ok": True, "kind": "uploaded", "offset": 2, "nextOffset": 5,
            "decks": 1, "preparedUnits": 3, "submittedUnits": 0, "totalDecks": 9,
        }, result)
        self.assertEqual([({
            "w": WORKSPACE, "actor": JUDGE, "p_kind": "uploaded", "p_offset": 2, "p_limit": 3,
        }, True)], self.store.backfill_calls)
        self.assertEqual(legacy_review, self.store.reviews[("uploaded-1", MEMBER)])
        self.assertEqual({}, self.store.submissions)
        self.assertEqual(0, self.store.generate_calls)
        self.assertEqual({}, self.store.jobs)
        self.assertFalse(any(table == "reviews" for table, _query, _privileged in ctx.select_calls))

    def test_backfill_keeps_existing_bounds_and_rejects_invalid_int4_inputs(self):
        cases = (
            ({}, (0, 8)),
            ({"offset": -5, "limit": 0}, (0, 1)),
            ({"offset": "7", "limit": 100}, (7, 20)),
            ({"offset": 2147483647, "limit": 2147483647}, (2147483647, 20)),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                self.store.backfill_calls.clear()
                result = ai_reviews.handle_post(self.ctx(JUDGE, body, {"action": "backfill"}))
                call = self.store.backfill_calls[0][0]
                self.assertEqual(expected, (call["p_offset"], call["p_limit"]))
                self.assertEqual(expected[0], result["offset"])

        invalid = (
            {"kind": "video"},
            {"offset": "not-an-integer"},
            {"offset": 1.5},
            {"offset": True},
            {"offset": 2147483648},
            {"offset": -2147483649},
            {"limit": "not-an-integer"},
            {"limit": 1.5},
            {"limit": True},
            {"limit": 2147483648},
            {"limit": -2147483649},
        )
        for body in invalid:
            with self.subTest(body=body):
                self.store.backfill_calls.clear()
                with self.assertRaises(ValueError):
                    ai_reviews.handle_post(self.ctx(JUDGE, body, {"action": "backfill"}))
                self.assertEqual([], self.store.backfill_calls)

    def test_backfill_rejects_ordinary_member_without_rpc(self):
        with self.assertRaises(PermissionError):
            ai_reviews.handle_post(self.ctx(MEMBER, {"kind": "uploaded"}, {"action": "backfill"}))
        self.assertEqual([], self.store.backfill_calls)

    def test_backfill_missing_migration_fails_without_submit_fallback(self):
        legacy_review = {
            "workspace_id": WORKSPACE, "user_id": MEMBER, "deck_id": "uploaded-1",
            "data": {"mode": "deck", "scores": {"visual": 4}},
        }
        self.store.reviews[("uploaded-1", MEMBER)] = legacy_review
        self.store.backfill_error = ValueError("PGRST202 function does not exist")
        with self.assertRaisesRegex(ValueError, "数据库迁移"):
            ai_reviews.handle_post(self.ctx(JUDGE, {"kind": "uploaded"}, {"action": "backfill"}))
        self.assertEqual(1, len(self.store.backfill_calls))
        self.assertEqual(legacy_review, self.store.reviews[("uploaded-1", MEMBER)])
        self.assertEqual({}, self.store.submissions)
        self.assertEqual(0, self.store.generate_calls)

    def test_generated_prompt_reads_image_paths_and_checks_page_plan(self):
        deck = self.store.decks["generated-1"]
        deck["data"] = {
            "id": "generated-1",
            "name": "生成",
            "title": "生成",
            "images": [{"id": "g1", "name": "g1.png", "path": WORKSPACE + "/generated-1/g1.png"}],
            "generation": {
                "outline": "先提出问题，再给出方案",
                "style": "克制的黑白现代风格",
                "pages": [
                    {"unitId": "g1", "position": 1, "text": "当前页文案锚点", "title": "方案页"},
                    {"unitId": "g2", "position": 2, "text": "第二页文案"},
                ],
                "references": [
                    {"imagePaths": [WORKSPACE + "/generated-1/ref-a.png", WORKSPACE + "/generated-1/ref-b.png"]},
                    {"imagePath": WORKSPACE + "/generated-1/ref-c.png"},
                ],
            },
        }
        for path in ("ref-a.png", "ref-b.png", "ref-c.png"):
            self.store.storage[WORKSPACE + "/generated-1/" + path] = b"reference"
        self.assertEqual(
            ai_reviews._reference_images(deck),
            [
                WORKSPACE + "/generated-1/ref-a.png",
                WORKSPACE + "/generated-1/ref-b.png",
                WORKSPACE + "/generated-1/ref-c.png",
            ],
        )
        job = {"scope": "page", "unitId": "g1", "position": 1, "sourceKind": "generated", "deckId": "generated-1"}
        messages = ai_reviews._build_messages(
            self.ctx(MEMBER), job, deck, self.store.settings[WORKSPACE],
            self.store.workspaces[WORKSPACE]["settings"], None,
        )
        rendered = json.dumps(messages, ensure_ascii=False)
        self.assertIn("当前页文案锚点", rendered)
        self.assertIn("大纲", rendered)
        self.assertIn("风格", rendered)
        self.assertIn("文字", rendered)
        self.assertIn("当前页对应的生成计划文案", rendered)
        self.assertEqual(rendered.count("data:image/png;base64,"), 4)
        task = json.loads(messages[1]["content"][0]["text"])
        self.assertNotIn("promptBudget", task)
        self.assertEqual("当前页文案锚点", task["generationPagePlan"]["text"])
        self.assertNotIn("budget", task["generationPagePlan"])

    def test_assisted_review_methods_expand_only_system_prompt(self):
        deck = self.store.decks["generated-1"]
        settings = self.store.settings[WORKSPACE]
        workspace_settings = self.store.workspaces[WORKSPACE]["settings"]
        base_job = {
            "scope": "page", "unitId": "g1", "position": 1,
            "sourceKind": "generated", "deckId": "generated-1", "variant": "baseline",
        }
        assisted_job = dict(base_job, variant="assisted")
        deck_assisted_job = dict(assisted_job, scope="deck", unitId="")
        baseline = ai_reviews._build_messages(
            self.ctx(MEMBER), base_job, deck, settings, workspace_settings, None,
        )
        assisted = ai_reviews._build_messages(
            self.ctx(MEMBER), assisted_job, deck, settings, workspace_settings, None,
        )
        deck_assisted = ai_reviews._build_messages(
            self.ctx(MEMBER), deck_assisted_job, deck, settings, workspace_settings, None,
        )

        baseline_system = baseline[0]["content"]
        assisted_system = assisted[0]["content"]
        deck_assisted_system = deck_assisted[0]["content"]
        self.assertEqual(
            baseline_system,
            "你是观页的专职视觉评委，按 GPT-6.2 Sol 的严谨口径进行单粒度预评。"
            "返回纯 JSON，不要 Markdown，不要解释图表之外的内容。",
        )
        markers = (
            "视觉入口", "同类元素的间距与边缘对齐", "图表是否表达内容真正的关系",
            "非对称（不对称）", "不因个人偏好直接判差", "只依据图中可见证据",
        )
        for marker in markers:
            self.assertIn(marker, assisted_system)
            self.assertIn(marker, deck_assisted_system)
            self.assertNotIn(marker, baseline_system)
        self.assertIn("逐页（scope=page）", assisted_system)
        self.assertNotIn("整套（scope=deck）", assisted_system)
        self.assertIn("整套（scope=deck）", deck_assisted_system)
        self.assertNotIn("逐页（scope=page）", deck_assisted_system)

        self.assertEqual(baseline[1], assisted[1])
        serialized_user = json.dumps(assisted[1], ensure_ascii=False)
        for marker in markers:
            self.assertNotIn(marker, serialized_user)

        task = json.loads(assisted[1]["content"][0]["text"])
        self.assertEqual(task["requiredDimensions"], ["visual", "layout", "match"])
        self.assertEqual(task["generatedWeights"], {"visual": 40.0, "layout": 30.0, "match": 30.0})
        self.assertEqual(len(task["rules"]), 4)

        model_result = {
            "scores": {"visual": 4, "layout": 3, "match": 5},
            "classification": {"label": "模型分类"},
            "reasons": ["可见理由"],
            "summary": "预评",
            "confidence": 0.8,
        }
        baseline_result = ai_reviews._normalise_result(model_result, base_job, settings, workspace_settings)
        assisted_result = ai_reviews._normalise_result(model_result, assisted_job, settings, workspace_settings)
        self.assertEqual(baseline_result, assisted_result)
        self.assertEqual(baseline_result["scores"], {"visual": 4.0, "layout": 3.0, "match": 5.0})

        scores_only = {"scores": model_result["scores"]}
        self.assertEqual(ai_reviews._normalise_result(scores_only, base_job, settings, workspace_settings)["reasons"], [])
        for reasons in (None, [], [" "]):
            with self.subTest(reasons=reasons), self.assertRaisesRegex(ValueError, "缺少可核对的评分理由"):
                ai_reviews._normalise_result({**scores_only, "reasons": reasons}, assisted_job, settings, workspace_settings)

    def test_prompt_budget_keeps_valid_json_and_current_page_text(self):
        deck = self.store.decks["generated-1"]
        page_text = '当前页可检查文案："引号" \\ 路径\n第二行\x01' * 500
        deck["data"] = {
            "images": [{"id": "g1", "name": "g1.png", "path": WORKSPACE + "/generated-1/g1.png"}],
            "generation": {
                "outline": "先总后分" * 500,
                "style": {"name": "克制", "summary": '风格摘要："引号" \\ 路径\n' * 500},
                "contentStrategy": "内容策略：" * 500,
                "pages": [{"unitId": "g1", "position": 1, "title": "方案页",
                           "copy": [{"kind": "body", "text": page_text}], "layout": "左文右图" * 300}],
            },
        }
        job = {"scope": "page", "unitId": "g1", "position": 1, "sourceKind": "generated", "deckId": "generated-1"}
        messages = ai_reviews._build_messages(
            self.ctx(MEMBER), job, deck, self.store.settings[WORKSPACE],
            self.store.workspaces[WORKSPACE]["settings"], None,
        )
        text = messages[1]["content"][0]["text"]
        self.assertLessEqual(len(text), ai_reviews.MAX_PROMPT_JSON)
        task = json.loads(text)
        self.assertTrue({
            "modelRole", "sourceKind", "scope", "unitId", "deckId", "maxScore",
            "requiredDimensions", "generatedWeights", "matchChecklist", "rules",
        }.issubset(task))
        self.assertEqual(["visual", "layout", "match"], task["requiredDimensions"])
        self.assertEqual({"visual": 40.0, "layout": 30.0, "match": 30.0}, task["generatedWeights"])
        self.assertEqual((5, 4), (task["maxScore"], len(task["rules"])))
        self.assertTrue(task["promptBudget"]["truncated"])
        page_plan = task["generationPagePlan"]
        self.assertEqual("g1", page_plan["unitId"])
        self.assertTrue(page_plan["truncated"] and page_plan["budget"]["truncated"])
        self.assertIn("当前页可检查文案", page_plan["copy"])

    def test_prompt_budget_keeps_deck_text_and_assisted_calibration(self):
        deck = self.store.decks["generated-1"]
        long_reason = "可见证据：" + "版式层级与文案关系需要核对。" * 20
        samples = [{
            "deckId": f"sample-{index}", "scope": "page", "unitId": f"p{index}",
            "scores": {"visual": 4},
            "classificationSuggestion": {"label": "样本标记" if index == 1 else f"样本{index}"},
            "reasons": [long_reason],
        } for index in range(1, 5)]
        corrections = [{
            "deckId": f"correction-{index}",
            "classification": {"label": "纠偏标记" if index == 1 else f"纠偏{index}"},
            "reasons": [long_reason], "formalScores": {"visual": 4}, "formalTotal": 4,
            "summary": "纠偏摘要：" + "人工判断原因" * 20,
        } for index in range(1, 5)]
        training = {"selectedSamples": 4, "selectedCorrections": 4, "samples": samples, "corrections": corrections}
        deck["data"] = {
            "images": [{"id": "g1", "name": "g1.png", "path": WORKSPACE + "/generated-1/g1.png"}],
            "generation": {
                "outline": "整套路标：" + "先问题后方案" * 400,
                "style": {"name": "克制", "summary": "三段嵌套摘要：" + "风格与版面" * 400},
                "contentStrategy": "内容策略：" * 500,
                "generationBrief": "生成约束：" * 500,
                "pages": [
                    {"unitId": "g1", "position": 1, "title": "封面", "copy": [{"kind": "title", "text": "整套页文案锚点"}]},
                    {"unitId": "g2", "position": 2, "title": "证据", "copy": [{"kind": "body", "text": "第二页文案" * 300}]},
                ],
            },
        }
        job = {"scope": "deck", "unitId": "", "sourceKind": "generated", "deckId": "generated-1", "variant": "assisted"}
        messages = ai_reviews._build_messages(
            self.ctx(MEMBER), job, deck, self.store.settings[WORKSPACE],
            self.store.workspaces[WORKSPACE]["settings"], training,
        )
        text = messages[1]["content"][0]["text"]
        self.assertLessEqual(len(text), ai_reviews.MAX_PROMPT_JSON)
        task = json.loads(text)
        self.assertEqual("deck", task["scope"])
        self.assertIsNone(task["generationPagePlan"])
        self.assertTrue(task["generation"]["summarized"] and task["generation"]["budget"]["truncated"])
        self.assertIn("整套页文案锚点", task["generation"]["summary"])
        calibration_text = json.dumps(task["calibration"], ensure_ascii=False)
        self.assertTrue(task["calibration"]["budget"]["truncated"])
        self.assertIn("样本标记", calibration_text)
        self.assertIn("纠偏标记", calibration_text)
        baseline = ai_reviews._build_messages(
            self.ctx(MEMBER), dict(job, variant="baseline"), deck,
            self.store.settings[WORKSPACE], self.store.workspaces[WORKSPACE]["settings"], None,
        )
        self.assertNotIn("纠偏标记", baseline[1]["content"][0]["text"])

    def test_assisted_run_uses_claimed_track_calibration_and_images(self):
        calibration_dir = WORKSPACE + "/calibration"

        def correction(name, path, **overrides):
            item = {
                "correctionId": "private-" + name, "track": "generated", "split": "calibration",
                "deckId": "other-generated", "scope": "page", "previewPath": path,
                "status": "corrected", "formalScores": {"visual": 5}, "maxScore": 5,
                "formalTotal": 5, "scoreRatio": 1, "classification": {"label": "正式纠偏"},
                "style": "克制", "reasons": ["可见校准理由"], "reasonOther": "",
                "summary": "正式纠偏摘要", "sameClassification": True, "sameStyle": True,
            }
            item.update(overrides)
            return item

        def sample(name, path, **overrides):
            item = {
                "sampleId": "private-" + name, "track": "generated", "split": "calibration",
                "deckId": "other-generated", "scope": "page", "previewPath": path,
                "scores": {"visual": 4}, "maxScore": 5, "classification": {"label": "样本分类"},
                "style": "克制", "sameClassification": True, "sameStyle": True,
            }
            item.update(overrides)
            return item

        self.store.calibrations[("generated", "generated-1", "page")] = {
            "samples": [
                sample("sample-1", calibration_dir + "/oversized.png"),
                sample("sample-2", calibration_dir + "/sample-2.png"),
                sample("sample-3", calibration_dir + "/sample-3.png"),
                sample("off-track", calibration_dir + "/off-track.png", track="uploaded", summary="非法异轨"),
            ],
            "corrections": [
                correction("correction-1", calibration_dir + "/correction-1.png"),
                correction("correction-2", calibration_dir + "/missing.png"),
                correction("same-target", calibration_dir + "/same.png", deckId="generated-1", summary="非法同目标"),
                correction("holdout", calibration_dir + "/holdout.png", split="holdout", summary="非法 holdout"),
            ],
        }
        self.store.storage[calibration_dir + "/correction-1.png"] = b"correction-one"
        self.store.storage[calibration_dir + "/sample-2.png"] = b"sample-two"
        self.store.storage[calibration_dir + "/sample-3.png"] = b"sample-three"
        self.store.storage[calibration_dir + "/oversized.png"] = b"x" * (ai_reviews.MAX_CALIBRATION_IMAGE_BYTES + 1)
        self._seed_job("assisted", scope="page", unit_id="g1", variant="assisted", bucket=1, split="calibration")
        outcome = ai_reviews.run_review_attempt(self.ctx(JUDGE), "generated")
        self.assertEqual("completed", outcome["status"])
        self.assertEqual(1, len(self.store.calibration_calls))
        call = self.store.calibration_calls[0]
        self.assertEqual(("generated", "generated-1", "page", 8, JUDGE), (
            call["p_kind"], call["p_deck_id"], call["p_scope"], call["p_limit"], call["actor"],
        ))
        assisted = self.store.model_messages[-1]
        user_text = assisted[1]["content"][0]["text"]
        task = json.loads(user_text)
        calibration = task["calibration"]
        self.assertEqual(2, calibration["selectedCorrections"])
        self.assertEqual(3, calibration["selectedSamples"])
        self.assertEqual(
            ["cal-correction-1", "cal-correction-2", "cal-sample-1", "cal-sample-2", "cal-sample-3"],
            [item["refId"] for item in calibration["corrections"] + calibration["samples"]],
        )
        labels = [
            item["text"] for item in assisted[1]["content"]
            if item.get("type") == "text" and item["text"].startswith("人工评分示例")
        ]
        self.assertEqual(["人工评分示例 cal-correction-1", "人工评分示例 cal-sample-2"], [label.split("（")[0] for label in labels])
        self.assertIn("不是当前页", "".join(labels))
        self.assertEqual(3, json.dumps(assisted, ensure_ascii=False).count("data:image/png;base64,"))
        self.assertNotIn("previewPath", user_text)
        self.assertNotIn("private-", user_text)
        self.assertNotIn("非法", user_text)

        self._seed_job("baseline", scope="page", unit_id="g1", variant="baseline", bucket=1, split="calibration")
        baseline_outcome = ai_reviews.run_review_attempt(self.ctx(JUDGE), "generated")
        self.assertEqual("completed", baseline_outcome["status"])
        self.assertEqual(1, len(self.store.calibration_calls))
        baseline_text = self.store.model_messages[-1][1]["content"][0]["text"]
        self.assertNotIn("cal-correction", baseline_text)
        self.assertNotIn("人工评分示例", json.dumps(self.store.model_messages[-1], ensure_ascii=False))

    def test_calibration_get_is_scoped_path_free_and_judge_only(self):
        self.store.calibrations[("uploaded", "uploaded-1", "deck")] = {
            "samples": [{
                "sampleId": "private-sample", "track": "uploaded", "split": "calibration",
                "deckId": "other-uploaded", "scope": "deck", "previewPath": WORKSPACE + "/calibration/a.png",
                "scores": {"visual": 4}, "maxScore": 5, "classification": {"label": "保留"},
            }, {
                "sampleId": "private-off-track", "track": "generated", "split": "calibration",
                "deckId": "other-uploaded", "scope": "deck", "previewPath": WORKSPACE + "/calibration/b.png",
                "scores": {"visual": 1}, "maxScore": 5,
            }, {
                "sampleId": "private-holdout", "track": "uploaded", "split": "holdout",
                "deckId": "other-uploaded", "scope": "deck", "previewPath": WORKSPACE + "/calibration/c.png",
                "scores": {"visual": 1}, "maxScore": 5,
            }],
            "corrections": [],
        }
        query = {"action": "samples", "kind": "uploaded", "deckId": "uploaded-1", "scope": "deck"}
        with self.assertRaises(PermissionError):
            ai_reviews.handle_get(self.ctx(MEMBER, query=query))
        with self.assertRaises(ValueError):
            ai_reviews.handle_get(self.ctx(JUDGE, query={"action": "samples", "kind": "uploaded", "scope": "deck"}))
        result = ai_reviews.handle_get(self.ctx(JUDGE, query=query))
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertEqual({"uploaded"}, {item["track"] for item in result["samples"]})
        self.assertEqual(1, len(result["samples"]))
        self.assertNotIn("previewPath", serialized)
        self.assertNotIn("sampleId", serialized)
        self.assertNotIn("correctionId", serialized)
        self.assertNotIn("deckId", serialized)

    def test_calibration_get_rejects_invalid_deck_id_contract(self):
        base = {"action": "samples", "kind": "uploaded", "scope": "deck"}
        with self.assertRaisesRegex(ValueError, "需要 deckId"):
            ai_reviews.handle_get(self.ctx(JUDGE, query=dict(base)))
        for alias in ("deckId", "targetId", "deck_id"):
            for deck_id in (None, "", 3, True, "x" * 101):
                query = dict(base, **{alias: deck_id})
                with self.subTest(alias=alias, deck_id=repr(deck_id)):
                    with self.assertRaisesRegex(ValueError, "需要 deckId"):
                        ai_reviews.handle_get(self.ctx(JUDGE, query=query))

    def test_calibration_target_exclusion_preserves_exact_space_sensitive_ids(self):
        def sample(deck_id):
            return {
                "track": "uploaded", "split": "calibration", "deckId": deck_id,
                "scope": "deck", "scores": {"visual": 4}, "maxScore": 5,
            }

        target = " target α "
        self.store.calibrations[("uploaded", target, "deck")] = {
            "samples": [
                sample(target), sample("target α"), sample(" target α"),
                sample("target α "), sample("  target α  "), sample("   "), sample(""),
            ],
            "corrections": [],
        }
        for alias in ("deckId", "targetId", "deck_id"):
            query = {"action": "samples", "kind": "uploaded", alias: target, "scope": "deck"}
            result = ai_reviews.handle_get(self.ctx(JUDGE, query=query))
            self.assertEqual(5, result["selectedSamples"])
            self.assertEqual(5, len(result["samples"]))
            self.assertEqual(target, self.store.calibration_calls[-1]["p_deck_id"])

        space_target = "   "
        self.store.calibrations[("uploaded", space_target, "deck")] = {
            "samples": [sample(""), sample(" "), sample("  "), sample(space_target), sample("    ")],
            "corrections": [],
        }
        result = ai_reviews.handle_get(self.ctx(JUDGE, query={
            "action": "samples", "kind": "uploaded", "deck_id": space_target, "scope": "deck",
        }))
        self.assertEqual(3, result["selectedSamples"])
        self.assertEqual(3, len(result["samples"]))
        self.assertEqual(space_target, self.store.calibration_calls[-1]["p_deck_id"])

    def test_import_ppt_uses_converter_and_storage(self):
        source = b"dummy-pptx"
        payload = {
            "name": "临时测试",
            "source_group_sha256": "f" * 64,
            "sourceGroupSha256": "e" * 64,
            "files": [{
                "name": "temp.pptx",
                "data": base64.b64encode(source).decode("ascii"),
                "source_group_sha256": "d" * 64,
            }],
        }

        def fake_convert(source_path, output_dir):
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "page-0001.png").write_bytes(b"page-one")
            (output_dir / "page-0002.png").write_bytes(b"page-two")

        with mock.patch.object(ai_reviews, "_convert_document", side_effect=fake_convert):
            result = ai_reviews.handle_import(self.ctx(MEMBER, payload))
        self.assertTrue(result["ok"])
        self.assertEqual(result["pageCount"], 2)
        self.assertTrue(result["deckId"].startswith("import-"))
        self.assertEqual(sum(1 for path in self.store.storage if result["deckId"] in path), 2)
        self.assertEqual(1, len(self.store.import_provenance_calls))
        self.assertEqual([], self.store.legacy_add_calls)
        params, privileged = self.store.import_provenance_calls[0]
        self.assertTrue(privileged)
        self.assertEqual(WORKSPACE, params["w"])
        self.assertEqual(MEMBER, params["actor"])
        self.assertEqual(hashlib.sha256(source).hexdigest(), params["p_source_group_sha256"])
        self.assertNotIn("source_group_sha256", params["deck"])
        self.assertNotIn("sourceGroupSha256", params["deck"])

    def test_multi_file_import_keeps_legacy_merged_rpc_without_provenance(self):
        files = [("first.pptx", b"first-source"), ("second.pptx", b"second-source")]
        payload = {"name": "合并测试", "files": [
            {"name": name, "data": base64.b64encode(data).decode("ascii")}
            for name, data in files
        ]}

        def fake_convert(source_path, output_dir):
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "page-0001.png").write_bytes(source_path.read_bytes())

        with mock.patch.object(ai_reviews, "_convert_document", side_effect=fake_convert):
            result = ai_reviews.handle_import(self.ctx(MEMBER, payload))
        self.assertEqual(2, result["pageCount"])
        self.assertEqual([], self.store.import_provenance_calls)
        self.assertEqual(1, len(self.store.legacy_add_calls))
        params, privileged = self.store.legacy_add_calls[0]
        self.assertFalse(privileged)
        self.assertNotIn("source_group_sha256", params["deck"])
        self.assertEqual(2, len(params["deck"]["images"]))

    def test_single_file_import_missing_provenance_rpc_does_not_fallback(self):
        source = b"dummy-pptx"
        payload = {"files": [{"name": "temp.pptx", "data": base64.b64encode(source).decode("ascii")}]}
        self.store.import_provenance_error = ValueError("PGRST202 could not find the function")

        def fake_convert(source_path, output_dir):
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "page-0001.png").write_bytes(b"page")

        with mock.patch.object(ai_reviews, "_convert_document", side_effect=fake_convert):
            with self.assertRaisesRegex(ValueError, "请先执行最新数据库迁移"):
                ai_reviews.handle_import(self.ctx(MEMBER, payload))
        self.assertEqual([], self.store.legacy_add_calls)

    def test_library_route_is_member_compatible_read(self):
        result = ai_reviews.handle_get(self.ctx(MEMBER, query={"action": "library"}))
        self.assertEqual(result["schemaVersion"], 1)
        self.assertEqual([deck["id"] for deck in result["decks"]], ["uploaded-1"])


    def test_comparison_and_stats_use_single_privileged_workspace_rpc(self):
        expected = json.loads(json.dumps(self.store.comparison_result))
        expected.update({"holdoutUnits": 2, "baseline": {"count": 2, "mae": 0.12, "dimensions": {"visual": 0.1}}})
        expected.update({"assisted": {"count": 2, "mae": 0.09, "dimensions": {"visual": 0.08}}, "improvement": 0.03})
        expected["byKind"]["uploaded"] = {"holdoutUnits": 2, "baseline": expected["baseline"], "assisted": expected["assisted"], "improvement": 0.03}
        self.store.comparison_result = expected
        for action in ("comparison", "stats"):
            with self.subTest(action=action):
                self.store.comparison_calls.clear()
                result = ai_reviews.handle_get(self.ctx(MEMBER, body={"w": "client-body"}, query={
                    "action": action, "kind": "uploaded", "w": "client-query",
                }))
                self.assertEqual(expected, result)
                self.assertEqual([({"w": WORKSPACE}, True)], self.store.comparison_calls)

    def test_comparison_empty_bad_response_and_rpc_failure_are_reported(self):
        empty = self.store.comparison_result
        self.assertEqual(empty, ai_reviews.handle_get(self.ctx(MEMBER, query={"action": "comparison"})))

        with self.subTest(response=[]), self.assertRaisesRegex(ValueError, "返回格式无效"):
            self.store.comparison_result = []
            ai_reviews.handle_get(self.ctx(MEMBER, query={"action": "comparison"}))
        with self.subTest(response="partial"), self.assertRaisesRegex(ValueError, "返回格式无效"):
            self.store.comparison_result = {"holdoutUnits": 0}
            ai_reviews.handle_get(self.ctx(MEMBER, query={"action": "stats"}))

        self.store.comparison_result = empty
        self.store.comparison_error = KeyError("ai_review_comparison")
        with self.assertRaises(KeyError):
            ai_reviews.handle_get(self.ctx(MEMBER, query={"action": "comparison"}))
        self.store.comparison_error = RuntimeError("comparison unavailable")
        with self.assertRaisesRegex(RuntimeError, "comparison unavailable"):
            ai_reviews.handle_get(self.ctx(MEMBER, query={"action": "comparison"}))

    def test_comparison_anonymous_is_denied_without_rpc(self):
        with self.assertRaises(PermissionError):
            ai_reviews.handle_get(self.ctx("", query={"action": "comparison"}))
        self.assertEqual([], self.store.comparison_calls)

if __name__ == "__main__":
    unittest.main(verbosity=2)
