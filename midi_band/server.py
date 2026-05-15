"""TCP server (host side) for the midi_band protocol.

Owns the loaded song, the per-client track assignments, and the
prepare/ready/play handshake. Runs the host's own playback locally for
its share of tracks. No Project Gabriel imports so it works the same
way under the plugin or any other harness.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import midi_utils
from . import protocol as P
from .player import MidiPlayer

logger = logging.getLogger(__name__)

# bigger window than duo_song since we ship the full midi over the wire
READY_TIMEOUT = 3.0


def _now() -> float:
    return time.monotonic()


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

        self._server: Optional[asyncio.AbstractServer] = None
        self._peers: Dict[asyncio.StreamWriter, _Peer] = {}
        self._stop = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._dispatch_lock: Optional[asyncio.Lock] = None

        # currently loaded song state
        self._loaded_song: Optional[str] = None
        self._loaded_path: Optional[Path] = None
        self._loaded_info: Optional[dict] = None
        self._assignments: Dict[str, List[int]] = {}
        self._host_tracks: List[int] = []

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
                    if not self.player.is_playing():
                        continue
                    pos = self.player.current_position()
                    payload = P.encode({
                        "type": P.SYNC_TICK,
                        "server_t": _now(),
                        "pos": pos,
                    })
                    await self._broadcast(payload)
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
            while not self._stop:
                line = await reader.readline()
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
                "assignments": {}, "host_tracks": [],
            }
        return {
            "song": self._loaded_song,
            "tracks": self._loaded_info["tracks"],
            "duration": self._loaded_info["duration"],
            "assignments": dict(self._assignments),
            "host_tracks": list(self._host_tracks),
        }

    def load_song(self, query: str) -> dict:
        path = midi_utils.find_midi(self.library_dir, query)
        if path is None:
            return {"result": "error", "message": f"no midi matching '{query}'"}
        try:
            data = path.read_bytes()
            info = midi_utils.parse_tracks(data)
        except Exception as e:
            return {"result": "error", "message": f"failed to parse midi: {e}"}
        self._loaded_song = path.name
        self._loaded_path = path
        self._loaded_info = info
        # assignments dont survive a song change, force re-assignment
        self._assignments = {}
        self._host_tracks = []
        return {
            "result": "ok",
            "song": path.name,
            "duration": info["duration"],
            "tracks": info["tracks"],
            "instruction": (
                "Now assign tracks to bandmates with assignBandTracks or call "
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

        if self._loop is not None and self._loop.is_running():
            self._loop.create_task(self._broadcast_assignments())
        self.on_change()
        return {
            "result": "ok",
            "host_tracks": self._host_tracks,
            "assignments": self._assignments,
            "broadcast_view": self._assignments_for_broadcast(),
        }

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
            try:
                peer.writer.write(P.encode({
                    "type": P.PREPARE,
                    "session": session,
                    "song": self._loaded_song,
                    "file_b64": file_b64,
                    "tracks": tracks,
                    "track_names": track_names,
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
            events = midi_utils.expand_track_events(file_bytes, host_tracks)
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
            self.player.stop_playback()
        except Exception:
            pass
        await self._broadcast(P.encode({"type": P.STOP}))
        self.on_change()
        return {"result": "ok"}

    async def pause_playback(self) -> dict:
        ok = False
        try:
            ok = self.player.pause()
        except Exception as e:
            logger.warning(f"midi_band: host pause failed: {e}")
        await self._broadcast(P.encode({"type": P.PAUSE}))
        self.on_change()
        return {"result": "ok" if ok else "noop", "paused_at": self.player.status().get("position")}

    async def resume_playback(self) -> dict:
        start_at = _now() + self.lead
        await self._broadcast(P.encode({
            "type": P.RESUME,
            "start_at_server_t": start_at,
        }))
        try:
            self.player.resume(start_at)
        except Exception as e:
            logger.warning(f"midi_band: host resume failed: {e}")
        self.on_change()
        return {"result": "ok", "starts_in_seconds": round(self.lead, 2)}

    async def set_volume(self, level: float) -> dict:
        # level is 0.0 (silent) to 2.0 (loud-ish), 0.5 is the default
        applied = self.player.set_gain(level)
        await self._broadcast(P.encode({"type": P.VOLUME, "gain": applied}))
        self.on_change()
        return {"result": "ok", "gain": applied}

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
