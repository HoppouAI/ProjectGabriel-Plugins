"""OmniVoice TTS plugin for Project Gabriel.

Wraps k2-fsa OmniVoice (https://github.com/k2-fsa/OmniVoice) as a Gabriel
TTS provider. GPU-only diffusion model with 600+ language support, voice
cloning, voice design, and chunked streaming via sentence batching.

Activate with:
    tts:
      external_provider: omnivoice_tts

Config under `plugins.omnivoice_tts.*` in config.yml (or in
`plugins/omnivoice_tts/config.yml`, gitignored, see config.example.yml).
"""
import logging
import threading
from pathlib import Path

import yaml

from src.plugins import Plugin, PluginContext

from .provider import OmniVoiceProvider, _autodetect_device

logger = logging.getLogger(__name__)


def _load_local_config() -> dict:
    here = Path(__file__).parent / "config.yml"
    if not here.exists():
        return {}
    try:
        with open(here, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.warning(f"omnivoice_tts local config.yml failed to load: {e}")
    return {}


class OmniVoiceTTSPlugin(Plugin):
    name = "omnivoice_tts"
    version = "0.1.2"
    description = "OmniVoice TTS provider (k2-fsa, GPU diffusion, voice cloning + design, 600+ languages)"
    author = "HoppouAI"

    def setup(self, ctx: PluginContext):
        local_cfg = _load_local_config()
        data_dir = ctx.data_dir()

        def factory(config):
            return OmniVoiceProvider(
                config, local_overrides=local_cfg, data_dir=data_dir,
            )

        ctx.register_tts("omnivoice_tts", factory)

        # Same fallback the provider uses internally so the warmup key
        # matches the actual session key and the cache hit is real.
        def cfg(key, default=None):
            if key in local_cfg and local_cfg[key] not in (None, ""):
                return local_cfg[key]
            return ctx.plugin_config(key, default=default)

        warm_kwargs = dict(
            model_path=str(cfg("model", "k2-fsa/OmniVoice") or "k2-fsa/OmniVoice"),
            device=str(cfg("device", None) or _autodetect_device()),
            dtype_name=str(cfg("dtype", "float16") or "float16"),
            ref_audio=cfg("ref_audio", None) or None,
            ref_text=cfg("ref_text", None) or None,
            instruct=cfg("instruct", None) or None,
            language=cfg("language", None) or None,
            asr_model=str(cfg("asr_model", "openai/whisper-base")
                          or "openai/whisper-base"),
            use_flash_attn=bool(cfg("use_flash_attn", False)),
            use_cuda_graphs=bool(cfg("use_cuda_graphs", False)),
            max_graph_cache=int(cfg("max_graph_cache", 8)),
            cache_voice=bool(cfg("cache_voice", True)),
            voice_cache_dir=Path(data_dir) / "voices",
            low_vram=bool(cfg("low_vram", False)),
        )

        def _warm():
            try:
                OmniVoiceProvider.warmup(**warm_kwargs)
            except Exception as e:
                ctx.logger.warning(f"omnivoice_tts warmup thread crashed: {e}")

        threading.Thread(target=_warm, daemon=True, name="omnivoice_tts-warmup").start()

        ctx.logger.info(
            "omnivoice_tts registered (warming up in background). "
            "set tts.external_provider: omnivoice_tts to use it."
        )

    def teardown(self, ctx: PluginContext):
        pass


plugin = OmniVoiceTTSPlugin
