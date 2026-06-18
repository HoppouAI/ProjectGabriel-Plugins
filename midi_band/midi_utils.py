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


def _install_lenient_meta():
    """mido aborts the whole file on a malformed meta event, eg a truncated
    time_signature (fewer than the 4 data bytes it wants). Plenty of midis in
    the wild have these and lenient players (VanBasco etc) just shrug them off.
    Wrap mido's build_meta_message so a bad meta event is kept as raw bytes
    instead of crashing the parse. Patched once, at import."""
    try:
        from mido.midifiles import meta as _meta
        from mido.midifiles import midifiles as _mf
    except Exception:
        return
    orig = getattr(_mf, "build_meta_message", None)
    if orig is None or getattr(orig, "_pg_lenient", False):
        return
    Unknown = _meta.UnknownMetaMessage

    def lenient_build_meta_message(meta_type, data, delta=0):
        try:
            return orig(meta_type, data, delta)
        except Exception:
            # short/garbled meta event, keep the raw bytes so the rest of
            # the track still parses instead of nuking the whole file.
            try:
                return Unknown(meta_type, list(data), delta)
            except Exception:
                return Unknown(meta_type, [], delta)

    lenient_build_meta_message._pg_lenient = True
    # read_meta_message in midifiles.py captured the symbol by name, so patch
    # there. patch the meta module too for any other caller.
    _mf.build_meta_message = lenient_build_meta_message
    try:
        _meta.build_meta_message = lenient_build_meta_message
    except Exception:
        pass


_install_lenient_meta()


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


def _norm_program(v):
    """Accept a legacy int (GM program, bank 0) or a {'bank','program'}
    dict and return (bank, program), or None if unusable. program clamps
    to 0-127, bank to 0-128 (128 = drum kits)."""
    try:
        if isinstance(v, dict):
            p = int(v.get("program"))
            b = int(v.get("bank", 0) or 0)
        else:
            p = int(v)
            b = 0
    except Exception:
        return None
    if not (0 <= p <= 127):
        return None
    if b < 0 or b > 128:
        b = 0
    return b, p


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


# ordered: most specific first so "electric bass" hits before "bass"
_NAME_HINTS = [
    ("acoustic bass", 32), ("fretless bass", 35), ("slap bass", 36),
    ("synth bass", 38), ("electric bass", 33), ("bass", 33),
    ("distortion guitar", 30), ("overdriven guitar", 29),
    ("electric guitar", 27), ("clean guitar", 27),
    ("acoustic guitar", 25), ("nylon guitar", 24),
    ("rhythm guitar", 27), ("lead guitar", 30), ("guitar", 27),
    ("piano", 0), ("electric piano", 4), ("rhodes", 4),
    ("organ", 16), ("hammond", 16),
    ("violin", 40), ("viola", 41), ("cello", 42), ("contrabass", 43),
    ("strings", 48), ("string ensemble", 48), ("orchestra", 48),
    ("choir", 52), ("voice", 53), ("vocal", 53), ("vox", 53),
    ("trumpet", 56), ("trombone", 57), ("tuba", 58),
    ("french horn", 60), ("brass", 61),
    ("soprano sax", 64), ("alto sax", 65), ("tenor sax", 66),
    ("baritone sax", 67), ("sax", 65),
    ("flute", 73), ("piccolo", 72), ("clarinet", 71), ("oboe", 68),
    ("synth lead", 80), ("lead synth", 80), ("synth pad", 88),
    ("pad", 88), ("synth", 80),
    ("harp", 46), ("xylophone", 13), ("marimba", 12), ("vibraphone", 11),
    ("banjo", 105), ("sitar", 104), ("accordion", 21), ("harmonica", 22),
]


def infer_program_from_name(track_name: str) -> Optional[int]:
    if not track_name:
        return None
    low = track_name.lower().strip()
    # some midis prefix the track name with the 1-indexed GM program number,
    # eg "031 Distortion Guitar" or "086 Solo Vox". trust that over text hints.
    import re
    m = re.match(r"^0*(\d{1,3})\b", low)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 128:
            return n - 1
    for needle, prog in _NAME_HINTS:
        if needle in low:
            return prog
    return None


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


