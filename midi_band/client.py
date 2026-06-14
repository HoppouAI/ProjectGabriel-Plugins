"""TCP client for the midi_band protocol. Receives a midi file + a list
of track indices to play, schedules them through MidiPlayer.

Pure stdlib + sibling modules so the standalone client can use it
verbatim. Imports use `from . import x` to play nice as a package.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import logging
import time
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import quote

from . import midi_utils
from . import protocol as P
from .player import MidiPlayer

logger = logging.getLogger(__name__)

PING_INTERVAL = 4.0
RECONNECT_BACKOFF_MAX = 30.0
PING_BURST_COUNT = 4
PING_BURST_GAP = 0.05
SYNC_SAMPLE_WINDOW = 16  # NTP-style minimum filter window
SYNC_LOCK_SAMPLES = 4    # take best directly until we have this many samples
SYNC_CREEP_PER_SAMPLE = 0.005  # max 5ms shift per pong once locked
SYNC_NUDGE_THRESHOLD_S = 0.020  # ignore drift below 20ms
SYNC_NUDGE_MAX_PER_TICK = 0.040 # never shift playhead more than 40ms per tick
SYNC_HARD_JUMP_THRESHOLD_S = 0.500  # over half a second drift = hard reseek


class BandClient:
    def __init__(
        self,
        host: str,
        port: int,
        name: str,
        player: MidiPlayer,
        cache_dir: Optional[Path] = None,
        on_change: Optional[Callable[[], None]] = None,
        audio_player=None,
    ):
        self.host = host
        self.port = int(port)
        self.name = name or "musician"
        self.player = player
        self.audio_player = audio_player
        self.cache_dir = cache_dir
        self.on_change = on_change or (lambda: None)

        self._writer: Optional[asyncio.StreamWriter] = None
        self._task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._stop = False
        self._connected = False
        self._server_offset = 0.0  # add to local monotonic to get server_t

        # which player the current session drives. flips to the audio player
        # when a PREPARE arrives tagged mode=audio.
        self._active = player
        self._pending_mode: str = P.MODE_MIDI

        # cache of last prepare so play can use it
        self._pending_session: Optional[str] = None
        self._pending_file_b64: Optional[str] = None
        self._pending_tracks: List[int] = []
        self._pending_song: str = ""
        self._pending_track_names: List[str] = []
        self._pending_track_programs: dict = {}
        self._pending_duration: float = 0.0
        self._pending_count_in_beats: int = 0
        self._pending_count_in_bpm: float = 120.0
        # audio session staging
        self._pending_mix = None
        self._pending_sr: int = 0
        self._last_rtt: float = 0.0
        self._last_jitter: float = 0.0  # deviation of latest offset sample from smoothed
        self._sync_samples: collections.deque = collections.deque(maxlen=SYNC_SAMPLE_WINDOW)

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
        if self.audio_player is not None:
            try:
                self.audio_player.stop_playback()
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
        s = self._active.status()
        return {
            "connected": self._connected,
            "name": self.name,
            "song": s["song"] or self._pending_song or None,
            "tracks": s["tracks"] or self._pending_track_names,
            "playing": s["playing"],
            "paused": s.get("paused"),
            "position": s["position"],
            "duration": s["duration"] or self._pending_duration,
            "in_count_in": s.get("in_count_in"),
            "count_in_remaining": s.get("count_in_remaining"),
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
                    self._writer.write(P.encode({
                        "type": P.PING,
                        "t": time.monotonic(),
                        "client_jitter": self._last_jitter,
                        "client_rtt": self._last_rtt,
                    }))
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
                self._writer.write(P.encode({
                    "type": P.PING,
                    "t": time.monotonic(),
                    "client_jitter": self._last_jitter,
                    "client_rtt": self._last_rtt,
                }))
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
                self._last_rtt = rtt
                est = server_t + rtt * 0.5
                offset_sample = est - t_recv
                self._sync_samples.append((rtt, offset_sample))
                # NTP-style "minimum filter": pick the sample with lowest RTT,
                # because that one had the most symmetric path so its
                # offset estimate is the most trustworthy.
                best_rtt, best_offset = min(self._sync_samples, key=lambda s: s[0])
                if len(self._sync_samples) <= SYNC_LOCK_SAMPLES:
                    new_offset = best_offset
                else:
                    delta = best_offset - self._server_offset
                    if delta > SYNC_CREEP_PER_SAMPLE:
                        delta = SYNC_CREEP_PER_SAMPLE
                    elif delta < -SYNC_CREEP_PER_SAMPLE:
                        delta = -SYNC_CREEP_PER_SAMPLE
                    new_offset = self._server_offset + delta
                self._last_jitter = offset_sample - best_offset
                self._server_offset = new_offset
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
            self._active.stop_playback()
            self.on_change()
            return
        if kind == P.PAUSE:
            try:
                self._active.pause()
            except Exception as e:
                logger.warning(f"midi_band: client pause failed: {e}")
            self.on_change()
            return
        if kind == P.RESUME:
            start_at_server = float(msg.get("start_at_server_t") or 0.0)
            local_start = start_at_server - self._server_offset
            try:
                self._active.resume(local_start)
            except Exception as e:
                logger.warning(f"midi_band: client resume failed: {e}")
            self.on_change()
            return
        if kind == P.VOLUME:
            try:
                g = msg.get("gain")
                # don't let a 0 (full mute) fall through to a default
                gain = 0.5 if g is None else float(g)
                self.player.set_gain(gain)
                if self.audio_player is not None:
                    self.audio_player.set_gain(gain)
            except Exception:
                pass
            self.on_change()
            return
        if kind == P.SYNC_TICK:
            self._handle_sync_tick(msg)
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

    def _handle_sync_tick(self, msg: dict):
        if not self._active.is_playing():
            return
        try:
            server_t = float(msg.get("server_t"))
            server_pos = float(msg.get("pos"))
        except Exception:
            return
        # what does the server think the song position is right at this exact local instant?
        local_now = time.monotonic()
        server_now = local_now + self._server_offset
        expected_pos = server_pos + (server_now - server_t)
        actual_pos = self._active.current_position()
        drift = expected_pos - actual_pos  # positive => we're behind
        ad = abs(drift)
        if ad < SYNC_NUDGE_THRESHOLD_S:
            return
        if ad >= SYNC_HARD_JUMP_THRESHOLD_S:
            # too far gone for a smooth nudge, hard re-seek to the server's
            # current position immediately. Restart playback from there.
            try:
                self._active.seek_to(expected_pos, start_at_local=local_now)
                logger.info(f"midi_band: hard re-sync, jumped {drift*1000:.0f}ms")
            except Exception as e:
                logger.warning(f"midi_band: hard re-sync failed: {e}")
            return
        # smooth nudge: move scheduled_start in the right direction, capped
        # so we never make it audible (40ms per tick max).
        step = max(-SYNC_NUDGE_MAX_PER_TICK, min(SYNC_NUDGE_MAX_PER_TICK, drift))
        # if we're behind (drift > 0), shift scheduled_start EARLIER => events fire sooner
        try:
            self._active.nudge(-step)
        except Exception:
            pass

    async def _handle_prepare(self, msg: dict):
        if str(msg.get("mode") or P.MODE_MIDI) == P.MODE_AUDIO:
            return await self._handle_prepare_audio(msg)
        # midi session, make sure the active player is the synth
        self._active = self.player
        self._pending_mode = P.MODE_MIDI
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
        self._pending_track_programs = dict(msg.get("track_programs") or {})
        self._pending_duration = duration
        self._pending_count_in_beats = int(msg.get("count_in_beats") or 0)
        self._pending_count_in_bpm = float(msg.get("count_in_bpm") or 120.0)
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
        if self._pending_mode == P.MODE_AUDIO:
            return await self._handle_play_audio(msg)
        if not self._pending_tracks:
            # nothing to do this round
            return
        start_at_server = float(msg.get("start_at_server_t") or 0.0)
        local_start = start_at_server - self._server_offset
        try:
            file_bytes = base64.b64decode(self._pending_file_b64 or "")
            events = midi_utils.expand_track_events(
                file_bytes, self._pending_tracks, self._pending_track_programs
            )
            events, count_in_lead = midi_utils.with_count_in(
                events,
                self._pending_count_in_beats,
                self._pending_count_in_bpm,
            )
        except Exception as e:
            logger.error(f"midi_band: failed to expand events: {e}")
            return
        self.player.schedule(
            events, local_start, self._pending_song,
            self._pending_track_names, self._pending_duration + count_in_lead,
            count_in_lead=count_in_lead,
        )
        self.on_change()

    # ----- audio band mode -----

    async def _handle_prepare_audio(self, msg: dict):
        session = str(msg.get("session") or "")
        song = str(msg.get("song") or "")
        http_port = int(msg.get("http_port") or 0)
        duration = float(msg.get("duration") or 0.0)
        stems = list(msg.get("stems") or [])
        writer = self._writer
        if writer is None:
            return
        self._active = self.audio_player or self.player
        self._pending_mode = P.MODE_AUDIO
        self._pending_session = session
        self._pending_song = song
        self._pending_duration = duration
        self._pending_track_names = [str(s.get("label") or "") for s in stems]
        self._pending_mix = None
        self._pending_sr = 0
        if not stems:
            # not assigned any stems this round, ack so the host doesnt block.
            try:
                writer.write(P.encode({"type": P.READY, "session": session}))
                await writer.drain()
            except Exception:
                pass
            return
        if self.audio_player is None or not self.audio_player.available():
            try:
                writer.write(P.encode({
                    "type": P.NACK, "session": session,
                    "reason": "audio support not installed",
                }))
                await writer.drain()
            except Exception:
                pass
            return
        try:
            loop = asyncio.get_running_loop()
            mix, sr = await loop.run_in_executor(
                None, self._prepare_audio_blocking, song, http_port, stems
            )
        except Exception as e:
            logger.error(f"midi_band: audio prepare failed: {e}")
            try:
                writer.write(P.encode({
                    "type": P.NACK, "session": session,
                    "reason": "stem fetch failed",
                }))
                await writer.drain()
            except Exception:
                pass
            return
        if mix is None:
            try:
                writer.write(P.encode({
                    "type": P.NACK, "session": session,
                    "reason": "stem decode failed",
                }))
                await writer.drain()
            except Exception:
                pass
            return
        self._pending_mix = mix
        self._pending_sr = sr
        # quick offset refresh, then ready
        await self._ping_burst()
        try:
            writer.write(P.encode({"type": P.READY, "session": session}))
            await writer.drain()
        except Exception:
            pass
        self.on_change()

    async def _handle_play_audio(self, msg: dict):
        if self._pending_mix is None or self.audio_player is None:
            return
        start_at_server = float(msg.get("start_at_server_t") or 0.0)
        local_start = start_at_server - self._server_offset
        try:
            self.audio_player.schedule_audio(
                self._pending_mix, self._pending_sr, local_start,
                self._pending_song, self._pending_track_names,
                self._pending_duration,
            )
        except Exception as e:
            logger.error(f"midi_band: audio schedule failed: {e}")
            return
        self.on_change()

    def _stem_cache_dir(self) -> Path:
        base = Path(self.cache_dir) if self.cache_dir else Path("audio_cache")
        return base / "stems"

    def _prepare_audio_blocking(self, song: str, http_port: int, stems: list):
        # runs in a thread: download any stems we dont already have cached
        # (keyed by sha so identical files are reused) then mix them down.
        from .audio_player import mix_stems
        cache = self._stem_cache_dir()
        cache.mkdir(parents=True, exist_ok=True)
        paths = []
        for s in stems:
            idx = int(s.get("index"))
            sha = str(s.get("sha") or "")
            ext = str(s.get("ext") or "wav").lstrip(".")
            size = int(s.get("size") or 0)
            fname = (sha or f"{idx}") + "." + ext
            dest = cache / fname
            if not dest.exists() or (size and dest.stat().st_size != size):
                url = (
                    f"http://{self.host}:{http_port}/api/stem"
                    f"?song={quote(song)}&index={idx}"
                )
                self._download_stem(url, dest)
            paths.append(dest)
        return mix_stems(paths)

    def _download_stem(self, url: str, dest: Path):
        req = urllib.request.Request(url, headers={"User-Agent": "midi_band-client"})
        tmp = dest.with_suffix(dest.suffix + ".part")
        with urllib.request.urlopen(req, timeout=120) as resp:  # nosec B310 - http to configured band host on LAN
            with tmp.open("wb") as f:
                for chunk in iter(lambda: resp.read(1 << 16), b""):
                    f.write(chunk)
        tmp.replace(dest)

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
