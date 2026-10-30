# Phase 4 progress: arrange from a reference

Branch `phase-4-arrange` (from `phase-3-1-hear`). Overnight unattended run on `pc`, 2026-10-29.

## Plan

Three tools, all on top of the Phase 1 template format so the existing tools keep working:

1. **`analyze_reference`** (SPEC 4.1 + 4.2): a reference track → an arrangement template.
2. **`mark_sections`**: any template → locators, optionally empty scenes and a "Sections"
   track of named blocks in the current set. Dry run by default (plan first).
3. **`compare_arrangement`** ("arrange like this"): the set's Arrangement vs a template,
   section by section, with concrete changes; `apply=True` does the mutes and unmutes.

SPEC 4.3's "one request arranges the loops like the reference" is the existing
`arrange_session` with the generated template: `arrange_session("ref_<name>", "reference")`.

## What the service gives (read from `sections-allin1`, not edited)

`POST /v1/sections` (JSON by hash or multipart, like the other routes): `bpm`,
`bpm_rounded`, `steadiness`, `beats`, `downbeats` (seconds), `beat_positions`, `segments`
[{start, end, label, loudness_lufs, energy}], `method`, `device`, `partial` when loudness
failed. Labels are Harmonix's (start, intro, verse, chorus, bridge, break, inst, solo,
outro, end); no "drop". `/health` features list `sections` once deployed (v0.2.0). The
client call (`AudioService.sections`) already existed from Phase 3.1.

## How a reference becomes a template (`reference.py`)

1. **Bars by downbeat.** Each boundary's bar is its position among allin1's downbeats
   (interpolated inside a bar, the median bar length past either end), not seconds divided
   by a tempo, so a drifting or live-played reference still lands on bars.
2. **Phrase snapping** to `phrase_bars` (8 by default; 1, 2, 4, 16 allowed). The grid
   offset stays 0 unless another offset puts at least two inner boundaries on the grid (and
   more than offset 0 does) while moving the start by at most 2 bars: that's a pickup bar.
   One odd boundary is treated as an odd section (with a warning), not a pickup. Segments
   that collapse onto one grid line merge into the neighbour; each final section takes the
   label with most overlap and a duration-weighted power mean of the loudness. Boundaries
   moved more than a quarter phrase get a "check it by ear" warning.
3. **Energy 1 to 10** like the genre templates: 10 for the loudest section, one step per 2 dB
   below it (from `loudness_lufs`; `energy` as a fallback).
