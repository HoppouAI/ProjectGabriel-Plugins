"""Voice fingerprinting via Resemblyzer.

Listens to the mic_chunk plugin event, keeps the last N seconds of audio
in a ring buffer, and on demand turns that into a 256-dim speaker
embedding. Saved voices live on disk as a .npz of stacked embeddings
plus a metadata json. Identification is cosine similarity, highest
match above threshold wins.

Resemblyzer is lazy loaded on first use (the import pulls in torch
and downloads ~17MB of weights), so importing this module is cheap.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


# Resemblyzer wants 16kHz float32 mono in the [-1, 1] range.
TARGET_SR = 16000


class VoiceRecognizer:
    def __init__(
        self,
        data_dir: Path,
        buffer_seconds: float = 5.0,
        min_audio_seconds: float = 1.5,
        similarity_threshold: float = 0.75,
    ):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.buffer_seconds = float(buffer_seconds)
        self.min_audio_seconds = float(min_audio_seconds)
        self.similarity_threshold = float(similarity_threshold)

        self._encoder = None
        self._encoder_lock = threading.Lock()

        # Ring buffer of int16 samples (already at 16kHz). We resize on
        # the fly so the buffer matches buffer_seconds * TARGET_SR.
        self._buf: deque = deque(maxlen=int(self.buffer_seconds * TARGET_SR))
        self._buf_lock = threading.Lock()
        self._last_chunk_at: float = 0.0

        # speech-start edge detector state
        self._speech_active: bool = False
        self._last_voiced_at: float = 0.0
        self._on_speech_start: Any = None  # callable() set by plugin
        self._energy_threshold: float = 500.0  # int16 RMS, tuned for typical mic
        self._silence_gap_seconds: float = 0.8  # silence needed to "reset"

        # voices: name -> {"embedding": np.ndarray(256,), "created_at": float, "sample_count": int}
        self._voices: dict[str, dict[str, Any]] = {}
        self._voices_path = self.data_dir / "voices.npz"
        self._meta_path = self.data_dir / "voices.json"
        self._load()

    # ---- speech edge detector --------------------------------------------

    def set_speech_start_callback(self, fn, energy_threshold: float = 500.0, silence_gap_seconds: float = 0.8):
        """Register a callback fired on the rising edge of speech (first
        voiced chunk after >silence_gap_seconds of silence). Callback
        runs on the audio thread, keep it fast or schedule work."""
        self._on_speech_start = fn
        self._energy_threshold = float(energy_threshold)
        self._silence_gap_seconds = float(silence_gap_seconds)

    def _update_speech_state(self, samples: np.ndarray):
        if self._on_speech_start is None:
            return
        # cheap RMS in int16 space
        if samples.size == 0:
            return
        rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
        now = time.time()
        if rms >= self._energy_threshold:
            if not self._speech_active:
                # rising edge: but only if we've had enough silence first
                if (now - self._last_voiced_at) >= self._silence_gap_seconds:
                    self._speech_active = True
                    try:
                        self._on_speech_start()
                    except Exception as e:
                        logger.debug(f"on_speech_start callback errored: {e}")
                else:
                    # too soon after last voiced chunk, treat as continuation
                    self._speech_active = True
            self._last_voiced_at = now
        else:
            if self._speech_active and (now - self._last_voiced_at) >= self._silence_gap_seconds:
                self._speech_active = False

    # ---- model loading ----------------------------------------------------

    def _ensure_encoder(self):
        if self._encoder is not None:
            return self._encoder
        with self._encoder_lock:
            if self._encoder is not None:
                return self._encoder
            try:
                from resemblyzer import VoiceEncoder  # type: ignore
            except ImportError as e:
                raise RuntimeError(
                    "resemblyzer is not installed. Run: pip install resemblyzer"
                ) from e
            logger.info("loading resemblyzer VoiceEncoder (first use may download weights)")
            self._encoder = VoiceEncoder(verbose=False)
        return self._encoder

    # ---- audio intake -----------------------------------------------------

    def feed_audio(self, data: bytes, sample_rate: int):
        """Called from the plugin's mic_chunk subscriber. Resamples to
        16k if needed and appends int16 samples to the ring buffer.
        Cheap, takes microseconds per call."""
        if not data:
            return
        try:
            samples = np.frombuffer(data, dtype=np.int16)
        except Exception:
            return
        if samples.size == 0:
            return
        if sample_rate != TARGET_SR:
            # cheap linear resample, good enough for speaker id
            ratio = TARGET_SR / float(sample_rate)
            new_len = int(samples.size * ratio)
            if new_len <= 0:
                return
            xp = np.arange(samples.size)
            x = np.linspace(0, samples.size - 1, new_len)
            samples = np.interp(x, xp, samples).astype(np.int16)
        with self._buf_lock:
            self._buf.extend(samples.tolist())
            self._last_chunk_at = time.time()
        # edge detector runs outside the buf lock, only reads samples
        self._update_speech_state(samples)

    def _snapshot_float32(self) -> np.ndarray:
        with self._buf_lock:
            if len(self._buf) == 0:
                return np.zeros(0, dtype=np.float32)
            arr = np.array(self._buf, dtype=np.int16)
        return (arr.astype(np.float32) / 32768.0)

    # ---- embedding + matching --------------------------------------------

    def _compute_current_embedding(self) -> np.ndarray | None:
        wav = self._snapshot_float32()
        if wav.size < int(self.min_audio_seconds * TARGET_SR):
            return None
        encoder = self._ensure_encoder()
        try:
            from resemblyzer import preprocess_wav  # type: ignore
            wav_p = preprocess_wav(wav, source_sr=TARGET_SR)
            if wav_p.size < int(self.min_audio_seconds * TARGET_SR * 0.5):
                # preprocess strips silence, may leave too little
                return None
            emb = encoder.embed_utterance(wav_p)
            return np.asarray(emb, dtype=np.float32)
        except Exception as e:
            logger.warning(f"embedding failed: {e}")
            return None

    @staticmethod
    def _cosine(a: np.ndarray, b: np.ndarray) -> float:
        na = float(np.linalg.norm(a))
        nb = float(np.linalg.norm(b))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return float(np.dot(a, b) / (na * nb))

    # ---- public api -------------------------------------------------------

    def save_voice(self, username: str) -> dict:
        username = (username or "").strip()
        if not username:
            return {"ok": False, "error": "username is empty"}
        emb = self._compute_current_embedding()
        if emb is None:
            return {
                "ok": False,
                "error": (
                    f"not enough recent audio in the buffer (need at least "
                    f"{self.min_audio_seconds:.1f}s of speech). Ask them to "
                    f"keep talking for a moment then try again."
                ),
            }
        # If the name already exists, average the new embedding with the
        # stored one. This lets multiple captures of the same person
        # smooth the centroid over time.
        if username in self._voices:
            prev = self._voices[username]["embedding"]
            count = int(self._voices[username].get("sample_count", 1))
            new_emb = (prev * count + emb) / (count + 1)
            self._voices[username] = {
                "embedding": new_emb.astype(np.float32),
                "created_at": float(self._voices[username].get("created_at", time.time())),
                "updated_at": time.time(),
                "sample_count": count + 1,
            }
            action = "updated"
        else:
            self._voices[username] = {
                "embedding": emb,
                "created_at": time.time(),
                "updated_at": time.time(),
                "sample_count": 1,
            }
            action = "saved"
        self._save()
        return {"ok": True, "action": action, "username": username, "sample_count": self._voices[username]["sample_count"]}

    def identify_current(self) -> dict:
        if not self._voices:
            return {
                "username": "unknown",
                "confidence": 0.0,
                "reason": (
                    "no voices have been saved yet. Look at the vision context "
                    "to figure out who is speaking, or just ask their name and "
                    "then call saveVoice."
                ),
            }
        emb = self._compute_current_embedding()
        if emb is None:
            return {
                "username": "unknown",
                "confidence": 0.0,
                "reason": (
                    f"not enough recent audio (need ~{self.min_audio_seconds:.1f}s). "
                    f"Wait for them to talk a bit more then try again."
                ),
            }
        best_name = None
        best_score = -1.0
        for name, rec in self._voices.items():
            score = self._cosine(emb, rec["embedding"])
            if score > best_score:
                best_score = score
                best_name = name
        if best_score >= self.similarity_threshold and best_name is not None:
            return {
                "username": best_name,
                "confidence": round(best_score, 4),
                "threshold": self.similarity_threshold,
            }
        return {
            "username": "unknown",
            "confidence": round(max(0.0, best_score), 4),
            "best_guess": best_name,
            "threshold": self.similarity_threshold,
            "reason": (
                "voice does not match any saved profile above the similarity "
                "threshold. Look at the vision/image context to figure out who "
                "is speaking, or ask their name and call saveVoice to remember "
                "them for next time."
            ),
        }

    def list_voices(self) -> list[dict]:
        out = []
        for name, rec in self._voices.items():
            out.append({
                "username": name,
                "created_at": rec.get("created_at"),
                "updated_at": rec.get("updated_at"),
                "sample_count": int(rec.get("sample_count", 1)),
            })
        return out

    def forget_voice(self, username: str) -> dict:
        if username not in self._voices:
            return {"ok": False, "error": f"no saved voice named '{username}'"}
        del self._voices[username]
        self._save()
        return {"ok": True, "removed": username}

    def rename_voice(self, old_name: str, new_name: str) -> dict:
        if old_name not in self._voices:
            return {"ok": False, "error": f"no saved voice named '{old_name}'"}
        new_name = (new_name or "").strip()
        if not new_name:
            return {"ok": False, "error": "new name is empty"}
        if new_name in self._voices and new_name != old_name:
            return {"ok": False, "error": f"'{new_name}' already exists"}
        self._voices[new_name] = self._voices.pop(old_name)
        self._save()
        return {"ok": True, "renamed_from": old_name, "renamed_to": new_name}

    # ---- persistence ------------------------------------------------------

    def _load(self):
        if not self._voices_path.exists() or not self._meta_path.exists():
            return
        try:
            with np.load(self._voices_path, allow_pickle=False) as f:
                names = list(f["names"])
                embs = f["embeddings"]
            with open(self._meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
            for i, raw_name in enumerate(names):
                name = str(raw_name)
                m = meta.get(name, {})
                self._voices[name] = {
                    "embedding": np.asarray(embs[i], dtype=np.float32),
                    "created_at": float(m.get("created_at", 0.0)),
                    "updated_at": float(m.get("updated_at", 0.0)),
                    "sample_count": int(m.get("sample_count", 1)),
                }
            logger.info(f"voiceid loaded {len(self._voices)} saved voice(s)")
        except Exception as e:
            logger.warning(f"failed to load saved voices: {e}")

    def _save(self):
        try:
            if not self._voices:
                # wipe files if nothing left
                for p in (self._voices_path, self._meta_path):
                    if p.exists():
                        p.unlink()
                return
            names = list(self._voices.keys())
            embs = np.stack([self._voices[n]["embedding"] for n in names]).astype(np.float32)
            np.savez(self._voices_path, names=np.array(names), embeddings=embs)
            meta = {
                n: {
                    "created_at": self._voices[n].get("created_at"),
                    "updated_at": self._voices[n].get("updated_at"),
                    "sample_count": int(self._voices[n].get("sample_count", 1)),
                }
                for n in names
            }
            with open(self._meta_path, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, indent=2)
        except Exception as e:
            logger.error(f"failed to save voices: {e}")
