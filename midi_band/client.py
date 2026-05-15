"""TCP client for the midi_band protocol. Receives a midi file + a list
of track indices to play, schedules them through MidiPlayer.

Pure stdlib + sibling modules so the standalone client can use it
verbatim. Imports use `from . import x` to play nice as a package.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from pathlib import Path
from typing import Callable, List, Optional

from . import midi_utils
from . import protocol as P
from .player import MidiPlayer

logger = logging.getLogger(__name__)

PING_INTERVAL = 4.0
RECONNECT_BACKOFF_MAX = 30.0
PING_BURST_COUNT = 4
PING_BURST_GAP = 0.05


class BandClient:
    def __init__(
        self,
        host: str,
        port: int,
        name: str,
        player: MidiPlayer,
        cache_dir: Optional[Path] = None,
        on_change: Optional[Callable[[], None]] = None,
    ):
        self.host = host
        self.port = int(port)
        self.name = name or "musician"
        self.player = player
        self.cache_dir = cache_dir
        self.on_change = on_change or (lambda: None)

        self._writer: Optional[asyncio.StreamWriter] = None
        self._task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._stop = False
        self._connected = False
        self._server_offset = 0.0  # add to local monotonic to get server_t

        # cache of last prepare so play can use it
        self._pending_session: Optional[str] = None
        self._pending_file_b64: Optional[str] = None
        self._pending_tracks: List[int] = []
        self._pending_song: str = ""
        self._pending_track_names: List[str] = []
        self._pending_duration: float = 0.0

        # last assignments broadcast from host (everybody-on-the-band view)
        self._all_assignments: dict = {}

    # ----- lifecycle -----

    def start(self) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("midi_band client: no running loop")
            return False
        self._stop = False
        self._task = loop.create_task(self._loop())
        return True

    def stop(self):
        self._stop = True
        try:
            self.player.stop_playback()
        except Exception:
            pass
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
        if self._ping_task is not None:
            self._ping_task.cancel()

    def status(self) -> dict:
        s = self.player.status()
        return {
            "connected": self._connected,
            "name": self.name,
            "song": s["song"] or self._pending_song or None,
            "tracks": s["tracks"] or self._pending_track_names,
            "playing": s["playing"],
            "position": s["position"],
            "duration": s["duration"] or self._pending_duration,
            "assignments": dict(self._all_assignments),
            "server_offset": self._server_offset,
        }

    # ----- main loop -----

    async def _loop(self):
        backoff = 1.0
        while not self._stop:
            try:
                logger.info(f"midi_band: connecting to {self.host}:{self.port}")
                reader, writer = await asyncio.open_connection(
                    self.host, self.port, limit=P.READER_LIMIT
                )
                self._writer = writer
                writer.write(P.encode({"type": P.HELLO, "name": self.name, "kind": "client"}))
                await writer.drain()
                self._connected = True
                backoff = 1.0
                self.on_change()
                self._ping_task = asyncio.create_task(self._pinger())
                while not self._stop:
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        msg = P.decode(line)
                    except Exception:
                        continue
                    await self._on_msg(msg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"midi_band: client connection failed: {e}")
            finally:
                self._connected = False
                if self._ping_task is not None:
                    self._ping_task.cancel()
                    self._ping_task = None
                if self._writer is not None:
                    try:
                        self._writer.close()
                    except Exception:
                        pass
                    self._writer = None
                self.on_change()
            if self._stop:
                break
            await asyncio.sleep(min(backoff, RECONNECT_BACKOFF_MAX))
            backoff = min(backoff * 2.0, RECONNECT_BACKOFF_MAX)

    async def _pinger(self):
        try:
            while not self._stop and self._writer is not None:
                try:
                    self._writer.write(P.encode({"type": P.PING, "t": time.monotonic()}))
                    await self._writer.drain()
                except Exception:
                    return
                await asyncio.sleep(PING_INTERVAL)
        except asyncio.CancelledError:
            pass

    async def _ping_burst(self):
        if self._writer is None:
            return
        try:
            for _ in range(PING_BURST_COUNT):
                self._writer.write(P.encode({"type": P.PING, "t": time.monotonic()}))
                await self._writer.drain()
                await asyncio.sleep(PING_BURST_GAP)
        except Exception:
            pass

    # ----- inbound dispatch -----

    async def _on_msg(self, msg: dict):
        kind = msg.get("type")
        if kind == P.WELCOME:
            logger.info(f"midi_band: welcomed by host '{msg.get('name')}'")
            return
        if kind == P.PONG:
            try:
                t_sent = float(msg.get("t_in"))
                server_t = float(msg.get("server_t"))
                t_recv = time.monotonic()
                rtt = t_recv - t_sent
                est = server_t + rtt * 0.5
                offset = est - t_recv
                if self._server_offset == 0.0:
                    self._server_offset = offset
                else:
                    self._server_offset = self._server_offset * 0.7 + offset * 0.3
            except Exception:
                pass
            return
        if kind == P.PREPARE:
            await self._handle_prepare(msg)
            return
        if kind == P.PLAY:
            await self._handle_play(msg)
            return
        if kind == P.STOP:
            self.player.stop_playback()
            self.on_change()
            return
        if kind == P.SOUNDCHECK:
            await self._handle_soundcheck(msg)
            return
        if kind == P.ASSIGNMENTS:
            self._all_assignments = dict(msg.get("assignments") or {})
            self.on_change()
            return
        if kind == P.ERROR:
            logger.warning(f"midi_band: server error: {msg.get('message')}")

    async def _handle_prepare(self, msg: dict):
        session = str(msg.get("session") or "")
        song = str(msg.get("song") or "")
        file_b64 = str(msg.get("file_b64") or "")
        tracks = list(msg.get("tracks") or [])
        track_names = list(msg.get("track_names") or [])
        duration = float(msg.get("duration") or 0.0)
        writer = self._writer
        if writer is None:
            return
        if not file_b64:
            try:
                writer.write(P.encode({
                    "type": P.NACK, "session": session,
                    "reason": "missing midi data",
                }))
                await writer.drain()
            except Exception:
                pass
            return
        if not tracks:
            # not assigned anything this round, ack ready so the host doesnt
            # block waiting for us. we just wont play anything.
            try:
                writer.write(P.encode({"type": P.READY, "session": session}))
                await writer.drain()
            except Exception:
                pass
            return
        # cache the file so the user has a copy on disk
        if self.cache_dir and song:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                (self.cache_dir / song).write_bytes(base64.b64decode(file_b64))
            except Exception as e:
                logger.warning(f"midi_band: cache write failed: {e}")
        # stash for the play phase
        self._pending_session = session
        self._pending_file_b64 = file_b64
        self._pending_tracks = tracks
        self._pending_song = song
        self._pending_track_names = track_names
        self._pending_duration = duration
        # quick offset refresh, then ready
        await self._ping_burst()
        try:
            writer.write(P.encode({"type": P.READY, "session": session}))
            await writer.drain()
        except Exception:
            pass
        self.on_change()

    async def _handle_play(self, msg: dict):
        session = str(msg.get("session") or "")
        if session != self._pending_session:
            logger.warning(f"midi_band: play for unknown session {session}")
            return
        if not self._pending_tracks:
            # nothing to do this round
            return
        start_at_server = float(msg.get("start_at_server_t") or 0.0)
        local_start = start_at_server - self._server_offset
        try:
            file_bytes = base64.b64decode(self._pending_file_b64 or "")
            events = midi_utils.expand_track_events(file_bytes, self._pending_tracks)
        except Exception as e:
            logger.error(f"midi_band: failed to expand events: {e}")
            return
        self.player.schedule(
            events, local_start, self._pending_song,
            self._pending_track_names, self._pending_duration,
        )
        self.on_change()

    async def _handle_soundcheck(self, msg: dict):
        ticks = list(msg.get("ticks") or [])
        duration = float(msg.get("duration") or 10.0)
        start_at_server = float(msg.get("start_at_server_t") or 0.0)
        local_start = start_at_server - self._server_offset
        # quick offset refresh so the lead stays accurate
        await self._ping_burst()
        local_start = start_at_server - self._server_offset
        if not ticks:
            # no ticks for us this round, still warm the synth so the next
            # song starts instantly
            try:
                self.player.warmup()
            except Exception:
                pass
            return
        self.player.schedule_ticks(ticks, local_start, "soundcheck", duration)
        self.on_change()
