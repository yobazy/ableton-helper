# Phase 3.1 progress: let the AI hear

Branch `phase-3-1-hear` (from `master`). Overnight unattended run on `pc`, 2026-10-28.

## Plan

### Where the audio is

- Live's `file_path` for every audio clip is already in the snapshot (`lom._clip_info`), for
  Session clips (`track`, `slot`) and Arrangement clips (`track`, `start`/`end` in beats).
- The MCP server runs on the same Mac as Live, so those paths are readable locally. Nothing
  new is needed in the Remote Script to find audio.
- MIDI clips have no audio file. The tools say so. Rendering MIDI tracks to audio is SPEC 3.2
  (a Max for Live capture device), not this phase.
- A clip is addressed as `track` + `slot` (Session), `track` + `bar` (the Arrangement clip
  covering that bar), `track` alone (every distinct audio file on the track), or a plain `path`.

### Getting files to the service

The Audio Analysis Service (`https://pc.taila3582d.ts.net:8770`, see
`~/Programming/audio-analysis/README.md`) keys everything by the file's SHA-256:

1. Hash the file locally (memoised by path + size + mtime).
2. `POST` JSON `{"audio": sha, ...}`. A `200` answers from the service's cache, no upload.
3. On `404 audio_not_found`, send the same request as `multipart/form-data` with the file as
   `file` and the other fields as form values. One extra round trip, no separate `/v1/audio`.

### Local cache

`~/.cache/ableton-helper/audio/<sha[:2]>/<sha>/` (override with `ABLETON_HELPER_CACHE`): one
JSON per analyze feature, spectrogram PNGs per option set, downloaded stems, the `.mid` and
notes from transcription, sections. Errors and partial answers are never cached; nor are CLAP
embeddings (see decisions).

### Tools

| Tool | What it returns |
|---|---|
| `analyze_audio` | Per file: duration, BPM (+ confidence, alternatives, length in bars), key (+ Camelot, confidence, strength), loudness (integrated LUFS, range, peak), energy (curve squashed to at most 32 points, movement in LU, a coarse low/medium/high), optional genre/mood tags |
| `audio_spectrogram` | The mel spectrogram PNG as an image Claude can look at, plus its cached path |
| `separate_stems` | Local paths of the Demucs stems (downloaded into the cache) |
| `audio_to_midi` | The `.mid` path, tempo used and a note summary; `add_to_live=True` makes a new MIDI track after the source with the notes in a Session clip, or in the Arrangement at `place_at_bar` |
| `tag_loop_roles` | For each audio clip: role (kick, clap, hats, perc, drums, bass, chords, pad, lead, vocal, fx), confidence, level and the reason; `rename_tracks` renames unclear tracks ("Bass (Audio 7)") so `match_roles` picks them up |
| `detect_sections` | `/v1/sections` segments in seconds and bars from the first downbeat; a plain "not available yet" while the service lacks it |

Code: `audio_service.py` (client), `hearing.py` (finding audio, shaping answers, MIDI ops),
`roles.py` (role tagging), thin wrappers in `server.py`. One new Remote Script op in `lom.py`.

## Decisions

- **No new dependencies.** `urllib` for HTTP with hand-written multipart; `wave` for reading
  stems. Files are read into memory for upload (loops are small; the service caps at 300 MB).
- **Timeouts:** health 4 s, analyze 180 s, spectrogram 90 s, transcribe 180 s, stems 900 s
  (Demucs falls back to the CPU when the GPU is busy), downloads 120 s.
- **CLAP is broken on the service today**, so role tagging doesn't depend on it. Measured
  tonight: cosine 0.999 between unrelated text prompts and 0.997 between a synthetic kick and
  hats. Likely cause in `audio-analysis/app/features/clap.py`: `_normalise` takes
  `pooler_output`, which under transformers 5 looks like the unprojected pooled state, not the
  projected embedding. Not edited (another session owns that repo tonight).
  `tag_loop_roles` checks CLAP health on every call (fresh, never cached) and blends CLAP in
  automatically once it's fixed. Audio `embedding` results aren't cached locally so a fix on
  the service shows up straight away.
- **Role tagging evidence:** Demucs stem shares (drums / bass / other / vocals), the drums
  stem's energy below 150 Hz and above 4 kHz (24 dB/octave Butterworth, pure Python on the
  first 12 s) and hits per beat, basic-pitch polyphony / note length / pitch, key strength,
  loudness movement (risers), and names as a 1.6x nudge. Kick vs. full kit is judged by
  whether there are tops at all in dB (a kick swamps the energy split) plus hits per beat.
  Tuned on six synthetic loops (kick, hats, bass, pad, lead, kick + hats): all six right
  against the real service, confidences 0.29 (hats) to 1.0 (bass). **Not yet tried on real
  loops.**
