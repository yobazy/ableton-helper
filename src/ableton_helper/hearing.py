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

from . import roles
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
    if track is None and (slot is not None or bar is not None):
        raise HearingError("slot and bar need a track too.")
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


# ── stems ────────────────────────────────────────────────────────────────────

STEM_MODELS = {"htdemucs": "drums, bass, other, vocals", "htdemucs_ft": "the same, finer and 4x slower",
               "htdemucs_6s": "adds guitar and piano"}


def stems(service: AudioService, source: AudioSource, model: str = "htdemucs", fmt: str = "flac") -> dict[str, Any]:
    if model not in STEM_MODELS:
        raise HearingError("model is one of: " +"; ".join(f"{k} ({v})" for k, v in STEM_MODELS.items()))
    if fmt not in ("flac", "wav"):
        raise HearingError("format is flac or wav.")
    result = service.stems(source.path, model=model, fmt=fmt)
    return {"source": source.label, **source.to_dict(), "model": result["model"],
            "sample_rate": result.get("sample_rate"), "stems": result["stems"],
            "cached": result.get("cached", False),
            "next": "Drag the files into Live, or analyse one with analyze_audio(path=...)."}


# ── audio to MIDI ────────────────────────────────────────────────────────────

SESSION_CLIP_SCRIPT_VERSION = (0, 2, 0)
MIDI_COLOR = 0x7B61FF


def notes_to_beats(notes: list[dict[str, Any]], bpm: float, quantize: float | None = 0.25) -> list[dict[str, Any]]:
    """basic-pitch notes (seconds, velocity 0..1) → Live notes (beats, velocity 1..127).
    ``quantize`` snaps starts and lengths to a grid in beats (0.25 = 1/16); 0 or None leaves them."""
    per_second = bpm / 60.0
    out: dict[tuple[int, float], dict[str, Any]] = {}
    for n in notes:
        start = float(n["start"]) * per_second
        length = max(0.0, float(n["end"]) - float(n["start"])) * per_second
        if quantize:
            start = round(start / quantize) * quantize
            length = max(quantize, round(length / quantize) * quantize)
        elif length <= 0:
            continue
        velocity = max(1, min(127, int(round(float(n.get("velocity", 0.8)) * 127))))
        note = {"pitch": int(n["pitch"]), "start_time": round(start, 4), "duration": round(length, 4),
                "velocity": velocity}
        key = (note["pitch"], note["start_time"])
        # Quantising can land two notes on one spot; keep the longer.
        if key not in out or out[key]["duration"] < note["duration"]:
            out[key] = note
    return sorted(out.values(), key=lambda n: (n["start_time"], n["pitch"]))


def fit_to_length(notes: list[dict[str, Any]], length: float) -> tuple[list[dict[str, Any]], int]:
    kept, dropped = [], 0
    for n in notes:
        if n["start_time"] >= length - 1e-6:
            dropped += 1
            continue
        kept.append({**n, "duration": min(n["duration"], length - n["start_time"])})
    return kept, dropped


def midi_summary(notes: list[dict[str, Any]]) -> dict[str, Any]:
    if not notes:
        return {"note_count": 0}
    pitches = [n["pitch"] for n in notes]
    return {"note_count": len(notes), "lowest": note_name(min(pitches)), "highest": note_name(max(pitches)),
            "distinct_pitches": len(set(pitches)), "lowest_midi": min(pitches), "highest_midi": max(pitches)}


NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(pitch: int) -> str:
    return f"{NOTE_NAMES[pitch % 12]}{pitch // 12 - 2}"  # Live's naming: 60 = C3


def tempo_for(service: AudioService, source: AudioSource, bpm: float | None,
              set_tempo: float | None) -> tuple[float, str]:
    """The tempo the file was played at: given, else detected, else the set's."""
    if bpm:
        return float(bpm), "given"
    try:
        detected = service.analyze(source.path, ["bpm"])["results"].get("bpm") or {}
    except AudioServiceUnavailable:
        raise
    except AudioServiceError:
        detected = {}
    if "error" not in detected and detected.get("bpm") and \
            (detected.get("confidence", 1) >= 0.5 or set_tempo is None):
        return float(detected.get("bpm_rounded") or detected["bpm"]), "detected in the file"
    if set_tempo:
        return float(set_tempo), "the set's tempo (detection was unsure)"
    raise HearingError("Couldn't tell the file's tempo; pass bpm.")


