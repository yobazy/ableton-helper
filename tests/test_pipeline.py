"""Template → plan → ops → executor, end to end against the fake Live set."""

import json

import pytest

import lom
from fake_live import FakeApp, FakeSong, make_track, note_spec
from ableton_helper import server
from ableton_helper import templates as tpl
from ableton_helper.matching import match_roles
from ableton_helper.plan import PlanError, compile_plan, session_plan, skeleton_plan

BAR = 4.0


class FakeConnection:
    """Talks to lom.Executor directly instead of over a socket."""

    def __init__(self, song):
        self.song = song
        self.executor = lom.Executor(song, FakeApp(), note_spec)
        self.requests = 0

    def ping(self):
        return {"name": "AbletonHelper", "version": "test"}

    def snapshot(self, include_notes=False, include_devices=True):
        return lom.snapshot(self.song, include_notes, include_devices)

    def run_ops(self, ops, stop_on_error=False):
        self.requests += 1
        result = self.executor.run(ops, stop_on_error)
        result["main_thread_ms"] = 0
        return result

    def send(self, kind, params=None):
        raise NotImplementedError(kind)


@pytest.fixture
def song():
    return FakeSong()


@pytest.fixture
def conn(song, monkeypatch):
    c = FakeConnection(song)
    monkeypatch.setattr(server, "_live", c)
    return c


def loop_set(song):
    """A typical producer's Session View: 8 loops, sloppy names."""
    kick = [(36, i, 0.25, 110) for i in range(4)]
    make_track(song, "Kick", [(0, "kick 909", 4.0, kick)])
    make_track(song, "Hats", [(0, "hh loop", 4.0, [(42, i + 0.5, 0.25) for i in range(4)])])
    make_track(song, "Open Hat", [(0, "oh", 4.0, [(46, 0.5, 0.5)])])
    make_track(song, "Clap", [(0, "clap", 4.0, [(39, 1, 0.25), (39, 3, 0.25)])])
    make_track(song, "Shaker loop", [(0, "Shaker 126", 8.0, None)], midi=False)
    make_track(song, "Bass – rolling sub", [(0, "bassline A", 12.0, [(36, 0, 1), (36, 4, 1), (38, 8, 1)])])
    make_track(song, "Chords", [(0, "stabs Am", 16.0, [(57, 0, 2), (60, 0, 2), (64, 0, 2)])])
    make_track(song, "Vocal", [(0, "hook main", 16.0, None), (1, "hook alt", 16.0, None)], midi=False)
    make_track(song, "Audio 9", [(0, "Audio 9", 4.0, None)], midi=False)


# ── templates ────────────────────────────────────────────────────────────────

def test_every_template_compiles():
    for genre in tpl.available_genres():
        t = tpl.load_template(genre)
        for variant in t.variants:
            arr = tpl.compile_arrangement(t, variant)
            assert arr.total_bars % 8 == 0, (genre, variant)
            assert arr.spans and arr.sections


def test_house_extended_shape():
    arr = tpl.compile_arrangement(tpl.load_template("house"), "extended")
    assert arr.total_bars == 224
    starts = {s.name: s.start for s in arr.sections}
    assert starts["Breakdown"] == 65 and starts["Drop 1"] == 97 and starts["Outro B"] == 209

    def playing(role, bar):
        return any(s.role == role and s.start <= bar < s.end for s in arr.spans)

    assert playing("kick", 63) and not playing("kick", 64)  # kick gap before the breakdown
    assert playing("hats", 64)
    assert not any(s.start <= 96 < s.end for s in arr.spans)  # a bar of silence before the drop
    assert playing("extra_layer", 170) and not playing("extra_layer", 100)
    fills = [t.start for t in arr.transitions if t.type == "fill"]
    assert 16 in fills and 80 not in fills  # no drum fill in the breakdown
    assert 224 not in fills  # nor at the very end


def test_aliases_and_errors():
    assert tpl.load_template("DnB").genre == "drum_and_bass"
    with pytest.raises(tpl.TemplateError):
        tpl.load_template("polka")
    with pytest.raises(tpl.TemplateError):
        tpl.compile_arrangement(tpl.load_template("techno"), "radio")


def test_describe_mentions_sections():
    text = tpl.describe(tpl.compile_arrangement(tpl.load_template("house"), "radio"))
    assert "Drop 1" in text and "| Bars |" in text


# ── matching ─────────────────────────────────────────────────────────────────

