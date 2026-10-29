"""mark_sections and compare_arrangement against a fake Live set."""

import json

import pytest

from fake_live import FakeClip, FakeCue, FakeSong, make_track
from test_hearing_tools import FakeLive
from ableton_helper import reference, server
from ableton_helper import templates as tpl

BAR = 4.0

TEMPLATE = {
    "genre": "ref_small", "name": "Reference: Small", "bpm": 124, "bpm_range": [121, 127],
    "roles": {k: dict(v) for k, v in reference.REF_ROLES.items()},
    "variants": {"reference": {"sections": [
        {"name": "Intro", "bars": 8, "energy": 6, "roles": ["kick", "hats"]},
        {"name": "Groove", "bars": 8, "energy": 8, "roles": ["kick", "hats", "bass", "music"]},
        {"name": "Breakdown", "bars": 8, "energy": 4, "roles": ["music", "vocal"]},
        {"name": "Drop", "bars": 16, "energy": 10, "roles": ["kick", "hats", "bass", "music", "vocal"]},
        {"name": "Outro", "bars": 8, "energy": 6, "roles": ["kick", "hats"]},
    ], "transitions": []}},
}


@pytest.fixture
def small(tmp_path, monkeypatch):
    monkeypatch.setenv("ABLETON_HELPER_TEMPLATES", str(tmp_path / "templates"))
    tpl.load_template.cache_clear()
    reference.save_template("ref_small", TEMPLATE)
    yield "ref_small"
    tpl.load_template.cache_clear()


def _clip(name, bar, bars, muted=False):
    c = FakeClip(name, bars * BAR, True, (bar - 1) * BAR)
    c.muted = muted
    return c


@pytest.fixture
def song():
    s = FakeSong()
    s.tempo = 124.0
    kick = make_track(s, "Kick", [(0, "kick", 4.0, [(36, 0, 0.25, 110)])])
    kick.arrangement_clips = [_clip("kick", b, 8) for b in range(1, 49, 8)]
    bass = make_track(s, "Bass", [(0, "bass", 4.0, [(36, 0, 1, 100)])])
    bass.arrangement_clips = [_clip("bass", 9, 8), _clip("bass", 25, 16, muted=True)]
    chords = make_track(s, "Chords", [(0, "chords", 4.0, [(60, 0, 4, 90)])])
    chords.arrangement_clips = [_clip("chords", 1, 48)]
    make_track(s, "Vocal", [(0, "vox", 4.0, [(60, 0, 1, 90)])])
    weird = make_track(s, "Audio 3", [], midi=True)
    weird.arrangement_clips = [_clip("Audio 3", 1, 8)]
    return s


@pytest.fixture
def live(song, monkeypatch):
    fake = FakeLive(song, version="0.3.0")
    monkeypatch.setattr(server, "_live", fake)
    return fake


# ── mark_sections ────────────────────────────────────────────────────────────

def test_mark_sections_shows_the_plan_first(live, small):
    out = json.loads(server.mark_sections(small))
    assert out["dry_run"] and live.batches == [] and live.song.cue_points == []
    assert "| 25 | 0:46 | Drop | 16 | 10 | locator |" in out["plan"]
    assert [op["op"] for op in out["op_list"]].count("create_locator") == 5


def test_mark_sections_makes_locators_scenes_and_blocks(live, small):
    out = json.loads(server.mark_sections(small, scenes=True, section_track=True, dry_run=False))
    assert out["ok"] == out["ops"] and len(live.batches) == 1
    song = live.song
    assert [(c.time / BAR + 1, c.name) for c in song.cue_points] == \
        [(1, "Intro"), (9, "Groove"), (17, "Breakdown"), (25, "Drop"), (41, "Outro")]
    assert [s.name for s in song.scenes] == ["Intro (8 bars)", "Groove (8 bars)", "Breakdown (8 bars)",
                                             "Drop (16 bars)", "Outro (8 bars)"]
    sections = song.tracks[-1]
    assert sections.name == "Sections"
    assert [(c.start_time / BAR + 1, c.length / BAR, c.name) for c in sections.arrangement_clips][3] == \
        (25, 16, "Drop (energy 10)")
    # Running it again reuses the track and rewrites its blocks.
    json.loads(server.mark_sections(small, section_track=True, dry_run=False))
    assert [t.name for t in song.tracks].count("Sections") == 1
    assert len(song.tracks[-1].arrangement_clips) == 5
    assert len(song.cue_points) == 5


def test_mark_sections_keeps_or_replaces_other_locators(live, small):
    other = FakeCue(99 * BAR)
    other.name = "Old idea"
    live.song.cue_points.append(other)
    out = json.loads(server.mark_sections(small, start_bar=9))
    assert any("Keeps the set's other locators: 'Old idea' (bar 100)" in n for n in out["notes"])
    assert "| 9 | " in out["plan"]
    json.loads(server.mark_sections(small, start_bar=9, replace_locators=True, dry_run=False))
    assert [c.name for c in live.song.cue_points] == ["Intro", "Groove", "Breakdown", "Drop", "Outro"]
    assert live.song.cue_points[0].time == 8 * BAR


