"""Reference track → template: bars from downbeats, phrase snapping, naming, hearing parts."""

import json

import pytest

import reference_audio as ref
from fake_audio_service import FakeAudioService
from fake_live import FakeSong, make_track
from ableton_helper import reference, server
from ableton_helper import templates as tpl
from ableton_helper.hearing import AudioSource, HearingError
from ableton_helper.reference import BarGrid, RefSection, build_sections, name_sections, snap

FEATURES = ("bpm", "key", "tags", "embedding", "loudness", "sections")


@pytest.fixture
def user_templates(tmp_path, monkeypatch):
    monkeypatch.setenv("ABLETON_HELPER_TEMPLATES", str(tmp_path / "templates"))
    tpl.load_template.cache_clear()
    yield tmp_path / "templates"
    tpl.load_template.cache_clear()


@pytest.fixture
def track_file(tmp_path):
    p = tmp_path / "Club Track.wav"
    p.write_bytes(b"RIFF" + b"reference" * 40)
    return p


@pytest.fixture
def svc(tmp_path, track_file, monkeypatch):
    fake = FakeAudioService(tmp_path / "cache", features=FEATURES)
    folder = tmp_path / "stems"
    folder.mkdir()
    fake.stem_audio["Club Track"] = ref.stems(folder)
    fake.sections_answer = ref.sections_answer()
    fake.answers["Club Track"] = {"duration": ref.total_seconds(),
                                  "bpm": {"bpm": 120.0, "bpm_rounded": 120, "confidence": 0.97}}
    monkeypatch.setattr(server, "_audio", fake)
    return fake


# ── bars and phrases ─────────────────────────────────────────────────────────

def test_bars_follow_the_downbeats_even_when_the_tempo_drifts():
    grid = BarGrid([1.0, 3.0, 5.0, 7.2, 9.4, 11.4])
    assert grid.bar(1.0) == 0 and grid.bar(5.0) == 2
    assert grid.bar(6.1) == pytest.approx(2.5)        # halfway through a 2.2 s bar
    assert grid.bar(13.4) == pytest.approx(6.0)        # past the last downbeat: the median bar (2 s)
    assert grid.bar(0.0) == pytest.approx(-0.5)        # a pickup before the first downbeat
    for b in (-0.5, 0, 2.5, 3.7, 5):
        assert grid.bar(grid.time(b)) == pytest.approx(b)


def test_needs_downbeats():
    with pytest.raises(HearingError, match="downbeats"):
        BarGrid([2.0])


def test_snap_to_eight_bar_phrases():
    assert snap([0, 15.6, 32.3, 47.8, 64.2], 8) == ([0, 16, 32, 48, 64], 0)


def test_snap_finds_a_pickup_bar():
    # The first downbeat is a one-bar pickup: every phrase starts a bar later.
    snapped, offset = snap([0, 1.1, 17.0, 32.8, 49.2], 8)
    assert offset == 1 and snapped == [1, 1, 17, 33, 49]


def test_short_segments_merge_into_their_neighbours():
    snapped, _ = snap([0, 16, 18.5, 32], 8)
    assert snapped == [0, 16, 16, 32]
    grid = BarGrid([i * 2.0 for i in range(40)])
    segs = [{"start": 0, "end": 32, "label": "intro", "loudness_lufs": -14},
            {"start": 32, "end": 37, "label": "break", "loudness_lufs": -20},
            {"start": 37, "end": 64, "label": "chorus", "loudness_lufs": -8}]
    sections, _, warnings = build_sections(segs, grid, 8)
    assert [(s.start, s.end, s.label) for s in sections] == [(0, 16, "intro"), (16, 32, "chorus")]
    # The 2.5-bar break is folded into the chorus's loudness (a power mean).
    assert -9.0 < sections[1].loudness_db < -8.0
    assert sections[1].energy == 10 and sections[0].energy == 7
    assert len(warnings) == 1 and "bar 19.5" in warnings[0]  # the break that was swallowed


