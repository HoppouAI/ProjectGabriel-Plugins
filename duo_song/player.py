"""Tiny pygame.mixer wrapper. Loads a file and plays it, scheduled to start
at a specific local monotonic time. Single-song-at-a-time, calling play()
again kills the previous one.

We use pygame because it handles mp3/ogg/wav/flac without us having to
ship a decoder. The mixer runs its own thread, so play() returns instantly.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_pygame = None
_init_err: Optional[str] = None


def _ensure_pygame():
    global _pygame, _init_err
    if _pygame is not None:
        return _pygame
    try:
        import pygame  # type: ignore
        # init mixer with a reasonable buffer, low latency-ish
        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=1024)
        _pygame = pygame
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
        self._started_at: float = 0.0  # local monotonic
        self._volume = max(0.0, min(1.0, float(volume)))

    def available(self) -> bool:
        return _ensure_pygame() is not None

    def schedule_play(self, path: Path, title: str, start_at_local: float, duration: float = 0.0) -> bool:
        pg = _ensure_pygame()
        if pg is None:
            return False
        if not path.exists():
            logger.warning(f"duo_song: file missing: {path}")
            return False
        with self._lock:
            self._cancel_timer_locked()
            self._current_path = path
            self._current_title = title
            self._duration = float(duration or 0.0)
            self._started_at = float(start_at_local)
        delay = max(0.0, start_at_local - time.monotonic())
        t = threading.Timer(delay, self._fire, args=(path, title))
        t.daemon = True
        with self._lock:
            self._timer = t
        t.start()
        logger.info(f"duo_song: scheduled '{title}' in {delay:.2f}s")
        return True

    def _fire(self, path: Path, title: str):
        pg = _ensure_pygame()
        if pg is None:
            return
        try:
            pg.mixer.music.load(str(path))
            pg.mixer.music.set_volume(self._volume)
            pg.mixer.music.play()
            # snap started_at to the actual moment we hit play, so position()
            # is accurate even if the timer fired a hair late
            with self._lock:
                self._started_at = time.monotonic()
            logger.info(f"duo_song: playing '{title}'")
        except Exception as e:
            logger.error(f"duo_song: failed to play {path}: {e}")

    def stop(self):
        pg = _ensure_pygame()
        with self._lock:
            self._cancel_timer_locked()
            self._current_path = None
            self._current_title = None
            self._duration = 0.0
        if pg is not None:
            try:
                pg.mixer.music.stop()
            except Exception:
                pass

    def _cancel_timer_locked(self):
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:
                pass
            self._timer = None

    def set_volume(self, vol: float):
        self._volume = max(0.0, min(1.0, float(vol)))
        pg = _ensure_pygame()
        if pg is not None:
            try:
                pg.mixer.music.set_volume(self._volume)
            except Exception:
                pass

    def is_playing(self) -> bool:
        pg = _ensure_pygame()
        if pg is None:
            return False
        try:
            return bool(pg.mixer.music.get_busy())
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