def test_scenes_need_the_new_remote_script(live, small):
    live.version = "0.2.0"
    text = server.mark_sections(small, scenes=True, dry_run=False)
    assert "ableton-helper install" in text and live.batches == []
    assert json.loads(server.mark_sections(small, dry_run=False))["ok"]  # locators work with 0.2.0


def test_mark_sections_errors(live, small):
    assert "Nothing to do" in server.mark_sections(small, locators=False)
    assert "start_bar" in server.mark_sections(small, start_bar=0)
    assert server.mark_sections("no such genre").startswith("Error: No template")
    out = json.loads(server.mark_sections("house"))  # genres work too, extended by default
    assert "House extended" in out["template"]


# ── compare_arrangement ──────────────────────────────────────────────────────

def test_compare_lists_concrete_changes(live, small):
    out = json.loads(server.compare_arrangement(small))
    assert out["aligned_to"].startswith("bar 1")
    says = [c["say"] for c in out["changes"]]
    assert "Mute 'Kick' in Breakdown (bars 17-24): the template has no kick there." in says
    assert "Unmute 'Bass' in Drop (bars 25-40): its clips there are switched off." in says
    assert "Add 'Vocal' to Breakdown, Drop (bars 17-40), from Session slot 0." in says
    assert "Mute 'Chords' in Intro (bars 1-8): the template has no chords there." in says
    assert "Mute 'Chords' in Outro (bars 41-48): the template has no chords there." in says
    assert len(says) == 5
    assert out["missing_parts"] == [{"part": "hats", "sections": ["Intro", "Groove", "Drop", "Outro"]}]
    assert out["not_compared"]["tracks"] == ["4 'Audio 3'"]
    assert out["grid"][2]["playing"] == ["Kick", "Chords"]
    assert live.batches == []


def test_apply_mutes_and_unmutes_whole_clips(live, small):
    out = json.loads(server.compare_arrangement(small, apply=True))
    song = live.song
    assert [c.muted for c in song.tracks[0].arrangement_clips] == [False, False, True, False, False, False]
    assert [c.muted for c in song.tracks[1].arrangement_clips] == [False, False]
    assert not song.tracks[2].arrangement_clips[0].muted   # one long clip: can't be split
    assert "Switched 2 clips" in out["applied"]
    assert len(out["needs_a_split"]["changes"]) == 2 and "Cmd+E" in out["needs_a_split"]["why"]
    # The kick's breakdown is now handled; asking again doesn't repeat it.
    again = json.loads(server.compare_arrangement(small))
    assert not any(c["track_name"] == "Kick" for c in again["changes"])
    assert not any(c["track_name"] == "Bass" for c in again["changes"])


def test_apply_needs_the_new_remote_script(live, small):
    live.version = "0.2.0"
    assert "ableton-helper install" in server.compare_arrangement(small, apply=True)
    assert live.batches == []


def test_compare_spots_a_missing_breakdown_and_moved_sections(monkeypatch, small):
    s = FakeSong()
    for name in ("Kick", "Bass", "Hats", "Chords", "Vocal"):
        t = make_track(s, name, [(0, name.lower(), 4.0, [(36, 0, 1, 100)])])
        t.arrangement_clips = [_clip(name.lower(), b, 8) for b in range(1, 57, 8)]  # all along, 56 bars
    for bar, name in ((9, "Intro"), (49, "Drop")):
        cue = FakeCue((bar - 1) * BAR)
        cue.name = name
        s.cue_points.append(cue)
    monkeypatch.setattr(server, "_live", FakeLive(s, version="0.3.0"))
    out = json.loads(server.compare_arrangement(small))
    assert out["start_bar"] == 9 and "'Intro' locator" in out["aligned_to"]
    text = " ".join(out["summary"])
    assert "breaks down at Breakdown (bars 25-32, energy 8 to 4) but the set keeps going: take out Kick, Bass, Hats." in text
    assert "winds down at Outro" in text
    assert "Drop at bar 49, template bar 33" in text
    assert all(c["bar"] >= 9 for c in out["changes"])
    assert any(c["action"] == "mute" and c["track_name"] == "Kick" and c["bar"] == 25 for c in out["changes"])


def test_compare_with_nothing_arranged(monkeypatch, small):
    s = FakeSong()
    make_track(s, "Kick", [(0, "kick", 4.0, None)])
    monkeypatch.setattr(server, "_live", FakeLive(s))
    out = json.loads(server.compare_arrangement(small))
    assert out["changes"] == [] and "arrange_session" in out["summary"][0]


def test_lanes_and_unknown_kinds_are_left_out(live, small):
    lane = make_track(live.song, "Risers & Sweeps", [])
    lane.arrangement_clips = [_clip("Riser", 17, 8)]
    fx = make_track(live.song, "Crash FX", [])
    fx.arrangement_clips = [_clip("crash", 25, 1)]
    out = json.loads(server.compare_arrangement(small))
    assert not any(c["track_name"] in ("Risers & Sweeps", "Crash FX") for c in out["changes"])
    assert out["not_in_template"] == ["6 'Crash FX' (fx)"]
