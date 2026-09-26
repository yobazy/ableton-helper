# Ableton Helper — Spec

An AI arrangement assistant for Ableton Live. You make the loops and sounds; it turns them into a full, genre-appropriate arrangement in Arrangement View, and you chat with it from Claude Code/Desktop or from a device inside Live.

**Problem:** Getting from an 8-bar loop to a finished track (intro, builds, breakdowns, drops, outro) is slow and hard. This tool handles the structure so the producer can focus on the sound.

---

## Goals

- Build a complete arrangement skeleton for a genre in one request, in seconds rather than minutes.
- Arrange the user's own loops and clips, not just empty placeholders.
- Run on the user's Claude subscription (Claude Code), with no separate API key needed for personal use.
- Let the AI understand what the audio sounds like, not only track and clip names.
- Arrange a track to match the structure of a reference track.

## Non-goals (for now)

- Writing full songs or melodies from scratch. Generating loops is a possible later feature.
- Mixing and mastering.
- Distributing the tool to other users. Personal use only for now; see the Risks section.

---

## Architecture

```
Ableton Live
 ├─ Remote Script (forked ableton-mcp, + batch tools)  ◄──socket──┐
 └─ Max for Live chat device (node.script) ──spawns──┐            │
                                                     ▼            │
                              Claude Code (subscription login)    │
                                                     │ MCP        │
                                                     ▼            │
                                          MCP server (Python) ────┘
                                                     │
                                                     ▼
                                   Analysis service (Python, local)
                                   librosa · Essentia · allin1 · Demucs · CLAP
```

