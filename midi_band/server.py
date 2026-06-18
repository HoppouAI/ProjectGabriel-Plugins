"""TCP server (host side) for the midi_band protocol.

Owns the loaded song, the per-client track assignments, and the
prepare/ready/play handshake. Runs the host's own playback locally for
its share of tracks. No Project Gabriel imports so it works the same
way under the plugin or any other harness.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import midi_utils
from . import protocol as P
from . import conductor as _conductor
from .player import MidiPlayer
from .audio_player import AudioPlayer, mix_stems
from .audio_library import AudioLibrary

logger = logging.getLogger(__name__)

# bigger window than duo_song since we ship the full midi over the wire
READY_TIMEOUT = 3.0
# audio clients download + decode their stems before acking, the first play
# of a song can take a while so give them a lot more room than midi.
AUDIO_READY_TIMEOUT = 30.0
# clients ping every 4s, so if we hear nothing for this long the socket is a
# half-open ghost (client crashed / restarted) and we reap it.
PEER_IDLE_TIMEOUT = 15.0


def _now() -> float:
    return time.monotonic()


def _strip_ext(name: str) -> str:
    """Drop a trailing .mid / .midi so the chatbox and UI show a clean name."""
    for ext in (".midi", ".mid"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name


class _Peer:
    def __init__(self, name: str, writer: asyncio.StreamWriter):
        self.name = name
        self.writer = writer
        self.connected_at = _now()
        self.ready_session: Optional[str] = None
        self.nack_reason: Optional[str] = None
        # most recent sync info reported by client in its PINGs
        self.sync_jitter: float = 0.0
        self.sync_rtt: float = 0.0
        self.sync_updated_at: float = 0.0


class BandServer:
    def __init__(
        self,
        bind: str,
        port: int,
        instance_name: str,
        player: MidiPlayer,
        library_dir: Path,
        schedule_lead_seconds: float = 1.5,
        on_change: Optional[Callable[[], None]] = None,
        count_in_beats: int = 4,
        count_in_bpm: float = 120.0,
        presets_path: Optional[Path] = None,
        conductor_key_provider: Optional[Callable[[], str]] = None,
        conductor_model: Optional[str] = None,
        audio_player: Optional[AudioPlayer] = None,
        audio_library_dir: Optional[Path] = None,
        audio_http_port: Optional[int] = None,
    ):
        self.bind = bind or "0.0.0.0"
        self.port = int(port)
        self.instance_name = instance_name or "gabriel"
        self.player = player
        self.library_dir = library_dir
        self.lead = max(0.5, float(schedule_lead_seconds))
        self.count_in_beats = max(0, int(count_in_beats))
        self.count_in_bpm = max(20.0, float(count_in_bpm))
        self.on_change = on_change or (lambda: None)

        # audio band mode: a second player + a folder-based stem library.
        # midi mode is the default, the webui / a tool flips self.mode.
        self.mode = P.MODE_MIDI
        self.audio_player = audio_player
        self.audio_library = AudioLibrary(audio_library_dir) if audio_library_dir else None
        self.audio_http_port = int(audio_http_port) if audio_http_port else None

        self._server: Optional[asyncio.AbstractServer] = None
        self._peers: Dict[asyncio.StreamWriter, _Peer] = {}
        self._stop = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._dispatch_lock: Optional[asyncio.Lock] = None

        # currently loaded song state
        self._loaded_song: Optional[str] = None
        self._loaded_path: Optional[Path] = None
        self._loaded_info: Optional[dict] = None
        # how the loaded file was read. requested is what the user asked for
        # (auto/track/channel), resolved is the concrete mode parse_tracks
        # actually used. clients and the host expand with the resolved one.
        self._parse_mode: str = "auto"
        self._resolved_parse_mode: str = "track"
        self._assignments: Dict[str, List[int]] = {}
        self._host_tracks: List[int] = []
        # per-track instrument overrides, {track_index: {bank, program}}.
        # empties out on song load, baked into events at play time.
        self._track_programs: Dict[int, dict] = {}

        # AI conductor hooks, both optional. key provider is called lazily so
        # it picks up the host key even if it lands after setup
        self._conductor_key_provider = conductor_key_provider
        self._conductor_model = conductor_model or "gemini-3.1-flash-lite"
        # one long-lived chat session so the conductor is genuinely multi-turn
        self._conductor_session = None

        # saved assignment layouts, persisted under the plugin data dir
        self._presets_path = presets_path
        self._presets: Dict[str, dict] = {}
        self._load_presets()
        # which saved preset is live right now, so the chatbox can show its
        # name instead of the raw song file. cleared on any manual reassign.
        self._active_preset: Optional[str] = None
        # friendly display names for midi files, {filename: nice name}
        self._song_names_path = (
            self._presets_path.parent / "song_names.json" if self._presets_path else None
        )
        self._song_names: Dict[str, str] = {}
        self._load_song_names()

        # keep-warm "sync tone": a soft continuous hum every member plays to
        # hold VRChat's voice gate open so the band stops drifting on phrases.
        self._tone_on = False
        self._tone_gain = 0.15

    # ----- lifecycle -----

    def start(self) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("midi_band server: no running loop")
            return False
        self._loop = loop
        self._stop = False
        loop.create_task(self._serve())
        return True

    def stop(self):
        self._stop = True
        try:
            self.player.stop_playback()
        except Exception:
            pass
        if self.audio_player is not None:
            try:
                self.audio_player.stop_playback()
            except Exception:
                pass
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._shutdown_async)

    def _shutdown_async(self):
        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
        for w in list(self._peers.keys()):
            try:
                w.close()
            except Exception:
                pass
        self._peers.clear()

    # ----- active player / mode -----

    def _active(self):
        # whichever player drives transport for the current mode. audio
        # mode falls back to the midi player if the audio stack didn't load.
        if self.mode == P.MODE_AUDIO and self.audio_player is not None:
            return self.audio_player
        return self.player

    def active_status(self) -> dict:
        return self._active().status()

    def get_mode(self) -> str:
        return self.mode

    def set_mode(self, mode: str) -> dict:
        m = P.MODE_AUDIO if str(mode).lower() == P.MODE_AUDIO else P.MODE_MIDI
        if m == self.mode:
            return {"result": "ok", "mode": self.mode}
        if m == P.MODE_AUDIO and self.audio_player is None:
            return {"result": "error", "message": "audio mode unavailable, install sounddevice + soundfile"}
        # stop playback and drop the loaded song, track indices don't carry
        # across modes
        try:
            self._active().stop_playback()
        except Exception:
            pass
        self.mode = m
        self._loaded_song = None
        self._loaded_path = None
        self._loaded_info = None
        self._parse_mode = "auto"
        self._resolved_parse_mode = "track"
        self._assignments = {}
        self._host_tracks = []
        self._track_programs = {}
        self._active_preset = None
        self.on_change()
        return {"result": "ok", "mode": self.mode}

    # ----- networking -----

    async def _serve(self):
        try:
            self._server = await asyncio.start_server(
                self._handle_peer, self.bind, self.port, limit=P.READER_LIMIT
            )
            logger.info(f"midi_band: hosting on {self.bind}:{self.port}")
            asyncio.create_task(self._sync_broadcaster())
            async with self._server:
                await self._server.serve_forever()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"midi_band server crashed: {e}")

    async def _sync_broadcaster(self):
        # while the player is producing audio, broadcast our authoritative
        # clock + playback position once a second so clients can correct drift.
        try:
            while not self._stop:
                await asyncio.sleep(1.0)
                try:
                    active = self._active()
                    if not active.is_playing():
                        continue
                    pos = active.current_position()
                    payload = P.encode({
                        "type": P.SYNC_TICK,
                        "server_t": _now(),
                        "pos": pos,
                    })
                    # fire-and-forget: don't await drain, a slow peer must
                    # never stall the asyncio loop and starve other tasks.
                    # missing one tick is harmless, the next one will catch up.
                    for w in list(self._peers.keys()):
                        try:
                            w.write(payload)
                        except Exception:
                            pass
                except Exception as e:
                    logger.debug(f"midi_band: sync_broadcaster tick error: {e}")
        except asyncio.CancelledError:
            pass

    async def _handle_peer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer_addr = writer.get_extra_info("peername")
        peer_name = f"{peer_addr}"
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not line:
                writer.close()
                return
            try:
                hello = P.decode(line)
            except Exception:
                writer.close()
                return
            if hello.get("type") != P.HELLO:
                writer.close()
                return
            peer_name = str(hello.get("name") or peer_name)
            # one name = one peer. a client that reconnects (restart, network
            # blip, audio deps relaunch) opens a fresh socket while the old
            # half-open one can linger as a ghost, kick any stale same-name
            # peer before adding this one so the roster doesn't duplicate.
            for old_w, old_p in list(self._peers.items()):
                if old_w is not writer and old_p.name == peer_name:
                    self._peers.pop(old_w, None)
                    try:
                        old_w.close()
                    except Exception:
                        pass
                    logger.info(f"midi_band: replaced stale peer: {peer_name}")
            self._peers[writer] = _Peer(peer_name, writer)
            writer.write(P.encode({"type": P.WELCOME, "name": self.instance_name}))
            await writer.drain()
            logger.info(f"midi_band: peer joined: {peer_name}")
            self.on_change()
            # bring the new peer up to date on current assignments
            if self._assignments or self._host_tracks:
                try:
                    writer.write(P.encode({
                        "type": P.ASSIGNMENTS,
                        "assignments": self._assignments_for_broadcast(),
                    }))
                    await writer.drain()
                except Exception:
                    pass
            # if the keep-warm tone is running, get this peer humming too
            if self._tone_on:
                try:
                    writer.write(P.encode({
                        "type": P.TONE, "on": True, "gain": self._tone_gain,
                    }))
                    await writer.drain()
                except Exception:
                    pass
            while not self._stop:
                try:
                    line = await asyncio.wait_for(
                        reader.readline(), timeout=PEER_IDLE_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logger.info(f"midi_band: peer {peer_name} idle, dropping")
                    break
                if not line:
                    break
                try:
                    msg = P.decode(line)
                except Exception:
                    continue
                await self._on_peer_msg(writer, msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"midi_band: peer {peer_name} dropped: {e}")
        finally:
            self._peers.pop(writer, None)
            try:
                writer.close()
            except Exception:
                pass
            logger.info(f"midi_band: peer left: {peer_name}")
            self.on_change()

    async def _on_peer_msg(self, writer: asyncio.StreamWriter, msg: dict):
        kind = msg.get("type")
        if kind == P.PING:
            t_in = msg.get("t")
            writer.write(P.encode({"type": P.PONG, "t_in": t_in, "server_t": _now()}))
            try:
                await writer.drain()
            except Exception:
                pass
            peer = self._peers.get(writer)
            if peer is not None:
                cj = msg.get("client_jitter")
                cr = msg.get("client_rtt")
                if isinstance(cj, (int, float)):
                    peer.sync_jitter = float(cj)
                if isinstance(cr, (int, float)):
                    peer.sync_rtt = float(cr)
                peer.sync_updated_at = _now()
            return
        if kind == P.READY:
            peer = self._peers.get(writer)
            if peer is not None:
                peer.ready_session = str(msg.get("session") or "")
                peer.nack_reason = None
            return
        if kind == P.NACK:
            peer = self._peers.get(writer)
            if peer is not None:
                peer.ready_session = str(msg.get("session") or "")
                peer.nack_reason = str(msg.get("reason") or "declined")
                logger.warning(f"midi_band: '{peer.name}' nack: {peer.nack_reason}")
            return

    async def _broadcast(self, payload: bytes):
        dead: List[asyncio.StreamWriter] = []
        for w in list(self._peers.keys()):
            try:
                w.write(payload)
                await w.drain()
            except Exception:
                dead.append(w)
        for w in dead:
            self._peers.pop(w, None)
            try:
                w.close()
            except Exception:
                pass

    # ----- public host API used by tools -----

    def list_songs(self) -> List[str]:
        return midi_utils.list_midi_files(self.library_dir)

    def list_clients(self) -> List[str]:
        return [p.name for p in self._peers.values()]

    def loaded_info(self) -> dict:
        if not self._loaded_info:
            return {
                "song": None, "tracks": [], "duration": 0.0,
                "assignments": {}, "host_tracks": [], "track_programs": {},
                "mode": self.mode, "parse_mode": self._parse_mode,
                "resolved_parse_mode": self._resolved_parse_mode,
            }
        return {
            "song": self._loaded_song,
            "tracks": self._loaded_info["tracks"],
            "duration": self._loaded_info["duration"],
            "assignments": dict(self._assignments),
            "host_tracks": list(self._host_tracks),
            "track_programs": {str(i): p for i, p in self._track_programs.items()},
            "mode": self.mode,
            "parse_mode": self._parse_mode,
            "resolved_parse_mode": self._resolved_parse_mode,
        }

    def list_soundfont_presets(self) -> list:
        """Instruments the host's soundfont actually ships, for the UI's
        per-track instrument picker. [{bank, program, name}], can be empty."""
        try:
            return self.player.list_presets()
        except Exception:
            return []

    def load_song(self, query: str, mode: Optional[str] = None) -> dict:
        path = midi_utils.find_midi(self.library_dir, query)
        if path is None:
            return {"result": "error", "message": f"no midi matching '{query}'"}
        req_mode = (mode or "auto").lower()
        if req_mode not in ("auto", "track", "channel"):
            req_mode = "auto"
        try:
            data = path.read_bytes()
            info = midi_utils.parse_tracks(data, req_mode)
        except Exception as e:
            return {"result": "error", "message": f"failed to parse midi: {e}"}
        self._loaded_song = path.name
        self._loaded_path = path
        self._loaded_info = info
        self._parse_mode = req_mode
        self._resolved_parse_mode = info.get("parse_mode", "track")
        # assignments dont survive a song change, force re-assignment
        self._assignments = {}
        self._host_tracks = []
        self._track_programs = {}
        self._active_preset = None
        return {
            "result": "ok",
            "song": path.name,
            "duration": info["duration"],
            "tracks": info["tracks"],
            "parse_mode": self._resolved_parse_mode,
            "instruction": (
                "Now assign tracks to bandmates with assignBandTracks or call "
                "autoAssignBandTracks for an automatic split, then call startMidiBand."
            ),
        }

    def set_parse_mode(self, mode: str) -> dict:
        """Re-read the loaded song with a forced parse mode (auto/track/
        channel). Channel-organized files that read as all "Drums" by track
        come apart properly by channel. Resets assignments since the virtual
        track indices differ between modes."""
        if not self._loaded_song:
            return {"result": "error", "message": "no song loaded"}
        m = (mode or "auto").lower()
        if m not in ("auto", "track", "channel"):
            return {"result": "error", "message": "mode must be auto, track or channel"}
        return self.load_song(self._loaded_song, m)


    # ----- audio band mode -----

    def list_audio_songs(self) -> list:
        if self.audio_library is None:
            return []
        return self.audio_library.list_songs()

    def audio_song_info(self, name: str) -> Optional[dict]:
        if self.audio_library is None:
            return None
        return self.audio_library.song_info(name)

    def create_audio_song(self, name: str) -> dict:
        if self.audio_library is None:
            return {"result": "error", "message": "audio library unavailable"}
        return self.audio_library.create_song(name)

    def add_audio_stem(self, song: str, filename: str, data: bytes) -> dict:
        if self.audio_library is None:
            return {"result": "error", "message": "audio library unavailable"}
        res = self.audio_library.add_stem(song, filename, data)
        # if the song we just changed is the loaded one, refresh its tracks
        if res.get("result") == "ok" and self.mode == P.MODE_AUDIO and res.get("song") == self._loaded_song:
            self.load_audio_song(self._loaded_song)
        return res

    def rename_audio_stem(self, song: str, index: int, label: str) -> dict:
        if self.audio_library is None:
            return {"result": "error", "message": "audio library unavailable"}
        res = self.audio_library.rename_stem(song, index, label)
        if res.get("result") == "ok" and self.mode == P.MODE_AUDIO and res.get("song") == self._loaded_song:
            self.load_audio_song(self._loaded_song)
        return res

    def delete_audio_stem(self, song: str, index: int) -> dict:
        if self.audio_library is None:
            return {"result": "error", "message": "audio library unavailable"}
        res = self.audio_library.delete_stem(song, index)
        if res.get("result") == "ok" and self.mode == P.MODE_AUDIO and res.get("song") == self._loaded_song:
            self.load_audio_song(self._loaded_song)
        return res

    def delete_audio_song(self, name: str) -> dict:
        if self.audio_library is None:
            return {"result": "error", "message": "audio library unavailable"}
        res = self.audio_library.delete_song(name)
        if res.get("result") == "ok" and name == self._loaded_song:
            self._loaded_song = None
            self._loaded_info = None
            self._assignments = {}
            self._host_tracks = []
            self.on_change()
        return res

    def audio_stem_path(self, song: str, index: int) -> Optional[Path]:
        if self.audio_library is None:
            return None
        return self.audio_library.stem_path(song, index)

    def load_audio_song(self, name: str) -> dict:
        if self.audio_library is None:
            return {"result": "error", "message": "audio library unavailable"}
        info = self.audio_library.song_info(name)
        if info is None:
            return {"result": "error", "message": f"no audio song '{name}'"}
        stems = info.get("stems", [])
        if not stems:
            return {"result": "error", "message": "this song has no stems yet"}
        tracks = []
        for s in stems:
            lbl = s.get("label") or f"stem {s.get('index')}"
            tracks.append({
                "index": int(s.get("index")),
                "name": lbl,
                "display_label": lbl,
                "instrument": lbl,
                "note_count": 1,
                "duration": float(s.get("duration") or 0.0),
                "channel": None,
            })
        self.mode = P.MODE_AUDIO
        self._loaded_song = info["name"]
        self._loaded_path = None
        self._loaded_info = {"tracks": tracks, "duration": info.get("duration", 0.0)}
        self._assignments = {}
        self._host_tracks = []
        self._track_programs = {}
        self._active_preset = None
        self.on_change()
        return {
            "result": "ok",
            "song": info["name"],
            "duration": info.get("duration", 0.0),
            "tracks": tracks,
            "instruction": (
                "Now assign stems to bandmates with assignBandTracks or call "
                "autoAssignBandTracks for an automatic split, then call startMidiBand."
            ),
        }

    def assign_tracks(self, host_tracks: List[int],
                      client_assignments: Dict[str, List[int]]) -> dict:
        if not self._loaded_info:
            return {"result": "error", "message": "no song loaded, call loadMidiSong first"}
        max_idx = len(self._loaded_info["tracks"])

        def clean(lst):
            out = []
            for x in lst or []:
                try:
                    i = int(x)
                except Exception:
                    continue
                if 0 <= i < max_idx and i not in out:
                    out.append(i)
            return out

        self._host_tracks = clean(host_tracks)
        self._assignments = {}
        for k, v in (client_assignments or {}).items():
            cleaned = clean(v)
            if cleaned:
                self._assignments[str(k)] = cleaned
        # a hand-rolled assignment is no longer faithfully the saved preset
        self._active_preset = None

        if self._loop is not None and self._loop.is_running():
            self._loop.create_task(self._broadcast_assignments())
        self.on_change()
        return {
            "result": "ok",
            "host_tracks": self._host_tracks,
            "assignments": self._assignments,
            "broadcast_view": self._assignments_for_broadcast(),
        }

    def set_track_instrument(self, index, program, bank=0) -> dict:
        """Force a single track to play as a different instrument than the
        midi asked for. program None or < 0 clears the override. bank picks
        a soundfont variation bank (0 = General MIDI, 128 = drum kits).
        Takes effect on the next play."""
        if not self._loaded_info:
            return {"result": "error", "message": "no song loaded"}
        try:
            i = int(index)
        except Exception:
            return {"result": "error", "message": "bad track index"}
        if i < 0 or i >= len(self._loaded_info["tracks"]):
            return {"result": "error", "message": "track index out of range"}
        if program is None:
            self._track_programs.pop(i, None)
            self.on_change()
            return {"result": "ok", "index": i, "program": None}
        try:
            p = int(program)
        except Exception:
            return {"result": "error", "message": "program must be a number"}
        if p < 0:
            self._track_programs.pop(i, None)
            self.on_change()
            return {"result": "ok", "index": i, "program": None}
        if p > 127:
            return {"result": "error", "message": "program must be 0-127"}
        try:
            b = int(bank) if bank is not None else 0
        except Exception:
            b = 0
        if b < 0 or b > 128:
            b = 0
        self._track_programs[i] = {"bank": b, "program": p}
        self.on_change()
        return {"result": "ok", "index": i, "program": p, "bank": b}

    def auto_assign(self) -> dict:
        if not self._loaded_info:
            return {"result": "error", "message": "no song loaded"}
        playable = [t["index"] for t in self._loaded_info["tracks"] if t["note_count"] > 0]
        if not playable:
            return {"result": "error", "message": "this midi has no playable tracks"}
        members = [self.instance_name] + [p.name for p in self._peers.values()]
        if not members:
            return {"result": "error", "message": "no band members"}
        bucket: Dict[str, List[int]] = {m: [] for m in members}
        for i, ti in enumerate(playable):
            bucket[members[i % len(members)]].append(ti)
        host_tracks = bucket.pop(self.instance_name, [])
        return self.assign_tracks(host_tracks, bucket)

    def _conductor(self):
        if self._conductor_session is None:
            self._conductor_session = _conductor.ConductorSession(
                self._conductor_key_provider, self._conductor_model,
            )
        return self._conductor_session

    def conductor_reset(self) -> dict:
        """Forget the conductor's chat history and start fresh."""
        if self._conductor_session is not None:
            self._conductor_session.reset()
        return {"result": "ok"}

    async def ai_conduct_stream(self, prompt: str):
        """Stream one conductor turn. Yields event dicts the webui relays to
        the browser: text / tool / applied / error / done."""
        if not self._loaded_info:
            yield {"type": "error", "message": "load a song before conducting"}
            return
        async for ev in self._conductor().send(str(prompt or ""), self):
            yield ev

    # ----- conductor tool surface (called by ConductorSession via ctx) -----

    def _instrument_name(self, bank, program, presets=None) -> Optional[str]:
        try:
            b = int(bank)
            p = int(program)
        except Exception:
            return None
        if presets:
            for it in presets:
                if it.get("bank") == b and it.get("program") == p:
                    return it.get("name")
        if b == 0 and 0 <= p < len(midi_utils.GM_PROGRAMS):
            return midi_utils.GM_PROGRAMS[p]
        if b == 128:
            return f"Kit {p}"
        return f"bank {b} program {p}"

    def conductor_snapshot(self) -> dict:
        """Current band state in the shape conductor._state_block wants."""
        info = self._loaded_info or {}
        tracks_in = info.get("tracks", []) if info else []
        per_track: Dict[int, List[str]] = {}
        for ti in self._host_tracks:
            per_track.setdefault(int(ti), []).append(self.instance_name)
        for name, lst in self._assignments.items():
            for ti in (lst or []):
                per_track.setdefault(int(ti), []).append(name)
        presets = None
        tracks = []
        for t in tracks_in:
            idx = int(t.get("index"))
            label = t.get("display_label") or t.get("instrument") or t.get("name") or f"track {idx}"
            chans = t.get("channels")
            if not isinstance(chans, list):
                chans = [t.get("channel")] if t.get("channel") is not None else []
            drum = 9 in [c for c in chans if isinstance(c, int)]
            inst = None
            ov = self._track_programs.get(idx)
            if ov:
                if presets is None:
                    presets = self.list_soundfont_presets()
                inst = self._instrument_name(ov.get("bank", 0), ov.get("program", 0), presets)
            tracks.append({
                "index": idx,
                "label": label,
                "drum": bool(drum),
                "notes": int(t.get("note_count", 0) or 0),
                "members": per_track.get(idx, []),
                "instrument": inst,
            })
        members = [{"name": self.instance_name, "is_host": True}]
        for p in self._peers.values():
            members.append({"name": p.name, "is_host": False})
        return {
            "song": self._loaded_song,
            "mode": self.mode,
            "host_name": self.instance_name,
            "members": members,
            "tracks": tracks,
        }

    def conductor_list_instruments(self, query=None) -> list:
        presets = self.list_soundfont_presets()
        items = []
        if presets:
            for it in presets:
                items.append({
                    "name": it.get("name"),
                    "bank": it.get("bank"),
                    "program": it.get("program"),
                })
        else:
            for p, nm in enumerate(midi_utils.GM_PROGRAMS):
                items.append({"name": nm, "bank": 0, "program": p})
        q = str(query or "").strip().lower()
        if q:
            items = [it for it in items if q in str(it.get("name", "")).lower()]
        # keep the tool response bounded
        return items[:300]

    def _resolve_instrument(self, name):
        q = str(name or "").strip().lower()
        if not q:
            return None
        presets = self.list_soundfont_presets()
        pool = []
        if presets:
            for it in presets:
                pool.append((str(it.get("name", "")), int(it.get("bank", 0)), int(it.get("program", 0))))
        else:
            for p, nm in enumerate(midi_utils.GM_PROGRAMS):
                pool.append((nm, 0, p))
        for nm, b, p in pool:
            if nm.lower() == q:
                return (b, p)
        for nm, b, p in pool:
            if q in nm.lower():
                return (b, p)
        return None

    def conductor_apply_assignments(self, args) -> dict:
        if not self._loaded_info:
            return {"result": "error", "message": "no song is loaded", "_summary": "no song loaded"}
        raw = args.get("assignments") or []
        members = [self.instance_name] + [p.name for p in self._peers.values()]
        valid_idx = {int(t["index"]) for t in self._loaded_info["tracks"]
                     if int(t.get("note_count", 0) or 0) > 0}
        parsed = _conductor.parse_assignments(raw, members, self.instance_name, valid_idx)
        host_tracks = parsed["host_tracks"]
        client_assignments = parsed["client_assignments"]
        if not host_tracks and not client_assignments:
            return {"result": "error", "message": "that arrangement was empty",
                    "unknown_members": parsed["unknown_members"], "_summary": "couldn't place anyone"}
        applied = self.assign_tracks(host_tracks, client_assignments)
        if applied.get("result") != "ok":
            applied.setdefault("_summary", "couldn't set the arrangement")
            return applied
        reasoning = str(args.get("reasoning") or "").strip()
        # tell the model who ended up idle and which playable tracks are silent
        # so it can self-correct in a follow-up round instead of leaving gaps.
        busy = set(client_assignments.keys())
        if host_tracks:
            busy.add(self.instance_name)
        idle_members = [m for m in members if m not in busy]
        label_by_idx = {
            int(t["index"]): (t.get("display_label") or t.get("instrument") or f"track {t['index']}")
            for t in self._loaded_info["tracks"]
        }
        silent_tracks = [
            {"track": i, "label": label_by_idx.get(i, f"track {i}")}
            for i in sorted(valid_idx - parsed["used"])
        ]
        out = {
            "result": "ok",
            "host_tracks": host_tracks,
            "assignments": client_assignments,
            "idle_members": idle_members,
            "silent_tracks": silent_tracks,
            "unknown_members": parsed["unknown_members"],
            "_summary": reasoning or "set the arrangement",
        }
        if idle_members or silent_tracks:
            out["fix_hint"] = (
                "Some members are idle or playable tracks are silent. Unless the user "
                "wanted it sparse, call assignTracks again so every track is covered and "
                "members are used where you can. You may ONLY soak up spare members by "
                "doubling a choir or vocal voice (choir, 'aah', 'ooh'). Every other "
                "instrument stays unique, never double it. If there is nothing vocal to "
                "double, it is fine to leave an extra member idle."
            )
        return out

    def conductor_set_instrument(self, args) -> dict:
        if self.mode != P.MODE_MIDI:
            return {"result": "error", "message": "instrument changes only work in MIDI mode",
                    "_summary": "re-voicing is MIDI-mode only"}
        if not self._loaded_info:
            return {"result": "error", "message": "no song is loaded", "_summary": "no song loaded"}
        try:
            track = int(args.get("track"))
        except Exception:
            return {"result": "error", "message": "need a track index", "_summary": "no track given"}
        tracks = self._loaded_info["tracks"]
        if track < 0 or track >= len(tracks):
            return {"result": "error", "message": "that track index is out of range", "_summary": "bad track"}
        label = tracks[track].get("display_label") or tracks[track].get("instrument") or f"track {track}"
        if args.get("reset"):
            self.set_track_instrument(track, None)
            return {"result": "ok", "track": track, "_summary": f"reset {label} to its own sound"}
        bank = args.get("bank")
        program = args.get("program")
        name = str(args.get("instrument") or "").strip()
        if program is None and name:
            resolved = self._resolve_instrument(name)
            if resolved is None:
                return {"result": "error", "message": f"couldn't find a sound called '{name}'",
                        "_summary": f"no sound called '{name}'"}
            bank, program = resolved
        if program is None:
            return {"result": "error", "message": "name an instrument or give bank and program",
                    "_summary": "no instrument given"}
        res = self.set_track_instrument(track, program, bank if bank is not None else 0)
        if res.get("result") != "ok":
            res.setdefault("_summary", "couldn't change the sound")
            return res
        nm = self._instrument_name(res.get("bank", 0), res.get("program", 0))
        res["_summary"] = f"set {label} to {nm}"
        return res

    # ----- saved assignment presets -----

    def _clean_preset_name(self, name: str) -> str:
        raw = re.sub(r"\s+", " ", str(name or "").strip())
        raw = re.sub(r"[\x00-\x1f]", "", raw)
        return raw[:60].strip()

    def _load_presets(self):
        self._presets = {}
        if self._presets_path is None:
            return
        try:
            if self._presets_path.exists():
                data = json.loads(self._presets_path.read_text("utf-8")) or {}
                pr = data.get("presets") if isinstance(data, dict) else None
                if isinstance(pr, dict):
                    self._presets = pr
        except Exception as e:
            logger.warning(f"midi_band: could not read presets: {e}")

    def _save_presets(self):
        if self._presets_path is None:
            return
        try:
            self._presets_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._presets_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"presets": self._presets}, indent=2), "utf-8")
            tmp.replace(self._presets_path)
        except Exception as e:
            logger.warning(f"midi_band: could not save presets: {e}")

    def _load_song_names(self):
        self._song_names = {}
        if self._song_names_path is None:
            return
        try:
            if self._song_names_path.exists():
                data = json.loads(self._song_names_path.read_text("utf-8")) or {}
                names = data.get("names") if isinstance(data, dict) else None
                if isinstance(names, dict):
                    self._song_names = {str(k): str(v) for k, v in names.items() if v}
        except Exception as e:
            logger.warning(f"midi_band: could not read song names: {e}")

    def _save_song_names(self):
        if self._song_names_path is None:
            return
        try:
            self._song_names_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._song_names_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"names": self._song_names}, indent=2), "utf-8")
            tmp.replace(self._song_names_path)
        except Exception as e:
            logger.warning(f"midi_band: could not save song names: {e}")

    def song_display_name(self, filename: str) -> str:
        """The friendly name for a song file: a saved override if one exists,
        otherwise the filename with its .mid extension stripped."""
        fn = str(filename or "")
        if not fn:
            return ""
        return self._song_names.get(fn) or _strip_ext(fn)

    def song_display_label(self) -> str:
        """What the chatbox should show right now: the live preset's name if a
        preset is what's playing, else the loaded song's friendly name."""
        if not self._loaded_song:
            return ""
        if self._active_preset and self._active_preset in self._presets:
            return self._active_preset
        return self.song_display_name(self._loaded_song)

    def rename_song(self, filename: str, display: str) -> dict:
        """Give a midi file a friendly display name. An empty name (or one
        that matches the default) clears the override."""
        fn = str(filename or "").strip()
        if not fn:
            return {"result": "error", "message": "filename required"}
        if not (self.library_dir / fn).exists():
            return {"result": "error", "message": f"'{fn}' is not in the library"}
        disp = re.sub(r"\s+", " ", str(display or "").strip())
        disp = re.sub(r"[\x00-\x1f]", "", disp)[:80].strip()
        if disp and disp != _strip_ext(fn):
            self._song_names[fn] = disp
        else:
            self._song_names.pop(fn, None)
        self._save_song_names()
        self.on_change()
        return {"result": "ok", "file": fn, "display": self.song_display_name(fn)}

    def rename_preset(self, old: str, new: str) -> dict:
        """Rename a saved layout. Keeps its song + assignments, just changes
        the label shown in the UI and the chatbox."""
        src = None
        for key in (self._clean_preset_name(old), str(old or "").strip()):
            if key in self._presets:
                src = key
                break
        if src is None:
            return {"result": "error", "message": f"no preset named '{old}'"}
        dst = self._clean_preset_name(new)
        if not dst:
            return {"result": "error", "message": "new name required"}
        if dst == src:
            return {"result": "ok", "preset": src}
        if dst in self._presets:
            return {"result": "error", "message": f"a preset named '{dst}' already exists"}
        pr = self._presets.pop(src)
        pr["name"] = dst
        pr["updated"] = time.time()
        self._presets[dst] = pr
        if self._active_preset == src:
            self._active_preset = dst
        self._save_presets()
        self.on_change()
        return {"result": "ok", "preset": dst, "renamed_from": src}

    def list_presets(self) -> dict:
        present = set(self.list_clients())
        loaded = self._loaded_song
        out = []
        for name, pr in self._presets.items():
            pmode = pr.get("mode", P.MODE_MIDI)
            if pmode != self.mode:
                continue
            assignments = pr.get("assignments", {}) or {}
            mates = list(assignments.keys())
            missing = [m for m in mates if m not in present]
            song = pr.get("song")
            if pmode == P.MODE_AUDIO:
                available = bool(song) and self.audio_library is not None and (
                    self.audio_library.song_info(song) is not None
                )
            else:
                available = bool(song) and ((self.library_dir / song).exists() or song == loaded)
            count = len(pr.get("host_tracks", []) or []) + sum(len(v or []) for v in assignments.values())
            out.append({
                "name": name,
                "mode": pmode,
                "song": song,
                "members": mates,
                "track_count": count,
                "missing": missing,
                "ready": not missing,
                "song_loaded": song == loaded,
                "song_available": available,
                "updated": pr.get("updated"),
            })
        out.sort(key=lambda p: p.get("updated") or 0, reverse=True)
        return {"result": "ok", "presets": out, "members": sorted(present)}

    def save_preset(self, name: str) -> dict:
        if not self._loaded_info or not self._loaded_song:
            return {"result": "error", "message": "load a song before saving a preset"}
        clean = self._clean_preset_name(name)
        if not clean:
            return {"result": "error", "message": "preset name required"}
        now = time.time()
        prev = self._presets.get(clean) or {}
        self._presets[clean] = {
            "name": clean,
            "mode": self.mode,
            "song": self._loaded_song,
            "parse_mode": self._parse_mode,
            "host_tracks": list(self._host_tracks),
            "assignments": {k: list(v) for k, v in self._assignments.items()},
            "track_programs": {str(i): p for i, p in self._track_programs.items()},
            "created": prev.get("created", now),
            "updated": now,
        }
        self._save_presets()
        # saving the current layout makes it the live preset for the chatbox
        self._active_preset = clean
        return {"result": "ok", "preset": clean}

    def load_preset(self, name: str, force: bool = False) -> dict:
        clean = self._clean_preset_name(name)
        pr = self._presets.get(clean) or self._presets.get(str(name or "").strip())
        if pr is None:
            return {"result": "error", "message": f"no preset named '{name}'"}
        pmode = pr.get("mode", P.MODE_MIDI)
        song = pr.get("song")
        if not song:
            return {"result": "error", "message": "this preset has no song"}
        present = set(self.list_clients())
        assignments = pr.get("assignments", {}) or {}
        missing = [m for m in assignments.keys() if m not in present]
        if missing and not force:
            return {
                "result": "blocked",
                "code": "missing_members",
                "missing": missing,
                "message": "waiting on " + ", ".join(missing),
            }
        # flip into the preset's mode if needed (this clears any loaded song)
        if pmode != self.mode:
            sw = self.set_mode(pmode)
            if sw.get("result") != "ok":
                return sw
        # make sure the preset's song is the loaded one, in the same parse
        # mode, so the track indices line up
        p_parse = pr.get("parse_mode", "auto")
        need_reload = song != self._loaded_song
        if pmode != P.MODE_AUDIO and p_parse != self._parse_mode:
            need_reload = True
        if need_reload:
            if pmode == P.MODE_AUDIO:
                if self.audio_library is None or self.audio_library.song_info(song) is None:
                    return {"result": "error", "message": f"'{song}' is not in the audio library"}
                res = self.load_audio_song(song)
            else:
                if not (self.library_dir / song).exists():
                    return {"result": "error", "message": f"'{song}' is not in the library"}
                res = self.load_song(song, p_parse)
            if res.get("result") != "ok":
                return res
        # only hand out parts to bandmates who are actually here. anyone
        # missing leaves their tracks in the unassigned pool to reassign
        keep = {m: list(v) for m, v in assignments.items() if m in present}
        res = self.assign_tracks(list(pr.get("host_tracks", [])), keep)
        # restore instrument overrides for this song
        self._track_programs = {}
        ntracks = len(self._loaded_info["tracks"]) if self._loaded_info else 0
        for k, v in (pr.get("track_programs") or {}).items():
            try:
                ti = int(k)
            except Exception:
                continue
            np = midi_utils._norm_program(v)
            if np is None:
                continue
            b, p = np
            if 0 <= ti < ntracks:
                self._track_programs[ti] = {"bank": b, "program": p}
        orphan: List[int] = []
        for m in missing:
            orphan.extend(assignments.get(m, []) or [])
        # remember this preset is live so the chatbox shows its name
        self._active_preset = clean
        res.update({
            "preset": clean,
            "song": self._loaded_song,
            "missing": missing,
            "orphan_tracks": orphan,
            "forced": bool(missing),
        })
        return res

    def delete_preset(self, name: str) -> dict:
        for key in (self._clean_preset_name(name), str(name or "").strip()):
            if key in self._presets:
                self._presets.pop(key, None)
                if self._active_preset == key:
                    self._active_preset = None
                self._save_presets()
                return {"result": "ok", "deleted": key}
        return {"result": "error", "message": f"no preset named '{name}'"}

    def _assignments_for_broadcast(self) -> dict:
        if not self._loaded_info:
            return {}
        tracks = self._loaded_info["tracks"]

        def names_for(idxs):
            return [tracks[i]["name"] for i in idxs if 0 <= i < len(tracks)]

        out = {self.instance_name: names_for(self._host_tracks)}
        for name, idxs in self._assignments.items():
            out[name] = names_for(idxs)
        return out

    async def _broadcast_assignments(self):
        msg = P.encode({
            "type": P.ASSIGNMENTS,
            "assignments": self._assignments_for_broadcast(),
        })
        await self._broadcast(msg)

    async def start_playback(self) -> dict:
        if self._dispatch_lock is None:
            self._dispatch_lock = asyncio.Lock()
        async with self._dispatch_lock:
            if self.mode == P.MODE_AUDIO:
                return await self._start_audio_playback_locked()
            return await self._start_playback_locked()

    async def _start_playback_locked(self) -> dict:
        if not self._loaded_info or self._loaded_path is None:
            return {"result": "error", "message": "no song loaded"}

        # if nothing was assigned at all, default to host playing every
        # playable track (solo mode)
        if not self._assignments and not self._host_tracks:
            self._host_tracks = [t["index"] for t in self._loaded_info["tracks"] if t["note_count"] > 0]

        session = uuid.uuid4().hex[:12]
        try:
            file_bytes = self._loaded_path.read_bytes()
        except Exception as e:
            return {"result": "error", "message": f"failed to read midi: {e}"}
        file_b64 = midi_utils.load_midi_b64(self._loaded_path)
        duration = self._loaded_info["duration"]
        track_info = self._loaded_info["tracks"]

        # phase 1: send each peer their personal prepare with only their tracks
        peers_at_prepare = list(self._peers.values())
        for p in peers_at_prepare:
            p.ready_session = None
            p.nack_reason = None

        for peer in peers_at_prepare:
            tracks = self._assignments.get(peer.name, [])
            track_names = [track_info[i].get("display_label") or track_info[i]["name"] for i in tracks if 0 <= i < len(track_info)]
            peer_programs = {str(i): self._track_programs[i] for i in tracks if i in self._track_programs}
            try:
                peer.writer.write(P.encode({
                    "type": P.PREPARE,
                    "session": session,
                    "song": self._loaded_song,
                    "song_label": self.song_display_label(),
                    "file_b64": file_b64,
                    "tracks": tracks,
                    "track_names": track_names,
                    "track_programs": peer_programs,
                    "parse_mode": self._resolved_parse_mode,
                    "duration": duration,
                    "count_in_beats": self.count_in_beats,
                    "count_in_bpm": self.count_in_bpm,
                }))
                await peer.writer.drain()
            except Exception as e:
                logger.warning(f"midi_band: prepare to {peer.name} failed: {e}")
                peer.nack_reason = "send failed"
                peer.ready_session = session

        # phase 2: wait for everyone to ack
        if peers_at_prepare:
            deadline = _now() + READY_TIMEOUT
            while _now() < deadline:
                pending = sum(1 for p in peers_at_prepare if p.ready_session != session)
                if pending == 0:
                    break
                await asyncio.sleep(0.05)

        ready_count = sum(
            1 for p in peers_at_prepare
            if p.ready_session == session and p.nack_reason is None
        )
        nacks = [
            f"{p.name}: {p.nack_reason}"
            for p in peers_at_prepare if p.nack_reason
        ]

        # phase 3: pick start time and broadcast play
        start_at = _now() + self.lead
        play_msg = P.encode({
            "type": P.PLAY,
            "session": session,
            "start_at_server_t": start_at,
        })
        await self._broadcast(play_msg)

        # also schedule local host playback
        host_tracks = self._host_tracks
        host_track_names = [track_info[i].get("display_label") or track_info[i]["name"] for i in host_tracks if 0 <= i < len(track_info)]
        if host_tracks:
            host_programs = {i: self._track_programs[i] for i in host_tracks if i in self._track_programs}
            events = midi_utils.expand_track_events(file_bytes, host_tracks, host_programs, self._resolved_parse_mode)
            events, count_in_lead = midi_utils.with_count_in(
                events, self.count_in_beats, self.count_in_bpm
            )
            self.player.schedule(events, start_at, self._loaded_song,
                                 host_track_names, duration + count_in_lead,
                                 count_in_lead=count_in_lead)
        self.on_change()
        return {
            "result": "ok",
            "session": session,
            "song": self._loaded_song,
            "starts_in_seconds": round(self.lead, 2),
            "host_tracks": host_track_names,
            "peers_total": len(peers_at_prepare),
            "peers_ready": ready_count,
            "peers_nack": nacks,
        }

    async def _start_audio_playback_locked(self) -> dict:
        if not self._loaded_info or not self._loaded_song:
            return {"result": "error", "message": "no song loaded"}
        if self.audio_library is None or self.audio_player is None:
            return {"result": "error", "message": "audio mode unavailable"}
        if not self.audio_http_port:
            return {"result": "error", "message": "audio mode needs the Control Room web server enabled"}

        # nothing assigned: host plays every stem (solo mode)
        if not self._assignments and not self._host_tracks:
            self._host_tracks = [t["index"] for t in self._loaded_info["tracks"]]

        info = self.audio_library.song_info(self._loaded_song) or {"stems": []}
        by_index = {int(s.get("index")): s for s in info.get("stems", [])}
        duration = self._loaded_info["duration"]
        tracks = self._loaded_info["tracks"]
        session = uuid.uuid4().hex[:12]

        def stem_meta(idx):
            s = by_index.get(int(idx))
            if not s:
                return None
            ext = Path(s.get("file", "")).suffix.lstrip(".") or "wav"
            return {
                "index": int(idx),
                "label": s.get("label"),
                "sha": s.get("sha"),
                "ext": ext,
                "size": s.get("size"),
                "samplerate": s.get("samplerate"),
                "duration": s.get("duration"),
            }

        peers_at_prepare = list(self._peers.values())
        for p in peers_at_prepare:
            p.ready_session = None
            p.nack_reason = None

        for peer in peers_at_prepare:
            idxs = self._assignments.get(peer.name, [])
            stems = [m for m in (stem_meta(i) for i in idxs) if m]
            try:
                peer.writer.write(P.encode({
                    "type": P.PREPARE,
                    "mode": P.MODE_AUDIO,
                    "session": session,
                    "song": self._loaded_song,
                    "song_label": self.song_display_label(),
                    "http_port": int(self.audio_http_port),
                    "stems": stems,
                    "duration": duration,
                }))
                await peer.writer.drain()
            except Exception as e:
                logger.warning(f"midi_band: audio prepare to {peer.name} failed: {e}")
                peer.nack_reason = "send failed"
                peer.ready_session = session

        # mix the host's own stems while clients fetch theirs. run it off the
        # loop so decoding a fat wav doesn't stall peer acks coming in.
        host_tracks = self._host_tracks
        host_labels = [tracks[i].get("display_label") for i in host_tracks if 0 <= i < len(tracks)]
        host_mix, host_sr = None, 0
        if host_tracks:
            paths = [p for p in (self.audio_library.stem_path(self._loaded_song, i) for i in host_tracks) if p]
            try:
                host_mix, host_sr = await asyncio.get_running_loop().run_in_executor(
                    None, mix_stems, paths
                )
            except Exception as e:
                logger.error(f"midi_band: host audio mix failed: {e}")

        # wait for clients to ack once their stems are decoded and ready
        if peers_at_prepare:
            deadline = _now() + AUDIO_READY_TIMEOUT
            while _now() < deadline:
                pending = sum(1 for p in peers_at_prepare if p.ready_session != session)
                if pending == 0:
                    break
                await asyncio.sleep(0.1)

        ready_count = sum(
            1 for p in peers_at_prepare
            if p.ready_session == session and p.nack_reason is None
        )
        nacks = [f"{p.name}: {p.nack_reason}" for p in peers_at_prepare if p.nack_reason]

        start_at = _now() + self.lead
        await self._broadcast(P.encode({
            "type": P.PLAY,
            "session": session,
            "start_at_server_t": start_at,
        }))

        if host_mix is not None and len(host_mix):
            self.audio_player.schedule_audio(
                host_mix, host_sr, start_at, self._loaded_song, host_labels, duration
            )
        self.on_change()
        return {
            "result": "ok",
            "session": session,
            "song": self._loaded_song,
            "starts_in_seconds": round(self.lead, 2),
            "host_tracks": host_labels,
            "peers_total": len(peers_at_prepare),
            "peers_ready": ready_count,
            "peers_nack": nacks,
        }

    async def soundcheck(self, duration: float = 10.0, bpm: float = 120.0) -> dict:
        # synth-only sync test: each band member plays a percussion note on
        # alternating beats at given bpm, for `duration` seconds. doubles as
        # a fluidsynth + soundfont warmup so the first real song is snappy.
        beat_sec = 60.0 / max(30.0, float(bpm))
        total_beats = max(2, int(float(duration) / beat_sec))

        members = [self.instance_name] + [p.name for p in self._peers.values()]
        if not members:
            return {"result": "error", "message": "no band members"}

        # GM percussion notes on channel 9, distinct timbres per slot
        palette = [76, 77, 56, 81, 80, 75, 60, 61, 62, 63]

        plan: Dict[str, list] = {m: [] for m in members}
        for b in range(total_beats):
            slot = b % len(members)
            who = members[slot]
            note = palette[slot % len(palette)]
            plan[who].append([b * beat_sec, note, 9, 110, 0.12])

        session = uuid.uuid4().hex[:12]
        start_at = _now() + self.lead

        for peer in list(self._peers.values()):
            ticks = plan.get(peer.name) or []
            try:
                peer.writer.write(P.encode({
                    "type": P.SOUNDCHECK,
                    "session": session,
                    "start_at_server_t": start_at,
                    "ticks": ticks,
                    "duration": float(duration),
                    "bpm": float(bpm),
                }))
                await peer.writer.drain()
            except Exception as e:
                logger.warning(f"midi_band: soundcheck send to {peer.name} failed: {e}")

        host_ticks = plan.get(self.instance_name) or []
        if host_ticks:
            self.player.schedule_ticks(host_ticks, start_at, "soundcheck", float(duration))

        self.on_change()
        return {
            "result": "ok",
            "session": session,
            "starts_in_seconds": round(self.lead, 2),
            "duration": float(duration),
            "bpm": float(bpm),
            "members": members,
            "host_ticks": len(host_ticks),
            "peer_ticks": {n: len(plan[n]) for n in members if n != self.instance_name},
        }

    async def stop_playback(self) -> dict:
        try:
            self._active().stop_playback()
        except Exception:
            pass
        await self._broadcast(P.encode({"type": P.STOP}))
        self.on_change()
        return {"result": "ok"}

    async def pause_playback(self) -> dict:
        ok = False
        try:
            ok = self._active().pause()
        except Exception as e:
            logger.warning(f"midi_band: host pause failed: {e}")
        await self._broadcast(P.encode({"type": P.PAUSE}))
        self.on_change()
        return {"result": "ok" if ok else "noop", "paused_at": self._active().status().get("position")}

    async def resume_playback(self) -> dict:
        start_at = _now() + self.lead
        await self._broadcast(P.encode({
            "type": P.RESUME,
            "start_at_server_t": start_at,
        }))
        try:
            self._active().resume(start_at)
        except Exception as e:
            logger.warning(f"midi_band: host resume failed: {e}")
        self.on_change()
        return {"result": "ok", "starts_in_seconds": round(self.lead, 2)}

    async def set_volume(self, level: float) -> dict:
        # master fader: level is 0.0 (silent) to 2.0 (loud-ish), 0.5 is default.
        # sets everyone, host and clients, to the same gain. set both local
        # players so the level sticks across a mode switch.
        applied = self.player.set_gain(level)
        if self.audio_player is not None:
            try:
                self.audio_player.set_gain(level)
            except Exception:
                pass
        await self._broadcast(P.encode({"type": P.VOLUME, "gain": applied}))
        self.on_change()
        return {"result": "ok", "gain": applied}

    async def set_tone(self, on: bool, gain: Optional[float] = None) -> dict:
        # keep-warm hum: turn it on/off and set its level for everyone at
        # once. level is 0.0 to 1.0, applied on a reserved synth channel so
        # it rides under whatever song is playing without colliding.
        on = bool(on)
        if gain is not None:
            try:
                self._tone_gain = max(0.0, min(1.0, float(gain)))
            except (TypeError, ValueError):
                pass
        self._tone_on = on
        try:
            if on:
                self.player.start_tone(self._tone_gain)
            else:
                self.player.stop_tone()
        except Exception as e:
            logger.warning(f"midi_band: host tone toggle failed: {e}")
        await self._broadcast(P.encode({
            "type": P.TONE, "on": on, "gain": self._tone_gain,
        }))
        self.on_change()
        return {"result": "ok", "tone_on": self._tone_on, "tone_gain": self._tone_gain}

    def tone_status(self) -> dict:
        return {"on": self._tone_on, "gain": self._tone_gain}

    def get_sync_status(self) -> dict:
        """Snapshot of clock-sync health for every connected client."""
        now = _now()
        # build per-name list of instrument labels from current assignments
        track_names: List[str] = []
        if self._loaded_info:
            track_names = [t.get("display_label") or t.get("instrument") or t.get("name") or f"track {t.get('index')}" for t in self._loaded_info["tracks"]]
        def labels_for(name: str) -> List[str]:
            idxs = self._assignments.get(name) or []
            return [track_names[i] if 0 <= i < len(track_names) else f"track {i}" for i in idxs]
        host_labels = [track_names[i] if 0 <= i < len(track_names) else f"track {i}" for i in self._host_tracks]
        members = [{
            "name": self.instance_name,
            "is_host": True,
            "tracks": host_labels,
            "jitter_ms": 0.0,
            "rtt_ms": 0.0,
            "age_seconds": 0.0,
        }]
        for peer in self._peers.values():
            age = now - peer.sync_updated_at if peer.sync_updated_at else None
            members.append({
                "name": peer.name,
                "is_host": False,
                "tracks": labels_for(peer.name),
                "jitter_ms": round(peer.sync_jitter * 1000.0, 2),
                "rtt_ms": round(peer.sync_rtt * 1000.0, 2),
                "age_seconds": round(age, 2) if age is not None else None,
                "ready_session": peer.ready_session,
                "nack_reason": peer.nack_reason,
            })
        members.sort(key=lambda m: (not m["is_host"], m["name"]))
        return {
            "host": self.instance_name,
            "lead_seconds": round(self.lead, 3),
            "song": self._loaded_song,
            "members": members,
        }
