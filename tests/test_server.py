import unittest
import os
from unittest.mock import patch

from fastapi.testclient import TestClient

import server


class TestServer(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(server.app)

    def test_health(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/api/v1/health").json(), {"status": "ok"})

    def test_api_discovery_and_openapi(self):
        discovery = self.client.get("/api/v1").json()
        self.assertEqual(discovery["openapi"], "/openapi.json")
        schema = self.client.get("/openapi.json").json()
        self.assertIn("/api/v1/pronunciation", schema["paths"])
        self.assertIn("/api/v1/speech-to-text", schema["paths"])
        self.assertIn("/api/v1/phonemes", schema["paths"])
        self.assertIn("/api/v1/tts", schema["paths"])
        self.assertNotIn("/pronunciation", schema["paths"])

    def test_home_serves_ui(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("<html", response.text)

    def test_phonemes(self):
        response = self.client.post("/phonemes", data={"text": "hello world"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertGreater(len(body["phonemes"]), 0)
        self.assertEqual(len(body["phonemes"]), len(body["words"]))

    @patch("server.speech.get_phonemes_with_word_mapping", return_value=(["h", "ə"], {0: "hello", 1: "hello"}))
    def test_versioned_phonemes_accepts_json(self, _):
        response = self.client.post("/api/v1/phonemes", json={"text": "hello", "lang": "en"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"phonemes": ["h", "ə"], "words": ["hello", "hello"], "language": "en"})

    @patch("server.speech.get_phonemes_with_word_mapping", return_value=(["h"], {0: "hello"}))
    def test_optional_api_key_protects_compute_endpoints(self, _):
        with patch.dict(os.environ, {"OPENPRONOUNCE_API_KEY": "test-secret"}):
            missing = self.client.post("/api/v1/phonemes", json={"text": "hello"})
            wrong = self.client.post(
                "/api/v1/phonemes", json={"text": "hello"}, headers={"X-API-Key": "wrong"}
            )
            header = self.client.post(
                "/api/v1/phonemes", json={"text": "hello"}, headers={"X-API-Key": "test-secret"}
            )
            bearer = self.client.post(
                "/api/v1/phonemes", json={"text": "hello"}, headers={"Authorization": "Bearer test-secret"}
            )
            public_health = self.client.get("/api/v1/health")

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(header.status_code, 200)
        self.assertEqual(bearer.status_code, 200)
        self.assertEqual(public_health.status_code, 200)

    def test_empty_and_oversized_uploads_are_rejected_before_decode(self):
        empty = self.client.post(
            "/api/v1/speech-to-text", files={"file": ("empty.wav", b"", "audio/wav")}
        )
        with patch.object(server, "MAX_UPLOAD_BYTES", 4):
            oversized = self.client.post(
                "/api/v1/speech-to-text", files={"file": ("large.wav", b"12345", "audio/wav")}
            )
        self.assertEqual(empty.status_code, 422)
        self.assertEqual(oversized.status_code, 413)

    @patch("server.speech.transcribe", return_value="HELLO")
    def test_speech2text(self, _):
        import io
        import numpy as np
        import soundfile as sf
        buf = io.BytesIO()
        sf.write(buf, np.zeros(16000, dtype="float32"), 16000, format="WAV")
        buf.seek(0)
        response = self.client.post("/speech2text", files={"file": ("rec.wav", buf, "audio/wav")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"transcript": "HELLO"})

    def test_ui_assets_and_languages(self):
        for path in ("/static/ui.js", "/static/audio.js", "/static/viseme.js", "/static/assets/logo.svg"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        home = self.client.get("/").text
        for element in ("record-btn", "language-select", "expected-text", "word-chips", "score-ring"):
            self.assertIn(f'id="{element}"', home)
        self.assertIn('id="api-key"', home)
        languages = self.client.get("/languages").json()
        self.assertEqual(languages["default"], "en")
        self.assertIn({"code": "en", "name": "English"}, languages["languages"])
