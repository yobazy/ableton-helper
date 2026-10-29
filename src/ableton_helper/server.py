"""MCP server: arranging tools for Claude, backed by the AbletonHelper Remote Script."""

from __future__ import annotations

import json
import time
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from . import hearing
from . import templates as tpl
from .audio_service import AudioService, AudioServiceError
from .connection import LiveConnection, LiveError
from .matching import match_roles as _match_roles
from .plan import PlanError, beats_per_bar, compile_plan, session_plan, skeleton_plan

INSTRUCTIONS = """\
Ableton Helper arranges tracks in Ableton Live's Arrangement View.

Workflow:
1. Call get_session first to see tracks, Session clips and what's already arranged.
2. For a new idea with no loops yet, use build_skeleton: named, coloured placeholder
   clips per part and locators per section, so the producer can fill them in.
3. When the producer has loops in Session View, call match_roles to see which track
   plays which part, confirm anything unclear with them, then arrange_session.
4. For anything custom, write a plan and send it with build_arrangement: one call
   builds the whole thing, so never place clips one by one.

Rules:
- Bars are 1-indexed like Live's ruler. 16- and 32-bar sections are the norm in
  dance music; change something every 8 bars.
- `clear=True` deletes arrangement clips in the ranges being written. Ask before using it
  on a set that already has an arrangement.
- Each build is one batch; tell the producer they can undo it in Live (Cmd+Z) if they
  don't like it.
- You can hear audio clips through the analysis service on the home PC: analyze_audio
  (BPM, key, loudness, energy), audio_spectrogram (look at it), separate_stems,
  audio_to_midi and tag_loop_roles (what a badly named loop is). MIDI clips have no
  audio file; use their notes. If the PC is off, say so and carry on with names and notes.
"""

mcp = FastMCP("ableton-helper", instructions=INSTRUCTIONS)
_live: LiveConnection | None = None


def live() -> LiveConnection:
    global _live
    if _live is None:
        _live = LiveConnection()
    return _live


_audio: AudioService | None = None


def audio() -> AudioService:
    global _audio
    if _audio is None:
        _audio = AudioService()
    return _audio


def _sources(track: int | None, slot: int | None, bar: float | None, path: str | None,
             one: bool = False, snap: dict[str, Any] | None = None) -> list[hearing.AudioSource]:
    if snap is None and not path:
        snap = live().snapshot(include_devices=False)
    sources = hearing.find_sources(None if path else snap, track, slot, bar, path)
    if one and len(sources) > 1:
        where = f"Track {track} has" if track is not None else "The set has"
        raise hearing.HearingError(
            f"{where} {len(sources)} audio files and this works on one; pick it with track + slot "
            f"(Session), track + bar (Arrangement) or path: " + "; ".join(s.label for s in sources[:8]))
    return sources


HEARING_ERRORS = (hearing.HearingError, AudioServiceError, LiveError)


def _dump(data: Any) -> str:
    return json.dumps(data, indent=1, default=str)


