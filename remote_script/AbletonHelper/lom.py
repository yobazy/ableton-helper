"""Live Object Model operations for the AbletonHelper Remote Script.

Everything here takes a ``song`` object and plain data, and never imports
Live or _Framework, so it can be unit-tested against fakes (tests/fake_live.py).
The Remote Script (__init__.py) calls these on Live's main thread.

Times are in beats throughout. The MCP server converts bars to beats.
"""

from __future__ import absolute_import, print_function, unicode_literals

EPSILON = 1e-4


# ── Snapshot ──────────────────────────────────────────────────────────────────

def _attr(obj, name, cast=None, default=None):
    try:
        value = getattr(obj, name)
    except Exception:
        return default
    if cast is None:
        return value
    try:
        return cast(value)
    except Exception:
        return default


def read_notes(clip, start=0.0, length=None):
    """Notes of a MIDI clip as dicts, relative to clip time 0."""
    if not _attr(clip, "is_midi_clip", bool, False):
        return []
    if length is None:
        length = float(clip.length)
    getter = getattr(clip, "get_notes_extended", None)
    notes = []
    if getter is not None:
        for n in getter(0, 128, float(start), float(length)):
            notes.append({
                "pitch": int(n.pitch),
                "start_time": float(n.start_time),
                "duration": float(n.duration),
                "velocity": float(n.velocity),
                "mute": bool(getattr(n, "mute", False)),
            })
        return notes
    for n in clip.get_notes(float(start), 0, float(length), 128):
        notes.append({
            "pitch": int(n[0]), "start_time": float(n[1]), "duration": float(n[2]),
            "velocity": float(n[3]), "mute": bool(n[4]) if len(n) > 4 else False,
        })
    return notes


def note_summary(notes):
    if not notes:
        return {"note_count": 0}
    pitches = [n["pitch"] for n in notes]
    return {
        "note_count": len(notes),
        "pitch_min": min(pitches),
        "pitch_max": max(pitches),
        "distinct_pitches": len(set(pitches)),
    }


def _clip_info(clip, include_notes):
    info = {
        "name": _attr(clip, "name", str, ""),
        "length": _attr(clip, "length", float, 0.0),
        "color": _attr(clip, "color", int, 0),
        "is_midi": _attr(clip, "is_midi_clip", bool, False),
        "is_audio": _attr(clip, "is_audio_clip", bool, False),
        "looping": _attr(clip, "looping", bool, False),
    }
    path = _attr(clip, "file_path", str, None)
    if path:
        info["file_path"] = path
    if info["is_midi"]:
        notes = read_notes(clip)
        info.update(note_summary(notes))
        if include_notes:
            info["notes"] = notes
    return info


def snapshot(song, include_notes=False, include_devices=True):
    """Everything an arranging assistant needs to know about the set, in one read."""
    tracks = []
    for ti, track in enumerate(song.tracks):
        session = []
        for si, slot in enumerate(track.clip_slots):
            if slot.has_clip:
                info = _clip_info(slot.clip, include_notes)
                info["slot"] = si
                session.append(info)
        arrangement = []
        for clip in _attr(track, "arrangement_clips", list, []):
            info = _clip_info(clip, False)
            info["start"] = _attr(clip, "start_time", float, 0.0)
            info["end"] = _attr(clip, "end_time", float, 0.0)
            arrangement.append(info)
        entry = {
            "index": ti,
            "name": _attr(track, "name", str, ""),
            "type": "midi" if _attr(track, "has_midi_input", bool, False) else "audio",
            "color": _attr(track, "color", int, 0),
            "mute": _attr(track, "mute", bool, False),
            "is_group": _attr(track, "is_foldable", bool, False),
            "session_clips": session,
            "arrangement_clips": arrangement,
        }
        if include_devices:
            entry["devices"] = [
                {"name": _attr(d, "name", str, ""), "class_name": _attr(d, "class_name", str, "")}
                for d in _attr(track, "devices", list, [])
            ]
        tracks.append(entry)
    return {
        "tempo": _attr(song, "tempo", float, 120.0),
        "signature": [_attr(song, "signature_numerator", int, 4),
                      _attr(song, "signature_denominator", int, 4)],
        "song_length": _attr(song, "song_length", float, 0.0),
        "is_playing": _attr(song, "is_playing", bool, False),
        "locators": [{"name": _attr(c, "name", str, ""), "time": _attr(c, "time", float, 0.0)}
                     for c in _attr(song, "cue_points", list, [])],
        "scenes": [_attr(s, "name", str, "") for s in _attr(song, "scenes", list, [])],
        "tracks": tracks,
    }


# ── Batch executor ────────────────────────────────────────────────────────────

class OpError(Exception):
    pass


