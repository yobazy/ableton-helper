"""Tag loops by role (kick, bass, hats, pad, vocal, fx...) from what they sound like.

Evidence, in order of weight:

1. **Stems.** Demucs splits the loop into drums, bass, other and vocals; each stem's share
   of the energy says which family the loop is in. The stems come back as 16-bit WAV, so
   they're read here with the standard library, whatever format the original was.
2. **Inside drums:** the drums stem's balance of lows (<150 Hz), mids and highs (>4 kHz),
   and how many hits there are per beat: kick, hats, clap, perc or a full kit.
3. **Inside "other":** basic-pitch notes (how many at once, how long, how high) and the
   key strength: chords, pad, lead or fx.
4. **CLAP**, when the service's embeddings are healthy: the loop's similarity to text like
   "a deep bassline". Checked on every call, because on 2026-10-28 every vector the
   service returned pointed the same way (cosine 0.999 between unrelated prompts).
5. **Names**, when they mean something, as a weak prior (matching.py's keywords).

Kinds are the ones matching.py uses, so a tagged track can feed match_roles.
"""

from __future__ import annotations

import array
import math
import statistics
import sys
import wave
from dataclasses import dataclass, field
from typing import Any

from .matching import MIN_SCORE, _kind_scores, tokens

KINDS = ["kick", "clap", "hats", "perc", "drums", "bass", "chords", "pad", "lead", "vocal", "fx"]
DRUM_KINDS = ["kick", "clap", "hats", "perc", "drums"]
TONAL_KINDS = ["chords", "pad", "lead", "fx"]

CLAP_PROMPTS = {
    "kick": ["a kick drum loop", "four on the floor kick drum"],
    "clap": ["hand claps on the backbeat", "a snare drum pattern"],
    "hats": ["a hi-hat loop", "closed hi-hats ticking"],
    "perc": ["a percussion loop with shakers and congas", "bongo and tambourine percussion"],
    "drums": ["a full drum loop with kick, snare and hi-hats", "a breakbeat drum loop"],
    "bass": ["a deep bassline", "a sub bass synth line"],
    "chords": ["piano chords", "synth chord stabs"],
    "pad": ["an ambient synth pad", "a lush pad with long sustained chords"],
    "lead": ["a synth lead melody", "a plucked synth arpeggio"],
    "vocal": ["a singing vocal", "a vocal chop"],
    "fx": ["a white noise riser sweep", "a whoosh sound effect"],
}
CLAP_HEALTH_PAIR = ("a kick drum loop", "a choir singing softly")
CLAP_HEALTHY_BELOW = 0.95  # cosine between those two; a working CLAP gives far less
CLAP_WEIGHT = 0.4
NAME_BOOST = 1.6
MAX_SECONDS = 12.0  # of each stem, for the pure-Python band split


# ── reading 16-bit WAV stems ─────────────────────────────────────────────────

def read_wav_mono(path: str, max_seconds: float = MAX_SECONDS) -> tuple[list[float], int]:
    try:
        with wave.open(path, "rb") as w:
            if w.getsampwidth() != 2:
                raise ValueError(f"{path}: expected 16-bit PCM")
            rate, channels = w.getframerate(), w.getnchannels()
            frames = w.readframes(min(w.getnframes(), int(max_seconds * rate)))
    except (wave.Error, EOFError) as e:
        raise ValueError(f"Couldn't read the stem {path}: {e}") from e
    data = array.array("h")
    data.frombytes(frames)
    if sys.byteorder == "big":
        data.byteswap()
    if channels == 1:
        return [x / 32768.0 for x in data], rate
    return [sum(data[i:i + channels]) / (32768.0 * channels) for i in range(0, len(data), channels)], rate


def energy(samples: list[float]) -> float:
    return sum(x * x for x in samples) / max(1, len(samples))


@dataclass
class Bands:
    low: float
    mid: float
    high: float
    hits_per_second: float