def _note_channels(track) -> List[int]:
    """Sorted channels that actually play notes in a flat message stream.
    Shared by the per-channel parse and expand so a virtual track index
    maps to the same channel in both. Empty if no notes."""
    chans = set()
    for msg in track or []:
        if msg.is_meta:
            continue
        if msg.type in ("note_on", "note_off") and hasattr(msg, "channel"):
            chans.add(msg.channel)
    return sorted(chans)


def _merged_messages(mid):
    """Flatten every track into one delta-timed stream. Lets the per-channel
    parser treat channel-organized multitrack files (instruments live on the
    channels, the tracks are just sections/segments) the same way it treats
    a type-0 single-track file. For a single-track file this is just track 0."""
    if not mid.tracks:
        return []
    if len(mid.tracks) == 1:
        return mid.tracks[0]
    import mido
    try:
        return mido.merge_tracks(mid.tracks)
    except Exception:
        out = []
        for tr in mid.tracks:
            out.extend(tr)
        return out


def _looks_channel_organized(mid) -> bool:
    """True when instruments live on channels rather than tracks: several
    note tracks pile onto the same channels (eg every section/segment track
    writes drums on ch9 plus a few melody channels). Parsing such a file by
    track mislabels everything (usually all "Drums"), parsing by channel
    recovers the real instruments."""
    note_tracks = 0
    chan_track_count: dict = {}
    for tr in mid.tracks:
        chans = set()
        for msg in tr:
            if msg.is_meta:
                continue
            if msg.type == "note_on" and getattr(msg, "velocity", 0) > 0 \
                    and hasattr(msg, "channel"):
                chans.add(msg.channel)
        if chans:
            note_tracks += 1
            for ch in chans:
                chan_track_count[ch] = chan_track_count.get(ch, 0) + 1
    distinct = len(chan_track_count)
    if note_tracks <= 1 or distinct == 0:
        return False
    # one channel shared by lots of tracks AND way more tracks than channels
    shared = any(c >= 3 for c in chan_track_count.values())
    many = note_tracks >= distinct * 2
    return shared and many


def _resolve_parse_mode(mid, mode) -> str:
    """Map the requested mode (auto/track/channel) to a concrete one.
    parse_tracks and expand_track_events both call this so the virtual
    track indices line up between the two passes."""
    m = (mode or "auto").lower()
    if m in ("track", "channel"):
        return m
    if len(mid.tracks) <= 1:
        return "channel"
    return "channel" if _looks_channel_organized(mid) else "track"


def _parse_by_channel(track, tpb, tempo_map) -> Optional[dict]:
    """Treat each MIDI channel as its own assignable track. Used for type-0
    files and any channel-organized file where the instruments live on the
    channels (the tracks are just sections), so the band can split them
    instead of dumping the whole song (often mislabeled "Drums" because
    channel 9 is in the mix) onto one player."""
    units = _note_channels(track)
    if not units:
        return None
    prog_per_chan: dict = {}
    notes_per_chan = {ch: 0 for ch in units}
    last_tick_per_chan = {ch: 0 for ch in units}
    abs_tick = 0
    for msg in track:
        abs_tick += msg.time
        if msg.is_meta:
            continue
        ch = getattr(msg, "channel", None)
        if ch is None:
            continue
        if msg.type == "program_change":
            # keep the first non-zero program a channel asks for, else 0
            cur = prog_per_chan.get(ch)
            if cur is None or (cur == 0 and msg.program != 0):
                prog_per_chan[ch] = msg.program
        elif msg.type in ("note_on", "note_off") and ch in notes_per_chan:
            if msg.type == "note_on" and msg.velocity > 0:
                notes_per_chan[ch] += 1
            last_tick_per_chan[ch] = abs_tick

    total_duration = 0.0
    tracks_info = []
    for pos, ch in enumerate(units):
        last_tick = last_tick_per_chan[ch]
        dur = _ticks_to_seconds(last_tick, tpb, tempo_map) if last_tick else 0.0
        total_duration = max(total_duration, dur)
        if ch == 9:
            instrument = "Drums"
        else:
            prog = prog_per_chan.get(ch)
            if prog is not None and 0 <= prog < 128:
                instrument = GM_PROGRAMS[prog]
            else:
                instrument = GM_PROGRAMS[0]
        label = instrument or f"Channel {ch + 1}"
        tracks_info.append({
            "index": pos,
            "name": label,
            "instrument": instrument,
            "display_label": label,
            "channels": [ch],
            "note_count": notes_per_chan[ch],
            "duration": round(dur, 2),
        })
    return {
        "tracks": tracks_info,
        "duration": round(total_duration, 2),
        "ticks_per_beat": tpb,
    }


