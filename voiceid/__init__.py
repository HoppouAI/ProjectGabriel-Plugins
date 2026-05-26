"""Voice fingerprinting plugin.

Subscribes to the host 'mic_chunk' event (api v3+) to keep a short ring
buffer of recent audio, then exposes tools for the AI to save / lookup
voices by username. The model decides when to call identifyCurrentSpeaker
on its own, the plugin doesnt push annotations into the turn anymore."""
import logging

from src.plugins import Plugin, PluginContext

from .recognizer import VoiceRecognizer
from .tools import VoiceIDTools

logger = logging.getLogger(__name__)


class VoiceIDPlugin(Plugin):
    name = "voiceid"
    version = "0.2.1"
    api_version = 3
    description = "Voice fingerprinting via Resemblyzer, AI can save voices to usernames and identify the current speaker"
    author = "HoppouAI"

    def setup(self, ctx: PluginContext):
        buffer_seconds = float(ctx.plugin_config("buffer_seconds", 5.0))
        min_audio_seconds = float(ctx.plugin_config("min_audio_seconds", 1.5))
        threshold = float(ctx.plugin_config("similarity_threshold", 0.75))

        recognizer = VoiceRecognizer(
            data_dir=ctx.data_dir(),
            buffer_seconds=buffer_seconds,
            min_audio_seconds=min_audio_seconds,
            similarity_threshold=threshold,
        )
        self._recognizer = recognizer

        VoiceIDTools._recognizer = recognizer
        ctx.register_tool(VoiceIDTools)

        ctx.subscribe(
            "mic_chunk",
            lambda data, sample_rate: recognizer.feed_audio(data, sample_rate),
        )

        ctx.logger.info(
            f"voiceid ready -- {len(recognizer.list_voices())} saved voice(s), "
            f"threshold={threshold}, buffer={buffer_seconds:.1f}s, min={min_audio_seconds:.1f}s"
        )

    def teardown(self, ctx: PluginContext):
        try:
            if hasattr(self, "_recognizer"):
                self._recognizer._save()
        except Exception as e:
            ctx.logger.debug(f"voiceid teardown save errored: {e}")
