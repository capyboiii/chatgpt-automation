"""Browser smoke test for the unified UI; requires a local server on port 8765.

API writes are intercepted so this never launches ChatGPT or changes user data.
"""

from playwright.sync_api import sync_playwright


def main():
    errors = []
    state = {"started": False, "stopped": False, "stop_failure": True}

    def jobs(route):
        if not state["started"]:
            collections = []
        else:
            collections = [{
                "id": "test-row", "name": "Demo", "free_sequence": True,
                "status": "paused" if state["stopped"] else "running",
                "done_count": 0, "total_count": 2,
                "jobs": [{"status": "done" if state["stopped"] else "running",
                          "prompt_name": "PROMPT A", "folder": "Demo",
                          "results": ["/files/outputs/Demo/1.png"] if state["stopped"] else [],
                          "error": None},
                         {"status": "pending", "prompt_name": "PROMPT B",
                          "folder": "Demo", "results": [], "error": None}],
            }]
        route.fulfill(json={"active": state["started"] and not state["stopped"],
                            "stopped": state["stopped"],
                            "collections": collections,
                            "fleet": {"acc1": {"status": "busy", "collection_name": "Demo"}},
                            "exhausted": [], "thinking_level": "cao",
                            "thinking_state": {"acc1": "confirmed"} if state["started"] else {},
                            "warnings": []})

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/jobs", jobs)
        page.route("**/api/profiles", lambda route: route.fulfill(json={"profiles": [{"name": "acc1"}]}))
        page.route("**/api/templates", lambda route: route.fulfill(json={
            "groups": [], "items": [
                {"rel": "demo/a.png", "name": "a.png", "url": "/assets/a.png"},
                {"rel": "demo/b.png", "name": "b.png", "url": "/assets/b.png"},
            ]}))
        page.route("**/api/csv-session/start", lambda route: (
            state.update(started=True),
            route.fulfill(json={"started": 1, "prompt_columns": ["PROMPT A", "PROMPT B"]})))
        page.route("**/api/csv-session/preview", lambda route: route.fulfill(json={
            "headers": ["PROMPT A", "PROMPT B", "COLLECTION", "SIZE / VARIANT"], "rows": 1,
            "suggested_prompts": ["PROMPT A", "PROMPT B"],
            "suggested_size_chart": "SIZE / VARIANT"}))
        def stop(route):
            if state["stop_failure"]:
                state["stop_failure"] = False
                route.fulfill(status=500, json={"detail": "Lỗi dừng thử nghiệm"})
            else:
                state["stopped"] = True
                route.fulfill(json={"ok": True})

        page.route("**/api/jobs/stop", stop)
        page.route("**/api/csv-session/test-row/resume", lambda route: (
            state.update(stopped=False), route.fulfill(json={"resumed": 1, "enqueued": True})))
        page.goto("http://127.0.0.1:8765/csv-session")
        page.locator("#btn-csv").wait_for(state="visible")
        page.locator("#btn-img").click()
        assert page.locator("#attach-panel").is_visible()
        page.locator(".attachment-library summary").click()
        page.locator("#attach-grid .csvs-thumb").first.click()
        assert page.locator("#selected-attachments .attachment-chip").count() == 1
        page.locator("#selected-attachments [data-remove]").click()
        assert page.locator("#selected-attachments .attachment-chip").count() == 0
        page.locator("#btn-close").click()
        assert not page.locator("#attach-panel").is_visible()
        page.set_input_files("#csv-input", {"name": "demo.csv", "mimeType": "text/csv",
                                          "buffer": b"PROMPT A,PROMPT B,COLLECTION,SIZE / VARIANT\na,b,Demo,XL\n"})
        page.locator("#csv-field-panel").wait_for(state="visible")
        assert not page.locator("#csv-size-enabled").is_checked()
        assert "2 yêu cầu" in page.locator("#btn-start-csv").inner_text()
        page.locator("#csv-size-enabled").check()
        assert "2 yêu cầu + bảng kích thước" in page.locator("#btn-start-csv").inner_text()
        page.locator("#csv-size-enabled").uncheck()
        page.locator("#btn-start-csv").click()
        page.locator("#list .collection-card").wait_for()
        assert "PROMPT B" in page.locator("#list").inner_text()
        assert page.locator("#thinking-card").count() == 0
        assert page.locator("#fleet-chips").is_hidden()
        assert page.locator("#btn-stop").is_enabled()
        page.locator("#btn-stop").click()
        page.get_by_text("Lỗi dừng thử nghiệm").wait_for()
        assert not state["stopped"]
        page.locator("#btn-stop").click()
        page.wait_for_timeout(2500)
        page.locator("#list .csvs-preview-link").click()
        page.locator("#csv-image-viewer").wait_for(state="visible")
        page.keyboard.press("Escape")
        page.locator("#csv-image-viewer").wait_for(state="hidden")
        assert page.locator("#btn-resume").is_enabled()
        assert page.locator("#list .csvs-retry").is_enabled()
        assert page.locator("#list .csvs-regenerate").count() == 1
        assert "Đã dừng" in page.locator("#run-text").inner_text()
        page.locator("#list .row-action-menu summary").click()
        page.locator("#list .csvs-retry").click()
        page.wait_for_function("!document.querySelector('#btn-stop').disabled")
        assert not state["stopped"]
        assert not errors, errors
        for path in ("/mockup", "/csv-session"):
            other = browser.new_page()
            page_errors = []
            other.on("pageerror", lambda error: page_errors.append(str(error)))
            response = other.goto(f"http://127.0.0.1:8765{path}")
            assert response.ok, (path, response.status)
            if path == "/mockup":
                assert other.locator(".workflow-run-panel #run-btn").count() == 1
                assert other.locator("#results-panel #btn-clear-jobs").count() == 1
            else:
                assert other.locator(".workflow-block #attach-panel").count() == 1
                assert other.locator(".workflow-submit #btn-start-csv").count() == 1
                assert other.locator("#results-panel #btn-clear-all").count() == 1
            other.wait_for_timeout(700)
            assert not page_errors, (path, page_errors)
            other.close()
        workspace = browser.new_page()
        workspace_errors = []
        workspace.on("pageerror", lambda error: workspace_errors.append(str(error)))
        workspace.route("**/api/profiles/manual-login", lambda route: route.fulfill(
            json={"profile": "acc99", "opened": True, "auto": False}))
        workspace.goto("http://127.0.0.1:8765/")
        assert workspace.locator("#tab-csv-session").get_attribute("aria-selected") == "true"
        workspace.wait_for_timeout(600)
        assert workspace.locator("#btn-accounts").is_visible()
        assert "Tài khoản" in workspace.locator("#btn-accounts").inner_text()
        workspace.locator("#tab-mockup").click()
        workspace.frame_locator("#frame-mockup").locator("#open-accounts").wait_for(state="attached")
        workspace.locator("#btn-accounts").click()
        workspace.frame_locator("#frame-mockup").locator("#accounts-drawer").wait_for(state="visible")
        workspace.frame_locator("#frame-mockup").locator("#open-manual-login").click()
        workspace.frame_locator("#frame-mockup").locator("#toast-container").get_by_text(
            "Đã mở Chrome cho acc99", exact=False).wait_for()
        workspace.locator("#tab-csv-session").click()
        workspace.frame_locator("#frame-csv-session").locator("#btn-csv").wait_for()
        workspace.locator("#theme-toggle").click()
        assert workspace.locator("html").get_attribute("data-theme") == "light"
        for mode in ("mockup", "csv-session"):
            frame = workspace.frame_locator(f"#frame-{mode}")
            frame.locator("html[data-theme='light']").wait_for(state="attached")
        workspace.reload()
        assert workspace.locator("html").get_attribute("data-theme") == "light"
        workspace.frame_locator("#frame-csv-session").locator("html[data-theme='light']").wait_for()
        workspace.locator("#theme-toggle").click()
        workspace.frame_locator("#frame-csv-session").locator("html[data-theme='dark']").wait_for()
        assert not workspace_errors, workspace_errors
        workspace.close()
        mobile = browser.new_page(viewport={"width": 390, "height": 844})
        mobile.goto("http://127.0.0.1:8765/")
        assert mobile.locator("#theme-toggle").is_visible()
        assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        mobile.close()
        live = browser.new_page()
        live_errors = []
        live.on("pageerror", lambda error: live_errors.append(str(error)))
        live.goto("http://127.0.0.1:8765/csv-session")
        api = live.request.get("http://127.0.0.1:8765/api/jobs")
        assert api.ok
        live_rows = [c for c in api.json()["collections"] if c.get("free_sequence")]
        live.wait_for_timeout(1200)
        assert live.locator("#list .collection-card").count() == len(live_rows)
        assert not live_errors, live_errors
        live.close()
        print("UI smoke OK: two modes, CSV upload, High status, queue, stop/resume controls")
        browser.close()


if __name__ == "__main__":
    main()
