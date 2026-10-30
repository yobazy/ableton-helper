"""Genre templates: load the YAML files and compile them into a bar timeline.

Bars are 1-indexed like Live's ruler, and every range is half-open:
a 16-bar section starting at bar 1 covers bars [1, 17).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

TEMPLATE_DIR = Path(__file__).parent / "templates"

# Which lane (track) each transition placeholder goes on.
TRANSITION_LANES = {
    "riser": "Risers & Sweeps",
    "downlifter": "Risers & Sweeps",
    "sweep": "Risers & Sweeps",
    "crash": "Impacts",
    "impact": "Impacts",
    "reverse_cymbal": "Impacts",
    "fill": "Fills",
}
LANE_COLORS = {"Risers & Sweeps": "#8B8D98", "Impacts": "#B5B3AD", "Fills": "#F2555A"}
DRUM_KINDS = {"kick", "hats", "open_hats", "clap", "perc", "ride", "drums"}


class TemplateError(ValueError):
    pass


@dataclass
class Role:
    key: str
    name: str
    kind: str
    color: str
    in_all: bool = True
    fallback: list[str] = field(default_factory=list)
    variation_of: str | None = None
    note: str | None = None


@dataclass
class Section:
    name: str
    start: int  # bar, 1-indexed
    bars: int
    energy: int | None
    roles: list[str]

    @property
    def end(self) -> int:
        return self.start + self.bars


@dataclass
class Span:
    role: str
    start: int
    end: int
    section: str


@dataclass
class TransitionClip:
    type: str
    lane: str
    start: int
    end: int
    section: str | None
    note: str | None = None


@dataclass
class Template:
    genre: str
    name: str
    description: str
    bpm: float
    bpm_range: list[float]
    roles: dict[str, Role]
    variants: dict[str, dict[str, Any]]
    reference: dict[str, Any] | None = None  # set on templates made from a reference track
    section_labels: dict[str, str] | None = None  # allin1 label → section kind, for analyze_reference


@dataclass
class Arrangement:
    template: Template
    variant: str
    bpm: float
    sections: list[Section]
    spans: list[Span]
    transitions: list[TransitionClip]
    gaps: list[dict[str, Any]]

    @property
    def total_bars(self) -> int:
        return self.sections[-1].end - 1 if self.sections else 0

    def roles_used(self) -> list[str]:
        used = {s.role for s in self.spans}
        return [k for k in self.template.roles if k in used]

    def lanes_used(self) -> list[str]:
        used = {t.lane for t in self.transitions}
        return [lane for lane in LANE_COLORS if lane in used]


# ── loading ──────────────────────────────────────────────────────────────────

def user_template_dir() -> Path:
    """Where generated templates (from reference tracks) are saved."""
    return Path(os.environ.get("ABLETON_HELPER_TEMPLATES",
                               Path.home() / ".config" / "ableton-helper" / "templates")).expanduser()


def available_genres() -> list[str]:
    return sorted(p.stem for p in TEMPLATE_DIR.glob("*.yaml"))


def user_genres() -> list[str]:
    d = user_template_dir()
    return sorted(p.stem for p in d.glob("*.yaml")) if d.is_dir() else []


def template_key(genre: str) -> str:
    key = genre.strip().lower().replace(" ", "_").replace("-", "_").replace("&", "and")
    aliases = {"dnb": "drum_and_bass", "d_and_b": "drum_and_bass", "ukg": "uk_garage",
               "garage": "uk_garage", "techhouse": "tech_house", "deephouse": "deep_house"}
    return aliases.get(key, key)


def load_template(genre: str) -> Template:
    """A built-in genre, else a template in the user folder. Cached per file version, so an
    edited or regenerated file is picked up without restarting the server."""
    key = template_key(genre)
    path = TEMPLATE_DIR / f"{key}.yaml"
    if not path.exists():
        path = user_template_dir() / f"{key}.yaml"
    if not KEY_PATTERN.fullmatch(key) or not path.exists():
        mine = user_genres()
        raise TemplateError(f"No template for '{genre}'. Available: {', '.join(available_genres())}"
                            + (f"; from references: {', '.join(mine)}" if mine else ""))
    stat = path.stat()
    return _load_file(str(path), (stat.st_mtime_ns, stat.st_size), key)


KEY_PATTERN = re.compile(r"[a-z0-9_]+")


@lru_cache(maxsize=64)
def _load_file(path: str, _version: tuple[int, int], key: str) -> Template:
    try:
        data = yaml.safe_load(Path(path).read_text())
    except yaml.YAMLError as e:
        raise TemplateError(f"Template '{key}' isn't valid YAML: {e}") from e
    return parse_template(data, key)


def parse_template(data: dict[str, Any], key: str) -> Template:
    """A template from its YAML data; compiles every variant so a broken one fails here."""
    if not isinstance(data, dict) or not data.get("roles") or not data.get("variants"):
        raise TemplateError(f"Template '{key}' needs roles and variants")
    roles = {}
    for rkey, spec in data["roles"].items():
        spec = spec or {}
        roles[rkey] = Role(
            key=rkey,
            name=spec.get("name", rkey.replace("_", " ").title()),
            kind=spec.get("kind", rkey),
            color=spec.get("color", "#8B8D98"),
            in_all=spec.get("in_all", True),
            fallback=list(spec.get("fallback", [])),
            variation_of=spec.get("variation_of"),
            note=spec.get("note"),
        )
    template = Template(
        genre=data.get("genre", key),
        name=data.get("name", key),
        description=data.get("description", ""),
        bpm=float(data.get("bpm", 120)),
        bpm_range=list(data.get("bpm_range", [data.get("bpm", 120)] * 2)),
        roles=roles,
        variants=data["variants"],
        reference=data.get("reference"),
        section_labels=_section_labels(data.get("section_labels"), key),
    )
    for vname in template.variants:
        compile_arrangement(template, vname)  # fail fast on a broken template
    return template


# The kinds of section a reference's labels can map to (analyze_reference names them).
SECTION_KINDS = ("Intro", "Groove", "Breakdown", "Build", "Drop", "Outro")


def _section_labels(raw: Any, key: str) -> dict[str, str] | None:
    """``section_labels: {inst: Groove, solo: Drop}``: keys are allin1 labels (any case),
    values one of SECTION_KINDS (any case)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TemplateError(f"Template '{key}': section_labels is a map like {{chorus: Drop}}")
    kinds = {k.lower(): k for k in SECTION_KINDS}
    out = {}
    for label, kind in raw.items():
        found = kinds.get(str(kind).strip().lower())
        if not found:
            raise TemplateError(f"Template '{key}': section_labels maps '{label}' to '{kind}'; "
                                f"use one of {', '.join(SECTION_KINDS)}")
        out[str(label).strip().lower()] = found
    return out


