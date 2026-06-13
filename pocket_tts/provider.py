"""Pocket TTS streaming provider.

Mirrors the public surface of `src.tts.QwenTTSProvider` so the Gabriel
sessions can use it as a drop in:

    p = PocketTTSProvider(config)
    p.start()
    p.feed_text("hello ")
    p.feed_text("world.")
    p.turn_complete()
    pcm = await p.get_audio()      # 16-bit PCM mono 24kHz
    p.interrupt()
    p.stop()

Architecture (single in-process model, no server):
  feed_text()  -> _text_queue (thread-safe)
    -> _splitter_thread sentence-splits via stream2sentence
    -> _sentence_queue
      -> _synth_thread serially runs pocket_tts.TTSModel.generate_audio_stream
         for each sentence and converts each chunk float32 -> int16 PCM
        -> _audio_queue (asyncio.Queue, fed via call_soon_threadsafe)
          -> get_audio() called by the host TTS loop
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


# rough heuristic for "this looks like a built in voice name", anything
# without a path separator, scheme, or file extension. lets the user
# write voice: "alba" instead of voice: "alba" with extra ceremony.
_BUILTIN_VOICE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _looks_like_url(s: str) -> bool:
    return s.startswith("hf://") or s.startswith("http://") or s.startswith("https://")


class PocketTTSProvider:
    # Process-wide warm cache. Lets a background warmup thread populate
    # the model + voice state before the first session, and lets a
    # later session (reconnect, etc) reuse the same model instead of
    # paying the load cost again.
    _warm_cache: dict = {}
    _warm_lock = threading.Lock()
    # Per-key singleflight lock so the background warmup thread and the
    # host's session-start loader cant both miss the cache and reload
    # the same model in parallel.
    _load_locks: dict = {}

    def __init__(self, config, local_overrides: dict | None = None, data_dir: Path | None = None):
        self.config = config
        overrides = local_overrides or {}

        def cfg(key, default=None):
            if key in overrides and overrides[key] not in (None, ""):
                return overrides[key]
            return config.get("plugins", "pocket_tts", key, default=default)

        self._language = cfg("language", "english") or "english"
        self._voice = cfg("voice", "alba") or "alba"
        self._quantize = bool(cfg("quantize", False))
        self._temp = float(cfg("temperature", 0.7))
        self._lsd_decode_steps = int(cfg("lsd_decode_steps", 1))
        eos = cfg("eos_threshold", -4.0)
        self._eos_threshold = float(eos) if eos not in (None, "") else -4.0
        nc = cfg("noise_clamp", None)
        self._noise_clamp = float(nc) if nc not in (None, "", "null") else None
        fae = cfg("frames_after_eos", None)
        self._frames_after_eos = int(fae) if fae not in (None, "", "null") else None
        self._cache_voice = bool(cfg("cache_voice", True))
        self._truncate_clone = bool(cfg("truncate_clone", False))
        self._first_chunk_min_samples = int(cfg("first_chunk_min_samples", 1920))

        # Where to stash extracted voice .safetensors files between runs.
        if data_dir is None:
            data_dir = Path(".") / "data" / "plugins" / "pocket_tts"
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
        self._voice_state = None
        self._ready = threading.Event()
        self._load_error: str | None = None
        # bumped on every interrupt(). synth thread snapshots this at
        # the top of each sentence and drops pcm whose epoch is stale,
        # so a sentence mid stream when the user barges in cant leak
        # late chunks into the next turn.
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
            "pocket_tts started, language=%s voice=%s quantize=%s",
            self._language, self._voice, self._quantize,
        )

    def stop(self):
        self._running = False
        self._interrupted = True
        # unblock any thread waiting on a queue
        self._text_queue.put(None)
        self._sentence_queue.put(None)
        for thread in (self._splitter_thread, self._synth_thread, self._loader_thread):
            if thread is not None:
                thread.join(timeout=3)
        self._splitter_thread = None
        self._synth_thread = None
        self._loader_thread = None
        # let pytorch release the model when no other refs are held
        self._model = None
        self._voice_state = None
        self._ready.clear()

    def feed_text(self, text: str):
        if not text:
            return
        self._interrupted = False
        logger.debug("pocket_tts feed_text: %r", text)
        self._text_queue.put(text)

    def turn_complete(self):
        # sentinel flushes the splitter so any half buffered text gets
        # forced into a sentence.
        self._text_queue.put(None)

    def interrupt(self):
        self._interrupted = True
        # bump epoch BEFORE draining queues so any push from an in flight
        # sentence stream sees the new epoch and drops silently.
        self._gen_epoch += 1
        drained = 0
        for q in (self._text_queue, self._sentence_queue):
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except queue.Empty:
                    break
        # nudge the splitter so it doesn't sit on an empty generator
        self._text_queue.put(None)
        # drain the asyncio output queue from the loop thread
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._drain_audio_queue)
        logger.debug(
            "pocket_tts interrupted (drained %d queued items, epoch=%d)",
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

    # -- model + voice state loader (runs once on start) ------------------

    @staticmethod
    def _cache_key(language, voice, quantize, temperature,
                   lsd_decode_steps, eos_threshold, noise_clamp,
                   truncate_clone) -> tuple:
        return (
            str(language or "english"),
            str(voice or "alba"),
            bool(quantize),
            float(temperature),
            int(lsd_decode_steps),
            float(eos_threshold),
            None if noise_clamp is None else float(noise_clamp),
            bool(truncate_clone),
        )

    def _my_cache_key(self) -> tuple:
        return self._cache_key(
            self._language, self._voice, self._quantize, self._temp,
            self._lsd_decode_steps, self._eos_threshold,
            self._noise_clamp, self._truncate_clone,
        )

    @classmethod
    def warmup(cls, *, language, voice, quantize, temperature,
               lsd_decode_steps, eos_threshold, noise_clamp,
               truncate_clone, cache_voice, voice_cache_dir):
        """Load the model + voice state into the process-wide warm cache
        on a background thread so the first session that picks pocket_tts
        starts instantly. Idempotent, so it's safe to call again if config
        changes (a different key just adds another entry)."""
        key = cls._cache_key(
            language, voice, quantize, temperature, lsd_decode_steps,
            eos_threshold, noise_clamp, truncate_clone,
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
            stub._language = language
            stub._voice = voice
            stub._quantize = quantize
            stub._temp = temperature
            stub._lsd_decode_steps = lsd_decode_steps
            stub._eos_threshold = eos_threshold
            stub._noise_clamp = noise_clamp
            stub._truncate_clone = truncate_clone
            stub._cache_voice = cache_voice
            stub._voice_cache_dir = Path(voice_cache_dir)
            stub._voice_cache_dir.mkdir(parents=True, exist_ok=True)
            stub._model = None
            stub._voice_state = None
            stub._load_error = None
            stub._ready = threading.Event()
            logger.info(
                "pocket_tts: warming up model (lang=%s voice=%s quantize=%s) in background ...",
                language, voice, quantize,
            )
            stub._load_model_and_voice()
            if stub._model is None or stub._voice_state is None:
                logger.warning(
                    "pocket_tts: warmup did not finish cleanly: %s",
                    stub._load_error or "unknown error",
                )
            else:
                logger.info(
                    "pocket_tts: warmup done, first session will be hot"
                )
        except Exception as e:
            logger.warning("pocket_tts: warmup thread crashed: %s", e)

    def _load_model_and_voice(self):
        # hot path: reuse a previously warmed model + voice state
        key = self._my_cache_key()
        with self._warm_lock:
            cached = self._warm_cache.get(key)
        if cached is not None:
            self._model, self._voice_state = cached
            logger.info("pocket_tts: reusing pre-warmed model + voice (hot start)")
            self._ready.set()
            return

        # serialize concurrent loads of the same key so warmup + session
        # start dont both pay the full model load cost in parallel.
        with self._warm_lock:
            load_lock = self._load_locks.get(key)
            if load_lock is None:
                load_lock = threading.Lock()
                self._load_locks[key] = load_lock

        with load_lock:
            # re-check under the per-key lock
            with self._warm_lock:
                cached = self._warm_cache.get(key)
            if cached is not None:
                self._model, self._voice_state = cached
                logger.info("pocket_tts: reusing pre-warmed model + voice (hot start)")
                self._ready.set()
                return

            try:
                from pocket_tts import TTSModel
                t0 = time.monotonic()
                logger.info(
                    "pocket_tts: loading model (language=%s, quantize=%s) ...",
                    self._language, self._quantize,
                )
                self._model = TTSModel.load_model(
                    language=self._language,
                    temp=self._temp,
                    lsd_decode_steps=self._lsd_decode_steps,
                    noise_clamp=self._noise_clamp,
                    eos_threshold=self._eos_threshold,
                    quantize=self._quantize,
                )
                logger.info(
                    "pocket_tts: model loaded in %.1fs (sr=%d)",
                    time.monotonic() - t0, self._model.sample_rate,
                )
                self._voice_state = self._resolve_voice_state(self._voice)
                # populate the warm cache so a session restart skips this work
                with self._warm_lock:
                    self._warm_cache[key] = (self._model, self._voice_state)
                self._ready.set()
            except Exception as e:
                self._load_error = str(e)
                logger.exception("pocket_tts: failed to load model / voice: %s", e)
                # leave _ready unset, synth_loop will see the error and bail
                self._ready.set()

    def _resolve_voice_state(self, voice: str):
        """Turn the configured `voice` into a model_state dict.

        Accepts:
          - built-in voice name (e.g. "alba")
          - hf://, http(s):// URL
          - local audio file (wav/mp3/flac/ogg)
          - local .safetensors file (fast)

        Local audio files get cached as .safetensors after the first
        extraction so subsequent restarts skip the slow encode.
        """
        from pocket_tts import TTSModel, export_model_state  # noqa: F401

        if not voice:
            voice = "alba"

        v = voice.strip()

        # Built-in voice name (just letters/digits/underscore, no dots, no slashes)
        if _BUILTIN_VOICE_RE.match(v) and not _looks_like_url(v):
            logger.info("pocket_tts: using built in voice %r", v)
            return self._model.get_state_for_audio_prompt(v)

        # URL form, let pocket-tts download it
        if _looks_like_url(v):
            logger.info("pocket_tts: using remote voice %s", v)
            return self._model.get_state_for_audio_prompt(v)

        # Local file form
        path = Path(v).expanduser()
        if not path.is_file():
            raise FileNotFoundError(
                f"voice path not found: {path} (also not a known built in name)"
            )

        # If it's already a safetensors file, just load it.
        if path.suffix.lower() == ".safetensors":
            logger.info("pocket_tts: loading cached voice state %s", path)
            return self._model.get_state_for_audio_prompt(path)

        # Audio file. Check for an on-disk cache first.
        if self._cache_voice:
            cache_path = self._voice_cache_path(path)
            if cache_path is not None and cache_path.is_file():
                logger.info(
                    "pocket_tts: loading cached voice state for %s -> %s",
                    path.name, cache_path.name,
                )
                try:
                    return self._model.get_state_for_audio_prompt(cache_path)
                except Exception as e:
                    logger.warning(
                        "pocket_tts: cached voice state failed to load (%s), re extracting",
                        e,
                    )

        logger.info(
            "pocket_tts: extracting voice state from %s (this can take a few seconds)",
            path,
        )
        state = self._model.get_state_for_audio_prompt(path, truncate=self._truncate_clone)

        if self._cache_voice:
            cache_path = self._voice_cache_path(path)
            if cache_path is not None:
                try:
                    export_model_state(state, str(cache_path))
                    logger.info(
                        "pocket_tts: cached voice state to %s", cache_path,
                    )
                except Exception as e:
                    logger.warning("pocket_tts: failed to write voice cache: %s", e)

        return state

    def _voice_cache_path(self, audio_path: Path) -> Path | None:
        try:
            mtime = int(os.path.getmtime(audio_path))
        except OSError:
            return None
        key_src = (
            f"{os.path.abspath(audio_path)}|{mtime}|{self._language}|{int(self._quantize)}"
        )
        digest = hashlib.sha1(key_src.encode("utf-8")).hexdigest()[:16]
        stem = re.sub(r"[^A-Za-z0-9_\-]", "_", audio_path.stem)[:32] or "voice"
        return self._voice_cache_dir / f"{stem}_{digest}.safetensors"

    # -- splitter thread (sync, stream2sentence is blocking) --------------

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
                "pocket_tts: stream2sentence is required, install with "
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
                    if s:
                        logger.info("pocket_tts sentence: %r", s[:80])
                        self._sentence_queue.put(s)
            except Exception as e:
                if not self._interrupted:
                    logger.error("pocket_tts splitter error: %s", e)

    # -- synth thread (runs the model serially) ---------------------------

    def _synth_loop(self):
        # block until the model + voice state are ready (or load_error set)
        self._ready.wait()
        if self._model is None or self._voice_state is None:
            logger.error(
                "pocket_tts: synth loop bailing, model not loaded (%s)",
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
            epoch = self._gen_epoch
            try:
                self._synthesize_sentence(sentence, epoch=epoch)
            except Exception as e:
                if not self._interrupted:
                    logger.warning(
                        "pocket_tts synth failed for %r: %s", sentence[:60], e,
                    )

    def _synthesize_sentence(self, sentence: str, epoch: int | None = None):
        if self._model is None or self._voice_state is None:
            return

        kwargs = {"copy_state": True}
        if self._frames_after_eos is not None:
            kwargs["frames_after_eos"] = self._frames_after_eos

        # generate_audio_stream yields torch.Tensor [samples] float in ~[-1, 1]
        # at self._model.sample_rate (24kHz on the published configs).
        first = True
        carry = np.empty(0, dtype=np.float32)
        for chunk in self._model.generate_audio_stream(
            self._voice_state, sentence, **kwargs,
        ):
            # bail mid sentence the moment interrupt() fires. saves the
            # remaining diffusion work for this sentence + everything
            # that was queued behind it.
            if (self._interrupted or not self._running
                    or (epoch is not None and epoch != self._gen_epoch)):
                return
            arr = chunk.detach().cpu().numpy().astype(np.float32, copy=False)
            if arr.ndim > 1:
                arr = arr.reshape(-1)
            if first and self._first_chunk_min_samples > 0:
                # the very first chunk from pocket-tts can be very small
                # (a few ms) which makes some downstream playback paths
                # stutter. accumulate a tiny lookahead before we send
                # the first frame.
                carry = np.concatenate([carry, arr])
                if carry.shape[0] < self._first_chunk_min_samples:
                    continue
                arr = carry
                carry = np.empty(0, dtype=np.float32)
                first = False
            self._push_pcm(arr, epoch=epoch)

        # final tail flush, but skip if we were cut off mid sentence,
        # the host would just throw it away anyway and it leaks ~80ms
        # of stale audio onto the next turn.
        if (carry.shape[0] > 0 and not self._interrupted and self._running
                and (epoch is None or epoch == self._gen_epoch)):
            self._push_pcm(carry, epoch=epoch)

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