def _biquad(kind: str, freq: float, rate: int) -> tuple[float, float, float, float, float]:
    """Butterworth (Q = 1/sqrt 2) low- or high-pass, RBJ cookbook, normalised by a0."""
    w0 = 2 * math.pi * min(freq, rate * 0.45) / rate
    alpha = math.sin(w0) / math.sqrt(2)
    cos = math.cos(w0)
    if kind == "low":
        b0 = b2 = (1 - cos) / 2
        b1 = 1 - cos
    else:
        b0 = b2 = (1 + cos) / 2
        b1 = -(1 + cos)
    a0 = 1 + alpha
    return b0 / a0, b1 / a0, b2 / a0, -2 * cos / a0, (1 - alpha) / a0


def _filter(samples: list[float], coeffs: tuple[float, float, float, float, float], passes: int = 2) -> list[float]:
    b0, b1, b2, a1, a2 = coeffs
    y = samples
    for _ in range(passes):  # two passes: 24 dB/octave
        out = []
        x1 = x2 = y1 = y2 = 0.0
        for x in y:
            v = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2, x1, y2, y1 = x1, x, y1, v
            out.append(v)
        y = out
    return y


def bands(samples: list[float], rate: int) -> Bands:
    """Energy below 150 Hz and above 4 kHz (24 dB/octave Butterworth), the rest as mids,
    and the hits per second."""
    total = energy(samples)
    if not samples or total <= 0:
        return Bands(0.0, 1.0, 0.0, 0.0)
    low = energy(_filter(samples, _biquad("low", 150, rate)))
    high = energy(_filter(samples, _biquad("high", 4000, rate)))
    mid = max(0.0, total - low - high)
    parts = low + mid + high
    hop = max(1, rate // 100)  # 10 ms envelope
    env = [math.sqrt(energy(samples[i:i + hop])) for i in range(0, len(samples) - hop + 1, hop)]
    seconds = len(samples) / rate
    return Bands(low / parts, mid / parts, high / parts, count_hits(env) / seconds)


def count_hits(env: list[float], min_gap: int = 6) -> int:
    """Onsets in a 10 ms RMS envelope: a jump to at least twice the level 20 ms before,
    and big next to the loudest jump, at least 60 ms apart."""
    if len(env) < 4:
        return 0
    peak_rise = max(env[i] - env[i - 2] for i in range(2, len(env)))
    if peak_rise <= 0:
        return 0
    hits, last = 0, -min_gap
    for i in range(2, len(env)):
        rise = env[i] - env[i - 2]
        if rise >= 0.2 * peak_rise and env[i] >= 2 * env[i - 2] and i - last >= min_gap:
            hits += 1
            last = i
    return hits


# ── features ─────────────────────────────────────────────────────────────────

@dataclass
class Features:
    stem_share: dict[str, float] | None = None
    drum_bands: Bands | None = None
    bpm: float | None = None
    key_strength: float | None = None
    movement_lu: float | None = None
    notes: list[dict[str, Any]] = field(default_factory=list)
    duration: float | None = None
    name_kind: str | None = None
    clap: dict[str, float] | None = None

    def hits_per_beat(self) -> float | None:
        if self.drum_bands is None or not self.bpm:
            return None
        return self.drum_bands.hits_per_second * 60 / self.bpm

    def note_stats(self) -> dict[str, float] | None:
        """Polyphony (notes sounding at once, on average), median length in beats, median pitch."""
        notes = [n for n in self.notes if n["end"] > n["start"]]
        if len(notes) < 2:
            return None
        spans = sorted((n["start"], n["end"]) for n in notes)
        covered, cur_s, cur_e = 0.0, spans[0][0], spans[0][1]
        for s, e in spans[1:]:
            if s > cur_e:
                covered += cur_e - cur_s
                cur_s, cur_e = s, e
            else:
                cur_e = max(cur_e, e)
        covered += cur_e - cur_s
        sounding = sum(n["end"] - n["start"] for n in notes)
        bps = (self.bpm or 120) / 60
        per_bar = len(notes) / max(1e-6, (self.duration or spans[-1][1]) * bps / 4)
        return {"polyphony": sounding / covered if covered else 1.0,
                "length_beats": statistics.median((n["end"] - n["start"]) * bps for n in notes),
                "pitch": statistics.median(n["pitch"] for n in notes),
                "notes_per_bar": per_bar}


def name_kind(*names: str | None) -> str | None:
    scores: dict[str, float] = {}
    for n in names:
        for kind, s in _kind_scores(tokens(n or ""), 1.0).items():
            scores[kind] = scores.get(kind, 0) + s
    if not scores:
        return None
    kind, score = max(scores.items(), key=lambda kv: kv[1])
    kind = {"open_hats": "hats", "ride": "hats", "snare_roll": "fx"}.get(kind, kind)
    return kind if score >= MIN_SCORE and kind in KINDS else None


# ── scoring ──────────────────────────────────────────────────────────────────

def _soft(x: float, at: float, width: float) -> float:
    """0..1, rising through ``at`` over about ``width``."""
    return 1 / (1 + math.exp(-(x - at) / width))


@dataclass
class Verdict:
    kind: str
    confidence: float
    reason: str
    runner_up: str | None
    scores: dict[str, float]

    @property
    def level(self) -> str:
        return "high" if self.confidence >= 0.6 else "medium" if self.confidence >= 0.35 else "low"

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.kind, "confidence": round(self.confidence, 2), "level": self.level,
                "reason": self.reason, "runner_up": self.runner_up,
                "scores": {k: round(v, 2) for k, v in sorted(self.scores.items(), key=lambda kv: -kv[1])[:4]}}