def test_silence_at_the_ends_is_left_out_and_odd_boundaries_are_flagged():
    grid = BarGrid([1.0 + i * 2.0 for i in range(40)])
    segs = [{"start": 0, "end": 1.0, "label": "start", "loudness_lufs": -70},
            {"start": 1.0, "end": 25.0, "label": "intro", "loudness_lufs": -14},   # 12 bars
            {"start": 25.0, "end": 65.0, "label": "chorus", "loudness_lufs": -8},
            {"start": 65.0, "end": 70.0, "label": "end", "loudness_lufs": -70}]
    sections, _, warnings = build_sections(segs, grid, 8)
    assert sections[0].start == 0 and sections[0].label == "intro"
    assert sum(s.bars for s in sections) == 32
    assert any("off the 8-bar grid" in w for w in warnings)


def _sections(spec):
    out, bar = [], 0
    for label, bars, energy, parts in spec:
        out.append(RefSection(bar, bar + bars, label, energy=energy, parts=list(parts)))
        bar += bars
    return out


def test_dance_names_come_from_energy_and_the_kick():
    full = ["kick", "hats", "bass", "music"]
    secs = _sections([("intro", 16, 6, ["kick", "hats"]), ("inst", 16, 8, full),
                      ("break", 16, 5, ["music", "vocal"]), ("inst", 8, 6, ["music", "hats"]),
                      ("chorus", 32, 10, full), ("break", 16, 5, ["music"]),
                      ("chorus", 32, 10, full + ["vocal"]), ("outro", 16, 6, ["kick", "hats"])])
    name_sections(secs, heard=True)
    assert [s.name for s in secs] == ["Intro", "Groove", "Breakdown 1", "Build", "Drop 1",
                                      "Breakdown 2", "Drop 2", "Outro"]


def test_names_without_stems_use_energy_only():
    secs = _sections([("intro", 16, 4, []), ("intro", 16, 5, []), ("chorus", 32, 10, []),
                      ("break", 16, 4, []), ("chorus", 32, 9, []), ("outro", 16, 4, [])])
    name_sections(secs, heard=False)
    assert [s.name for s in secs] == ["Intro A", "Intro B", "Drop 1", "Breakdown", "Drop 2", "Outro"]


def test_transitions_follow_the_energy():
    secs = _sections([("intro", 16, 5, []), ("break", 16, 4, []), ("chorus", 32, 10, []),
                      ("outro", 16, 5, [])])
    for s, n in zip(secs, ["Intro", "Breakdown", "Drop", "Outro"]):
        s.name = n
    out = reference.transitions(secs)
    assert {"type": "riser", "section": "Breakdown", "at": "end", "bars": 8} in out
    assert {"type": "downlifter", "section": "Outro", "at": "start", "bars": 2} in out
    assert {"type": "crash", "sections": ["Drop"]} in out


# ── end to end on the fake service ───────────────────────────────────────────

def test_a_reference_becomes_a_template(svc, track_file, user_templates):
    out = json.loads(server.analyze_reference(path=str(track_file)))
    assert out["template"] == "ref_club_track" and out["variant"] == "reference"
    assert out["bpm"] == 120 and out["bars"] == 48 and out["parts_from"] == "stems"
    rows = [(r["name"], r["bar"], r["bars"]) for r in out["sections"]]
    assert rows == [("Intro", 1, 8), ("Groove", 9, 8), ("Breakdown", 17, 8), ("Drop", 25, 16), ("Outro", 41, 8)]
    parts = {r["name"]: r["parts"] for r in out["sections"]}
    assert parts["Intro"] == ["Kick", "Hats and tops"]
    assert parts["Groove"] == ["Kick", "Hats and tops", "Bass", "Chords and synths"]
    assert parts["Breakdown"] == ["Chords and synths", "Vocal"]
    assert parts["Drop"] == ["Kick", "Hats and tops", "Bass", "Chords and synths", "Vocal"]
    assert parts["Outro"] == ["Kick", "Hats and tops"]
    assert [r["energy"] for r in out["sections"]] == [7, 8, 6, 10, 8]
    assert (user_templates / "ref_club_track.yaml").is_file()

    # It's an ordinary template now (SPEC 4.2: apply_template accepts it).
    t = tpl.load_template("ref_club_track")
    assert t.reference["file"] == "Club Track.wav" and t.reference["parts_from"] == "stems"
    arr = tpl.compile_arrangement(t, "reference")
    assert arr.total_bars == 48
    assert any(tr.type == "riser" and tr.section == "Breakdown" for tr in arr.transitions)
    assert "Drop" in server.preview_template("ref_club_track", "reference")
    listed = {e["genre"]: e for e in json.loads(server.list_templates())}
    assert listed["ref_club_track"]["from_reference"] == "Club Track.wav"


