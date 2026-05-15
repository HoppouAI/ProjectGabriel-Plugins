"""fluidsynth-based MIDI player. Loads a soundfont, runs a tight playback
thread that schedules each event against a target monotonic timestamp.

Used by both the plugin and the standalone client. No Project Gabriel
imports so the standalone can pull this in directly.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_fs_mod = None
_fs_err: Optional[str] = None


def _load_fs():
    global _fs_mod, _fs_err
    if _fs_mod is not None:
        return _fs_mod
    try:
        import fluidsynth  # type: ignore
        _fs_mod = fluidsynth
        return fluidsynth
    except Exception as e:
        _fs_err = str(e)
        logger.error(f"midi_band: pyfluidsynth import failed: {e}")
        return None


class MidiPlayer:
    def __init__(self, soundfont: Optional[Path] = None, gain: float = 0.5,
                 driver: Optional[str] = None,
                 auto_install_dir: Optional[Path] = None,
                 auto_install: bool = True):
        self.soundfont = soundfont
        self.gain = max(0.0, min(2.0, float(gain)))
        self.driver = driver
        self.auto_install_dir = auto_install_dir
        self.auto_install = bool(auto_install)
        self._fs = None
        self._sfid = None
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._current_song: Optional[str] = None
        self._current_tracks: List[str] = []
        self._scheduled_start: float = 0.0
        self._duration: float = 0.0
        self._native_primed = False

    def _prime_native(self) -> bool:
        if self._native_primed:
            return True
        try:
            from . import fluidsynth_install
        except ImportError:
            try:
                import fluidsynth_install  # type: ignore
            except ImportError:
                fluidsynth_install = None  # type: ignore
        if fluidsynth_install is None:
            self._native_primed = True
            return True
        ok = fluidsynth_install.ensure_fluidsynth(
            self.auto_install_dir, allow_download=self.auto_install
        )
        self._native_primed = True
        return ok

    def available(self) -> bool:
        self._prime_native()
        if _load_fs() is None:
            return False
        if self.soundfont is None:
            return False
        return self.soundfont.exists()

    def _ensure_synth(self) -> bool:
        self._prime_native()
        fs_mod = _load_fs()
        if fs_mod is None:
            return False
        with self._lock:
            if self._fs is not None:
                return True
            try:
                self._fs = fs_mod.Synth(gain=self.gain)
                if self.driver:
                    self._fs.start(driver=self.driver)
                else:
                    self._fs.start()
                if self.soundfont and self.soundfont.exists():
                    self._sfid = self._fs.sfload(str(self.soundfont))
                    # default each channel to program 0 of the soundfont so
                    # midis that never send program_change still make sound.
                    # channel 9 is GM drums (bank 128).
                    for ch in range(16):
                        try:
                            bank = 128 if ch == 9 else 0
                            self._fs.program_select(ch, self._sfid, bank, 0)
                        except Exception:
                            pass
                else:
                    logger.warning("midi_band: no soundfont loaded, synth will be silent")
                return True
            except Exception as e:
                logger.error(f"midi_band: synth init failed: {e}")
                self._fs = None
                return False

    def schedule(self, events, start_at_local: float, song: str,
                 track_names: List[str], duration: float) -> bool:
        if not self._ensure_synth():
            return False
        # kill any prior playback before starting a new one
        self.stop_playback()
        with self._lock:
            self._stop_flag = threading.Event()
            self._current_song = song
            self._current_tracks = list(track_names)
            self._scheduled_start = float(start_at_local)
            self._duration = float(duration)
            self._thread = threading.Thread(
                target=self._run,
                args=(events, start_at_local, self._stop_flag),
                daemon=True,
            )
            self._thread.start()
        logger.info(
            f"midi_band: scheduled '{song}' in "
            f"{max(0.0, start_at_local - time.monotonic()):.2f}s, "
            f"{len(events)} events, tracks={track_names}"
        )
        return True

    def _run(self, events, start_at, stop_flag):
        try:
            # wait until start, mixed sleep + busy-wait for tight timing
            while not stop_flag.is_set():
                now = time.monotonic()
                if now >= start_at:
                    break
                wait = start_at - now
                if wait > 0.005:
                    time.sleep(wait - 0.003)
            if stop_flag.is_set():
                return
            for offset, msg in events:
                if stop_flag.is_set():
                    break
                target = start_at + offset
                while not stop_flag.is_set():
                    now = time.monotonic()
                    if now >= target:
                        break
                    wait = target - now
                    if wait > 0.003:
                        time.sleep(wait - 0.002)
                if stop_flag.is_set():
                    break
                self._emit(msg)
        except Exception as e:
            logger.error(f"midi_band: play loop crashed: {e}")
        finally:
            try:
                self._all_notes_off()
            except Exception:
                pass

    def _emit(self, msg):
        fs = self._fs
        if fs is None:
            return
        try:
            t = msg.type
            if t == "note_on":
                if msg.velocity > 0:
                    fs.noteon(msg.channel, msg.note, msg.velocity)
                else:
                    fs.noteoff(msg.channel, msg.note)
            elif t == "note_off":
                fs.noteoff(msg.channel, msg.note)
            elif t == "control_change":
                fs.cc(msg.channel, msg.control, msg.value)
            elif t == "program_change":
                bank = 128 if msg.channel == 9 else 0
                if self._sfid is not None:
                    try:
                        fs.program_select(msg.channel, self._sfid, bank, msg.program)
                    except Exception:
                        # some soundfonts dont have every program, fall back
                        fs.program_change(msg.channel, msg.program)
                else:
                    fs.program_change(msg.channel, msg.program)
            elif t == "pitchwheel":
                fs.pitch_bend(msg.channel, msg.pitch)
            elif t in ("aftertouch", "polytouch"):
                # optional, skip if pyfluidsynth doesnt expose the call
                pass
        except Exception as e:
            logger.debug(f"midi_band: emit failed for {msg}: {e}")

    def _all_notes_off(self):
        fs = self._fs
        if fs is None:
            return
        for ch in range(16):
            try:
                fs.cc(ch, 123, 0)  # all notes off
                fs.cc(ch, 120, 0)  # all sound off
            except Exception:
                pass

    def stop_playback(self):
        with self._lock:
            sf = self._stop_flag
            t = self._thread
        if sf is not None:
            sf.set()
        # join briefly so noteoffs finish before we move on
        if t is not None and t.is_alive():
            try:
                t.join(timeout=0.3)
            except Exception:
                pass
        try:
            self._all_notes_off()
        except Exception:
            pass
        with self._lock:
            self._current_song = None
            self._current_tracks = []
            self._duration = 0.0

    def is_playing(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive())

    def status(self) -> dict:
        playing = self.is_playing()
        pos = 0.0
        if playing or self._current_song:
            pos = max(0.0, time.monotonic() - self._scheduled_start)
        return {
            "playing": playing,
            "song": self._current_song,
            "tracks": list(self._current_tracks),
            "position": pos,
            "duration": self._duration,
        }

    def shutdown(self):
        self.stop_playback()
        with self._lock:
            if self._fs is not None:
                try:
                    self._fs.delete()
                except Exception:
                    pass
                self._fs = None
                self._sfid = None
