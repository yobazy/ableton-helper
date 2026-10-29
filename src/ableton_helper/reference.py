"""A reference track → an arrangement template (SPEC 4.1 and 4.2).

1. ``/v1/sections`` on the analysis service (allin1): downbeats and labelled segments
   (intro, verse, chorus, break, inst, outro...) with each segment's loudness.
2. Segment boundaries become bars by counting downbeats, then snap to a phrase grid
   (8 bars by default). The grid's offset is the one that fits the boundaries best, so
   a one-bar pickup doesn't push every section off by one.
3. Demucs stems say what plays in each section: drums (split into kick and tops by the
   drums stem's lows and highs), bass, other (chords and synths) and vocals.
4. Sections get dance names (Intro, Groove, Breakdown, Build, Drop, Outro) from the
   label, the energy and whether the kick is in, and the result is written in the
   Phase 1 template format, so preview_template, build_skeleton and arrange_session
   all take it like a genre.

No MCP imports here; server.py wraps it.
"""

from __future__ import annotations

import array
import bisect
import math
import re
import statistics
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import roles
from . import templates as tpl
from .audio_service import AudioService, AudioServiceError, AudioServiceUnavailable
from .hearing import SECTIONS_MISSING, AudioSource, HearingError

VARIANT = "reference"
PHRASE_BARS = 8
SILENCE_LABELS = {"start", "end"}  # allin1's silence before and after the music
# When a section has no energy: what its label usually is in a dance track.
LABEL_KINDS = {"chorus": "Drop", "break": "Breakdown", "bridge": "Breakdown"}

# The parts a reference is heard as. Kinds and fallbacks are matching.py's, so the
# producer's loops find their way in through match_roles.
REF_ROLES: dict[str, dict[str, Any]] = {
    "kick": {"name": "Kick", "kind": "kick", "fallback": ["drums"], "color": "#E5484D"},
    "hats": {"name": "Hats and tops", "kind": "hats", "fallback": ["perc", "drums"], "color": "#F76B15"},
    "bass": {"name": "Bass", "kind": "bass", "color": "#0090FF"},
    "music": {"name": "Chords and synths", "kind": "chords", "fallback": ["pad", "lead"], "color": "#30A46C"},
    "vocal": {"name": "Vocal", "kind": "vocal", "fallback": ["lead"], "color": "#8E4EC6"},
}
STEM_ROLE = {"bass": "bass", "other": "music", "vocals": "vocal"}  # drums → kick and hats

# Without stems: what usually plays in each kind of section.
DEFAULT_PARTS = {
    "Intro": ["kick", "hats"],
    "Groove": ["kick", "hats", "bass", "music"],
    "Breakdown": ["music", "vocal"],
    "Build": ["hats", "music", "vocal"],
    "Drop": ["kick", "hats", "bass", "music", "vocal"],
    "Outro": ["kick", "hats"],
}

# Activity thresholds in dB (tuned on generated stems; see docs/PROGRESS-phase-4.md).
ACTIVE_BELOW_OWN_PEAK = -15.0   # a stem this far under its own loudest section is off
ACTIVE_BELOW_MIX_PEAK = -35.0   # and anything this far under the loudest stem is off
KICK_BELOW_PEAK = -12.0
TOPS_BELOW_PEAK = -15.0
EXCERPT_SECONDS = 4.0           # of the drums stem per section, for the band split
ENVELOPE_SECONDS = 0.5


# ── bars from downbeats ──────────────────────────────────────────────────────

@dataclass
class BarGrid:
    """Seconds ↔ bars (0 = the first downbeat), following the downbeats where there are some."""
    downbeats: list[float]

    def __post_init__(self):
        if len(self.downbeats) < 2:
            raise HearingError("The analysis found fewer than two downbeats, so it can't count bars.")
        self.bar_seconds = statistics.median(b - a for a, b in zip(self.downbeats, self.downbeats[1:]))

    def bar(self, t: float) -> float:
        d = self.downbeats
        if t <= d[0]:
            return (t - d[0]) / self.bar_seconds
        if t >= d[-1]:
            return len(d) - 1 + (t - d[-1]) / self.bar_seconds
        i = bisect.bisect_right(d, t) - 1
        return i + (t - d[i]) / (d[i + 1] - d[i])

    def time(self, bar: float) -> float:
        d = self.downbeats
        if bar <= 0:
            return d[0] + bar * self.bar_seconds
        if bar >= len(d) - 1:
            return d[-1] + (bar - (len(d) - 1)) * self.bar_seconds
        i = int(bar)
        return d[i] + (bar - i) * (d[i + 1] - d[i])


