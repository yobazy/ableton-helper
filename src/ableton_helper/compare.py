"""Put a template's sections into the set, and compare the set's arrangement with a template.

``marker_plan`` turns a template (a genre, or one made from a reference track) into
locators, empty Session scenes and a "Sections" track of named blocks. ``compare``
reads the current Arrangement, works out which part each track plays (from track and
clip names, like match_roles) and lists what to change, section by section, so the set
follows the template: tracks to mute or unmute, loops to add, missing parts, breakdowns
the set doesn't have, and sections whose locators sit somewhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import templates as tpl
from .matching import MIN_SCORE, _kind_scores, tokens
from .plan import beats_per_bar, hex_color

SCENES_SCRIPT_VERSION = (0, 3, 0)
SECTION_TRACK = "Sections"
# Energy 1 to 10 → a block colour from cool to hot.
ENERGY_COLORS = ["#3E63DD", "#3E63DD", "#0090FF", "#12A594", "#30A46C", "#F5D90A", "#FFB224",
                 "#F76B15", "#E5484D", "#E5484D"]
EPS = 1e-6
PLAYING = 0.5   # a track plays in a section when active clips cover at least half of it
WEIGHTS = {"kick": 3, "bass": 3, "drums": 3}  # how much a part adds to the set's energy


class CompareError(ValueError):
    pass


def script_at_least(ping: dict[str, Any], version: tuple[int, ...]) -> bool:
    try:
        have = tuple(int(x) for x in str(ping.get("version", "0")).split(".")[:3])
    except ValueError:
        return False
    return have >= version


def default_variant(template: tpl.Template, variant: str | None) -> str:
    if variant:
        return variant
    return "extended" if "extended" in template.variants else next(iter(template.variants))


# ── markers ──────────────────────────────────────────────────────────────────

def energy_color(energy: int | None) -> str:
    return ENERGY_COLORS[max(1, min(10, energy or 5)) - 1]


def marker_plan(arr: tpl.Arrangement, snapshot: dict[str, Any], start_bar: int = 1,
                locators: bool = True, scenes: bool = False, section_track: bool = False,
                replace_locators: bool = False, set_tempo: bool = False) -> dict[str, Any]:
    """What mark_sections will do, as rows to show the producer, a build plan and extra ops."""
    if start_bar < 1:
        raise CompareError("start_bar is 1 or later.")
    if not (locators or scenes or section_track):
        raise CompareError("Nothing to do: turn on locators, scenes or section_track.")
    bpb = beats_per_bar(snapshot)
    shift = start_bar - 1
    rows = []
    for s in arr.sections:
        rows.append({"bar": s.start + shift, "bars": s.bars, "section": s.name,
                     "time": tpl.bar_to_time(s.start + shift, arr.bpm), "energy": s.energy})
    plan: dict[str, Any] = {"bpm": arr.bpm if set_tempo else None, "show": True,
                            "locators": [], "tracks": [], "clips": []}
    pre_ops: list[dict[str, Any]] = []   # before the plan's own ops (after its reset)
    post_ops: list[dict[str, Any]] = []
    notes: list[str] = []

    section_bars = {s.start + shift for s in arr.sections}
    existing = [{"bar": round(c["time"] / bpb + 1, 2), "name": c["name"], "time": c["time"]}
                for c in snapshot.get("locators", [])]
    if locators:
        plan["locators"] = [{"bar": s.start + shift, "name": s.name} for s in arr.sections]
        others = [c for c in existing if not any(abs(c["bar"] - b) < EPS for b in section_bars)]
        renamed = [c for c in existing if any(abs(c["bar"] - b) < EPS for b in section_bars)]
        if replace_locators and others:
            pre_ops += [{"op": "delete_locator", "time": c["time"]} for c in others]
            notes.append("Deletes the set's other locators: "
                         + ", ".join(f"'{c['name']}' (bar {c['bar']:g})" for c in others) + ".")
        elif others:
            notes.append("Keeps the set's other locators: "
                         + ", ".join(f"'{c['name']}' (bar {c['bar']:g})" for c in others)
                         + ". replace_locators=True deletes them.")
        if renamed:
            notes.append("Renames the locators already at section starts: "
                         + ", ".join(f"'{c['name']}' (bar {c['bar']:g})" for c in renamed) + ".")
    if section_track:
        existing_track = next((t for t in snapshot["tracks"]
                               if t["name"] == SECTION_TRACK and t["type"] == "midi"), None)
        if existing_track:
            plan["tracks"].append({"ref": "sections", "existing": existing_track["index"]})
            plan["clear"] = True  # only clears the ranges written, on that track
            notes.append(f"Rewrites the blocks on the existing '{SECTION_TRACK}' track (track "
                         f"{existing_track['index']}).")
        else:
            plan["tracks"].append({"ref": "sections", "name": SECTION_TRACK, "type": "midi",
                                   "color": "#8B8D98"})
        plan["clips"] = [{"track": "sections", "bar": s.start + shift, "bars": s.bars,
                          "name": s.name + (f" (energy {s.energy})" if s.energy else ""),
                          "color": energy_color(s.energy)} for s in arr.sections]
    if scenes:
        post_ops = [{"op": "create_scene", "index": -1, "name": f"{s.name} ({s.bars} bars)",
                     "color": hex_color(energy_color(s.energy))} for s in arr.sections]
        notes.append(f"Adds {len(arr.sections)} empty scenes after the set's "
                     f"{len(snapshot.get('scenes', []))} scenes, one per section, in order.")
    if set_tempo:
        notes.append(f"Sets the tempo to {arr.bpm:g} BPM.")
    return {"rows": rows, "plan": plan, "pre_ops": pre_ops, "post_ops": post_ops, "notes": notes}


def plan_table(rows: list[dict[str, Any]], locators: bool, scenes: bool, section_track: bool) -> str:
    made = [w for w, on in (("locator", locators), ("scene", scenes), ("block", section_track)) if on]
    lines = ["| Bar | Time | Section | Bars | Energy | Adds |", "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['bar']} | {r['time']} | {r['section']} | {r['bars']} | "
                     f"{r['energy'] if r['energy'] is not None else ''} | {', '.join(made)} |")
    return "\n".join(lines)


# ── what the set plays where ─────────────────────────────────────────────────

@dataclass
class TrackPart:
    index: int
    name: str
    kind: str | None
    clips: list[dict[str, Any]]                  # arrangement clips, beats
    session_slots: list[int] = field(default_factory=list)


def track_kind(track: dict[str, Any]) -> str | None:
    scores = _kind_scores(tokens(track["name"]), 2.0)
    for c in track.get("arrangement_clips", []) + track.get("session_clips", []):
        for kind, s in _kind_scores(tokens(c.get("name", "")), 1.0).items():
            scores[kind] = scores.get(kind, 0.0) + s
    if not scores:
        return None
    kind, score = max(scores.items(), key=lambda kv: kv[1])
    return kind if score >= MIN_SCORE else None


def set_parts(snapshot: dict[str, Any]) -> list[TrackPart]:
    out = []
    for t in snapshot["tracks"]:
        if t.get("is_group") or t["name"] in tpl.LANE_COLORS or t["name"] == SECTION_TRACK:
            continue
        if not t.get("arrangement_clips") and not t.get("session_clips"):
            continue
        out.append(TrackPart(t["index"], t["name"], track_kind(t), list(t.get("arrangement_clips", [])),
                             [c["slot"] for c in t.get("session_clips", [])]))
    return out


def coverage(clips: list[dict[str, Any]], start: float, end: float, muted: bool) -> float:
    """Share of [start, end) covered by clips that are (muted=True) or aren't muted."""
    spans = sorted((max(start, c["start"]), min(end, c["end"])) for c in clips
                   if bool(c.get("muted")) == muted and c["end"] > start + EPS and c["start"] < end - EPS)
    covered, reach = 0.0, start
    for a, b in spans:
        a = max(a, reach)
        if b > a:
            covered += b - a
            reach = b
    return covered / (end - start) if end > start else 0.0


