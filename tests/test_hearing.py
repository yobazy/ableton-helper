"""Hearing tools against a fake audio analysis service (tests/fake_audio_service.py).

Doesn't import the MCP server, so it runs without the ``mcp`` package.
"""

import json

import pytest

import lom
from fake_audio_service import FakeAudioService
from fake_live import FakeClip, FakeSong, make_track
from ableton_helper import hearing
from ableton_helper.audio_service import AudioService, AudioServiceError, AudioServiceUnavailable

KICK_ANSWER = {
    "duration": 2.0,
    "bpm": {"bpm": 124.02, "bpm_rounded": 124, "alternatives": [62.01, 248.04], "confidence": 0.95},
    "key": {"key": "Am", "name": "A minor", "camelot": "8A", "confidence": 0.2, "strength": 0.3},
    "loudness": {"integrated_lufs": -9.0, "range_lu": 2.0, "peak_dbfs": -0.3,
                 "energy_curve": [-9.5, -9.0]},
}


@pytest.fixture
def svc(tmp_path):
    return FakeAudioService(tmp_path / "cache")


@pytest.fixture
def files(tmp_path):
    d = tmp_path / "samples"
    d.mkdir()
    out = {}
    for name in ("Audio 7", "Audio 8", "loop 3", "Vox take"):
        p = d / f"{name}.wav"
        p.write_bytes(b"RIFF" + name.encode() * 50)  # distinct content, distinct hash
        out[name] = str(p)
    return out


@pytest.fixture
def snapshot(files):
    song = FakeSong()
    make_track(song, "Kick", [(0, "kick 909", 4.0, [(36, 0, 0.25, 110)])])  # MIDI
    a7 = make_track(song, "Audio 7", [(0, "Audio 7", 8.0, None)], midi=False)
    a7.clip_slots[0].clip.file_path = files["Audio 7"]
    a8 = make_track(song, "Audio 8", [(0, "Audio 8", 16.0, None), (2, "loop 3", 16.0, None)], midi=False)
    a8.clip_slots[0].clip.file_path = files["Audio 8"]
    a8.clip_slots[2].clip.file_path = files["loop 3"]
    # The same file in the arrangement twice, and once more from bar 9.
    for start in (0.0, 32.0):
        c = FakeClip("Audio 8", 16.0, midi=False, start_time=start, file_path=files["Audio 8"])
        a8.arrangement_clips.append(c)
    vox = make_track(song, "Vox", [], midi=False)
    vox.arrangement_clips.append(FakeClip("Vox take", 32.0, midi=False, start_time=64.0,
                                          file_path=files["Vox take"]))
    return lom.snapshot(song, include_devices=False)


# ── client ───────────────────────────────────────────────────────────────────

def test_upload_only_when_the_service_hasnt_got_it(svc, files):
    svc.answers["Audio 7"] = KICK_ANSWER
    first = svc.analyze(files["Audio 7"], ["bpm", "key"])
    assert first["results"]["bpm"]["bpm_rounded"] == 124
    assert [k for _m, _p, k in svc.calls] == ["json", "multipart"]  # by hash, 404, upload

    svc.calls.clear()
    svc._hashes.clear()
    # A fresh feature for a stored file goes by hash with no upload.
    svc.analyze(files["Audio 7"], ["loudness"])
    assert [k for _m, _p, k in svc.calls] == ["json"]


def test_results_are_cached_locally_by_hash(svc, files, tmp_path):
    svc.answers["Audio 7"] = KICK_ANSWER
    svc.analyze(files["Audio 7"], ["bpm", "key"])
    svc.calls.clear()
    again = svc.analyze(files["Audio 7"], ["bpm", "key"])
    assert svc.calls == []
    assert set(again["cached"]) == {"bpm", "key"}
    assert again["duration"] == 2.0
    # A new client with the same cache dir still hits it.
    other = FakeAudioService(tmp_path / "cache")
    other.analyze(files["Audio 7"], ["bpm"])
    assert other.calls == []


def test_errors_are_not_cached(svc, files):
    svc.answers["Audio 7"] = {"bpm": {"error": "boom"}}
    assert "error" in svc.analyze(files["Audio 7"], ["bpm"])["results"]["bpm"]
    svc.answers["Audio 7"] = KICK_ANSWER
    assert svc.analyze(files["Audio 7"], ["bpm"])["results"]["bpm"]["bpm"] == 124.02


def test_unreachable_service_says_so_plainly(tmp_path, files):
    real = AudioService("http://127.0.0.1:9", tmp_path / "cache")  # discard port: nothing listens
    with pytest.raises(AudioServiceUnavailable, match="home PC"):
        real.health()
    with pytest.raises(AudioServiceUnavailable):
        real.analyze(files["Audio 7"], ["bpm"])


def test_missing_file_is_a_clear_error(svc, tmp_path):
    with pytest.raises(AudioServiceError, match="not found"):
        svc.analyze(tmp_path / "gone.wav", ["bpm"])


def test_service_error_carries_its_code(svc, files):
    from ableton_helper.audio_service import Response
    svc.stored["x"] = "x"
    resp = Response(422, json.dumps({"detail": {"error": "undecodable", "message": "Not audio"}}).encode(), "")
    with pytest.raises(AudioServiceError, match="Not audio") as info:
        svc._raise_for(resp, "/v1/analyze")
    assert info.value.code == "undecodable"


# ── finding the audio in a set ───────────────────────────────────────────────

def test_session_clip_source(snapshot, files):
    [s] = hearing.find_sources(snapshot, track=1, slot=0)
    assert s.path == files["Audio 7"]
    assert "track 1 'Audio 7', slot 0" in s.label