class Executor(object):
    """Runs primitive ops against a song.

    Tracks can be referred to by index (int) or by a ``ref`` string bound by an
    earlier create_track/use_track op. Refs persist across calls until a
    ``reset`` op, so the same ops can run as one batch or one request at a time
    (the benchmark's "naive" mode).
    """

    def __init__(self, song, application=None, note_spec_factory=None):
        self.song = song
        self.application = application
        # Live.Clip.MidiNoteSpecification in Live 11+; None → legacy set_notes.
        self.note_spec_factory = note_spec_factory
        self.refs = {}

    # -- public --------------------------------------------------------------

    def run(self, ops, stop_on_error=False):
        results = []
        errors = []
        undo_open = False
        begin = getattr(self.song, "begin_undo_step", None)
        if begin is not None and len(ops) > 1:
            try:
                begin()
                undo_open = True
            except Exception:
                pass
        try:
            for i, op in enumerate(ops):
                try:
                    results.append(self.run_one(op))
                except Exception as e:
                    errors.append({"index": i, "op": op.get("op"), "error": str(e)})
                    results.append(None)
                    if stop_on_error:
                        break
        finally:
            if undo_open:
                try:
                    self.song.end_undo_step()
                except Exception:
                    pass
        return {"ops": len(ops), "ok": len(ops) - len(errors), "errors": errors,
                "results": results, "refs": dict(self.refs)}

    def run_one(self, op):
        kind = op.get("op")
        handler = getattr(self, "_op_" + str(kind), None)
        if handler is None:
            raise OpError("Unknown op: %s" % kind)
        return handler(op)

    # -- helpers -------------------------------------------------------------

    def _track_index(self, ref):
        if isinstance(ref, bool):
            raise OpError("Bad track reference: %r" % ref)
        if isinstance(ref, int):
            index = ref
        elif ref in self.refs:
            index = self.refs[ref]
        else:
            raise OpError("Unknown track ref: %r" % ref)
        if index < 0 or index >= len(self.song.tracks):
            raise OpError("Track index out of range: %d" % index)
        return index

    def _track(self, ref):
        return self.song.tracks[self._track_index(ref)]

    def _session_clip(self, track, slot):
        slots = track.clip_slots
        if slot < 0 or slot >= len(slots) or not slots[slot].has_clip:
            raise OpError("No session clip in slot %d on track '%s'" % (slot, track.name))
        return slots[slot].clip

    def _find_arrangement_clip(self, track, start):
        best = None
        for clip in track.arrangement_clips:
            if abs(float(clip.start_time) - start) < EPSILON:
                best = clip
        return best

    def _write_notes(self, clip, notes):
        if not notes:
            return 0
        if self.note_spec_factory is not None and hasattr(clip, "add_new_notes"):
            specs = tuple(
                self.note_spec_factory(
                    pitch=int(n["pitch"]),
                    start_time=float(n["start_time"]),
                    duration=float(n["duration"]),
                    velocity=float(n.get("velocity", 100)),
                    mute=bool(n.get("mute", False)),
                )
                for n in notes
            )
            clip.add_new_notes(specs)
        else:
            clip.set_notes(tuple(
                (int(n["pitch"]), float(n["start_time"]), float(n["duration"]),
                 float(n.get("velocity", 100)), bool(n.get("mute", False)))
                for n in notes
            ))
        return len(notes)

    @staticmethod
    def _slice_notes(notes, offset, length):
        """Notes starting within [offset, offset+length), shifted to 0 and clipped."""
        out = []
        end = offset + length
        for n in notes:
            s = n["start_time"]
            if s + EPSILON >= offset and s < end - EPSILON:
                m = dict(n)
                m["start_time"] = s - offset
                m["duration"] = min(n["duration"], end - s)
                out.append(m)
        return out

    # -- ops -----------------------------------------------------------------

    def _op_reset(self, op):
        self.refs = {}
        return {"reset": True}

    def _op_set_tempo(self, op):
        self.song.tempo = float(op["bpm"])
        return {"tempo": float(self.song.tempo)}

    def _op_create_track(self, op):
        kind = op.get("type", "midi")
        index = int(op.get("index", -1))
        if kind == "midi":
            self.song.create_midi_track(index)
        elif kind == "audio":
            self.song.create_audio_track(index)
        else:
            raise OpError("Track type must be midi or audio, got %r" % kind)
        new_index = len(self.song.tracks) - 1 if index < 0 else index
        # Existing refs at or after the insertion point shift right by one.
        if index >= 0:
            for key, value in self.refs.items():
                if value >= index:
                    self.refs[key] = value + 1
        track = self.song.tracks[new_index]
        if op.get("name"):
            track.name = str(op["name"])
        if op.get("color") is not None:
            track.color = int(op["color"])
        if op.get("ref"):
            self.refs[op["ref"]] = new_index
        return {"index": new_index, "name": track.name}

    def _op_use_track(self, op):
        index = self._track_index(int(op["index"]))
        self.refs[op["ref"]] = index
        track = self.song.tracks[index]
        if op.get("color") is not None:
            track.color = int(op["color"])
        return {"index": index, "name": track.name}

    def _op_clear_arrangement(self, op):
        track = self._track(op["track"])
        start = float(op.get("start", 0.0))
        end = op.get("end")
        end = float(end) if end is not None else float("inf")
        if not hasattr(track, "delete_clip"):
            raise OpError("Track.delete_clip is unavailable in this Live version")
        doomed = [c for c in track.arrangement_clips
                  if float(c.start_time) < end - EPSILON and float(c.end_time) > start + EPSILON]
        for clip in doomed:
            track.delete_clip(clip)
        return {"deleted": len(doomed)}

    def _op_create_locator(self, op):
        song = self.song
        target = float(op["time"])
        cue = None
        for c in song.cue_points:
            if abs(float(c.time) - target) < EPSILON:
                cue = c
        if cue is None:
            original = song.current_song_time
            song.current_song_time = target
            song.set_or_delete_cue()
            try:
                song.current_song_time = original
            except Exception:
                pass
            for c in song.cue_points:
                if abs(float(c.time) - target) < EPSILON:
                    cue = c
        if cue is None:
            raise OpError("Could not create locator at beat %s" % target)
        if op.get("name"):
            cue.name = str(op["name"])
        return {"time": float(cue.time), "name": cue.name}

    def _op_place_session_clip(self, op):
        """Copy one session clip onto the arrangement at ``time`` (one loop length)."""
        track = self._track(op["track"])
        clip = self._session_clip(track, int(op["slot"]))
        time = float(op["time"])
        track.duplicate_clip_to_arrangement(clip, time)
        placed = self._find_arrangement_clip(track, time)
        if placed is not None:
            if op.get("name"):
                placed.name = str(op["name"])
            if op.get("color") is not None:
                placed.color = int(op["color"])
        return {"time": time, "length": float(clip.length)}

    def _op_create_midi_clip(self, op):
        """Create an arrangement MIDI clip, optionally with notes.

        Notes come from ``notes`` (clip-relative dicts) or ``notes_from``:
        {"slot": n, "offset": beats} copies the matching slice of that track's
        session clip (used for partial tiles at the end of a section).
        """
        track = self._track(op["track"])
        start = float(op["start"])
        length = float(op["length"])
        if not hasattr(track, "create_midi_clip"):
            raise OpError("Track.create_midi_clip needs Live 12")
        if not _attr(track, "has_midi_input", bool, False):
            raise OpError("Track '%s' is not a MIDI track" % track.name)
        clip = track.create_midi_clip(start, length)
        if clip is None:
            clip = self._find_arrangement_clip(track, start)
        if clip is None:
            raise OpError("Created clip not found at beat %s" % start)
        if op.get("name"):
            clip.name = str(op["name"])
        if op.get("color") is not None:
            clip.color = int(op["color"])
        notes = op.get("notes")
        source = op.get("notes_from")
        if source is not None:
            src = self._session_clip(track, int(source["slot"]))
            notes = self._slice_notes(read_notes(src), float(source.get("offset", 0.0)), length)
        written = self._write_notes(clip, notes or [])
        return {"start": start, "length": length, "notes": written}

    def _op_delete_track(self, op):
        index = self._track_index(op["track"])
        name = self.song.tracks[index].name
        self.song.delete_track(index)
        self.refs = dict((k, v - 1 if v > index else v) for k, v in self.refs.items() if v != index)
        return {"deleted": name}

    def _op_delete_locator(self, op):
        song = self.song
        target = float(op["time"])
        if not any(abs(float(c.time) - target) < EPSILON for c in song.cue_points):
            return {"deleted": False}
        original = song.current_song_time
        song.current_song_time = target
        song.set_or_delete_cue()
        try:
            song.current_song_time = original
        except Exception:
            pass
        return {"deleted": True}

    def _op_set_track(self, op):
        track = self._track(op["track"])
        for key in ("name", "color", "mute"):
            if key in op:
                value = op[key]
                setattr(track, key, int(value) if key == "color" else value)
        return {"name": track.name}

    def _op_show_arrangement(self, op):
        if self.application is not None:
            self.application.view.show_view("Arranger")
        if op.get("back_to_arranger", True):
            try:
                self.song.back_to_arranger = False
            except Exception:
                pass
        if "time" in op:
            self.song.current_song_time = float(op["time"])
        return {"view": "Arranger"}
