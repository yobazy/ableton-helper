# Phase 4 progress: arrange from a reference

Branch `phase-4-arrange` (from `phase-3-1-hear`). Overnight unattended run on `pc`, 2026-10-29.

## Plan

Three tools, all on top of the Phase 1 template format so the existing tools keep working:

1. **`analyze_reference`** (SPEC 4.1 + 4.2): a reference track → an arrangement template.
   - `POST /v1/sections` (allin1 on the audio-analysis branch `sections-allin1`, not live yet):
     `bpm`, `downbeats`, `segments` [{start, end, label, loudness_lufs, energy}].
   - Bars come from the downbeats (nearest downbeat to each boundary), not from dividing by a
     tempo, so a drifting or live-played reference still lands on bars.
   - Snap boundaries to a phrase grid (8 bars by default, with the best offset in case the
     reference has a pickup); segments that collapse onto one grid line merge.
   - Energy 1 to 10 per section from the service's per-section loudness.
   - Which parts play per section from Demucs stems (drums, bass, other, vocals), and inside
     the drums stem the lows (kick) and highs (hats and tops) of a few seconds per section.
   - Dance names from labels + energy + parts: Intro, Groove, Breakdown, Build, Drop, Outro.
   - Saved as a YAML template in a user folder, so `preview_template`, `build_skeleton`,
     `arrange_session` (SPEC 4.3's "one request") all take it as a genre.
2. **`mark_sections`**: any template (reference or genre) → locators, optionally empty
   scenes and a "Sections" track of named blocks, in the current set. Dry run by default:
   it shows the plan and the op list first.
3. **`compare_arrangement`** ("arrange like this"): the set's current Arrangement vs a template,
   section by section: which tracks to mute, unmute or add where, which parts are missing,
   where the reference has a breakdown the set doesn't, and section lengths that differ.
   `apply=True` mutes/unmutes the clips that sit wholly inside a section.

## Decisions

(filled in as I go)

## Status

- [ ] Plan
- [ ] `analyze_reference`
- [ ] `mark_sections`
- [ ] `compare_arrangement`
- [ ] Tests (mocked service and Live), opt-in integration test
- [ ] README / SPEC
- [ ] Self-review, draft PR

## Needs Bazil

(filled in at the end)
