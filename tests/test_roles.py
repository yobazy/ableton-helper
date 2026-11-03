"""Role tagging from sound: scoring rules, the band split, and the tool flow on a fake service."""

import pytest

import tones
from fake_audio_service import FakeAudioService
from ableton_helper import hearing, roles

QUIET = {"bass": 0.0, "other": 0.0, "vocals": 0.0}


def drums_only(low, mid, high, hits_per_second, bpm=120.0):
    return roles.Features(stem_share={"drums": 1.0, **QUIET}, bpm=bpm, key_strength=0.3,
                          drum_bands=roles.Bands(low, mid, high, hits_per_second))


def notes(pitches_at_once, length_s, starts, base=60):
    return [{"start": s, "end": s + length_s, "pitch": base + 3 * k, "velocity": 0.8}
            for s in starts for k in range(pitches_at_once)]


# ── scoring ──────────────────────────────────────────────────────────────────

def test_kick_hats_clap_perc_and_kit():
    assert roles.score(drums_only(0.8, 0.2, 0.0001, 2.0)).kind == "kick"      # 1 hit a beat
    assert roles.score(drums_only(0.05, 0.3, 0.65, 4.0)).kind == "hats"
    assert roles.score(drums_only(0.05, 0.85, 0.1, 1.0)).kind == "clap"       # sparse mids
    assert roles.score(drums_only(0.05, 0.85, 0.1, 6.0)).kind == "perc"       # busy mids
    assert roles.score(drums_only(0.6, 0.3, 0.05, 4.0)).kind == "drums"       # lows + tops, busy


def test_tonal_parts_from_notes():
    other = {"drums": 0.0, "bass": 0.0, "other": 1.0, "vocals": 0.0}
    pad = roles.Features(stem_share=other, bpm=120, key_strength=0.8, duration=8,
                         notes=notes(4, 3.9, [0, 4]))
    chords = roles.Features(stem_share=other, bpm=120, key_strength=0.8, duration=8,
                            notes=notes(3, 0.2, [0, 1, 2, 3, 4, 5, 6, 7]))
    lead = roles.Features(stem_share=other, bpm=120, key_strength=0.8, duration=8,
                          notes=notes(1, 0.2, [i * 0.25 for i in range(32)], base=70))
    riser = roles.Features(stem_share=other, bpm=120, key_strength=0.2, duration=8, movement_lu=20)
    assert roles.score(pad).kind == "pad"
    assert roles.score(chords).kind == "chords"
    assert roles.score(lead).kind == "lead"
    verdict = roles.score(riser)
    assert verdict.kind == "fx" and "riser" in verdict.reason


def test_bass_and_vocal_from_stems():
    bass = roles.Features(stem_share={"drums": 0.05, "bass": 0.9, "other": 0.05, "vocals": 0.0},
                          bpm=124, notes=notes(1, 0.2, [0, 0.5, 1, 1.5], base=36))
    vocal = roles.Features(stem_share={"drums": 0.0, "bass": 0.05, "other": 0.15, "vocals": 0.8})
    v = roles.score(bass)
    assert v.kind == "bass" and v.level == "high" and "bass stem" in v.reason and "low" in v.reason
    assert roles.score(vocal).kind == "vocal"


def test_without_stems_key_and_pitch_decide():
    tonal_low = roles.Features(key_strength=0.9, bpm=120, notes=notes(1, 0.3, [0, 0.5, 1], base=33))
    noisy = roles.Features(key_strength=0.2, bpm=120,
                           drum_bands=None)
    assert roles.score(tonal_low).kind == "bass"
    assert roles.score(noisy).kind == "drums"


def test_name_is_only_a_nudge():
    ambiguous = drums_only(0.35, 0.45, 0.2, 2.0)
    named = drums_only(0.35, 0.45, 0.2, 2.0)
    named.name_kind = "perc"
    assert roles.score(named).scores["perc"] > roles.score(ambiguous).scores["perc"]
    # A clear sound beats a misleading name.
    wrong = drums_only(0.05, 0.3, 0.65, 4.0)
    wrong.name_kind = "bass"
    assert roles.score(wrong).kind == "hats"


def test_confidence_levels():
    assert roles.Verdict("kick", 0.7, "", None, {}).level == "high"
    assert roles.Verdict("kick", 0.4, "", None, {}).level == "medium"
    assert roles.Verdict("kick", 0.2, "", None, {}).level == "low"


def test_name_kind():
    assert roles.name_kind("Audio 7", "Audio 7") is None
    assert roles.name_kind("Bass – rolling sub") == "bass"
    assert roles.name_kind("OpenHat") == "hats"


def test_clap_health_and_probs():
    assert not roles.clap_is_healthy([[0.0, 1.0], [0.0, 1.0]])
    assert roles.clap_is_healthy([[1.0, 0.0], [0.0, 1.0]])
    probs = roles.clap_probs([1.0, 0.0], {"kick": [[0.9, 0.1]], "pad": [[0.1, 0.9]]})
    assert probs["kick"] > 0.99


# ── the band split on generated sounds ───────────────────────────────────────