# ── snapping to phrases ──────────────────────────────────────────────────────

MAX_LEAD_IN = 2.0  # bars an offset may cut from (or add before) the start: a pickup, not a section


def best_offset(boundaries: list[float], phrase: int) -> int:
    """The grid offset (0 to phrase - 1) for a reference whose phrases don't start on its
    first downbeat (a pickup). Offset 0 unless another one puts at least two inner
    boundaries on the grid (within half a bar), more than 0 does, while moving the start
    by at most MAX_LEAD_IN bars. One odd boundary is an odd section, not a pickup."""
    inner = boundaries[1:-1]
    if phrase <= 1 or len(inner) < 2:
        return 0

    def dist(b: float, o: int) -> float:
        return abs(b - _nearest(b, o, phrase))

    def on_grid(o: int) -> int:
        return sum(1 for b in inner if dist(b, o) <= 0.5)

    def cost(o: int) -> float:
        return round(sum(dist(b, o) for b in inner), 6)

    best = 0
    for o in range(1, phrase):
        if dist(boundaries[0], o) > MAX_LEAD_IN or on_grid(o) < 2 or on_grid(o) <= on_grid(0):
            continue
        if (on_grid(o), -cost(o)) > (on_grid(best), -cost(best)):
            best = o
    return best


def _nearest(b: float, offset: int, phrase: int) -> int:
    """The nearest grid line, halves rounding up (Python's round() sends halves to even,
    which split boundaries that sit exactly half a phrase off in both directions)."""
    return offset + math.floor((b - offset) / phrase + 0.5) * phrase


def snap(boundaries: list[float], phrase: int) -> tuple[list[int], int]:
    """Boundaries (bars) snapped to the phrase grid, never going backwards, and the offset.
    Two boundaries landing on one line merge their sections, unless they're at least half a
    phrase apart: then the later one moves to the next line, so a real section survives."""
    phrase = max(1, int(phrase))
    offset = best_offset(boundaries, phrase)
    out: list[int] = []
    for i, b in enumerate(boundaries):
        s = _nearest(b, offset, phrase)
        if out and s <= out[-1]:
            s = out[-1] + phrase if b - boundaries[i - 1] >= phrase / 2 else out[-1]
        out.append(s)
    if len(out) >= 2 and out[-1] <= out[0]:  # a reference shorter than one phrase
        out[-1] = out[0] + phrase
    return out, offset


# ── energy ───────────────────────────────────────────────────────────────────

def _power_mean_db(pairs: list[tuple[float, float]]) -> float | None:
    """Duration-weighted power mean of (dB, weight) pairs."""
    total = sum(w for _, w in pairs)
    if total <= 0:
        return None
    return 10 * math.log10(sum(10 ** (db / 10) * w for db, w in pairs) / total)


def energy_level(relative_db: float | None) -> int | None:
    """1 to 10 like the genre templates: 10 for the loudest section, one step per 2 dB below."""
    if relative_db is None:
        return None
    return max(1, min(10, round(10 + relative_db / 2)))


# ── reading stems ────────────────────────────────────────────────────────────

def _open16(path: str) -> wave.Wave_read:
    try:
        w = wave.open(path, "rb")
    except (wave.Error, EOFError) as e:
        raise ValueError(f"Couldn't read the stem {path}: {e}") from e
    if w.getsampwidth() != 2:
        w.close()
        raise ValueError(f"{path}: expected 16-bit PCM")
    return w


def _samples(frames: bytes) -> array.array:
    data = array.array("h")
    data.frombytes(frames[:len(frames) - len(frames) % 2])
    if sys.byteorder == "big":
        data.byteswap()
    return data


def stem_envelope(path: str, window: float = ENVELOPE_SECONDS, stride: int = 7) -> list[float]:
    """Mean square per ``window`` seconds, read in chunks. Every ``stride``-th sample is
    enough for a level (an odd stride takes both channels of a stereo file in turn)."""
    out = []
    with _open16(path) as w:
        per = max(1, int(window * w.getframerate()))
        while True:
            frames = w.readframes(per)
            if not frames:
                break
            sub = _samples(frames)[::stride]
            out.append(sum(x * x for x in sub) / (max(1, len(sub)) * 32768.0 ** 2))
    return out


