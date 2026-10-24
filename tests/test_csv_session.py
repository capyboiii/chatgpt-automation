import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from fastapi import HTTPException

import server
from chatgpt_pool import ChatGPTPool, NoImage, Refused, SEL_PROMPT, _Shot, _Slot, _classify, _next_index


class CsvSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name)
        self.old_collections = server.COLLECTIONS.copy()
        self.old_jobs = server.JOBS.copy()
        server.COLLECTIONS.clear()
        server.JOBS.clear()
        self.addCleanup(self.restore_state)

    def restore_state(self):
        server.COLLECTIONS.clear()
        server.COLLECTIONS.update(self.old_collections)
        server.JOBS.clear()
        server.JOBS.update(self.old_jobs)

    def test_prompt_finder_waits_for_all_visible_composer_variants(self):
        pool = ChatGPTPool({})
        locator = Mock()
        locator.first = locator
        locator.wait_for = AsyncMock()
        page = Mock()
        page.locator.return_value = locator
        search = pool._find(page, SEL_PROMPT, 12_000)
        with self.assertRaises(StopIteration) as finished:
            search.send(None)
        self.assertIs(finished.exception.value, locator)
        selector = page.locator.call_args.args[0]
        self.assertIn('[contenteditable="true"][role="textbox"]:visible', selector)
        self.assertIn('form textarea:visible', selector)
        locator.wait_for.assert_awaited_once_with(state="visible", timeout=12_000)

    def test_missing_composer_reports_login_instead_of_selectors(self):
        pool = ChatGPTPool({})
        page = Mock(url="https://chatgpt.com/auth/login")
        page.evaluate = AsyncMock(return_value="Log in\nSign up")
        diagnostic = pool._prompt_unavailable_reason(page)
        with self.assertRaises(StopIteration) as finished:
            diagnostic.send(None)
        self.assertIn("đăng nhập", finished.exception.value)

    def test_csv_row_builds_one_folder_and_ordered_jobs(self):
        csv_data = 'STT,PROMPT A,PROMPT B,COLLECTION,Style\n1,first,second,Liberty,Crystal\n'
        with patch.object(server, "OUTPUTS", self.output), \
             patch.object(server, "_csv_profiles", return_value=["acc1"]), \
             patch.object(server, "_start_free_cols", return_value=False) as start:
            response = TestClient(server.app).post(
                "/api/csv-session/start",
                files={"file": ("sample.csv", io.BytesIO(csv_data.encode()), "text/csv")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        collection = start.call_args.args[0][0]
        self.assertEqual([j["prompt"] for j in collection["jobs"]], ["first", "second"])
        self.assertEqual({j["folder"] for j in collection["jobs"]}, {"Liberty__Crystal"})
        self.assertEqual([j["results"] for j in collection["jobs"]], [[], []])

    def test_mockup_and_csv_use_same_queue_admission(self):
        (self.output / "shirt.png").write_bytes(b"template")
        with patch.object(server, "TEMPLATES", self.output), \
             patch.object(server, "_get_profiles", return_value=[{"name": "acc1"}]), \
             patch.object(server, "_start_free_cols", return_value=True) as queue:
            request = server.generate_collections({
                "profiles": ["acc1"],
                "collections": [{"name": "Shirts", "prompt": "Create mockup",
                                 "templates": ["shirt.png"]}],
                "skip_done": False,
            })
            with self.assertRaises(StopIteration) as completed:
                request.send(None)
        self.assertTrue(completed.exception.value["enqueued"])
        self.assertEqual(len(queue.call_args.args[0]), 1)
        self.assertEqual(queue.call_args.args[1], ["acc1"])
        self.assertEqual(server.COLLECTIONS, {})  # Chưa nhận vào queue thì chưa hiện bộ giả.

    def test_queue_positions_include_both_modes(self):
        for key, kind, status in (("a", False, "running"),
                                  ("b", True, "pending"),
                                  ("c", False, "pending")):
            server.COLLECTIONS[key] = {"id": key, "name": key,
                "prompt_name": key, "free": kind, "status": status, "jobs": []}
        with patch.dict(server.RUN, {"active": True}):
            rows = server._public_collections()
        self.assertEqual([row["queue_position"] for row in rows], [None, 1, 2])

    def test_shared_queue_accepts_and_rolls_back_rejected_work(self):
        loop = Mock()
        loop.is_closed.return_value = False
        pool = Mock(running_collections=True, stopped=False)
        pool._slots = [Mock(profile="acc1")]
        job = {"id": "queued-job", "status": "pending"}
        col = {"id": "queued-col", "name": "Queued", "jobs": [job],
               "status": "pending"}
        previous_ready = server._POOL_READY.is_set()
        server._POOL_READY.set()

        def submit(coroutine, actual_loop):
            self.assertIs(actual_loop, loop)
            coroutine.close()
            return Mock(result=Mock(return_value=accepted[0]))

        accepted = [False]
        try:
            with patch.dict(server.RUN, {"active": True}), \
                 patch.object(server, "ACTIVE_POOL", pool), \
                 patch.object(server, "ACTIVE_LOOP", loop), \
                 patch.object(server, "save_collections_state"), \
                 patch.object(server.asyncio, "run_coroutine_threadsafe", side_effect=submit):
                with self.assertRaises(HTTPException):
                    server._start_free_cols([col], ["acc1"])
                self.assertNotIn(col["id"], server.COLLECTIONS)
                self.assertNotIn(job["id"], server.JOBS)
                accepted[0] = True
                self.assertTrue(server._start_free_cols([col], ["acc1"]))
                self.assertIs(server.COLLECTIONS[col["id"]], col)
                self.assertIs(server.JOBS[job["id"]], job)
        finally:
            if not previous_ready:
                server._POOL_READY.clear()

    def test_new_queue_keeps_completed_batch_downloadable_after_save_and_reload(self):
        image = self.output / "Previous" / "1.png"
        image.parent.mkdir()
        image.write_bytes(b"previous image")
        url = "/files/outputs/Previous/1.png"
        old_job = {"id": "old-job", "status": "done", "dest": str(image),
                   "folder": "Previous", "results": [url], "result_url": url}
        old_col = {"id": "old-col", "name": "Previous", "prompt_name": "PROMPT 1",
                   "free": True, "free_sequence": True, "status": "done",
                   "jobs": [old_job]}
        server.COLLECTIONS[old_col["id"]] = old_col
        server.JOBS[old_job["id"]] = old_job
        new_job = {"id": "new-job", "status": "pending", "results": [],
                   "dest": str(self.output / "Next" / "1.png"),
                   "result_url": "/files/outputs/Next/1.png"}
        new_col = {"id": "new-col", "name": "Next", "prompt_name": "PROMPT 1",
                   "free": True, "free_sequence": True, "status": "pending",
                   "jobs": [new_job]}
        state_file = self.output / "collections_state.json"
        pool = Mock(running_collections=True, stopped=False)
        pool._slots = [Mock(profile="acc1")]
        loop = Mock()
        loop.is_closed.return_value = False
        was_ready = server._POOL_READY.is_set()
        server._POOL_READY.set()

        def submit(coroutine, actual_loop):
            coroutine.close()
            return Mock(result=Mock(return_value=True))

        try:
            with patch.dict(server.RUN, {"active": True}), \
                 patch.object(server, "ACTIVE_POOL", pool), \
                 patch.object(server, "ACTIVE_LOOP", loop), \
                 patch.object(server, "OUTPUTS", self.output), \
                 patch.object(server, "COLLECTIONS_STATE_FILE", state_file), \
                 patch.object(server.asyncio, "run_coroutine_threadsafe", side_effect=submit):
                self.assertTrue(server._start_free_cols([new_col], ["acc1"]))
                self.assertEqual([c["id"] for c in server._public_collections()],
                                 ["old-col", "new-col"])
                new_image = self.output / "Next" / "1.png"
                new_image.parent.mkdir()
                new_image.write_bytes(b"new image")
                new_job.update(status="done", results=["/files/outputs/Next/1.png"])
                new_col["status"] = "done"
                server._on_pool_job_update(new_job)
                with patch("fastapi.responses.StreamingResponse",
                           side_effect=lambda buf, **kwargs: buf):
                    archive_data = server.download_jobs_zip(cid="old-col")
                with zipfile.ZipFile(archive_data) as archive:
                    self.assertEqual(archive.read("designs/Previous/1.png"), b"previous image")
                server.COLLECTIONS.clear()
                server.JOBS.clear()
                server.load_collections_state()
                self.assertEqual(server.COLLECTIONS["old-col"]["status"], "done")
                self.assertEqual(server.COLLECTIONS["new-col"]["status"], "done")
                self.assertEqual(server._job_files(server.JOBS["old-job"]), [image])
        finally:
            if not was_ready:
                server._POOL_READY.clear()

    def test_delete_mockup_batch_keeps_csv_results_and_shared_files(self):
        image = self.output / "Shared" / "1.png"
        image.parent.mkdir()
        image.write_bytes(b"shared")
        url = "/files/outputs/Shared/1.png"
        mockup_job = {"id": "mockup-job", "folder": "Shared", "dest": str(image),
                      "result_url": url, "status": "done"}
        csv_job = {"id": "csv-job", "folder": "Shared", "dest": str(image),
                   "results": [url], "result_url": url, "status": "done"}
        server.COLLECTIONS["mockup"] = {"id": "mockup", "name": "Mockup",
            "free_sequence": False, "status": "done", "jobs": [mockup_job]}
        server.COLLECTIONS["csv"] = {"id": "csv", "name": "CSV",
            "free": True, "free_sequence": True, "status": "done", "jobs": [csv_job]}
        server.COLLECTIONS["legacy-csv"] = {"id": "legacy-csv", "name": "Old CSV",
            "free": True, "status": "done", "jobs": []}
        server.JOBS.update({"mockup-job": mockup_job, "csv-job": csv_job})
        with patch.object(server, "OUTPUTS", self.output), \
             patch.object(server, "save_collections_state"):
            result = server.delete_mockup_collections()
        self.assertEqual(result["deleted_collections"], 1)
        self.assertEqual(result["deleted_files"], 0)
        self.assertNotIn("mockup", server.COLLECTIONS)
        self.assertIn("csv", server.COLLECTIONS)
        self.assertIn("legacy-csv", server.COLLECTIONS)
        self.assertTrue(image.exists())

    def test_running_mockup_batch_cannot_be_deleted(self):
        server.COLLECTIONS["busy"] = {"id": "busy", "name": "Busy",
            "free_sequence": False, "status": "running", "jobs": []}
        with self.assertRaises(HTTPException) as error:
            server.delete_mockup_collections()
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("busy", server.COLLECTIONS)

    def test_download_all_is_scoped_to_selected_mode(self):
        for cid, is_csv in (("mockup", False), ("csv", True)):
            image = self.output / cid / "1.png"
            image.parent.mkdir()
            image.write_bytes(cid.encode())
            url = f"/files/outputs/{cid}/1.png"
            job = {"id": f"{cid}-job", "folder": cid, "dest": str(image),
                   "result_url": url, "status": "done"}
            if is_csv:
                job["results"] = [url]
            server.COLLECTIONS[cid] = {"id": cid, "name": cid,
                "free": is_csv, "free_sequence": is_csv,
                "status": "done", "jobs": [job]}
        with patch.object(server, "OUTPUTS", self.output), \
             patch("fastapi.responses.StreamingResponse",
                   side_effect=lambda buf, **kwargs: buf):
            mockup_zip = server.download_jobs_zip(mode="mockup")
            csv_zip = server.download_jobs_zip(mode="csv-session")
        with zipfile.ZipFile(mockup_zip) as archive:
            self.assertEqual(archive.namelist(), ["designs/mockup/1.png"])
        with zipfile.ZipFile(csv_zip) as archive:
            self.assertEqual(archive.namelist(), ["designs/csv/1.png"])
        with self.assertRaises(HTTPException) as error:
            server.download_jobs_zip(cid="missing")
        self.assertEqual(error.exception.status_code, 404)

    def test_chat_navigation_failure_returns_collection_to_queue(self):
        pool = ChatGPTPool({})
        slot = _Slot("acc1", 0, 0)
        slot.fresh = False
        pool._new_chat = AsyncMock(side_effect=RuntimeError("trang lỗi"))
        job = {"id": "j", "status": "pending"}
        col = {"id": "c", "name": "Bộ", "free_sequence": True,
               "status": "running", "jobs": [job]}
        task = pool._run_collection_on_slot(slot, object(), col)
        with self.assertRaises(StopIteration) as completed:
            task.send(None)
        self.assertFalse(completed.exception.value)
        self.assertEqual(col["status"], "pending")
        self.assertEqual(job["status"], "pending")
        self.assertIn("Không mở được chat", pool.stalled["acc1"])

    def test_queue_refuses_new_work_after_stop_or_close(self):
        pool = ChatGPTPool({})
        work = [{"id": "new", "jobs": [], "status": "pending"}]
        self.assertFalse(pool.enqueue_collections(work))
        pool.running_collections = True
        pool.stopped = True
        self.assertFalse(pool.enqueue_collections(work))
        self.assertTrue(pool.collection_queue.empty())

    def test_preview_and_custom_prompt_field_order(self):
        csv_data = ('COLLECTION,ART BRIEF,RENDER STEP,SIZE / VARIANT\n'
                    'Liberty,first,second,S 20cm\n')
        headers, rows, suffix = server._read_csv_session_table(
            csv_data.encode(), "custom.csv")
        self.assertEqual(headers,
                         ["COLLECTION", "ART BRIEF", "RENDER STEP", "SIZE / VARIANT"])
        self.assertEqual(rows[0]["ART BRIEF"], "first")
        self.assertEqual(suffix, ".csv")
        upload = Mock(filename="custom.csv", read=AsyncMock(return_value=csv_data.encode()))
        with patch.object(server, "OUTPUTS", self.output), \
             patch.object(server, "_csv_profiles", return_value=["acc1"]), \
             patch.object(server, "_start_free_cols", return_value=False) as start:
            coroutine = server.csv_session_start(
                upload, templates=[], skip_done=False,
                prompt_fields=["RENDER STEP", "ART BRIEF"],
                size_chart_field="__none__")
            with self.assertRaises(StopIteration) as completed:
                coroutine.send(None)
        jobs = start.call_args.args[0][0]["jobs"]
        self.assertEqual([j["prompt"] for j in jobs], ["second", "first"])
        self.assertEqual(completed.exception.value["prompt_columns"],
                         ["RENDER STEP", "ART BRIEF"])

    def test_delete_one_collection_removes_only_its_images(self):
        folder = self.output / "Shared"
        folder.mkdir()
        first = folder / "1.png"
        second = folder / "2.png"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        url1 = "/files/outputs/Shared/1.png"
        url2 = "/files/outputs/Shared/2.png"
        first_job = {"id": "j1", "folder": "Shared", "results": [url1, url2], "status": "done"}
        second_job = {"id": "j2", "folder": "Shared", "results": [url2], "status": "done"}
        server.COLLECTIONS["c1"] = {"id": "c1", "name": "One", "free_sequence": True,
                                    "status": "done", "jobs": [first_job]}
        server.COLLECTIONS["c2"] = {"id": "c2", "name": "Two", "free_sequence": True,
                                    "status": "done", "jobs": [second_job]}
        server.JOBS.update({"j1": first_job, "j2": second_job})
        with patch.object(server, "OUTPUTS", self.output), \
             patch.object(server, "save_collections_state") as saved:
            result = server.csv_session_delete_one("c1")
        self.assertEqual(result["deleted_files"], 1)
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertNotIn("c1", server.COLLECTIONS)
        self.assertIn("j2", server.JOBS)
        saved.assert_called_once()

    def test_delete_running_collection_is_rejected(self):
        server.COLLECTIONS["busy"] = {"id": "busy", "name": "Busy",
                                      "free_sequence": True, "status": "running", "jobs": []}
        with self.assertRaises(HTTPException) as raised:
            server.csv_session_delete_one("busy")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("busy", server.COLLECTIONS)

    def test_delete_all_csv_session_collections_removes_csv_and_keeps_mockup(self):
        csv_folder = self.output / "CSVRow"
        csv_folder.mkdir()
        csv_img = csv_folder / "1.png"
        csv_img.write_bytes(b"csv")
        csv_url = "/files/outputs/CSVRow/1.png"
        csv_job = {"id": "csv-j", "folder": "CSVRow", "results": [csv_url], "dest": str(csv_img),
                   "status": "done"}

        mockup_folder = self.output / "MockupRow"
        mockup_folder.mkdir()
        mockup_img = mockup_folder / "1.png"
        mockup_img.write_bytes(b"mockup")
        mockup_url = "/files/outputs/MockupRow/1.png"
        mockup_job = {"id": "mockup-j", "folder": "MockupRow", "results": [mockup_url],
                      "dest": str(mockup_img), "status": "done"}

        server.COLLECTIONS["csv1"] = {"id": "csv1", "name": "CSV1", "free": True,
                                      "free_sequence": True, "status": "done", "jobs": [csv_job]}
        server.COLLECTIONS["mockup1"] = {"id": "mockup1", "name": "Mockup1", "free": False,
                                         "free_sequence": False, "status": "done", "jobs": [mockup_job]}
        server.JOBS.update({"csv-j": csv_job, "mockup-j": mockup_job})

        with patch.object(server, "OUTPUTS", self.output), \
             patch.object(server, "save_collections_state") as saved:
            result = server.delete_csv_session_collections()

        self.assertEqual(result["deleted_collections"], 1)
        self.assertEqual(result["deleted_files"], 1)
        self.assertNotIn("csv1", server.COLLECTIONS)
        self.assertIn("mockup1", server.COLLECTIONS)
        self.assertNotIn("csv-j", server.JOBS)
        self.assertIn("mockup-j", server.JOBS)
        self.assertFalse(csv_img.exists())
        self.assertTrue(mockup_img.exists())
        self.assertTrue(saved.called)

    def test_delete_all_csv_session_stops_running_batch_and_clears_all(self):
        server.COLLECTIONS["csv_busy"] = {"id": "csv_busy", "name": "Busy",
                                          "free": True, "free_sequence": True,
                                          "status": "running", "jobs": []}
        server.RUN["active"] = True
        with patch.object(server, "save_collections_state"):
            result = server.delete_csv_session_collections()
        self.assertEqual(result["deleted_collections"], 1)
        self.assertNotIn("csv_busy", server.COLLECTIONS)
        self.assertFalse(server.RUN["active"])

    def test_delete_all_csv_session_route(self):
        server.COLLECTIONS["csv_done"] = {"id": "csv_done", "name": "Done",
                                          "free": True, "free_sequence": True,
                                          "status": "done", "jobs": []}
        with patch.object(server, "save_collections_state"):
            response = TestClient(server.app).delete("/api/csv-session")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted_collections"], 1)
        self.assertNotIn("csv_done", server.COLLECTIONS)

    def test_three_selected_prompts_do_not_auto_add_size_chart(self):
        self.assertEqual(server.csv_session_start.__defaults__[-1].default, "__none__")
        csv_data = ("PROMPT 1,PROMPT 2,PROMPT 3,SIZE / VARIANT\n"
                    "hero,detail,lifestyle,XL\n")
        upload = Mock(filename="three.csv", read=AsyncMock(return_value=csv_data.encode()))
        with patch.object(server, "_csv_profiles", return_value=["acc1"]), \
             patch.object(server, "_start_free_cols", return_value=False) as start:
            coroutine = server.csv_session_start(
                upload, templates=[], skip_done=False,
                prompt_fields=["PROMPT 1", "PROMPT 2", "PROMPT 3"],
                size_chart_field="__none__")
            with self.assertRaises(StopIteration) as completed:
                coroutine.send(None)
        jobs = start.call_args.args[0][0]["jobs"]
        self.assertEqual([j["prompt_name"] for j in jobs],
                         ["PROMPT 1", "PROMPT 2", "PROMPT 3"])
        self.assertEqual(completed.exception.value["size_chart_rows"], 0)

    def test_size_chart_job_is_prompt_only(self):
        csv_data = "PROMPT 1,SIZE / VARIANT\nhero,XL / 24 cm\n"
        upload = Mock(filename="chart.csv", read=AsyncMock(return_value=csv_data.encode()))
        with patch.object(server, "OUTPUTS", self.output), \
             patch.object(server, "_csv_profiles", return_value=["acc1"]), \
             patch.object(server, "_start_free_cols", return_value=False) as start, \
             patch.dict(server.RUN, {"active": False}):
            coroutine = server.csv_session_start(
                upload, templates=[], skip_done=False,
                prompt_fields=["PROMPT 1"], size_chart_field="SIZE / VARIANT")
            with self.assertRaises(StopIteration):
                coroutine.send(None)
        jobs = start.call_args.args[0][0]["jobs"]
        self.assertFalse(jobs[0]["prompt_only"])
        self.assertTrue(jobs[1]["prompt_only"])
        self.assertIn("SIZE / VARIANT SOURCE:\nXL / 24 cm", jobs[1]["prompt"])

    def test_any_selected_size_field_is_prompt_only(self):
        csv_data = "PROMPT 1,SIZE\nhero,Create a size guide image\n"
        upload = Mock(filename="size.csv", read=AsyncMock(return_value=csv_data.encode()))
        with patch.object(server, "OUTPUTS", self.output), \
             patch.object(server, "_csv_profiles", return_value=["acc1"]), \
             patch.object(server, "_start_free_cols", return_value=False) as start, \
             patch.dict(server.RUN, {"active": False}):
            coroutine = server.csv_session_start(
                upload, templates=[], skip_done=False,
                prompt_fields=["PROMPT 1", "SIZE"], size_chart_field="__none__")
            with self.assertRaises(StopIteration):
                coroutine.send(None)
        jobs = start.call_args.args[0][0]["jobs"]
        self.assertEqual([job["prompt_only"] for job in jobs], [False, True])

    def test_regenerate_one_image_keeps_other_results_and_original_chat(self):
        folder = "Liberty"
        output_dir = self.output / folder
        output_dir.mkdir()
        for name in ("1.png", "2.png"):
            (output_dir / name).write_bytes(b"old")
        old_urls = [f"/files/outputs/{folder}/{name}" for name in ("1.png", "2.png")]
        job = {"id": "job1", "dest": str(output_dir / "1.png"), "folder": folder,
               "prompt": "Create two product photos", "prompt_name": "PROMPT A",
               "results": old_urls, "status": "done", "result_url": old_urls[0]}
        col = {"id": "col1", "name": folder, "free": True, "free_sequence": True,
               "status": "done", "ran_on": "acc1",
               "chat_url": "https://chatgpt.com/c/original-chat", "jobs": [job]}
        server.COLLECTIONS[col["id"]] = col
        server.JOBS[job["id"]] = job
        with patch.object(server, "_csv_profiles", return_value=["acc1"]), \
             patch.object(server, "_start_free_cols", return_value=False) as start:
            result = server.csv_session_regenerate_image(
                "col1", "job1", {"image_index": 1})
        self.assertTrue(result["queued"])
        task = start.call_args.args[0][0]
        self.assertEqual(task["required_profile"], "acc1")
        self.assertEqual(task["chat_url"], col["chat_url"])
        self.assertEqual(task["source_image_path"], str(output_dir / "1.png"))
        self.assertEqual(job["results"], old_urls)
        replacement = task["jobs"][0]
        replacement["status"] = "done"
        replacement["results"] = [f"/files/outputs/{folder}/3.png"]
        server._on_pool_job_update(replacement)
        self.assertEqual(job["results"], [old_urls[0], replacement["results"][0]])

    def test_regenerate_image_without_saved_chat_does_not_start_new_chat(self):
        folder = self.output / "Old"
        folder.mkdir()
        (folder / "1.png").write_bytes(b"old")
        url = "/files/outputs/Old/1.png"
        job = {"id": "oldjob", "dest": str(folder / "1.png"), "folder": "Old",
               "prompt": "Draw", "results": [url], "status": "done"}
        server.COLLECTIONS["oldcol"] = {"id": "oldcol", "name": "Old",
            "free_sequence": True, "status": "done", "ran_on": "acc1", "jobs": [job]}
        with patch.object(server, "_start_free_cols") as start:
            with self.assertRaises(HTTPException) as error:
                server.csv_session_regenerate_image(
                    "oldcol", "oldjob", {"image_index": 0})
        self.assertEqual(error.exception.status_code, 409)
        start.assert_not_called()

    def test_regenerate_size_chart_uses_prompt_without_attachment(self):
        pool = ChatGPTPool({"browser": {"profiles_dir": self.tmp.name}})
        slot = _Slot("acc1", 0, 0)
        page = type("Page", (), {"url": "https://chatgpt.com/c/chart-chat",
                                  "goto": AsyncMock()})()
        folder = "ChartRegen"
        job = {"id": "chart", "prompt": "Create size chart", "prompt_name": "SIZE CHART",
               "prompt_only": False, "folder": folder,
               "dest": str(self.output / folder / "1.png"), "status": "pending"}
        col = {"chat_url": page.url, "source_image_index": 0,
               "source_image_path": str(self.output / "missing.png")}
        sent = []

        async def save(actual_page, shot, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(shot.data)
            return True

        async def send(actual_page, prompt):
            sent.append(prompt)

        pool._find = AsyncMock()
        pool._set_thinking = AsyncMock()
        pool._collect = AsyncMock(return_value=[])
        pool._tail = AsyncMock(return_value="")
        pool._assistant_count = AsyncMock(return_value=0)
        pool._clear_attachments = AsyncMock(return_value=True)
        pool._upload = AsyncMock(return_value=True)
        pool._send = send
        pool._wait_all_images = AsyncMock(return_value=[_Shot("new", b"new", 1)])
        pool._save = save
        sequence = pool._run_regenerate_image(slot, page, col, job, AsyncMock())
        with self.assertRaises(StopIteration) as finished:
            sequence.send(None)
        self.assertTrue(finished.exception.value)
        pool._upload.assert_not_awaited()
        pool._clear_attachments.assert_awaited_once_with(page)
        self.assertEqual(sent, ["Create size chart"])

    def test_regenerate_detail_attaches_image_one_and_sends_exact_csv_prompt(self):
        pool = ChatGPTPool({"browser": {"profiles_dir": self.tmp.name}})
        slot = _Slot("acc1", 0, 0)
        page = type("Page", (), {"url": "https://chatgpt.com/c/detail-chat",
                                  "goto": AsyncMock()})()
        folder = "DetailRegen"
        first_image = self.output / folder / "1.png"
        first_image.parent.mkdir()
        first_image.write_bytes(b"reference")
        csv_prompt = "Create detail image\nKeep the original colors."
        job = {"id": "detail", "prompt": csv_prompt, "prompt_name": "PROMPT 2 - DETAIL",
               "folder": folder, "dest": str(first_image), "status": "pending"}
        col = {"chat_url": page.url, "source_image_index": 0,
               "source_image_path": str(first_image)}
        sent = []

        async def save(actual_page, shot, dest):
            dest.write_bytes(shot.data)
            return True

        async def send(actual_page, prompt):
            sent.append(prompt)

        pool._find = AsyncMock()
        pool._set_thinking = AsyncMock()
        pool._collect = AsyncMock(return_value=[])
        pool._tail = AsyncMock(return_value="")
        pool._assistant_count = AsyncMock(return_value=0)
        pool._clear_attachments = AsyncMock(return_value=True)
        pool._upload = AsyncMock(return_value=True)
        pool._send = send
        pool._wait_all_images = AsyncMock(return_value=[_Shot("new", b"new", 1)])
        pool._save = save
        sequence = pool._run_regenerate_image(slot, page, col, job, AsyncMock())
        with self.assertRaises(StopIteration) as finished:
            sequence.send(None)
        self.assertTrue(finished.exception.value)
        self.assertEqual(sent, [csv_prompt])
        self.assertEqual(pool._upload.await_args.args[1], [first_image])
        pool._clear_attachments.assert_not_awaited()

    def test_product_description_is_not_sent_and_prompt_columns_keep_file_order(self):
        csv_data = ('STT,PROMPT 2 - DETAIL,PRODUCT DESCRIPTION,PROMPT 1 - HERO,'
                    'PROMPT 3 - LIFESTYLE,SIZE / VARIANT\n'
                    '1,second,do not send,first,third,XL\n')
        with patch.object(server, "_csv_profiles", return_value=["acc1"]), \
             patch.object(server, "_start_free_cols", return_value=False) as start:
            response = TestClient(server.app).post(
                "/api/csv-session/start",
                data={"size_chart_field": "SIZE / VARIANT"},
                files={"file": ("sample.csv", io.BytesIO(csv_data.encode()), "text/csv")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        jobs = start.call_args.args[0][0]["jobs"]
        self.assertEqual([job["prompt_name"] for job in jobs],
                         ["PROMPT 2 - DETAIL", "PROMPT 1 - HERO",
                          "PROMPT 3 - LIFESTYLE", "SIZE CHART"])
        self.assertEqual([job["prompt"] for job in jobs[:3]],
                         ["second", "first", "third"])
        self.assertIn("SIZE / VARIANT SOURCE:\nXL", jobs[3]["prompt"])
        self.assertTrue(jobs[3]["prompt_only"])
        self.assertTrue(all(not job["prompt_only"] for job in jobs[:3]))
        self.assertTrue(all("do not send" not in job["prompt"] for job in jobs))

    def test_excel_headers_and_stt_build_ordered_session(self):
        class FakeSheet:
            def iter_rows(self, values_only):
                return iter([
                    ("STT", "PROMPT 1 - HERO", "PROMPT 2 - DETAIL",
                     "PROMPT 3 - LIFESTYLE", "PRODUCT DESCRIPTION", "SIZE / VARIANT"),
                    (1, "hero", "detail", "lifestyle", "description", "XL"),
                ])

        class FakeWorkbook:
            worksheets = [FakeSheet()]
            def close(self):
                pass

        with patch("openpyxl.load_workbook", return_value=FakeWorkbook()), \
             patch.object(server, "_csv_profiles", return_value=["acc1"]), \
             patch.object(server, "_start_free_cols", return_value=False) as start:
            response = TestClient(server.app).post(
                "/api/csv-session/start",
                data={"size_chart_field": "SIZE / VARIANT"},
                files={"file": ("ADI_SHORTALLS_top10.xlsx", io.BytesIO(b"fixture"),
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        collection = start.call_args.args[0][0]
        self.assertEqual(collection["folder"], "ADI_SHORTALLS_top10__1")
        self.assertEqual([job["prompt"] for job in collection["jobs"][:3]],
                         ["hero", "detail", "lifestyle"])
        self.assertEqual(collection["jobs"][3]["prompt_name"], "SIZE CHART")
        self.assertIn("SIZE / VARIANT SOURCE:\nXL",
                      collection["jobs"][3]["prompt"])
        self.assertEqual(response.json()["size_chart_rows"], 1)

    def test_skip_done_requires_completed_jobs_not_just_two_files(self):
        folder = self.output / "Liberty__Crystal"
        folder.mkdir()
        (folder / "1.png").write_bytes(b"partial A")
        (folder / "2.png").write_bytes(b"partial A")
        csv_data = 'PROMPT A,PROMPT B,COLLECTION,Style\nfirst,second,Liberty,Crystal\n'
        with patch.object(server, "OUTPUTS", self.output), \
             patch.object(server, "_csv_profiles", return_value=["acc1"]), \
             patch.object(server, "_start_free_cols", return_value=False) as start:
            response = TestClient(server.app).post(
                "/api/csv-session/start", data={"skip_done": "true"},
                files={"file": ("sample.csv", io.BytesIO(csv_data.encode()), "text/csv")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(start.call_args.args[0]), 1)

    def test_resume_one_csv_session_row_only(self):
        job = {"id": "j", "status": "paused", "results": ["old.png"],
               "error": "timeout"}
        row = {"id": "row", "name": "Demo", "free_sequence": True,
               "status": "paused", "jobs": [job]}
        server.COLLECTIONS["row"] = row
        server.JOBS["j"] = job
        with patch.object(server, "_csv_profiles", return_value=["acc1"]), \
             patch.object(server, "_start_free_cols", return_value=False) as start:
            response = TestClient(server.app).post("/api/csv-session/row/resume")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["resumed"], 1)
        self.assertEqual(start.call_args.args[0], [row])
        self.assertEqual((row["status"], job["status"], job["results"]),
                         ("pending", "pending", []))
        with patch.object(server, "_start_free_cols"):
            duplicate = TestClient(server.app).post("/api/csv-session/row/resume")
        self.assertEqual(duplicate.status_code, 409)

    def test_regenerate_preserves_old_results_and_creates_new_ids(self):
        job = {"id": "old-job", "status": "done", "results": ["/old/1.png"],
               "folder": "Demo", "prompt": "design", "error": None}
        old = {"id": "old-row", "name": "Demo", "free": True,
               "free_sequence": True, "status": "done", "jobs": [job]}
        server.COLLECTIONS["old-row"] = old
        server.JOBS["old-job"] = job
        with patch.object(server, "_csv_profiles", return_value=["acc1"]), \
             patch.object(server, "_start_free_cols", return_value=False) as start:
            response = TestClient(server.app).post("/api/collections/old-row/regenerate")
        self.assertEqual(response.status_code, 200, response.text)
        clone = start.call_args.args[0][0]
        self.assertNotEqual(clone["id"], old["id"])
        self.assertNotEqual(clone["jobs"][0]["id"], job["id"])
        self.assertEqual(clone["jobs"][0]["results"], [])
        self.assertEqual(clone["jobs"][0]["folder"], job["folder"])
        self.assertEqual(clone["regeneration"], 2)
        self.assertEqual(job["results"], ["/old/1.png"])

    def test_csvrun_old_mode_is_disabled(self):
        with self.assertRaises(HTTPException) as error:
            server.csvrun_resume_one("one-row")
        self.assertEqual(error.exception.status_code, 410)
        with self.assertRaises(HTTPException) as error:
            server.csvrun_resume()
        self.assertEqual(error.exception.status_code, 410)
        with self.assertRaises(HTTPException) as error:
            server.csvrun_start(Mock()).send(None)
        self.assertEqual(error.exception.status_code, 410)
        self.assertEqual(server.csv_page().headers["location"], "/csv-session")

    def test_jobs_api_exposes_thinking_confirmation_for_ui(self):
        with patch.dict(server.RUN, {"thinking_level": "cao",
                                     "thinking_state": {"acc1": "confirmed",
                                                        "acc2": "warning"}}, clear=False):
            response = TestClient(server.app).get("/api/jobs")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["thinking_level"], "cao")
        self.assertEqual(data["thinking_state"],
                         {"acc1": "confirmed", "acc2": "warning"})

    def test_duplicate_collection_names_get_separate_folders(self):
        csv_data = ('PROMPT A,PROMPT B,COLLECTION,Style\n'
                    'first,second,Liberty,Crystal\n'
                    'third,fourth,Liberty,Crystal\n')
        with patch.object(server, "OUTPUTS", self.output), \
             patch.object(server, "_csv_profiles", return_value=["acc1"]), \
             patch.object(server, "_start_free_cols", return_value=False) as start:
            response = TestClient(server.app).post(
                "/api/csv-session/start",
                files={"file": ("sample.csv", io.BytesIO(csv_data.encode()), "text/csv")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        folders = [c["folder"] for c in start.call_args.args[0]]
        self.assertEqual(folders, ["Liberty__Crystal", "Liberty__Crystal__Row-2"])

    def test_restore_marks_missing_result_paused(self):
        folder = "Liberty__Crystal"
        url = f"/files/outputs/{folder}/1.png"
        state = {"collections": [{"id": "c", "name": "Liberty", "status": "done",
            "free": True, "free_sequence": True,
            "jobs": [{"id": "a", "status": "done", "dest": str(self.output / folder / "1.png"),
                      "folder": folder, "results": [url]}]}]}
        state_file = self.output / "state.json"
        state_file.write_text(json.dumps(state), encoding="utf-8")
        with patch.object(server, "OUTPUTS", self.output), \
             patch.object(server, "COLLECTIONS_STATE_FILE", state_file):
            server.load_collections_state()
        self.assertEqual(server.COLLECTIONS["c"]["status"], "paused")
        self.assertEqual(server.COLLECTIONS["c"]["jobs"][0]["status"], "paused")

    def test_empty_job_does_not_borrow_first_image_or_enter_zip(self):
        image = self.output / "Liberty__Crystal" / "1.png"
        image.parent.mkdir()
        image.write_bytes(b"image")
        first = {"id": "a", "dest": str(image), "result_url": "/files/outputs/Liberty__Crystal/1.png",
                 "folder": "Liberty__Crystal", "prompt_name": "PROMPT A", "template_name": "",
                 "template_url": "", "status": "done", "results": ["/files/outputs/Liberty__Crystal/1.png"]}
        second = {**first, "id": "b", "prompt_name": "PROMPT B", "status": "pending", "results": []}
        server.COLLECTIONS["c"] = {"id": "c", "name": "Liberty", "prompt_name": "PROMPT A",
                                    "free": True, "free_sequence": True, "jobs": [first, second]}
        with patch.object(server, "OUTPUTS", self.output):
            public = server._public_collections()[0]
            self.assertIsNone(public["jobs"][1]["result_url"])
            self.assertEqual(server._job_files(second), [])

    def test_numeric_png_index_ignores_other_files(self):
        (self.output / "1.png").write_bytes(b"x")
        (self.output / "99.txt").write_bytes(b"x")
        self.assertEqual(_next_index(self.output), 2)

    def test_following_prompts_attach_first_image_in_same_chat(self):
        pool = ChatGPTPool({"browser": {"profiles_dir": self.tmp.name}, "run": {"max_retries": 0}})
        slot = _Slot("acc1", 0, 0)
        page = object()
        folder = "Liberty__Crystal"
        jobs = [{"id": key, "prompt": key, "prompt_name": key,
                 "folder": folder, "dest": str(self.output / folder / "1.png"),
                 "status": "pending", "results": []} for key in ("A", "B", "C")]
        col = {"name": "Liberty", "jobs": jobs}
        sent = []

        async def send(actual_page, prompt):
            self.assertIs(actual_page, page)
            sent.append(prompt)

        async def save(actual_page, shot, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(shot.data)
            return True

        pool._send = send
        pool._save = save
        pool._upload = AsyncMock(return_value=True)
        pool._collect = AsyncMock(return_value=[])
        pool._wait_all_images = AsyncMock(side_effect=[
            [_Shot("a", b"A", 1)], [_Shot("b", b"B", 2)],
            [_Shot("c", b"C", 3)]])
        with patch("chatgpt_pool.asyncio.sleep", new_callable=AsyncMock):
            import asyncio
            result = asyncio.run(pool._run_free_sequence(
                slot, page, col, jobs, set(), AsyncMock()))
        self.assertTrue(result)
        self.assertEqual([prompt.split("\n\nGenerate exactly the number of images requested", 1)[0]
                          for prompt in sent], ["A", "B", "C"])
        self.assertTrue(all("do not add extra images." in prompt for prompt in sent))
        self.assertEqual([p.name for p in sorted((self.output / folder).iterdir())],
                         ["1.png", "2.png", "3.png"])
        self.assertEqual(pool._upload.await_count, 2)
        self.assertEqual([call.args[1] for call in pool._upload.await_args_list],
                         [[self.output / folder / "1.png"]] * 2)
        self.assertEqual(col["status"], "done")

    def test_size_chart_sends_prompt_without_any_attachment(self):
        pool = ChatGPTPool({"browser": {"profiles_dir": self.tmp.name},
                            "run": {"max_retries": 0}})
        slot = _Slot("acc1", 0, 0)
        page = object()
        folder = "ChartOnlyAttachmentRule"
        reference = self.output / "reference.png"
        reference.write_bytes(b"reference")
        jobs = [{"id": key, "prompt": key, "prompt_name": key,
                 "prompt_only": False, "folder": folder,
                 "dest": str(self.output / folder / "1.png"),
                 "status": "pending", "results": []}
                for key in ("HERO", "SIZE CHART")]
        collection = {"name": folder, "jobs": jobs, "templates": [str(reference)]}
        sent = []

        async def save(actual_page, shot, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(shot.data)
            return True

        async def send(actual_page, prompt):
            sent.append(prompt)

        pool._save = save
        pool._send = send
        pool._upload = AsyncMock(return_value=True)
        pool._clear_attachments = AsyncMock(return_value=True)
        pool._collect = AsyncMock(return_value=[])
        pool._tail = AsyncMock(return_value="")
        pool._assistant_count = AsyncMock(return_value=0)
        pool._wait_all_images = AsyncMock(side_effect=[
            [_Shot("a", b"A", 1)], [_Shot("b", b"B", 2)]])
        sequence = pool._run_free_sequence(
            slot, page, collection, jobs, set(), AsyncMock())
        with self.assertRaises(StopIteration) as finished:
            sequence.send(None)  # All mocked awaits complete synchronously.
        result = finished.exception.value
        self.assertTrue(result)
        self.assertEqual(len(sent), 2)
        self.assertTrue(sent[1].startswith("SIZE CHART"))
        self.assertIn("Generate exactly ONE image", sent[1])
        self.assertIn("Do not include product photos", sent[1])
        self.assertEqual(pool._upload.await_count, 1)
        self.assertEqual(pool._upload.await_args.args[1], [reference])
        pool._clear_attachments.assert_awaited_once_with(page)

    def test_second_prompt_ignores_previous_turn_image(self):
        import asyncio
        pool = ChatGPTPool({})
        pool._screen_images = AsyncMock(return_value=[
            {"src": "old.png"}, {"src": "new.png"}])
        new_shot = _Shot("new.png", b"new", 2)
        pool._from_screen = AsyncMock(return_value=[new_shot])
        picked = asyncio.run(pool._pick_all(object(), [new_shot], {"old.png"}))
        self.assertEqual(picked, [new_shot])
        self.assertEqual(pool._from_screen.await_args.args[2], 1)
        self.assertEqual(pool._from_screen.await_args.kwargs["exclude"], {"old.png"})

    def test_busy_generation_obeys_batch_timeout(self):
        import asyncio
        pool = ChatGPTPool({"run": {"batch_timeout": 0.03, "no_progress_timeout": 30}})
        page = type("Page", (), {"wait_for_timeout": AsyncMock()})()
        pool._tail = AsyncMock(return_value="")
        pool._poll = AsyncMock(return_value=(True, []))
        pool._raise_if_bad = AsyncMock()
        with self.assertRaises(NoImage):
            asyncio.run(pool._wait_all_images(page, set()))

    def test_high_thinking_uses_calendar_picker_and_verifies_label(self):
        import asyncio

        pool = ChatGPTPool({"run": {"thinking": "cao"}})
        slider_state = {"now": "1"}
        button = Mock(count=AsyncMock(return_value=1),
                      is_visible=AsyncMock(return_value=True),
                      click=AsyncMock())
        slider = Mock(wait_for=AsyncMock(), focus=AsyncMock())
        slider.get_attribute = AsyncMock(
            side_effect=lambda key: "2" if key == "aria-valuemax" else slider_state["now"])

        async def press(key):
            self.assertEqual(key, "End")
            slider_state["now"] = "2"

        slider.press = AsyncMock(side_effect=press)
        status = Mock(inner_text=AsyncMock(return_value="High, 3 of 3."))
        page = Mock()
        page.locator.side_effect = lambda sel: Mock(first={
            'button[aria-label="Select ChatGPT model"]': button,
            '[role="menu"] [role="slider"]': slider,
            '[role="menu"] [role="status"]': status,
        }[sel])
        page.wait_for_timeout = AsyncMock()
        page.keyboard.press = AsyncMock()
        self.assertTrue(asyncio.run(pool._set_thinking(page)))
        slider.press.assert_awaited_once_with("End")
        self.assertEqual(slider_state["now"], "2")
        page.keyboard.press.assert_awaited_once_with("Escape")

    def test_high_thinking_does_not_accept_wrong_picker_label(self):
        import asyncio

        pool = ChatGPTPool({"run": {"thinking": "cao"}})
        button = Mock(count=AsyncMock(return_value=1),
                      is_visible=AsyncMock(return_value=True), click=AsyncMock())
        slider = Mock(wait_for=AsyncMock(), get_attribute=AsyncMock(return_value="2"))
        status = Mock(inner_text=AsyncMock(return_value="Medium, 3 of 3."))
        page = Mock()
        page.locator.side_effect = lambda sel: Mock(first={
            'button[aria-label="Select ChatGPT model"]': button,
            '[role="menu"] [role="slider"]': slider,
            '[role="menu"] [role="status"]': status,
        }[sel])
        page.keyboard.press = AsyncMock()
        self.assertFalse(asyncio.run(pool._set_thinking(page)))
        self.assertTrue(pool.warnings)

    def test_thinking_is_set_before_collection_prompt(self):
        import asyncio

        pool = ChatGPTPool({})
        slot = _Slot("acc1", 0, 0)
        slot.fresh = True
        calls = []
        pool._find = AsyncMock()
        pool._set_thinking = AsyncMock(side_effect=lambda page: calls.append("thinking"))
        pool._collect = AsyncMock(return_value=[])
        pool._run_free_sequence = AsyncMock(side_effect=lambda *args: calls.append("prompt"))
        col = {"name": "row", "free_sequence": True,
               "jobs": [{"id": "a", "status": "pending"}]}
        asyncio.run(pool._run_collection_on_slot(slot, object(), col))
        self.assertEqual(calls, ["thinking", "prompt"])

    def test_stopping_pool_rejects_new_queue(self):
        pool = type("StoppingPool", (), {"stopped": True})()
        loop = type("OpenLoop", (), {"is_closed": lambda self: False})()
        with patch.object(server, "ACTIVE_POOL", pool), \
             patch.object(server, "ACTIVE_LOOP", loop):
            with self.assertRaises(HTTPException) as error:
                server._start_free_cols([], ["acc1"])
        self.assertEqual(error.exception.status_code, 409)

    def test_stop_during_prompt_wait_pauses_without_saving(self):
        import asyncio
        pool = ChatGPTPool({"run": {"max_retries": 0}})
        slot = _Slot("acc1", 0, 0)
        job = {"id": "a", "prompt": "A", "prompt_name": "A",
               "folder": "Liberty", "dest": str(self.output / "Liberty" / "1.png"),
               "status": "pending", "results": []}
        collection = {"name": "Liberty", "jobs": [job]}
        pool._collect = AsyncMock(return_value=[])
        pool._send = AsyncMock()
        pool._save = AsyncMock()

        async def stop_wait(*args):
            pool.stopped = True
            raise NoImage("Đã dừng")

        pool._wait_all_images = stop_wait
        result = asyncio.run(pool._run_free_sequence(
            slot, object(), collection, [job], set(), AsyncMock()))
        self.assertFalse(result)
        self.assertEqual(job["status"], "paused")
        self.assertEqual(collection["status"], "paused")
        pool._save.assert_not_awaited()

    def test_third_party_refusal_stops_row_without_blocking_account(self):
        import asyncio
        kind, _, _ = _classify(
            "We couldn't create this image because it includes third-party content.")
        self.assertEqual(kind, "refused")
        pool = ChatGPTPool({"run": {"max_retries": 3}})
        slot = _Slot("acc1", 0, 0)
        jobs = [{"id": name, "prompt": name, "prompt_name": name,
                 "folder": "Liberty", "dest": str(self.output / "Liberty" / "1.png"),
                 "status": "pending", "results": []} for name in ("A", "B")]
        collection = {"name": "Liberty", "jobs": jobs}
        pool._collect = AsyncMock(return_value=[])
        pool._tail = AsyncMock(return_value="")
        pool._send = AsyncMock()
        pool._wait_all_images = AsyncMock(side_effect=Refused("third-party content"))
        with patch("chatgpt_pool.asyncio.sleep", new_callable=AsyncMock):
            result = asyncio.run(pool._run_free_sequence(
                slot, object(), collection, jobs, set(), AsyncMock()))
        self.assertTrue(result)  # slot stays available for the next collection
        self.assertEqual(collection["status"], "failed")
        self.assertEqual([j["status"] for j in jobs], ["failed", "failed"])
        self.assertEqual(pool.exhausted, {})
        self.assertEqual(pool._send.await_count, 3)

    def test_new_third_party_message_is_not_treated_as_old_error(self):
        import asyncio
        pool = ChatGPTPool({})
        pool._tail = AsyncMock(return_value=(
            "We couldn't create this image because it includes third-party content."))
        with self.assertRaises(Refused):
            asyncio.run(pool._raise_if_bad(object(), stale="Previous answer was fine."))
        pool._assistant_count = AsyncMock(return_value=2)
        with self.assertRaises(Refused):
            asyncio.run(pool._raise_if_bad(
                object(), stale=("third-party content", 1)))

    def test_refusal_baseline_is_captured_before_send(self):
        import asyncio
        pool = ChatGPTPool({"run": {"max_retries": 0}})
        slot = _Slot("acc1", 0, 0)
        job = {"id": "a", "prompt": "A", "prompt_name": "A",
               "folder": "Liberty", "dest": str(self.output / "Liberty" / "1.png"),
               "status": "pending", "results": []}
        collection = {"name": "Liberty", "jobs": [job]}
        pool._collect = AsyncMock(return_value=[])
        pool._tail = AsyncMock(return_value="Old successful answer")
        pool._send = AsyncMock()

        async def refuse(page, seen, net, stale):
            self.assertEqual(stale[0], "Old successful answer")
            raise Refused("third-party content")

        pool._wait_all_images = refuse
        asyncio.run(pool._run_free_sequence(
            slot, object(), collection, [job], set(), AsyncMock()))
        self.assertEqual(collection["status"], "failed")

    def test_refusal_twice_then_success_keeps_next_prompt_in_same_chat(self):
        import asyncio
        pool = ChatGPTPool({"run": {"max_retries": 3}})
        slot = _Slot("acc1", 0, 0)
        page = object()
        jobs = [{"id": name, "prompt": name, "prompt_name": name,
                 "folder": "Liberty", "dest": str(self.output / "Liberty" / "1.png"),
                 "status": "pending", "results": []} for name in ("A", "B")]
        collection = {"name": "Liberty", "jobs": jobs}
        pool._collect = AsyncMock(return_value=[])
        pool._tail = AsyncMock(return_value="")
        pool._send = AsyncMock()
        pool._wait_all_images = AsyncMock(side_effect=[
            Refused("third-party content"), Refused("third-party content"),
            [_Shot("a.png", b"A", 1)], [_Shot("b.png", b"B", 2)]])

        async def save(actual_page, shot, dest):
            self.assertIs(actual_page, page)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(shot.data)
            return True

        pool._save = save
        with patch("chatgpt_pool.asyncio.sleep", new_callable=AsyncMock):
            result = asyncio.run(pool._run_free_sequence(
                slot, page, collection, jobs, set(), AsyncMock()))
        self.assertTrue(result)
        self.assertEqual(collection["status"], "done")
        self.assertEqual([call.args[1].split("\n\nGenerate exactly the number of images requested", 1)[0]
                          for call in pool._send.await_args_list],
                         ["A", "A", "A", "B"])
        self.assertEqual([p.name for p in sorted((self.output / "Liberty").iterdir())],
                         ["1.png", "2.png"])

    def test_queue_continues_after_refused_collection(self):
        import asyncio
        pool = ChatGPTPool({"run": {"idle_exit_seconds": 0}})
        slot = _Slot("acc1", 0, 0)
        slot.set_page(object())
        pool._slots = [slot]
        visited = []

        async def run_one(actual_slot, page, collection, on_update=None):
            visited.append(collection["name"])
            collection["status"] = "failed" if collection["name"] == "first" else "done"
            return True

        pool._run_collection_on_slot = run_one
        rows = [{"id": name, "name": name, "status": "pending", "jobs": []}
                for name in ("first", "second")]
        asyncio.run(pool.run_collections(rows, idle_exit=0))
        self.assertEqual(visited, ["first", "second"])
        self.assertEqual([r["status"] for r in rows], ["failed", "done"])

    def test_pool_runs_two_profiles_in_parallel(self):
        import asyncio
        pool = ChatGPTPool({"run": {"idle_exit_seconds": 0}})
        slots = [_Slot(name, 0, i) for i, name in enumerate(("acc1", "acc2"))]
        for slot in slots:
            slot.set_page(object())
        pool._slots = slots
        active = 0
        peak = 0

        async def run_one(slot, page, collection, on_update=None):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.1)
            active -= 1
            collection["status"] = "done"
            return True

        pool._run_collection_on_slot = run_one
        rows = [{"id": str(i), "name": str(i), "status": "pending", "jobs": []}
                for i in range(2)]
        asyncio.run(pool.run_collections(rows, idle_exit=0))
        self.assertEqual(peak, 2)
        self.assertEqual([r["status"] for r in rows], ["done", "done"])

    def test_ten_profiles_drain_twenty_five_collections(self):
        import asyncio

        pool = ChatGPTPool({"run": {"idle_exit_seconds": 0}})
        pool._slots = [_Slot(f"acc{i}", 0, i) for i in range(10)]
        for slot in pool._slots:
            slot.set_page(object())
        active = 0
        peak = 0
        handled = []

        async def run_one(slot, page, collection, on_update=None):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            handled.append((slot.profile, collection["id"]))
            collection["status"] = "done"
            active -= 1
            return True

        pool._run_collection_on_slot = run_one
        rows = [{"id": str(i), "name": str(i), "jobs": []} for i in range(25)]
        asyncio.run(pool.run_collections(rows, idle_exit=0))
        self.assertEqual(peak, 10)
        self.assertEqual(len(handled), 25)
        self.assertEqual({profile for profile, _ in handled},
                         {f"acc{i}" for i in range(10)})
        self.assertTrue(all(row["status"] == "done" for row in rows))

    def test_pool_starts_worker_added_during_running_batch(self):
        import asyncio

        async def scenario():
            pool = ChatGPTPool({"run": {"idle_exit_seconds": 0}})
            first = _Slot("acc1", 0, 0)
            first.set_page(object())
            pool._slots = [first]
            first_started = asyncio.Event()
            release_first = asyncio.Event()
            second_done = asyncio.Event()
            handled = []

            async def run_one(slot, page, collection, on_update=None):
                handled.append((slot.profile, collection["name"]))
                if collection["name"] == "first":
                    first_started.set()
                    await release_first.wait()
                else:
                    second_done.set()
                collection["status"] = "done"
                return True

            pool._run_collection_on_slot = run_one
            rows = [{"id": name, "name": name, "status": "pending", "jobs": []}
                    for name in ("first", "second")]
            task = asyncio.create_task(pool.run_collections(rows, idle_exit=0))
            await asyncio.wait_for(first_started.wait(), 2)
            second = _Slot("acc2", 0, 1)
            second.set_page(object())
            pool._slots.append(second)
            try:
                await asyncio.wait_for(second_done.wait(), 3)
            finally:
                release_first.set()
            await asyncio.wait_for(task, 4)
            return handled

        handled = asyncio.run(scenario())
        self.assertIn(("acc2", "second"), handled)

    def test_exhausted_profile_hands_collection_to_other_profile(self):
        import asyncio

        pool = ChatGPTPool({"run": {"idle_exit_seconds": 0}})
        first = _Slot("acc1", 0, 0)
        second = _Slot("acc2", 0, 1)
        first.set_page(object())
        second.set_page(object())
        pool._slots = [first, second]
        visited = []

        async def run_one(slot, page, collection, on_update=None):
            visited.append(slot.profile)
            if slot.profile == "acc1":
                pool.exhausted["acc1"] = "quota"
                return False
            collection["status"] = "done"
            return True

        pool._run_collection_on_slot = run_one
        row = {"id": "one", "name": "one", "status": "pending",
               "jobs": [{"id": "j", "status": "pending"}]}
        asyncio.run(pool.run_collections([row], idle_exit=0))
        self.assertEqual(visited, ["acc1", "acc2"])
        self.assertEqual(row["status"], "done")

    def test_reserve_uses_second_tab_when_first_fails(self):
        import asyncio

        pool = ChatGPTPool({"browser": {"reserves": ["acc2"],
                                        "tabs_per_account": 2}})

        async def boot(name, slots, delay):
            slots[0].fail("tab đầu lỗi")
            slots[1].set_page(object())

        pool._boot_profile = boot
        slot = asyncio.run(pool.recruit_reserve())
        self.assertIsNotNone(slot)
        self.assertEqual((slot.profile, slot.tab_no), ("acc2", 1))

    def test_second_upload_cannot_start_duplicate_pool_during_boot(self):
        old_run = server.RUN.copy()
        old_pool, old_loop = server.ACTIVE_POOL, server.ACTIVE_LOOP
        old_ready = server._POOL_READY.is_set()
        self.addCleanup(lambda: server.RUN.update(old_run))
        self.addCleanup(setattr, server, "ACTIVE_POOL", old_pool)
        self.addCleanup(setattr, server, "ACTIVE_LOOP", old_loop)
        self.addCleanup(server._POOL_READY.set if old_ready else server._POOL_READY.clear)
        server.RUN["active"] = False
        server.ACTIVE_POOL = None
        server.ACTIVE_LOOP = None
        rows = [{"id": "c", "name": "c", "jobs": []}]
        profiles = [f"acc{i}" for i in range(1, 11)]
        with patch.object(server.threading, "Thread") as thread, \
             patch.object(server, "save_collections_state"), \
             patch.object(server._POOL_READY, "wait", return_value=False):
            self.assertFalse(server._start_free_cols(rows, profiles))
            with self.assertRaises(HTTPException) as error:
                server._start_free_cols([{"id": "d", "name": "d", "jobs": []}], profiles)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(thread.call_count, 1)
        self.assertEqual(thread.call_args.kwargs["kwargs"]["profiles"], ["acc1"])

    def test_new_queue_work_only_recruits_needed_profiles(self):
        import asyncio

        pool = ChatGPTPool({})
        pool.running_collections = True
        pool._slots = [_Slot("acc1", 0, 0)]
        profiles = [f"acc{i}" for i in range(1, 11)]
        rows = [{"id": str(i), "jobs": []} for i in range(2)]
        with patch.object(pool, "add_profiles") as add:
            self.assertTrue(asyncio.run(self._enqueue(pool, rows, profiles)))
        self.assertEqual(add.call_args.args[0], profiles[:3])

    async def _enqueue(self, pool, rows, profiles):
        return pool.enqueue_collections(rows, profiles)


if __name__ == "__main__":
    unittest.main()
