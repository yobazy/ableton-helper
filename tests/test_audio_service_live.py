"""Opt-in: the real audio analysis service on the home PC, with generated sounds only.

    ABLETON_HELPER_LIVE_AUDIO=1 uv run pytest tests/test_audio_service_live.py

Uses a fresh cache dir, so every call goes to the service. AUDIO_ANALYSIS_URL picks
another address (the dev copy on :8771, say). Takes about half a minute when the
models are warm; the first call after the PC has been idle loads them.
"""

import os

import pytest

import tones
from ableton_helper import hearing
from ableton_helper.audio_service import AudioService

pytestmark = pytest.mark.skipif(not os.environ.get("ABLETON_HELPER_LIVE_AUDIO"),
                                reason="set ABLETON_HELPER_LIVE_AUDIO=1 to call the real service")


@pytest.fixture
def svc(tmp_path):
    return AudioService(os.environ.get("AUDIO_ANALYSIS_URL", "https://pc.taila3582d.ts.net:8770"),
                        tmp_path / "cache")


def test_a_440_hz_tone(svc, tmp_path):
    path = tones.tone(tmp_path / "a440.wav", 440.0, seconds=3.0)
    source = hearing.AudioSource(str(path))

    health = svc.health()
    assert health["ok"] and "bpm" in health["features"]

    out = hearing.analyze(svc, [source])["files"][0]
    assert "error" not in out, out
    assert 2.9 < out["seconds"] < 3.1
    assert out["key"].startswith("A ")                    # A major or A minor
    assert -30 < out["loudness"]["integrated_lufs"] < 0

    png = hearing.spectrogram(svc, source, width=400, height=200)["image"]
    with open(png, "rb") as f:
        assert f.read(4) == b"\x89PNG"

    midi = hearing.audio_to_midi(svc, source, bpm=120)
    assert midi["note_count"] >= 1 and midi["lowest_midi"] <= 69 <= midi["highest_midi"]
    with open(midi["midi_file"], "rb") as f:
        assert f.read(4) == b"MThd"


def test_stems_and_roles_on_generated_loops(svc, tmp_path):
    kick = tones.kick(tmp_path / "Audio 1.wav")
    bass = tones.bass(tmp_path / "Audio 2.wav")
    stems = svc.stems(kick, fmt="wav")["stems"]
    assert set(stems) >= {"drums", "bass", "other", "vocals"} and all(os.path.isfile(p) for p in stems.values())
    out = hearing.tag_roles(svc, [hearing.AudioSource(str(kick)), hearing.AudioSource(str(bass))])
    assert [it.get("role") for it in out["loops"]] == ["kick", "bass"], out