def audio_to_midi(service: AudioService, source: AudioSource, bpm: float | None = None,
                  set_tempo: float | None = None, quantize: float | None = 0.25,
                  beats_per_bar: float = 4.0, onset: float | None = None, frame: float | None = None,
                  min_note_ms: float | None = None) -> dict[str, Any]:
    if quantize is not None and quantize < 0:
        raise HearingError("quantize is a grid in beats (0.25 = 1/16), or 0 for none.")
    result = service.transcribe(source.path, onset=onset, frame=frame, min_note_ms=min_note_ms)
    tempo, tempo_from = tempo_for(service, source, bpm, set_tempo)
    notes = notes_to_beats(result["notes"], tempo, quantize)
    if source.length_beats:
        length = float(source.length_beats)
    else:
        end = max((n["start_time"] + n["duration"] for n in notes), default=0.0)
        length = max(1, math.ceil(end / beats_per_bar - 1e-6)) * beats_per_bar
    notes, dropped = fit_to_length(notes, length)
    out = {"source": source.label, **source.to_dict(), "midi_file": result.get("midi_path"),
           "tempo": tempo, "tempo_from": tempo_from, "quantize_beats": quantize or None,
           "clip_bars": round(length / beats_per_bar, 2), **midi_summary(notes),
           "cached": result.get("cached", False)}
    if dropped:
        out["dropped_past_clip_end"] = dropped
    out["_notes"] = notes
    out["_length"] = length
    return out


def midi_clip_ops(source: AudioSource, notes: list[dict[str, Any]], length: float,
                  at_bar: float | None, beats_per_bar: float, name: str | None = None) -> list[dict[str, Any]]:
    """Ops for a new MIDI track right after the source track, holding the notes in a Session
    clip (slot 0), or in an Arrangement clip at ``at_bar``."""
    title = name or f"{source.clip_name or Path(source.path).stem} (MIDI)"
    index = source.track + 1 if source.track is not None else -1
    ops: list[dict[str, Any]] = [{"op": "create_track", "type": "midi", "name": title, "ref": "midi",
                                  "index": index, "color": MIDI_COLOR}]
    if at_bar is None:
        ops.append({"op": "create_session_midi_clip", "track": "midi", "slot": 0, "length": length,
                    "name": title, "notes": notes})
    else:
        ops.append({"op": "create_midi_clip", "track": "midi", "start": (float(at_bar) - 1) * beats_per_bar,
                    "length": length, "name": title, "notes": notes})
    return ops


def clap_prompt_vectors(service: AudioService) -> dict[str, list[list[float]]] | None:
    """Prompt vectors per role, or None when the service's CLAP looks broken."""
    # Always fresh: a cached broken answer would keep CLAP off after the service is fixed.
    if not roles.clap_is_healthy(service.embed_text(list(roles.CLAP_HEALTH_PAIR), use_cache=False)):
        return None
    texts = [t for ps in roles.CLAP_PROMPTS.values() for t in ps]
    vectors = dict(zip(texts, service.embed_text(texts)))
    return {k: [vectors[t] for t in ps] for k, ps in roles.CLAP_PROMPTS.items()}


def role_features(service: AudioService, source: AudioSource, use_stems: bool = True,
                  clap_prompts: dict[str, list[list[float]]] | None = None) -> roles.Features:
    features = ["bpm", "key", "loudness"] + (["embedding"] if clap_prompts else [])
    data = service.analyze(source.path, features)
    res = data["results"]
    f = roles.Features(duration=data.get("duration"),
                       name_kind=roles.name_kind(source.track_name, source.clip_name, Path(source.path).stem))
    if "error" not in res.get("bpm", {"error": 1}):
        f.bpm = res["bpm"].get("bpm")
    if "error" not in res.get("key", {"error": 1}):
        f.key_strength = res["key"].get("strength", res["key"].get("confidence"))
    if "error" not in res.get("loudness", {"error": 1}):
        f.movement_lu = energy_summary(res["loudness"]).get("movement_lu")
    emb = res.get("embedding")
    if clap_prompts and emb and "error" not in emb and emb.get("vector"):
        f.clap = roles.clap_probs(emb["vector"], clap_prompts)
    f.notes = service.transcribe(source.path)["notes"]
    if use_stems:
        stem_files = service.stems(source.path, model="htdemucs", fmt="wav")["stems"]
        energies = {}
        for name, p in stem_files.items():
            samples, rate = roles.read_wav_mono(p)
            energies[name] = roles.energy(samples)
            if name == "drums":
                f.drum_bands = roles.bands(samples, rate)
        total = sum(energies.values())
        if total > 0:
            f.stem_share = {k: v / total for k, v in energies.items()}
    return f