def _execute(plan: dict[str, Any], snapshot: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    compiled = compile_plan(plan, snapshot)
    report: dict[str, Any] = {"ops": len(compiled.ops), **compiled.stats}
    if compiled.warnings:
        report["warnings"] = compiled.warnings
    if dry_run:
        report["dry_run"] = True
        report["op_list"] = compiled.ops
        return report
    started = time.perf_counter()
    result = live().run_ops(compiled.ops)
    report["seconds"] = round(time.perf_counter() - started, 2)
    report["main_thread_ms"] = result.get("main_thread_ms")
    report["ok"] = result["ok"]
    if result["errors"]:
        report["errors"] = result["errors"][:20]
        if len(result["errors"]) > 20:
            report["errors_truncated"] = len(result["errors"]) - 20
    return report


def _compact_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    bpb = snap["signature"][0] * 4.0 / snap["signature"][1]
    tracks = []
    for t in snap["tracks"]:
        entry: dict[str, Any] = {"index": t["index"], "name": t["name"], "type": t["type"]}
        if t.get("is_group"):
            entry["group"] = True
        if t.get("devices"):
            entry["devices"] = [d["name"] for d in t["devices"]]
        if t["session_clips"]:
            entry["session_clips"] = [
                {k: v for k, v in {
                    "slot": c["slot"], "name": c["name"], "bars": round(c["length"] / bpb, 2),
                    "notes": c.get("note_count"), "file": c.get("file_path")}.items() if v is not None}
                for c in t["session_clips"]]
        if t["arrangement_clips"]:
            entry["arrangement"] = [
                {"name": c["name"], "bar": round(c["start"] / bpb + 1, 2),
                 "bars": round((c["end"] - c["start"]) / bpb, 2)} for c in t["arrangement_clips"]]
        tracks.append(entry)
    return {
        "tempo": snap["tempo"],
        "signature": f"{snap['signature'][0]}/{snap['signature'][1]}",
        "arrangement_bars": round(snap["song_length"] / bpb, 1),
        "locators": [{"bar": round(c["time"] / bpb + 1, 2), "name": c["name"]} for c in snap["locators"]],
        "tracks": tracks,
    }


def _error(e: Exception) -> str:
    return f"Error: {e}"


# ── tools ────────────────────────────────────────────────────────────────────

@mcp.tool()
def live_status() -> str:
    """Check that Ableton Live and the AbletonHelper Remote Script are connected."""
    try:
        started = time.perf_counter()
        info = live().ping()
        info["round_trip_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return _dump(info)
    except LiveError as e:
        return _error(e)


@mcp.tool()
def get_session(include_notes: bool = False) -> str:
    """Read the whole Live set in one call: tempo, tracks, devices, Session clips
    (slot, name, length in bars, note count, audio file) and the current arrangement
    (clips and locators, in bars). Set include_notes to get every MIDI note (large)."""
    try:
        snap = live().snapshot(include_notes=include_notes)
    except LiveError as e:
        return _error(e)
    if include_notes:
        return _dump(snap)
    return _dump(_compact_snapshot(snap))


@mcp.tool()
def list_templates() -> str:
    """List the genre templates and their variants (e.g. extended club mix, radio edit)."""
    out = []
    for genre in tpl.available_genres():
        t = tpl.load_template(genre)
        variants = {}
        for name in t.variants:
            arr = tpl.compile_arrangement(t, name)
            variants[name] = f"{arr.total_bars} bars, {tpl.bar_to_time(arr.total_bars + 1, arr.bpm)} " \
                             f"at {arr.bpm:g} BPM — {t.variants[name].get('description', '')}"
        out.append({"genre": genre, "name": t.name, "bpm_range": t.bpm_range,
                    "description": t.description, "variants": variants})
    return _dump(out)


@mcp.tool()
def preview_template(genre: str, variant: str = "extended", bpm: float | None = None) -> str:
    """Show a genre template as a table: sections, bar ranges, timestamps, energy,
    which parts play, gaps and transition placeholders. Doesn't need Live."""
    try:
        return tpl.describe(tpl.compile_arrangement(tpl.load_template(genre), variant, bpm))
    except tpl.TemplateError as e:
        return _error(e)


@mcp.tool()
def build_skeleton(genre: str, variant: str = "extended", bpm: float | None = None,
                   set_tempo: bool = True, include_transitions: bool = True,
                   dry_run: bool = False) -> str:
    """Build a genre's full arrangement as named, coloured placeholder clips: one new MIDI
    track per part (Kick, Bass, Chords...), a locator at every section, and placeholder
    tracks for risers, impacts and fills. The producer then fills the blocks with sounds.
    Everything happens in one batch. dry_run returns the op list without touching Live."""
    try:
        arr = tpl.compile_arrangement(tpl.load_template(genre), variant, bpm)
        snap = live().snapshot(include_devices=False)
        plan = skeleton_plan(arr, snap, set_tempo=set_tempo, include_transitions=include_transitions)
        report = _execute(plan, snap, dry_run)
        report["arrangement"] = f"{arr.template.name} {variant}: {arr.total_bars} bars at {arr.bpm:g} BPM"
        return _dump(report)
    except (tpl.TemplateError, PlanError, LiveError) as e:
        return _error(e)


@mcp.tool()
def match_roles(genre: str, variant: str = "extended",
                overrides: dict[str, list[dict[str, int]]] | None = None) -> str:
    """Work out which Session View track plays which part of a genre template (kick, bass,
    chords...) from track and clip names. Returns the mapping, template parts with no
    matching track, and tracks it couldn't place. Show this to the producer and fix it with
    overrides ({"role": [{"track": 3, "slot": 0}]}) before calling arrange_session."""
    try:
        template = tpl.load_template(genre)
        arr = tpl.compile_arrangement(template, variant)
        snap = live().snapshot(include_devices=False)
        match = _match_roles(template, arr.roles_used(), snap, overrides)
        return _dump(match.to_dict(template))
    except (tpl.TemplateError, LiveError, ValueError) as e:
        return _error(e)


@mcp.tool()
def arrange_session(genre: str, variant: str = "extended",
                    overrides: dict[str, list[dict[str, int]]] | None = None,
                    bpm: float | None = None, clear: bool = False,
                    placeholders_for_missing: bool = True, include_transitions: bool = True,
                    dry_run: bool = False) -> str:
    """Arrange the producer's Session View loops into Arrangement View following a genre
    template, in one batch. Loops are tiled across each section; parts the set doesn't have
    yet get placeholder tracks marked "(to do)". Uses the same matching as match_roles, with
    the same overrides. bpm sets the tempo only if given. clear=True first deletes existing
    arrangement clips in the ranges being written (ask the producer first)."""
    try:
        template = tpl.load_template(genre)
        arr = tpl.compile_arrangement(template, variant, bpm)
        snap = live().snapshot(include_devices=False)
        match = _match_roles(template, arr.roles_used(), snap, overrides)
        plan = session_plan(arr, match, snap, set_tempo=bpm is not None,
                            placeholders_for_missing=placeholders_for_missing,
                            include_transitions=include_transitions, clear=clear)
        report = _execute(plan, snap, dry_run)
        report["matched"] = {k: [f"{s.track_name} / {s.clip_name}" for s in v]
                             for k, v in match.sources.items()}
        if match.missing:
            report["placeholders_for"] = [template.roles[k].name for k in match.missing]
        if match.unknown_tracks:
            report["not_used"] = [g.name for g in match.unknown_tracks]
        return _dump(report)
    except (tpl.TemplateError, PlanError, LiveError, ValueError) as e:
        return _error(e)


@mcp.tool()
def build_arrangement(plan: dict[str, Any], dry_run: bool = False) -> str:
    """Build a custom arrangement in one batch. The plan (bars are 1-indexed):
    {
      "bpm": 124,                                  optional, sets tempo
      "clear": false,                              delete existing clips where writing
      "locators": [{"bar": 1, "name": "Intro"}],
      "tracks": [
        {"ref": "kick", "name": "Kick", "type": "midi", "color": "#E5484D"},   new track
        {"ref": "bass", "existing": 3}                                         existing track index
      ],
      "clips": [
        {"track": "bass", "bar": 33, "bars": 32, "source": {"slot": 0}},       tile a Session clip
        {"track": "kick", "bar": 1, "bars": 16, "name": "Kick"},                empty placeholder
        {"track": "kick", "bar": 17, "bars": 8,
         "source": {"notes": [{"pitch": 36, "start_time": 0, "duration": 0.25, "velocity": 110}],
                    "loop_bars": 1}}                                           MIDI notes, looped
      ]
    }
    A clip's "track" may also be a plain track index. Session clips tile only on their own
    track. Note times are in beats within one loop."""
    try:
        snap = live().snapshot(include_devices=False)
        return _dump(_execute(plan, snap, dry_run))
    except (PlanError, LiveError, KeyError, ValueError) as e:
        return _error(e)


@mcp.tool()
def add_transition(type: str, bar: int, bars: int = 1, name: str | None = None) -> str:
    """Drop a transition placeholder onto the arrangement: riser, downlifter, sweep, crash,
    impact, reverse_cymbal or fill. It goes on the matching transitions track (created if
    missing). For parts dropping out (a kick gap before a drop), rebuild with a template
    or plan instead, because arrangement clips can't be split through the Live API."""
    lane = tpl.TRANSITION_LANES.get(type)
    if lane is None:
        return _error(f"Unknown transition '{type}'. Options: {', '.join(tpl.TRANSITION_LANES)}")
    try:
        snap = live().snapshot(include_devices=False)
        existing = next((t for t in snap["tracks"] if t["name"] == lane and t["type"] == "midi"), None)
        track = {"ref": "lane", "existing": existing["index"]} if existing else \
            {"ref": "lane", "name": lane, "type": "midi", "color": tpl.LANE_COLORS[lane]}
        plan = {"tracks": [track], "show": False,
                "clips": [{"track": "lane", "bar": bar, "bars": bars,
                           "name": name or type.replace("_", " ").title(),
                           "color": tpl.LANE_COLORS[lane]}]}
        return _dump(_execute(plan, snap))
    except (PlanError, LiveError) as e:
        return _error(e)


@mcp.tool()
def get_clip_notes(track: int, slot: int) -> str:
    """Read the MIDI notes of one Session clip (pitch, start_time and duration in beats, velocity)."""
    try:
        return _dump(live().send("clip_notes", {"track": track, "slot": slot}))
    except LiveError as e:
        return _error(e)


# ── hearing (audio analysis service on the home PC) ──────────────────────────

@mcp.tool()
def analyze_audio(track: int | None = None, slot: int | None = None, bar: float | None = None,
                  path: str | None = None, tags: bool = False) -> str:
    """Listen to audio and report BPM, key (with Camelot), loudness (LUFS) and an energy
    curve. Pick the audio as: track + slot (a Session clip), track + bar (the Arrangement
    clip at that bar), track alone (every audio file on the track), or path (any audio file).
    tags=True adds genre and mood tags. Results are cached by file hash, so asking again
    is instant. Needs the home PC; MIDI clips have no audio to analyse."""
    try:
        return _dump(hearing.analyze(audio(), _sources(track, slot, bar, path), tags=tags))
    except HEARING_ERRORS as e:
        return _error(e)


@mcp.tool()
def audio_spectrogram(track: int | None = None, slot: int | None = None, bar: float | None = None,
                      path: str | None = None, start: float | None = None,
                      duration: float | None = None, width: int | None = None,
                      height: int | None = None):
    """Show a mel spectrogram of one audio clip or file as an image you can look at
    (time left to right, frequency bottom to top). Pick the audio like analyze_audio
    (track + slot, track + bar, or path). start and duration are in seconds; width and
    height in pixels (default 1200 x 400). Also returns the PNG's path in the cache."""
    try:
        [source] = _sources(track, slot, bar, path, one=True)
        info = hearing.spectrogram(audio(), source, start, duration, width, height)
        return [_dump(info), Image(path=info["image"])]
    except HEARING_ERRORS as e:
        return _error(e)


@mcp.tool()
def separate_stems(track: int | None = None, slot: int | None = None, bar: float | None = None,
                   path: str | None = None, model: str = "htdemucs", format: str = "flac") -> str:
    """Split one audio clip or file into stems with Demucs and download them to this
    machine. Returns the local file paths per stem (drums, bass, other, vocals; the
    htdemucs_6s model adds guitar and piano). Pick the audio like analyze_audio. Takes
    seconds on the PC's GPU, longer if the GPU is busy. Cached by file hash."""
    try:
        [source] = _sources(track, slot, bar, path, one=True)
        return _dump(hearing.stems(audio(), source, model, format))
    except HEARING_ERRORS as e:
        return _error(e)


@mcp.tool()
def audio_to_midi(track: int | None = None, slot: int | None = None, bar: float | None = None,
                  path: str | None = None, add_to_live: bool = False,
                  place_at_bar: float | None = None, bpm: float | None = None,
                  quantize: float = 0.25, onset: float | None = None, frame: float | None = None,
                  min_note_ms: float | None = None) -> str:
    """Turn one audio clip or file into MIDI (basic-pitch). Returns the .mid file path,
    the tempo used and a note summary. add_to_live=True also creates a new MIDI track
    after the source track with the notes in a Session clip (slot 0), or, with
    place_at_bar, in an Arrangement clip starting at that bar. Pick the audio like
    analyze_audio. Seconds become beats at the file's detected BPM (or bpm if given);
    quantize snaps to a grid in beats (0.25 = 1/16, 0 = off). onset and frame (0 to 1)
    make detection stricter when higher; min_note_ms drops short blips."""
    try:
        snap = live().snapshot(include_devices=False) if add_to_live or not path else None
        [source] = _sources(track, slot, bar, path, one=True, snap=snap)
        set_tempo = snap["tempo"] if snap else None
        bpb = beats_per_bar(snap) if snap else 4.0
        result = hearing.audio_to_midi(audio(), source, bpm=bpm, set_tempo=set_tempo,
                                       quantize=quantize, beats_per_bar=bpb, onset=onset,
                                       frame=frame, min_note_ms=min_note_ms)
        notes, length = result.pop("_notes"), result.pop("_length")
        if add_to_live:
            if not notes:
                result["added_to_live"] = False
                result["why_not"] = "No notes were detected, so nothing was added."
                return _dump(result)
            if place_at_bar is None and not hearing.script_supports_session_clips(live().ping()):
                return _error("The AbletonHelper Remote Script in Live is older than 0.2.0 and can't "
                              "create Session clips. Run `uv run ableton-helper install` and restart "
                              "Live, or pass place_at_bar to put the MIDI in the Arrangement instead.")
            ops = hearing.midi_clip_ops(source, notes, length, place_at_bar, bpb)
            ran = live().run_ops(ops, stop_on_error=True)
            result["added_to_live"] = not ran["errors"]
            if ran["errors"]:
                result["errors"] = ran["errors"]
            else:
                created = ran["results"][0]
                result["new_track"] = {"index": created["index"], "name": created["name"]}
                result["placed"] = "Session slot 0" if place_at_bar is None else f"Arrangement bar {place_at_bar:g}"
                result["undo"] = "One undo step in Live (Cmd+Z)."
        return _dump(result)
    except HEARING_ERRORS as e:
        return _error(e)


@mcp.tool()
def tag_loop_roles(track: int | None = None, slot: int | None = None, path: str | None = None,
                   only_unclear: bool = True, rename_tracks: bool = False,
                   use_stems: bool = True) -> str:
    """Listen to loops and say what each one is (kick, clap, hats, perc, drums, bass,
    chords, pad, lead, vocal, fx), with a confidence and the reason. With nothing picked
    it covers the set's audio clips whose track and clip names don't already say
    (only_unclear=False: all of them); or pick a track, a track + slot, or a path.
    rename_tracks=True renames those tracks to e.g. "Bass (Audio 7)" in one undo step, so
    match_roles and arrange_session can place them; tell the producer first. Uses stems
    (Demucs on the PC), pitch and key; a few seconds per new loop, cached after."""
    try:
        snap = None if path else live().snapshot(include_devices=False)
        sources = _sources(track, slot, None, path, snap=snap)
        if only_unclear and track is None and path is None:
            sources = [s for s in sources if not hearing.roles.name_kind(s.track_name, s.clip_name)]
            if not sources:
                return _dump({"loops": [], "note": "Every audio track's name already says what it is. "
                                                   "Pass only_unclear=False to listen anyway."})
        result = hearing.tag_roles(audio(), sources, use_stems=use_stems)
        if rename_tracks and snap is not None:
            ops, renamed = hearing.rename_ops(snap, hearing.track_roles(result["loops"]))
            if ops:
                ran = live().run_ops(ops)
                result["renamed"] = renamed
                if ran["errors"]:
                    result["rename_errors"] = ran["errors"]
            else:
                result["renamed"] = []
        for item in result["loops"]:
            item.pop("_verdict", None)
        return _dump(result)
    except HEARING_ERRORS as e:
        return _error(e)


@mcp.tool()
def detect_sections(track: int | None = None, slot: int | None = None, bar: float | None = None,
                    path: str | None = None) -> str:
    """Find the sections of a song or long recording (intro, verse, chorus, break, outro...),
    with start times, bars from the first downbeat and an energy per section. Meant for
    reference tracks: pass path, or pick a clip like analyze_audio. Says so plainly if the
    analysis service doesn't offer sections yet."""
    try:
        snap = None if path else live().snapshot(include_devices=False)
        [source] = _sources(track, slot, bar, path, one=True, snap=snap)
        return _dump(hearing.sections(audio(), source, beats_per_bar(snap) if snap else 4.0))
    except HEARING_ERRORS as e:
        return _error(e)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
