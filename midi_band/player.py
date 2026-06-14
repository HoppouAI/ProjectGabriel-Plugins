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
from typing import Callable, List, Optional

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
                 device: Optional[str] = None,
                 auto_install_dir: Optional[Path] = None,
                 auto_install: bool = True):
        self.soundfont = soundfont
        self.gain = max(0.0, min(2.0, float(gain)))
        self.driver = driver
        self.device = device
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
        # for pause/resume: keep last scheduled events around so we can
        # replay from a given offset.
        self._last_events: list = []
        self._last_song_orig_duration: float = 0.0
        self._paused_offset: float = 0.0
        self._is_paused: bool = False
        self._count_in_lead: float = 0.0
        self.on_finished: Callable[[], None] = lambda: None

    def _default_driver(self) -> Optional[str]:
        # fluidsynth's auto driver is unreliable on Windows: it tries
        # ALSA-style "default" which doesn't exist and prints
        # 'Device "default" does not exist'. Force dsound there.
        import platform as _plat
        sysname = _plat.system()
        if sysname == "Windows":
            return "dsound"
        if sysname == "Darwin":
            return "coreaudio"
        return None  # let fluidsynth pick on Linux/other

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
                drv = self.driver or self._default_driver()
                # route to a specific output device by name (e.g. a virtual
                # audio cable) when set, so multiple instances can target
                # different sinks instead of all stacking on the default.
                if drv and self.device:
                    try:
                        self._fs.setting(f"audio.{drv}.device", str(self.device))
                        # read it back, fluidsynth silently falls back to
                        # 'default' if the name doesn't match an endpoint
                        # exactly, and pyfluidsynth's setting() returns None
                        # so we can't tell from the call alone.
                        try:
                            got = self._fs.get_setting(f"audio.{drv}.device")
                        except Exception:
                            got = None
                        if got and str(got) != str(self.device):
                            logger.warning(
                                f"midi_band: fluidsynth did not accept audio.{drv}.device='{self.device}', "
                                f"current value is '{got}'. names must match the wasapi friendly name EXACTLY. "
                                f"run 'standalone_client.py --list-devices' to see accepted names."
                            )
                        else:
                            logger.info(f"midi_band: routing audio to '{self.device}' via {drv}")
                    except Exception as e:
                        logger.warning(f"midi_band: could not set audio.{drv}.device='{self.device}': {e}")
                if drv:
                    self._fs.start(driver=drv)
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

    def warmup(self) -> bool:
        # forces fluidsynth init + soundfont load so the first real play
        # doesnt eat the latency of doing it then.
        return self._ensure_synth()

    def schedule_ticks(self, ticks, start_at_local: float, label: str = "soundcheck",
                       duration: float = 10.0) -> bool:
        # ticks: iterable of (offset_seconds, note, channel, velocity, length).
        # builds synthetic mido note_on/note_off events and reuses schedule().
        try:
            import mido
        except Exception as e:
            logger.error(f"midi_band: mido missing for soundcheck: {e}")
            return False
        events = []
        for tk in ticks:
            try:
                offset, note, channel, velocity, length = tk
            except Exception:
                continue
            on = mido.Message("note_on", channel=int(channel),
                              note=int(note), velocity=int(velocity))
            off = mido.Message("note_off", channel=int(channel),
                               note=int(note), velocity=0)
            events.append((float(offset), on))
            events.append((float(offset) + max(0.05, float(length)), off))
        events.sort(key=lambda e: e[0])
        return self.schedule(events, start_at_local, label, [label], duration)

    def schedule(self, events, start_at_local: float, song: str,
                 track_names: List[str], duration: float,
                 count_in_lead: float = 0.0) -> bool:
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
            self._last_events = list(events)
            self._last_song_orig_duration = float(duration)
            self._is_paused = False
            self._paused_offset = 0.0
            self._count_in_lead = float(count_in_lead)
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

    def _boost_thread_priority(self):
        """Tell the OS this thread is doing real-time audio work so it
        gets scheduled ahead of the asyncio loop and webui server when
        the GIL gets contended. Best-effort, swallow failures."""
        try:
            import platform as _plat
            if _plat.system() != "Windows":
                return
            import ctypes
            # bump process timer resolution to 1ms so time.sleep() is
            # actually granular instead of rounding up to the default ~15ms
            try:
                ctypes.WinDLL("winmm").timeBeginPeriod(1)
            except Exception:
                pass
            # THREAD_PRIORITY_TIME_CRITICAL = 15
            handle = ctypes.windll.kernel32.GetCurrentThread()
            ctypes.windll.kernel32.SetThreadPriority(handle, 15)
        except Exception:
            pass

    def _run(self, events, start_at, stop_flag):
        self._boost_thread_priority()
        try:
            # wait until start
            while not stop_flag.is_set():
                now = time.monotonic()
                start = self._scheduled_start
                wait = start - now
                if wait <= 0:
                    break
                # sleep most of the way, leave a small tail to converge tightly
                if wait > 0.002:
                    time.sleep(wait - 0.001)
                else:
                    time.sleep(0.0005)
            if stop_flag.is_set():
                return
            for offset, msg in events:
                if stop_flag.is_set():
                    break
                while not stop_flag.is_set():
                    now = time.monotonic()
                    target = self._scheduled_start + offset
                    wait = target - now
                    if wait <= 0:
                        break
                    if wait > 0.002:
                        # release the GIL so the asyncio loop / webui
                        # thread can do their work without delaying us
                        time.sleep(wait - 0.001)
                    else:
                        # sub-2ms tail: short sleep instead of busy-spin so
                        # we don't hog the GIL between every single event
                        time.sleep(0.0005)
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
            # if we got here without being stopped, the song ended naturally.
            # clear state so the chatbox / webui know it's done. stop_playback
            # already clears state when it kills us, this only matters on
            # natural completion.
            if not stop_flag.is_set():
                with self._lock:
                    if self._stop_flag is stop_flag:
                        self._current_song = None
                        self._current_tracks = []
                        self._duration = 0.0
                        self._last_events = []
                        self._is_paused = False
                        self._paused_offset = 0.0
                        self._count_in_lead = 0.0
                # nudge listeners so the UI/chatbox re-render right away
                try:
                    self.on_finished()
                except Exception:
                    pass

    def nudge(self, delta_seconds: float) -> None:
        """Shift scheduled_start by delta. Negative = catch up (events fire sooner),
        positive = slow down (events fire later)."""
        with self._lock:
            self._scheduled_start += float(delta_seconds)

    def current_position(self) -> float:
        """Where the playhead is right now in song-relative seconds. 0 if not playing."""
        if not self.is_playing():
            return self._paused_offset if self._is_paused else 0.0
        return max(0.0, time.monotonic() - self._scheduled_start)

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
            self._last_events = []
            self._is_paused = False
            self._paused_offset = 0.0

    def pause(self) -> bool:
        # freeze playback, remember how far we got, kill the thread so no
        # more notes fire. resume() picks back up from saved offset.
        with self._lock:
            if not (self._thread and self._thread.is_alive()):
                return False
            elapsed = max(0.0, time.monotonic() - self._scheduled_start)
            sf = self._stop_flag
            t = self._thread
            song = self._current_song
            tracks = list(self._current_tracks)
            events = list(self._last_events)
            orig_dur = self._last_song_orig_duration
        if sf is not None:
            sf.set()
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
            self._is_paused = True
            self._paused_offset = elapsed
            # keep song/tracks/events so resume() can use them
            self._current_song = song
            self._current_tracks = tracks
            self._last_events = events
            self._last_song_orig_duration = orig_dur
        return True

    def resume(self, start_at_local: float) -> bool:
        with self._lock:
            if not self._is_paused or not self._last_events:
                return False
            offset = self._paused_offset
            song = self._current_song or ""
            tracks = list(self._current_tracks)
            orig_dur = self._last_song_orig_duration
            # filter out events already played
            remaining = [(o, m) for (o, m) in self._last_events if o >= offset]
        if not remaining:
            self.stop_playback()
            return False
        # set scheduled_start such that an event at offset=paused_offset
        # fires exactly at start_at_local. _run does target = start + offset.
        effective_start = float(start_at_local) - offset
        new_duration = max(0.0, orig_dur - offset)
        with self._lock:
            self._stop_flag = threading.Event()
            self._scheduled_start = effective_start
            self._duration = new_duration + offset  # so position math stays sensible
            self._is_paused = False
            self._thread = threading.Thread(
                target=self._run,
                args=(remaining, effective_start, self._stop_flag),
                daemon=True,
            )
            self._thread.start()
        logger.info(f"midi_band: resumed '{song}' from {offset:.2f}s")
        return True

    def seek_to(self, target_seconds: float, start_at_local: Optional[float] = None) -> bool:
        """Hard re-seek mid-playback to target_seconds and resume immediately.
        Used by the client's drift-correction when the smooth nudge isn't enough."""
        if not self._last_events:
            return False
        if start_at_local is None:
            start_at_local = time.monotonic()
        offset = max(0.0, float(target_seconds))
        # snapshot what stop_playback is about to wipe
        with self._lock:
            saved_events = list(self._last_events)
            saved_song = self._current_song
            saved_tracks = list(self._current_tracks)
            saved_orig_dur = self._last_song_orig_duration
        self.stop_playback()
        with self._lock:
            self._last_events = saved_events
            self._current_song = saved_song
            self._current_tracks = saved_tracks
            self._last_song_orig_duration = saved_orig_dur
            self._is_paused = True
            self._paused_offset = offset
        return self.resume(start_at_local)

    def set_gain(self, gain: float) -> float:
        g = max(0.0, min(2.0, float(gain)))
        self.gain = g
        with self._lock:
            fs = self._fs
        if fs is not None:
            try:
                # pyfluidsynth exposes settings via .setting
                fs.setting("synth.gain", g)
            except Exception as e:
                logger.debug(f"midi_band: setting synth.gain failed: {e}")
        return g

    def _soundfont_ready(self) -> bool:
        # true once a soundfont is actually loaded, or before synth init when
        # the configured file exists. false means this synth makes no sound,
        # so no volume change is audible here.
        if self._sfid is not None:
            return True
        try:
            return bool(self.soundfont and self.soundfont.exists())
        except Exception:
            return False

    def is_playing(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive())

    def status(self) -> dict:
        playing = self.is_playing()
        pos = 0.0
        if self._is_paused:
            pos = self._paused_offset
        elif playing or self._current_song:
            pos = max(0.0, time.monotonic() - self._scheduled_start)
        # don't let position run past duration once the song ended
        if self._duration > 0:
            pos = min(pos, self._duration)
        lead = float(self._count_in_lead)
        in_count_in = playing and lead > 0 and pos < lead
        # song-relative position: zero during count-in, then ticks up
        song_pos = max(0.0, pos - lead) if lead > 0 else pos
        song_dur = max(0.0, self._duration - lead) if lead > 0 else self._duration
        return {
            "playing": playing,
            "paused": self._is_paused,
            "song": self._current_song,
            "tracks": list(self._current_tracks),
            "position": song_pos,
            "duration": song_dur,
            "gain": self.gain,
            "count_in_lead": lead,
            "count_in_remaining": max(0.0, lead - pos) if in_count_in else 0.0,
            "in_count_in": in_count_in,
            "has_soundfont": self._soundfont_ready(),
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
