"""MIDI parsing helpers. Lists files in a library, inspects tracks, and
expands a subset of tracks into an absolute-time event list ready for
the player. Pure stdlib + mido, no Project Gabriel imports so the
standalone client can use this too.
"""
from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import List, Optional

# General MIDI program names so the AI sees readable instruments instead of
# raw program numbers when picking who plays what.
GM_PROGRAMS = [
    "Acoustic Grand Piano", "Bright Acoustic Piano", "Electric Grand Piano",
    "Honky-tonk Piano", "Electric Piano 1", "Electric Piano 2", "Harpsichord",
    "Clavinet",
    "Celesta", "Glockenspiel", "Music Box", "Vibraphone", "Marimba", "Xylophone",
    "Tubular Bells", "Dulcimer",
    "Drawbar Organ", "Percussive Organ", "Rock Organ", "Church Organ",
    "Reed Organ", "Accordion", "Harmonica", "Tango Accordion",
    "Acoustic Guitar (nylon)", "Acoustic Guitar (steel)", "Electric Guitar (jazz)",
    "Electric Guitar (clean)", "Electric Guitar (muted)", "Overdriven Guitar",
    "Distortion Guitar", "Guitar Harmonics",
    "Acoustic Bass", "Electric Bass (finger)", "Electric Bass (pick)",
    "Fretless Bass", "Slap Bass 1", "Slap Bass 2", "Synth Bass 1", "Synth Bass 2",
    "Violin", "Viola", "Cello", "Contrabass", "Tremolo Strings",
    "Pizzicato Strings", "Orchestral Harp", "Timpani",
    "String Ensemble 1", "String Ensemble 2", "Synth Strings 1", "Synth Strings 2",
    "Choir Aahs", "Voice Oohs", "Synth Voice", "Orchestra Hit",
    "Trumpet", "Trombone", "Tuba", "Muted Trumpet", "French Horn",
    "Brass Section", "Synth Brass 1", "Synth Brass 2",
    "Soprano Sax", "Alto Sax", "Tenor Sax", "Baritone Sax",
    "Oboe", "English Horn", "Bassoon", "Clarinet",
    "Piccolo", "Flute", "Recorder", "Pan Flute", "Blown Bottle",
    "Shakuhachi", "Whistle", "Ocarina",
    "Lead 1 (square)", "Lead 2 (sawtooth)", "Lead 3 (calliope)", "Lead 4 (chiff)",
    "Lead 5 (charang)", "Lead 6 (voice)", "Lead 7 (fifths)", "Lead 8 (bass + lead)",
    "Pad 1 (new age)", "Pad 2 (warm)", "Pad 3 (polysynth)", "Pad 4 (choir)",
    "Pad 5 (bowed)", "Pad 6 (metallic)", "Pad 7 (halo)", "Pad 8 (sweep)",
    "FX 1 (rain)", "FX 2 (soundtrack)", "FX 3 (crystal)", "FX 4 (atmosphere)",
    "FX 5 (brightness)", "FX 6 (goblins)", "FX 7 (echoes)", "FX 8 (sci-fi)",
    "Sitar", "Banjo", "Shamisen", "Koto", "Kalimba", "Bagpipe", "Fiddle", "Shanai",
    "Tinkle Bell", "Agogo", "Steel Drums", "Woodblock", "Taiko Drum",
    "Melodic Tom", "Synth Drum", "Reverse Cymbal",
    "Guitar Fret Noise", "Breath Noise", "Seashore", "Bird Tweet",
    "Telephone Ring", "Helicopter", "Applause", "Gunshot",
]

MIDI_EXTS = {".mid", ".midi"}


def list_midi_files(library: Path) -> List[str]:
    if not library.exists():
        return []
    return sorted(p.name for p in library.iterdir()
                  if p.is_file() and p.suffix.lower() in MIDI_EXTS)


def find_midi(library: Path, query: str) -> Optional[Path]:
    if not query or not library.exists():
        return None
    q = query.strip().lower()
    for p in sorted(library.iterdir()):
        if not p.is_file() or p.suffix.lower() not in MIDI_EXTS:
            continue
        if p.name.lower() == q or p.stem.lower() == q:
            return p
    for p in sorted(library.iterdir()):
        if not p.is_file() or p.suffix.lower() not in MIDI_EXTS:
            continue
        if q in p.name.lower():
            return p
    return None


def load_midi_b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _build_tempo_map(mid) -> list:
    """List of (abs_tick, tempo_us_per_beat). Tempo events can live in any
    track so we walk all of them and merge sorted."""
    tempos = []
    for track in mid.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.type == "set_tempo":
                tempos.append((abs_tick, msg.tempo))
    tempos.sort(key=lambda x: x[0])
    return tempos