# ── compiling ────────────────────────────────────────────────────────────────

def _resolve_roles(template: Template, tokens: list[str], where: str) -> list[str]:
    selected: list[str] = []
    removed: set[str] = set()
    for token in tokens:
        if token == "all":
            selected.extend(k for k, r in template.roles.items() if r.in_all)
        elif token.startswith("-"):
            removed.add(token[1:])
        else:
            selected.append(token)
    for key in list(selected) + list(removed):
        if key not in template.roles:
            raise TemplateError(f"{where}: unknown role '{key}'")
    seen: set[str] = set()
    ordered = []
    for key in template.roles:  # keep the template's role order
        if key in selected and key not in removed and key not in seen:
            ordered.append(key)
            seen.add(key)
    return ordered


def _range_for(section: Section, at: str, bars: int | None) -> tuple[int, int]:
    if at == "whole" or bars is None:
        return section.start, section.end
    bars = min(bars, section.bars)
    if at == "start":
        return section.start, section.start + bars
    if at == "end":
        return section.end - bars, section.end
    raise TemplateError(f"'at' must be start, end or whole, got '{at}'")


def _subtract(spans: list[Span], role_filter, gap_start: int, gap_end: int) -> list[Span]:
    out = []
    for s in spans:
        if not role_filter(s) or s.end <= gap_start or s.start >= gap_end:
            out.append(s)
            continue
        if s.start < gap_start:
            out.append(Span(s.role, s.start, gap_start, s.section))
        if s.end > gap_end:
            out.append(Span(s.role, gap_end, s.end, s.section))
    return out


