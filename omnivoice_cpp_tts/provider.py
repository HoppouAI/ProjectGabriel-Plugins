"""omnivoice.cpp streaming TTS provider.

Mirrors the public surface of the host `QwenTTSProvider` (and the sibling
pocket_tts / omnivoice_tts providers) so a Gabriel session can use it as a
drop in:

    p = OmniVoiceCppProvider(config)
    p.start()
    p.feed_text("hello ")
    p.feed_text("world.")
    p.turn_complete()
    pcm = await p.get_audio()      # 16-bit PCM mono at audio.receive_sample_rate
    p.interrupt()
    p.stop()

Unlike omnivoice_tts (python + torch diffusion) this talks to the native
omnivoice.cpp engine through ctypes. No torch, no server, the C lib loads
quantized GGUF weights and runs the whole synth path. We feed it text one
sentence at a time and pump the streaming audio chunks it hands back.

Architecture (single in-process native engine):
  feed_text() -> _text_queue (thread-safe)
    -> _splitter_thread sentence-splits via stream2sentence
    -> _sentence_queue
      -> _synth_thread serially calls ov_synthesize() per sentence with a
         streaming on_chunk callback that converts float -> int16 PCM
        -> _audio_queue (asyncio.Queue, fed via call_soon_threadsafe)
          -> get_audio() called by the host TTS loop
"""
from __future__ import annotations

import ctypes as C
import logging
import os
import queue
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from . import _ffi

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


# nonverbal tags OmniVoice's tokenizer actually understands, from the
# k2-fsa/OmniVoice readme "Non-Verbal & Pronunciation Control". anything
# else inside brackets the AI invents (eg [amazed], [happy]) would be read
# out letter by letter, so it gets stripped.
_OMNIVOICE_TAGS = {
    "laughter", "sigh",
    "confirmation-en",
    "question-en", "question-ah", "question-oh",
    "question-ei", "question-yi",
    "surprise-ah", "surprise-oh", "surprise-wa", "surprise-yo",
    "dissatisfaction-hnn",
}

# common things the model emits that map onto a real tag. the AI rarely
# writes the exact canonical token, so fold the obvious variants in
# instead of throwing the expression away.
_TAG_ALIASES = {
    "laugh": "laughter", "laughs": "laughter", "laughing": "laughter",
    "laughter": "laughter", "laugher": "laughter", "laughes": "laughter",
    "chuckle": "laughter", "chuckles": "laughter", "chuckling": "laughter",
    "giggle": "laughter", "giggles": "laughter", "giggling": "laughter",
    "haha": "laughter", "haha!": "laughter", "lol": "laughter",
    "sighs": "sigh", "sighing": "sigh", "exhale": "sigh", "exhales": "sigh",
    "gasp": "surprise-ah", "gasps": "surprise-ah",
    "surprise": "surprise-ah", "surprised": "surprise-ah",
    "question": "question-en", "questioning": "question-en",
    "confirm": "confirmation-en", "confirmation": "confirmation-en",
    "mhm": "confirmation-en", "mm-hmm": "confirmation-en", "uh-huh": "confirmation-en",
    "dissatisfaction": "dissatisfaction-hnn", "dissatisfied": "dissatisfaction-hnn",
    "hmph": "dissatisfaction-hnn", "hnn": "dissatisfaction-hnn",
}

# bounded length so we dont chew through a real quote that happens to have
# brackets around something long.
_BRACKET_TAG_RE = re.compile(r"\[([^\[\]]{1,40})\]")


def _normalize_nonverbal_tags(text: str) -> str:
    def repl(m):
        inner = m.group(1).strip().lower()
        if inner in _OMNIVOICE_TAGS:
            return f"[{inner}]"
        alias = _TAG_ALIASES.get(inner)
        if alias:
            return f"[{alias}]"
        return " "  # unknown tag, drop it
    cleaned = _BRACKET_TAG_RE.sub(repl, text)
    return re.sub(r"  +", " ", cleaned).strip()


# the two gguf files that load together, by variant
_BASE_FILE = "omnivoice-base-{v}.gguf"
_CODEC_FILE = "omnivoice-tokenizer-{v}.gguf"
_VALID_VARIANTS = ("F32", "BF16", "Q8_0", "Q4_K_M")

