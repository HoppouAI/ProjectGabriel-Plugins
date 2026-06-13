"""Voice fingerprinting via SpeechBrain ECAPA-TDNN.

ECAPA-TDNN is current SOTA for speaker embeddings, trained on VoxCeleb
1+2. Cosine clusters are way tighter than the older GE2E based encoders
which kept causing mixups between similar voices. We also store multiple
embeddings per saved person and score by the max cosine instead of a
single averaged centroid, so a quiet voice and an excited voice from the
same person both still match.

The encoder is lazy loaded on first use and cached under
data/plugins/voiceid/ecapa_model/. First load downloads ~14MB.
"""
from __future__ import annotations

import json
import logging
import math
import shutil
import sys
import threading
import time
import types
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# ECAPA-TDNN expects 16k mono float32 in [-1, 1]
TARGET_SR = 16000

# How many embeddings we keep per saved voice. New captures push old ones
# out. Multiple embeddings beat a single centroid because real voices
# vary a lot (yelling vs whispering, mood, mic distance).
MAX_EMBEDDINGS_PER_VOICE = 12

# Top-1 must beat top-2 by this much cosine, otherwise we say unknown
# instead of guessing wrong. Stops "Bob and Alice sound kinda alike so
# pick one at random".
DISAMBIG_MARGIN = 0.06

# ECAPA-TDNN embedding dim. Used to detect old resemblyzer files (256-dim)
# and migrate them aside on load.
ECAPA_DIM = 192


# speechbrain 1.x registers LazyModule wrappers for optional integrations
# like k2_fsa, huggingface wordemb (fasttext), nlp (spacy/flair), and the
# numba transducer loss. when transformers does its AutoX integration
# discovery (or anything else touches the deprecated aliases) the lazy
# import triggers a real `import <optional_dep>` which fails if that dep
# isnt installed (and almost none of them are). that failure then
# cascades into 'Could not import module AutoFeatureExtractor' from
# transformers and kills any other plugin that uses transformers (eg
# omnivoice_tts).
# fix: pre-stub the broken integrations with empty modules in
# sys.modules so the lazy imports hit the cache and return the stub.
_BROKEN_SB_INTEGRATIONS = (
    "speechbrain.integrations.k2_fsa",
    "speechbrain.integrations.huggingface.wordemb",
    "speechbrain.integrations.nlp",
    "speechbrain.integrations.numba.transducer_loss",
)


class _SpeechbrainStubModule(types.ModuleType):
    def __getattr__(self, attr):
        if attr.startswith("_"):
            raise AttributeError(attr)
        raise ImportError(
            f"{self.__name__} stubbed (optional dep not installed)"
        )


def _stub_broken_speechbrain_integrations():
    for name in _BROKEN_SB_INTEGRATIONS:
        if name in sys.modules:
            continue
        try:
            __import__(name)
        except Exception:
            sys.modules[name] = _SpeechbrainStubModule(name)


