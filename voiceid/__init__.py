"""Voice fingerprinting plugin.

Subscribes to the host 'mic_chunk' event (api v3+) to keep a short ring
buffer of recent audio, then exposes tools for the AI to save / lookup
voices by username. Falls back to telling the model to use vision when
the speaker is unknown.

If auto_announce is enabled, the plugin also watches for the rising
edge of speech and, after enough audio has accumulated, fires a
background identify and pushes a short text annotation into the
current incoming user turn via ctx.send_realtime_text. So the model
sees "[Speaker: Tom]" alongside Tom's audio, in the same turn.
"""
import asyncio
import logging
import threading
import time

from src.plugins import Plugin, PluginContext

from .recognizer import VoiceRecognizer
from .tools import VoiceIDTools

logger = logging.getLogger(__name__)


class VoiceIDPlugin(Plugin):
    name = "voiceid"
    version = "0.2.0"
    api_version = 3
    description = "Voice fingerprinting via Resemblyzer, AI can save voices to usernames and identify the current speaker"
    author = "HoppouAI"

    def setup(self, ctx: PluginContext):
        buffer_seconds = float(ctx.plugin_config("buffer_seconds", 5.0))
        min_audio_seconds = float(ctx.plugin_config("min_audio_seconds", 1.5))
        threshold = float(ctx.plugin_config("similarity_threshold", 0.75))

        # auto-announce config
        self._auto_announce = bool(ctx.plugin_config("auto_announce", True))
        self._announce_delay = float(ctx.plugin_config("announce_delay_seconds", 1.6))
        self._announce_cooldown = float(ctx.plugin_config("announce_cooldown_seconds", 8.0))
        energy_threshold = float(ctx.plugin_config("energy_threshold", 500.0))
        silence_gap = float(ctx.plugin_config("silence_gap_seconds", 0.8))
        self._announce_unknown = bool(ctx.plugin_config("announce_unknown", False))

        recognizer = VoiceRecognizer(
            data_dir=ctx.data_dir(),
            buffer_seconds=buffer_seconds,
            min_audio_seconds=min_audio_seconds,
            similarity_threshold=threshold,
        )
        self._recognizer = recognizer
        self._ctx = ctx
        self._last_announce_at = 0.0
        self._pending = False
        self._pending_lock = threading.Lock()
        self._loop = None

        VoiceIDTools._recognizer = recognizer
        ctx.register_tool(VoiceIDTools)

        ctx.subscribe("mic_chunk", self._on_mic_chunk)

        if self._auto_announce:
            recognizer.set_speech_start_callback(
                self._on_speech_start,
                energy_threshold=energy_threshold,
                silence_gap_seconds=silence_gap,
            )
            ctx.logger.info(
                f"voiceid auto-announce on, delay={self._announce_delay}s, "
                f"cooldown={self._announce_cooldown}s, energy_thr={energy_threshold}"
            )

        ctx.logger.info(
            f"voiceid ready -- {len(recognizer.list_voices())} saved voice(s), "
            f"threshold={threshold}, buffer={buffer_seconds:.1f}s, min={min_audio_seconds:.1f}s"
        )

    # ---- mic chunk + loop capture ----------------------------------------

    def _on_mic_chunk(self, data, sample_rate):
        # mic_chunk fires from inside the asyncio loop thread, so this
        # is a reliable place to capture the loop ref for later use
        # from worker threads (run_coroutine_threadsafe).
        if self._loop is None:
            try:
                self._loop = asyncio.get_event_loop()
            except RuntimeError:
                pass
        self._recognizer.feed_audio(data, sample_rate)

    # ---- auto-announce ----------------------------------------------------

    def _on_speech_start(self):
        now = time.time()
        if (now - self._last_announce_at) < self._announce_cooldown:
            return
        with self._pending_lock:
            if self._pending:
                return
            self._pending = True
        # schedule on a worker thread so we dont block the audio callback
        t = threading.Thread(target=self._run_announce, name="voiceid-announce", daemon=True)
        t.start()

    def _run_announce(self):
        try:
            # wait so the ring buffer has enough recent speech for resemblyzer
            time.sleep(self._announce_delay)
            result = self._recognizer.identify_current()
            name = result.get("username", "unknown")
            conf = result.get("confidence", 0.0)
            if name == "unknown" and not self._announce_unknown:
                return
            if name == "unknown":
                annotation = "[System: current speaker is unknown, no saved voice matches]"
            else:
                annotation = f"[System: current speaker is {name} (voice match {conf:.2f})]"
            self._last_announce_at = time.time()
            # ctx.send_realtime_text is async, hop to the running loop
            loop = self._loop
            if loop is None:
                self._ctx.logger.debug("no captured loop, cant send speaker annotation")
                return
            asyncio.run_coroutine_threadsafe(
                self._ctx.send_realtime_text(annotation), loop
            )
            self._ctx.logger.debug(f"voiceid announced: {annotation}")
        except Exception as e:
            self._ctx.logger.debug(f"auto-announce failed: {e}")
        finally:
            with self._pending_lock:
                self._pending = False

    def _app_loop(self):
        # session owns the asyncio loop, grab it via the bound session ref
        session = self._ctx.session
        if session is None:
            return None
        loop = getattr(session, "_loop", None)
        if loop is not None:
            return loop
        # fallback: just try to find the running loop
        try:
            return asyncio.get_event_loop()
        except RuntimeError:
            return None

    def teardown(self, ctx: PluginContext):
        try:
            if hasattr(self, "_recognizer"):
                self._recognizer._save()
        except Exception as e:
            ctx.logger.debug(f"voiceid teardown save errored: {e}")
