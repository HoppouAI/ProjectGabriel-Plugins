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
import threading
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


def _coerce_float_opt(v):
    if v in (None, "", "null"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class PocketTTSPlugin(Plugin):
    name = "pocket_tts"
    version = "0.1.5"
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

        # Same fallback the provider uses internally (local sidecar yml
        # wins over host config), so the warmup key matches the actual
        # session key and the cache hit is real.
        def cfg(key, default=None):
            if key in local_cfg and local_cfg[key] not in (None, ""):
                return local_cfg[key]
            return ctx.plugin_config(key, default=default)

        warm_kwargs = dict(
            language=str(cfg("language", "english") or "english"),
            voice=str(cfg("voice", "alba") or "alba"),
            quantize=bool(cfg("quantize", False)),
            temperature=float(cfg("temperature", 0.7) or 0.7),
            lsd_decode_steps=int(cfg("lsd_decode_steps", 1) or 1),
            eos_threshold=float(cfg("eos_threshold", -4.0) if cfg("eos_threshold", -4.0) not in (None, "") else -4.0),
            noise_clamp=_coerce_float_opt(cfg("noise_clamp", None)),
            truncate_clone=bool(cfg("truncate_clone", False)),
            cache_voice=bool(cfg("cache_voice", True)),
            voice_cache_dir=Path(data_dir) / "voices",
        )

        def _warm():
            try:
                PocketTTSProvider.warmup(**warm_kwargs)
            except Exception as e:
                ctx.logger.warning(f"pocket_tts warmup thread crashed: {e}")

        threading.Thread(target=_warm, daemon=True, name="pocket_tts-warmup").start()

        ctx.logger.info(
            "pocket_tts registered (warming up in background). "
            "set tts.external_provider: pocket_tts to use it."
        )

    def teardown(self, ctx: PluginContext):
        pass


plugin = PocketTTSPlugin