def kinds_for(template: tpl.Template, role_keys: list[str], present: set[str]) -> set[str]:
    """The kinds of track a section wants: each role's kind, or its first fallback the set has."""
    out = set()
    for key in role_keys:
        role = template.roles[key]
        for kind in [role.kind] + role.fallback:
            if kind in present:
                out.add(kind)
                break
        else:
            out.add(role.kind)
    return out


def _bars(a: int, b: int) -> str:
    return f"bar {a}" if b - a == 1 else f"bars {a}-{b - 1}"


def _merge_changes(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join the same change on the same track over back-to-back sections."""
    out: list[dict[str, Any]] = []
    for c in changes:
        last = out[-1] if out else None
        if last and last["action"] == c["action"] and last["track"] == c["track"] and last["end_bar"] == c["bar"]:
            last["end_bar"] = c["end_bar"]
            last["sections"] += c["sections"]
        else:
            out.append({**c, "sections": list(c["sections"])})
    return out


def find_start_bar(arr: tpl.Arrangement, snapshot: dict[str, Any]) -> tuple[int, str]:
    bpb = beats_per_bar(snapshot)
    first = arr.sections[0].name.strip().lower()
    for c in snapshot.get("locators", []):
        if c["name"].strip().lower() == first:
            return int(round(c["time"] / bpb)) + 1, f"the '{c['name']}' locator"
    return 1, "bar 1 (no locator named after the first section)"


def compare(arr: tpl.Arrangement, snapshot: dict[str, Any], start_bar: int | None = None) -> dict[str, Any]:
    template = arr.template
    bpb = beats_per_bar(snapshot)
    if start_bar is None:
        start_bar, aligned = find_start_bar(arr, snapshot)
    else:
        aligned = "the start_bar you gave"
    shift = start_bar - 1
    parts = set_parts(snapshot)
    known = [p for p in parts if p.kind]
    template_kinds = {k for r in template.roles.values() for k in [r.kind] + r.fallback}
    in_play = [p for p in known if p.kind in template_kinds]
    present = {p.kind for p in in_play}
    arranged = any(p.clips for p in parts)

    out: dict[str, Any] = {"template": template.genre, "variant": arr.variant, "aligned_to": aligned,
                           "start_bar": start_bar}
    if not arranged:
        out["summary"] = ["Nothing is in the Arrangement yet. arrange_session with this template builds "
                          "it from your Session loops in one go."]
        out["changes"] = []
        return out

    changes: list[dict[str, Any]] = []
    grid = []
    set_energy = []
    for s in arr.sections:
        a, b = (s.start + shift - 1) * bpb, (s.end + shift - 1) * bpb
        want = kinds_for(template, s.roles, present)
        row = {"section": s.name, "bars": f"{s.start + shift}-{s.end + shift - 1}", "energy": s.energy,
               "playing": [], "wanted": sorted(want)}
        weight = 0
        for p in in_play:
            on = coverage(p.clips, a, b, muted=False)
            off = coverage(p.clips, a, b, muted=True)
            playing = on >= PLAYING
            if playing:
                row["playing"].append(p.name)
                weight += WEIGHTS.get(p.kind, 1)
            base = {"track": p.index, "track_name": p.name, "part": p.kind, "bar": s.start + shift,
                    "end_bar": s.end + shift, "sections": [s.name]}
            if p.kind in want and not playing:
                if off >= PLAYING:
                    changes.append({**base, "action": "unmute"})
                else:
                    changes.append({**base, "action": "add",
                                    "from": f"Session slot {p.session_slots[0]}" if p.session_slots
                                    else "its clips elsewhere in the arrangement"})
            elif p.kind not in want and playing:
                changes.append({**base, "action": "mute"})
        set_energy.append(weight)
        grid.append(row)

    changes = _merge_changes(changes)
    for c in changes:
        where = _bars(c["bar"], c["end_bar"])
        secs = ", ".join(c["sections"])
        if c["action"] == "mute":
            c["say"] = f"Mute '{c['track_name']}' in {secs} ({where}): the template has no {c['part']} there."
        elif c["action"] == "unmute":
            c["say"] = f"Unmute '{c['track_name']}' in {secs} ({where}): its clips there are switched off."
        else:
            c["say"] = f"Add '{c['track_name']}' to {secs} ({where}), from {c['from']}."

    summary = []
    # Breakdowns: the template falls by 3 or more and the set doesn't fall at all.
    for i in range(1, len(arr.sections)):
        prev, s = arr.sections[i - 1], arr.sections[i]
        if prev.energy and s.energy and prev.energy - s.energy >= 3 and set_energy[i] >= set_energy[i - 1]:
            outs = [c["track_name"] for c in changes if c["action"] == "mute" and s.name in c["sections"]]
            summary.append(
                f"The template breaks down at {s.name} ({_bars(s.start + shift, s.end + shift)}, energy "
                f"{prev.energy} to {s.energy}) but the set keeps going"
                + (f": take out {', '.join(outs)}." if outs else
                   ": take the kick and bass out there so the next section can hit."))
        elif prev.energy and s.energy and s.energy - prev.energy >= 3 and set_energy[i] <= set_energy[i - 1]:
            summary.append(f"The template lifts at {s.name} ({_bars(s.start + shift, s.end + shift)}, energy "
                           f"{prev.energy} to {s.energy}) but the set doesn't get bigger there.")
    missing = []
    for kind in sorted({k for s in arr.sections for k in kinds_for(template, s.roles, present)} - present):
        where = [s.name for s in arr.sections if kind in kinds_for(template, s.roles, present)]
        missing.append({"part": kind, "sections": where})
        summary.append(f"No track plays {kind}; the template has it in {', '.join(where)}.")

    lengths = []
    by_name = {c["name"].strip().lower(): round(c["time"] / bpb + 1, 2) for c in snapshot.get("locators", [])}
    for s in arr.sections:
        at = by_name.get(s.name.strip().lower())
        if at is not None and abs(at - (s.start + shift)) > EPS:
            lengths.append({"section": s.name, "set_bar": at, "template_bar": s.start + shift})
    if lengths:
        summary.append("Sections start elsewhere in the set: " + "; ".join(
            f"{x['section']} at bar {x['set_bar']:g}, template bar {x['template_bar']}" for x in lengths)
            + ". The changes above follow the template's bars.")
    end_bar = round(snapshot.get("song_length", 0.0) / bpb + 1, 1)
    template_end = arr.sections[-1].end + shift
    if abs(end_bar - template_end) >= 8:
        summary.append(f"The set's arrangement ends at bar {end_bar:g}; the template ends at bar {template_end}.")

    out["grid"] = grid
    out["changes"] = changes
    if not changes and not summary:
        summary.append("The arrangement already follows the template section by section.")
    out["summary"] = summary
    if missing:
        out["missing_parts"] = missing
    unclear = [p for p in parts if not p.kind]
    if unclear:
        out["not_compared"] = {"tracks": [f"{p.index} '{p.name}'" for p in unclear],
                               "why": "Their names don't say what they are. tag_loop_roles(rename_tracks=True) "
                                      "can name audio loops, or rename them."}
    off_template = [p for p in known if p.kind not in template_kinds]
    if off_template:
        out["not_in_template"] = [f"{p.index} '{p.name}' ({p.kind})" for p in off_template]
    return out


def mute_ops(changes: list[dict[str, Any]], beats_per_bar_: float) -> list[dict[str, Any]]:
    """Ops for the mute and unmute changes. Adds need loops placed, so they stay suggestions."""
    ops = []
    for c in changes:
        if c["action"] in ("mute", "unmute"):
            ops.append({"op": "set_clips_muted", "track": c["track"], "muted": c["action"] == "mute",
                        "start": (c["bar"] - 1) * beats_per_bar_, "end": (c["end_bar"] - 1) * beats_per_bar_})
    return ops
