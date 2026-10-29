"""A generated "reference track" for tests: stems per part, laid out by section.

Standard library only. Low sample rate on purpose so a 48-bar track builds in about a
second; the parts are crude (a decaying 55 Hz thump, noise ticks, sine tones) but
separate cleanly in level and band, which is all the section hearing needs.
"""

from __future__ import annotations

import math
import random
import struct
import wave
from pathlib import Path

RATE = 6000
BPM = 120.0
BAR_SECONDS = 4 * 60.0 / BPM  # 2 s

# (name, bars, parts) — the shape of a small club track.
LAYOUT = [
    ("intro", 8, {"kick", "hats"}),
    ("inst", 8, {"kick", "hats", "bass", "music"}),
    ("break", 8, {"music", "vocal"}),
    ("chorus", 16, {"kick", "hats", "bass", "music", "vocal"}),
    ("outro", 8, {"kick", "hats"}),
]


def _write(path: Path, samples: list[float]) -> bytes:
    data = b"".join(struct.pack("<h", max(-32767, min(32767, int(s * 32767)))) for s in samples)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(data)
    return path.read_bytes()


def _spans(part: str, layout=LAYOUT) -> list[tuple[float, float]]:
    out, bar = [], 0
    for _label, bars, parts in layout:
        if part in parts:
            out.append((bar * BAR_SECONDS, (bar + bars) * BAR_SECONDS))
        bar += bars
    return out


def total_seconds(layout=LAYOUT) -> float:
    return sum(b for _, b, _ in layout) * BAR_SECONDS


_made: dict[int, dict[str, bytes]] = {}


def stems(folder: Path, layout=LAYOUT) -> dict[str, bytes]:
    """WAV bytes for drums, bass, other and vocals, also written to ``folder``."""
    if id(layout) not in _made:
        _made[id(layout)] = _render(folder, layout)
    for name, data in _made[id(layout)].items():
        (folder / f"{name}.wav").write_bytes(data)
    return _made[id(layout)]


def _render(folder: Path, layout) -> dict[str, bytes]:
    n = int(total_seconds(layout) * RATE)
    beat = 60.0 / BPM
    rnd = random.Random(7)
    drums = [0.0] * n
    for a, b in _spans("kick", layout):
        t = a
        while t < b - 1e-9:
            start = int(t * RATE)
            for i in range(int(0.2 * RATE)):
                if start + i < n:
                    drums[start + i] += 0.8 * math.sin(2 * math.pi * 55 * i / RATE) * math.exp(-i / RATE * 12)
            t += beat
    for a, b in _spans("hats", layout):
        t = a + beat / 2
        while t < b - 1e-9:
            start = int(t * RATE)
            prev = 0.0
            for i in range(int(0.04 * RATE)):
                if start + i < n:
                    x = rnd.uniform(-1, 1)
                    drums[start + i] += 0.5 * (x - prev) * math.exp(-i / RATE * 80)  # bright noise
                    prev = x
            t += beat

    def tone(part: str, freqs: list[float], amp: float) -> list[float]:
        out = [0.0] * n
        for a, b in _spans(part, layout):
            for i in range(int(a * RATE), min(n, int(b * RATE))):
                out[i] = amp * sum(math.sin(2 * math.pi * f * i / RATE) for f in freqs) / len(freqs)
        return out

    return {
        "drums": _write(folder / "drums.wav", drums),
        "bass": _write(folder / "bass.wav", tone("bass", [55.0], 0.6)),
        "other": _write(folder / "other.wav", tone("music", [220.0, 277.2, 329.6], 0.4)),
        "vocals": _write(folder / "vocals.wav", tone("vocal", [440.0], 0.3)),
    }


def mixdown(path: Path, layout=LAYOUT) -> Path:
    """All the parts in one file, for the real service (opt-in test)."""
    folder = path.parent / (path.stem + "-stems")
    folder.mkdir(exist_ok=True)
    stems(folder, layout)
    tracks = []
    for name in ("drums", "bass", "other", "vocals"):
        with wave.open(str(folder / f"{name}.wav"), "rb") as w:
            raw = w.readframes(w.getnframes())
        tracks.append(struct.unpack(f"<{len(raw) // 2}h", raw))
    mixed = [sum(vals) / 32768.0 / 2 for vals in zip(*tracks)]
    _write(path, mixed)
    return path


def sections_answer(jitter: float = 0.3, loudness=(-14.0, -12.0, -16.0, -8.0, -13.0), layout=LAYOUT) -> dict:
    """What /v1/sections would say about the layout: boundaries a little off the bar lines."""
    segments, bar = [], 0
    for i, (label, bars, _parts) in enumerate(layout):
        start = bar * BAR_SECONDS + (jitter if i else 0.0)
        bar += bars
        end = bar * BAR_SECONDS + (jitter if i < len(layout) - 1 else 0.0)
        segments.append({"start": start, "end": end, "label": label, "loudness_lufs": loudness[i],
                         "energy": round(10 ** ((loudness[i] - max(loudness)) / 20), 3)})
    total_bars = sum(b for _, b, _ in layout)
    return {"bpm": BPM, "bpm_rounded": int(BPM), "steadiness": 0.99,
            "downbeats": [i * BAR_SECONDS for i in range(total_bars)],
            "beats": [i * BAR_SECONDS / 4 for i in range(total_bars * 4)],
            "segments": segments, "method": "allin1:harmonix-all", "device": "cuda"}