def test_bands_on_generated_loops(tmp_path):
    k = roles.bands(*roles.read_wav_mono(str(tones.kick(tmp_path / "k.wav"))))
    h = roles.bands(*roles.read_wav_mono(str(tones.hats(tmp_path / "h.wav"))))
    p = roles.bands(*roles.read_wav_mono(str(tones.pad(tmp_path / "p.wav"))))
    assert k.low > 0.6 and h.high > 0.6 and p.mid > 0.9
    assert 1.5 < k.hits_per_second < 2.3       # 124 BPM, one a beat: about 2 a second
    assert p.hits_per_second < 1.5             # a pad has (almost) no hits


def test_read_wav_rejects_junk(tmp_path):
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"not a wav")
    with pytest.raises(ValueError, match="Couldn't read"):
        roles.read_wav_mono(str(junk))


# ── the tool flow on a fake service ──────────────────────────────────────────

@pytest.fixture
def svc(tmp_path):
    return FakeAudioService(tmp_path / "cache")


def _stems(tmp_path, **parts):
    silence = tones._write(tmp_path / "silence.wav", [0.0] * 2000)
    out = {}
    for name in ("drums", "bass", "other", "vocals"):
        p = parts.get(name, silence)
        out[name] = p.read_bytes()
    return out


def _loop(tmp_path, name):
    p = tmp_path / f"{name}.wav"
    p.write_bytes(b"RIFF" + name.encode() * 40)
    return p


def test_tag_roles_end_to_end(svc, tmp_path):
    kick_file, hats_file = _loop(tmp_path, "Audio 1"), _loop(tmp_path, "Audio 2")
    svc.stem_audio["Audio 1"] = _stems(tmp_path, drums=tones.kick(tmp_path / "k.wav"))
    svc.stem_audio["Audio 2"] = _stems(tmp_path, drums=tones.hats(tmp_path / "h.wav"))
    for stem in ("Audio 1", "Audio 2"):
        svc.answers[stem] = {"duration": 3.9, "bpm": {"bpm": 124.0, "confidence": 0.95},
                             "key": {"strength": 0.3}, "loudness": {"integrated_lufs": -10, "energy_curve": [-10]}}
    sources = [hearing.AudioSource(str(kick_file), 0, "Audio 1", "Audio 1", slot=0),
               hearing.AudioSource(str(hats_file), 1, "Audio 2", "Audio 2", slot=0)]
    out = hearing.tag_roles(svc, sources)
    assert [it["role"] for it in out["loops"]] == ["kick", "hats"]
    assert all(it["reason"] for it in out["loops"])
    assert "CLAP" not in out["listened_with"] and "broken" in out["clap"]  # fake vectors are all alike

    snapshot = {"tracks": [{"index": 0, "name": "Audio 1"}, {"index": 1, "name": "Audio 2"},
                           {"index": 2, "name": "Bass"}]}
    ops, done = hearing.rename_ops(snapshot, hearing.track_roles(out["loops"]), min_confidence=0.2)
    assert ops[0] == {"op": "set_track", "track": 0, "name": "Kick (Audio 1)"}
    assert len(done) == 2


def test_tag_roles_uses_clap_when_healthy(svc, tmp_path):
    f = _loop(tmp_path, "Audio 3")
    svc.stem_audio["Audio 3"] = _stems(tmp_path, other=tones.pad(tmp_path / "p.wav"))
    a, b = roles.CLAP_HEALTH_PAIR
    svc.text_vectors.update({t: [1.0, 0.0, 0.0] for ts in roles.CLAP_PROMPTS.values() for t in ts})
    svc.text_vectors.update({a: [1.0, 0.0, 0.0], b: [0.0, 1.0, 0.0],
                             roles.CLAP_PROMPTS["pad"][0]: [0.0, 0.0, 1.0]})
    svc.answers["Audio 3"] = {"bpm": {"bpm": 120.0}, "key": {"strength": 0.9},
                              "loudness": {"integrated_lufs": -14, "energy_curve": [-14]},
                              "embedding": {"vector": [0.0, 0.0, 1.0]}}
    svc.transcriptions["Audio 3"] = notes(4, 3.9, [0, 4])
    out = hearing.tag_roles(svc, [hearing.AudioSource(str(f))])
    [item] = out["loops"]
    assert "CLAP" in out["listened_with"] and item["role"] == "pad" and "CLAP" in item["reason"]


def test_one_bad_loop_doesnt_stop_the_rest(svc, tmp_path):
    good, bad = _loop(tmp_path, "Audio 4"), _loop(tmp_path, "Audio 5")
    svc.stem_audio["Audio 4"] = _stems(tmp_path, drums=tones.kick(tmp_path / "k.wav"))
    for stem in ("Audio 4", "Audio 5"):
        svc.answers[stem] = {"bpm": {"bpm": 124.0}, "key": {"strength": 0.3}}
    out = hearing.tag_roles(svc, [hearing.AudioSource(str(bad)), hearing.AudioSource(str(good))])
    assert "Couldn't read" in out["loops"][0]["error"]  # the fake's stems aren't WAVs for this one
    assert out["loops"][1]["role"] == "kick"


def test_rename_leaves_named_and_unsure_tracks(svc):
    snapshot = {"tracks": [{"index": 0, "name": "Bass loop"}, {"index": 1, "name": "Audio 9"}]}
    verdicts = {0: roles.Verdict("kick", 0.9, "", None, {}), 1: roles.Verdict("pad", 0.2, "", None, {})}
    assert hearing.rename_ops(snapshot, verdicts) == ([], [])
