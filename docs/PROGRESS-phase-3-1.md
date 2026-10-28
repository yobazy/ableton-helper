# Phase 3.1 progress: let the AI hear

Branch `phase-3-1-hear` (from `master`). Overnight unattended run on `pc`, 2026-10-28.

## Plan

### Where the audio is

- Live's `file_path` for every audio clip is already in the snapshot (`lom._clip_info`), for
  Session clips (`track`, `slot`) and Arrangement clips (`track`, `start`/`end` in beats).
- The MCP server runs on the same Mac as Live, so those paths are readable locally. Nothing
  new is needed in the Remote Script to find audio.
- MIDI clips have no audio file. The tools say so and point at stems/MIDI tools only for
  audio clips. Rendering MIDI tracks to audio is SPEC 3.2 (a Max for Live capture device), not
  this phase.
- A clip is addressed as `track` + `slot` (Session), `track` + `bar` (the Arrangement clip
  covering that bar), `track` alone (every distinct audio file on the track), or a plain `path`.

### Getting files to the service

The Audio Analysis Service (`https://pc.taila3582d.ts.net:8770`, see
`~/Programming/audio-analysis/README.md`) keys everything by the file's SHA-256:

1. Hash the file locally (memoised by path + size + mtime).
2. `POST` JSON `{"audio": sha, ...}`. A `200` answers from the service's cache, no upload.
3. On `404 audio_not_found`, send the same request as `multipart/form-data` with the file as
   `file` and the other fields as form values. One round trip, no separate `/v1/audio` call.

### Local cache

`~/.cache/ableton-helper/audio/<sha[:2]>/<sha>/` (override with `ABLETON_HELPER_CACHE`):
one JSON per analyze feature, spectrogram PNGs per option set, downloaded stems, the `.mid`
and notes from transcription. Errors and partial answers are never cached.

### Client (`src/ableton_helper/audio_service.py`)

Standard library only (`urllib`), so the MCP server gains no dependencies. Timeouts per call
(health 4 s, analyze 180 s, spectrogram 90 s, transcribe 180 s, stems 900 s because Demucs can
fall back to the CPU). An unreachable `pc` raises `AudioServiceUnavailable` with a plain
message; service errors raise `AudioServiceError` with the service's `error` code.

### Tools

| Tool | What it returns |
|---|---|
| `analyze_audio` | Per file: duration, BPM (+ confidence), key (+ Camelot), loudness (integrated LUFS, range, peak), an energy summary (curve squashed to at most 32 points, plus a coarse low/medium/high), optional genre/mood tags |
| `audio_spectrogram` | The mel spectrogram PNG as an image Claude can look at, plus its cached path |
| `separate_stems` | Local paths of the Demucs stems (downloaded into the cache) |
| `audio_to_midi` | The `.mid` path and a note summary; with `add_to_live=True` a new MIDI track with the notes in a Session clip (or in the Arrangement at `bar`) |
| `tag_loop_roles` | For each audio clip: role (kick, bass, hats, pad, vocal, fx...), confidence and the reason, from CLAP audio/text similarity plus pitch range and key strength; optional renaming of badly named tracks so `match_roles` picks them up |
| `detect_sections` | Optional stub: `/v1/sections` segments in seconds and bars, skipped with a clear message when the service doesn't have it yet |

The tool logic lives in `src/ableton_helper/hearing.py`, which doesn't import `mcp`, so it's
testable on its own; `server.py` only wraps it.

## Decisions

- **No new dependencies.** `urllib` for HTTP and hand-written multipart. Loops are small;
  files are read into memory for upload (the service caps uploads at 300 MB anyway).
- **Role matching uses CLAP zero-shot**, not hand-made spectral rules. The service already has
  `embedding` for audio and `/v1/embed/text` for text in the same space (checked tonight: it
  answers on the deployed v0.1.0). Each role has a few text prompts; the clip's similarity to
  each role becomes a softmax confidence. Pitch range from `/v1/transcribe` and key strength
  nudge bass vs. lead vs. drums. Track and clip names, when they mean something, are a weak
  prior. The `reason` string says which of these decided it.
- **Audio to MIDI timing:** basic-pitch gives seconds. Notes are converted to beats with the
  file's detected BPM (Live warps loops to the set tempo, so beats in the file are what count),
  falling back to the set tempo; `bpm` overrides. Starts are quantised to 1/16 by default
  (`quantize=0` turns it off).
- **Dropping MIDI into Live:** the Remote Script could only create Arrangement MIDI clips. I
  added one op, `create_session_midi_clip` (`ClipSlot.create_clip` + notes), in `lom.py`, tested
  against the fake. It needs the Remote Script reinstalled (`uv run ableton-helper install`).
  Without that, `bar=` puts the clip in the Arrangement with the existing op.
- **Whole files:** an audio clip's warp markers and loop region aren't in the snapshot, so the
  tools analyse the whole file the clip points at. Usually that is the loop.

## Environment notes (this run)

- `uv` isn't on `pc`, and `pip install` / `docker run` needed an approval that can't be given
  unattended. So the full suite (`uv run pytest`) couldn't run here: the existing
  `tests/test_pipeline.py` imports `mcp`, which isn't installed system-wide.
- What I could run: the new tests (`tests/test_hearing.py`), which don't import `mcp`, through a
  tiny pytest stand-in kept in the git-ignored `.scratch/` folder. See "Try it".

## Status

- [x] Plan (this file)
- [ ] Client
- [ ] analyze_audio
- [ ] audio_spectrogram
- [ ] separate_stems
- [ ] audio_to_midi (+ drop into Live)
- [ ] tag_loop_roles
- [ ] Tests (mocked service + opt-in integration)
- [ ] README / SPEC
- [ ] detect_sections stub (optional)
- [ ] Self-review, draft PR

## Needs Bazil

1. Run `uv run pytest` on the Mac (couldn't run the `mcp`-importing tests here).
