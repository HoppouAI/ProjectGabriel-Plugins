"""OmniVoice streaming TTS engine.

Vendored copy of omnivoice_tts/provider.py for the standalone server.
Identical class (`OmniVoiceProvider`), identical behavior. The standalone
server in server.py instantiates this with a tiny fake config object so
the rest of the plugin code never needs to know it's not inside Gabriel.

Keep this file in sync with omnivoice_tts/provider.py when you change
the model loading or synth pipeline. Both share the same _warm_cache so
keeping the constructor signature aligned matters.

Wraps `k2-fsa/OmniVoice` (https://github.com/k2-fsa/OmniVoice) as a Gabriel
TTS source. Mirrors the public surface of the host `QwenTTSProvider`:

    p = OmniVoiceProvider(config)
    p.start()
    p.feed_text("hello ")
    p.feed_text("world.")
    p.turn_complete()
    pcm = await p.get_audio()      # 16-bit PCM mono at audio.receive_sample_rate
    p.interrupt()
    p.stop()

Architecture (single in-process model, gpu inference):
  feed_text() -> _text_queue (thread-safe)
    -> _splitter_thread sentence splits via stream2sentence
    -> _sentence_queue
      -> _synth_thread batches up to N sentences and runs
         OmniVoice.generate(text=batch, voice_clone_prompt=prompt, ...)
        -> int16 PCM
          -> _audio_queue (asyncio.Queue, fed via call_soon_threadsafe)
            -> get_audio() called by the host TTS loop

OmniVoice is a diffusion language model so each `generate()` call covers a
whole sentence in one forward pass. True chunk-by-chunk audio streaming
inside one sentence isn't a thing the model exposes; we get cheap
"streaming" by sentence-level batching, exactly like the standalone
omnivoice-serve flow.

Optimizations carried over from the standalone server:
  - torch.set_grad_enabled(False) + torch.inference_mode() everywhere
  - optional FA2 + CUDA graph cache via .perf
  - low-VRAM autodetect (auto empty_cache between sentences)
  - anchor-sentence trick for auto voice (lock the voice on sentence 1)
  - voice clone prompts cached to disk so restarts skip ref-audio encoding
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import queue
import re
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002702-\U000027B0"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "\U000020E3"
    "\U00002600-\U000026FF"
    "\U00002300-\U000023FF"
    "\U0000200B-\U0000200F"
    "\U0000205F-\U00002060"
    "]+",
    flags=re.UNICODE,
)


def _strip_emojis(text: str) -> str:
    cleaned = _EMOJI_RE.sub(" ", text)
    return re.sub(r"  +", " ", cleaned).strip()


# nonverbal tags omnivoice's tokenizer actually understands (see
# omnivoice.models.omnivoice._NONVERBAL_PATTERN). anything else inside
# square brackets is something the AI invented (eg [amazed], [happy])
# and would just get spoken letter by letter, so strip it.
_OMNIVOICE_KEPT_TAGS = {
    "laughter", "sigh",
    "confirmation-en",
    "question-en", "question-ah", "question-oh",
    "question-ei", "question-yi",
    "surprise-ah", "surprise-oh", "surprise-wa", "surprise-yo",
    "dissatisfaction-hnn",
}
_BRACKET_TAG_RE = re.compile(r"\[([^\[\]]{1,40})\]")


def _strip_unsupported_tags(text: str) -> str:
    def repl(m):
        inner = m.group(1).strip().lower()
        if inner in _OMNIVOICE_KEPT_TAGS:
            # keep, but normalize to lowercase the tokenizer expects
            return f"[{inner}]"
        return " "
    cleaned = _BRACKET_TAG_RE.sub(repl, text)
    return re.sub(r"  +", " ", cleaned).strip()


# threshold below which we flip into "low vram" mode (more aggressive cache
# clearing between sentences). Matches the server-side default.
_LOW_VRAM_THRESHOLD_GB = 8.5


def _autodetect_device() -> str:
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def _resolve_dtype(name: str):
    import torch
    name = (name or "float16").lower()
    return {
        "float16": torch.float16,
        "fp16": torch.float16,
        "half": torch.float16,
        "bfloat16": torch.bfloat16,
        "bf16": torch.bfloat16,
        "float32": torch.float32,
        "fp32": torch.float32,
        "float": torch.float32,
    }.get(name, torch.float16)


def _resample_int16_safe(arr_f32: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr:
        return arr_f32
    try:
        from scipy.signal import resample_poly
        from math import gcd
        g = gcd(src_sr, dst_sr)
        return resample_poly(arr_f32, dst_sr // g, src_sr // g).astype(np.float32, copy=False)
    except Exception:
        # crude linear fallback. quality is fine for 24k -> 24k anyway.
        ratio = dst_sr / float(src_sr)
        new_len = int(round(arr_f32.shape[0] * ratio))
        if new_len <= 0:
            return arr_f32
        x_old = np.linspace(0.0, 1.0, num=arr_f32.shape[0], endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=new_len, endpoint=False)
        return np.interp(x_new, x_old, arr_f32).astype(np.float32, copy=False)


def _to_mono_float_np(audio) -> np.ndarray:
    """Coerce a generate() output sample to a 1D float32 numpy array.

    Upstream omnivoice 0.1.5 returns list[np.ndarray] with shape (T,),
    the custom fork returns list[torch.Tensor] with shape (1, T). We
    accept either and any dtype/device.
    """
    if isinstance(audio, np.ndarray):
        arr = audio
    else:
        # assume torch tensor
        t = audio
        try:
            t = t.detach()
        except AttributeError:
            pass
        try:
            t = t.to("cpu")
        except AttributeError:
            pass
        try:
            t = t.float()
        except AttributeError:
            pass
        arr = t.numpy()
    arr = np.asarray(arr)
    if arr.ndim > 1:
        arr = arr.reshape(-1)
    if arr.dtype != np.float32:
        arr = arr.astype(np.float32, copy=False)
    return arr


def _free_torch_vram():
    """Best-effort gc.collect + cuda empty_cache. Safe to call anywhere."""
    try:
        import gc
        gc.collect()
    except Exception:
        pass
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            try:
                torch.cuda.ipc_collect()
            except Exception:
                pass
    except Exception:
        pass


def _cuda_vram_used_gb(device: str | None) -> float | None:
    """Return current allocated cuda memory in GB for a device string, or None."""
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        if device and device.startswith("cuda"):
            idx = int(device.split(":")[1]) if ":" in device else 0
        else:
            idx = 0
        return torch.cuda.memory_allocated(idx) / (1024 ** 3)
    except Exception:
        return None


def _unload_asr_model(model) -> bool:
    """Drop the ASR pipeline off an OmniVoice model and free its vram.

    Whisper is only used once, to transcribe the reference audio when
    ref_text is missing. After the voice clone prompt is built, the
    pipeline just sits around eating ~500 MB to 1 GB of vram. Nuke it.

    Returns True if something was dropped, False if there was nothing
    to unload (asr was never loaded).
    """
    pipe = getattr(model, "_asr_pipe", None)
    if pipe is None:
        return False

    # null out the pipeline's internal handles so the underlying
    # whisper / tokenizer / feature_extractor become collectable
    for attr in ("model", "tokenizer", "feature_extractor",
                 "image_processor", "processor"):
        try:
            setattr(pipe, attr, None)
        except Exception:
            pass

    try:
        model._asr_pipe = None
    except Exception:
        pass

    _free_torch_vram()
    return True


class OmniVoiceProvider:
    # Process-wide warm cache. Lets a background warmup thread populate
    # the model + voice prompt before the first session, and lets a
    # later session (reconnect, etc) reuse the same model instead of
    # paying the load cost again.
    _warm_cache: dict = {}
    _warm_lock = threading.Lock()
    # Per-key singleflight lock so the background warmup thread and the
    # host's session-start loader cant both blast through the cache miss
    # at the same time and each do a full 5s load + ~5gb vram alloc.
    _load_locks: dict = {}

    def __init__(self, config, local_overrides: dict | None = None, data_dir: Path | None = None):
        self.config = config
        overrides = local_overrides or {}

        def cfg(key, default=None):
            if key in overrides and overrides[key] not in (None, ""):
                return overrides[key]
            return config.get("plugins", "omnivoice_tts", key, default=default)

        self._model_path = str(cfg("model", "k2-fsa/OmniVoice") or "k2-fsa/OmniVoice")
        self._device = cfg("device", None) or _autodetect_device()
        self._dtype_name = str(cfg("dtype", "float16") or "float16")

        self._ref_audio = cfg("ref_audio", None) or None
        self._ref_text = cfg("ref_text", None) or None
        self._instruct = cfg("instruct", None) or None
        self._language = cfg("language", None) or None

        self._num_step = int(cfg("num_step", 16))
        self._guidance_scale = float(cfg("guidance_scale", 2.0))
        sp = cfg("speed", None)
        self._speed = float(sp) if sp not in (None, "", "null") else None
        self._denoise = bool(cfg("denoise", True))

        self._stream_batch_size = max(1, int(cfg("stream_batch_size", 2)))
        self._anchor_first_sentence = bool(cfg("anchor_first_sentence", True))
        self._first_chunk_min_samples = int(cfg("first_chunk_min_samples", 1920))

        self._use_flash_attn = bool(cfg("use_flash_attn", False))
        self._use_cuda_graphs = bool(cfg("use_cuda_graphs", False))
        self._max_graph_cache = int(cfg("max_graph_cache", 8))

        self._asr_model = str(cfg("asr_model", "openai/whisper-base")
                              or "openai/whisper-base")
        self._cache_voice = bool(cfg("cache_voice", True))
        self._low_vram = bool(cfg("low_vram", False))
        # standalone server flips this so the cached model keeps whisper
        # around for fresh ref_audio uploads
        self._force_asr = bool(cfg("force_asr", False))

        if data_dir is None:
            data_dir = Path(".") / "data" / "plugins" / "omnivoice_tts"
        self._voice_cache_dir = Path(data_dir) / "voices"

        self._target_sr = config.get("audio", "receive_sample_rate", default=24000)

        # queues + lifecycle
        self._text_queue: "queue.Queue[str | None]" = queue.Queue()
        self._sentence_queue: "queue.Queue[str | None]" = queue.Queue()
        self._audio_queue: "asyncio.Queue[bytes]" = asyncio.Queue()

        self._running = False
        self._interrupted = False
        self._splitter_thread: threading.Thread | None = None
        self._synth_thread: threading.Thread | None = None
        self._loader_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        self._model = None
        self._voice_clone_prompt = None  # locked at warmup or first sentence
        self._sample_rate: int | None = None
        self._ready = threading.Event()
        self._load_error: str | None = None
        # set per turn so the anchor-sentence trick only fires once at the
        # very first sentence of the response when in auto-voice mode
        self._turn_anchor_done = False
        # bumped on every interrupt(). synth thread snapshots this at the
        # top of each sentence and drops any pcm whose epoch is stale, so
        # an in flight gpu generate from before the interrupt cant leak
        # audio into the next turn.
        self._gen_epoch = 0

    # ── public api ───────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._interrupted = False
        try:
            import nltk
            nltk.download("punkt_tab", quiet=True)
        except Exception:
            pass

        self._voice_cache_dir.mkdir(parents=True, exist_ok=True)
        self._loader_thread = threading.Thread(
            target=self._load_model_and_voice, daemon=True,
        )
        self._loader_thread.start()
        self._splitter_thread = threading.Thread(
            target=self._splitter_loop, daemon=True,
        )
        self._splitter_thread.start()
        # synth thread is started lazily in get_audio() once we have an
        # asyncio loop reference to push pcm onto.
        logger.info(
            "omnivoice_tts started (model=%s device=%s dtype=%s voice=%s)",
            self._model_path, self._device, self._dtype_name,
            self._describe_voice(),
        )

    def stop(self):
        self._running = False
        self._interrupted = True
        self._text_queue.put(None)
        self._sentence_queue.put(None)
        for thread in (self._splitter_thread, self._synth_thread, self._loader_thread):
            if thread is not None:
                thread.join(timeout=3)
        self._splitter_thread = None
        self._synth_thread = None
        self._loader_thread = None
        # don't drop self._model here, the warm cache still holds it for the
        # next session. local refs go away, gpu memory stays parked.
        self._model = None
        self._voice_clone_prompt = None
        self._ready.clear()

    def feed_text(self, text: str):
        if not text:
            return
        self._interrupted = False
        logger.debug("omnivoice_tts feed_text: %r", text)
        self._text_queue.put(text)

    def turn_complete(self):
        # sentinel flushes the splitter so any half buffered text gets
        # forced into a sentence. also resets the anchor flag for the
        # next turn.
        self._text_queue.put(None)
        self._turn_anchor_done = False

    def interrupt(self):
        self._interrupted = True
        # bounce the anchor flag so we re capture the voice on the next
        # turns first sentence instead of carrying a half captured one
        self._turn_anchor_done = False
        # bump epoch BEFORE draining queues. any push from an in flight
        # generate that finishes after this point will see the new epoch
        # and silently drop instead of poisoning the next turn.
        self._gen_epoch += 1
        drained = 0
        for q in (self._text_queue, self._sentence_queue):
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except queue.Empty:
                    break
        self._text_queue.put(None)
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._drain_audio_queue)
        logger.debug(
            "omnivoice_tts interrupted (drained %d queued items, epoch=%d)",
            drained, self._gen_epoch,
        )

    def _drain_audio_queue(self):
        while True:
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def get_audio(self) -> bytes | None:
        self._ensure_loop_and_synth()
        try:
            return await asyncio.wait_for(self._audio_queue.get(), timeout=0.1)
        except (asyncio.TimeoutError, asyncio.QueueEmpty):
            return None

    # ── internals ────────────────────────────────────────────────────────

    def _ensure_loop_and_synth(self):
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        if self._synth_thread is None:
            self._synth_thread = threading.Thread(
                target=self._synth_loop, daemon=True,
            )
            self._synth_thread.start()

    def _describe_voice(self) -> str:
        if self._ref_audio:
            return f"clone:{Path(self._ref_audio).name}"
        if self._instruct:
            return f"design:{self._instruct[:40]}"
        return "auto"

    # -- warm cache + loader ---------------------------------------------

    @staticmethod
    def _cache_key(model_path, device, dtype_name, asr_model,
                   use_flash_attn, use_cuda_graphs) -> tuple:
        # voice / ref_audio / instruct / language deliberately left out
        # so a per-client voice swap doesnt force the 5gb diffusion
        # model to reload. the voice prompt is built per-session and
        # already has its own on-disk cache keyed on the audio path.
        return (
            str(model_path or "k2-fsa/OmniVoice"),
            str(device or "cpu"),
            str(dtype_name or "float16").lower(),
            str(asr_model or "openai/whisper-base"),
            bool(use_flash_attn),
            bool(use_cuda_graphs),
        )

    def _my_cache_key(self) -> tuple:
        return self._cache_key(
            self._model_path, self._device, self._dtype_name,
            self._asr_model, self._use_flash_attn, self._use_cuda_graphs,
        )

    @classmethod
    def warmup(cls, *, model_path, device, dtype_name, ref_audio, ref_text,
               instruct, language, asr_model, use_flash_attn, use_cuda_graphs,
               max_graph_cache, cache_voice, voice_cache_dir, low_vram=False,
               force_asr=False):
        """Load the OmniVoice model + (optional) voice clone prompt into
        the process-wide warm cache on a background thread so the first
        session that picks omnivoice_tts starts instantly.

        `force_asr=True` makes the warmup load whisper alongside the
        model even when no ref_audio is configured. Standalone-server
        mode sets this so the cached model can transcribe ref_audio
        clips that arrive from clients without reloading."""
        key = cls._cache_key(
            model_path, device, dtype_name, asr_model,
            use_flash_attn, use_cuda_graphs,
        )
        with cls._warm_lock:
            if key in cls._warm_cache:
                return
        try:
            try:
                import nltk
                nltk.download("punkt_tab", quiet=True)
            except Exception:
                pass
            stub = cls.__new__(cls)
            stub._model_path = model_path
            stub._device = device
            stub._dtype_name = dtype_name
            stub._ref_audio = ref_audio
            stub._ref_text = ref_text
            stub._instruct = instruct
            stub._language = language
            stub._asr_model = asr_model
            stub._use_flash_attn = use_flash_attn
            stub._use_cuda_graphs = use_cuda_graphs
            stub._max_graph_cache = max_graph_cache
            stub._cache_voice = cache_voice
            stub._voice_cache_dir = Path(voice_cache_dir)
            stub._voice_cache_dir.mkdir(parents=True, exist_ok=True)
            stub._low_vram = low_vram
            stub._force_asr = force_asr
            stub._model = None
            stub._voice_clone_prompt = None
            stub._sample_rate = None
            stub._load_error = None
            stub._ready = threading.Event()
            logger.info(
                "omnivoice_tts: warming up model=%s device=%s dtype=%s in background ...",
                model_path, device, dtype_name,
            )
            stub._load_model_and_voice()
            if stub._model is None:
                logger.warning(
                    "omnivoice_tts: warmup did not finish cleanly: %s",
                    stub._load_error or "unknown error",
                )
            else:
                logger.info("omnivoice_tts: warmup done, first session will be hot")
        except Exception as e:
            logger.warning("omnivoice_tts: warmup thread crashed: %s", e)

    def _load_model_and_voice(self):
        # hot path: reuse a previously warmed model. voice prompt is
        # built per session (cheap, has its own on-disk cache) so a
        # client swapping ref_audio doesnt invalidate the heavy load.
        key = self._my_cache_key()
        with self._warm_lock:
            cached = self._warm_cache.get(key)
        if cached is not None:
            self._model, self._sample_rate = cached
            logger.info("omnivoice_tts: reusing pre-warmed model (hot start)")
            try:
                self._voice_clone_prompt = self._resolve_voice_clone_prompt()
            except Exception as e:
                logger.warning("omnivoice_tts: voice prompt resolve failed: %s", e)
                self._voice_clone_prompt = None
            self._ready.set()
            return

        # serialize concurrent loads of the same key. without this the
        # warmup thread and the host's session-start loader both miss
        # the cache, both call OmniVoice.from_pretrained, and we burn
        # ~5gb of vram + 5s loading the same model twice.
        with self._warm_lock:
            load_lock = self._load_locks.get(key)
            if load_lock is None:
                load_lock = threading.Lock()
                self._load_locks[key] = load_lock

        with load_lock:
            # re-check under the per-key lock, the other loader may
            # have just won and populated the cache while we waited.
            with self._warm_lock:
                cached = self._warm_cache.get(key)
            if cached is not None:
                self._model, self._sample_rate = cached
                logger.info("omnivoice_tts: reusing pre-warmed model (hot start)")
                try:
                    self._voice_clone_prompt = self._resolve_voice_clone_prompt()
                except Exception as e:
                    logger.warning("omnivoice_tts: voice prompt resolve failed: %s", e)
                    self._voice_clone_prompt = None
                self._ready.set()
                return

            try:
                import torch
                from omnivoice import OmniVoice
                try:
                    from .perf import apply_perf_tweaks
                except ImportError:
                    from perf import apply_perf_tweaks  # standalone mode

                torch.set_grad_enabled(False)

                t0 = time.monotonic()
                # ASR (whisper) is only needed when we have to transcribe
                # a ref_audio clip on the fly. force_asr is set by the
                # standalone server so a client uploading a fresh ref
                # clip later doesnt require reloading the diffusion
                # model just to add whisper.
                need_asr = bool(getattr(self, "_force_asr", False)) or (
                    bool(self._ref_audio) and not self._ref_text
                )
                logger.info(
                    "omnivoice_tts: loading model from %s on %s (dtype=%s, asr=%s) ...",
                    self._model_path, self._device, self._dtype_name, need_asr,
                )
                self._model = OmniVoice.from_pretrained(
                    self._model_path,
                    device_map=self._device,
                    dtype=_resolve_dtype(self._dtype_name),
                    load_asr=need_asr,
                    asr_model_name=self._asr_model,
                )
                self._sample_rate = int(self._model.sampling_rate)
                logger.info(
                    "omnivoice_tts: model loaded in %.1fs (sr=%d)",
                    time.monotonic() - t0, self._sample_rate,
                )

                # auto detect low vram if user didnt force it
                if not self._low_vram and self._device.startswith("cuda") and torch.cuda.is_available():
                    try:
                        gpu_idx = int(self._device.split(":")[1]) if ":" in self._device else 0
                        total_gb = torch.cuda.get_device_properties(gpu_idx).total_memory / (1024 ** 3)
                        if total_gb < _LOW_VRAM_THRESHOLD_GB:
                            self._low_vram = True
                            logger.info(
                                "omnivoice_tts: low-vram mode auto-enabled (%.1f GB)",
                                total_gb,
                            )
                    except Exception:
                        pass

                # opt-in perf tweaks
                if self._use_flash_attn or self._use_cuda_graphs:
                    enabled = apply_perf_tweaks(
                        self._model,
                        use_flash_attn=self._use_flash_attn,
                        use_cuda_graphs=self._use_cuda_graphs,
                        max_graph_cache=self._max_graph_cache,
                    )
                    logger.info("omnivoice_tts: perf tweaks: %s", enabled)

                self._voice_clone_prompt = self._resolve_voice_clone_prompt()

                # whisper is only used to transcribe ref_audio when
                # ref_text is missing. once the clone prompt is built
                # (or we decided we dont need one) the pipeline is dead
                # weight. drop it. in standalone-server mode with
                # force_asr=True we keep whisper around so the next
                # client upload doesnt have to reload the whole model.
                if not getattr(self, "_force_asr", False):
                    try:
                        vram_before = _cuda_vram_used_gb(self._device)
                        dropped = _unload_asr_model(self._model)
                        if dropped:
                            vram_after = _cuda_vram_used_gb(self._device)
                            if vram_before is not None and vram_after is not None:
                                logger.info(
                                    "omnivoice_tts: unloaded asr model (vram %.2f -> %.2f GB)",
                                    vram_before, vram_after,
                                )
                            else:
                                logger.info("omnivoice_tts: unloaded asr model")
                    except Exception as e:
                        logger.debug("omnivoice_tts: asr unload failed: %s", e)

                # populate the warm cache so a session restart skips this work
                with self._warm_lock:
                    self._warm_cache[key] = (self._model, self._sample_rate)
                self._ready.set()
            except Exception as e:
                self._load_error = str(e)
                logger.exception("omnivoice_tts: failed to load model / voice: %s", e)
                self._ready.set()

    def _resolve_voice_clone_prompt(self):
        """If config sets `ref_audio`, build a VoiceClonePrompt once and
        cache it to disk. Returns None when in voice-design or auto mode."""
        if not self._ref_audio:
            return None

        ref_path = Path(str(self._ref_audio)).expanduser()
        if not ref_path.is_file():
            logger.warning(
                "omnivoice_tts: ref_audio not found at %s, falling back to auto voice",
                ref_path,
            )
            return None

        cache_path = self._voice_cache_path(ref_path)
        if self._cache_voice and cache_path is not None and cache_path.is_file():
            try:
                prompt = self._load_voice_prompt(cache_path)
                logger.info(
                    "omnivoice_tts: loaded cached voice prompt %s", cache_path.name,
                )
                return prompt
            except Exception as e:
                logger.warning(
                    "omnivoice_tts: cached voice prompt failed (%s), re-encoding",
                    e,
                )

        logger.info(
            "omnivoice_tts: encoding voice clone prompt from %s ...", ref_path,
        )
        try:
            import torch
            with torch.inference_mode():
                prompt = self._model.create_voice_clone_prompt(
                    ref_audio=str(ref_path),
                    ref_text=self._ref_text,
                )
        except Exception as e:
            logger.warning(
                "omnivoice_tts: voice clone encoding failed: %s, falling back to auto",
                e,
            )
            return None

        if self._cache_voice and cache_path is not None:
            try:
                self._save_voice_prompt(prompt, cache_path)
                logger.info("omnivoice_tts: cached voice prompt to %s", cache_path)
            except Exception as e:
                logger.warning("omnivoice_tts: failed to save voice cache: %s", e)
        return prompt

    def _voice_cache_path(self, audio_path: Path) -> Path | None:
        try:
            mtime = int(os.path.getmtime(audio_path))
        except OSError:
            return None
        key_src = (
            f"{os.path.abspath(audio_path)}|{mtime}|{self._model_path}|"
            f"{self._dtype_name}|{self._ref_text or ''}"
        )
        digest = hashlib.sha1(key_src.encode("utf-8")).hexdigest()[:16]
        stem = re.sub(r"[^A-Za-z0-9_\-]", "_", audio_path.stem)[:32] or "voice"
        return self._voice_cache_dir / f"{stem}_{digest}.pt"

    def _save_voice_prompt(self, prompt, path: Path) -> None:
        import torch
        torch.save(
            {
                "ref_audio_tokens": prompt.ref_audio_tokens.detach().cpu(),
                "ref_text": prompt.ref_text,
                "ref_rms": float(prompt.ref_rms),
            },
            str(path),
        )

    def _load_voice_prompt(self, path: Path):
        import torch
        from omnivoice.models.omnivoice import VoiceClonePrompt
        data = torch.load(str(path), map_location="cpu", weights_only=True)
        tokens = data["ref_audio_tokens"]
        # move tokens onto the model's device so generate() doesn't pay the
        # transfer cost on every sentence
        try:
            tokens = tokens.to(self._model.device)
        except Exception:
            pass
        return VoiceClonePrompt(
            ref_audio_tokens=tokens,
            ref_text=str(data["ref_text"]),
            ref_rms=float(data["ref_rms"]),
        )

    # -- splitter (sync, stream2sentence is blocking) --------------------

    def _text_generator(self):
        last_text_time = time.monotonic()
        while True:
            try:
                chunk = self._text_queue.get(timeout=0.1)
            except queue.Empty:
                if time.monotonic() - last_text_time > 1.5:
                    return
                if self._interrupted or not self._running:
                    return
                continue
            if chunk is None:
                return
            last_text_time = time.monotonic()
            yield chunk

    def _splitter_loop(self):
        try:
            from stream2sentence import generate_sentences
        except ImportError:
            logger.error(
                "omnivoice_tts: stream2sentence is required, install with "
                "`.\\bin\\uv.exe pip install stream2sentence nltk`"
            )
            return

        while self._running:
            text_gen = self._text_generator()
            try:
                for sentence in generate_sentences(
                    text_gen,
                    minimum_sentence_length=10,
                    minimum_first_fragment_length=10,
                    quick_yield_single_sentence_fragment=True,
                    context_size=3,
                    context_size_look_overhead=3,
                    force_first_fragment_after_words=15,
                ):
                    if self._interrupted or not self._running:
                        break
                    s = _strip_emojis(sentence)
                    s = _strip_unsupported_tags(s)
                    if s:
                        logger.info("omnivoice_tts sentence: %r", s[:80])
                        self._sentence_queue.put(s)
            except Exception as e:
                if not self._interrupted:
                    logger.error("omnivoice_tts splitter error: %s", e)

    # -- synth (runs the model serially, batches sentences) -------------

    def _drain_pending_sentences(self, max_count: int) -> list[str]:
        """Pull up to max_count more sentences from the queue without
        waiting. Used to grow the current batch when more sentences are
        already ready."""
        out: list[str] = []
        while len(out) < max_count:
            try:
                s = self._sentence_queue.get_nowait()
            except queue.Empty:
                break
            if s is None:
                break
            out.append(s)
        return out

    def _synth_loop(self):
        self._ready.wait()
        if self._model is None:
            logger.error(
                "omnivoice_tts: synth loop bailing, model not loaded (%s)",
                self._load_error or "unknown error",
            )
            return

        while self._running:
            try:
                sentence = self._sentence_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if sentence is None:
                continue
            if self._interrupted:
                continue

            # snapshot the current epoch. if interrupt() fires while the
            # gpu is busy below, every push for this sentence will be
            # dropped at the push site, no playback leakage.
            epoch = self._gen_epoch

            # in auto-voice mode we generate the very first sentence of a
            # turn solo, then capture it as a VoiceClonePrompt so all
            # subsequent sentences in the same turn share the same voice.
            in_auto_mode = (
                self._voice_clone_prompt is None and not self._instruct
            )
            if (in_auto_mode and not self._turn_anchor_done
                    and self._anchor_first_sentence):
                try:
                    audio = self._generate_one(sentence, anchor_capture=True)
                    self._turn_anchor_done = True
                    if audio is not None and epoch == self._gen_epoch:
                        self._push_audio(audio, epoch=epoch)
                except Exception as e:
                    if not self._interrupted:
                        logger.warning(
                            "omnivoice_tts anchor synth failed for %r: %s",
                            sentence[:60], e,
                        )
                continue

            # build a batch up to stream_batch_size
            batch = [sentence]
            extra = self._drain_pending_sentences(self._stream_batch_size - 1)
            batch.extend(extra)

            try:
                audios = self._generate_batch(batch)
            except Exception as e:
                if not self._interrupted:
                    logger.warning(
                        "omnivoice_tts synth failed for %d-batch: %s",
                        len(batch), e,
                    )
                continue
            for audio in audios:
                if self._interrupted or epoch != self._gen_epoch:
                    break
                self._push_audio(audio, epoch=epoch)

            if self._low_vram:
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass

    def _gen_kwargs(self) -> dict:
        from omnivoice import OmniVoiceGenerationConfig
        gen_config = OmniVoiceGenerationConfig(
            num_step=self._num_step,
            guidance_scale=self._guidance_scale,
            denoise=self._denoise,
        )
        kw: dict[str, Any] = {"generation_config": gen_config}
        if self._language:
            kw["language"] = self._language
        if self._speed is not None and self._speed != 1.0:
            kw["speed"] = self._speed
        return kw

    def _generate_one(self, sentence: str, anchor_capture: bool = False):
        import torch
        kw = self._gen_kwargs()
        if self._voice_clone_prompt is not None:
            kw["voice_clone_prompt"] = self._voice_clone_prompt
        elif self._instruct:
            kw["instruct"] = self._instruct
        kw["text"] = sentence
        with torch.inference_mode():
            audios = self._model.generate(**kw)
        if not audios:
            return None
        audio = audios[0]
        if anchor_capture:
            try:
                # create_voice_clone_prompt wants (waveform_tensor_1d, sr).
                # generate() may return either a (1, T) torch tensor or a
                # (T,) numpy array depending on the omnivoice version, so
                # normalize to a 1D float32 torch tensor.
                arr = _to_mono_float_np(audio)
                wav = torch.from_numpy(arr.astype(np.float32, copy=False))
                with torch.inference_mode():
                    self._voice_clone_prompt = self._model.create_voice_clone_prompt(
                        ref_audio=(wav, self._sample_rate),
                        ref_text=sentence,
                    )
                logger.info(
                    "omnivoice_tts: locked voice from anchor sentence (%d chars)",
                    len(sentence),
                )
            except Exception as e:
                logger.warning(
                    "omnivoice_tts: failed to capture anchor voice: %s", e,
                )
        return audio

    def _generate_batch(self, batch: list[str]):
        import torch
        kw = self._gen_kwargs()
        if self._voice_clone_prompt is not None:
            kw["voice_clone_prompt"] = self._voice_clone_prompt
        elif self._instruct:
            kw["instruct"] = self._instruct
        kw["text"] = batch if len(batch) > 1 else batch[0]
        with torch.inference_mode():
            audios = self._model.generate(**kw)
        # generate() returns list[Tensor] regardless, but normalize
        if isinstance(audios, list):
            return audios
        return [audios]

    def _push_audio(self, audio, epoch: int | None = None):
        """Convert a single audio sample to int16 PCM at target_sr and
        push onto the asyncio audio queue. Handles both torch tensors
        ((1, T) or (T,), any dtype, any device) and numpy arrays ((T,)
        or (1, T)).

        If `epoch` is given and the provider has since been interrupted
        (epoch bumped), the chunk is silently dropped.
        """
        if epoch is not None and epoch != self._gen_epoch:
            return
        arr = _to_mono_float_np(audio)
        if arr.size == 0:
            return
        if self._sample_rate and self._target_sr and self._sample_rate != self._target_sr:
            arr = _resample_int16_safe(arr, self._sample_rate, self._target_sr)
        self._push_pcm(arr, epoch=epoch)

    def _push_pcm(self, arr: np.ndarray, epoch: int | None = None):
        if arr.size == 0:
            return
        if epoch is not None and epoch != self._gen_epoch:
            return
        # tensor.numpy() can return a read-only view, so don't use out=arr
        arr = np.clip(arr, -1.0, 1.0)
        pcm = (arr * 32767.0).astype(np.int16, copy=False).tobytes()
        if not self._loop or not self._loop.is_running():
            return
        self._loop.call_soon_threadsafe(self._audio_queue.put_nowait, pcm)