# hosted win64 vulkan prebuilt, pulled on first run if no lib is configured
_DEFAULT_LIB_URL = (
    "https://github.com/HoppouAI/ProjectGabriel-Plugin-Resources/raw/main/"
    "omnivoice_cpp_tts/omnivoice-cpp-vulkan-win64.zip"
)


def _resample_linear(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out or x.size == 0:
        return x.astype(np.float32, copy=False)
    n_out = int(round(x.shape[0] * sr_out / float(sr_in)))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    src_idx = np.linspace(0.0, x.shape[0] - 1, n_out)
    return np.interp(src_idx, np.arange(x.shape[0]), x).astype(np.float32)


class OmniVoiceCppProvider:
    # Process-wide warm cache. The heavy thing is the loaded native engine
    # (ov_context handle), keyed on the lib + model paths + compute flags.
    # Voice (instruct / ref_audio) is a per-synthesis param, NOT part of
    # the context, so swapping voices never reloads the model.
    _warm_cache: dict = {}
    _warm_lock = threading.Lock()
    _load_locks: dict = {}
    # one global log bridge install, guarded so we only wire it once
    _log_installed = False
    # serialize the one-time native lib download across threads
    _lib_download_lock = threading.Lock()

    def __init__(self, config, local_overrides: dict | None = None, data_dir: Path | None = None):
        self.config = config
        overrides = local_overrides or {}

        def cfg(key, default=None):
            if key in overrides and overrides[key] not in (None, ""):
                return overrides[key]
            return config.get("plugins", "omnivoice_cpp_tts", key, default=default)

        # native lib location. explicit config wins, else env var.
        self._lib_dir = (cfg("lib_dir", None)
                         or os.environ.get("OMNIVOICE_CPP_DIR")
                         or None)
        # if no lib is found locally, pull this prebuilt zip (windows only)
        self._lib_url = cfg("lib_url", None) or _DEFAULT_LIB_URL
        self._auto_download_lib = bool(cfg("auto_download_lib", True))

        # model selection. explicit paths win, else auto-download a variant
        # from the HF repo into data_dir/models.
        self._model_repo = str(cfg("model_repo", "Serveurperso/OmniVoice-GGUF")
                               or "Serveurperso/OmniVoice-GGUF")
        self._variant = str(cfg("model_variant", "Q4_K_M") or "Q4_K_M")
        self._base_model = cfg("base_model", None) or None
        self._codec_model = cfg("codec_model", None) or None

        # compute flags
        self._use_fa = bool(cfg("use_fa", True))
        self._clamp_fp16 = bool(cfg("clamp_fp16", False))

        # voice. ref_audio (cloning, needs ref_text) OR instruct (design).
        self._ref_audio = cfg("ref_audio", None) or None
        self._ref_text = cfg("ref_text", None) or None
        self._instruct = cfg("instruct", None) or None
        self._language = cfg("language", None) or ""

        # generation knobs -> MaskGIT sampler config
        self._num_step = int(cfg("num_step", 8))
        self._guidance_scale = float(cfg("guidance_scale", 2.0))
        self._t_shift = float(cfg("t_shift", 0.1))
        self._layer_penalty_factor = float(cfg("layer_penalty_factor", 5.0))
        self._position_temperature = float(cfg("position_temperature", 5.0))
        self._class_temperature = float(cfg("class_temperature", 0.0))
        self._seed = int(cfg("seed", 42))
        self._denoise = bool(cfg("denoise", True))
        self._preprocess_prompt = bool(cfg("preprocess_prompt", True))

        # skip past the first ~80ms so the receiver doesnt stutter on a
        # tiny initial frame. samples at the model's 24k native rate.
        self._first_chunk_min_samples = int(cfg("first_chunk_min_samples", 1920))

        if data_dir is None:
            data_dir = Path(".") / "data" / "plugins" / "omnivoice_cpp_tts"
        self._data_dir = Path(data_dir)
        self._models_dir = self._data_dir / "models"

        self._sample_rate = 24000  # OmniVoice native, refined after load
        self._target_sr = int(config.get("audio", "receive_sample_rate", default=24000))

        # resolved native handles, filled by the loader
        self._lib = None
        self._ctx = None  # ov_context handle (c_void_p)
        # voice reference samples (24k mono float32) if cloning
        self._ref_samples: np.ndarray | None = None

        # public mirrors for host logging / introspection
        self._model_path = f"omnivoice.cpp:{self._variant}"
        self._device = "native"

        # queues + lifecycle
        self._text_queue: "queue.Queue[str | None]" = queue.Queue()
        self._sentence_queue: "queue.Queue[str | None]" = queue.Queue()
        self._audio_queue: "asyncio.Queue[bytes]" = None  # set on first get_audio

        self._running = False
        self._interrupted = False
        self._splitter_thread: threading.Thread | None = None
        self._synth_thread: threading.Thread | None = None
        self._loader_thread: threading.Thread | None = None
        self._loop = None

        self._ready = threading.Event()
        self._load_error: str | None = None
        # bumped on every interrupt(). the synth on_chunk callback checks
        # it and aborts so a sentence mid stream when the user barges in
        # cant leak late chunks into the next turn.
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

        self._loader_thread = threading.Thread(
            target=self._load_engine_and_voice, daemon=True,
        )
        self._loader_thread.start()
        self._splitter_thread = threading.Thread(
            target=self._splitter_loop, daemon=True,
        )
        self._splitter_thread.start()
        logger.info(
            "omnivoice_cpp_tts started (variant=%s use_fa=%s voice=%s)",
            self._variant, self._use_fa, self._describe_voice(),
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
        # dont ov_free here, the warm cache still holds the engine for the
        # next session. local refs drop, gpu memory stays parked.
        self._lib = None
        self._ctx = None
        self._ready.clear()

    def feed_text(self, text: str):
        if not text:
            return
        self._interrupted = False
        logger.debug("omnivoice_cpp_tts feed_text: %r", text)
        self._text_queue.put(text)

    def turn_complete(self):
        # sentinel flushes the splitter so any half buffered text gets
        # forced into a sentence.
        self._text_queue.put(None)

    def interrupt(self):
        self._interrupted = True
        # bump epoch BEFORE draining so any in flight synth callback sees
        # the new epoch and aborts.
        self._gen_epoch += 1
        drained = 0
        for q in (self._text_queue, self._sentence_queue):
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except queue.Empty:
                    break
        # nudge the splitter so it doesnt sit on an empty generator
        self._text_queue.put(None)
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._drain_audio_queue)
        logger.debug(
            "omnivoice_cpp_tts interrupted (drained %d, epoch=%d)",
            drained, self._gen_epoch,
        )

    def _drain_audio_queue(self):
        if self._audio_queue is None:
            return
        import asyncio
        while True:
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def get_audio(self) -> bytes | None:
        self._ensure_loop_and_synth()
        import asyncio
        try:
            return await asyncio.wait_for(self._audio_queue.get(), timeout=0.1)
        except (asyncio.TimeoutError, asyncio.QueueEmpty):
            return None

    # ── internals ────────────────────────────────────────────────────────

    def _ensure_loop_and_synth(self):
        import asyncio
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        if self._audio_queue is None:
            self._audio_queue = asyncio.Queue()
        if self._synth_thread is None and self._running:
            self._synth_thread = threading.Thread(
                target=self._synth_loop, daemon=True,
            )
            self._synth_thread.start()

    def _describe_voice(self) -> str:
        if self._ref_audio:
            return f"clone:{Path(str(self._ref_audio)).name}"
        if self._instruct:
            return f"design:{self._instruct}"
        return "auto"

    # -- engine + voice loader (runs once on start) -----------------------

    def _resolve_lib_dir(self) -> str:
        dll = _ffi._dll_name()
        if self._lib_dir:
            return str(self._lib_dir)
        # bundled next to the plugin
        local = Path(__file__).parent / "native"
        if (local / dll).is_file():
            return str(local)
        # previously auto-downloaded into the plugin data dir
        downloaded = self._data_dir / "native"
        if (downloaded / dll).is_file():
            return str(downloaded)
        # auto-download the prebuilt. the hosted zip is win64, windows only.
        if self._auto_download_lib and self._lib_url and os.name == "nt":
            with OmniVoiceCppProvider._lib_download_lock:
                if not (downloaded / dll).is_file():
                    self._download_and_extract_lib(downloaded)
            if (downloaded / dll).is_file():
                return str(downloaded)
        raise FileNotFoundError(
            "omnivoice_cpp_tts: no native engine found. on windows it normally "
            "auto-downloads on first run. set plugins.omnivoice_cpp_tts.lib_dir to "
            "the folder holding omnivoice.dll (and its ggml dlls), or set the "
            "OMNIVOICE_CPP_DIR env var, or drop the dlls in the plugin's native/ "
            "folder. see the plugin README."
        )

    def _download_and_extract_lib(self, dest: Path):
        import urllib.request
        import zipfile
        dest.mkdir(parents=True, exist_ok=True)
        tmp = dest / "_download.zip"
        logger.info(
            "omnivoice_cpp_tts: downloading native engine from %s (one time) ...",
            self._lib_url,
        )
        try:
            with urllib.request.urlopen(self._lib_url, timeout=120) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
            with zipfile.ZipFile(tmp) as z:
                z.extractall(dest)
            logger.info("omnivoice_cpp_tts: native engine ready at %s", dest)
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass

    def _resolve_models(self) -> tuple[str, str]:
        """Return (base_gguf_path, codec_gguf_path), downloading from HF if
        explicit paths arent configured."""
        if self._base_model and self._codec_model:
            return str(self._base_model), str(self._codec_model)

        variant = self._variant
        if variant not in _VALID_VARIANTS:
            logger.warning(
                "omnivoice_cpp_tts: unknown model_variant %r, falling back to Q4_K_M "
                "(valid: %s)", variant, ", ".join(_VALID_VARIANTS),
            )
            variant = "Q4_K_M"

        try:
            from huggingface_hub import hf_hub_download
        except ImportError as e:
            raise RuntimeError(
                "huggingface_hub is required to auto-download the gguf models. "
                "either install it or set base_model + codec_model to local paths."
            ) from e

        self._models_dir.mkdir(parents=True, exist_ok=True)
        base_name = _BASE_FILE.format(v=variant)
        codec_name = _CODEC_FILE.format(v=variant)
        logger.info(
            "omnivoice_cpp_tts: resolving models %s + %s from %s ...",
            base_name, codec_name, self._model_repo,
        )
        base = hf_hub_download(
            repo_id=self._model_repo, filename=base_name,
            local_dir=str(self._models_dir),
        )
        codec = hf_hub_download(
            repo_id=self._model_repo, filename=codec_name,
            local_dir=str(self._models_dir),
        )
        return str(base), str(codec)

    def _engine_cache_key(self, lib_dir, base, codec) -> tuple:
        return (
            str(lib_dir),
            str(base),
            str(codec),
            bool(self._use_fa),
            bool(self._clamp_fp16),
        )

    def _load_engine_and_voice(self):
        try:
            lib_dir = self._resolve_lib_dir()
            base, codec = self._resolve_models()
        except Exception as e:
            self._load_error = str(e)
            logger.error("omnivoice_cpp_tts: %s", e)
            self._ready.set()
            return

        key = self._engine_cache_key(lib_dir, base, codec)

        with self._warm_lock:
            cached = self._warm_cache.get(key)
        if cached is not None:
            self._lib, self._ctx, self._sample_rate = cached
            logger.info("omnivoice_cpp_tts: reusing pre-warmed engine (hot start)")
            self._resolve_ref_voice_safe()
            self._ready.set()
            return

        with self._warm_lock:
            load_lock = self._load_locks.get(key)
            if load_lock is None:
                load_lock = threading.Lock()
                self._load_locks[key] = load_lock

        with load_lock:
            with self._warm_lock:
                cached = self._warm_cache.get(key)
            if cached is not None:
                self._lib, self._ctx, self._sample_rate = cached
                logger.info("omnivoice_cpp_tts: reusing pre-warmed engine (hot start)")
                self._resolve_ref_voice_safe()
                self._ready.set()
                return

            try:
                t0 = time.monotonic()
                lib = _ffi.load_library(lib_dir)
                self._install_log_bridge(lib)
                logger.info(
                    "omnivoice_cpp_tts: loading engine %s (base=%s codec=%s use_fa=%s) ...",
                    getattr(lib, "_ov_version_str", "?"),
                    Path(base).name, Path(codec).name, self._use_fa,
                )

                init = _ffi.ov_init_params()
                lib.ov_init_default_params(C.byref(init))
                init.abi_version = _ffi.OV_ABI_VERSION
                init.model_path = str(base).encode("utf-8")
                init.codec_path = str(codec).encode("utf-8")
                init.use_fa = self._use_fa
                init.clamp_fp16 = self._clamp_fp16

                ctx = lib.ov_init(C.byref(init))
                if not ctx:
                    err = lib.ov_last_error()
                    err = err.decode("utf-8", "replace") if err else "ov_init returned NULL"
                    raise RuntimeError(f"ov_init failed: {err}")

                self._lib = lib
                self._ctx = ctx
                try:
                    self._sample_rate = 24000  # OmniVoice fixed native rate
                except Exception:
                    pass

                logger.info(
                    "omnivoice_cpp_tts: engine loaded in %.1fs (sr=%d)",
                    time.monotonic() - t0, self._sample_rate,
                )

                with self._warm_lock:
                    self._warm_cache[key] = (lib, ctx, self._sample_rate)

                self._resolve_ref_voice_safe()
                self._ready.set()
            except Exception as e:
                self._load_error = str(e)
                logger.exception("omnivoice_cpp_tts: failed to load engine: %s", e)
                self._ready.set()

    def _install_log_bridge(self, lib):
        # route the native logger into our python logger once. keep the
        # CFUNCTYPE alive on the class so it isnt GC'd.
        if OmniVoiceCppProvider._log_installed:
            return
        _PY_LVL = {
            _ffi.OV_LOG_DEBUG: logging.DEBUG,
            _ffi.OV_LOG_INFO: logging.INFO,
            _ffi.OV_LOG_WARN: logging.WARNING,
            _ffi.OV_LOG_ERROR: logging.ERROR,
        }

        def _sink(level, msg, _ud):
            try:
                text = msg.decode("utf-8", "replace") if msg else ""
            except Exception:
                text = "<undecodable>"
            logger.log(_PY_LVL.get(int(level), logging.INFO), "engine: %s", text)

        cb = _ffi.OV_LOG_CB(_sink)
        OmniVoiceCppProvider._log_cb_ref = cb  # keep alive
        try:
            lib.ov_log_set(cb, None)
            OmniVoiceCppProvider._log_installed = True
        except Exception as e:
            logger.debug("omnivoice_cpp_tts: could not install log bridge: %s", e)

    def _resolve_ref_voice_safe(self):
        try:
            self._resolve_ref_voice()
        except Exception as e:
            logger.warning(
                "omnivoice_cpp_tts: voice clone load failed (%s), "
                "falling back to design/auto voice", e,
            )
            self._ref_samples = None

    def _resolve_ref_voice(self):
        """If ref_audio is set, load it as 24k mono float for cloning. Needs
        a matching ref_text (the engine has no ASR fallback)."""
        self._ref_samples = None
        if not self._ref_audio:
            return
        path = Path(str(self._ref_audio)).expanduser()
        if not path.is_file():
            logger.warning(
                "omnivoice_cpp_tts: ref_audio not found at %s, using design/auto voice",
                path,
            )
            return
        if not self._ref_text:
            logger.warning(
                "omnivoice_cpp_tts: ref_audio set but ref_text missing. the cpp "
                "engine has no ASR, so cloning needs the transcript. set "
                "plugins.omnivoice_cpp_tts.ref_text. using design/auto voice for now."
            )
            return
        try:
            import soundfile as sf
        except ImportError:
            logger.warning(
                "omnivoice_cpp_tts: soundfile not installed, cant load ref_audio "
                "for cloning. install soundfile or use instruct voice design."
            )
            return

        data, sr = sf.read(str(path), dtype="float32", always_2d=False)
        arr = np.asarray(data, dtype=np.float32)
        if arr.ndim > 1:  # downmix to mono
            arr = arr.mean(axis=1).astype(np.float32)
        arr = _resample_linear(arr, int(sr), 24000)
        # contiguous so the ctypes pointer is stable for the synth calls
        self._ref_samples = np.ascontiguousarray(arr, dtype=np.float32)
        logger.info(
            "omnivoice_cpp_tts: loaded voice clone ref %s (%.1fs @ 24k)",
            path.name, self._ref_samples.shape[0] / 24000.0,
        )

    # -- splitter thread --------------------------------------------------

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
                "omnivoice_cpp_tts: stream2sentence is required, install with "
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
                    s = _normalize_nonverbal_tags(s)
                    if s:
                        logger.info("omnivoice_cpp_tts sentence: %r", s[:80])
                        self._sentence_queue.put(s)
            except Exception as e:
                if not self._interrupted:
                    logger.error("omnivoice_cpp_tts splitter error: %s", e)

    # -- synth thread (runs the native engine serially) ------------------

    def _synth_loop(self):
        self._ready.wait()
        if self._ctx is None or self._lib is None:
            logger.error(
                "omnivoice_cpp_tts: synth loop bailing, engine not loaded (%s)",
                self._load_error or "unknown error",
            )
            return

        while self._running:
            try:
                sentence = self._sentence_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if sentence is None or self._interrupted:
                continue
            epoch = self._gen_epoch
            try:
                self._synthesize_sentence(sentence, epoch=epoch)
            except Exception as e:
                if not self._interrupted:
                    logger.warning(
                        "omnivoice_cpp_tts synth failed for %r: %s", sentence[:60], e,
                    )

    def _synthesize_sentence(self, sentence: str, epoch: int):
        lib = self._lib
        ctx = self._ctx
        if lib is None or ctx is None:
            return

        params = _ffi.ov_tts_params()
        lib.ov_tts_default_params(C.byref(params))
        params.abi_version = _ffi.OV_ABI_VERSION

        # encode strings, keep refs alive for the duration of the call
        text_b = sentence.encode("utf-8")
        lang_b = (self._language or "").encode("utf-8")
        params.text = text_b
        params.lang = lang_b

        instruct_b = None
        ref_text_b = None
        if self._ref_samples is not None and self._ref_text:
            # voice cloning path
            ref_text_b = str(self._ref_text).encode("utf-8")
            params.ref_text = ref_text_b
            params.ref_audio_24k = self._ref_samples.ctypes.data_as(
                C.POINTER(C.c_float))
            params.ref_n_samples = int(self._ref_samples.shape[0])
        elif self._instruct:
            instruct_b = str(self._instruct).encode("utf-8")
            params.instruct = instruct_b

        # generation knobs
        params.mg_num_step = self._num_step
        params.mg_guidance_scale = self._guidance_scale
        params.mg_t_shift = self._t_shift
        params.mg_layer_penalty_factor = self._layer_penalty_factor
        params.mg_position_temperature = self._position_temperature
        params.mg_class_temperature = self._class_temperature
        params.mg_seed = C.c_uint64(self._seed & 0xFFFFFFFFFFFFFFFF).value
        params.denoise = self._denoise
        params.preprocess_prompt = self._preprocess_prompt
        # we feed one sentence per call (stream2sentence already split the
        # stream), so the native long-form chunker never needs to fire. we
        # leave ov_tts_default_params' 15/30s defaults in place as a cheap
        # safety net for a pathological run-on sentence and dont expose them.

        # streaming callback state, captured per sentence
        state = {"first": True, "carry": np.empty(0, dtype=np.float32)}

        def _on_chunk(samples_ptr, n_samples, _ud) -> bool:
            # abort the moment interrupt fires or a newer turn started
            if (self._interrupted or not self._running
                    or epoch != self._gen_epoch):
                return False
            if not samples_ptr or n_samples <= 0:
                return True
            buf = np.ctypeslib.as_array(samples_ptr, shape=(int(n_samples),))
            arr = np.array(buf, dtype=np.float32, copy=True)
            if state["first"] and self._first_chunk_min_samples > 0:
                state["carry"] = np.concatenate([state["carry"], arr])
                if state["carry"].shape[0] < self._first_chunk_min_samples:
                    return True
                arr = state["carry"]
                state["carry"] = np.empty(0, dtype=np.float32)
                state["first"] = False
            self._push_pcm(arr, epoch=epoch)
            return True

        cb = _ffi.OV_AUDIO_CHUNK_CB(_on_chunk)
        params.on_chunk = cb

        # cancel callback, same abort condition, polled between chunks
        def _cancel(_ud) -> bool:
            return bool(self._interrupted or not self._running
                        or epoch != self._gen_epoch)

        cancel_cb = _ffi.OV_CANCEL_CB(_cancel)
        params.cancel = cancel_cb

        out = _ffi.ov_audio()
        rc = lib.ov_synthesize(ctx, C.byref(params), C.byref(out))
        # streaming mode keeps out empty; free is safe either way
        try:
            lib.ov_audio_free(C.byref(out))
        except Exception:
            pass

        if rc != _ffi.OV_STATUS_OK and rc != _ffi.OV_STATUS_CANCELLED:
            if not self._interrupted:
                err = lib.ov_last_error()
                err = err.decode("utf-8", "replace") if err else ""
                logger.warning(
                    "omnivoice_cpp_tts: ov_synthesize -> %s %s",
                    _ffi.status_name(rc), err,
                )
            return

        # flush the first-chunk carry if we never crossed the min threshold
        carry = state["carry"]
        if (carry.shape[0] > 0 and not self._interrupted and self._running
                and epoch == self._gen_epoch):
            self._push_pcm(carry, epoch=epoch)

    def _push_pcm(self, arr: np.ndarray, epoch: int):
        if arr.size == 0 or epoch != self._gen_epoch:
            return
        if self._target_sr != self._sample_rate:
            arr = _resample_linear(arr, self._sample_rate, self._target_sr)
        arr = np.clip(arr, -1.0, 1.0)
        pcm = (arr * 32767.0).astype(np.int16, copy=False).tobytes()
        if not self._loop or not self._loop.is_running():
            return
        self._loop.call_soon_threadsafe(self._audio_queue.put_nowait, pcm)

    # ── warmup ───────────────────────────────────────────────────────────

    @classmethod
    def warmup(cls, *, lib_dir, model_repo, model_variant, base_model,
               codec_model, use_fa, clamp_fp16, data_dir,
               lib_url=None, auto_download_lib=True):
        """Load the native engine into the process-wide warm cache on a
        background thread so the first session starts hot. Voice is resolved
        per session, so only the engine (model) is warmed here."""
        stub = cls.__new__(cls)
        stub.config = None
        stub._lib_dir = lib_dir or os.environ.get("OMNIVOICE_CPP_DIR") or None
        stub._lib_url = lib_url or _DEFAULT_LIB_URL
        stub._auto_download_lib = auto_download_lib
        stub._model_repo = model_repo
        stub._variant = model_variant
        stub._base_model = base_model
        stub._codec_model = codec_model
        stub._use_fa = use_fa
        stub._clamp_fp16 = clamp_fp16
        stub._ref_audio = None
        stub._ref_text = None
        stub._instruct = None
        stub._ref_samples = None
        stub._sample_rate = 24000
        stub._data_dir = Path(data_dir)
        stub._models_dir = Path(data_dir) / "models"
        stub._lib = None
        stub._ctx = None
        stub._load_error = None
        stub._ready = threading.Event()
        try:
            stub._load_engine_and_voice()
            if stub._ctx is None:
                logger.warning(
                    "omnivoice_cpp_tts: warmup did not finish cleanly: %s",
                    stub._load_error or "unknown error",
                )
            else:
                logger.info("omnivoice_cpp_tts: warmup done, first session will be hot")
        except Exception as e:
            logger.warning("omnivoice_cpp_tts: warmup thread crashed: %s", e)
