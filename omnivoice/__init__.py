"""OmniVoice TTS plugin.

Wraps a local `omnivoice-serve` REST server (`/v1/tts/stream`) as a
Gabriel TTS provider. Output is 16-bit PCM mono 24kHz which is what
the audio pipeline expects natively, so no resampling.

Activate with:
    tts:
      external_provider: omnivoice

Config under `plugins.omnivoice.*` in config.yml (or in
`plugins/omnivoice/config.yml`, gitignored, see config.example.yml).
"""
import logging
from pathlib import Path

import yaml

from src.plugins import Plugin, PluginContext

from .provider import OmniVoiceTTSProvider

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
        logger.warning(f"omnivoice local config.yml failed to load: {e}")
    return {}


class OmniVoicePlugin(Plugin):
    name = "omnivoice"
    version = "0.1.0"
    description = "OmniVoice TTS provider (REST streaming)"
    author = "HoppouAI"

    def setup(self, ctx: PluginContext):
        local_cfg = _load_local_config()

        def factory(config):
            return OmniVoiceTTSProvider(config, local_overrides=local_cfg)

        ctx.register_tts("omnivoice", factory)
        ctx.logger.info(
            "omnivoice tts registered. set tts.external_provider: omnivoice to use it."
        )

    def teardown(self, ctx: PluginContext):
        pass
