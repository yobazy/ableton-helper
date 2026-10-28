"""An in-memory stand-in for the audio analysis service, behind AudioService's transport.

It behaves like the real API where the client cares: requests by hash get
``404 audio_not_found`` until the file has been uploaded (multipart), results come
from ``answers`` keyed by file name, and files (stems, MIDI) are served by URL.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ableton_helper.audio_service import AudioService, AudioServiceUnavailable, Response

PNG = b"\x89PNG\r\n\x1a\n" + b"fake"


def _parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, str], bytes, str]:
    boundary = content_type.split("boundary=", 1)[1].encode()
    fields, data, filename = {}, b"", ""
    for part in body.split(b"--" + boundary):
        if b"\r\n\r\n" not in part:
            continue
        head, value = part.split(b"\r\n\r\n", 1)
        value = value[:-2] if value.endswith(b"\r\n") else value
        head_text = head.decode()
        name = head_text.split('name="', 1)[1].split('"', 1)[0]
        if name == "file":
            data = value
            filename = head_text.split('filename="', 1)[1].split('"', 1)[0]
        else:
            fields[name] = value.decode()
    return fields, data, filename


class FakeAudioService(AudioService):
    def __init__(self, cache_dir, features=("bpm", "key", "tags", "embedding", "loudness")):
        super().__init__("https://fake.test:8770", cache_dir)
        self.features = list(features)
        self.stored: dict[str, str] = {}      # sha → uploaded file name
        self.calls: list[tuple[str, str, str]] = []  # (method, path, "json"|"multipart"|"")
        self.answers: dict[str, dict[str, Any]] = {}  # file stem → feature → result
        self.transcriptions: dict[str, list[dict[str, Any]]] = {}
        self.text_vectors: dict[str, list[float]] = {}
        self.sections_answer: dict[str, Any] | None = None
        self.files: dict[str, bytes] = {}
        self.down = False

    def _request(self, method, path, *, body=None, content_type=None, timeout=60.0):
        if self.down:
            raise AudioServiceUnavailable("Can't reach the audio analysis service (test).")
        path = path.replace(self.base_url, "")
        kind = "multipart" if content_type and content_type.startswith("multipart") else \
            "json" if content_type else ""
        self.calls.append((method, path, kind))
        if method == "GET":
            if path == "/health":
                return self._json({"ok": True, "version": "test", "features": self.features})
            if path in self.files:
                return Response(200, self.files[path], "application/octet-stream")
            return self._json({"detail": "Not Found"}, 404)
        if path == "/v1/embed/text":
            texts = json.loads(body)["texts"]
            return self._json({"vectors": [self.text_vectors.get(t, [0.0, 0.0, 1.0]) for t in texts]})
        if path == "/v1/sections" and "sections" not in self.features:
            return self._json({"detail": "Not Found"}, 404)
        if kind == "multipart":
            fields, data, filename = _parse_multipart(body, content_type)
            sha = hashlib.sha256(data).hexdigest()
            self.stored[sha] = filename
            if "features" in fields:
                fields["features"] = fields["features"].split(",")
        else:
            fields = json.loads(body)
            sha = fields["audio"]
            if sha not in self.stored:
                return self._json({"detail": {"error": "audio_not_found", "message": "Upload it."}}, 404)
        stem = self.stored[sha].rsplit(".", 1)[0]
        return self._route(path, sha, stem, fields)

    def _route(self, path, sha, stem, fields):
        answers = self.answers.get(stem, {})
        if path == "/v1/analyze":
            results = {f: answers.get(f, {"error": f"no fake answer for {f}"}) for f in fields["features"]}
            return self._json({"audio": sha, "duration": answers.get("duration", 4.0),
                               "results": results, "cached": []})
        if path == "/v1/spectrogram":
            return Response(200, PNG, "image/png")
        if path == "/v1/stems":
            model = fields.get("model", "htdemucs")
            urls = {}
            for name in ("drums", "bass", "other", "vocals"):
                url = f"/v1/stems/{sha}/{model}/{name}.wav"
                self.files[url] = f"{name} of {stem}".encode()
                urls[name] = url
            return self._json({"audio": sha, "model": model, "sample_rate": 44100, "stems": urls})
        if path == "/v1/transcribe":
            url = f"/v1/midi/{sha}-x.mid"
            self.files[url] = b"MThd fake"
            return self._json({"audio": sha, "midi": url, "method": "basic-pitch",
                               "notes": self.transcriptions.get(stem, [])})
        if path == "/v1/sections":
            return self._json({"audio": sha, "duration": 60.0, "cached": False, **(self.sections_answer or {})})
        return self._json({"detail": "Not Found"}, 404)

    @staticmethod
    def _json(data, status=200):
        return Response(status, json.dumps(data).encode(), "application/json")

    def uploads(self) -> int:
        return sum(1 for _m, _p, k in self.calls if k == "multipart")
