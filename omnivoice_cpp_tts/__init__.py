"""omnivoice.cpp TTS plugin for Project Gabriel.

Wraps the native omnivoice.cpp engine (ServeurpersoCom fork of OmniVoice,
https://github.com/ServeurpersoCom/omnivoice.cpp) as a Gabriel TTS provider.
Runs quantized GGUF weights in-process through ctypes, no torch and no
separate server. Faster and lighter on VRAM than the python omnivoice_tts
plugin, at the cost of needing the native lib built for your GPU.

Activate with:
    tts:
      external_provider: omnivoice_cpp_tts

Config under `plugins.omnivoice_cpp_tts.*` in config.yml (or in
`plugins/omnivoice_cpp_tts/config.yml`, gitignored, see config.example.yml).

You MUST point it at a built/prebuilt omnivoice.dll via `lib_dir` (or the
OMNIVOICE_CPP_DIR env var). The GGUF models auto-download from HuggingFace
on first run. See the plugin README for how to build the native engine for
your GPU architecture.
"""
import logging
import threading
from pathlib import Path

import yaml

from src.plugins import Plugin, PluginContext

from .provider import OmniVoiceCppProvider

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
        logger.warning(f"omnivoice_cpp_tts local config.yml failed to load: {e}")
    return {}


class OmniVoiceCppTTSPlugin(Plugin):
    name = "omnivoice_cpp_tts"
    version = "0.1.0"
    description = "OmniVoice TTS via native omnivoice.cpp (ctypes, GGUF, no torch)"
    author = "HoppouAI"

    def setup(self, ctx: PluginContext):
        local_cfg = _load_local_config()
        data_dir = ctx.data_dir()

        def factory(config):
            return OmniVoiceCppProvider(
                config, local_overrides=local_cfg, data_dir=data_dir,
            )

        ctx.register_tts("omnivoice_cpp_tts", factory)

        # same fallback the provider uses internally (local sidecar yml wins
        # over host config), so the warmup engine key matches the real
        # session key and the cache hit is real.
        def cfg(key, default=None):
            if key in local_cfg and local_cfg[key] not in (None, ""):
                return local_cfg[key]
            return ctx.plugin_config(key, default=default)

        warm_kwargs = dict(
            lib_dir=cfg("lib_dir", None) or None,
            model_repo=str(cfg("model_repo", "Serveurperso/OmniVoice-GGUF")
                           or "Serveurperso/OmniVoice-GGUF"),
            model_variant=str(cfg("model_variant", "Q8_0") or "Q8_0"),
            base_model=cfg("base_model", None) or None,
            codec_model=cfg("codec_model", None) or None,
            use_fa=bool(cfg("use_fa", True)),
            clamp_fp16=bool(cfg("clamp_fp16", False)),
            data_dir=Path(data_dir),
        )

        def _warm():
            try:
                OmniVoiceCppProvider.warmup(**warm_kwargs)
            except Exception as e:
                ctx.logger.warning(f"omnivoice_cpp_tts warmup thread crashed: {e}")

        threading.Thread(
            target=_warm, daemon=True, name="omnivoice_cpp_tts-warmup",
        ).start()

        ctx.logger.info(
            "omnivoice_cpp_tts registered (warming up in background). "
            "set tts.external_provider: omnivoice_cpp_tts to use it."
        )

    def teardown(self, ctx: PluginContext):
        pass


plugin = OmniVoiceCppTTSPlugin