class VoiceRecognizer:
    def __init__(
        self,
        data_dir: Path,
        buffer_seconds: float = 5.0,
        min_audio_seconds: float = 1.5,
        similarity_threshold: float = 0.4,
        max_embeddings_per_voice: int = MAX_EMBEDDINGS_PER_VOICE,
        disambig_margin: float = DISAMBIG_MARGIN,
    ):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.buffer_seconds = float(buffer_seconds)
        self.min_audio_seconds = float(min_audio_seconds)
        self.similarity_threshold = float(similarity_threshold)
        self.max_emb = int(max_embeddings_per_voice)
        self.margin = float(disambig_margin)

        self._encoder = None
        self._encoder_lock = threading.Lock()
        self._device = "cpu"

        # Ring buffer of int16 samples already at 16kHz.
        self._buf: deque = deque(maxlen=int(self.buffer_seconds * TARGET_SR))
        self._buf_lock = threading.Lock()
        self._last_chunk_at: float = 0.0

        # name -> {"embeddings": (K, 192) float32, "created_at": float,
        #          "updated_at": float, "sample_count": int}
        self._voices: dict[str, dict[str, Any]] = {}
        self._voices_path = self.data_dir / "voices.npz"
        self._meta_path = self.data_dir / "voices.json"
        self._load()

    # ---- model loading ----------------------------------------------------

    def _ensure_encoder(self):
        if self._encoder is not None:
            return self._encoder
        with self._encoder_lock:
            if self._encoder is not None:
                return self._encoder
            try:
                import torch  # noqa: F401
            except ImportError as e:
                raise RuntimeError(
                    "voiceid needs torch. Run: .\\bin\\uv.exe pip install torch torchaudio"
                ) from e
            # neutralize speechbrain's broken k2 integration before we
            # pull speechbrain in. otherwise the LazyModule will blow up
            # when transformers (in any other plugin) does AutoX
            # integration discovery later in the boot.
            _stub_broken_speechbrain_integrations()
            try:
                # speechbrain >= 1.0 path
                from speechbrain.inference.classifiers import EncoderClassifier
            except ImportError:
                try:
                    # legacy 0.5.x fallback
                    from speechbrain.pretrained import EncoderClassifier  # type: ignore
                except ImportError as e:
                    raise RuntimeError(
                        "voiceid needs speechbrain. "
                        "Run: .\\bin\\uv.exe pip install speechbrain"
                    ) from e

            import torch
            try:
                if torch.cuda.is_available():
                    self._device = "cuda"
            except Exception:
                pass

            cache_dir = self.data_dir / "ecapa_model"
            cache_dir.mkdir(parents=True, exist_ok=True)
            logger.info(
                "loading ECAPA-TDNN speaker encoder "
                "(first run downloads ~14MB to %s)", cache_dir,
            )
            self._encoder = EncoderClassifier.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                savedir=str(cache_dir),
                run_opts={"device": self._device},
            )
            logger.info(f"voiceid encoder ready on {self._device}")
        return self._encoder

    # ---- audio intake -----------------------------------------------------

    def feed_audio(self, data: bytes, sample_rate: int):
        """Called from the plugin's mic_chunk subscriber. Resamples to
        16k if needed and appends int16 samples to the ring buffer.
        Cheap, takes microseconds."""
        if not data:
            return
        try:
            samples = np.frombuffer(data, dtype=np.int16)
        except Exception:
            return
        if samples.size == 0:
            return
        if sample_rate != TARGET_SR:
            samples = self._resample_int16(samples, sample_rate, TARGET_SR)
            if samples.size == 0:
                return
        with self._buf_lock:
            self._buf.extend(samples.tolist())
            self._last_chunk_at = time.time()

    @staticmethod
    def _resample_int16(samples: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
        if src_sr == dst_sr or samples.size == 0:
            return samples
        try:
            from scipy.signal import resample_poly
            g = math.gcd(src_sr, dst_sr)
            up = dst_sr // g
            down = src_sr // g
            f = samples.astype(np.float32)
            r = resample_poly(f, up, down)
            return np.clip(r, -32768, 32767).astype(np.int16)
        except Exception:
            ratio = dst_sr / float(src_sr)
            new_len = int(samples.size * ratio)
            if new_len <= 0:
                return np.zeros(0, dtype=np.int16)
            xp = np.arange(samples.size)
            x = np.linspace(0, samples.size - 1, new_len)
            return np.interp(x, xp, samples).astype(np.int16)

    def _snapshot_float32(self) -> np.ndarray:
        with self._buf_lock:
            if len(self._buf) == 0:
                return np.zeros(0, dtype=np.float32)
            arr = np.array(self._buf, dtype=np.int16)
        return arr.astype(np.float32) / 32768.0

    @staticmethod
    def _energy_trim(wav: np.ndarray) -> np.ndarray:
        """Trim leading/trailing silence with an adaptive RMS gate so the
        embedder only sees actual speech."""
        if wav.size == 0:
            return wav
        frame = max(1, int(0.02 * TARGET_SR))  # 20ms frames
        n = wav.size // frame
        if n == 0:
            return wav
        frames = wav[: n * frame].reshape(n, frame)
        rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
        if not np.any(rms):
            return wav
        gate = max(0.005, float(np.median(rms)) * 0.5)
        active = rms > gate
        if not np.any(active):
            return wav
        first = int(np.argmax(active))
        last = int(active.size - np.argmax(active[::-1]))
        return wav[first * frame : last * frame]

    # ---- embedding + matching --------------------------------------------

    def _compute_embedding(self, wav: np.ndarray) -> np.ndarray | None:
        if wav.size < int(self.min_audio_seconds * TARGET_SR):
            return None
        wav_t = self._energy_trim(wav)
        if wav_t.size < int(self.min_audio_seconds * TARGET_SR * 0.6):
            return None
        encoder = self._ensure_encoder()
        try:
            import torch
            with torch.no_grad():
                t = torch.from_numpy(wav_t.astype(np.float32, copy=False)).unsqueeze(0)
                if self._device != "cpu":
                    t = t.to(self._device)
                emb = encoder.encode_batch(t)
                # encode_batch returns (batch, 1, dim), squeeze to (dim,)
                emb = emb.squeeze().detach().cpu().numpy().astype(np.float32)
            n = float(np.linalg.norm(emb))
            if n < 1e-8 or emb.size != ECAPA_DIM:
                return None
            return (emb / n).astype(np.float32)
        except Exception as e:
            logger.warning(f"ecapa embedding failed: {e}")
            return None

    def _compute_current_embedding(self) -> np.ndarray | None:
        return self._compute_embedding(self._snapshot_float32())

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
                    f"not enough recent speech to fingerprint (need ~"
                    f"{self.min_audio_seconds:.1f}s of clear talking). Wait for "
                    f"them to keep talking and try again."
                ),
            }
        rec = self._voices.get(username)
        if rec is None:
            self._voices[username] = {
                "embeddings": emb[None, :].astype(np.float32),
                "created_at": time.time(),
                "updated_at": time.time(),
                "sample_count": 1,
            }
            action = "saved"
        else:
            embs = np.vstack([rec["embeddings"], emb[None, :]]).astype(np.float32)
            if embs.shape[0] > self.max_emb:
                embs = embs[-self.max_emb :]
            rec["embeddings"] = embs
            rec["updated_at"] = time.time()
            rec["sample_count"] = int(rec.get("sample_count", 0)) + 1
            action = "updated"
        self._save()
        return {
            "ok": True,
            "action": action,
            "username": username,
            "sample_count": int(self._voices[username]["sample_count"]),
            "stored_embeddings": int(self._voices[username]["embeddings"].shape[0]),
        }

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
                    f"not enough recent speech (need ~{self.min_audio_seconds:.1f}s "
                    f"of clear talking). Wait for them to talk a bit more then try again."
                ),
            }
        # max cosine over each person's stored embeddings
        scores: list[tuple[str, float]] = []
        for name, rec in self._voices.items():
            embs = rec["embeddings"]
            sims = embs @ emb  # both L2 normalized so dot == cosine
            scores.append((name, float(sims.max())))
        scores.sort(key=lambda kv: kv[1], reverse=True)
        best_name, best_score = scores[0]
        runner_score = scores[1][1] if len(scores) > 1 else -1.0

        if best_score < self.similarity_threshold:
            return {
                "username": "unknown",
                "confidence": round(max(0.0, best_score), 4),
                "threshold": self.similarity_threshold,
                "reason": (
                    "the voice does not match any saved profile clearly enough. "
                    "Look at the vision/image context to figure out who is "
                    "speaking, or ask their name and call saveVoice to remember "
                    "them next time."
                ),
            }
        if len(scores) > 1 and (best_score - runner_score) < self.margin:
            return {
                "username": "unknown",
                "confidence": round(best_score, 4),
                "threshold": self.similarity_threshold,
                "reason": (
                    "the voice is close to two or more saved profiles and we "
                    "cant tell which one. Ask them their name to be sure, then "
                    "call saveVoice with the right name to refine the fingerprint."
                ),
            }
        return {
            "username": best_name,
            "confidence": round(best_score, 4),
            "threshold": self.similarity_threshold,
        }

    def list_voices(self) -> list[dict]:
        out = []
        for name, rec in self._voices.items():
            out.append(
                {
                    "username": name,
                    "created_at": rec.get("created_at"),
                    "updated_at": rec.get("updated_at"),
                    "sample_count": int(rec.get("sample_count", 1)),
                    "stored_embeddings": int(rec["embeddings"].shape[0]),
                }
            )
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
                embs = np.asarray(f["embeddings"], dtype=np.float32)
        except Exception as e:
            logger.warning(f"failed to load voices.npz: {e}")
            return

        # Detect old resemblyzer format (256-dim) and move it aside
        # instead of corrupting the new store.
        if embs.ndim != 2 or (embs.shape[1] != ECAPA_DIM and embs.shape[1] != 0):
            self._migrate_legacy(embs.shape[1] if embs.ndim == 2 else None)
            return

        try:
            with open(self._meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except Exception as e:
            logger.warning(f"failed to load voices.json: {e}")
            meta = {}

        # New format: one row per embedding, names parallel to it (with
        # repeats for users who have multiple captures).
        groups: dict[str, list[np.ndarray]] = {}
        if len(names) == embs.shape[0]:
            for i, raw in enumerate(names):
                name = str(raw)
                groups.setdefault(name, []).append(embs[i].astype(np.float32))
        else:
            logger.warning("voices.npz names/embeddings length mismatch, ignoring")
            return

        for name, vecs in groups.items():
            arr = np.stack(vecs).astype(np.float32)
            # re-normalize defensively in case an old build saved unnormalized
            norms = np.linalg.norm(arr, axis=1, keepdims=True)
            norms[norms < 1e-8] = 1.0
            arr = arr / norms
            if arr.shape[0] > self.max_emb:
                arr = arr[-self.max_emb :]
            m = meta.get(name, {})
            self._voices[name] = {
                "embeddings": arr.astype(np.float32),
                "created_at": float(m.get("created_at", 0.0)),
                "updated_at": float(m.get("updated_at", 0.0)),
                "sample_count": int(m.get("sample_count", arr.shape[0])),
            }
        logger.info(f"voiceid loaded {len(self._voices)} saved voice(s)")

    def _migrate_legacy(self, dim: int | None):
        """Old saves used a different encoder (resemblyzer, 256-dim).
        Move them aside so the user doesnt lose them, but start fresh."""
        suffix = ".legacy"
        if dim:
            suffix = f".legacy_{dim}d"
        try:
            shutil.move(str(self._voices_path), str(self._voices_path) + suffix)
            if self._meta_path.exists():
                shutil.move(str(self._meta_path), str(self._meta_path) + suffix)
            logger.warning(
                "voiceid: existing voices were from a different encoder, moved aside as %s. "
                "Re-save voices with saveVoice to fingerprint them with the new model.",
                self._voices_path.name + suffix,
            )
        except Exception as e:
            logger.warning(f"voiceid: failed to migrate legacy voices: {e}")

    def _save(self):
        try:
            if not self._voices:
                for p in (self._voices_path, self._meta_path):
                    if p.exists():
                        p.unlink()
                return
            names_flat: list[str] = []
            rows: list[np.ndarray] = []
            for name, rec in self._voices.items():
                for row in rec["embeddings"]:
                    names_flat.append(name)
                    rows.append(row.astype(np.float32))
            embs = (
                np.stack(rows).astype(np.float32)
                if rows
                else np.zeros((0, ECAPA_DIM), np.float32)
            )
            np.savez(self._voices_path, names=np.array(names_flat), embeddings=embs)
            meta = {
                n: {
                    "created_at": rec.get("created_at"),
                    "updated_at": rec.get("updated_at"),
                    "sample_count": int(rec.get("sample_count", 1)),
                }
                for n, rec in self._voices.items()
            }
            with open(self._meta_path, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, indent=2)
        except Exception as e:
            logger.error(f"failed to save voices: {e}")