def read_excerpt(path: str, start: float, seconds: float) -> tuple[list[float], int]:
    """Mono samples from ``start`` for ``seconds``, halved in rate above 32 kHz (it's for
    a 150 Hz / 4 kHz split, so 22 kHz is plenty)."""
    with _open16(path) as w:
        rate, channels = w.getframerate(), w.getnchannels()
        first = max(0, min(int(start * rate), w.getnframes()))
        w.setpos(first)
        data = _samples(w.readframes(int(seconds * rate)))
    mono = [x / 32768.0 for x in data] if channels == 1 else \
        [sum(data[i:i + channels]) / (32768.0 * channels) for i in range(0, len(data) - channels + 1, channels)]
    if rate > 32000:
        return mono[::2], rate // 2
    return mono, rate


def _db(power: float) -> float:
    return 10 * math.log10(power) if power > 1e-12 else -120.0


# ── sections ─────────────────────────────────────────────────────────────────

@dataclass
class RefSection:
    start: int                 # bar in the reference, 0 = its first downbeat
    end: int
    label: str                 # allin1's label with the most overlap
    start_s: float = 0.0
    end_s: float = 0.0
    loudness_db: float | None = None
    energy: int | None = None
    parts: list[str] = field(default_factory=list)
    part_levels: dict[str, float] = field(default_factory=dict)  # dB below that part's peak
    kind: str = ""
    name: str = ""
    raw_start: float = 0.0     # bar before snapping

    @property
    def bars(self) -> int:
        return self.end - self.start


def build_sections(segments: list[dict[str, Any]], grid: BarGrid, phrase: int) -> tuple[list[RefSection], int, list[str]]:
    music = [s for s in segments if s.get("label") not in SILENCE_LABELS] or list(segments)
    if not music:
        raise HearingError("The analysis found no sections in this track.")
    raw = [grid.bar(float(s["start"])) for s in music] + [grid.bar(float(music[-1]["end"]))]
    snapped, offset = snap(raw, phrase)
    warnings = []
    tolerance = max(1.0, phrase / 4)
    for r, s in zip(raw[1:-1], snapped[1:-1]):
        if abs(r - s) > tolerance:
            warnings.append(f"A boundary at bar {r + 1:.1f} of the reference is {abs(r - s):.1f} bars off "
                            f"the {phrase}-bar grid; it was moved to bar {s + 1}. Check that section by ear.")
    out: list[RefSection] = []
    for a, b in zip(snapped, snapped[1:]):
        if b <= a:
            continue
        overlaps = []
        for seg, ra, rb in zip(music, raw, raw[1:]):
            o = min(b, rb) - max(a, ra)
            if o > 1e-6:
                overlaps.append((seg, o))
        if not overlaps:  # past the analysed audio (the last phrase rounded up)
            overlaps = [(music[-1], 1.0)]
        label = max(overlaps, key=lambda so: so[1])[0].get("label") or "inst"
        loud = [(float(seg["loudness_lufs"]), o) for seg, o in overlaps if seg.get("loudness_lufs") is not None]
        if not loud:  # older answers: only the relative energy (an amplitude ratio)
            loud = [(20 * math.log10(max(float(seg["energy"]), 1e-6)), o)
                    for seg, o in overlaps if seg.get("energy") is not None]
        # The last raw boundary that landed here is the one this section really starts at.
        raw_start = next((r for r, s in reversed(list(zip(raw, snapped))) if s == a), a)
        out.append(RefSection(a, b, label, grid.time(a), grid.time(b), _power_mean_db(loud), raw_start=raw_start))
    loudest = max((s.loudness_db for s in out if s.loudness_db is not None), default=None)
    for s in out:
        s.energy = energy_level(None if s.loudness_db is None or loudest is None else s.loudness_db - loudest)
    dropped = round(raw[0] - snapped[0], 1)
    if offset and snapped[0] > 0.5:
        warnings.append(f"The reference's first {snapped[0]} bar(s) before the first phrase were left out "
                        "(a pickup or a lead-in).")
    elif dropped > 1:
        warnings.append(f"The first section was stretched back {dropped:g} bars to start on the phrase grid.")
    return out, offset, warnings


