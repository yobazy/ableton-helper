"""The hearing MCP tools end to end: fake Live set + fake analysis service."""

import json

import pytest

import lom
import tones
from fake_audio_service import FakeAudioService
from fake_live import FakeApp, FakeClip, FakeSong, make_track, note_spec
from ableton_helper import server


class FakeLive:
    def __init__(self, song, version="0.2.0"):
        self.song, self.version = song, version
        self.executor = lom.Executor(song, FakeApp(), note_spec)
        self.batches = []

    def ping(self):
        return {"name": "AbletonHelper", "version": self.version}

    def snapshot(self, include_notes=False, include_devices=True):
        return lom.snapshot(self.song, include_notes, include_devices)

    def run_ops(self, ops, stop_on_error=False):
        self.batches.append(ops)
        return self.executor.run(ops, stop_on_error)


@pytest.fixture
def files(tmp_path):
    out = {}
    for name in ("Audio 7", "Bass loop"):
        p = tmp_path / f"{name}.wav"
        p.write_bytes(b"RIFF" + name.encode() * 30)
        out[name] = str(p)
    return out


@pytest.fixture
def song(files):
    song = FakeSong()
    song.tempo = 124.0
    make_track(song, "Kick", [(0, "kick", 4.0, [(36, 0, 0.25, 110)])])
    a7 = make_track(song, "Audio 7", [(0, "Audio 7", 8.0, None)], midi=False)
    a7.clip_slots[0].clip.file_path = files["Audio 7"]
    bass = make_track(song, "Bass loop", [(0, "Bass loop", 8.0, None)], midi=False)
    bass.clip_slots[0].clip.file_path = files["Bass loop"]
    bass.arrangement_clips.append(FakeClip("Bass loop", 8.0, midi=False, start_time=0.0,
                                           file_path=files["Bass loop"]))
    return song


@pytest.fixture
def live(song, monkeypatch):
    fake = FakeLive(song)
    monkeypatch.setattr(server, "_live", fake)
    return fake


@pytest.fixture
def svc(tmp_path, monkeypatch):
    fake = FakeAudioService(tmp_path / "cache")
    monkeypatch.setattr(server, "_audio", fake)
    return fake


ANSWER = {"duration": 3.87, "bpm": {"bpm": 124.0, "bpm_rounded": 124, "confidence": 0.95},
          "key": {"key": "Am", "name": "A minor", "camelot": "8A", "confidence": 0.8, "strength": 0.8},
          "loudness": {"integrated_lufs": -12.0, "range_lu": 3.0, "peak_dbfs": -1.0, "energy_curve": [-12, -12]}}


def test_analyze_tool(live, svc):
    svc.answers["Audio 7"] = ANSWER
    out = json.loads(server.analyze_audio(track=1, slot=0))
    assert out["files"][0]["bpm"] == 124 and out["files"][0]["key"] == "A minor (8A)"


def test_tools_say_when_the_pc_is_off(live, svc):
    svc.down = True
    for text in (server.analyze_audio(track=1, slot=0), server.separate_stems(track=1, slot=0),
                 server.audio_to_midi(track=1, slot=0), server.tag_loop_roles()):
        assert text.startswith("Error:") and "audio analysis service" in text


def test_one_file_tools_want_one_file(live, svc):
    text = server.audio_spectrogram(track=2)  # session + arrangement are the same file: one source
    assert not isinstance(text, str)
    song_track = live.song.tracks[2]
    song_track.arrangement_clips.append(FakeClip("other", 8.0, midi=False, start_time=32.0,
                                                 file_path=live.song.tracks[1].clip_slots[0].clip.file_path))
    text = server.separate_stems(track=2)
    assert text.startswith("Error:") and "works on one" in text
    assert "The set has" in server.audio_spectrogram()


def test_spectrogram_tool_returns_an_image(live, svc):
    info, image = server.audio_spectrogram(track=1, slot=0)
    assert json.loads(info)["image"] == image.path


def test_midi_clip_is_a_tool_error(live, svc):
    assert "MIDI clip" in server.analyze_audio(track=0, slot=0)


def test_audio_to_midi_adds_a_session_clip(live, svc):
    svc.answers["Audio 7"] = ANSWER
    svc.transcriptions["Audio 7"] = [{"start": 0.0, "end": 0.24, "pitch": 45, "velocity": 0.8},
                                     {"start": 0.97, "end": 1.2, "pitch": 48, "velocity": 0.8}]
    out = json.loads(server.audio_to_midi(track=1, slot=0, add_to_live=True))
    assert out["added_to_live"] and out["new_track"]["index"] == 2
    track = live.song.tracks[2]
    assert track.name == "Audio 7 (MIDI)"
    clip = track.clip_slots[0].clip
    assert clip.length == 8.0 and [(n.pitch, n.start_time) for n in clip.notes] == [(45, 0.0), (48, 2.0)]
    assert len(live.batches) == 1


def test_audio_to_midi_with_an_old_remote_script(live, svc):
    live.version = "0.1.0"
    svc.answers["Audio 7"] = ANSWER
    svc.transcriptions["Audio 7"] = [{"start": 0.0, "end": 0.24, "pitch": 45, "velocity": 0.8}]
    text = server.audio_to_midi(track=1, slot=0, add_to_live=True)
    assert "ableton-helper install" in text and live.batches == []
    # The Arrangement route works with the old script.
    out = json.loads(server.audio_to_midi(track=1, slot=0, add_to_live=True, place_at_bar=9))
    assert out["added_to_live"]
    assert live.song.tracks[2].arrangement_clips[0].start_time == 32.0


def test_tag_loop_roles_renames_only_unclear_tracks(live, svc, tmp_path):
    silence = tones._write(tmp_path / "s.wav", [0.0] * 2000).read_bytes()
    bass = tones.bass(tmp_path / "b.wav").read_bytes()
    svc.stem_audio["Audio 7"] = {"drums": silence, "bass": bass, "other": silence, "vocals": silence}
    svc.answers["Audio 7"] = ANSWER
    out = json.loads(server.tag_loop_roles(rename_tracks=True))
    assert [it["track"] for it in out["loops"]] == [1]  # "Bass loop" already says what it is
    assert out["loops"][0]["role"] == "bass"
    assert out["renamed"] == ["track 1: 'Audio 7' → 'Bass (Audio 7)'"]
    assert live.song.tracks[1].name == "Bass (Audio 7)"


def test_detect_sections_when_the_service_lacks_them(live, svc, files):
    out = json.loads(server.detect_sections(path=files["Audio 7"]))
    assert out["available"] is False and "sections" in out["why"]
