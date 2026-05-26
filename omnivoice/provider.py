"""OmniVoice TTS streaming provider.

Mirrors the public surface of `src.tts.QwenTTSProvider` so the Gabriel
sessions can use it as a drop-in:

    p = OmniVoiceTTSProvider(config)
    p.start()
    p.feed_text("hello ")
    p.feed_text("world.")
    p.turn_complete()
    pcm = await p.get_audio()      # 16-bit PCM mono 24kHz
    p.interrupt()
    p.stop()

Architecture:
  feed_text() -> _text_queue (thread)
    -> _splitter_loop sentence-splits via stream2sentence
    -> _sentence_queue
      -> _dispatch_task launches concurrent _synthesize_async per sentence,
         each writing PCM into an ordered sub-queue
        -> _feeder_task drains sub-queues in order into _audio_queue
          -> get_audio() consumed by the session
"""
from __future__ import annotations

import asyncio
import logging
import queue
import re
import threading
import time
from typing import Any

import httpx
from stream2sentence import generate_sentences

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


def _strip_wav_header(buf: bytes) -> tuple[bytes, bytes]:
    """Find the start of the WAV `data` chunk and return (header, pcm_tail).

    omnivoice-serve sends a streaming WAV with a placeholder data-size
    of 0xFFFFFFFF. We don't care about the header at all, we just need
    to forward the PCM frames that come after it. Returns the bytes
    consumed by the header (or '' if not found yet) and the remaining
    PCM frames.
    """
    idx = buf.find(b"data")
    if idx == -1:
        return b"", buf  # header not complete yet
    # 'data' is followed by 4 bytes of size (little endian uint32)
    pcm_start = idx + 4 + 4
    if len(buf) < pcm_start:
        return b"", buf  # need more bytes for the size field
    return buf[:pcm_start], buf[pcm_start:]