| Component | Language | Responsibility |
|---|---|---|
| Remote Script | Python (Live's embedded runtime) | Runs inside Live. Carries out commands against the Live Object Model, including large batch commands in one pass. |
| MCP server | Python | Exposes tools to Claude, holds genre templates, and turns high-level requests into batch commands. |
| Genre templates | YAML | Arrangement knowledge stored as data: sections, bar counts, element roles, transitions. |
| Max for Live device | Max + Node (node.script) | Chat window inside Live. Spawns `claude -p` in the background and streams replies back. |
| Analysis service | Python | Turns audio into data Claude can use: features, tags, stems, MIDI, sections, spectrogram images. |

**Base:** fork [ahujasid/ableton-mcp](https://github.com/ahujasid/ableton-mcp) rather than starting from scratch. Check its license before forking. The fork's main addition is arrangement knowledge and batch tools; the socket connection to Live is already solved.

---

## Build order

### Progress (2026-09-26)

| Step | Status |
|---|---|
| 1.1 Baseline | Benchmark written (`uv run ableton-helper bench`). Not yet run: needs Live open with the Remote Script enabled. |
| 1.2 `get_session_snapshot` | Done: the `get_session` tool. |
| 1.3 `build_arrangement` | Done: one request, one main-thread tick, one undo step. |
| 1.4 Genre templates | Done: house (extended, radio), tech house, deep house, techno, DnB, UK garage. |
| 1.5 Arrange existing loops | Done: the `match_roles` and `arrange_session` tools. |
| 1.6 Transitions | Partly done. Gaps are applied at build time. Placeholders cover risers, downlifters, crashes and fills. Filter-sweep automation isn't built yet. |

**Change from the plan:** instead of forking ableton-mcp, this repo has its own small Remote Script that reuses upstream's socket and threading approach (MIT, credited in THIRD_PARTY_NOTICES.md). Two reasons:
- Upstream includes telemetry and an opt-in dataset uploader (Supabase), which aren't wanted here.
- Its one-request-per-change design is exactly what this project replaces.

Our script listens on port 9878, so it can run next to upstream (9877). The 1.1 benchmark compares the two designs by sending the same ops one request at a time and then as one batch.

### Phase 1 — Batch tools and genre templates (MVP)

**1.1 Fork and measure a baseline**
- Fork ableton-mcp, install the Remote Script, and connect it to Claude Code.
- Build the house skeleton (8 tracks, 224 bars) with the original tools and record how long it takes.
- *Acceptance:* the baseline time and number of tool calls are written in `docs/benchmarks.md`.

**1.2 `get_session_snapshot`**
- One call returns everything Claude needs to know about the set: tempo, time signature, tracks (name, type, color, devices), Session and Arrangement clips (name, length, position, audio file path, and a MIDI note summary), and locators.
- *Acceptance:* one round trip returns the full state for a set with 16 tracks and 64 clips.

**1.3 `build_arrangement` batch tool**
- Input is one JSON plan: tracks to create or reuse, locators, and a list of clip placements. Each placement either takes an existing Session clip, creates an empty named placeholder, or writes MIDI notes.
- The Remote Script runs the whole plan in a single pass. It creates each loop once and copies it with `duplicate_clip_to_arrangement`, and adds MIDI notes in bulk rather than one call per note.
- *Acceptance:* the house skeleton builds from a single tool call at least 10× faster than the 1.1 baseline, and the result matches the plan exactly.

**1.4 Genre templates (house first)**
- A YAML schema covering sections (name, start bar, length), element roles (kick, hats, bass, chords, etc.), which roles play in each section, and transition markers.
- Add `list_templates` and `apply_template(genre, bpm, length_variant)`. The latter turns a template into a `build_arrangement` plan.
- *Acceptance:* the house template produces the 224-bar layout (Intro A through Outro), with colored, named placeholder clips and a locator at each section start.

**1.5 Arrange existing loops**
- Match the user's Session clips to template roles using track and clip names (`"Bass – rolling sub"` → bass). Ask the user about anything it can't match.
- *Acceptance:* given a Session View with 6–8 loops labeled by name, one request places them in Arrangement View following the template.

**1.6 Transition tools**
- `add_transition(bar, type)`, where type is one of: kick dropout, drum fill placeholder, filter sweep automation, riser placeholder, crash.
- *Acceptance:* each type can be applied at any phrase boundary, and each one can be undone in Live.

**Example template (sketch):**
```yaml
genre: house
bpm: 124
sections:
  - { name: Intro A,     start: 1,   bars: 16, roles: [kick, hats] }
  - { name: Intro B,     start: 17,  bars: 16, roles: [kick, hats, perc] }
  - { name: Groove,      start: 33,  bars: 16, roles: [kick, hats, perc, bass, clap] }
  - { name: Groove +,    start: 49,  bars: 16, roles: [kick, hats, perc, bass, clap, open_hats, chords_filtered] }
  - { name: Breakdown,   start: 65,  bars: 16, roles: [chords, pad, hook] }
  - { name: Build,       start: 81,  bars: 16, roles: [chords, hook, snare_roll, riser] }
  - { name: Drop 1,      start: 97,  bars: 32, roles: [all] }
  - { name: Mid,         start: 129, bars: 16, roles: [kick, hats, perc, bass, clap, new_element] }
  - { name: Breakdown 2, start: 145, bars: 16, roles: [chords, hook_variation] }
  - { name: Drop 2,      start: 161, bars: 32, roles: [all, extra_layer] }
  - { name: Outro,       start: 193, bars: 32, roles: [kick, hats, perc] }
transitions:
  - { at: 64,  type: kick_dropout, bars: 1 }
  - { at: 96,  type: silence, bars: 1 }
  - { at: every_8, type: fill }
```

### Phase 2 — Max for Live chat window

**2.1 Chat device UI**
- A Max for Live device with a message input and a scrolling reply view.
- *Acceptance:* the device loads on any track and accepts text input.

**2.2 Claude Code bridge**
- `node.script` runs `claude -p "<msg>" --output-format stream-json --resume <session_id>` with an MCP config pointing at the Phase 1 server.
- Streams text into the UI and shows tool calls as short status lines.
- *Acceptance:* typing "build a house skeleton at 124" in the device produces the arrangement, with no other window needed.

**2.3 Context from Live**
- Automatically adds the user's current selection (the selected track, clip, or time range) to each message.
- *Acceptance:* "make 3 variations of this" works on the selected clip without the user naming it.

*Requires Live Suite (confirmed: user is on Live 12 Suite).*

### Phase 3 — Audio analysis ("let the AI hear")

**3.1 Analysis service**
- A local Python service exposed as extra MCP tools:
  - `analyze_audio(path)` returns key, BPM, loudness, and an energy curve (librosa/Essentia), plus timbre tags (CLAP/Essentia models).
  - `spectrogram(path)` returns a PNG image that Claude reads visually.
  - `separate_stems(path)` splits audio into stems (Demucs).
  - `audio_to_midi(path)` converts audio to MIDI (basic-pitch).
- *Acceptance:* each tool returns results for a 30-second loop in under 30 seconds, running on this Mac.

**3.2 Getting audio out of Live**
- Audio clips: read `file_path` from the snapshot.
- MIDI and instrument tracks: a Max for Live capture device records the track's output to a WAV file while the song plays, because the Live API can't export audio.
- *Acceptance:* after one play-through, every track has an analyzable audio file.

**3.3 Hearing-aware arranging**
- `get_session_snapshot` includes analysis results (tags and energy) for each clip, and the role matching in 1.5 uses them in addition to names.
- *Acceptance:* poorly named loops (`Audio 7`) still get assigned plausible roles.

### Phase 4 — Arranging from a reference track

**4.1 Reference analysis**
- `analyze_reference(path)` finds beats, downbeats, and sections (allin1), measures energy per section, and uses stem separation to work out which instruments are active in each section.
- *Acceptance:* for 3 known house tracks, the detected section boundaries fall within 1 bar of the correct phrase boundaries.

**4.2 Reference → template**
- Converts the analysis into the Phase 1 template format, snapped to 8- and 16-bar phrases.
- *Acceptance:* `apply_template` accepts a template generated from a reference.

**4.3 "Arrange like this"**
- One request arranges the user's loops following the structure and energy of the reference track.
- *Acceptance:* the section lengths and the points where energy rises and falls follow the reference.

---

## Risks and open questions

| Item | Notes |
|---|---|
| Arrangement API speed | Creating arrangement clips through the API is reportedly slow in Max JS. Measure the Python Remote Script path in 1.1 before relying on it. Fallback: write the skeleton as an `.als` file (gzipped XML) offline. |
| Subscription use | Running Claude Code headless with a claude.ai login is fine for personal use. Anthropic doesn't allow third-party products to offer claude.ai login, so distributing this to others would need API keys. |
| Remote Script debugging | Live's embedded Python is hard to debug. Log to a file from day one. |
| Live edition | Resolved: Live 12 Suite, so Max for Live is available for Phase 2. |
| Live version | Resolved: Live 12, which includes the arrangement clip API fixes. |
| Analysis dependencies | Essentia, Demucs, and allin1 are heavy installs. Keep them in a separate virtual environment or service so the MCP server stays light. |

## Later / backlog

- Generating loops (drum patterns and basslines as MIDI) per genre.
- More genres: tech house, deep house, techno, DnB, UK garage.
- An `.als` skeleton generator for new projects, with no live connection needed.
- A "critique my arrangement" tool, e.g. "nothing changes between bars 97 and 128."
- An energy-curve view in the Max for Live device.
