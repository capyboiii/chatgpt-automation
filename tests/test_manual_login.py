import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import server


class ManualLoginTests(unittest.TestCase):
    def test_new_manual_login_creates_profile_and_opens_browser(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(server, "_get_profiles", return_value=[{"name": "acc1", "tabs": 1}]), \
             patch.object(server, "_profiles_dir", return_value=Path(directory)), \
             patch.object(server, "_save_profiles") as save, \
             patch.object(server, "profile_login", return_value={"opened": True, "auto": False}) as login, \
             patch.dict(server.BULK, {"active": False}):
            response = TestClient(server.app).post("/api/profiles/manual-login")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["profile"], "acc2")
        self.assertEqual(save.call_args.args[0][-1], {"name": "acc2", "tabs": 1})
        login.assert_called_once_with("acc2")

    def test_manual_login_rejected_during_bulk_login(self):
        with patch.dict(server.BULK, {"active": True}):
            response = TestClient(server.app).post("/api/profiles/manual-login")
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