def hear_parts(sections: list[RefSection], stem_files: dict[str, str]) -> None:
    """Fill each section's parts from the stems' levels."""
    envelopes = {name: stem_envelope(p) for name, p in stem_files.items() if name in STEM_ROLE or name == "drums"}
    levels: dict[str, list[float]] = {}
    for name, env in envelopes.items():
        row = []
        for s in sections:
            a = int(s.start_s / ENVELOPE_SECONDS)
            b = max(a + 1, int(math.ceil(s.end_s / ENVELOPE_SECONDS)))
            chunk = env[max(0, a):b]
            row.append(_db(sum(chunk) / len(chunk)) if chunk else -120.0)
        levels[name] = row
    mix_peak = max((max(r) for r in levels.values()), default=-120.0)
    if mix_peak <= -119.0:  # every level would equal its own peak and look "in"
        raise ValueError("the stems are silent")

    def active(name: str, i: int) -> tuple[bool, float]:
        row = levels[name]
        below = row[i] - max(row)
        return below >= ACTIVE_BELOW_OWN_PEAK and row[i] - mix_peak >= ACTIVE_BELOW_MIX_PEAK, below

    for name, role in STEM_ROLE.items():
        if name not in levels:
            continue
        for i, s in enumerate(sections):
            on, below = active(name, i)
            if on:
                s.parts.append(role)
                s.part_levels[role] = round(below, 1)

    if "drums" not in levels:
        return
    low, high = [], []
    for i, s in enumerate(sections):
        on, _ = active("drums", i)
        if not on:
            low.append(None)
            high.append(None)
            continue
        middle = (s.start_s + s.end_s) / 2
        seconds = min(EXCERPT_SECONDS, max(0.5, s.end_s - s.start_s))
        samples, rate = read_excerpt(stem_files["drums"], max(0.0, middle - seconds / 2), seconds)
        b = roles.bands(samples, rate)
        e = roles.energy(samples)
        low.append(_db(e * b.low))
        high.append(_db(e * b.high))
    low_peak = max((x for x in low if x is not None), default=None)
    high_peak = max((x for x in high if x is not None), default=None)
    for s, lo, hi in zip(sections, low, high):
        found = []
        if lo is not None and lo - low_peak >= KICK_BELOW_PEAK:
            found.append(("kick", lo - low_peak))
        if hi is not None and hi - high_peak >= TOPS_BELOW_PEAK:
            found.append(("hats", hi - high_peak))
        if lo is not None and not found:  # drums are in but neither band stands out
            found.append(("hats", hi - high_peak if hi is not None else 0.0))
        for role, below in reversed(found):
            s.parts.insert(0, role)
            s.part_levels[role] = round(below, 1)
    order = list(REF_ROLES)
    for s in sections:
        s.parts.sort(key=order.index)


def name_sections(sections: list[RefSection], heard: bool) -> None:
    """Dance names from the label, the energy and whether the kick is in."""
    n = len(sections)
    lead = 0  # sections labelled intro from the start, and outro at the end
    while lead < n - 1 and sections[lead].label == "intro":
        lead += 1
    tail = n
    while tail > max(lead, 1) and sections[tail - 1].label == "outro":
        tail -= 1
    for i, s in enumerate(sections):
        if i < lead or i >= tail:
            s.kind = "Intro" if i < lead else "Outro"
            continue
        kick = ("kick" in s.parts) if heard else None
        if s.energy is None:  # no loudness this time: go by allin1's label
            kind = LABEL_KINDS.get(s.label, "Groove")
            if kick is False:
                kind = "Breakdown"
            elif kick and kind == "Breakdown":
                kind = "Groove"
            if kind != "Drop" and i == 0 and n > 1:
                kind = "Intro"
            elif kind != "Drop" and i == n - 1 and n > 2:
                kind = "Outro"
            s.kind = kind
            continue
        level = s.energy
        before = sections[i - 1].energy if i and sections[i - 1].energy is not None else level
        # A drop is (nearly) the loudest part, or a jump of 3+ after a dip, with the kick in.
        is_drop = kick is not False and s.label not in ("intro", "outro") and \
            (level >= 9 or (level >= 7 and level - before >= 3))
        if i == 0 and n > 1 and (s.label == "intro" or not is_drop):
            s.kind = "Intro"
        elif i == n - 1 and n > 2 and (s.label == "outro" or not is_drop):
            s.kind = "Outro"
        elif is_drop:
            s.kind = "Drop"
        elif kick is False or (kick is None and level <= 5) or (s.label in ("break", "bridge") and level <= 6):
            s.kind = "Breakdown"
        else:
            s.kind = "Groove"
    # Breakdown, then a short section, then a drop: the short one is the build.
    for i in range(1, n - 1):
        s = sections[i]
        if sections[i + 1].kind == "Drop" and s.kind in ("Breakdown", "Groove") and s.bars <= 16 \
                and sections[i - 1].kind == "Breakdown":
            s.kind = "Build"
    counts: dict[str, int] = {}
    for s in sections:
        counts[s.kind] = counts.get(s.kind, 0) + 1
    seen: dict[str, int] = {}
    for s in sections:
        seen[s.kind] = seen.get(s.kind, 0) + 1
        if counts[s.kind] == 1:
            s.name = s.kind
        elif s.kind in ("Intro", "Outro"):
            s.name = f"{s.kind} {'ABCDEFGHIJ'[min(seen[s.kind], 10) - 1]}"
        else:
            s.name = f"{s.kind} {seen[s.kind]}"


