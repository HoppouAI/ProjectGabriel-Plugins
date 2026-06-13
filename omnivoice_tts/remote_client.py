"""Remote TTS provider that talks to a standalone omnivoice_tts server.

Mirrors the public surface of `OmniVoiceProvider` from .provider so the
plugin's __init__.py can swap in either one based on config. All the
heavy lifting (model load, gpu work, sentence splitting) happens on the
server. We just stream text up and audio frames back down.

Use this by setting `plugins.omnivoice_tts.remote.url` in the host
config to the server's ws URL, eg ws://192.168.1.10:8788/tts. The
plugin's __init__ takes care of picking remote vs local.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

from .standalone import protocol as P


logger = logging.getLogger("plugin.omnivoice_tts.remote")


class OmniVoiceRemoteProvider:
    """Drop-in replacement for `OmniVoiceProvider` that proxies over WS.

    Constructor takes the host config like the local one does, so the
    plugin can pick whichever and the rest of the host doesn't care.
    """

    def __init__(self, config, local_overrides: dict | None = None, data_dir=None):
        self.config = config
        overrides = local_overrides or {}

        def cfg(*keys, default=None):
            cur = overrides
            for k in keys:
                if not isinstance(cur, dict) or k not in cur:
                    cur = None
                    break
                cur = cur[k]
            if cur not in (None, ""):
                return cur
            return config.get("plugins", "omnivoice_tts", *keys, default=default)

        self._url = str(cfg("remote", "url", default="") or "")
        self._reconnect = bool(cfg("remote", "reconnect", default=True))
        self._timeout = float(cfg("remote", "timeout_seconds", default=30.0))
        self._send_overrides = cfg("remote", "send_voice_override", default=None)
        # ping/pong heartbeat so the connection doesnt go silent and get
        # torn down by routers / load balancers
        self._heartbeat = float(cfg("remote", "heartbeat_seconds", default=20.0))

        # Where to keep voice cache locally? remote mode skips local
        # voice cloning so we don't need it. ignored for now.
        self._data_dir = data_dir

        # public, mirrors local provider field name so host code that
        # peeks at this for logging doesn't break
        self._model_path = f"remote:{self._url}"
        self._device = "remote"
        self._target_sr = int(config.get("audio", "receive_sample_rate", default=24000))

        # state ----------------------------------------------------------
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_task: asyncio.Task | None = None
        self._audio_queue: "asyncio.Queue[bytes]" = asyncio.Queue()
        self._connected = asyncio.Event()
        self._ready = threading.Event()  # matches local provider api
        self._load_error: str | None = None
        # buffered control msgs so feed_text() called before connect
        # doesnt drop on the floor
        self._pending: list[dict] = []
        self._pending_lock = threading.Lock()

    # ── public api ───────────────────────────────────────────────────────

    def start(self):
        if not self._url:
            self._load_error = "plugins.omnivoice_tts.remote.url not set"
            self._ready.set()
            logger.error("%s", self._load_error)
            return
        if self._running:
            return
        self._running = True
        self._load_error = None
        self._ready.clear()
        # ws task is started lazily from get_audio() once we have a loop,
        # same trick the local provider uses for its synth thread.

    def stop(self):
        self._running = False
        if self._loop is not None and self._ws_task is not None:
            try:
                self._loop.call_soon_threadsafe(self._ws_task.cancel)
            except Exception:
                pass
        self._ws_task = None
        self._ready.clear()
        self._connected.clear()
        with self._pending_lock:
            self._pending.clear()

    def feed_text(self, text: str):
        if not text or not self._running:
            return
        self._send_or_queue({"type": P.TYPE_FEED_TEXT, "text": text})

    def turn_complete(self):
        if not self._running:
            return
        self._send_or_queue({"type": P.TYPE_TURN_COMPLETE})

    def interrupt(self):
        if not self._running:
            return
        # drop anything we've buffered locally that hasn't gone out yet
        with self._pending_lock:
            self._pending = [m for m in self._pending if m.get("type") == P.TYPE_INTERRUPT]
        # drain any audio frames already received but not consumed
        try:
            while True:
                self._audio_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        self._send_or_queue({"type": P.TYPE_INTERRUPT})

    async def get_audio(self) -> bytes | None:
        self._ensure_ws_task()
        try:
            return await asyncio.wait_for(self._audio_queue.get(), timeout=0.1)
        except (asyncio.TimeoutError, asyncio.QueueEmpty):
            return None

    # ── internals ────────────────────────────────────────────────────────

    def _ensure_ws_task(self):
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        if self._ws_task is None and self._running:
            self._ws_task = self._loop.create_task(self._ws_main())

    def _send_or_queue(self, msg: dict):
        if self._loop is None or not self._connected.is_set():
            with self._pending_lock:
                self._pending.append(msg)
            return
        try:
            asyncio.run_coroutine_threadsafe(self._send_json(msg), self._loop)
        except Exception as e:
            logger.warning("remote send failed, requeueing: %s", e)
            with self._pending_lock:
                self._pending.append(msg)

    async def _send_json(self, msg: dict):
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps(msg))
        except Exception as e:
            logger.warning("ws send failed: %s", e)

    async def _ws_main(self):
        """Connect, talk, reconnect on drop. Lives for the lifetime of
        the provider."""
        try:
            import websockets
        except ImportError:
            self._load_error = "the 'websockets' package isn't installed. add it to requirements."
            self._ready.set()
            logger.error("%s", self._load_error)
            return

        backoff = 1.0
        while self._running:
            try:
                logger.info("connecting to %s ...", self._url)
                async with websockets.connect(
                    self._url,
                    open_timeout=self._timeout,
                    ping_interval=self._heartbeat,
                    ping_timeout=self._heartbeat * 1.5,
                    max_size=16 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    backoff = 1.0
                    if self._send_overrides:
                        await ws.send(json.dumps({
                            "type": P.TYPE_CONFIG,
                            "overrides": self._send_overrides,
                        }))
                    await self._handle_connection(ws)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected.clear()
                self._ready.clear()
                logger.warning("ws connection failed: %s", e)
                if not self._reconnect:
                    self._load_error = str(e)
                    self._ready.set()
                    return
            self._ws = None
            if not self._running:
                break
            await asyncio.sleep(min(backoff, 30.0))
            backoff = min(backoff * 2, 30.0)

    async def _handle_connection(self, ws):
        # flush any control messages buffered while we were offline
        with self._pending_lock:
            pending = list(self._pending)
            self._pending.clear()
        for m in pending:
            try:
                await ws.send(json.dumps(m))
            except Exception:
                break

        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                # audio frame
                await self._audio_queue.put(bytes(raw))
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            t = msg.get("type")
            if t == P.TYPE_HELLO:
                # server reports the real sample rate after warmup
                sr = msg.get("sample_rate")
                if sr:
                    self._target_sr = int(sr)
                m = msg.get("model")
                if m:
                    self._model_path = f"remote:{m}@{self._url}"
                self._connected.set()
            elif t == P.TYPE_READY:
                self._ready.set()
                logger.info("remote omnivoice ready (sr=%s model=%s)", self._target_sr, self._model_path)
            elif t == P.TYPE_AUDIO_START:
                # don't really need to do anything, just a marker
                pass
            elif t == P.TYPE_AUDIO_END:
                pass
            elif t == P.TYPE_INTERRUPTED:
                # server confirmed the interrupt, drop our local buffer
                try:
                    while True:
                        self._audio_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            elif t == P.TYPE_ERROR:
                logger.warning("remote tts error: %s", msg.get("message"))
                self._load_error = str(msg.get("message") or "")
                self._ready.set()
            elif t == P.TYPE_LOG:
                lvl = (msg.get("level") or "info").lower()
                txt = msg.get("message") or ""
                getattr(logger, lvl, logger.info)("server: %s", txt)
            elif t == P.TYPE_PONG:
                pass

    # ── compat shims ─────────────────────────────────────────────────────
    # the local provider exposes a few things the host queries for logs /
    # introspection. mirror what's safe to mirror.

    def _describe_voice(self) -> str:
        return "remote"
