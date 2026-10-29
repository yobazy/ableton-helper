"""Short synthetic loops for tests (standard library only, no samples or recordings).

Each generator writes a mono 16-bit WAV at 22.05 kHz and returns its path. They're
crude on purpose: enough for a model to tell a low sustained tone from noise ticks.
"""

from __future__ import annotations

import math
import random
import struct
import wave
from pathlib import Path

SR = 22050


def _write(path: Path, samples: list[float]) -> Path:
    peak = max(1e-9, max(abs(s) for s in samples))
    gain = 0.8 / peak
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"".join(struct.pack("<h", int(s * gain * 32767)) for s in samples))
    return path


def _beats(bpm: float, bars: int) -> tuple[int, float]:
    spb = 60.0 / bpm
    return int(bars * 4 * spb * SR), spb


def kick(path: Path, bpm: float = 124, bars: int = 2) -> Path:
    n, spb = _beats(bpm, bars)
    out = [0.0] * n
    for b in range(bars * 4):
        start = int(b * spb * SR)
        phase = 0.0
        for i in range(int(0.35 * SR)):
            if start + i >= n:
                break
            t = i / SR
            freq = 45 + 110 * math.exp(-t * 30)  # pitch drop
            phase += 2 * math.pi * freq / SR
            out[start + i] += math.sin(phase) * math.exp(-t * 9)
    return _write(path, out)


def hats(path: Path, bpm: float = 124, bars: int = 2, seed: int = 1) -> Path:
    rnd = random.Random(seed)
    n, spb = _beats(bpm, bars)
    out = [0.0] * n
    for e in range(bars * 8):  # eighths, accent on the offbeat
        start = int(e * spb / 2 * SR)
        amp = 1.0 if e % 2 else 0.5
        prev = 0.0
        for i in range(int(0.05 * SR)):
            if start + i >= n:
                break
            x = rnd.uniform(-1, 1)
            out[start + i] += (x - prev) * amp * math.exp(-i / SR * 70)  # differencing ≈ high-pass
            prev = x
    return _write(path, out)


def bass(path: Path, bpm: float = 124, bars: int = 2) -> Path:
    n, spb = _beats(bpm, bars)
    out = [0.0] * n
    pattern = [45, 45, 48, 43]  # A1 A1 C2 G1 (MIDI)
    for e in range(bars * 8):
        start = int((e * spb / 2 + spb / 4) * SR)  # offbeat sixteenth push
        f = 440 * 2 ** ((pattern[(e // 2) % 4] - 69) / 12)
        for i in range(int(spb / 2 * 0.9 * SR)):
            if start + i >= n:
                break
            t = i / SR
            # Saw-ish: a few harmonics.
            s = sum(math.sin(2 * math.pi * f * h * t) / h for h in (1, 2, 3, 4))
            out[start + i] += s * min(1.0, t * 200) * math.exp(-t * 3)
    return _write(path, out)


def pad(path: Path, bpm: float = 124, bars: int = 2) -> Path:
    n, _spb = _beats(bpm, bars)
    chord = [57, 60, 64, 67]  # Am7
    out = []
    for i in range(n):
        t = i / SR
        env = min(1.0, t / 0.8) * min(1.0, (n - i) / SR / 0.8)
        s = 0.0
        for p in chord:
            f = 440 * 2 ** ((p - 69) / 12)
            s += math.sin(2 * math.pi * f * t) + 0.3 * math.sin(2 * math.pi * f * 1.003 * t)
        out.append(s * env * (0.8 + 0.2 * math.sin(2 * math.pi * 0.3 * t)))
    return _write(path, out)


def lead(path: Path, bpm: float = 124, bars: int = 2) -> Path:
    """A one-note-at-a-time square-ish melody around A4."""
    n, spb = _beats(bpm, bars)
    out = [0.0] * n
    melody = [69, 72, 76, 74, 72, 69, 67, 69]
    for e in range(bars * 8):
        start = int(e * spb / 2 * SR)
        f = 440 * 2 ** ((melody[e % 8] - 69) / 12)
        for i in range(int(spb / 2 * 0.8 * SR)):
            if start + i >= n:
                break
            t = i / SR
            s = sum(math.sin(2 * math.pi * f * h * t) / h for h in (1, 3, 5))
            out[start + i] += s * min(1.0, t * 300) * math.exp(-t * 4)
    return _write(path, out)


def drum_loop(path: Path, bpm: float = 124, bars: int = 2) -> Path:
    """Kick and hats together: a full kit."""
    a, b = path.with_suffix(".k.wav"), path.with_suffix(".h.wav")
    kick(a, bpm, bars)
    hats(b, bpm, bars)
    with wave.open(str(a)) as wa, wave.open(str(b)) as wb:
        ka = struct.unpack(f"<{wa.getnframes()}h", wa.readframes(wa.getnframes()))
        hb = struct.unpack(f"<{wb.getnframes()}h", wb.readframes(wb.getnframes()))
    a.unlink()
    b.unlink()
    return _write(path, [x / 32768 + 0.8 * y / 32768 for x, y in zip(ka, hb)])


def tone(path: Path, freq: float = 440.0, seconds: float = 3.0) -> Path:
    """A plain sine with a soft start and end: the integration test's input."""
    n = int(seconds * SR)
    out = [math.sin(2 * math.pi * freq * i / SR) * min(1.0, i / 500, (n - i) / 500) for i in range(n)]
    return _write(path, out)


GENERATORS = {"kick": kick, "hats": hats, "bass": bass, "pad": pad, "lead": lead, "drums": drum_loop}