def score(f: Features) -> Verdict:
    s: dict[str, float] = {k: 0.0 for k in KINDS}
    why: dict[str, list[str]] = {k: [] for k in KINDS}
    stats = f.note_stats()
    tonal = None if f.key_strength is None else _soft(f.key_strength, 0.55, 0.08)

    if f.stem_share:
        drums, bass_, other, vocals = (f.stem_share.get(k, 0.0) for k in ("drums", "bass", "other", "vocals"))
    else:
        # No stems: lean on key strength and pitch alone.
        t = 0.5 if tonal is None else tonal
        low = stats is not None and stats["pitch"] < 48
        drums, bass_, other, vocals = 1 - t, t * (0.7 if low else 0.1), t * (0.3 if low else 0.9), 0.0

    # Drums: which kind of drum part.
    if drums > 0:
        b = f.drum_bands
        hpb = f.hits_per_beat()
        if b is not None:
            sparse = 1.0 if hpb is None else 1 - _soft(hpb, 1.3, 0.25)
            # A kick swamps the energy, so judge "are there tops at all" in dB: hats in
            # a kit sit around -15 dB, a lone kick's click below -25 dB.
            tops = _soft(10 * math.log10(b.high + 1e-9), -20, 3)
            busy = 0.5 if hpb is None else _soft(hpb, 1.25, 0.2)
            w = {"kick": b.low ** 1.5 * (1.4 - tops) * (1.4 - 0.8 * busy),
                 "hats": b.high * (1.2 - b.low),
                 "clap": b.mid * (1 - b.low) * (0.3 + sparse),
                 "perc": b.mid * (1 - b.low) * (1.3 - sparse),
                 # A full kit has real lows and real tops (or mids) at once, and many hits.
                 "drums": 1.5 * _soft(b.low, 0.25, 0.06) * max(tops, _soft(b.mid, 0.35, 0.06))
                          * (0.6 + 0.8 * busy)}
            total = sum(w.values()) or 1.0
            for k, v in w.items():
                s[k] += drums * v / total
            band_note = f"drums stem lows {b.low:.0%}, mids {b.mid:.0%}, highs {b.high:.0%}"
            if hpb is not None:
                band_note += f", {hpb:.1f} hits a beat"
            for k in DRUM_KINDS:
                why[k].append(band_note)
        else:
            s["drums"] += drums
    if drums >= 0.05 and f.stem_share:
        for k in DRUM_KINDS:
            why[k].insert(0, f"{drums:.0%} of the energy is drums")

    # Bass.
    s["bass"] += bass_
    if f.stem_share and bass_ >= 0.05:
        why["bass"].append(f"{bass_:.0%} of the energy is in the bass stem")
    if stats is not None and stats["pitch"] < 48:
        s["bass"] += 0.15
        why["bass"].append(f"notes sit low (around {_name(stats['pitch'])})")
    # A kick often leaks into Demucs' bass stem: short, low and on the beat.
    if bass_ > 0.2 and f.drum_bands is not None and f.drum_bands.low > 0.6 and \
            stats is not None and stats["length_beats"] < 0.35:
        s["kick"] += 0.5 * bass_
        why["kick"].append("short low hits on the beat")

    # Other: chords, pad, lead or fx.
    if other > 0:
        if stats is None:
            w = {"fx": 1.0, "pad": 0.3, "chords": 0.1, "lead": 0.1}
            note = "almost no clear notes"
        else:
            poly, length, pitch = stats["polyphony"], stats["length_beats"], stats["pitch"]
            many = _soft(poly, 1.7, 0.25)
            long_ = _soft(length, 1.5, 0.4)
            w = {"pad": many * long_ + 0.1,
                 "chords": many * (1 - long_) + 0.1,
                 "lead": (1 - many) * _soft(pitch, 55, 4) + 0.1,
                 "fx": 0.15}
            note = f"{poly:.1f} notes at once, {length:.1f} beats long, around {_name(pitch)}"
        if tonal is not None and tonal < 0.3:
            w["fx"] += 0.6
            note += f", weak key ({f.key_strength:.2f})"
        if f.movement_lu is not None and f.movement_lu > 12:
            w["fx"] += 0.5
            note += f", loudness moves {f.movement_lu:.0f} LU (a riser or sweep?)"
        total = sum(w.values())
        for k, v in w.items():
            s[k] += other * v / total
            why[k].append(note)
        if f.stem_share and other >= 0.05:
            for k in TONAL_KINDS:
                why[k].insert(0, f"{other:.0%} of the energy is melodic/other")

    s["vocal"] += vocals
    if vocals >= 0.05:
        why["vocal"].append(f"{vocals:.0%} of the energy is vocals")

    if f.clap:
        for k in KINDS:
            s[k] = (1 - CLAP_WEIGHT) * s[k] + CLAP_WEIGHT * f.clap.get(k, 0.0)
        top_clap = max(f.clap.items(), key=lambda kv: kv[1])
        why[top_clap[0]].append(f"CLAP hears it as {top_clap[0]} ({top_clap[1]:.0%})")

    if f.name_kind:
        s[f.name_kind] *= NAME_BOOST
        why[f.name_kind].append("the name agrees")

    total = sum(s.values()) or 1.0
    probs = {k: v / total for k, v in s.items()}
    ranked = sorted(probs.items(), key=lambda kv: -kv[1])
    kind, conf = ranked[0]
    reason = "; ".join(dict.fromkeys(why[kind])) or "not much to go on"
    return Verdict(kind, conf, reason, ranked[1][0] if ranked[1][1] > 0 else None, probs)


def _name(pitch: float) -> str:
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    p = int(round(pitch))
    return f"{names[p % 12]}{p // 12 - 2}"


# ── CLAP (only when the service's embeddings are healthy) ────────────────────

def clap_probs(audio_vec: list[float], prompt_vecs: dict[str, list[list[float]]],
               scale: float = 50.0) -> dict[str, float]:
    sims = {k: max(sum(a * b for a, b in zip(audio_vec, v)) for v in vs) for k, vs in prompt_vecs.items()}
    top = max(sims.values())
    exp = {k: math.exp((v - top) * scale) for k, v in sims.items()}
    total = sum(exp.values())
    return {k: v / total for k, v in exp.items()}


def clap_is_healthy(vectors: list[list[float]]) -> bool:
    a, b = vectors
    return sum(x * y for x, y in zip(a, b)) < CLAP_HEALTHY_BELOW
