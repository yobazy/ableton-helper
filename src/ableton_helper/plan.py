"""Arrangement plans (in bars) → primitive ops for the Remote Script (in beats).

A plan is what Claude, or a template, describes:

    {
      "bpm": 124,                      # optional; sets the tempo
      "clear": false,                  # delete existing arrangement clips where we place new ones
      "locators": [{"bar": 1, "name": "Intro"}],
      "tracks": [
        {"ref": "kick", "name": "Kick", "type": "midi", "color": "#E5484D"},   # new track
        {"ref": "bass", "existing": 3}                                         # a track already in the set
      ],
      "clips": [
        {"track": "kick", "bar": 1, "bars": 16, "source": {"placeholder": true}, "name": "Kick"},
        {"track": "bass", "bar": 33, "bars": 32, "source": {"slot": 0}},
        {"track": "kick", "bar": 17, "bars": 8, "source": {"notes": [...], "loop_bars": 1}}
      ]
    }

Bars are 1-indexed like Live's ruler. ``{"slot": n}`` tiles the Session clip in
that slot of the same track across the range; a partial tile at the end of a
MIDI range is rebuilt from the loop's notes so nothing spills past the range.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .matching import RoleMatch
from .templates import LANE_COLORS, Arrangement

EPS = 1e-6


class PlanError(ValueError):
    pass


def hex_color(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).lstrip("#")
    if len(text) != 6:
        raise PlanError(f"Colors are #RRGGBB, got {value!r}")
    return int(text, 16)


@dataclass
class Compiled:
    ops: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)


def beats_per_bar(snapshot: dict[str, Any]) -> float:
    num, den = snapshot.get("signature", [4, 4])
    return num * 4.0 / den


def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + EPS:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def compile_plan(plan: dict[str, Any], snapshot: dict[str, Any]) -> Compiled:
    bpb = beats_per_bar(snapshot)
    tracks_by_index = {t["index"]: t for t in snapshot["tracks"]}
    warnings: list[str] = []
    ops: list[dict[str, Any]] = [{"op": "reset"}]

    if plan.get("bpm"):
        ops.append({"op": "set_tempo", "bpm": float(plan["bpm"])})

    # Tracks: what each ref points at, and what we know about it.
    info: dict[str, dict[str, Any]] = {}
    for t in plan.get("tracks", []):
        ref = t.get("ref")
        if not ref:
            raise PlanError(f"Track entry needs a 'ref': {t}")
        if ref in info:
            raise PlanError(f"Duplicate track ref '{ref}'")
        if "existing" in t:
            index = int(t["existing"])
            live = tracks_by_index.get(index)
            if live is None:
                raise PlanError(f"Track ref '{ref}': there is no track {index} in the set")
            op = {"op": "use_track", "ref": ref, "index": index}
            if t.get("color") is not None:
                op["color"] = hex_color(t["color"])
            ops.append(op)
            info[ref] = {"type": live["type"], "name": live["name"],
                         "clips": {c["slot"]: c for c in live["session_clips"]}}
        else:
            kind = t.get("type", "midi")
            ops.append({"op": "create_track", "ref": ref, "type": kind, "name": t.get("name", ref),
                        "color": hex_color(t.get("color"))})
            info[ref] = {"type": kind, "name": t.get("name", ref), "clips": {}}

    def resolve(ref: Any) -> str:
        if isinstance(ref, int):
            key = f"#{ref}"
            if key not in info:
                live = tracks_by_index.get(ref)
                if live is None:
                    raise PlanError(f"There is no track {ref} in the set")
                ops.append({"op": "use_track", "ref": key, "index": ref})
                info[key] = {"type": live["type"], "name": live["name"],
                             "clips": {c["slot"]: c for c in live["session_clips"]}}
            return key
        if ref not in info:
            raise PlanError(f"Clip refers to unknown track '{ref}'")
        return ref

    # Group clip requests; slot sources on one track merge so tiles run continuously.
    tiles: dict[tuple[str, int], list[tuple[float, float]]] = {}
    singles: list[dict[str, Any]] = []
    for c in plan.get("clips", []):
        ref = resolve(c["track"])
        start = (float(c["bar"]) - 1) * bpb
        length = float(c["bars"]) * bpb
        if length <= 0:
            raise PlanError(f"Clip on '{ref}' has no length: {c}")
        source = c.get("source") or {"placeholder": True}
        if "slot" in source:
            slot = int(source["slot"])
            if slot not in info[ref]["clips"]:
                raise PlanError(f"Track '{info[ref]['name']}' has no Session clip in slot {slot}")
            tiles.setdefault((ref, slot), []).append((start, start + length))
        else:
            if info[ref]["type"] != "midi":
                raise PlanError(f"Track '{info[ref]['name']}' is an audio track; placeholder and note "
                                f"clips need a MIDI track")
            singles.append({"ref": ref, "start": start, "length": length, "source": source,
                            "name": c.get("name"), "color": hex_color(c.get("color"))})

    # Clear what's already on the arrangement where we're about to write.
    if plan.get("clear"):
        ranges: dict[str, list[tuple[float, float]]] = {}
        for (ref, _), spans in tiles.items():
            ranges.setdefault(ref, []).extend(spans)
        for s in singles:
            ranges.setdefault(s["ref"], []).append((s["start"], s["start"] + s["length"]))
        for ref, spans in ranges.items():
            for a, b in _merge(spans):
                ops.append({"op": "clear_arrangement", "track": ref, "start": a, "end": b})

    for loc in plan.get("locators", []):
        ops.append({"op": "create_locator", "time": (float(loc["bar"]) - 1) * bpb,
                    "name": loc.get("name", "")})

    placed = rebuilt = 0
    for (ref, slot), spans in tiles.items():
        clip = info[ref]["clips"][slot]
        loop = float(clip["length"])
        if loop <= EPS:
            raise PlanError(f"Session clip '{clip['name']}' has zero length")
        for a, b in _merge(spans):
            t = a
            while t + loop <= b + EPS:
                ops.append({"op": "place_session_clip", "track": ref, "slot": slot, "time": t})
                placed += 1
                t += loop
            tail = b - t
            if tail > EPS:
                if clip.get("is_midi"):
                    ops.append({"op": "create_midi_clip", "track": ref, "start": t, "length": tail,
                                "name": clip["name"], "color": clip.get("color"),
                                "notes_from": {"slot": slot, "offset": 0.0}})
                    rebuilt += 1
                else:
                    warnings.append(
                        f"'{clip['name']}' on '{info[ref]['name']}' is {loop / bpb:g} bars, which doesn't "
                        f"divide the range ending at bar {b / bpb + 1:g}; the last {tail / bpb:g} bars "
                        f"were left empty (audio clips can't be trimmed through the Live API).")

    for s in singles:
        op = {"op": "create_midi_clip", "track": s["ref"], "start": s["start"], "length": s["length"]}
        if s["name"]:
            op["name"] = s["name"]
        if s["color"] is not None:
            op["color"] = s["color"]
        src = s["source"]
        if "notes" in src:
            loop_beats = float(src.get("loop_bars", s["length"] / bpb)) * bpb
            notes = []
            offset = 0.0
            while offset < s["length"] - EPS:
                for n in src["notes"]:
                    start = float(n["start_time"]) + offset
                    if start < s["length"] - EPS and float(n["start_time"]) < loop_beats - EPS:
                        m = dict(n)
                        m["start_time"] = start
                        m["duration"] = min(float(n["duration"]), s["length"] - start)
                        notes.append(m)
                offset += loop_beats
            op["notes"] = notes
        ops.append(op)

    if plan.get("show", True):
        ops.append({"op": "show_arrangement", "time": 0.0})

    return Compiled(ops, warnings, {"session_clip_copies": placed, "rebuilt_tails": rebuilt,
                                    "new_clips": len(singles),
                                    "locators": len(plan.get("locators", []))})


# ── plans from templates ─────────────────────────────────────────────────────

def _locators(arr: Arrangement) -> list[dict[str, Any]]:
    return [{"bar": s.start, "name": s.name} for s in arr.sections]


def _transition_tracks_and_clips(arr: Arrangement, snapshot: dict[str, Any]):
    tracks, clips = [], []
    existing = {t["name"]: t for t in snapshot["tracks"] if t["type"] == "midi"}
    for lane in arr.lanes_used():
        ref = "lane:" + lane
        if lane in existing:
            tracks.append({"ref": ref, "existing": existing[lane]["index"]})
        else:
            tracks.append({"ref": ref, "name": lane, "type": "midi", "color": LANE_COLORS[lane]})
    for tr in arr.transitions:
        clips.append({"track": "lane:" + tr.lane, "bar": tr.start, "bars": tr.end - tr.start,
                      "name": tr.type.replace("_", " ").title(), "color": LANE_COLORS[tr.lane]})
    return tracks, clips


def skeleton_plan(arr: Arrangement, snapshot: dict[str, Any], set_tempo: bool = True,
                  include_transitions: bool = True, clear: bool = False) -> dict[str, Any]:
    """Every role becomes a new MIDI track of named, colored placeholder clips."""
    roles = arr.template.roles
    tracks = [{"ref": key, "name": roles[key].name, "type": "midi", "color": roles[key].color}
              for key in arr.roles_used()]
    clips = [{"track": s.role, "bar": s.start, "bars": s.end - s.start,
              "name": roles[s.role].name, "color": roles[s.role].color} for s in arr.spans]
    if include_transitions:
        t_tracks, t_clips = _transition_tracks_and_clips(arr, snapshot)
        tracks += t_tracks
        clips += t_clips
    return {"bpm": arr.bpm if set_tempo else None, "clear": clear, "locators": _locators(arr),
            "tracks": tracks, "clips": clips}


def session_plan(arr: Arrangement, match: RoleMatch, snapshot: dict[str, Any],
                 set_tempo: bool = False, placeholders_for_missing: bool = True,
                 include_transitions: bool = True, clear: bool = False) -> dict[str, Any]:
    """Arrange the user's Session clips along the template; placeholders fill the gaps."""
    roles = arr.template.roles
    tracks: list[dict[str, Any]] = []
    clips: list[dict[str, Any]] = []
    seen_tracks: set[int] = set()
    for key, sources in match.sources.items():
        for src in sources:
            if src.track not in seen_tracks:
                tracks.append({"ref": f"t{src.track}", "existing": src.track})
                seen_tracks.add(src.track)
    for span in arr.spans:
        for src in match.sources.get(span.role, []):
            clips.append({"track": f"t{src.track}", "bar": span.start, "bars": span.end - span.start,
                          "source": {"slot": src.slot}})
    if placeholders_for_missing:
        missing = [k for k in arr.roles_used() if k in match.missing]
        for key in missing:
            tracks.append({"ref": key, "name": f"{roles[key].name} (to do)", "type": "midi",
                           "color": roles[key].color})
        clips += [{"track": s.role, "bar": s.start, "bars": s.end - s.start,
                   "name": roles[s.role].name, "color": roles[s.role].color}
                  for s in arr.spans if s.role in missing]
    if include_transitions:
        t_tracks, t_clips = _transition_tracks_and_clips(arr, snapshot)
        tracks += t_tracks
        clips += t_clips
    return {"bpm": arr.bpm if set_tempo else None, "clear": clear, "locators": _locators(arr),
            "tracks": tracks, "clips": clips}
