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
    version = "0.2.2"
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

        def voiceid_prompt():
            return (
                "**Voice Identification:** Run `identifyCurrentSpeaker` whenever someone speaks "
                "to you so you can recognize their voice and know who is speaking. If the result is "
                "unknown, you should ask for their name."
            )

        ctx.register_prompt_contributor("voiceid_instruction", voiceid_prompt)
        if hasattr(ctx, "discord") and ctx.discord:
            ctx.discord.register_prompt_contributor("voiceid_instruction", voiceid_prompt)

        # Preload the voiceid encoder in a background thread so it's fully ready at startup without blocking
        import threading
        def preload():
            try:
                recognizer._ensure_encoder()
                ctx.logger.info("voiceid: background preloading of model complete")
            except Exception as e:
                ctx.logger.warning(f"voiceid: failed to preload model: {e}")

        threading.Thread(target=preload, daemon=True).start()

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
