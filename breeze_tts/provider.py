"""Breeze-TTS-2.cpp streaming TTS provider.

Mirrors the public surface of the host `HoppouTTSProvider` (and the sibling
pocket_tts / omnivoice_cpp_tts providers) so a Gabriel session can use it as
a drop in:

    p = BreezeTTSProvider(config)
    p.start()
    p.feed_text("hello ")
    p.feed_text("world.")
    p.turn_complete()
    pcm = await p.get_audio()      # 16-bit PCM mono at audio.receive_sample_rate
    p.interrupt()
    p.stop()

Unlike the other providers this one does NOT sentence split locally. The
breeze-server websocket buffers text and drains it on sentence boundaries
itself, so we just forward the model's deltas verbatim and pull the audio
frames back. That also gets us real barge in (`cancel` stops mid sentence)
and mid turn delivery changes for free.

    feed_text() -> _out queue -> _sender task -> ws {"type":"text"}
                                                    |
    get_audio() <- _audio_queue <- _receiver task <- binary frames
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

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

# the model writes stage directions as [laughs] or *sighs*, both of which get
# read out letter by letter if they reach the tokenizer
_TAG_RE = re.compile(r"\[([^\[\]]{1,40})\]|\*([^*\n]{1,40})\*")

# breeze takes free form events, but the base form fires far more reliably than
# the inflected one. measured: (laugh) lands at cfg 2.5, (laughs) does nothing.
_TAG_ALIASES = {
    "laughs": "laugh", "laughing": "laugh", "laughter": "laugh",
    "chuckles": "chuckle", "chuckling": "chuckle",
    "giggles": "giggle", "giggling": "giggle",
    "sighs": "sigh", "sighing": "sigh", "exhales": "sigh",
    "gasps": "gasp", "gasping": "gasp",
    "coughs": "cough", "coughing": "cough",
    "sniffs": "sniff", "sniffles": "sniff",
    "whispers": "whisper", "whispering": "whisper",
    "yawns": "yawn", "yawning": "yawn",
}


def _strip_emojis(text: str) -> str:
    return re.sub(r"  +", " ", _EMOJI_RE.sub(" ", text))


def _clean(text: str, vocal_events: bool) -> str:
    def repl(m):
        inner = (m.group(1) or m.group(2)).strip().lower()
        if not vocal_events:
            return " "
        return f"({_TAG_ALIASES.get(inner, inner)}) "
    return re.sub(r"  +", " ", _TAG_RE.sub(repl, _strip_emojis(text)))


def _resample_linear(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out or x.size == 0:
        return x
    n_out = int(round(x.shape[0] * sr_out / float(sr_in)))
    if n_out <= 0:
        return np.zeros(0, dtype=np.int16)
    src = np.linspace(0.0, x.shape[0] - 1, n_out)
    return np.interp(src, np.arange(x.shape[0]), x).astype(np.int16)


class BreezeTTSProvider:
    def __init__(self, config, local_overrides: dict | None = None, data_dir: Path | None = None):
        self.config = config
        overrides = local_overrides or {}

        def cfg(key, default=None):
            if key in overrides and overrides[key] not in (None, ""):
                return overrides[key]
            return config.get("plugins", "breeze_tts", key, default=default)

        self._host = str(cfg("host", "127.0.0.1") or "127.0.0.1")
        self._port = int(cfg("port", 8080) or 8080)
        self._ws_port = cfg("ws_port", None)

        # voice. voice_id wins, else a clip gets uploaded once to make one,
        # else it is voice design driven by instruction alone.
        self._voice_id = cfg("voice_id", None) or None
        self._ref_audio = cfg("ref_audio", None) or None
        self._ref_text = cfg("ref_text", None) or None
        self._voice_name = cfg("voice_name", None) or None

        self._instruction = str(cfg("instruction", "Speak clearly and naturally.")
                                or "Speak clearly and naturally.")
        self._cfg_scale = float(cfg("cfg_scale", 1.0))
        self._seed = int(cfg("seed", 42))
        self._temperature = float(cfg("temperature", 0))
        self._top_k = int(cfg("top_k", 0))
        self._vocal_events = bool(cfg("vocal_events", False))

        # optionally launch the server ourselves instead of expecting one up
        self._auto_start = bool(cfg("auto_start", False))
        self._exe = cfg("exe", None) or None
        self._model = cfg("model", None) or None
        self._extra_args = cfg("extra_args", None) or []

        if data_dir is None:
            data_dir = Path(".") / "data" / "plugins" / "breeze_tts"
        self._data_dir = Path(data_dir).resolve()

        self._sample_rate = 24000  # refined from the ready message
        self._target_sr = int(config.get("audio", "receive_sample_rate", default=24000))

        self._model_path = f"breeze-tts-2.cpp:{self._host}:{self._port}"
        self._device = "server"

        self._out: "queue.Queue[dict]" = queue.Queue()
        self._audio_queue: "asyncio.Queue[bytes]" = None
        self._loop = None
        self._tasks: list[asyncio.Task] = []

        self._running = False
        self._interrupted = False
        self._ready = threading.Event()
        self._loader_thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None

    # ── public api ───────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._interrupted = False
        self._loader_thread = threading.Thread(
            target=self._connect_prep, daemon=True, name="breeze_tts-prep",
        )
        self._loader_thread.start()
        logger.info(
            "breeze_tts started (server=%s:%s voice=%s)",
            self._host, self._port, self._describe_voice(),
        )

    def stop(self):
        self._running = False
        self._interrupted = True
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        if self._loader_thread is not None:
            self._loader_thread.join(timeout=3)
            self._loader_thread = None
        self._ready.clear()
        if self._proc is not None:
            proc, self._proc = self._proc, None
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass

    def feed_text(self, text: str):
        if not text:
            return
        self._interrupted = False
        cleaned = _clean(text, self._vocal_events)
        if not cleaned.strip():
            return
        self._out.put({"type": "text", "text": cleaned})

    def turn_complete(self):
        # speaks whatever is buffered even without a sentence ending
        self._out.put({"type": "end", "text": ""})

    def interrupt(self):
        self._interrupted = True
        while True:
            try:
                self._out.get_nowait()
            except queue.Empty:
                break
        self._out.put({"type": "cancel"})
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._drain_audio_queue)

    async def get_audio(self) -> bytes | None:
        self._ensure_tasks()
        try:
            return await asyncio.wait_for(self._audio_queue.get(), timeout=0.1)
        except (asyncio.TimeoutError, asyncio.QueueEmpty):
            return None

    # ── internals ────────────────────────────────────────────────────────

    def _describe_voice(self) -> str:
        if self._voice_id:
            return f"clone:{self._voice_id}"
        if self._ref_audio:
            return f"clone:{Path(str(self._ref_audio)).name}"
        return "design"

    def _drain_audio_queue(self):
        if self._audio_queue is None:
            return
        while True:
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def _ensure_tasks(self):
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        if self._audio_queue is None:
            self._audio_queue = asyncio.Queue()
        if not self._tasks and self._running:
            self._tasks = [asyncio.create_task(self._session_task())]

    # -- server discovery + voice upload (runs once on start) -------------

    def _base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def _connect_prep(self):
        import requests

        if not self._wait_for_health(timeout=3) and self._auto_start:
            self._spawn_server()

        try:
            r = requests.get(f"{self._base_url()}/health", timeout=5)
            health = r.json()
            self._sample_rate = int(health.get("sample_rate", 24000))
            if self._ws_port in (None, "", 0):
                self._ws_port = int(health.get("ws_port", self._port + 1))
        except Exception as e:
            logger.error("breeze_tts cannot reach %s: %s", self._base_url(), e)
            return

        if self._ws_port in (None, "", 0):
            logger.error("breeze_tts server has its websocket turned off")
            return

        if not self._voice_id and self._ref_audio:
            self._voice_id = self._upload_voice()

        self._ready.set()
        logger.info(
            "breeze_tts ready (ws://%s:%s, %d Hz, voice=%s)",
            self._host, self._ws_port, self._sample_rate, self._describe_voice(),
        )

    def _wait_for_health(self, timeout: float) -> bool:
        import requests
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self._running:
            try:
                if requests.get(f"{self._base_url()}/health", timeout=2).ok:
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _spawn_server(self):
        if not self._exe or not self._model:
            logger.error("breeze_tts auto_start needs both exe and model set")
            return
        args = [str(self._exe), str(self._model),
                "--host", self._host, "--port", str(self._port)]
        args += [str(a) for a in self._extra_args]
        try:
            self._proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            logger.error("breeze_tts could not launch %s: %s", self._exe, e)
            return
        logger.info("breeze_tts launched %s, waiting for it to load", Path(str(self._exe)).name)
        # a big gguf off a cold disk takes a while, so be generous here
        if not self._wait_for_health(timeout=180):
            logger.error("breeze_tts server never came up")

    def _upload_voice(self) -> str | None:
        import requests
        clip = Path(str(self._ref_audio))
        if not clip.exists():
            logger.error("breeze_tts ref_audio not found: %s", clip)
            return None
        if not self._ref_text:
            logger.error("breeze_tts ref_audio needs ref_text, the exact transcript of the clip")
            return None
        data = {"ref_text": self._ref_text}
        if self._voice_name:
            data["name"] = self._voice_name
        try:
            with open(clip, "rb") as f:
                r = requests.post(
                    f"{self._base_url()}/v1/voices",
                    files={"ref_audio": (clip.name, f, "audio/wav")},
                    data=data, timeout=120,
                )
            r.raise_for_status()
            vid = r.json().get("id")
        except Exception as e:
            logger.error("breeze_tts voice upload failed: %s", e)
            return None
        logger.info("breeze_tts registered voice '%s' from %s", vid, clip.name)
        return vid

    # -- websocket session ------------------------------------------------

    def _start_msg(self) -> dict:
        msg = {
            "type": "start",
            "instruction": self._instruction,
            "cfg_scale": self._cfg_scale,
            "seed": self._seed,
        }
        if self._voice_id:
            msg["voice_id"] = self._voice_id
        if self._ref_text and not self._voice_id:
            msg["ref_text"] = self._ref_text
        if self._temperature:
            msg["temperature"] = self._temperature
        if self._top_k:
            msg["top_k"] = self._top_k
        return msg

    async def _session_task(self):
        import websockets

        while self._running:
            if not self._ready.is_set():
                await asyncio.sleep(0.1)
                continue
            url = f"ws://{self._host}:{self._ws_port}"
            try:
                async with websockets.connect(url, max_size=None, ping_interval=20) as ws:
                    hello = json.loads(await ws.recv())
                    self._sample_rate = int(hello.get("sample_rate", self._sample_rate))
                    await ws.send(json.dumps(self._start_msg()))
                    await asyncio.gather(self._sender(ws), self._receiver(ws))
            except asyncio.CancelledError:
                return
            except Exception as e:
                if self._running:
                    logger.warning("breeze_tts socket dropped (%s), reconnecting", e)
                    await asyncio.sleep(1.0)

    async def _sender(self, ws):
        while self._running:
            try:
                msg = self._out.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.01)
                continue
            await ws.send(json.dumps(msg))

    async def _receiver(self, ws):
        async for frame in ws:
            if isinstance(frame, bytes):
                if self._interrupted or self._audio_queue is None:
                    continue
                self._audio_queue.put_nowait(self._to_target(frame))
                continue
            try:
                msg = json.loads(frame)
            except Exception:
                continue
            kind = msg.get("type")
            if kind == "speaking":
                logger.info("breeze_tts speaking: %r", str(msg.get("text", ""))[:80])
            elif kind == "error":
                logger.error("breeze_tts server error: %s", msg.get("message"))
            elif kind == "queued":
                logger.debug("breeze_tts waiting on the gpu")

    def _to_target(self, pcm: bytes) -> bytes:
        if self._sample_rate == self._target_sr:
            return pcm
        arr = np.frombuffer(pcm, dtype=np.int16)
        return _resample_linear(arr, self._sample_rate, self._target_sr).tobytes()
