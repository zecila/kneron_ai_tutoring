import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://llm.test/v1")
os.environ.setdefault("TTS_BASE_URL", "http://tts.test")
os.environ.setdefault("REDIS_URL", "memory://")

import server


class AvatarConfigTest(unittest.TestCase):
    def setUp(self):
        server.app.config.update(TESTING=True, RATELIMIT_ENABLED=False)

    def test_environment_flag_accepts_common_boolean_values(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"TEST_FLAG": value}):
                    self.assertTrue(server._environment_flag("TEST_FLAG", False))

        for value in ("0", "false", "FALSE", "no", "off"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"TEST_FLAG": value}):
                    self.assertFalse(server._environment_flag("TEST_FLAG", True))

    def test_environment_flag_uses_default_and_rejects_invalid_values(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(server._environment_flag("TEST_FLAG", True))
        with patch.dict(os.environ, {"TEST_FLAG": "sometimes"}):
            with self.assertRaisesRegex(RuntimeError, "TEST_FLAG must be true or false"):
                server._environment_flag("TEST_FLAG", True)

    def test_public_config_reports_effective_avatar_setting(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                with patch.object(server, "AVATAR_ENABLED", enabled):
                    with server.app.test_client() as client:
                        response = client.get("/api/config")

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json(), {"avatar_enabled": enabled})
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_disabled_avatar_health_does_not_contact_livetalking(self):
        with patch.object(server, "AVATAR_ENABLED", False):
            with patch.object(server, "_livetalking_is_ready") as is_ready:
                with server.app.test_client() as client:
                    response = client.get("/api/avatar/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "disabled"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        is_ready.assert_not_called()

    def test_disabled_avatar_routes_do_not_run_upstream_work(self):
        routes = (
            "/api/lessons/lesson-1/avatar/webrtc/offer",
            "/api/lessons/lesson-1/avatar/speak",
            "/api/lessons/lesson-1/avatar/interrupt",
            "/api/lessons/lesson-1/avatar/speaking",
            "/api/lessons/lesson-1/avatar/disconnect",
        )

        with patch.object(server, "AVATAR_ENABLED", False):
            with patch.object(server, "resolve_lesson_access") as resolve_access:
                with patch.object(server.requests, "post") as post:
                    with patch.object(server, "_synthesize_tts_wav") as synthesize:
                        with server.app.test_client() as client:
                            responses = [client.post(route, json={}) for route in routes]

        for response in responses:
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.get_json(), {"error": "Avatar feature is disabled"})
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        resolve_access.assert_not_called()
        post.assert_not_called()
        synthesize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
