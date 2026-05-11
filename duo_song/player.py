"""pygame.mixer wrapper. We deliberately use mixer.Sound on a dedicated
reserved Channel instead of mixer.music, because the host already uses
mixer.music for its own local music tool and the two would fight each
other (calling music.play here was silently stopping the host's music
or vice versa, no audio came out either way).

Reserving a channel gives us our own independent playback path that
runs in parallel with anything else the host wants to do.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# pick a channel index well outside what pygame allocates by default (8).
# We grow the mixer to fit and reserve it.
DUO_CHANNEL_INDEX = 15
TOTAL_CHANNELS = 16

_pygame = None
_init_err: Optional[str] = None
_channel = None


def _ensure_pygame():
    global _pygame, _init_err, _channel
    if _pygame is not None and _channel is not None:
        return _pygame
    try:
        import pygame  # type: ignore
        # init pygame core too, on some windows builds the mixer wont produce
        # output until pygame.init() has run at least once.
        if not pygame.get_init():
            pygame.init()
        if not pygame.mixer.get_init():
            # let SDL pick the device defaults, dont force a sample rate that
            # might not match the user's audio device.
            pygame.mixer.init()
        # make room for our reserved channel
        if pygame.mixer.get_num_channels() < TOTAL_CHANNELS:
            pygame.mixer.set_num_channels(TOTAL_CHANNELS)
        ch = pygame.mixer.Channel(DUO_CHANNEL_INDEX)
        # mark it reserved so other pygame code doesnt steal it
        try:
            pygame.mixer.set_reserved(DUO_CHANNEL_INDEX + 1)
        except Exception:
            pass
        _pygame = pygame
        _channel = ch
        logger.info(f"duo_song: pygame mixer ready on channel {DUO_CHANNEL_INDEX}")
        return pygame
    except Exception as e:
        _init_err = str(e)
        logger.error(f"duo_song: pygame mixer init failed: {e}")
        return None


class Player:
    def __init__(self, volume: float = 0.6):
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._current_path: Optional[Path] = None
        self._current_title: Optional[str] = None
        self._duration: float = 0.0
        self._started_at: float = 0.0
        self._volume = max(0.0, min(1.0, float(volume)))
        self._sound = None  # holds the Sound so it isnt GC'd mid-playback

    def available(self) -> bool:
        return _ensure_pygame() is not None

    def schedule_play(self, path: Path, title: str, start_at_local: float, duration: float = 0.0) -> bool:
        pg = _ensure_pygame()
        if pg is None:
            return False
        if not path.exists():
            logger.warning(f"duo_song: file missing: {path}")
            return False
        # preload now so the timer thread doesnt hit disk at fire time
        try:
            sound = pg.mixer.Sound(str(path))
        except Exception as e:
            logger.error(f"duo_song: failed to load {path}: {e}")
            return False
        with self._lock:
            self._cancel_timer_locked()
            self._stop_channel_locked()
            self._current_path = path
            self._current_title = title
            self._duration = float(duration or sound.get_length())
            self._started_at = float(start_at_local)
            self._sound = sound
        delay = max(0.0, start_at_local - time.monotonic())
        t = threading.Timer(delay, self._fire, args=(title,))
        t.daemon = True
        with self._lock:
            self._timer = t
        t.start()
        logger.info(f"duo_song: scheduled '{title}' in {delay:.2f}s")
        return True

    def _fire(self, title: str):
        global _channel
        pg = _ensure_pygame()
        if pg is None:
            return
        with self._lock:
            sound = self._sound
        if sound is None:
            return
        try:
            ch = _channel
            if ch is None:
                logger.error("duo_song: no reserved channel, cant play")
                return
            ch.set_volume(self._volume)
            ch.play(sound)
            with self._lock:
                self._started_at = time.monotonic()
            logger.info(f"duo_song: playing '{title}'")
        except Exception as e:
            logger.error(f"duo_song: failed to play '{title}': {e}")

    def stop(self):
        with self._lock:
            self._cancel_timer_locked()
            self._stop_channel_locked()
            self._current_path = None
            self._current_title = None
            self._duration = 0.0
            self._sound = None

    def _cancel_timer_locked(self):
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:
                pass
            self._timer = None

    def _stop_channel_locked(self):
        global _channel
        ch = _channel
        if ch is None:
            return
        try:
            ch.stop()
        except Exception:
            pass

    def set_volume(self, vol: float):
        global _channel
        self._volume = max(0.0, min(1.0, float(vol)))
        ch = _channel
        if ch is not None:
            try:
                ch.set_volume(self._volume)
            except Exception:
                pass

    def is_playing(self) -> bool:
        global _channel
        ch = _channel
        if ch is None:
            return False
        try:
            return bool(ch.get_busy())
        except Exception:
            return False

    def status(self) -> dict:
        with self._lock:
            title = self._current_title
            duration = self._duration
            started = self._started_at
        pos = max(0.0, time.monotonic() - started) if title else 0.0
        return {
            "title": title,
            "playing": self.is_playing(),
            "position": pos,
            "duration": duration,
        }