def _ticks_to_seconds(target_tick: int, tpb: int, tempo_map: list) -> float:
    import mido
    seconds = 0.0
    last_tick = 0
    cur_tempo = 500000  # default 120 BPM
    for tc_tick, tc_tempo in tempo_map:
        if tc_tick >= target_tick:
            break
        seconds += mido.tick2second(tc_tick - last_tick, tpb, cur_tempo)
        last_tick = tc_tick
        cur_tempo = tc_tempo
    seconds += mido.tick2second(max(0, target_tick - last_tick), tpb, cur_tempo)
    return seconds


def parse_tracks(file_bytes: bytes) -> dict:
    """Return summary info about every track in the file:
    {tracks: [{index, name, instrument, channels, note_count, duration}],
     duration: float, ticks_per_beat: int}
    Costs one full pass through the file, do it once on load.
    """
    import mido
    mid = mido.MidiFile(file=io.BytesIO(file_bytes))
    tpb = mid.ticks_per_beat
    tempo_map = _build_tempo_map(mid)

    total_duration = 0.0
    tracks_info = []
    for i, track in enumerate(mid.tracks):
        name = ""
        instrument = ""
        channels = set()
        program_per_channel = {}
        note_count = 0
        last_note_tick = 0
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.type == "track_name" and not name:
                name = (msg.name or "").strip()
            elif msg.type == "instrument_name" and not instrument:
                instrument = (msg.name or "").strip()
            elif msg.type == "program_change":
                channels.add(msg.channel)
                program_per_channel[msg.channel] = msg.program
            elif msg.type in ("note_on", "note_off"):
                channels.add(msg.channel)
                if msg.type == "note_on" and msg.velocity > 0:
                    note_count += 1
                last_note_tick = abs_tick
        track_dur = _ticks_to_seconds(last_note_tick, tpb, tempo_map) if last_note_tick else 0.0
        total_duration = max(total_duration, track_dur)

        if not instrument:
            if 9 in channels:
                instrument = "Drums"
            elif program_per_channel:
                _, prog = next(iter(program_per_channel.items()))
                instrument = GM_PROGRAMS[prog] if 0 <= prog < 128 else f"Program {prog}"
        if not name:
            name = instrument or f"Track {i}"

        tracks_info.append({
            "index": i,
            "name": name,
            "instrument": instrument,
            "channels": sorted(channels),
            "note_count": note_count,
            "duration": round(track_dur, 2),
        })
    return {
        "tracks": tracks_info,
        "duration": round(total_duration, 2),
        "ticks_per_beat": tpb,
    }


def expand_track_events(file_bytes: bytes, track_indices: list) -> list:
    """Return [(offset_seconds, mido.Message), ...] sorted by offset, for
    only the tracks in track_indices. Meta messages are dropped, only
    sounding events make it through.

    Channel setup events (program_change, control_change, pitchwheel)
    that live in OTHER tracks but target a channel our chosen tracks
    use also get pulled in. Without this, midis that put all the
    program_change events in a conductor track end up sounding like
    piano on every client because they only got the note tracks."""
    import mido
    mid = mido.MidiFile(file=io.BytesIO(file_bytes))
    tpb = mid.ticks_per_beat
    tempo_map = _build_tempo_map(mid)

    note_kinds = {"note_on", "note_off", "aftertouch", "polytouch"}
    setup_kinds = {"program_change", "control_change", "pitchwheel"}
    wanted = set(int(i) for i in track_indices if i is not None)

    # first pass: which channels do our tracks actually play on?
    used_channels = set()
    for ti in wanted:
        if ti < 0 or ti >= len(mid.tracks):
            continue
        for msg in mid.tracks[ti]:
            if msg.is_meta:
                continue
            if msg.type in note_kinds and hasattr(msg, "channel"):
                used_channels.add(msg.channel)

    events = []
    for ti, track in enumerate(mid.tracks):
        is_ours = ti in wanted
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.is_meta:
                continue
            t = msg.type
            if is_ours and (t in note_kinds or t in setup_kinds):
                events.append((_ticks_to_seconds(abs_tick, tpb, tempo_map), msg))
            elif (not is_ours) and t in setup_kinds and \
                    hasattr(msg, "channel") and msg.channel in used_channels:
                events.append((_ticks_to_seconds(abs_tick, tpb, tempo_map), msg))
    events.sort(key=lambda x: x[0])
    return events
