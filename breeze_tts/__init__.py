"""Breeze-TTS-2.cpp TTS plugin for Project Gabriel.

Wraps the breeze-server websocket API from
https://github.com/HoppouAI/Breeze-TTS-2.cpp as a Gabriel TTS provider.
The server holds the model, we just stream text at it and pull PCM back,
so nothing heavy loads inside Gabriel's process.

Activate with:
    tts:
      provider: breeze_tts

Config under `plugins.breeze_tts.*` in config.yml (or in
`plugins/breeze_tts/config.yml`, gitignored, see config.example.yml).

You need a breeze-server running, either started yourself or launched by
this plugin with `auto_start: true`. See the plugin README.
"""
import logging
from pathlib import Path

import yaml

from src.plugins import Plugin, PluginContext

from .provider import BreezeTTSProvider

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
        logger.warning(f"breeze_tts local config.yml failed to load: {e}")
    return {}


class BreezeTTSPlugin(Plugin):
    name = "breeze_tts"
    version = "0.1.0"
    description = "Breeze TTS 2 via the breeze-tts-2.cpp server (websocket streaming)"
    author = "HoppouAI"

    def setup(self, ctx: PluginContext):
        local_cfg = _load_local_config()
        data_dir = ctx.data_dir()

        def factory(config):
            return BreezeTTSProvider(
                config, local_overrides=local_cfg, data_dir=data_dir,
            )

        ctx.register_tts("breeze_tts", factory)
        ctx.logger.info(
            "breeze_tts registered. set tts.provider: breeze_tts to use it."
        )

    def teardown(self, ctx: PluginContext):
        pass


plugin = BreezeTTSPlugin