def test_role_matching(song):
    loop_set(song)
    snap = lom.snapshot(song)
    t = tpl.load_template("house")
    arr = tpl.compile_arrangement(t, "extended")
    m = match_roles(t, arr.roles_used(), snap)
    track_of = {k: [s.track_name for s in v] for k, v in m.sources.items()}
    assert track_of["kick"] == ["Kick"]
    assert track_of["hats"] == ["Hats"]
    assert track_of["open_hats"] == ["Open Hat"]
    assert track_of["perc"] == ["Shaker loop"]
    assert track_of["bass"] == ["Bass – rolling sub"]
    assert track_of["chords"] == track_of["chords_filtered"] == ["Chords"]
    assert track_of["hook"] == ["Vocal"]
    assert m.sources["hook_variation"][0].slot == 1  # second clip for the variation
    assert "pad" in m.missing and "snare_roll" in m.missing
    assert [g.name for g in m.unknown_tracks] == ["Audio 9"]


def test_role_specific_narrowing(song):
    make_track(song, "Sub", [(0, "sub", 8.0, None)])
    make_track(song, "Reese", [(0, "reese bass", 8.0, None)])
    t = tpl.load_template("drum_and_bass")
    m = match_roles(t, ["sub", "reese"], lom.snapshot(song))
    assert [s.track_name for s in m.sources["sub"]] == ["Sub"]
    assert [s.track_name for s in m.sources["reese"]] == ["Reese"]


def test_overrides_win(song):
    loop_set(song)
    t = tpl.load_template("house")
    m = match_roles(t, ["pad"], lom.snapshot(song), {"pad": [{"track": 8, "slot": 0}]})
    assert m.sources["pad"][0].track_name == "Audio 9"


# ── plan compilation ─────────────────────────────────────────────────────────

def test_tiling_and_midi_tail(song):
    loop_set(song)
    snap = lom.snapshot(song)
    plan = {"tracks": [{"ref": "bass", "existing": 5}],
            "clips": [{"track": "bass", "bar": 1, "bars": 8, "source": {"slot": 0}}]}
    ops = compile_plan(plan, snap).ops
    places = [o for o in ops if o["op"] == "place_session_clip"]
    tails = [o for o in ops if o["op"] == "create_midi_clip"]
    assert [o["time"] for o in places] == [0.0, 12.0]  # a 3-bar loop fits twice in 8 bars
    assert tails[0]["start"] == 24.0 and tails[0]["length"] == 8.0


def test_audio_tail_warns(song):
    loop_set(song)
    plan = {"tracks": [{"ref": "shk", "existing": 4}],
            "clips": [{"track": "shk", "bar": 1, "bars": 3, "source": {"slot": 0}}]}
    compiled = compile_plan(plan, lom.snapshot(song))
    assert compiled.warnings and "left empty" in compiled.warnings[0]


def test_touching_ranges_merge(song):
    loop_set(song)
    plan = {"tracks": [{"ref": "bass", "existing": 5}],
            "clips": [{"track": "bass", "bar": 1, "bars": 4, "source": {"slot": 0}},
                      {"track": "bass", "bar": 5, "bars": 5, "source": {"slot": 0}}]}
    ops = compile_plan(plan, lom.snapshot(song)).ops
    assert len([o for o in ops if o["op"] == "place_session_clip"]) == 3  # 9 bars = 3 × 3-bar loops


def test_plan_errors(song):
    loop_set(song)
    snap = lom.snapshot(song)
    with pytest.raises(PlanError):
        compile_plan({"tracks": [{"ref": "x", "existing": 99}]}, snap)
    with pytest.raises(PlanError):
        compile_plan({"tracks": [{"ref": "k", "existing": 0}],
                      "clips": [{"track": "k", "bar": 1, "bars": 4, "source": {"slot": 3}}]}, snap)
    with pytest.raises(PlanError):  # placeholders need a MIDI track
        compile_plan({"tracks": [{"ref": "v", "existing": 7}],
                      "clips": [{"track": "v", "bar": 1, "bars": 4}]}, snap)


# ── executor ─────────────────────────────────────────────────────────────────