def compile_arrangement(template: Template, variant: str = "extended",
                        bpm: float | None = None) -> Arrangement:
    if variant not in template.variants:
        raise TemplateError(
            f"{template.name} has no '{variant}' variant. Options: {', '.join(template.variants)}")
    spec = template.variants[variant]

    sections: list[Section] = []
    bar = 1
    for raw in spec["sections"]:
        where = f"{template.genre}/{variant}/{raw['name']}"
        roles = _resolve_roles(template, raw.get("roles", []), where)
        section = Section(raw["name"], bar, int(raw["bars"]), raw.get("energy"), roles)
        sections.append(section)
        bar = section.end
    by_name = {s.name: s for s in sections}
    total_end = bar

    spans = [Span(role, s.start, s.end, s.name) for s in sections for role in s.roles]

    gaps: list[dict[str, Any]] = []
    transitions: list[TransitionClip] = []
    for t in spec.get("transitions", []):
        kind = t["type"]
        if kind == "gap":
            section = by_name.get(t["section"])
            if section is None:
                raise TemplateError(f"gap refers to unknown section '{t['section']}'")
            g_start, g_end = _range_for(section, t.get("at", "end"), t.get("bars", 1))
            roles = t.get("roles", ["all"])
            role_set = None if "all" in roles else set(roles)
            spans = _subtract(spans, lambda s, rs=role_set: rs is None or s.role in rs, g_start, g_end)
            gaps.append({"start": g_start, "end": g_end, "roles": roles,
                         "section": section.name, "note": t.get("note")})
            continue

        lane = TRANSITION_LANES.get(kind)
        if lane is None:
            raise TemplateError(f"Unknown transition type '{kind}'")
        if "every" in t:
            every = int(t["every"])
            bars = int(t.get("bars", 1))
            for phrase_end in range(every, total_end - 1, every):
                start, end = phrase_end + 1 - bars, phrase_end + 1
                if not _drums_active(template, spans, start, end):
                    continue
                transitions.append(TransitionClip(kind, lane, start, end, _section_at(sections, start)))
        elif "sections" in t:
            for name in t["sections"]:
                section = by_name.get(name)
                if section is None:
                    raise TemplateError(f"{kind} refers to unknown section '{name}'")
                start, end = _range_for(section, "start", t.get("bars", 1))
                transitions.append(TransitionClip(kind, lane, start, end, name, t.get("note")))
        else:
            section = by_name.get(t.get("section"))
            if section is None:
                raise TemplateError(f"{kind} refers to unknown section '{t.get('section')}'")
            start, end = _range_for(section, t.get("at", "whole"), t.get("bars"))
            transitions.append(TransitionClip(kind, lane, start, end, section.name, t.get("note")))

    spans.sort(key=lambda s: (list(template.roles).index(s.role), s.start))
    transitions.sort(key=lambda t: (t.lane, t.start))
    return Arrangement(template, variant, float(bpm or template.bpm), sections, spans, transitions, gaps)


def _section_at(sections: list[Section], bar: int) -> str | None:
    for s in sections:
        if s.start <= bar < s.end:
            return s.name
    return None


def _drums_active(template: Template, spans: list[Span], start: int, end: int) -> bool:
    for s in spans:
        if template.roles[s.role].kind in DRUM_KINDS and s.start < end and s.end > start:
            return True
    return False


# ── presentation ─────────────────────────────────────────────────────────────

def bar_to_time(bar: int, bpm: float, beats_per_bar: float = 4.0) -> str:
    seconds = (bar - 1) * beats_per_bar * 60.0 / bpm
    return f"{int(seconds // 60)}:{int(round(seconds % 60)):02d}"


def describe(arr: Arrangement) -> str:
    """Markdown overview of an arrangement: sections, what plays, transitions."""
    t = arr.template
    lines = [
        f"## {t.name} — {arr.variant} ({arr.total_bars} bars, {arr.bpm:g} BPM, "
        f"{bar_to_time(arr.total_bars + 1, arr.bpm)})",
        t.description,
        "",
        "| Bars | Time | Section | Energy | Playing |",
        "|---|---|---|---|---|",
    ]
    for s in arr.sections:
        names = ", ".join(t.roles[r].name for r in s.roles) or "—"
        energy = "▮" * int(s.energy) if s.energy else ""
        lines.append(f"| {s.start}–{s.end - 1} | {bar_to_time(s.start, arr.bpm)} | {s.name} | "
                     f"{energy} | {names} |")
    if arr.gaps:
        lines += ["", "**Gaps (parts drop out):**"]
        for g in arr.gaps:
            who = "everything" if "all" in g["roles"] else ", ".join(t.roles[r].name for r in g["roles"])
            note = f" — {g['note']}" if g.get("note") else ""
            where = f"Bar {g['start']}" if g["end"] - g["start"] == 1 else f"Bars {g['start']}–{g['end'] - 1}"
            lines.append(f"- {where} ({g['section']}): {who} out{note}")
    if arr.transitions:
        lines += ["", "**Transition placeholders:**"]
        grouped: dict[str, list[str]] = {}
        for tr in arr.transitions:
            grouped.setdefault(tr.type, []).append(
                f"{tr.start}" if tr.end - tr.start == 1 else f"{tr.start}–{tr.end - 1}")
        for kind, where in grouped.items():
            lines.append(f"- {kind.replace('_', ' ').title()}: bars {', '.join(where)}")
    notes = [r for r in t.roles.values() if r.note and r.key in arr.roles_used()]
    if notes:
        lines += ["", "**Role notes:**"]
        lines += [f"- {r.name}: {r.note}" for r in notes]
    return "\n".join(lines)
