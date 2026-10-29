# Ableton Helper

An AI arrangement assistant for Ableton Live 12. You make the loops; Claude turns them into a full, genre-shaped arrangement in Arrangement View — in one batch, in seconds.

![A 224-bar tech house arrangement built from the template: one lane per part, locators at each section, and lanes for risers, impacts and fills](docs/arrangement.png)

See [SPEC.md](SPEC.md) for the full plan (Max for Live chat window, audio analysis, reference-track arranging).

## What it does

- **Skeletons:** "Build me a house skeleton" → one MIDI track per part (Kick, Hats, Bass, Chords, Hook…), named and coloured placeholder clips for every section, locators at each section, and tracks of placeholders for risers, impacts and fills.
- **Arrange your loops:** put your loops in Session View, then "arrange these as tech house". It works out which track is the kick, bass, chords and so on from the names, tiles each loop across the right sections, drops parts out before drops, and adds "(to do)" placeholder tracks for parts you haven't made yet.
- **Custom arrangements:** Claude can describe any arrangement as a plan and build it in one call.
- **Hearing:** Claude can listen to the audio clips in your set through the audio analysis service on the home PC: BPM, key, loudness and energy, a spectrogram it can look at, stems, audio to MIDI (dropped into a new MIDI clip if you like), and what a badly named loop is ("Audio 7" sounds like a bassline).

Genres: house (extended, radio), tech house, deep house, techno, drum & bass, UK garage. Templates are YAML files in [`src/ableton_helper/templates/`](src/ableton_helper/templates/), so you can edit them or add your own.

## Setup

Needs [uv](https://docs.astral.sh/uv/) and Ableton Live 12.

```sh
uv sync
uv run ableton-helper install      # copies the Remote Script into your User Library
```

Then in Live: **Preferences → Link, Tempo & MIDI → Control Surface**, pick **AbletonHelper** in a free slot (Input/Output: None). Live shows "AbletonHelper: listening on port 9878" in the status bar.

```sh
uv run ableton-helper status       # checks the connection
```

Claude Code picks up the MCP server from [`.mcp.json`](.mcp.json) when you open this folder. For Claude Desktop, add the same entry to its `claude_desktop_config.json`.

## Try it

In Claude Code, with Live open:

- "Show me the house template" (works without Live)
- "Build a tech house skeleton at 127"
- "Look at my session and arrange it as house. Ask me about anything you can't work out."
- "Add a riser from bar 145 for 16 bars"

Everything a build does is one batch. If you don't like it, undo in Live.

With the home PC on (Tailscale):

- "What key and tempo is the loop on track 4?"
- "Show me the spectrogram of the vocal clip"
- "What are my Audio tracks? Rename them so you can arrange them"
- "Turn the bassline on track 2 into MIDI and put it on a new track"
- "Split the loop in slot 1 of track 6 into stems"

## Hearing tools

| Tool | What it does |
|---|---|
| `analyze_audio` | BPM (with confidence and half/double alternatives), key with Camelot, loudness in LUFS, an energy curve and level, optional genre and mood tags |
| `audio_spectrogram` | A mel spectrogram image Claude looks at, plus its path |
| `separate_stems` | Demucs stems (drums, bass, other, vocals; `htdemucs_6s` adds guitar and piano) downloaded to local files |
| `audio_to_midi` | basic-pitch MIDI: the `.mid` path and a note summary. `add_to_live` puts the notes on a new MIDI track, in a Session clip or at a bar in the Arrangement |
| `tag_loop_roles` | Kick, clap, hats, perc, drums, bass, chords, pad, lead, vocal or fx for each loop, with a confidence and the reason. `rename_tracks` renames unclear tracks ("Bass (Audio 7)") so `match_roles` can place them |
| `detect_sections` | Song sections for a reference track (intro, chorus, break...), in seconds and bars, once the service offers them |

Pick audio as `track` + `slot` (a Session clip), `track` + `bar` (the Arrangement clip at that bar), `track` alone (every audio file on it) or `path`. The files are the ones Live's clips point at; MIDI clips have no audio to analyse yet.

The service is the audio-analysis project (`~/Programming/audio-analysis`) at `https://pc.taila3582d.ts.net:8770` (set `AUDIO_ANALYSIS_URL` to change it). Files go by SHA-256: a file the service has seen isn't uploaded again, and results are cached on this machine under `~/.cache/ableton-helper/audio` (`ABLETON_HELPER_CACHE`). If the PC is off, the tools say so and everything else keeps working.

`audio_to_midi` into a Session clip needs Remote Script 0.2.0: run `uv run ableton-helper install` again and restart Live.

## CLI

```sh
uv run ableton-helper templates             # list genres
uv run ableton-helper preview techno        # print a template's arrangement
uv run ableton-helper bench                 # time one-request-per-change vs batch (use an empty set)
```

## How it works

```
Claude ──MCP──> ableton-helper-mcp (Python)          Ableton Live
                  templates → plan → ops ──TCP 9878──> AbletonHelper Remote Script
                                                        runs the whole op list in one main-thread tick
```

- `remote_script/AbletonHelper/` runs inside Live. `lom.py` holds all Live API code and doesn't import Live, so it's tested against a fake (`tests/fake_live.py`).
- `src/ableton_helper/templates.py` compiles genre templates into bar ranges, gaps and transition placeholders.
- `src/ableton_helper/matching.py` guesses which track plays which part from track and clip names.
- `src/ableton_helper/plan.py` turns a plan in bars into primitive ops: tiling loops, rebuilding partial MIDI tails, locators, clearing.
- `src/ableton_helper/server.py` holds the MCP tools.
- `src/ableton_helper/audio_service.py` is the analysis service client (standard library only); `hearing.py` finds the audio behind clips and shapes the answers; `roles.py` tags loops by role from stems, pitch and key.

Why a batch: [ahujasid/ableton-mcp](https://github.com/ahujasid/ableton-mcp) (which this borrows from; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)) sends one request per change, and every request waits for Live's next main-thread tick. It's also one Claude tool call per change, which is the slow part. Building a 224-bar house skeleton takes about 108 operations: 108 tool calls there, one here. `uv run ableton-helper bench` measures the Live side on your machine and writes the results to `docs/benchmarks.md`.

## Tests

```sh
uv run pytest
ABLETON_HELPER_LIVE_AUDIO=1 uv run pytest tests/test_audio_service_live.py   # the real service, generated sounds
```

## Limits

- Hearing needs the home PC. Without it, name your tracks and clips well ("Bass – rolling sub", not "Audio 7").
- Hearing covers audio clips only, and the whole file a clip points at (not its warp or loop region). MIDI and instrument tracks need rendering first (SPEC 3.2).
- Role tagging was tuned on synthetic sounds; check its answers on real loops, especially low-confidence ones.
- Audio loops that don't divide a section evenly leave a short empty gap at the end of that section. The Live API can't trim audio clips; MIDI loops are rebuilt exactly.
- Parts can't be cut out of an existing arrangement clip after the fact (the API can't split clips). Gaps are applied when building.