def test_skeleton_builds_in_one_batch(song, conn):
    report = json.loads(server.build_skeleton("house"))
    assert conn.requests == 1
    assert report["ok"] == report["ops"] and "errors" not in report
    assert song.tempo == 124
    names = [t.name for t in song.tracks]
    assert names[:3] == ["Kick", "Hats", "Perc"]
    assert {"Risers & Sweeps", "Impacts", "Fills"} <= set(names)
    assert [c.name for c in song.cue_points][:3] == ["Intro A", "Intro B", "Groove"]
    assert song.cue_points[6].time == 96 * BAR  # Drop 1 at bar 97
    kick = song.tracks[0]
    assert all(c.name == "Kick" for c in kick.arrangement_clips)
    assert not any(c.start_time <= 63 * BAR < c.end_time for c in kick.arrangement_clips)
    assert song.undo_steps == 1


def test_arrange_session_end_to_end(song, conn):
    loop_set(song)
    report = json.loads(server.arrange_session("house", "extended"))
    assert "errors" not in report, report
    bass = song.tracks[5]
    # Bass plays from Groove (bar 33) and never spills into the breakdown (bar 65).
    assert min(c.start_time for c in bass.arrangement_clips) == 32 * BAR
    assert not any(c.start_time < 64 * BAR < c.end_time for c in bass.arrangement_clips)
    assert not any(64 * BAR <= c.start_time < 96 * BAR for c in bass.arrangement_clips)
    # 3-bar loop: the tail clip carries the right notes.
    tail = [c for c in bass.arrangement_clips if c.length < 12.0][0]
    assert tail.notes and all(n.start_time < tail.length for n in tail.notes)
    # Missing parts got "(to do)" placeholder tracks.
    assert "Pad (to do)" in [t.name for t in song.tracks]
    assert report["placeholders_for"] and "Audio 9" in report["not_used"]
    # The variation used the second vocal clip in Breakdown 2 (bar 145).
    vocal = song.tracks[7]
    assert any(c.name == "hook alt" and c.start_time == 144 * BAR for c in vocal.arrangement_clips)
    assert song.back_to_arranger is False


def test_clear_only_touches_written_ranges(song, conn):
    loop_set(song)
    kick = song.tracks[0]
    kick.arrangement_clips.append(lom_clip := __import__("fake_live").FakeClip("old", 4.0, True, 4000.0))
    plan = {"clear": True, "tracks": [{"ref": "k", "existing": 0}],
            "clips": [{"track": "k", "bar": 1, "bars": 4, "source": {"slot": 0}}]}
    json.loads(server.build_arrangement(plan))
    assert lom_clip in kick.arrangement_clips  # far away, untouched


def test_notes_source_loops(song, conn):
    song.tracks.append(__import__("fake_live").FakeTrack("Drums"))
    plan = {"tracks": [{"ref": "d", "existing": 0}],
            "clips": [{"track": "d", "bar": 1, "bars": 4, "name": "four on floor",
                       "source": {"notes": [{"pitch": 36, "start_time": b, "duration": 0.25}
                                            for b in range(4)], "loop_bars": 1}}]}
    report = json.loads(server.build_arrangement(plan))
    assert "errors" not in report
    clip = song.tracks[0].arrangement_clips[0]
    assert len(clip.notes) == 16 and clip.name == "four on floor"


def test_delete_track_shifts_refs(song):
    ex = lom.Executor(song, FakeApp(), note_spec)
    ex.run([{"op": "create_track", "ref": "a", "name": "A"},
            {"op": "create_track", "ref": "b", "name": "B"},
            {"op": "create_track", "ref": "c", "name": "C"},
            {"op": "delete_track", "track": "a"}])
    assert ex.refs == {"b": 0, "c": 1}
    ex.run([{"op": "set_track", "track": "c", "name": "C2"}])
    assert song.tracks[1].name == "C2"


def test_errors_are_collected_not_fatal(song):
    ex = lom.Executor(song, FakeApp(), note_spec)
    result = ex.run([{"op": "create_track", "ref": "a"}, {"op": "bogus"},
                     {"op": "create_locator", "time": 16.0, "name": "X"}])
    assert result["ok"] == 2 and result["errors"][0]["op"] == "bogus"
    assert song.cue_points[0].name == "X"


def test_add_transition_reuses_lane(song, conn):
    json.loads(server.add_transition("riser", 81, 16))
    json.loads(server.add_transition("sweep", 120, 4))
    lanes = [t for t in song.tracks if t.name == "Risers & Sweeps"]
    assert len(lanes) == 1 and len(lanes[0].arrangement_clips) == 2


def test_dry_run_touches_nothing(song, conn):
    report = json.loads(server.build_skeleton("techno", dry_run=True))
    assert report["dry_run"] and report["op_list"] and not song.tracks