def _expand_by_channel(track, tpb, tempo_map, track_indices, track_programs,
                       note_kinds, setup_kinds) -> list:
    """Per-channel event expansion for type-0 and channel-organized files.
    track_indices are virtual positions into the same channel ordering
    _parse_by_channel used."""
    import mido
    units = _note_channels(track)
    wanted_channels = set()
    for i in track_indices or []:
        try:
            pos = int(i)
        except Exception:
            continue
        if 0 <= pos < len(units):
            wanted_channels.add(units[pos])

    # instrument overrides keyed by virtual position, mapped to channel.
    # drum kits (ch9) are allowed: a program_change does switch kits in
    # GM/GS, the player just forces bank 128 on ch9 for it.
    overrides = {}
    for k, v in (track_programs or {}).items():
        np = _norm_program(v)
        if np is None:
            continue
        try:
            pos = int(k)
        except Exception:
            continue
        if 0 <= pos < len(units):
            overrides[units[pos]] = np  # (bank, program)

    events = []
    abs_tick = 0
    for msg in track:
        abs_tick += msg.time
        if msg.is_meta:
            continue
        ch = getattr(msg, "channel", None)
        if ch is None or ch not in wanted_channels:
            continue
        t = msg.type
        if t not in note_kinds and t not in setup_kinds:
            continue
        sec = _ticks_to_seconds(abs_tick, tpb, tempo_map)
        if t == "program_change" and ch in overrides:
            bank, prog = overrides[ch]
            if ch != 9 and bank:
                events.append((sec, mido.Message("control_change", channel=ch, control=0, value=bank)))
            try:
                msg = msg.copy(program=prog)
            except Exception:
                pass
        events.append((sec, msg))

    # if an overridden channel never sent a program_change, prepend one (plus
    # its bank) so the synth doesnt sit on the default instrument
    seen_pc = set(m.channel for _, m in events if m.type == "program_change")
    for ch, (bank, prog) in overrides.items():
        if ch in wanted_channels and ch not in seen_pc:
            if ch != 9 and bank:
                events.append((0.0, mido.Message("control_change", channel=ch, control=0, value=bank)))
            events.append((0.0, mido.Message("program_change", channel=ch, program=prog)))
    events.sort(key=lambda x: (x[0], 0 if x[1].type in setup_kinds else 1))
    return events


def parse_tracks(file_bytes: bytes, mode: str = "auto") -> dict:
    """Return summary info about every track in the file:
    {tracks: [{index, name, instrument, channels, note_count, duration}],
     duration: float, ticks_per_beat: int, parse_mode: str}
    Costs one full pass through the file, do it once on load.

    mode: "auto" picks per-track vs per-channel by inspecting the file,
    "track" forces the per-track read, "channel" forces the per-channel
    read (use it for channel-organized files that read as all "Drums").
    """
    import mido
    mid = mido.MidiFile(file=io.BytesIO(file_bytes), clip=True)
    tpb = mid.ticks_per_beat
    tempo_map = _build_tempo_map(mid)

    # channel mode: every instrument lives on its own channel and the tracks
    # are just sections. split per channel so each can be assigned, otherwise
    # the whole song lands on one player and reads as "Drums". covers type-0
    # singletrack files and channel-organized multitrack files.
    if _resolve_parse_mode(mid, mode) == "channel":
        split = _parse_by_channel(_merged_messages(mid), tpb, tempo_map)
        if split is not None:
            split["parse_mode"] = "channel"
            return split
        # nothing on any channel, fall back to the per-track read

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

        # drums first (channel 9 is GM percussion regardless of program).
        # also catch "drum" in the track name since some files put drums on
        # other channels.
        is_drum_name = bool(name) and "drum" in name.lower()
        if 9 in channels or is_drum_name:
            instrument = "Drums"
        if not instrument:
            # prefer a non-zero program_change, since program=0 is usually
            # just the default and doesnt mean the track is actually piano.
            nonzero = [p for p in program_per_channel.values() if p != 0]
            if nonzero:
                prog = nonzero[0]
                instrument = GM_PROGRAMS[prog] if 0 <= prog < 128 else f"Program {prog}"
            else:
                # all program_changes are 0 (or none at all). try to infer
                # from the track name, which often encodes the real instrument
                # (eg "031 Distortion Guitar").
                hinted = infer_program_from_name(name)
                if hinted is not None:
                    instrument = GM_PROGRAMS[hinted]
                elif program_per_channel:
                    instrument = GM_PROGRAMS[0]
        if not name:
            name = instrument or f"Track {i}"

        # display_label is what shows in the chatbox / UI / sync rows.
        # We avoid raw track names because some midi files stash author
        # metadata, emails, etc. in there. Prefer the GM instrument
        # always, fall back to a generic label.
        display_label = instrument or f"Track {i + 1}"

        tracks_info.append({
            "index": i,
            "name": name,
            "instrument": instrument,
            "display_label": display_label,
            "channels": sorted(channels),
            "note_count": note_count,
            "duration": round(track_dur, 2),
        })
    return {
        "tracks": tracks_info,
        "duration": round(total_duration, 2),
        "ticks_per_beat": tpb,
        "parse_mode": "track",
    }


