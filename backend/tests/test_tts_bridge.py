import io
import os
import sys
import unittest
import wave
from pathlib import Path
from unittest.mock import call, patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://llm.test/v1")
os.environ.setdefault("TTS_BASE_URL", "http://tts.test")
os.environ.setdefault("REDIS_URL", "memory://")

import server


class _FakeJSONResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class TTSBridgeTest(unittest.TestCase):
    def setUp(self):
        server.app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
        server._tutor_attempts.clear()

    def test_api_tts_uses_shared_synthesizer(self):
        calls = []

        def synthesize(text, model_name=None, version=None):
            calls.append((text, model_name, version))
            return b"RIFF-test-wav"

        with patch.object(server, "_synthesize_tts_wav", synthesize):
            with server.app.test_client() as client:
                response = client.post("/api/tts", json={"text": " Hello "})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "audio/wav")
        self.assertEqual(response.data, b"RIFF-test-wav")
        self.assertEqual(calls, [("Hello", None, None)])

    def test_tutor_speech_normalizes_subtraction(self):
        self.assertEqual(
            server._normalize_tutor_speech_text("2-1=1"),
            "2 minus 1 equals 1",
        )
        self.assertEqual(
            server._normalize_tutor_speech_text("The result is -2."),
            "The result is negative 2.",
        )
        self.assertEqual(
            server._normalize_tutor_speech_text("The symbol `-` means subtraction."),
            "The symbol minus means subtraction.",
        )

    def test_tutor_speech_normalizes_comparison_symbols(self):
        self.assertEqual(server._normalize_tutor_speech_text("-"), "minus")
        self.assertEqual(server._normalize_tutor_speech_text("<"), "less than")
        self.assertEqual(server._normalize_tutor_speech_text(">"), "greater than")
        self.assertEqual(
            server._normalize_tutor_speech_text("2 < 3 and 4 >= 4"),
            "2 is less than 3 and 4 is greater than or equal to 4",
        )

    def test_tts_text_is_split_on_speech_boundaries(self):
        text = (
            "First, identify the known values. "
            "Next, choose the equation that connects them. "
            "Finally, substitute the values and check the units."
        )

        chunks = server._split_tts_text(text, max_chars=70)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 70 for chunk in chunks))
        self.assertEqual(" ".join(chunks), text)

    def test_tts_text_hard_splits_an_oversized_word(self):
        chunks = server._split_tts_text("x" * 25, max_chars=10)

        self.assertEqual(chunks, ["x" * 10, "x" * 10, "x" * 5])

    def test_tts_wav_chunks_are_merged_into_one_valid_wav(self):
        def make_wav(frames):
            output = io.BytesIO()
            with wave.open(output, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(8000)
                wav_file.writeframes(frames)
            return output.getvalue()

        first_frames = b"\x01\x00" * 3
        second_frames = b"\x02\x00" * 2
        merged = server._merge_tts_wav_chunks([
            make_wav(first_frames),
            make_wav(second_frames),
        ])

        with wave.open(io.BytesIO(merged), "rb") as wav_file:
            self.assertEqual(wav_file.getnchannels(), 1)
            self.assertEqual(wav_file.getsampwidth(), 2)
            self.assertEqual(wav_file.getframerate(), 8000)
            self.assertEqual(wav_file.getnframes(), 5)
            self.assertEqual(wav_file.readframes(5), first_frames + second_frames)

    def test_tts_synthesizes_each_chunk_in_order(self):
        with patch.object(server, "_split_tts_text", return_value=["First.", "Second."]):
            with patch.object(
                server,
                "_request_tts_wav_chunk",
                side_effect=[b"wav-one", b"wav-two"],
            ) as request_chunk:
                with patch.object(server, "_merge_tts_wav_chunks", return_value=b"merged") as merge:
                    result = server._synthesize_tts_wav("First. Second.", "model", "voice")

        self.assertEqual(result, b"merged")
        self.assertEqual(
            request_chunk.call_args_list,
            [
                call("First.", "model", "voice", 1, 2),
                call("Second.", "model", "voice", 2, 2),
            ],
        )
        merge.assert_called_once_with([b"wav-one", b"wav-two"])

    def test_avatar_speak_synthesizes_tutor_audio_and_uploads_to_livetalking(self):
        commands = []
        synth_calls = []
        audio_uploads = []

        def post_command(path, payload):
            commands.append((path, payload))
            return {"code": 0, "msg": "ok"}

        def synthesize(text, model_name=None, version=None, should_continue=None):
            synth_calls.append((text, model_name, version))
            self.assertTrue(should_continue())
            return b"RIFF-tutor-wav"

        def post_audio(sessionid, wav_bytes):
            audio_uploads.append((sessionid, wav_bytes))
            return {"code": 0, "msg": "ok"}

        with patch.object(server, "resolve_lesson_access", return_value=True):
            with patch.object(server, "TUTOR_TTS_MODEL", "tutor-model"):
                with patch.object(server, "TUTOR_TTS_VERSION", "tutor-version"):
                    with patch.object(server, "_post_livetalking_command", post_command):
                        with patch.object(server, "_synthesize_tts_wav", synthesize):
                            with patch.object(server, "_post_livetalking_audio", post_audio):
                                with server.app.test_client() as client:
                                    response = client.post(
                                        "/api/lessons/lesson-1/avatar/speak",
                                        json={
                                            "sessionid": "avatar_123",
                                            "attempt_id": 1,
                                            "text": "Area = x^2.",
                                            "interrupt": True,
                                        },
                                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True})
        self.assertEqual(commands, [("/interrupt_talk", {"sessionid": "avatar_123"})])
        self.assertEqual(synth_calls, [("Area equals x squared.", "tutor-model", "tutor-version")])
        self.assertEqual(audio_uploads, [("avatar_123", b"RIFF-tutor-wav")])

    def test_livetalking_audio_upload_uses_humanaudio_multipart_contract(self):
        calls = []

        def post(**kwargs):
            calls.append(kwargs)
            return _FakeJSONResponse({"code": 0, "msg": "ok"})

        with patch.object(server, "LIVETALKING_BASE_URL", "http://live.test"):
            with patch.object(server.requests, "post", lambda *args, **kwargs: post(url=args[0], **kwargs)):
                result = server._post_livetalking_audio("avatar_123", b"RIFF-tutor-wav")

        self.assertEqual(result, {"code": 0, "msg": "ok"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["url"], "http://live.test/humanaudio")
        self.assertEqual(calls[0]["data"], {"sessionid": "avatar_123"})
        self.assertEqual(calls[0]["files"], {"file": ("tutor.wav", b"RIFF-tutor-wav", "audio/wav")})

    def test_avatar_speak_can_skip_interrupt(self):
        commands = []
        audio_uploads = []

        def post_command(path, payload):
            commands.append((path, payload))
            return {"code": 0, "msg": "ok"}

        def post_audio(sessionid, wav_bytes):
            audio_uploads.append((sessionid, wav_bytes))
            return {"code": 0, "msg": "ok"}

        with patch.object(server, "resolve_lesson_access", return_value=True):
            with patch.object(server, "_post_livetalking_command", post_command):
                with patch.object(server, "_synthesize_tts_wav", return_value=b"RIFF-tutor-wav"):
                    with patch.object(server, "_post_livetalking_audio", post_audio):
                        with server.app.test_client() as client:
                            response = client.post(
                                "/api/lessons/lesson-1/avatar/speak",
                                json={
                                    "sessionid": "avatar_123",
                                    "attempt_id": 1,
                                    "text": "Keep talking after this.",
                                    "interrupt": False,
                                },
                            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(commands, [])
        self.assertEqual(audio_uploads, [("avatar_123", b"RIFF-tutor-wav")])

    def test_avatar_speak_reports_tts_failure(self):
        def synthesize(*args, **kwargs):
            raise server.TTSServiceError("TTS service unavailable", upstream="upstream boom")

        with patch.object(server, "resolve_lesson_access", return_value=True):
            with patch.object(server, "_synthesize_tts_wav", synthesize):
                with patch.object(server, "_post_livetalking_audio") as post_audio:
                    with server.app.test_client() as client:
                        response = client.post(
                            "/api/lessons/lesson-1/avatar/speak",
                            json={
                                "sessionid": "avatar_123",
                                "attempt_id": 1,
                                "text": "Hello.",
                                "interrupt": False,
                            },
                        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.get_json(),
            {
                "error": "TTS service unavailable",
                "upstream": "upstream boom",
            },
        )
        post_audio.assert_not_called()

    def test_avatar_interrupt_invalidates_older_speech_attempt(self):
        commands = []

        def post_command(path, payload):
            commands.append((path, payload))
            return {"code": 0, "msg": "ok"}

        with patch.object(server, "resolve_lesson_access", return_value=True):
            with patch.object(server, "_post_livetalking_command", post_command):
                with server.app.test_client() as client:
                    response = client.post(
                        "/api/lessons/lesson-1/avatar/interrupt",
                        json={"sessionid": "avatar_123", "attempt_id": 2},
                    )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(server._is_current_tutor_attempt("avatar_123", 2))
        self.assertEqual(commands, [("/interrupt_talk", {"sessionid": "avatar_123"})])

    def test_superseded_avatar_speech_does_not_upload_audio(self):
        def synthesize(text, model_name=None, version=None, should_continue=None):
            self.assertTrue(should_continue())
            server._set_current_tutor_attempt("avatar_123", 2)
            if not should_continue():
                raise server.TutorSpeechSuperseded()
            return b"RIFF-stale-wav"

        with patch.object(server, "resolve_lesson_access", return_value=True):
            with patch.object(server, "_post_livetalking_command"):
                with patch.object(server, "_synthesize_tts_wav", synthesize):
                    with patch.object(server, "_post_livetalking_audio") as post_audio:
                        with server.app.test_client() as client:
                            response = client.post(
                                "/api/lessons/lesson-1/avatar/speak",
                                json={
                                    "sessionid": "avatar_123",
                                    "attempt_id": 1,
                                    "text": "This response is stale.",
                                    "interrupt": True,
                                },
                            )

        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["superseded"])
        post_audio.assert_not_called()


if __name__ == "__main__":
    unittest.main()
