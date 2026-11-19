"""Кнопка чата → нативный аватар: доставка, подтверждение, сроки и API.

PYTHONPATH=. python3 scripts/test_desktop_portrait.py
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import desktop_portrait as desktop


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.bridge = desktop.DesktopPortrait()
        self.clock = patch.object(desktop.time, "monotonic", return_value=100.0)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_no_panel_rejects_control(self):
        self.assertFalse(self.bridge.status()["connected"])
        with self.assertRaisesRegex(RuntimeError, "desktop_unavailable"):
            self.bridge.set("don", True)

    def test_retry_ack_switch_hide_and_tray_changes(self):
        self.bridge.report("", False, "")
        command = self.bridge.set("don", True)["pending"]
        # Потерянный ответ /report не теряет команду.
        for _ in range(2):
            self.assertEqual(self.bridge.report("", False, "")["pending"], command)
        state = self.bridge.report("don", True, command["id"])
        self.assertIsNone(state["pending"])
        self.assertTrue(state["shown"])
        self.assertEqual(state["persona"], "don")
        switched = self.bridge.set("kara", True)["pending"]
        self.assertNotEqual(command["id"], switched["id"])
        # Старое подтверждение не стирает более новую команду.
        self.assertEqual(self.bridge.report("don", True, command["id"])["pending"], switched)
        self.bridge.report("kara", True, switched["id"])
        hidden = self.bridge.set("kara", False)["pending"]
        self.assertFalse(self.bridge.report("kara", False, hidden["id"])["shown"])
        # Показали через трей — веб видит настоящее состояние.
        self.assertTrue(self.bridge.report("don", True, hidden["id"])["shown"])

    def test_stopped_panel_cannot_replay_commands(self):
        self.bridge.report("don", False, "")
        self.bridge.set("don", True)
        self.now.return_value += desktop.HEARTBEAT_TTL + 1
        self.assertIsNone(self.bridge.report("don", False, "")["pending"])
        self.assertFalse(self.bridge.status()["shown"])

    def test_command_expires_even_with_heartbeat(self):
        self.bridge.report("don", False, "")
        self.bridge.set("don", True)
        for n in range(1, 20):
            self.now.return_value = 100.0 + n
            state = self.bridge.report("don", False, "")
        self.assertIsNone(state["pending"])
        self.now.return_value += desktop.HEARTBEAT_TTL + 1
        self.assertFalse(self.bridge.status()["connected"])


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.allowed = True

        async def auth():
            if not self.allowed:
                raise HTTPException(status_code=401)

        app = FastAPI()
        app.include_router(desktop.create_router(auth))
        self.client = TestClient(app)
        bridge = patch.object(desktop, "bridge", desktop.DesktopPortrait())
        bridge.start()
        self.addCleanup(bridge.stop)

    def test_routes_validate_and_deliver(self):
        path = "/api/desktop/portrait"
        self.assertFalse(self.client.get(path).json()["connected"])
        self.assertEqual(self.client.post(path, json={"persona": "../bad", "shown": True}).status_code, 422)
        with patch("app.api.runtime.get_persona_info", return_value={"id": "don"}):
            self.assertEqual(self.client.post(path, json={"persona": "don", "shown": True}).status_code, 409)
            self.assertEqual(self.client.post(path + "/report", json={"persona": "", "shown": False}).status_code, 200)
            response = self.client.post(path, json={"persona": "don", "shown": True})
        self.assertEqual(response.status_code, 200)
        command = response.json()["pending"]
        state = self.client.post(path + "/report", json={"persona": "don", "shown": True, "applied": command["id"]}).json()
        self.assertTrue(state["shown"])
        self.assertIsNone(state["pending"])
        with patch("app.api.runtime.get_persona_info", return_value=None):
            self.assertEqual(self.client.post(path, json={"persona": "missing", "shown": True}).status_code, 404)

    def test_all_routes_require_auth(self):
        self.allowed = False
        for method, suffix, body in (("get", "", None), ("post", "", {"persona": "don", "shown": True}),
                                      ("post", "/report", {"shown": False})):
            with self.subTest(method=method, suffix=suffix):
                args = {} if body is None else {"json": body}
                self.assertEqual(getattr(self.client, method)("/api/desktop/portrait" + suffix, **args).status_code, 401)


if __name__ == "__main__":
    unittest.main()
