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
os.environ.setdefault("STT_WEBSOCKET_URL", "ws://stt.test/audio/stt")
os.environ.setdefault("REDIS_URL", "memory://")

import server


class STTConfigTest(unittest.TestCase):
    def setUp(self):
        server.app.config.update(TESTING=True, RATELIMIT_ENABLED=False)

    def test_returns_configured_websocket_url(self):
        with patch.object(server, "STT_WEBSOCKET_URL", "ws://10.0.0.8:8001/audio/stt"):
            with server.app.test_client() as client:
                response = client.get("/api/stt/config")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"websocket_url": "ws://10.0.0.8:8001/audio/stt"},
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_reports_missing_configuration(self):
        with patch.object(server, "STT_WEBSOCKET_URL", ""):
            with server.app.test_client() as client:
                response = client.get("/api/stt/config")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"error": "STT websocket URL is not configured"})

    def test_rejects_non_websocket_url(self):
        with patch.object(server, "STT_WEBSOCKET_URL", "http://stt.test/audio/stt"):
            with server.app.test_client() as client:
                response = client.get("/api/stt/config")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"error": "STT websocket URL is invalid"})

    def test_rejects_credentials_and_invalid_ports(self):
        invalid_urls = (
            "ws://user:password@stt.test/audio/stt",
            "ws://stt.test:not-a-port/audio/stt",
        )
        for websocket_url in invalid_urls:
            with self.subTest(websocket_url=websocket_url):
                with self.assertRaisesRegex(ValueError, "STT websocket URL is invalid"):
                    server._validated_stt_websocket_url(websocket_url)


if __name__ == "__main__":
    unittest.main()
