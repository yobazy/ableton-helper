"""MCP server: arranging tools for Claude, backed by the AbletonHelper Remote Script."""

from __future__ import annotations

import json
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import templates as tpl
from .connection import LiveConnection, LiveError
from .matching import match_roles as _match_roles
from .plan import PlanError, compile_plan, session_plan, skeleton_plan

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
- You can't hear audio. Use track and clip names, MIDI note data, and ask the producer.
"""

mcp = FastMCP("ableton-helper", instructions=INSTRUCTIONS)
_live: LiveConnection | None = None


def live() -> LiveConnection:
    global _live
    if _live is None:
        _live = LiveConnection()
    return _live


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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