class OmniVoiceTTSProvider:
    def __init__(self, config, local_overrides: dict | None = None):
        self.config = config
        overrides = local_overrides or {}

        def cfg(key, default=None):
            if key in overrides and overrides[key] not in (None, ""):
                return overrides[key]
            return config.get("plugins", "omnivoice", key, default=default)

        self._base_url = str(cfg("base_url", "http://127.0.0.1:8000")).rstrip("/")
        self._voice_id = cfg("voice_id", "") or None
        self._instruct = cfg("instruct", "") or None
        self._language = cfg("language", "") or None
        self._num_steps = int(cfg("num_steps", 24))
        self._guidance_scale = float(cfg("guidance_scale", 2.0))
        speed = cfg("speed", None)
        self._speed = float(speed) if speed not in (None, "", "null") else None
        self._denoise = bool(cfg("denoise", True))
        self._max_concurrent = int(cfg("max_concurrent", 2))
        self._request_timeout = float(cfg("request_timeout", 120))

        # sample rate the omnivoice server emits natively. matches the
        # default audio.receive_sample_rate of the host.
        self._target_sr = config.get("audio", "receive_sample_rate", default=24000)
        if self._target_sr != 24000:
            logger.warning(
                "omnivoice emits PCM at 24000 Hz but audio.receive_sample_rate is %d. "
                "Audio may sound pitch-shifted. Resampling not implemented here.",
                self._target_sr,
            )

        # queues + lifecycle
        self._text_queue: "queue.Queue[str | None]" = queue.Queue()
        self._sentence_queue: "queue.Queue[str]" = queue.Queue()
        self._ready_queue: "asyncio.Queue[asyncio.Queue[bytes | None]]" = asyncio.Queue()
        self._audio_queue: "asyncio.Queue[bytes]" = asyncio.Queue()

        self._running = False
        self._interrupted = False
        self._splitter_thread: threading.Thread | None = None
        self._client: httpx.AsyncClient | None = None
        self._async_tasks: list[asyncio.Task] = []
        self._synth_tasks: set[asyncio.Task] = set()
        self._synth_semaphore: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

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
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=self._request_timeout,
                write=30.0,
                pool=10.0,
            ),
            follow_redirects=True,
        )
        self._splitter_thread = threading.Thread(
            target=self._splitter_loop, daemon=True,
        )
        self._splitter_thread.start()
        mode = (
            "voice_clone" if self._voice_id
            else "voice_design" if self._instruct
            else "auto"
        )
        logger.info(
            "omnivoice TTS started (mode=%s, voice_id=%s, url=%s)",
            mode, self._voice_id or "-", self._base_url,
        )

    def stop(self):
        self._running = False
        self._interrupted = True
        self._text_queue.put(None)
        if self._splitter_thread:
            self._splitter_thread.join(timeout=3)
            self._splitter_thread = None
        for task in list(self._async_tasks):
            task.cancel()
        self._async_tasks.clear()
        for task in list(self._synth_tasks):
            task.cancel()
        self._synth_tasks.clear()
        if self._client:
            client = self._client
            self._client = None
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(
                    lambda c=client: asyncio.ensure_future(c.aclose())
                )

    def feed_text(self, text: str):
        if not text:
            return
        self._interrupted = False
        logger.debug("omnivoice feed_text: %r", text)
        self._text_queue.put(text)

    def turn_complete(self):
        # sentinel flushes the splitter so any half buffered text gets
        # forced into a sentence.
        self._text_queue.put(None)

    def interrupt(self):
        self._interrupted = True
        for q in (self._text_queue, self._sentence_queue):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        self._text_queue.put(None)
        for task in list(self._synth_tasks):
            task.cancel()
        self._synth_tasks.clear()
        for aq in (self._ready_queue, self._audio_queue):
            while True:
                try:
                    aq.get_nowait()
                except asyncio.QueueEmpty:
                    break

    async def get_audio(self) -> bytes | None:
        self._ensure_async_tasks()
        try:
            return await asyncio.wait_for(self._audio_queue.get(), timeout=0.1)
        except (asyncio.TimeoutError, asyncio.QueueEmpty):
            return None

    # ── internals ────────────────────────────────────────────────────────

    def _ensure_async_tasks(self):
        if self._async_tasks:
            return
        self._loop = asyncio.get_running_loop()
        self._synth_semaphore = asyncio.Semaphore(self._max_concurrent)
        self._async_tasks = [
            asyncio.create_task(self._dispatch_task()),
            asyncio.create_task(self._feeder_task()),
        ]

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
                        logger.info("omnivoice sentence: %r", s[:80])
                        self._sentence_queue.put(s)
            except Exception as e:
                if not self._interrupted:
                    logger.error("omnivoice splitter error: %s", e)

    async def _dispatch_task(self):
        while self._running:
            try:
                sentence = await asyncio.to_thread(
                    self._sentence_queue.get, True, 0.1,
                )
            except queue.Empty:
                continue
            except Exception:
                if not self._running:
                    return
                continue
            if self._interrupted:
                continue
            sub_q: "asyncio.Queue[bytes | None]" = asyncio.Queue()
            await self._ready_queue.put(sub_q)
            task = asyncio.create_task(self._synthesize_async(sentence, sub_q))
            self._synth_tasks.add(task)
            task.add_done_callback(self._synth_tasks.discard)

    async def _feeder_task(self):
        while self._running:
            try:
                sub_q = await asyncio.wait_for(self._ready_queue.get(), timeout=0.1)
            except (asyncio.TimeoutError, asyncio.QueueEmpty):
                continue
            while True:
                try:
                    pcm = await asyncio.wait_for(sub_q.get(), timeout=0.5)
                except (asyncio.TimeoutError, asyncio.QueueEmpty):
                    if self._interrupted or not self._running:
                        break
                    continue
                if pcm is None:
                    break
                if not self._interrupted:
                    await self._audio_queue.put(pcm)

    def _build_payload(self, text: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "text": text,
            "num_steps": self._num_steps,
            "guidance_scale": self._guidance_scale,
            "denoise": self._denoise,
        }
        if self._voice_id:
            body["voice_id"] = self._voice_id
        elif self._instruct:
            body["instruct"] = self._instruct
        if self._language:
            body["language"] = self._language
        if self._speed is not None:
            body["speed"] = self._speed
        return body

    async def _synthesize_async(self, text: str, sub_q: "asyncio.Queue[bytes | None]"):
        if not self._client:
            sub_q.put_nowait(None)
            return

        async with self._synth_semaphore:
            if self._interrupted:
                sub_q.put_nowait(None)
                return

            url = f"{self._base_url}/v1/tts/stream"
            payload = self._build_payload(text)
            header_buf = b""
            header_done = False
            try:
                async with self._client.stream("POST", url, json=payload) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread())[:300].decode("utf-8", "ignore")
                        logger.error(
                            "omnivoice /v1/tts/stream %d: %s", resp.status_code, body,
                        )
                        sub_q.put_nowait(None)
                        return
                    async for chunk in resp.aiter_bytes(chunk_size=4096):
                        if self._interrupted or not self._running:
                            return
                        if not chunk:
                            continue
                        if not header_done:
                            header_buf += chunk
                            _hdr, tail = _strip_wav_header(header_buf)
                            if not _hdr:
                                # still inside the header bytes
                                continue
                            header_done = True
                            header_buf = b""
                            if tail:
                                sub_q.put_nowait(tail)
                        else:
                            sub_q.put_nowait(chunk)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._interrupted:
                    logger.warning("omnivoice synth failed for %r: %s", text[:60], e)
            finally:
                sub_q.put_nowait(None)