def test_arrangement_clip_by_bar(snapshot, files):
    [s] = hearing.find_sources(snapshot, track=3, bar=20)
    assert s.path == files["Vox take"] and s.bar == 17


def test_track_lists_each_file_once(snapshot, files):
    sources = hearing.find_sources(snapshot, track=2)
    assert [s.path for s in sources] == [files["Audio 8"], files["loop 3"]]


def test_whole_set(snapshot):
    assert len(hearing.find_sources(snapshot)) == 4


def test_midi_clips_have_no_audio(snapshot):
    with pytest.raises(hearing.HearingError, match="MIDI clip"):
        hearing.find_sources(snapshot, track=0, slot=0)
    with pytest.raises(hearing.HearingError, match="no audio clips"):
        hearing.find_sources(snapshot, track=0)


def test_bad_addresses(snapshot):
    with pytest.raises(hearing.HearingError, match="No track 9"):
        hearing.find_sources(snapshot, track=9)
    with pytest.raises(hearing.HearingError, match="slot 5"):
        hearing.find_sources(snapshot, track=1, slot=5)
    with pytest.raises(hearing.HearingError, match="bar 99"):
        hearing.find_sources(snapshot, track=3, bar=99)
    with pytest.raises(hearing.HearingError, match="No file"):
        hearing.find_sources(None, path="/nowhere/at/all.wav")


# ── analyze ──────────────────────────────────────────────────────────────────

def test_analyze_summary(svc, snapshot):
    svc.answers["Audio 7"] = KICK_ANSWER
    out = hearing.analyze(svc, hearing.find_sources(snapshot, track=1, slot=0))
    [item] = out["files"]
    assert item["bpm"] == 124 and item["bpm_confidence"] == 0.95
    assert item["key"] == "A minor (8A)"
    assert item["bars_at_bpm"] == 1.03
    assert item["loudness"]["integrated_lufs"] == -9.0
    assert item["energy"]["level"] == "high"
    assert item["energy"]["curve_lufs"] == [-9.5, -9.0]


def test_analyze_keeps_going_past_a_bad_file(svc, snapshot, files):
    svc.answers["Audio 8"] = KICK_ANSWER
    sources = hearing.find_sources(snapshot, track=2)
    sources.insert(0, hearing.AudioSource("/moved/away.wav", 2, "Audio 8", "moved", slot=4))
    out = hearing.analyze(svc, sources)
    assert "not found" in out["files"][0]["error"]
    assert out["files"][1]["bpm"] == 124
    assert "errors" in out["files"][2]  # no fake answer for loop 3: per-feature errors


def test_analyze_unreachable_raises(svc, snapshot):
    svc.down = True
    with pytest.raises(AudioServiceUnavailable):
        hearing.analyze(svc, hearing.find_sources(snapshot, track=1))


def test_energy_curve_is_squashed_in_power():
    curve = [-10.0] * 50 + [-30.0] * 50
    squashed, step = hearing.squash_curve(curve, points=4)
    assert step == 25 and squashed == [-10.0, -10.0, -30.0, -30.0]
    mixed, _ = hearing.squash_curve([-10.0, -20.0], points=1)
    assert mixed[0] == -12.6  # mean power, not mean dB (-15)
    assert hearing.energy_level(-20) == "low" and hearing.energy_level(-14) == "medium"


# ── spectrogram ──────────────────────────────────────────────────────────────

def test_spectrogram_is_a_cached_png(svc, snapshot):
    [source] = hearing.find_sources(snapshot, track=1, slot=0)
    info = hearing.spectrogram(svc, source, width=600, height=200)
    with open(info["image"], "rb") as f:
        assert f.read(4) == b"\x89PNG"
    svc.calls.clear()
    assert hearing.spectrogram(svc, source, width=600, height=200)["image"] == info["image"]
    assert svc.calls == []
    # Other options are another image.
    assert hearing.spectrogram(svc, source, start=1.0, duration=2.0)["image"] != info["image"]


def test_spectrogram_checks_sizes(svc, snapshot):
    [source] = hearing.find_sources(snapshot, track=1, slot=0)
    with pytest.raises(hearing.HearingError, match="width"):
        hearing.spectrogram(svc, source, width=10)


# ── stems ────────────────────────────────────────────────────────────────────

def test_stems_are_downloaded_and_cached(svc, snapshot):
    [source] = hearing.find_sources(snapshot, track=1, slot=0)
    out = hearing.stems(svc, source)
    assert set(out["stems"]) == {"drums", "bass", "other", "vocals"}
    with open(out["stems"]["bass"], "rb") as f:
        assert f.read() == b"bass of Audio 7"
    assert out["cached"] is False
    svc.calls.clear()
    again = hearing.stems(svc, source)
    assert again["cached"] is True and svc.calls == []


def test_stems_redownload_when_a_file_is_gone(svc, snapshot):
    import os
    [source] = hearing.find_sources(snapshot, track=1, slot=0)
    out = hearing.stems(svc, source)
    os.remove(out["stems"]["drums"])
    assert hearing.stems(svc, source)["cached"] is False


def test_stems_options(svc, snapshot):
    [source] = hearing.find_sources(snapshot, track=1, slot=0)
    with pytest.raises(hearing.HearingError, match="htdemucs_6s"):
        hearing.stems(svc, source, model="spleeter")
    with pytest.raises(hearing.HearingError, match="flac or wav"):
        hearing.stems(svc, source, fmt="mp3")
