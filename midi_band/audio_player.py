"""sounddevice-based audio stem player. Plays a pre-mixed stereo buffer
against a target monotonic timestamp, with pause/resume/seek/nudge so it
drops into the same clock-sync model the MIDI player uses.

Mirrors the bits of MidiPlayer the band client/server lean on so the two
players are swappable depending on which mode the band is in. No Project
Gabriel imports, the standalone client pulls this in directly.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_sd = None
_sf = None
_np = None
_import_err: Optional[str] = None


def _load_audio_libs() -> bool:
    global _sd, _sf, _np, _import_err
    if _sd is not None and _sf is not None and _np is not None:
        return True
    try:
        import numpy as np  # type: ignore
        import sounddevice as sd  # type: ignore
        import soundfile as sf  # type: ignore
        _np, _sd, _sf = np, sd, sf
        return True
    except Exception as e:
        _import_err = str(e)
        logger.error(f"midi_band: audio libs import failed: {e}")
        return False


def audio_import_error() -> Optional[str]:
    return _import_err


def _resample(arr, sr_in: int, sr_out: int):
    # linear interp resample, safety net for a stem that isn't the song's
    # base rate. most stem-splitter outputs are already 44100 so this rarely
    # runs.
    np = _np
    if sr_in == sr_out or arr.shape[0] == 0:
        return arr
    n_in = arr.shape[0]
    n_out = int(round(n_in * (sr_out / float(sr_in))))
    if n_out <= 0:
        return arr[:0]
    x_old = np.linspace(0.0, 1.0, n_in, endpoint=False)
    x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
    out = np.empty((n_out, arr.shape[1]), dtype="float32")
    for c in range(arr.shape[1]):
        out[:, c] = np.interp(x_new, x_old, arr[:, c]).astype("float32")
    return out


def mix_stems(paths: List[Path], base_sr: Optional[int] = None):
    """Read stem files, force stereo, line them up to a common rate, sum, and
    peak-normalize so the sum doesn't clip. Returns (mix float32 [N,2], sr) or
    (None, 0) when nothing decodable was passed."""
    if not _load_audio_libs():
        raise RuntimeError(f"audio libs missing: {_import_err}")
    np, sf = _np, _sf
    arrays = []
    sr0 = base_sr
    for p in paths:
        try:
            data, sr = sf.read(str(p), dtype="float32", always_2d=True)
        except Exception as e:
            logger.warning(f"midi_band: stem read failed {p}: {e}")
            continue
        if data.shape[1] == 1:
            data = np.repeat(data, 2, axis=1)
        elif data.shape[1] > 2:
            data = data[:, :2]
        if sr0 is None:
            sr0 = sr
        if sr != sr0:
            data = _resample(data, sr, sr0)
        arrays.append(data)
    if not arrays:
        return None, 0
    n = max(a.shape[0] for a in arrays)
    mix = np.zeros((n, 2), dtype="float32")
    for a in arrays:
        mix[: a.shape[0]] += a
    peak = float(np.max(np.abs(mix))) if mix.size else 0.0
    if peak > 1.0:
        mix *= np.float32(1.0 / peak)
    return mix, int(sr0 or 44100)


class AudioPlayer:
    def __init__(self, gain: float = 0.5, device: Optional[str] = None):
        self.gain = max(0.0, min(2.0, float(gain)))
        self.device = device
        self._stream = None
        self._mix = None
        self._sr = 44100
        self._frame = 0          # read pointer in samples, advances each callback
        self._anchor_frame = 0   # frame the playhead lands on at _scheduled_start
        self._scheduled_start = 0.0
        self._started = False
        self._playing = False
        self._is_paused = False
        self._paused_frame = 0
        self._duration = 0.0
        self._current_song: Optional[str] = None
        self._current_tracks: List[str] = []
        self._suppress_finish = False
        self._lock = threading.Lock()
        self.on_finished = lambda: None

    def available(self) -> bool:
        return _load_audio_libs()

    def _resolve_device(self):
        dev = self.device
        if dev is None or str(dev).strip() == "":
            return None
        try:
            return int(str(dev).strip())
        except (TypeError, ValueError):
            pass
        try:
            target = str(dev).strip().lower()
            for i, d in enumerate(_sd.query_devices()):
                if d.get("max_output_channels", 0) <= 0:
                    continue
                name = str(d.get("name", "")).lower()
                if target in name or name in target:
                    return i
        except Exception as e:
            logger.warning(f"midi_band: audio device match failed for '{dev}': {e}")
        return str(dev)

    # ----- scheduling -----

    def schedule_audio(self, mix, sr: int, start_at_local: float, song: str,
                       tracks: List[str], duration: float) -> bool:
        if not _load_audio_libs():
            return False
        self.stop_playback()
        if mix is None or len(mix) == 0:
            return False
        with self._lock:
            self._mix = mix
            self._sr = int(sr)
            self._frame = 0
            self._anchor_frame = 0
            self._scheduled_start = float(start_at_local)
            self._started = False
            self._is_paused = False
            self._paused_frame = 0
            self._playing = True
            self._current_song = song
            self._current_tracks = list(tracks)
            self._duration = float(duration)
        if not self._open_stream():
            with self._lock:
                self._playing = False
            return False
        logger.info(
            f"midi_band: scheduled audio '{song}' in "
            f"{max(0.0, start_at_local - time.monotonic()):.2f}s, tracks={tracks}"
        )
        return True

    def _open_stream(self) -> bool:
        try:
            self._stream = _sd.OutputStream(
                samplerate=self._sr,
                channels=2,
                dtype="float32",
                device=self._resolve_device(),
                callback=self._callback,
                finished_callback=self._on_stream_finished,
            )
            self._stream.start()
            return True
        except Exception as e:
            logger.error(f"midi_band: audio stream open failed: {e}")
            self._stream = None
            return False

    def _close_stream(self, suppress: bool = True):
        st = self._stream
        self._stream = None
        if st is None:
            return
        self._suppress_finish = suppress
        try:
            st.stop()
        except Exception:
            pass
        try:
            st.close()
        except Exception:
            pass
        self._suppress_finish = False

    def _callback(self, outdata, frames, time_info, status):
        if status:
            logger.debug(f"midi_band: audio stream status {status}")
        with self._lock:
            mix = self._mix
            if mix is None or self._is_paused or not self._playing:
                outdata[:] = 0
                return
            now = time.monotonic()
            if not self._started:
                if now < self._scheduled_start:
                    outdata[:] = 0
                    return
                self._frame = self._anchor_frame + max(
                    0, int(round((now - self._scheduled_start) * self._sr))
                )
                self._started = True
            f = self._frame
            total = mix.shape[0]
            if f >= total:
                outdata[:] = 0
                self._playing = False
                raise _sd.CallbackStop
            end = f + frames
            chunk = mix[f:end]
            n = chunk.shape[0]
            g = self.gain
            if g != 1.0:
                outdata[:n] = chunk * _np.float32(g)
            else:
                outdata[:n] = chunk
            if n < frames:
                outdata[n:] = 0
            self._frame = end
            if end >= total:
                self._playing = False
                raise _sd.CallbackStop

    def _on_stream_finished(self):
        if self._suppress_finish:
            return
        # natural end of the song, the buffer ran out
        self._playing = False
        try:
            self.on_finished()
        except Exception:
            pass

    # ----- transport -----

    def stop_playback(self):
        self._close_stream(suppress=True)
        with self._lock:
            self._playing = False
            self._started = False
            self._is_paused = False
            self._frame = 0
            self._anchor_frame = 0
            self._paused_frame = 0
            self._current_song = None
            self._current_tracks = []
            self._duration = 0.0
            self._mix = None

    def pause(self) -> bool:
        with self._lock:
            if not self._playing or self._is_paused:
                return False
            self._paused_frame = self._frame if self._started else self._anchor_frame
            self._is_paused = True
        return True

    def resume(self, start_at_local: float) -> bool:
        with self._lock:
            if not self._is_paused or self._mix is None:
                return False
            self._anchor_frame = self._paused_frame
            self._scheduled_start = float(start_at_local)
            self._started = False
            self._is_paused = False
            self._playing = True
            need_stream = self._stream is None
        if need_stream:
            self._open_stream()
        return True

    def seek_to(self, target_seconds: float, start_at_local: Optional[float] = None) -> bool:
        if self._mix is None:
            return False
        if start_at_local is None:
            start_at_local = time.monotonic()
        with self._lock:
            self._anchor_frame = max(0, int(round(float(target_seconds) * self._sr)))
            self._scheduled_start = float(start_at_local)
            self._started = False
            self._is_paused = False
            self._playing = True
            need_stream = self._stream is None
        if need_stream:
            self._open_stream()
        return True

    def nudge(self, delta_seconds: float) -> None:
        # mirror the MIDI player: positive delta shifts the playhead BACK,
        # negative shifts it forward (catch up).
        shift = int(round(float(delta_seconds) * self._sr))
        with self._lock:
            if not self._started:
                self._anchor_frame = max(0, self._anchor_frame - shift)
            else:
                self._frame = max(0, self._frame - shift)

    def current_position(self) -> float:
        with self._lock:
            if self._is_paused:
                pos = self._paused_frame / float(self._sr)
            elif self._started:
                pos = self._frame / float(self._sr)
            elif self._playing:
                pos = self._anchor_frame / float(self._sr)
            else:
                pos = 0.0
        if self._duration > 0:
            pos = min(pos, self._duration)
        return max(0.0, pos)

    def is_playing(self) -> bool:
        return bool(self._playing and not self._is_paused and self._stream is not None)

    def set_gain(self, gain: float) -> float:
        g = max(0.0, min(2.0, float(gain)))
        self.gain = g
        return g

    def status(self) -> dict:
        playing = self.is_playing()
        pos = self.current_position()
        return {
            "playing": playing,
            "paused": self._is_paused,
            "song": self._current_song,
            "tracks": list(self._current_tracks),
            "position": pos,
            "duration": self._duration,
            "gain": self.gain,
            "count_in_lead": 0.0,
            "count_in_remaining": 0.0,
            "in_count_in": False,
            "has_soundfont": True,
        }

    def shutdown(self):
        self.stop_playback()
