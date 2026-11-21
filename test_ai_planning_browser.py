"""Edge/Chromium regression for planning history, draft saving and generation handoff."""

from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    expect = sync_playwright = None


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "public" / "ai-planning.js"
EDGE_CANDIDATES = [
    Path(os.environ.get("GUANYE_TEST_EDGE", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")),
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
]
PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"></head><body>
<main id="planning"></main>
<script src="/ai-planning.js"></script>
<script>
window.__planningEvents = [];
for (const name of ['guanye:planning-opened', 'guanye:planning-saved', 'guanye:planning-confirmed', 'guanye:generation-created']) {
  window.addEventListener(name, event => window.__planningEvents.push({name, detail: event.detail}));
}
window.addEventListener('guanye:generation-created', event => { window.__generationEvent = event.detail; });
window.mountPlanning = () => {
  const root = document.querySelector('#planning');
  return window.GuanyePlanning.mount(root, {
    request: window.GuanyeAI,
    onGenerationSubmitted: (payload, state) => {
      window.__generationSubmitted = {payload, state};
    },
  });
};
window.planning = window.mountPlanning();
</script>
</body></html>"""


def browser_path():
    for candidate in EDGE_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def draft(plan_id, title, revision=1, summary="摘要"):
    return {
        "schemaVersion": 1,
        "planId": plan_id,
        "title": title,
        "summary": summary,
        "generationBrief": "按确认稿生成",
        "mode": "content_draft",
        "pageCount": 1,
        "style": {"name": "深绿", "summary": "说明", "layoutLanguage": "十二栏", "palette": ["#173F35"]},
        "decisionBasis": {"contentAudience": "管理层", "userReferences": ["用户资料"], "libraryEvidence": ["高分参考"], "tradeoffs": ["少装饰"]},
        "designDirections": [{"name": "证据先行", "summary": "先结论后证据", "formLogic": "数据页用条形图", "contentAudience": "管理层", "userReferenceSupport": ["用户资料"], "libraryEvidence": ["高分参考"], "rationale": "匹配内容", "tradeoffs": ["冲击较弱"]}],
        "pages": [{"index": 1, "title": "封面", "narrativeRole": "cover", "copy": [{"kind": "title", "text": "封面"}], "layout": "左文右图", "visualBrief": "细线图", "contentSources": ["资料"], "styleReferenceDeckIds": ["reference"]}],
        "references": [{"deckId": "reference", "title": "高分模板", "score": 4.75, "completeReviewCount": 2, "pageCount": 6}],
        "warnings": [],
        "assumptions": [],
        "sourceManifest": [],
        "generationStyleImages": [],
        "revision": revision,
    }


def two_page_draft(plan_id, title, revision=1, summary="摘要"):
    plan = draft(plan_id, title, revision=revision, summary=summary)
    second = json.loads(json.dumps(plan["pages"][0], ensure_ascii=False))
    second["index"] = 2
    second["title"] = "第二页"
    plan["pages"].append(second)
    plan["pageCount"] = 2
    return plan


def history_item(plan_id, title, status="draft", revision=1, mode="content_draft", page_count=1, created_at="2026-11-21T00:00:00Z"):
    return {
        "planId": plan_id,
        "title": title,
        "mode": mode,
        "pageCount": page_count,
        "status": status,
        "revision": revision,
        "createdAt": created_at,
    }


def confirmed_payload(plan_id, title):
    plan = draft(plan_id, title)
    return {
        "planId": plan_id,
        "status": "confirmed",
        "revision": 2,
        "draft": plan,
        "createdAt": "2026-11-21T00:00:00Z",
        "confirmed": {
            "versionId": "version-1",
            "version": 1,
            "contentHash": "a" * 64,
            "confirmedAt": "2026-11-21T00:00:00Z",
            "payload": plan,
        },
    }


@unittest.skipUnless(sync_playwright and browser_path(), "requires Playwright and Microsoft Edge/Chrome")
class PlanningBrowserTest(unittest.TestCase):
    def _new_page(self, playwright, respond):
        browser = playwright.chromium.launch(executable_path=str(browser_path()), headless=True)
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", respond)
        page.goto("http://planning.test/")
        expect(page.get_by_text("我的策划", exact=True)).to_be_visible()
        return browser, page, errors

    @staticmethod
    def _serve_shell(route):
        path = urlsplit(route.request.url).path
        if path == "/":
            route.fulfill(content_type="text/html; charset=utf-8", body=PAGE)
            return True
        if path == "/ai-planning.js":
            route.fulfill(content_type="application/javascript; charset=utf-8", body=SCRIPT.read_text(encoding="utf-8"))
            return True
        return False

    def test_history_pagination_refresh_selected_and_stale_open(self):
        history_requests = []
        pending_plan_a = []
        history_head = [history_item("plan-a", "第一份策划"), history_item("plan-b", "第二份策划")]

        def respond(route):
            if self._serve_shell(route):
                return
            url = urlsplit(route.request.url)
            if url.hostname != "planning.test":
                route.abort()
                return
            query = parse_qs(url.query)
            if url.path == "/api/ai/planning/history":
                cursor = query.get("cursor", [""])[0]
                history_requests.append(cursor)
                if cursor == "":
                    route.fulfill(json={"items": history_head, "nextCursor": "cursor-1"})
                elif cursor == "cursor-1":
                    route.fulfill(json={"items": [history_item("plan-c", "第三份策划", revision=3)], "nextCursor": None})
                else:
                    route.fulfill(status=400, json={"error": "unexpected cursor"})
                return
            if url.path == "/api/ai/planning":
                plan_id = query.get("plan_id", [""])[0]
                if plan_id == "plan-a" and not pending_plan_a:
                    pending_plan_a.append(route)
                    return
                titles = {"plan-a": "第一份策划", "plan-b": "第二份策划", "plan-c": "第三份策划"}
                route.fulfill(json={"planId": plan_id, "status": "draft", "revision": 1, "draft": draft(plan_id, titles.get(plan_id, "未命名策划"))})
                return
            route.fulfill(status=404, json={"error": "unhandled fixture request"})

        with sync_playwright() as playwright:
            browser, page, errors = self._new_page(playwright, respond)
            expect(page.get_by_text("第一份策划", exact=True)).to_be_visible()
            with page.expect_request(lambda request: "cursor=cursor-1" in request.url):
                page.locator('[data-gp-history-action="more"]').click()
            expect(page.get_by_text("第三份策划", exact=True)).to_be_visible()
            self.assertEqual(page.locator('[data-plan-id="plan-c"]').count(), 1)

            page.locator('[data-gp-history-open="plan-c"]').click()
            expect(page.locator('[data-gp-plan-field="title"]')).to_have_value("第三份策划")
            page.locator('[data-gp-history-open="plan-a"]').click()
            expect(page.locator('[data-gp-plan-field="title"]')).to_be_disabled()
            page.locator('[data-gp-history-open="plan-b"]').click()
            expect(page.locator('[data-gp-plan-field="title"]')).to_have_value("第二份策划")
            pending_plan_a[0].fulfill(json={"planId": "plan-a", "status": "draft", "revision": 1, "draft": draft("plan-a", "第一份策划")})
            page.wait_for_timeout(100)
            expect(page.locator('[data-gp-plan-field="title"]')).to_have_value("第二份策划")

            history_head = [history_item("plan-new", "刷新后的新策划"), history_item("plan-a", "第一份策划")]
            with page.expect_request(lambda request: urlsplit(request.url).path == "/api/ai/planning/history" and "cursor=" not in request.url):
                page.locator('[data-gp-history-action="refresh"]').click()
            expect(page.get_by_text("刷新后的新策划", exact=True)).to_be_visible()
            self.assertEqual(page.locator('[data-plan-id="plan-b"]').count(), 1)
            expect(page.locator('[data-plan-id="plan-b"]')).to_have_attribute("data-selected", "true")
            self.assertEqual(page.locator('[data-plan-id="plan-a"]').count(), 1)

            page.evaluate("window.planning.destroy(); window.planning = window.mountPlanning()")
            expect(page.get_by_text("刷新后的新策划", exact=True)).to_be_visible()
            page.locator('[data-gp-history-open="plan-a"]').click()
            expect(page.locator('[data-gp-plan-field="title"]')).to_have_value("第一份策划")
            self.assertEqual(errors, [])
            browser.close()

    def test_save_retains_edit_on_conflict_and_refreshes_server_revision(self):
        server_history = history_item("plan-save", "可保存策划")
        save_bodies = []
        first_save = []

        def respond(route):
            if self._serve_shell(route):
                return
            url = urlsplit(route.request.url)
            query = parse_qs(url.query)
            if url.path == "/api/ai/planning/history":
                route.fulfill(json={"items": [server_history], "nextCursor": None})
                return
            if url.path == "/api/ai/planning" and query.get("plan_id", [""])[0] == "plan-save":
                route.fulfill(json={"planId": "plan-save", "status": "draft", "revision": 1, "draft": two_page_draft("plan-save", "可保存策划")})
                return
            if url.path == "/api/ai/planning/save":
                save_bodies.append(json.loads(route.request.post_data))
                if len(save_bodies) == 1:
                    first_save.append(route)
                    return
                route.fulfill(json={"planId": "plan-save", "status": "draft", "revision": 2, "draft": two_page_draft("plan-save", "服务器清理标题", revision=2, summary="服务器清理摘要")})
                return
            route.fulfill(status=404, json={"error": "unhandled fixture request"})

        with sync_playwright() as playwright:
            browser, page, errors = self._new_page(playwright, respond)
            expect(page.get_by_text("可保存策划", exact=True)).to_be_visible()
            page.locator('[data-gp-history-open="plan-save"]').click()
            title = page.locator('[data-gp-plan-field="title"]')
            expect(title).to_have_value("可保存策划")
            title.fill("本地未保存标题")
            save = page.locator('[data-gp-action="save"]')
            with page.expect_request(lambda request: urlsplit(request.url).path == "/api/ai/planning/save"):
                save.click()
            for _ in range(100):
                if first_save:
                    break
                page.wait_for_timeout(10)
            self.assertEqual(len(first_save), 1)
            expect(save).to_be_disabled()
            expect(save).to_have_text("保存中…")
            expect(page.locator('[data-gp-plan-field="title"]')).to_be_disabled()
            expect(page.locator('[data-gp-plan-field="summary"]')).to_be_disabled()
            expect(page.locator('[data-gp-page-field="title"]')).to_have_count(2)
            self.assertTrue(page.locator('[data-gp-page-field="title"]').evaluate_all("els => els.every(el => el.disabled)"))
            self.assertTrue(page.locator('[data-gp-role="narrativeRole"]').evaluate_all("els => els.every(el => el.disabled)"))
            self.assertTrue(page.locator('[data-gp-copy-kind="title"]').evaluate_all("els => els.every(el => el.disabled)"))
            self.assertTrue(page.locator('[data-gp-page-field="layout"]').evaluate_all("els => els.every(el => el.disabled)"))
            self.assertTrue(page.locator('[data-gp-action="up"], [data-gp-action="down"]').evaluate_all("els => els.every(el => el.disabled)"))
            first_save[0].fulfill(status=409, json={"error": "策划草稿已在其他窗口更新，请刷新后重试"})
            expect(page.locator("#gp-status")).to_contain_text("当前输入已保留")
            expect(page.locator("#gp-status")).to_contain_text("打开会替换当前内容")
            expect(title).to_have_value("本地未保存标题")
            expect(save).to_be_enabled()
            expect(title).to_be_enabled()
            expect(page.locator('[data-gp-action="up"]').nth(0)).to_be_disabled()
            expect(page.locator('[data-gp-action="up"]').nth(1)).to_be_enabled()
            expect(page.locator('[data-gp-action="down"]').nth(0)).to_be_enabled()
            expect(page.locator('[data-gp-action="down"]').nth(1)).to_be_disabled()
            save.click()
            expect(title).to_have_value("服务器清理标题")
            expect(page.locator('[data-gp-plan-field="summary"]')).to_have_value("服务器清理摘要")
            self.assertEqual(save_bodies[0]["expected_revision"], 1)
            self.assertEqual(save_bodies[0]["draft"]["title"], "本地未保存标题")
            self.assertEqual(save_bodies[1]["expected_revision"], 1)
            expect(page.locator('[data-plan-id="plan-save"]')).to_have_attribute("data-revision", "2")

            server_history["revision"] = 2
            server_history["title"] = "服务器清理标题"
            with page.expect_request(lambda request: urlsplit(request.url).path == "/api/ai/planning/history" and "cursor=" not in request.url):
                page.locator('[data-gp-history-action="refresh"]').click()
            expect(page.locator('[data-plan-id="plan-save"]')).to_have_attribute("data-revision", "2")
            expect(title).to_have_value("服务器清理标题")
            self.assertEqual(errors, [])
            browser.close()

    def test_confirmed_history_opens_and_202_generation_handoff(self):
        generation_bodies = []

        def respond(route):
            if self._serve_shell(route):
                return
            url = urlsplit(route.request.url)
            query = parse_qs(url.query)
            if url.path == "/api/ai/planning/history":
                route.fulfill(json={"items": [history_item("plan-confirmed", "已确认策划", status="confirmed", revision=2)], "nextCursor": None})
                return
            if url.path == "/api/ai/planning" and query.get("plan_id", [""])[0] == "plan-confirmed":
                route.fulfill(json=confirmed_payload("plan-confirmed", "已确认策划"))
                return
            if url.path == "/api/ai/generation":
                generation_bodies.append(json.loads(route.request.post_data))
                route.fulfill(status=202, json={"id": "job-202"})
                return
            route.fulfill(status=404, json={"error": "unhandled fixture request"})

        with sync_playwright() as playwright:
            browser, page, errors = self._new_page(playwright, respond)
            expect(page.get_by_text("已确认策划", exact=True)).to_be_visible()
            page.evaluate("window.planning.destroy(); window.planning = window.mountPlanning()")
            expect(page.get_by_text("已确认策划", exact=True)).to_be_visible()
            page.locator('[data-gp-history-open="plan-confirmed"]').click()
            expect(page.get_by_text("已确认 · 不可变版本", exact=True)).to_be_visible()
            generate = page.locator('[data-gp-action="generate"]')
            expect(generate).to_be_enabled()
            self.assertEqual(page.locator('[data-gp-action="save"]').count(), 0)
            with page.expect_request(lambda request: urlsplit(request.url).path == "/api/ai/generation"):
                generate.click()
            page.wait_for_function("window.__generationEvent?.jobId === 'job-202' && window.__generationEvent?.planId === 'plan-confirmed'")
            page.wait_for_function("window.__generationSubmitted?.payload?.id === 'job-202'")
            self.assertEqual(generation_bodies, [{"plan_id": "plan-confirmed"}])
            self.assertEqual(errors, [])
            browser.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
