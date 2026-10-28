"""Let Claude hear the audio in a Live set, through the shared audio analysis service.

Finds the audio files behind Live clips (from the snapshot's ``file_path``), sends them
to the service (audio_service.py) and turns the answers into small summaries. No MCP
imports here, so it's testable without the server; server.py wraps these as tools.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audio_service import AudioService, AudioServiceError, AudioServiceUnavailable
from .plan import beats_per_bar

MAX_FILES = 24  # per call, so "every clip in the set" can't run for an hour


class HearingError(ValueError):
    pass


# ── Which audio files a request means ────────────────────────────────────────

@dataclass
class AudioSource:
    path: str
    track: int | None = None
    track_name: str | None = None
    clip_name: str | None = None
    slot: int | None = None
    bar: float | None = None          # Arrangement clips: start bar (1-indexed)
    length_beats: float | None = None

    @property
    def label(self) -> str:
        if self.track is None:
            return Path(self.path).name
        where = f"slot {self.slot}" if self.slot is not None else f"arrangement bar {self.bar:g}"
        return f"'{self.clip_name or Path(self.path).stem}' on track {self.track} '{self.track_name}', {where}"

    def to_dict(self) -> dict[str, Any]:
        out = {"track": self.track, "track_name": self.track_name, "clip": self.clip_name,
               "slot": self.slot, "bar": self.bar, "file": self.path}
        return {k: v for k, v in out.items() if v is not None}


def _session_source(t: dict[str, Any], c: dict[str, Any]) -> AudioSource:
    return AudioSource(c["file_path"], t["index"], t["name"], c.get("name"), slot=c["slot"],
                       length_beats=c.get("length"))


def _arrangement_source(t: dict[str, Any], c: dict[str, Any], bpb: float) -> AudioSource:
    return AudioSource(c["file_path"], t["index"], t["name"], c.get("name"),
                       bar=round(c["start"] / bpb + 1, 2), length_beats=c["end"] - c["start"])


def _track(snapshot: dict[str, Any], index: int) -> dict[str, Any]:
    for t in snapshot["tracks"]:
        if t["index"] == index:
            return t
    raise HearingError(f"No track {index}. Tracks are numbered from 0; get_session lists them.")


def find_sources(snapshot: dict[str, Any] | None, track: int | None = None, slot: int | None = None,
                 bar: float | None = None, path: str | None = None) -> list[AudioSource]:
    """Audio files for a file path, a Session clip (track + slot), the Arrangement clip
    at a bar (track + bar), every audio clip on a track, or (nothing given) the whole set.
    Each file appears once."""
    if path:
        p = Path(path).expanduser()
        if not p.is_file():
            raise HearingError(f"No file at {p}")
        return [AudioSource(str(p))]
    if snapshot is None:
        raise HearingError("Give a track (and slot or bar) or a file path.")
    bpb = beats_per_bar(snapshot)
    if track is not None:
        t = _track(snapshot, track)
        if slot is not None:
            clip = next((c for c in t["session_clips"] if c["slot"] == slot), None)
            if clip is None:
                raise HearingError(f"Track {track} '{t['name']}' has no clip in slot {slot}.")
            if not clip.get("file_path"):
                raise HearingError(_no_audio_message(t, clip))
            return [_session_source(t, clip)]
        if bar is not None:
            beat = (float(bar) - 1) * bpb
            clip = next((c for c in t["arrangement_clips"]
                         if c["start"] - 1e-6 <= beat < c["end"] - 1e-6), None)
            if clip is None:
                raise HearingError(f"Track {track} '{t['name']}' has no arrangement clip at bar {bar:g}.")
            if not clip.get("file_path"):
                raise HearingError(_no_audio_message(t, clip))
            return [_arrangement_source(t, clip, bpb)]
        tracks = [t]
    else:
        tracks = [t for t in snapshot["tracks"] if not t.get("is_group")]
    seen: set[str] = set()
    out: list[AudioSource] = []
    for t in tracks:
        candidates = [_session_source(t, c) for c in t["session_clips"] if c.get("file_path")] + \
                     [_arrangement_source(t, c, bpb) for c in t["arrangement_clips"] if c.get("file_path")]
        for s in candidates:
            if s.path not in seen:
                seen.add(s.path)
                out.append(s)
    if not out:
        if track is not None:
            raise HearingError(f"Track {track} '{tracks[0]['name']}' has no audio clips. "
                               "MIDI tracks have no audio file to analyse yet.")
        raise HearingError("No audio clips in this set. MIDI tracks have no audio file to analyse yet.")
    return out


def _no_audio_message(t: dict[str, Any], clip: dict[str, Any]) -> str:
    if clip.get("is_midi"):
        return (f"'{clip.get('name')}' on track {t['index']} '{t['name']}' is a MIDI clip, so there's no "
                "audio file to analyse. get_clip_notes reads its notes.")
    return f"'{clip.get('name')}' on track {t['index']} '{t['name']}' has no audio file on disk."


def limit(sources: list[AudioSource], max_files: int = MAX_FILES) -> tuple[list[AudioSource], int]:
    return sources[:max_files], max(0, len(sources) - max_files)


# ── analyze ──────────────────────────────────────────────────────────────────

SILENCE_LUFS = -60.0


def _power(lufs: float) -> float:
    return 10 ** (lufs / 10)


def squash_curve(curve: list[float], points: int = 32) -> tuple[list[float], float]:
    """Average a per-second LUFS curve (in power, not dB) into at most ``points`` values.
    Returns the curve and the seconds each value covers."""
    if len(curve) <= points:
        return [round(x, 1) for x in curve], 1.0
    size = len(curve) / points
    out = []
    for i in range(points):
        chunk = curve[int(i * size):int((i + 1) * size)] or [curve[min(int(i * size), len(curve) - 1)]]
        mean = sum(_power(x) for x in chunk) / len(chunk)
        out.append(round(10 * math.log10(mean), 1) if mean > 0 else SILENCE_LUFS)
    return out, round(size, 2)


def energy_level(integrated_lufs: float | None) -> str | None:
    if integrated_lufs is None:
        return None
    if integrated_lufs >= -11:
        return "high"
    if integrated_lufs >= -18:
        return "medium"
    return "low"


def energy_summary(loudness: dict[str, Any]) -> dict[str, Any]:
    curve = [x for x in (loudness.get("energy_curve") or []) if x is not None]
    audible = [x for x in curve if x > SILENCE_LUFS]
    squashed, step = squash_curve(curve)
    out: dict[str, Any] = {"level": energy_level(loudness.get("integrated_lufs"))}
    if curve:
        out["curve_lufs"] = squashed
        out["seconds_per_point"] = step
    if audible:
        out["movement_lu"] = round(max(audible) - min(audible), 1)
    return out


def summarize_analysis(source: AudioSource, data: dict[str, Any]) -> dict[str, Any]:
    results = data.get("results") or {}
    out: dict[str, Any] = {"source": source.label, **source.to_dict()}
    duration = data.get("duration")
    if duration:
        out["seconds"] = round(duration, 2)
    errors = {f: r["error"] for f, r in results.items() if isinstance(r, dict) and "error" in r}
    bpm = results.get("bpm")
    if bpm and "error" not in bpm:
        out["bpm"] = bpm.get("bpm_rounded", bpm.get("bpm"))
        out["bpm_exact"] = bpm.get("bpm")
        out["bpm_confidence"] = bpm.get("confidence")
        if bpm.get("alternatives"):
            out["bpm_alternatives"] = bpm["alternatives"]
        if duration and bpm.get("bpm"):
            out["bars_at_bpm"] = round(duration * bpm["bpm"] / 240, 2)  # 4/4
    key = results.get("key")
    if key and "error" not in key:
        out["key"] = f"{key.get('name') or key.get('key')} ({key.get('camelot')})" if key.get("camelot") \
            else key.get("name") or key.get("key")
        out["key_confidence"] = key.get("confidence")
        if key.get("strength") is not None:
            out["key_strength"] = key["strength"]
    loud = results.get("loudness")
    if loud and "error" not in loud:
        out["loudness"] = {k: loud.get(k) for k in ("integrated_lufs", "range_lu", "peak_dbfs")}
        out["energy"] = energy_summary(loud)
    tags = results.get("tags")
    if tags and "error" not in tags:
        out["tags"] = {kind: [f"{t.get('style') or t.get('label')} {t.get('score', 0):.2f}" for t in items[:3]]
                       for kind, items in tags.items() if isinstance(items, list)}
    if errors:
        out["errors"] = errors
    return out


def analyze(service: AudioService, sources: list[AudioSource], tags: bool = False) -> dict[str, Any]:
    features = ["bpm", "key", "loudness"] + (["tags"] if tags else [])
    picked, skipped = limit(sources)
    items = []
    for s in picked:
        try:
            items.append(summarize_analysis(s, service.analyze(s.path, features)))
        except AudioServiceUnavailable:
            raise
        except AudioServiceError as e:  # one bad file shouldn't sink the rest
            items.append({"source": s.label, **s.to_dict(), "error": str(e)})
    out: dict[str, Any] = {"files": items}
    if skipped:
        out["skipped"] = f"{skipped} more files not analysed (limit {MAX_FILES} per call); ask per track."
    out["notes"] = ("Energy level compares loudness, and loops are often exported at different "
                    "gains, so treat it as a hint. A key with low confidence or strength usually "
                    "means drums or noise rather than a tonal part.")
    return out


# ── spectrogram ──────────────────────────────────────────────────────────────

def spectrogram(service: AudioService, source: AudioSource, start: float | None = None,
                duration: float | None = None, width: int | None = None,
                height: int | None = None) -> dict[str, Any]:
    if (width is not None and not 200 <= width <= 4000) or (height is not None and not 100 <= height <= 2000):
        raise HearingError("width is 200 to 4000 px and height 100 to 2000 px.")
    png = service.spectrogram(source.path, start=start, duration=duration, width=width, height=height)
    return {"source": source.label, **source.to_dict(), "image": str(png),
            "reading_it": "Mel spectrogram: time left to right, low to high frequency bottom to top, "
                          "brighter is louder. Kicks are short bright columns at the bottom, hats "
                          "thin ticks at the top, pads and chords long horizontal bands."}
