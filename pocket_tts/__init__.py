"""Pocket TTS plugin.

Wraps Kyutai Labs `pocket-tts` (https://github.com/kyutai-labs/pocket-tts)
as a Gabriel TTS provider. Runs entirely on the CPU, no server, no api
keys. Supports the built in voice catalog and persistent voice cloning
from a single audio file (cached as .safetensors so reruns are fast).

Activate with:
    tts:
      external_provider: pocket_tts

Config under `plugins.pocket_tts.*` in config.yml (or in
`plugins/pocket_tts/config.yml`, gitignored, see config.example.yml).
"""
import logging
from pathlib import Path

import yaml

from src.plugins import Plugin, PluginContext

from .provider import PocketTTSProvider

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
        logger.warning(f"pocket_tts local config.yml failed to load: {e}")
    return {}


class PocketTTSPlugin(Plugin):
    name = "pocket_tts"
    version = "0.1.0"
    description = "Pocket TTS provider (Kyutai, CPU-only, voice cloning)"
    author = "HoppouAI"

    def setup(self, ctx: PluginContext):
        local_cfg = _load_local_config()
        data_dir = ctx.data_dir()

        def factory(config):
            return PocketTTSProvider(
                config, local_overrides=local_cfg, data_dir=data_dir,
            )

        ctx.register_tts("pocket_tts", factory)
        ctx.logger.info(
            "pocket_tts registered. set tts.external_provider: pocket_tts to use it."
        )

    def teardown(self, ctx: PluginContext):
        pass


plugin = PocketTTSPlugin
