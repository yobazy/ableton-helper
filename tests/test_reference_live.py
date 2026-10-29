"""Opt-in: reference analysis against the real audio analysis service, on a generated track.

    ABLETON_HELPER_LIVE_AUDIO=1 uv run pytest tests/test_reference_live.py

The first test needs only stems (the deployed service has them). The second needs
/v1/sections, which is on audio-analysis's `sections-allin1` branch and not deployed yet:
it skips itself until /health lists "sections". Point AUDIO_ANALYSIS_URL at the dev copy
(`http://127.0.0.1:8771` on pc, say) to try it before then.
"""

import os

import pytest

import reference_audio as ref
from ableton_helper import reference
from ableton_helper import templates as tpl
from ableton_helper.audio_service import AudioService
from ableton_helper.hearing import AudioSource

pytestmark = pytest.mark.skipif(not os.environ.get("ABLETON_HELPER_LIVE_AUDIO"),
                                reason="set ABLETON_HELPER_LIVE_AUDIO=1 to call the real service")


@pytest.fixture
def svc(tmp_path):
    return AudioService(os.environ.get("AUDIO_ANALYSIS_URL", "https://pc.taila3582d.ts.net:8770"),
                        tmp_path / "cache")


@pytest.fixture
def track(tmp_path):
    return ref.mixdown(tmp_path / "generated reference.wav")


def test_parts_per_section_from_real_stems(svc, track):
    """Demucs on a generated 48-bar track, then the section hearing on its stems."""
    stems = svc.stems(track, model="htdemucs", fmt="wav")["stems"]
    answer = ref.sections_answer()
    sections, _, _ = reference.build_sections(answer["segments"], reference.BarGrid(answer["downbeats"]), 8)
    reference.hear_parts(sections, stems)
    heard = [sorted(p for p in s.parts if p != "vocal") for s in sections]
    # The "vocal" is a plain sine, which Demucs rightly files under other, so it's left out.
    expected = [sorted(p - {"vocal"}) for _l, _b, p in ref.LAYOUT]
    assert heard == expected


def test_a_generated_track_becomes_a_template(svc, track, tmp_path, monkeypatch):
    if not svc.has_feature("sections"):
        pytest.skip("this service has no /v1/sections yet")
    monkeypatch.setenv("ABLETON_HELPER_TEMPLATES", str(tmp_path / "templates"))
    out = reference.analyze_reference(svc, AudioSource(str(track)))
    assert 40 <= out["bars"] <= 56, out
    assert len(out["sections"]) >= 2, out
    assert 115 <= out["bpm"] <= 125, out
    assert tpl.compile_arrangement(tpl.load_template(out["template"]), "reference").total_bars == out["bars"]