- **Audio to MIDI timing:** basic-pitch gives seconds; notes become beats at the file's
  detected BPM (Live warps loops to the set tempo, so the file's beats are what count), the
  set's tempo if detection is unsure (confidence below 0.5), or `bpm` if given. Starts and
  lengths snap to 1/16 by default (`quantize=0` turns it off). A Session clip's length is the
  source clip's length; for a plain file it's the notes rounded up to whole bars.
- **Dropping MIDI into Live:** the Remote Script could only create Arrangement MIDI clips. I
  added `create_session_midi_clip` (`ClipSlot.create_clip` + notes) to `lom.py`, tested against
  the fake, and bumped the script to 0.2.0. The tool pings first and, with an older script,
  says to reinstall or use `place_at_bar` (which works with 0.1.0) instead of leaving an
  empty track.
- **Whole files:** a clip's warp markers and loop region aren't in the snapshot, so the tools
  analyse the whole file the clip points at.
- **Stems default to FLAC** for `separate_stems` (half the download; Live 12 reads it). Role
  tagging asks for WAV because it reads them with `wave`.
- **Sections:** the deployed service is v0.1.0 (`/health` features: bpm, key, tags,
  embedding, loudness; `/v1/sections` is 404). The tool checks `/health` first and also treats
  a bare 404 from the route as "not yet". The response shape follows the audio-analysis README.
- No Obsidian vault note: tonight's rules keep me inside this repo.

## Environment notes (this run)

- `uv` isn't on `pc`, and `pip install` / `docker run` needed approval that can't be given
  unattended, so `uv run pytest` couldn't run here.
- Instead I ran every test file with a small pytest stand-in plus a stub of FastMCP, both in
  the git-ignored `.scratch/` folder. That covers `tests/test_pipeline.py` too, so `server.py`
  imports and the existing 19 tests pass with the new code. The stub can't prove the real
  FastMCP accepts the tools' signatures or the spectrogram's `[text, Image]` return; that
  needs one real run.

## Status

- [x] Plan
- [x] Client (`audio_service.py`)
- [x] `analyze_audio`
- [x] `audio_spectrogram`
- [x] `separate_stems`
- [x] `audio_to_midi` (+ Session or Arrangement clip in Live)
- [x] `tag_loop_roles`
- [x] Tests: `test_hearing.py` (32), `test_roles.py` (14), `test_hearing_tools.py` (9) on a
      fake service; `test_audio_service_live.py` (2, opt-in) passed against the real service
- [x] README / SPEC
- [x] `detect_sections` stub (optional)
- [x] Self-review of `git diff master...HEAD`: fixed stale-CLAP caching, a "Track None"
      error message, `slot`/`bar` without `track` being ignored, negative `quantize`
- [x] Draft PR

Left for later: hearing-aware `match_roles` (use tags directly instead of renaming), real
loop calibration, analysing only a clip's loop region, results cache keyed by service version.

## Try it

On the Mac:

```sh
git fetch && git checkout phase-3-1-hear
uv sync
uv run pytest                                                          # everything, mocked service
ABLETON_HELPER_LIVE_AUDIO=1 uv run pytest tests/test_audio_service_live.py   # real service, generated sounds
uv run ableton-helper install                                          # Remote Script 0.2.0, then restart Live
```

Then in Claude Code with a set open: "What are my Audio tracks?", "Show me the spectrogram of
track 3 slot 1", "Turn the bassline on track 2 into MIDI and put it on a new track".

## Needs Bazil

1. Run `uv run pytest` on the Mac once: it couldn't run here (no uv, installs blocked). The
   same tests passed with stand-ins, but the real FastMCP hasn't seen the new tools.
2. Fix CLAP in audio-analysis (`clap.py` `_normalise` likely uses the unprojected
   `pooler_output`) and bump its `clap.1` VERSION so the service's cached vectors refresh.
   `tag_loop_roles` will pick CLAP up on its own.
3. Try `tag_loop_roles` on a few real loops and tell me where it's wrong; it was tuned on
   synthetic sounds.
4. Reinstall the Remote Script (`uv run ableton-helper install`) for MIDI into Session clips.
