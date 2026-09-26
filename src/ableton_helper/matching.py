"""Match the user's Session View tracks to template roles by name.

The AI can't hear the loops yet (that's Phase 3), so this works from track and
clip names. Each track gets one "kind" (kick, bass, chords...), then every
template role picks the tracks of its kind. Anything unclear is reported back
so Claude can ask the user instead of guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .templates import Template

# kind -> {keyword: weight}. Keywords match whole words; keywords of 4+ letters
# also match as a word prefix ("stabs" → "stab").
KIND_KEYWORDS: dict[str, dict[str, float]] = {
    "kick": {"kick": 3, "kicks": 3, "bd": 3, "bassdrum": 3, "kik": 3, "kck": 3},
    "clap": {"clap": 3, "claps": 3, "snare": 3, "snares": 3, "snr": 3, "cp": 2, "sd": 2, "rim": 1.5, "rimshot": 2},
    "hats": {"hat": 2, "hats": 2, "hh": 3, "hihat": 3, "hihats": 3, "hi": 1, "closed": 1.5, "ch": 1.5},
    "open_hats": {"open": 2.5, "oh": 3, "openhat": 4, "ohat": 4},
    "ride": {"ride": 3, "rides": 3, "cymbal": 2},
    "perc": {"perc": 3, "percs": 3, "percussion": 3, "shaker": 3, "shakers": 3, "conga": 3, "congas": 3,
             "bongo": 3, "bongos": 3, "tom": 2, "toms": 2, "cowbell": 3, "tamb": 3, "tambourine": 3,
             "clave": 3, "top": 2, "tops": 2, "wood": 1.5},
    "drums": {"drums": 3, "drum": 3, "beat": 2, "break": 2.5, "breaks": 2.5, "breakbeat": 3,
              "groove": 1.5, "kit": 2},
    "bass": {"bass": 3, "bassline": 3, "sub": 3, "reese": 3, "303": 3, "808": 2, "low": 1, "wobble": 2},
    "chords": {"chord": 3, "chords": 3, "stab": 3, "stabs": 3, "keys": 3, "piano": 3, "organ": 3,
               "rhodes": 3, "epiano": 3, "ep": 1.5, "harmony": 2},
    "pad": {"pad": 3, "pads": 3, "atmos": 3, "atmosphere": 3, "ambience": 3, "strings": 2.5,
            "drone": 3, "texture": 2.5},
    "lead": {"lead": 3, "leads": 3, "synth": 2, "pluck": 3, "plucks": 3, "arp": 3, "melody": 3,
             "hook": 2, "riff": 2.5, "motif": 3, "seq": 1.5},
    "vocal": {"vocal": 3, "vocals": 3, "vox": 3, "acapella": 3, "voice": 3, "chop": 2, "chops": 2,
              "adlib": 3, "sing": 2},
    "fx": {"fx": 3, "sfx": 3, "riser": 3, "risers": 3, "sweep": 3, "impact": 3, "crash": 2.5,
           "noise": 2, "uplifter": 3, "downlifter": 3, "reverse": 2, "whoosh": 3},
    "snare_roll": {"roll": 3, "rolls": 3, "buildup": 3, "snareroll": 4},
}

GENERIC_WORDS = {"mid", "main", "and", "the", "full", "phrase", "variation", "filtered", "swung"}

TRACK_NAME_WEIGHT = 2.0
CLIP_NAME_WEIGHT = 1.0
MIN_SCORE = 2.0


def tokens(text: str) -> list[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)  # "OpenHat" → "Open Hat"
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def _kind_scores(words: list[str], weight: float) -> dict[str, float]:
    scores: dict[str, float] = {}
    for kind, keywords in KIND_KEYWORDS.items():
        total = 0.0
        for word in words:
            if word in keywords:
                total += keywords[word]
                continue
            for kw, w in keywords.items():
                if len(kw) >= 4 and word.startswith(kw):
                    total += w
                    break
        if total:
            scores[kind] = total * weight
    # "Open Hat" should be open hats, not hats.
    if "open_hats" in scores and "hats" in scores:
        scores["open_hats"] += scores["hats"]
    return scores


@dataclass
class TrackGuess:
    index: int
    name: str
    kind: str | None
    score: float
    clips: list[dict[str, Any]]
    runner_up: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"track": self.index, "name": self.name, "kind": self.kind,
                "score": round(self.score, 1), "runner_up": self.runner_up,
                "clips": [{"slot": c["slot"], "name": c["name"], "bars_hint": c.get("length")}
                          for c in self.clips]}


def classify_tracks(snapshot: dict[str, Any]) -> list[TrackGuess]:
    guesses = []
    for track in snapshot["tracks"]:
        if track.get("is_group") or not track.get("session_clips"):
            continue
        scores = _kind_scores(tokens(track["name"]), TRACK_NAME_WEIGHT)
        for clip in track["session_clips"]:
            for kind, s in _kind_scores(tokens(clip["name"]), CLIP_NAME_WEIGHT).items():
                scores[kind] = scores.get(kind, 0.0) + s
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        kind, score = ranked[0] if ranked else (None, 0.0)
        if score < MIN_SCORE:
            kind = None
        guesses.append(TrackGuess(track["index"], track["name"], kind, score,
                                  track["session_clips"], ranked[1][0] if len(ranked) > 1 else None))
    return guesses


@dataclass
class Source:
    track: int
    slot: int
    track_name: str
    clip_name: str
    why: str

    def to_dict(self) -> dict[str, Any]:
        return {"track": self.track, "slot": self.slot, "track_name": self.track_name,
                "clip_name": self.clip_name, "why": self.why}


@dataclass
class RoleMatch:
    sources: dict[str, list[Source]] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    unknown_tracks: list[TrackGuess] = field(default_factory=list)
    unused_tracks: list[TrackGuess] = field(default_factory=list)
    guesses: list[TrackGuess] = field(default_factory=list)

    def to_dict(self, template: Template) -> dict[str, Any]:
        return {
            "roles": {k: [s.to_dict() for s in v] for k, v in self.sources.items()},
            "missing_roles": [{"role": k, "name": template.roles[k].name,
                               "looks_for": template.roles[k].kind} for k in self.missing],
            "unrecognised_tracks": [g.to_dict() for g in self.unknown_tracks],
            "unused_tracks": [g.to_dict() for g in self.unused_tracks],
        }


def match_roles(template: Template, roles: list[str], snapshot: dict[str, Any],
                overrides: dict[str, list[dict[str, int]]] | None = None) -> RoleMatch:
    """Pick session clips for each role.

    ``overrides`` maps role → [{"track": i, "slot": j}, ...] and wins over guessing.
    """
    overrides = overrides or {}
    guesses = classify_tracks(snapshot)
    by_kind: dict[str, list[TrackGuess]] = {}
    for g in guesses:
        if g.kind:
            by_kind.setdefault(g.kind, []).append(g)
    track_by_index = {t["index"]: t for t in snapshot["tracks"]}

    result = RoleMatch(guesses=guesses)
    used_tracks: set[int] = set()

    def first_clip(g: TrackGuess, why: str, prefer_second: bool = False) -> Source:
        clips = g.clips
        clip = clips[1] if prefer_second and len(clips) > 1 else clips[0]
        return Source(g.index, clip["slot"], g.name, clip["name"], why)

    # Pass 1: overrides and ordinary roles.
    ordered = [r for r in roles if not template.roles[r].variation_of] + \
              [r for r in roles if template.roles[r].variation_of]
    for key in ordered:
        role = template.roles[key]
        if key in overrides:
            picked = []
            for o in overrides[key]:
                t = track_by_index.get(int(o["track"]))
                if t is None:
                    raise ValueError(f"Override for '{key}': no track {o['track']}")
                slot = int(o.get("slot", t["session_clips"][0]["slot"] if t["session_clips"] else 0))
                clip = next((c for c in t["session_clips"] if c["slot"] == slot), None)
                if clip is None:
                    raise ValueError(f"Override for '{key}': track '{t['name']}' has no clip in slot {slot}")
                picked.append(Source(t["index"], slot, t["name"], clip["name"], "set by you"))
            result.sources[key] = picked
            used_tracks.update(s.track for s in picked)
            continue

        if role.variation_of and role.variation_of in result.sources:
            base = result.sources[role.variation_of]
            picked = []
            for s in base:
                t = track_by_index[s.track]
                g = TrackGuess(t["index"], t["name"], None, 0.0, t["session_clips"])
                has_second = len(g.clips) > 1
                picked.append(first_clip(
                    g, f"variation of {template.roles[role.variation_of].name}"
                       + ("" if has_second else " (only one clip on this track — same loop reused)"),
                    prefer_second=has_second))
            result.sources[key] = picked
            continue

        candidates: list[TrackGuess] = []
        why = ""
        for kind in [role.kind] + role.fallback:
            if by_kind.get(kind):
                candidates = by_kind[kind]
                why = f"looks like {kind}" + ("" if kind == role.kind else f" (fallback for {role.kind})")
                break
        if not candidates:
            result.missing.append(key)
            continue
        # Prefer tracks whose name mentions this specific role ("Sub" for the sub role).
        specific = (set(tokens(key)) | set(tokens(role.name))) - set(tokens(role.kind)) - GENERIC_WORDS
        narrowed = [g for g in candidates if specific & set(tokens(g.name))]
        if narrowed and len(narrowed) < len(candidates):
            candidates = narrowed
        result.sources[key] = [first_clip(g, why) for g in candidates]
        used_tracks.update(g.index for g in candidates)

    result.unknown_tracks = [g for g in guesses if g.kind is None and g.index not in used_tracks]
    result.unused_tracks = [g for g in guesses if g.kind is not None and g.index not in used_tracks]
    return result
