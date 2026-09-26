"""A small in-memory stand-in for Live's Python API (just what lom.py uses)."""

from __future__ import annotations


class FakeNote:
    def __init__(self, pitch, start_time, duration, velocity=100, mute=False):
        self.pitch, self.start_time, self.duration = pitch, start_time, duration
        self.velocity, self.mute = velocity, mute


def note_spec(**kwargs):
    return FakeNote(**kwargs)


class FakeClip:
    def __init__(self, name="", length=4.0, midi=True, start_time=0.0, notes=None, color=0,
                 file_path=None):
        self.name, self.length, self.color = name, float(length), color
        self.is_midi_clip, self.is_audio_clip = midi, not midi
        self.start_time = float(start_time)
        self.looping = True
        self.notes = list(notes or [])
        if file_path:
            self.file_path = file_path

    @property
    def end_time(self):
        return self.start_time + self.length

    def get_notes_extended(self, from_pitch, pitch_span, from_time, time_span):
        return [n for n in self.notes
                if from_pitch <= n.pitch < from_pitch + pitch_span
                and from_time <= n.start_time < from_time + time_span]

    def add_new_notes(self, specs):
        self.notes.extend(specs)

    def set_notes(self, tuples):
        self.notes.extend(FakeNote(*t) for t in tuples)

    def copy_to(self, time):
        c = FakeClip(self.name, self.length, self.is_midi_clip, time, None, self.color)
        c.notes = [FakeNote(n.pitch, n.start_time, n.duration, n.velocity, n.mute) for n in self.notes]
        return c


class FakeSlot:
    def __init__(self, clip=None):
        self.clip = clip

    @property
    def has_clip(self):
        return self.clip is not None


class FakeTrack:
    def __init__(self, name, midi=True, slots=8):
        self.name, self.color, self.mute = name, 0, False
        self.has_midi_input, self.has_audio_input = midi, not midi
        self.is_foldable = False
        self.clip_slots = [FakeSlot() for _ in range(slots)]
        self.arrangement_clips = []
        self.devices = []

    def _insert(self, clip):
        # Like Live: a new clip overwrites whatever it covers.
        self.arrangement_clips = [c for c in self.arrangement_clips
                                  if c.end_time <= clip.start_time or c.start_time >= clip.end_time]
        self.arrangement_clips.append(clip)
        self.arrangement_clips.sort(key=lambda c: c.start_time)

    def duplicate_clip_to_arrangement(self, clip, time):
        self._insert(clip.copy_to(time))

    def create_midi_clip(self, start, length):
        if not self.has_midi_input:
            raise RuntimeError("not a MIDI track")
        clip = FakeClip("", length, True, start)
        self._insert(clip)
        return clip

    def delete_clip(self, clip):
        self.arrangement_clips.remove(clip)


class FakeCue:
    def __init__(self, time):
        self.time, self.name = time, ""


class FakeView:
    def __init__(self):
        self.shown = []

    def show_view(self, name):
        self.shown.append(name)


class FakeApp:
    def __init__(self):
        self.view = FakeView()


class FakeSong:
    def __init__(self):
        self.tracks = []
        self.cue_points = []
        self.scenes = []
        self.tempo = 120.0
        self.signature_numerator = self.signature_denominator = 4
        self.current_song_time = 0.0
        self.is_playing = False
        self.back_to_arranger = True
        self.undo_steps = 0

    @property
    def song_length(self):
        ends = [c.end_time for t in self.tracks for c in t.arrangement_clips]
        return max(ends) if ends else 0.0

    def create_midi_track(self, index=-1):
        self._add(FakeTrack(f"{len(self.tracks) + 1}-MIDI", True), index)

    def create_audio_track(self, index=-1):
        self._add(FakeTrack(f"{len(self.tracks) + 1}-Audio", False), index)

    def _add(self, track, index):
        if index < 0:
            self.tracks.append(track)
        else:
            self.tracks.insert(index, track)

    def delete_track(self, index):
        del self.tracks[index]

    def set_or_delete_cue(self):
        for cue in self.cue_points:
            if abs(cue.time - self.current_song_time) < 1e-6:
                self.cue_points.remove(cue)
                return
        self.cue_points.append(FakeCue(self.current_song_time))
        self.cue_points.sort(key=lambda c: c.time)

    def begin_undo_step(self):
        self.undo_steps += 1

    def end_undo_step(self):
        pass


def make_track(song, name, clips, midi=True):
    """Add a track with Session clips: clips = [(slot, name, length_beats, notes)]."""
    track = FakeTrack(name, midi)
    for slot, clip_name, length, notes in clips:
        track.clip_slots[slot].clip = FakeClip(
            clip_name, length, midi, notes=[FakeNote(*n) for n in (notes or [])],
            file_path=None if midi else f"/samples/{clip_name}.wav")
    song.tracks.append(track)
    return track