def transitions(sections: list[RefSection]) -> list[dict[str, Any]]:
    """Risers into big rises, crashes on them, downlifters into big falls."""
    out: list[dict[str, Any]] = []
    crashes = []
    for a, b in zip(sections, sections[1:]):
        if a.energy is None or b.energy is None:
            continue
        if b.energy - a.energy >= 3:
            out.append({"type": "riser", "section": a.name, "at": "end", "bars": min(8, a.bars)})
            crashes.append(b.name)
        elif a.energy - b.energy >= 3:
            out.append({"type": "downlifter", "section": b.name, "at": "start", "bars": min(2, b.bars)})
    if crashes:
        out.append({"type": "crash", "sections": crashes})
    return out


# ── the template ─────────────────────────────────────────────────────────────

def template_key_for(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40].strip("_") or "track"
    return f"ref_{slug}"


def template_data(key: str, title: str, bpm: float, sections: list[RefSection],
                  meta: dict[str, Any]) -> dict[str, Any]:
    used = [r for r in REF_ROLES if any(r in s.parts for s in sections)] or list(REF_ROLES)
    total = sum(s.bars for s in sections)
    minutes = total * 4 * 60 / bpm / 60 if bpm else 0
    return {
        "genre": key,
        "name": f"Reference: {title}",
        "description": f"Made from the reference track '{title}': {len(sections)} sections, {total} bars.",
        "bpm": round(bpm, 2),
        "bpm_range": [round(bpm - 3), round(bpm + 3)],
        "roles": {k: {kk: vv for kk, vv in REF_ROLES[k].items()} for k in used},
        "variants": {VARIANT: {
            "description": f"The reference's structure, about {minutes:.1f} minutes at {bpm:g} BPM.",
            "sections": [{"name": s.name, "bars": s.bars, **({"energy": s.energy} if s.energy else {}),
                          "roles": [p for p in s.parts if p in used]} for s in sections],
            "transitions": transitions(sections),
        }},
        "reference": meta,
    }


def save_template(key: str, data: dict[str, Any]) -> Path:
    if key in tpl.available_genres():
        raise HearingError(f"'{key}' is a built-in genre; pick another name.")
    tpl.parse_template(data, key)  # the same check load_template does
    folder = tpl.user_template_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{key}.yaml"
    path.write_text("# Made by analyze_reference. Edit freely: it's an ordinary template.\n"
                    + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110))
    return path


def _tempo(service: AudioService, source: AudioSource, sections_bpm: float | None,
           downbeat_bpm: float | None = None) -> tuple[float, str]:
    """The bpm feature is the better tempo (the audio-analysis README says so); allin1's is the
    fallback. The bars come from allin1's downbeats, so a bpm reading at half or double that
    tempo is corrected to match them."""
    try:
        bpm = service.analyze(source.path, ["bpm"])["results"].get("bpm") or {}
    except AudioServiceUnavailable:
        raise
    except AudioServiceError:
        bpm = {}
    ref = downbeat_bpm or sections_bpm
    if "error" not in bpm and bpm.get("bpm") and bpm.get("confidence", 1) >= 0.5:
        value, why = float(bpm["bpm"]), "the bpm analysis"
        if ref and 1.8 < value / ref < 2.2:
            value, why = value / 2, "the bpm analysis, halved to match the downbeats"
        elif ref and 0.45 < value / ref < 0.55:
            value, why = value * 2, "the bpm analysis, doubled to match the downbeats"
        return tidy_bpm(value), why
    if sections_bpm:
        return tidy_bpm(float(sections_bpm)), "allin1's beats"
    raise HearingError("Couldn't tell the reference's tempo.")


