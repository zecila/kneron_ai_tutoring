import os
import sys
import unittest
from pathlib import Path

from flask import Flask, jsonify, request


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://llm.test/v1")
os.environ.setdefault("TTS_BASE_URL", "http://tts.test")
os.environ.setdefault("REDIS_URL", "memory://")

import server


def _request_details_app(trust_proxy):
    app = Flask(__name__)
    app.wsgi_app = server._proxy_aware_wsgi_app(app.wsgi_app, trust_proxy)

    @app.get("/")
    def details():
        return jsonify(remote_addr=request.remote_addr, scheme=request.scheme)

    return app


class ProxyConfigTest(unittest.TestCase):
    forwarded_headers = {
        "X-Forwarded-For": "198.51.100.23",
        "X-Forwarded-Proto": "https",
    }

    def test_trusted_proxy_uses_forwarded_client_and_scheme(self):
        app = _request_details_app(True)

        response = app.test_client().get("/", headers=self.forwarded_headers)

        self.assertEqual(
            response.get_json(),
            {"remote_addr": "198.51.100.23", "scheme": "https"},
        )

    def test_direct_mode_ignores_forwarded_headers(self):
        app = _request_details_app(False)

        response = app.test_client().get("/", headers=self.forwarded_headers)

        self.assertEqual(
            response.get_json(),
            {"remote_addr": "127.0.0.1", "scheme": "http"},
        )


if __name__ == "__main__":
    unittest.main()