def test_arrange_session_follows_a_reference(svc, track_file, user_templates, monkeypatch):
    """SPEC 4.3: one request arranges the producer's loops like the reference."""
    from test_pipeline import FakeConnection
    song = FakeSong()
    make_track(song, "Kick", [(0, "kick", 4.0, [(36, i, 0.25, 110) for i in range(4)])])
    make_track(song, "Bass", [(0, "bass", 4.0, [(36, 0, 1, 100)])])
    make_track(song, "Chords", [(0, "chords", 4.0, [(60, 0, 4, 90)])])
    conn = FakeConnection(song)
    monkeypatch.setattr(server, "_live", conn)
    server.analyze_reference(path=str(track_file))
    out = json.loads(server.arrange_session("ref_club_track", "reference"))
    assert out["ok"] == out["ops"], out
    beats = {t.name: sorted((c.start_time, c.end_time) for c in t.arrangement_clips) for t in song.tracks}
    bass = beats["Bass"]
    assert bass[0][0] == 8 * 4 and bass[-1][1] == 40 * 4      # bass from the Groove to the end of the Drop
    assert not any(16 * 4 <= a < 24 * 4 for a, _ in bass)       # and out for the Breakdown
    assert [c.name for c in song.cue_points] == ["Intro", "Groove", "Breakdown", "Drop", "Outro"]
    assert any(t.name.startswith("Hats and tops (to do)") for t in song.tracks)


def test_without_stems_parts_are_the_usual_ones(svc, track_file, user_templates):
    out = json.loads(server.analyze_reference(path=str(track_file), use_stems=False, save=False))
    assert out["parts_from"] == "defaults" and "saved_to" not in out
    assert not (user_templates / "ref_club_track.yaml").exists()
    parts = {r["name"]: r["parts"] for r in out["sections"]}
    assert parts["Breakdown"] == ["Chords and synths", "Vocal"]


def test_name_and_phrase_options(svc, track_file, user_templates):
    out = json.loads(server.analyze_reference(path=str(track_file), name="My Ref!", phrase_bars=16))
    assert out["template"] == "ref_my_ref"
    assert all(r["bars"] % 16 == 0 for r in out["sections"])
    assert "phrase_bars" in server.analyze_reference(path=str(track_file), phrase_bars=3)


def test_a_built_in_name_is_refused(user_templates):
    with pytest.raises(HearingError, match="built-in"):
        reference.save_template("house", {})


def test_says_when_the_service_has_no_sections(tmp_path, track_file, monkeypatch, user_templates):
    fake = FakeAudioService(tmp_path / "cache")  # no "sections" feature
    monkeypatch.setattr(server, "_audio", fake)
    text = server.analyze_reference(path=str(track_file))
    assert text.startswith("Error:") and "sections" in text
    fake.down = True
    assert "audio analysis service" in server.analyze_reference(path=str(track_file))


def test_broken_stems_fall_back_to_the_usual_parts(svc, track_file, user_templates):
    svc.stem_audio["Club Track"] = {}  # the fake serves text, not WAV
    out = json.loads(server.analyze_reference(path=str(track_file), save=False))
    assert out["parts_from"] == "defaults"
    assert any("Stems failed" in w for w in out["warnings"])


def test_template_key_for():
    assert reference.template_key_for("Daft Punk - One More Time (Extended)") == \
        "ref_daft_punk_one_more_time_extended"
    assert reference.template_key_for("!!!") == "ref_track"


def test_hear_parts_reads_real_wavs(tmp_path):
    folder = tmp_path / "s"
    folder.mkdir()
    ref.stems(folder)
    grid = BarGrid(ref.sections_answer()["downbeats"])
    sections, _, _ = build_sections(ref.sections_answer()["segments"], grid, 8)
    reference.hear_parts(sections, {n: str(folder / f"{n}.wav") for n in ("drums", "bass", "other", "vocals")})
    expected = [sorted(p) for _l, _b, p in ref.LAYOUT]
    assert [sorted(s.parts) for s in sections] == expected