4. **Parts per section from stems** (Demucs htdemucs, WAV): a 0.5 s mean-square envelope of
   each stem (every 7th sample, read in chunks, standard library only). A stem is "in" when
   it's within 15 dB of its own loudest section and within 35 dB of the loudest stem. Inside
   the drums stem, a 4 s excerpt per section goes through Phase 3's band split: kick when
   the lows are within 12 dB of their peak section, hats and tops when the highs are within
   15 dB. Parts: kick, hats, bass, music ("Chords and synths" = Demucs's other), vocal.
5. **Names**: leading intro-labelled sections are Intro (A, B...), trailing outro ones Outro;
   a Drop has the kick and is energy 9+, or 7+ after a jump of 3; no kick (or quiet, without
   stems) is a Breakdown; a short section between a Breakdown and a Drop is the Build; the
   rest is Groove. Repeats are numbered (Drop 1, Drop 2).
6. **Transitions**: a riser over the last 8 bars before a rise of 3+, a crash on it, a
   downlifter into a fall of 3+.
7. **Saved** as `~/.config/ableton-helper/templates/ref_<name>.yaml` (env
   `ABLETON_HELPER_TEMPLATES`), with a `reference:` block (file name, hash, raw segments,
   offset, where parts came from). Tempo from the `bpm` feature (the service README says it's
   the better tempo), else allin1's; 124.02 becomes 124.

## Decisions

- **No new dependencies**, as in 3.1: stems are read with `wave` + `array`.
- **Role set for references is fixed at five** (kick, hats, bass, music, vocal) because four
  stems can't tell chords from a lead. Their kinds and fallbacks are matching.py's, so the
  producer's loops find their way in (`music` falls back to pad, then lead; `vocal` to lead).
- **Templates load from the user folder too**, after the built-ins, keyed by file version
  (mtime + size), so a hand-edited or regenerated YAML is used without restarting the MCP
  server. Template names must be `[a-z0-9_]+` (no paths). A reference can't take a built-in
  genre's name; re-analysing under the same name replaces the file and says so.
- **If stems fail**, the parts are the usual ones per kind of section (and the answer says
  so); the PC being off is still an error.
- **mark_sections is a dry run by default** and returns a plan table plus the op list.
  Existing locators at section bars are renamed; others are kept unless
  `replace_locators=True`. Scenes are appended after the set's own. Re-running with
  `section_track` reuses the "Sections" track and rewrites only its blocks.
- **compare_arrangement** recognises tracks by track + clip names (matching.py's keywords);
  unclear ones go to `not_compared`, kinds the template doesn't use (FX) to
  `not_in_template`, transition lanes and the Sections track are skipped. A track "plays" a
  section when unmuted clips cover half of it. The template starts at the locator named
  like its first section, else bar 1, or `start_bar`. Suggestions: mute, unmute (clips there
  but switched off), add (from its Session slot), missing parts, breakdowns or lifts the set
  doesn't follow, locators that sit elsewhere, a much longer or shorter arrangement.
- **Applying** only mutes/unmutes, and only clips wholly inside a range: the Live API
  can't split clips, and muting a long clip would silence bars outside the section. The
  rest are reported under `needs_a_split` (split with Cmd+E, run again). Adding loops stays
  a suggestion (arrange_session rebuilds properly).
- **Remote Script 0.3.0**: `create_scene`, `set_clips_muted`, and clip `muted` in the
  snapshot. Locators work with older scripts; scenes and `apply` check the version first
  and say to reinstall.
- No vault note (tonight's rules).

## Environment notes (this run)

- Still no `uv`, and `pip install` (even into a scratch venv) needs approval that can't be
  given unattended. Tests ran with a pytest stand-in and a FastMCP stub in
  `/data/tmp/overnight-2026-10-29/scratch/f-ableton-p4/stub/` (not committed).
- The live service on pc is v0.1.0 (no `sections`), and no dev copy was running on 8771.
  I didn't start the audio-analysis dev compose: it's another repo, and its image build
  is heavy on a shared GPU night.

## Status

- [x] Plan
- [x] `analyze_reference` (+ user templates in `templates.py`)
- [x] `mark_sections`
- [x] `compare_arrangement` (+ `apply`)
- [x] Remote Script 0.3.0 ops, tested against the fake
- [x] Tests: `test_reference.py` (27), `test_compare.py` (13) on a fake service and set;
      all 117 pass with the stand-in (77 before this branch), 4 skipped (opt-in)
- [x] Opt-in `test_reference_live.py`: the stems test **passed against the real service**
      on a generated 48-bar track. Real Demucs heard every section's kick, hats, bass and
      music right; the "vocal" (a plain 440 Hz sine) went to Demucs's other stem, as it
      should, so the test leaves vocals out. The full sections test skips until `/health`
      lists `sections`.
- [x] README / SPEC
- [x] Self-review of `git diff origin/phase-3-1-hear...HEAD`: fixed templates cached until a
      restart, a 124.02 BPM tempo, silent overwrite of a same-named template, `../` in a
      template name
- [x] Independent review pass (below)
- [x] Draft PR: #2 "Phase 4: arrange from a reference track" (base `phase-3-1-hear`)

## Review pass

A fresh read of the whole branch diff, each finding reproduced with a script, then a
failing test, then fixed:

1. **Sections vanished on half-way boundaries.** Python's `round()` sends halves to even,
   so 4-bar sections at bars 4, 12, 20... snapped alternately down and up and collapsed
   (two choruses lost). Halves now round up, and two boundaries landing on one grid line
   that are at least half a phrase apart push the later one to the next line.
2. **Half or double tempo was taken as is.** The bars come from allin1's downbeats but the
   tempo from the bpm feature; a 62 BPM reading for a 124 BPM track is now doubled to match
   the downbeats (and the answer says so).
3. **Fallback parts were muted.** A Perc track standing in for hats, or a lead for "chords
   and synths", got "mute" everywhere. Sections are now judged per wanted role: any kind the
   role takes (its own or a fallback) belongs there.
4. **"Add" for a part another track already plays.** Two bass tracks taking turns each got
   "add" in the other's sections. A role is satisfied when any of its tracks plays.
5. **No energy and no stems made every middle section a Breakdown.** Without energy,
   sections are now named from allin1's label (chorus = Drop, break or bridge = Breakdown,
   else Groove).
6. **`moved_from_bar` could name a swallowed boundary** (display only). It now uses the last
   raw boundary that landed on the section's start.
7. Edge: all-silent stems marked every part as playing everywhere; they now count as failed
   stems (the usual parts, with a warning).

Checked and fine per the review: bars vs beats, mute ranges, locator deletion, op order
around compile_plan's reset and show, pairing ops with results in `apply`, built-in genres
still loading.

Still assumed: 4/4 (the downbeat tempo is 4 beats per bar).

## Try it

On the Mac:

```sh
git fetch && git checkout phase-4-arrange
uv sync
uv run pytest                                                         # everything, mocked
ABLETON_HELPER_LIVE_AUDIO=1 uv run pytest tests/test_reference_live.py   # real service
uv run ableton-helper install                                         # Remote Script 0.3.0, restart Live
```

Once the service has `/v1/sections`, in Claude Code: "Make a template from
~/Music/reference.mp3", "Put its sections in my set as locators and scenes", "Arrange my
loops like it", "Compare my arrangement with it; what should I mute where?"

## Needs Bazil

1. Deploy audio-analysis `sections-allin1` (v0.2.0) so `/v1/sections` is live; until then
   `analyze_reference` says sections aren't available. Then run
   `ABLETON_HELPER_LIVE_AUDIO=1 uv run pytest tests/test_reference_live.py`.
2. Run `uv run pytest` on the Mac once: real pytest and FastMCP haven't seen these tools.
3. SPEC 4.1's acceptance (3 known house tracks, boundaries within 1 bar) needs real tracks:
   try `analyze_reference` on three you know and tell me where the sections or parts are off.
   The thresholds were tuned on generated audio.
4. Reinstall the Remote Script (`uv run ableton-helper install`) for scenes and
   `compare_arrangement(apply=True)`.
5. Check in Live that setting `Clip.muted` on an arrangement clip behaves like the clip
   activator (it does in the API docs; not tried in Live tonight).

## Decisions applied 2026-10-30

Overnight run on `pc` (worktree `ov/ableton-p4`, pushed to `phase-4-arrange`).

- **Merged `origin/phase-3-1-hear`** (the spectrogram test fix for the real FastMCP, and the
  3.1 notes). It merged cleanly with no conflicts.
- **Decision 2A: Ableton Helper maps allin1's labels** (commit "Decision 2A: ..."). The
  service is unchanged.
  - `reference.SECTION_LABELS`, a documented table: intro → Intro, verse → Groove,
    chorus → Drop, inst → Drop, solo → Groove, break → Breakdown, bridge → Breakdown,
    outro → Outro. Any other label (or a new one from a future allin1) falls back to Groove
    (`FALLBACK_KIND`). `label_kind()` looks up a label (any case).
  - It's a hint, and energy and the kick have the last word. The old rule still holds
    (a drop needs the kick and energy 9+, or 7+ after a jump of 3). On top of that, a
    section whose label says Drop counts as a drop at 8+ when it comes straight after a
    breakdown (or the breakdown and its build). So an "inst" groove before the first
    breakdown stays a Groove (the fixtures' 48-bar track still names Intro, Groove,
    Breakdown, Drop, Outro), and an "inst" without the kick is a Breakdown. With no energy at
    all, the table's kind is used directly (inst is now a Drop there too, as chorus was).
  - **Template override**: any template can carry `section_labels: {inst: Groove, solo: Build}`.
    Keys are allin1 labels and values are one of Intro, Groove, Breakdown, Build, Drop or
    Outro, in any case. They are checked when the template loads, so a typo is an error
    rather than ignored. `analyze_reference` takes them from `labels_from=<template>` when
    given, else from the `ref_<name>` template it is about to replace, so editing the saved
    YAML and analysing again sticks. Generated templates write the full table out so it's
    there to edit. The answer has `labels_from` ("the default table" or "<template>'s
    section_labels") and warns when `labels_from` has none.
  - The intro/outro detection and the breakdown rule go through the same table, so an
    override like `bridge: Groove` really stops bridges being breakdowns.
  - Tests (7 new in `tests/test_reference.py`): the table and its fallback, inst/chorus vs
    verse after a breakdown, no kick, no energy, overrides, template validation, and the
    end-to-end edit-and-re-analyse loop with `labels_from`.
  - README's `analyze_reference` row explains it.
- **Decision 3A (sentence case everywhere)**: no change needed here. The package has no UI
  styling, and a scan of user-facing strings found no all-caps letter-spaced labels. New
  copy is sentence case.

Calls I made:
- I kept the table in Ableton Helper's `reference.py` rather than `templates.py`. The kind
  list is in `templates.py` (`SECTION_KINDS`) because templates validate against it.
- "solo" → Groove rather than Drop: it's rare in dance music, and energy promotes it anyway
  when it's loud.
- The "after a breakdown" condition is there because both test tracks (and real house
  tracks) label grooves "inst". Without it, every energy-8 groove became a Drop.

Checks run: `uv sync && uv run pytest`: **125 passed, 4 skipped** (the opt-in live tests),
with real pytest and FastMCP for the first time on this branch (118 before the decision
work). Self-review of `git diff origin/phase-3-1-hear...HEAD` for tonight's changes found
no bugs.

Left: the label table and the 8-after-a-breakdown rule are tuned on made-up sections.
SPEC 4.1's three real house tracks will tell whether allin1 labels drops "chorus" or "inst"
as often as expected.

### Needs Bazil (2026-10-30)

1. Roll out `sections-allin1` on the service (morning decision 1), then run
   `ABLETON_HELPER_LIVE_AUDIO=1 uv run pytest tests/test_reference_live.py`.
2. Try `analyze_reference` on three house tracks you know. If the drops are named wrong, say
   which allin1 label they had (it's in each section row as `label`). A
   `section_labels` edit may fix it without code.
3. Item 2 above (real pytest) is done on pc. Items 4 and 5 (reinstall the Remote Script,
   `Clip.muted` in Live) still stand.