def expand_track_events(file_bytes: bytes, track_indices: list,
                        track_programs: Optional[dict] = None,
                        mode: str = "auto") -> list:
    """Return [(offset_seconds, mido.Message), ...] sorted by offset, for
    only the tracks in track_indices. Meta messages are dropped, only
    sounding events make it through.

    Channel setup events (program_change, control_change, pitchwheel)
    that live in OTHER tracks but target a channel our chosen tracks
    use also get pulled in. Without this, midis that put all the
    program_change events in a conductor track end up sounding like
    piano on every client because they only got the note tracks.

    track_programs is an optional {track_index: gm_program} map that
    forces a track to play as a different instrument than the midi
    asked for. Wins over both the file's own program_change and our
    name-based guess."""
    import mido
    mid = mido.MidiFile(file=io.BytesIO(file_bytes), clip=True)
    tpb = mid.ticks_per_beat
    tempo_map = _build_tempo_map(mid)

    note_kinds = {"note_on", "note_off", "aftertouch", "polytouch"}
    setup_kinds = {"program_change", "control_change", "pitchwheel"}

    # channel mode: track_indices are per-channel virtual positions, mirror
    # the split parse_tracks made. covers type-0 and channel-organized files.
    if _resolve_parse_mode(mid, mode) == "channel":
        return _expand_by_channel(
            _merged_messages(mid), tpb, tempo_map, track_indices,
            track_programs, note_kinds, setup_kinds,
        )

    wanted = set(int(i) for i in track_indices if i is not None)

    # explicit per-track instrument overrides. wire form has string keys and
    # values are either a legacy GM int or a {bank, program} dict.
    overrides = {}
    for k, v in (track_programs or {}).items():
        np = _norm_program(v)
        if np is None:
            continue
        try:
            ti = int(k)
        except Exception:
            continue
        overrides[ti] = np  # (bank, program)
    # map each overridden track's note channels to its chosen (bank, program).
    # drum kits (ch9) are allowed, the synth switches kits on program_change.
    channel_override = {}
    for ti, bp in overrides.items():
        if ti < 0 or ti >= len(mid.tracks):
            continue
        for msg in mid.tracks[ti]:
            if msg.is_meta:
                continue
            if hasattr(msg, "channel") and msg.type in note_kinds:
                channel_override[msg.channel] = bp

    # first pass: which channels do our tracks actually play on?
    used_channels = set()
    track_names = {}
    for ti in wanted:
        if ti < 0 or ti >= len(mid.tracks):
            continue
        for msg in mid.tracks[ti]:
            if msg.is_meta:
                if msg.type == "track_name" and ti not in track_names:
                    track_names[ti] = (msg.name or "").strip()
                continue
            if msg.type in note_kinds and hasattr(msg, "channel"):
                used_channels.add(msg.channel)

    # figure out per-channel program override from track names. lots of
    # hand-made midis leave every program_change at 0 (piano) and rely on
    # an external mapping, which makes everyone sound like piano. for our
    # tracks, if a channel only ever asks for program 0, we infer something
    # sensible from the track name.
    channel_inferred = {}
    for ti in wanted:
        if ti < 0 or ti >= len(mid.tracks):
            continue
        name = track_names.get(ti, "")
        inferred = infer_program_from_name(name)
        if inferred is None:
            continue
        track_channels = set()
        nonzero_program_seen = set()
        for msg in mid.tracks[ti]:
            if msg.is_meta:
                continue
            if hasattr(msg, "channel") and msg.type in note_kinds:
                track_channels.add(msg.channel)
            if msg.type == "program_change" and msg.program != 0:
                nonzero_program_seen.add(msg.channel)
        for ch in track_channels:
            if ch == 9:
                continue  # leave drums alone
            if ch in nonzero_program_seen:
                continue  # the midi already picked something, respect it
            channel_inferred[ch] = inferred

    events = []
    for ti, track in enumerate(mid.tracks):
        is_ours = ti in wanted
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.is_meta:
                continue
            t = msg.type
            emit = (is_ours and (t in note_kinds or t in setup_kinds)) or \
                ((not is_ours) and t in setup_kinds and
                 hasattr(msg, "channel") and msg.channel in used_channels)
            if not emit:
                continue
            sec = _ticks_to_seconds(abs_tick, tpb, tempo_map)
            # explicit override wins (with its bank), else rewrite program=0
            # on inferred channels so the synth picks the right instrument
            # instead of grand piano
            if t == "program_change" and msg.channel in channel_override:
                bank, prog = channel_override[msg.channel]
                if msg.channel != 9 and bank:
                    events.append((sec, mido.Message("control_change", channel=msg.channel, control=0, value=bank)))
                try:
                    msg = msg.copy(program=prog)
                except Exception:
                    pass
            elif t == "program_change" and msg.program == 0 \
                    and msg.channel in channel_inferred:
                try:
                    msg = msg.copy(program=channel_inferred[msg.channel])
                except Exception:
                    pass
            events.append((sec, msg))

    # if a channel we're playing has NO program_change at all in any
    # track, prepend one so fluidsynth doesnt sit on the per-channel
    # default (piano). also covers the case where the inferred program
    # never had a corresponding program=0 to rewrite.
    seen_program_for = set()
    for _, msg in events:
        if msg.type == "program_change":
            seen_program_for.add(msg.channel)
    # explicit overrides first so they win, then name-inferred fallbacks
    for ch, (bank, prog) in channel_override.items():
        if ch in seen_program_for:
            continue
        if ch != 9 and bank:
            events.append((0.0, mido.Message("control_change", channel=ch, control=0, value=bank)))
        events.append((0.0, mido.Message("program_change", channel=ch, program=prog)))
        seen_program_for.add(ch)
    for ch, prog in channel_inferred.items():
        if ch in seen_program_for:
            continue
        events.append((0.0, mido.Message("program_change", channel=ch, program=prog)))
    events.sort(key=lambda x: (x[0], 0 if x[1].type in setup_kinds else 1))
    return events


def with_count_in(events: list, beats: int = 4, bpm: float = 120.0) -> tuple:
    """Prepend a metronome count-in to events. Returns (new_events, lead_seconds).
    Standard 4-on-the-floor click on GM percussion (channel 9, hi wood block)
    so all clients hear the same thing regardless of their assigned tracks.
    Beat 1 is accented."""
    import mido
    if beats <= 0 or bpm <= 0:
        return list(events), 0.0
    beat_dur = 60.0 / float(bpm)
    note = 76  # Hi Wood Block
    click = []
    for i in range(int(beats)):
        t = i * beat_dur
        vel = 110 if i == 0 else 90
        click.append((t, mido.Message("note_on", channel=9, note=note, velocity=vel)))
        click.append((t + 0.08, mido.Message("note_off", channel=9, note=note, velocity=0)))
    lead = beats * beat_dur
    shifted = [(o + lead, m) for (o, m) in events]
    out = click + shifted
    out.sort(key=lambda x: x[0])
    return out, lead