def tag_roles(service: AudioService, sources: list[AudioSource], use_stems: bool = True) -> dict[str, Any]:
    picked, skipped = limit(sources)
    clap_prompts = clap_prompt_vectors(service)
    items = []
    for s in picked:
        try:
            verdict = roles.score(role_features(service, s, use_stems, clap_prompts))
            items.append({"source": s.label, **s.to_dict(), **verdict.to_dict(), "_verdict": verdict})
        except AudioServiceUnavailable:
            raise
        except (AudioServiceError, ValueError, OSError) as e:
            items.append({"source": s.label, **s.to_dict(), "error": str(e)})
    out: dict[str, Any] = {"loops": items,
                           "listened_with": ("stems, " if use_stems else "") + "pitch, key"
                                            + (", CLAP" if clap_prompts else "")}
    if not clap_prompts:
        out["clap"] = ("Skipped: the service's CLAP embeddings look broken (unrelated texts come back "
                       "nearly identical), so roles come from stems, pitch and key only.")
    if skipped:
        out["skipped"] = f"{skipped} more files not tagged (limit {MAX_FILES} per call); ask per track."
    return out


def track_roles(items: list[dict[str, Any]]) -> dict[int, roles.Verdict]:
    """One verdict per track: average the probabilities of its loops."""
    by_track: dict[int, list[roles.Verdict]] = {}
    for it in items:
        if it.get("track") is not None and "_verdict" in it:
            by_track.setdefault(it["track"], []).append(it["_verdict"])
    out = {}
    for track, verdicts in by_track.items():
        mean = {k: sum(v.scores[k] for v in verdicts) / len(verdicts) for k in roles.KINDS}
        ranked = sorted(mean.items(), key=lambda kv: -kv[1])
        best = max(verdicts, key=lambda v: v.scores[ranked[0][0]])
        out[track] = roles.Verdict(ranked[0][0], ranked[0][1], best.reason, ranked[1][0], mean)
    return out


ROLE_LABELS = {"kick": "Kick", "clap": "Clap", "hats": "Hats", "perc": "Perc", "drums": "Drums",
               "bass": "Bass", "chords": "Chords", "pad": "Pad", "lead": "Lead", "vocal": "Vocal", "fx": "FX"}


def rename_ops(snapshot: dict[str, Any], verdicts: dict[int, roles.Verdict],
               min_confidence: float = 0.35) -> tuple[list[dict[str, Any]], list[str]]:
    """Rename tracks whose names say nothing ("Audio 7" → "Bass (Audio 7)") so match_roles
    can place them. Tracks whose names already mean something are left alone."""
    ops, done = [], []
    for t in snapshot["tracks"]:
        v = verdicts.get(t["index"])
        if v is None or v.confidence < min_confidence or roles.name_kind(t["name"]):
            continue
        new = f"{ROLE_LABELS[v.kind]} ({t['name']})"
        ops.append({"op": "set_track", "track": t["index"], "name": new})
        done.append(f"track {t['index']}: '{t['name']}' → '{new}'")
    return ops, done


# ── sections (when the service has them) ─────────────────────────────────────

SECTIONS_MISSING = ("The audio analysis service on the PC doesn't offer song sections yet "
                    "(no 'sections' in its /health features). They're being added to "
                    "audio-analysis; once it's redeployed this works with no change here.")


def sections(service: AudioService, source: AudioSource, beats_per_bar: float = 4.0) -> dict[str, Any]:
    """Song structure from /v1/sections, with each segment's start as a bar counted from
    the first downbeat."""
    if not service.has_feature("sections"):
        return {"available": False, "source": source.label, "why": SECTIONS_MISSING}
    try:
        data = service.sections(source.path)
    except AudioServiceUnavailable:
        raise
    except AudioServiceError as e:
        if e.status == 404 and e.code is None:  # the route itself is missing
            return {"available": False, "source": source.label, "why": SECTIONS_MISSING}
        raise
    bpm = data.get("bpm") or 0
    downbeats = data.get("downbeats") or []
    first = downbeats[0] if downbeats else 0.0
    sec_per_bar = 60.0 * beats_per_bar / bpm if bpm else None
    segments = []
    for s in data.get("segments") or []:
        seg = {"label": s.get("label"), "start_s": s.get("start"), "end_s": s.get("end"),
               "energy": s.get("energy"), "loudness_lufs": s.get("loudness_lufs")}
        if sec_per_bar:
            seg["bar"] = round((s["start"] - first) / sec_per_bar + 1, 1)
            seg["bars"] = round((s["end"] - s["start"]) / sec_per_bar, 1)
        segments.append({k: v for k, v in seg.items() if v is not None})
    out = {"available": True, "source": source.label, **source.to_dict(), "bpm": bpm,
           "first_downbeat_s": first, "segments": segments,
           "labels": "allin1's: intro, verse, chorus, bridge, break, inst, solo, outro (start and end "
                     "are silence). There is no 'drop': use energy to tell a drop from a breakdown."}
    if data.get("partial"):
        out["partial"] = True
    return out


def script_supports_session_clips(ping: dict[str, Any]) -> bool:
    try:
        version = tuple(int(x) for x in str(ping.get("version", "0")).split(".")[:3])
    except ValueError:
        return False
    return version >= SESSION_CLIP_SCRIPT_VERSION
