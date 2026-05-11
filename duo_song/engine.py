"""LAN duo engine. A duo song = two audio files (PT1 + PT2) that must
start at the same instant on two different machines. Host plays PT1,
client plays PT2. Each side only needs its OWN part on disk.

Wire format: newline delimited JSON.

    -> hello   {type, name}
    <- welcome {type, name}
    <-> ping   {type, t}
    <-> pong   {type, t_in, server_t}
    -> request_play {type, title}                          (client -> server)
    -> request_stop {type}
    <- prepare {type, session, title, peer_part}           (host -> client, get ready and tighten clock)
    -> ready   {type, session}                             (client -> host)
    -> nack    {type, session, reason}                     (client -> host)
    <- play    {type, session, title, peer_part, start_at_server_t, duration}
    <- stop    {type}

Sync flow on startDuoSong:
  1. host resolves the song to its PT1 + PT2 paths.
  2. host broadcasts prepare (peer_part = 2).
  3. each client verifies it has PT2, fires a tiny ping burst to refresh
     its clock offset, and acks ready.
  4. host waits up to READY_TIMEOUT for acks, then picks an absolute
     start_at_server_t a short lead in the future and broadcasts play.
  5. host schedules its own PT1 at start_at_server_t. Each client
     converts to local monotonic via the freshly tightened offset and
     schedules PT2 for the same instant.

Both pygame mixers fire against the same shared timestamp, so PT1 and
PT2 line up under typical LAN conditions (~30ms drift at start).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .library import find_pair, list_songs
from .player import Player

logger = logging.getLogger(__name__)

PING_INTERVAL = 4.0
RECONNECT_BACKOFF_MAX = 30.0
READY_TIMEOUT = 1.5          # how long host waits for peer ready acks
PING_BURST_COUNT = 4         # rapid pings on prepare to refresh clock offset
PING_BURST_GAP = 0.05


def _now() -> float:
    return time.monotonic()


def _encode(msg: dict) -> bytes:
    return (json.dumps(msg) + "\n").encode("utf-8")


def _probe_duration(path: Path) -> float:
    try:
        import pygame  # type: ignore
        snd = pygame.mixer.Sound(str(path))
        return float(snd.get_length())
    except Exception:
        return 0.0


class _Peer:
    """Server-side connected client record."""

    def __init__(self, name: str, writer: asyncio.StreamWriter):
        self.name = name
        self.writer = writer
        self.connected_at = _now()
        self.ready_session: Optional[str] = None
        self.nack_reason: Optional[str] = None


class DuoEngine:
    def __init__(
        self,
        role: str,
        instance_name: str,
        bind: str,
        host_address: str,
        port: int,
        library_dir: Path,
        schedule_lead_seconds: float = 1.2,
        volume: float = 0.6,
        auto_reconnect_seconds: float = 5.0,
        local_part: Optional[int] = None,
        on_change: Optional[Callable[[], None]] = None,
    ):
        self.role = role.lower().strip()
        self.instance_name = instance_name or "gabriel"
        self.bind = bind or "0.0.0.0"
        self.host_address = host_address or "127.0.0.1"
        self.port = int(port)
        self.library_dir = library_dir
        self.lead = max(0.5, float(schedule_lead_seconds))
        self.auto_reconnect = max(1.0, float(auto_reconnect_seconds))
        self.player = Player(volume=volume)
        self.on_change = on_change or (lambda: None)

        # which part this instance plays. config wins, otherwise derive from
        # role (host = PT1, client = PT2).
        if local_part in (1, 2):
            self.local_part = int(local_part)
        else:
            self.local_part = 1 if self.role == "host" else 2

        # server side
        self._server: Optional[asyncio.AbstractServer] = None
        self._peers: Dict[asyncio.StreamWriter, _Peer] = {}

        # client side
        self._client_writer: Optional[asyncio.StreamWriter] = None
        self._client_task: Optional[asyncio.Task] = None
        self._client_ping_task: Optional[asyncio.Task] = None
        self._server_offset: float = 0.0  # add to local monotonic to get server_t
        self._connected: bool = False
        self._stop = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self._current_title: Optional[str] = None
        self._current_duration: float = 0.0
        self._dispatch_lock: Optional[asyncio.Lock] = None

    # ----- lifecycle -----

    def start(self) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("duo_song: no running loop, cant start")
            return False
        self._loop = loop
        self._stop = False
        if self.role == "host":
            loop.create_task(self._serve())
        else:
            self._client_task = loop.create_task(self._client_loop())
        return True

    def stop(self):
        self._stop = True
        try:
            self.player.stop()
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
        if self._client_writer is not None:
            try:
                self._client_writer.close()
            except Exception:
                pass
        if self._client_task is not None:
            self._client_task.cancel()
        if self._client_ping_task is not None:
            self._client_ping_task.cancel()

    # ----- public API used by tools -----

    def list_songs(self) -> List[dict]:
        return list_songs(self.library_dir)

    def status(self) -> dict:
        ps = self.player.status()
        if self.role == "host":
            peers = [p.name for p in self._peers.values()]
            connected = True
        else:
            peers = ["host"] if self._connected else []
            connected = self._connected
        return {
            "role": self.role,
            "instance": self.instance_name,
            "local_part": self.local_part,
            "connected": connected,
            "peers": peers,
            "playing": ps["playing"],
            "title": ps["title"] or self._current_title,
            "position": ps["position"],
            "duration": ps["duration"] or self._current_duration,
            "server_offset": self._server_offset if self.role == "client" else 0.0,
        }

    async def request_play(self, title: str) -> dict:
        if self.role == "host":
            return await self._host_dispatch_play(title)
        return await self._client_send_request_play(title)

    async def request_stop(self) -> dict:
        if self.role == "host":
            await self._host_dispatch_stop()
            return {"result": "ok"}
        return await self._client_send_request_stop()

    # ----- host side -----

    async def _serve(self):
        try:
            self._server = await asyncio.start_server(self._handle_peer, self.bind, self.port)
            logger.info(f"duo_song: hosting on {self.bind}:{self.port}")
            async with self._server:
                await self._server.serve_forever()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"duo_song: server crashed: {e}")

    async def _handle_peer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer_addr = writer.get_extra_info("peername")
        peer_name = f"{peer_addr}"
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not line:
                writer.close()
                return
            try:
                hello = json.loads(line.decode("utf-8"))
            except Exception:
                writer.close()
                return
            if hello.get("type") != "hello":
                writer.close()
                return
            peer_name = str(hello.get("name") or peer_name)
            self._peers[writer] = _Peer(peer_name, writer)
            writer.write(_encode({"type": "welcome", "name": self.instance_name}))
            await writer.drain()
            logger.info(f"duo_song: peer connected: {peer_name}")
            self.on_change()

            while not self._stop:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                except Exception:
                    continue
                await self._on_peer_msg(writer, msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"duo_song: peer {peer_name} dropped: {e}")
        finally:
            self._peers.pop(writer, None)
            try:
                writer.close()
            except Exception:
                pass
            logger.info(f"duo_song: peer disconnected: {peer_name}")
            self.on_change()

    async def _on_peer_msg(self, writer: asyncio.StreamWriter, msg: dict):
        kind = msg.get("type")
        if kind == "ping":
            t_in = msg.get("t")
            writer.write(_encode({"type": "pong", "t_in": t_in, "server_t": _now()}))
            try:
                await writer.drain()
            except Exception:
                pass
            return
        if kind == "ready":
            peer = self._peers.get(writer)
            if peer is not None:
                peer.ready_session = str(msg.get("session") or "")
                peer.nack_reason = None
            return
        if kind == "nack":
            peer = self._peers.get(writer)
            if peer is not None:
                peer.ready_session = str(msg.get("session") or "")
                peer.nack_reason = str(msg.get("reason") or "declined")
                logger.warning(f"duo_song: peer '{peer.name}' nack: {peer.nack_reason}")
            return
        if kind == "request_play":
            await self._host_dispatch_play(str(msg.get("title") or ""))
            return
        if kind == "request_stop":
            await self._host_dispatch_stop()
            return

    async def _host_dispatch_play(self, title: str) -> dict:
        if self._dispatch_lock is None:
            self._dispatch_lock = asyncio.Lock()
        async with self._dispatch_lock:
            return await self._host_dispatch_play_locked(title)

    async def _host_dispatch_play_locked(self, title: str) -> dict:
        pair = find_pair(self.library_dir, title)
        if pair is None:
            return {"result": "error", "message": f"no song matching '{title}' in {self.library_dir}"}
        pt1: Optional[Path] = pair.get("pt1")  # type: ignore[assignment]
        pt2: Optional[Path] = pair.get("pt2")  # type: ignore[assignment]
        display = str(pair.get("title") or title)
        missing = []
        if pt1 is None:
            missing.append("PT1")
        if pt2 is None:
            missing.append("PT2")
        if missing:
            return {
                "result": "error",
                "message": f"song '{display}' is incomplete, missing: {', '.join(missing)}. Both PT1 and PT2 must exist in the library on the host.",
            }

        session = uuid.uuid4().hex[:12]
        duration = max(_probe_duration(pt1), _probe_duration(pt2))
        host_part = self.local_part
        peer_part = 2 if host_part == 1 else 1
        host_path: Path = pt1 if host_part == 1 else pt2  # type: ignore[assignment]

        # phase 1: prepare. Tell peers which part they sing.
        peers_at_prepare = list(self._peers.values())
        for p in peers_at_prepare:
            p.ready_session = None
            p.nack_reason = None
        await self._broadcast(_encode({
            "type": "prepare",
            "session": session,
            "title": display,
            "peer_part": peer_part,
        }))

        # phase 2: wait for ready acks.
        ready_count = 0
        nacks: List[str] = []
        if peers_at_prepare:
            deadline = _now() + READY_TIMEOUT
            while _now() < deadline:
                ready_count = sum(
                    1 for p in peers_at_prepare
                    if p.ready_session == session and p.nack_reason is None
                )
                nacks = [
                    f"{p.name}: {p.nack_reason}"
                    for p in peers_at_prepare
                    if p.ready_session == session and p.nack_reason
                ]
                pending = sum(1 for p in peers_at_prepare if p.ready_session != session)
                if pending == 0:
                    break
                await asyncio.sleep(0.05)

        # phase 3: pick the absolute start moment AFTER the handshake settles.
        start_at = _now() + self.lead
        self._current_title = display
        self._current_duration = duration
        await self._broadcast(_encode({
            "type": "play",
            "session": session,
            "title": display,
            "peer_part": peer_part,
            "start_at_server_t": start_at,
            "duration": duration,
        }))
        # host plays its configured part locally
        self.player.schedule_play(host_path, f"{display} (PT{host_part})", start_at, duration)
        self.on_change()

        return {
            "result": "ok",
            "title": display,
            "session": session,
            "starts_in_seconds": round(self.lead, 2),
            "host_plays": f"PT{host_part}",
            "peers_play": f"PT{peer_part}",
            "peers_total": len(peers_at_prepare),
            "peers_ready": ready_count,
            "peers_nack": nacks,
        }

    async def _host_dispatch_stop(self):
        self.player.stop()
        self._current_title = None
        self._current_duration = 0.0
        await self._broadcast(_encode({"type": "stop"}))
        self.on_change()

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

    # ----- client side -----

    async def _client_loop(self):
        backoff = 1.0
        while not self._stop:
            try:
                logger.info(f"duo_song: connecting to {self.host_address}:{self.port}")
                reader, writer = await asyncio.open_connection(self.host_address, self.port)
                self._client_writer = writer
                writer.write(_encode({"type": "hello", "name": self.instance_name}))
                await writer.drain()
                self._connected = True
                backoff = 1.0
                self.on_change()
                self._client_ping_task = asyncio.create_task(self._client_pinger())
                while not self._stop:
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except Exception:
                        continue
                    await self._on_server_msg(msg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"duo_song: client connection failed: {e}")
            finally:
                self._connected = False
                if self._client_ping_task is not None:
                    self._client_ping_task.cancel()
                    self._client_ping_task = None
                if self._client_writer is not None:
                    try:
                        self._client_writer.close()
                    except Exception:
                        pass
                    self._client_writer = None
                self.on_change()
            if self._stop:
                break
            await asyncio.sleep(min(backoff, RECONNECT_BACKOFF_MAX))
            backoff = min(backoff * 2.0, RECONNECT_BACKOFF_MAX)

    async def _client_pinger(self):
        try:
            while not self._stop and self._client_writer is not None:
                try:
                    self._client_writer.write(_encode({"type": "ping", "t": _now()}))
                    await self._client_writer.drain()
                except Exception:
                    return
                await asyncio.sleep(PING_INTERVAL)
        except asyncio.CancelledError:
            pass

    async def _on_server_msg(self, msg: dict):
        kind = msg.get("type")
        if kind == "welcome":
            logger.info(f"duo_song: welcomed by host '{msg.get('name')}'")
            return
        if kind == "pong":
            try:
                t_sent = float(msg.get("t_in"))
                server_t = float(msg.get("server_t"))
                t_recv = _now()
                rtt = t_recv - t_sent
                est_server_now = server_t + (rtt * 0.5)
                offset = est_server_now - t_recv
                if self._server_offset == 0.0:
                    self._server_offset = offset
                else:
                    self._server_offset = (self._server_offset * 0.7) + (offset * 0.3)
            except Exception:
                pass
            return
        if kind == "prepare":
            await self._handle_prepare(msg)
            return
        if kind == "play":
            await self._handle_play(msg)
            return
        if kind == "stop":
            self.player.stop()
            self._current_title = None
            self._current_duration = 0.0
            self.on_change()
            return
        if kind == "error":
            logger.warning(f"duo_song: host error: {msg.get('message')}")

    async def _handle_prepare(self, msg: dict):
        session = str(msg.get("session") or "")
        title = str(msg.get("title") or "")
        peer_part = int(msg.get("peer_part") or 2)
        writer = self._client_writer
        if writer is None:
            return

        # if the user configured this instance for a specific part, refuse
        # if the host is asking for the other one. Avoids both sides accidentally
        # singing PT1 because of a misconfig.
        if peer_part != self.local_part:
            try:
                writer.write(_encode({
                    "type": "nack",
                    "session": session,
                    "reason": (
                        f"part mismatch: host asked for PT{peer_part} but this instance is "
                        f"configured for PT{self.local_part}"
                    ),
                }))
                await writer.drain()
            except Exception:
                pass
            return

        pair = find_pair(self.library_dir, title)
        local_path: Optional[Path] = None
        if pair is not None:
            local_path = pair.get("pt2" if peer_part == 2 else "pt1")  # type: ignore[assignment]
        if local_path is None or not local_path.exists():
            try:
                writer.write(_encode({
                    "type": "nack",
                    "session": session,
                    "reason": f"missing PT{peer_part} for '{title}' in local library",
                }))
                await writer.drain()
            except Exception:
                pass
            return

        # ping burst to tighten clock offset right before commit
        try:
            for _ in range(PING_BURST_COUNT):
                writer.write(_encode({"type": "ping", "t": _now()}))
                await writer.drain()
                await asyncio.sleep(PING_BURST_GAP)
        except Exception:
            pass
        try:
            writer.write(_encode({"type": "ready", "session": session}))
            await writer.drain()
        except Exception:
            pass

    async def _handle_play(self, msg: dict):
        title = str(msg.get("title") or "")
        peer_part = int(msg.get("peer_part") or 2)
        start_at_server = float(msg.get("start_at_server_t") or 0.0)
        duration = float(msg.get("duration") or 0.0)
        local_start = start_at_server - self._server_offset
        pair = find_pair(self.library_dir, title)
        if pair is None:
            logger.warning(f"duo_song: host wants to play '{title}' but its missing locally")
            return
        path: Optional[Path] = pair.get("pt2" if peer_part == 2 else "pt1")  # type: ignore[assignment]
        if path is None or not path.exists():
            logger.warning(f"duo_song: missing PT{peer_part} of '{title}' on this side")
            return
        self._current_title = title
        self._current_duration = duration
        self.player.schedule_play(path, f"{title} (PT{peer_part})", local_start, duration)
        self.on_change()

    async def _client_send_request_play(self, title: str) -> dict:
        if self._client_writer is None or not self._connected:
            return {"result": "error", "message": "not connected to duo host"}
        try:
            self._client_writer.write(_encode({"type": "request_play", "title": title}))
            await self._client_writer.drain()
            return {"result": "ok", "message": f"asked host to start duet for '{title}'"}
        except Exception as e:
            return {"result": "error", "message": f"send failed: {e}"}

    async def _client_send_request_stop(self) -> dict:
        if self._client_writer is None or not self._connected:
            return {"result": "error", "message": "not connected to duo host"}
        try:
            self._client_writer.write(_encode({"type": "request_stop"}))
            await self._client_writer.drain()
            return {"result": "ok"}
        except Exception as e:
            return {"result": "error", "message": f"send failed: {e}"}
