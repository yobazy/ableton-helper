"""Client for the shared Audio Analysis Service on the home PC (see ~/Programming/audio-analysis).

Everything is keyed by the file's SHA-256. A request goes out by hash first; only when
the service answers ``404 audio_not_found`` is the file uploaded (multipart, same
endpoint). Results are cached locally by hash too, so asking twice costs nothing.

Standard library only, so the MCP server stays light.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import ssl
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_URL = "https://pc.taila3582d.ts.net:8770"
BASE_URL = os.environ.get("AUDIO_ANALYSIS_URL", DEFAULT_URL).rstrip("/")
CACHE_DIR = Path(os.environ.get("ABLETON_HELPER_CACHE", Path.home() / ".cache" / "ableton-helper" / "audio"))

# Seconds. Stems can fall back to the CPU when the GPU is busy, which is slow.
TIMEOUTS = {"health": 4.0, "analyze": 180.0, "spectrogram": 90.0, "transcribe": 180.0,
            "stems": 900.0, "sections": 300.0, "embed_text": 120.0, "download": 120.0}

# CLAP vectors stay on the service (which caches them too): on 2026-10-28 they were
# broken there, and a local copy would outlive the fix.
NOT_CACHED_LOCALLY = {"embedding"}

UNREACHABLE = (
    "Can't reach the audio analysis service at {url} ({why}). It runs on the home PC "
    "('pc'): check that the PC is on and that this machine is on Tailscale. Analysis is "
    "unavailable until then; everything else in Ableton Helper still works."
)


class AudioServiceError(RuntimeError):
    """The service answered with an error."""

    def __init__(self, message: str, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.code, self.status = code, status


class AudioServiceUnavailable(AudioServiceError):
    """The service couldn't be reached (PC off, asleep or off the tailnet)."""


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def _multipart(fields: dict[str, str], file_path: Path) -> tuple[bytes, str]:
    boundary = "ah-" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                     f"{value}\r\n".encode("utf-8"))
    filename = file_path.name.replace('"', "'").replace("\r", " ").replace("\n", " ")
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                 "Content-Type: application/octet-stream\r\n\r\n".encode("utf-8"))
    parts.append(file_path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _form_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class AudioService:
    def __init__(self, base_url: str = BASE_URL, cache_dir: Path | str = CACHE_DIR):
        self.base_url = base_url.rstrip("/")
        self.cache_dir = Path(cache_dir)
        self._hashes: dict[tuple[str, int, int], str] = {}
        self._ssl = ssl.create_default_context()

    # -- transport -------------------------------------------------------------

    def _request(self, method: str, path: str, *, body: bytes | None = None,
                 content_type: str | None = None, timeout: float = 60.0) -> Response:
        """One HTTP call. Overridden in tests."""
        url = path if path.startswith("http") else self.base_url + path
        req = urllib.request.Request(url, data=body, method=method)
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ssl) as r:
                return Response(r.status, r.read(), r.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as e:
            return Response(e.code, e.read() or b"", e.headers.get("Content-Type", "") if e.headers else "")
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            why = getattr(e, "reason", None) or e
            if isinstance(why, (socket.timeout, TimeoutError)) or "timed out" in str(why):
                why = f"no answer within {timeout:g} s"
            raise AudioServiceUnavailable(UNREACHABLE.format(url=self.base_url, why=why)) from e

    @staticmethod
    def _raise_for(resp: Response, what: str) -> None:
        if resp.status < 400:
            return
        code, message = None, resp.body.decode("utf-8", "replace")[:300]
        try:
            detail = resp.json()
            detail = detail.get("detail", detail) if isinstance(detail, dict) else detail
            if isinstance(detail, dict):
                code, message = detail.get("error"), detail.get("message", message)
            elif isinstance(detail, str):
                message = detail
        except (ValueError, UnicodeDecodeError):
            pass
        raise AudioServiceError(f"{what} failed ({resp.status}): {message}", code=code, status=resp.status)

    def _post_audio(self, endpoint: str, path: Path, sha: str, fields: dict[str, Any], timeout: float) -> Response:
        """POST by hash; upload the file in the same request if the service hasn't got it."""
        body = json.dumps({"audio": sha, **fields}).encode("utf-8")
        resp = self._request("POST", endpoint, body=body, content_type="application/json", timeout=timeout)
        if resp.status == 404 and self._error_code(resp) == "audio_not_found":
            form = {k: _form_value(v) for k, v in fields.items()}
            if "features" in fields and isinstance(fields["features"], list):
                form["features"] = ",".join(fields["features"])
            data, ctype = _multipart(form, path)
            resp = self._request("POST", endpoint, body=data, content_type=ctype, timeout=timeout)
        self._raise_for(resp, endpoint)
        return resp

    @staticmethod
    def _error_code(resp: Response) -> str | None:
        try:
            detail = resp.json().get("detail")
        except (ValueError, AttributeError, UnicodeDecodeError):
            return None
        return detail.get("error") if isinstance(detail, dict) else None

    # -- files and cache -------------------------------------------------------

    def file_hash(self, path: Path | str) -> str:
        p = Path(path).expanduser()
        if not p.is_file():
            raise AudioServiceError(f"Audio file not found: {p}. It may have been moved; "
                                    "in Live, File → Manage Files → Locate can find it.", code="missing_file")
        st = p.stat()
        key = (str(p.resolve()), st.st_size, st.st_mtime_ns)
        if key not in self._hashes:
            self._hashes[key] = sha256_file(p)
        return self._hashes[key]

    def cache_path(self, sha: str, name: str) -> Path:
        d = self.cache_dir / sha[:2] / sha
        d.mkdir(parents=True, exist_ok=True)
        return d / name

    def _cached_json(self, sha: str, name: str) -> Any | None:
        p = self.cache_dir / sha[:2] / sha / name
        if p.is_file():
            try:
                return json.loads(p.read_text())
            except ValueError:
                return None
        return None

    def _store_json(self, sha: str, name: str, data: Any) -> None:
        self.cache_path(sha, name).write_text(json.dumps(data))

    @staticmethod
    def _opts_tag(opts: dict[str, Any]) -> str:
        if not opts:
            return "default"
        return hashlib.sha1(json.dumps(opts, sort_keys=True).encode()).hexdigest()[:8]

    def _download(self, url: str, dest: Path) -> Path:
        resp = self._request("GET", url, timeout=TIMEOUTS["download"])
        self._raise_for(resp, f"download {url}")
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(resp.body)
        tmp.replace(dest)
        return dest

    # -- endpoints -------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        resp = self._request("GET", "/health", timeout=TIMEOUTS["health"])
        self._raise_for(resp, "health")
        return resp.json()

    def has_feature(self, feature: str) -> bool:
        return feature in (self.health().get("features") or [])

    def analyze(self, path: Path | str, features: list[str], options: dict[str, Any] | None = None) -> dict[str, Any]:
        """{"audio", "duration", "results": {feature: ...}, "cached": [features served locally]}"""
        p = Path(path).expanduser()
        sha = self.file_hash(p)
        options = options or {}
        tag = self._opts_tag(options)
        results: dict[str, Any] = {}
        local: list[str] = []
        meta = self._cached_json(sha, "meta.json") or {}
        for f in features:
            hit = None if f in NOT_CACHED_LOCALLY else self._cached_json(sha, f"analyze-{f}-{tag}.json")
            if hit is not None:
                results[f] = hit
                local.append(f)
        missing = [f for f in features if f not in results]
        if missing:
            fields: dict[str, Any] = {"features": missing}
            if options:
                fields["options"] = options
            data = self._post_audio("/v1/analyze", p, sha, fields, TIMEOUTS["analyze"]).json()
            for f in missing:
                r = (data.get("results") or {}).get(f, {"error": "No answer"})
                results[f] = r
                if f in NOT_CACHED_LOCALLY:
                    continue
                if isinstance(r, dict) and "error" not in r and not r.get("partial"):
                    self._store_json(sha, f"analyze-{f}-{tag}.json", r)
            if data.get("duration"):
                meta["duration"] = data["duration"]
                self._store_json(sha, "meta.json", meta)
        return {"audio": sha, "duration": meta.get("duration"), "results": results, "cached": local}

    def spectrogram(self, path: Path | str, start: float | None = None, duration: float | None = None,
                    width: int | None = None, height: int | None = None) -> Path:
        p = Path(path).expanduser()
        sha = self.file_hash(p)
        opts = {k: v for k, v in (("start", start), ("duration", duration), ("width", width),
                                  ("height", height)) if v is not None}
        dest = self.cache_path(sha, f"spectrogram-{self._opts_tag(opts)}.png")
        if dest.is_file():
            return dest
        resp = self._post_audio("/v1/spectrogram", p, sha, opts, TIMEOUTS["spectrogram"])
        if not resp.body.startswith(b"\x89PNG"):
            raise AudioServiceError("The spectrogram answer wasn't a PNG image.", code="bad_reply")
        dest.write_bytes(resp.body)
        return dest

    def stems(self, path: Path | str, model: str = "htdemucs", fmt: str = "wav") -> dict[str, Any]:
        """Separate stems and download them. Returns {"model", "sample_rate", "stems": {name: local path}}."""
        p = Path(path).expanduser()
        sha = self.file_hash(p)
        manifest_name = f"stems-{model}-{fmt}.json"
        manifest = self._cached_json(sha, manifest_name)
        if manifest and all(Path(f).is_file() for f in manifest["stems"].values()):
            return {**manifest, "cached": True}
        data = self._post_audio("/v1/stems", p, sha, {"model": model, "format": fmt}, TIMEOUTS["stems"]).json()
        out_dir = self.cache_path(sha, f"stems-{model}")
        out_dir.mkdir(exist_ok=True)
        local = {}
        for name, url in (data.get("stems") or {}).items():
            filename = Path(url).name
            local[name] = str(self._download(url, out_dir / filename))
        manifest = {"model": data.get("model", model), "sample_rate": data.get("sample_rate"), "stems": local}
        self._store_json(sha, manifest_name, manifest)
        return {**manifest, "cached": False}

    def transcribe(self, path: Path | str, onset: float | None = None, frame: float | None = None,
                   min_note_ms: float | None = None) -> dict[str, Any]:
        """basic-pitch → {"notes": [{start, end, pitch, velocity}] (seconds), "midi_path"}."""
        p = Path(path).expanduser()
        sha = self.file_hash(p)
        opts = {k: v for k, v in (("onset", onset), ("frame", frame), ("min_note_ms", min_note_ms))
                if v is not None}
        tag = self._opts_tag(opts)
        name = f"transcribe-{tag}.json"
        midi_dest = self.cache_path(sha, f"{p.stem}-{tag}.mid")
        hit = self._cached_json(sha, name)
        if hit is not None and Path(hit.get("midi_path") or "").is_file():
            return {**hit, "cached": True}
        data = self._post_audio("/v1/transcribe", p, sha, opts, TIMEOUTS["transcribe"]).json()
        midi_path = None
        if data.get("midi"):
            midi_path = str(self._download(data["midi"], midi_dest))
        result = {"notes": data.get("notes") or [], "method": data.get("method"), "midi_path": midi_path}
        self._store_json(sha, name, result)
        return {**result, "cached": False}

    def sections(self, path: Path | str) -> dict[str, Any]:
        p = Path(path).expanduser()
        sha = self.file_hash(p)
        hit = self._cached_json(sha, "sections.json")
        if hit is not None:
            return {**hit, "cached": True}
        data = self._post_audio("/v1/sections", p, sha, {}, TIMEOUTS["sections"]).json()
        if not data.get("partial"):
            self._store_json(sha, "sections.json", data)
        return data

    def embed_text(self, texts: list[str], use_cache: bool = True) -> list[list[float]]:
        """CLAP text vectors, in the same space as the audio ``embedding`` feature. Cached by
        text; ``use_cache=False`` asks the service fresh (and doesn't store the answer)."""
        out: dict[str, list[float]] = {}
        cache_file = self.cache_dir / "text-embeddings.json"
        cache: dict[str, list[float]] = {}
        if use_cache and cache_file.is_file():
            try:
                cache = json.loads(cache_file.read_text())
            except ValueError:
                cache = {}
        missing = [t for t in dict.fromkeys(texts) if t not in cache]
        for i in range(0, len(missing), 64):
            batch = missing[i:i + 64]
            body = json.dumps({"texts": batch}).encode("utf-8")
            resp = self._request("POST", "/v1/embed/text", body=body, content_type="application/json",
                                 timeout=TIMEOUTS["embed_text"])
            self._raise_for(resp, "/v1/embed/text")
            vectors = resp.json().get("vectors") or []
            if len(vectors) != len(batch):
                raise AudioServiceError("Text embedding answer had the wrong number of vectors.", code="bad_reply")
            cache.update(zip(batch, vectors))
        if missing and use_cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(cache))
        for t in texts:
            out[t] = cache[t]
        return [out[t] for t in texts]