def tidy_bpm(bpm: float) -> float:
    """124.02 is a 124 BPM track; 123.5 stays 123.5."""
    return float(round(bpm)) if abs(bpm - round(bpm)) < 0.15 else round(bpm, 2)


def analyze_reference(service: AudioService, source: AudioSource, name: str | None = None,
                      phrase_bars: int = PHRASE_BARS, use_stems: bool = True,
                      save: bool = True) -> dict[str, Any]:
    if phrase_bars not in (1, 2, 4, 8, 16):
        raise HearingError("phrase_bars is 1 (whole bars only), 2, 4, 8 or 16.")
    if not service.has_feature("sections"):
        raise HearingError(SECTIONS_MISSING)
    try:
        data = service.sections(source.path)
    except AudioServiceError as e:
        if not isinstance(e, AudioServiceUnavailable) and e.status == 404 and e.code is None:
            raise HearingError(SECTIONS_MISSING) from e
        raise
    grid = BarGrid([float(x) for x in data.get("downbeats") or []])
    sections, offset, warnings = build_sections(data.get("segments") or [], grid, phrase_bars)
    if data.get("partial"):
        warnings.append("The service couldn't measure loudness this time, so sections have no energy; "
                        "ask again later for energy.")
    bpm, bpm_from = _tempo(service, source, data.get("bpm"), 240.0 / grid.bar_seconds)

    heard = False
    if use_stems:
        try:
            stem_files = service.stems(source.path, model="htdemucs", fmt="wav")["stems"]
            hear_parts(sections, stem_files)
            heard = True
        except AudioServiceUnavailable:
            raise
        except (AudioServiceError, ValueError, OSError) as e:
            warnings.append(f"Stems failed ({e}), so the parts per section are the usual ones for "
                            "each kind of section rather than heard.")
    name_sections(sections, heard)
    if not heard:
        for s in sections:
            s.parts = list(DEFAULT_PARTS[s.kind])

    title = name or Path(source.path).stem
    key = template_key_for(title)
    meta = {
        "file": Path(source.path).name,
        "sha256": service.file_hash(source.path),
        "method": data.get("method"),
        "bpm_from": bpm_from,
        "first_downbeat_s": grid.downbeats[0],
        "phrase_bars": phrase_bars,
        "grid_offset_bars": offset,
        "parts_from": "stems" if heard else "defaults",
        "segments": [{"label": s.get("label"), "start_s": s.get("start"), "end_s": s.get("end"),
                      "bar": round(grid.bar(float(s["start"])) + 1, 1), "energy": s.get("energy")}
                     for s in data.get("segments") or []],
    }
    out_data = template_data(key, title, bpm, sections, meta)
    template = tpl.parse_template(out_data, key)
    replaced = save and (tpl.user_template_dir() / f"{key}.yaml").exists()
    saved = save_template(key, out_data) if save else None

    first = sections[0].start
    rows = []
    for s in sections:
        row = {"name": s.name, "bar": s.start - first + 1, "bars": s.bars,
               "time": tpl.bar_to_time(s.start - first + 1, bpm), "energy": s.energy,
               "label": s.label, "parts": [REF_ROLES[p]["name"] for p in s.parts]}
        if heard and s.part_levels:
            row["part_levels_db"] = {REF_ROLES[p]["name"]: v for p, v in s.part_levels.items()}
        if abs(s.raw_start - s.start) > 0.25:
            row["moved_from_bar"] = round(s.raw_start - first + 1, 1)
        rows.append(row)
    out: dict[str, Any] = {
        "template": key, "variant": VARIANT, "name": template.name, "bpm": round(bpm, 2),
        "bpm_from": bpm_from, "bars": sum(s.bars for s in sections), "phrase_bars": phrase_bars,
        "sections": rows, "parts_from": meta["parts_from"],
        "source": source.label,
    }
    if saved:
        out["saved_to"] = str(saved)
        if replaced:
            out["replaced"] = f"There was already a template called {key}; this one replaces it."
    if warnings:
        out["warnings"] = warnings
    out["next"] = (f"preview_template('{key}', 'reference') shows it; mark_sections('{key}') puts its "
                   f"locators in the set; arrange_session('{key}', 'reference') arranges the Session loops "
                   f"like it; compare_arrangement('{key}') checks an existing arrangement against it.")
    out["energy_is"] = ("1 to 10 from loudness: 10 is the loudest section, one step per 2 dB below it. "
                        "allin1's labels have no 'drop'; drops are named from energy and the kick.")
    return out
