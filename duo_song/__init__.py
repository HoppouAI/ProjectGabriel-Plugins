"""duo_song plugin entry.

Spins up a DuoEngine in either 'host' or 'client' role, registers tools
and a chatbox source. Library lives at data/plugins/duo_song/library/
unless the user overrides it via plugins.duo_song.library_dir in config.

Config sources in priority order (later wins):
  1. built in defaults below
  2. main config.yml under plugins.duo_song.*
  3. duo_song/config.yml dropped next to this file (gitignored)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from src.plugins import Plugin, PluginContext

from .chatbox import DuoChatbox
from .engine import DuoEngine
from .tools import DuoSongTools

logger = logging.getLogger(__name__)


def _load_local_config(plugin_dir: Path) -> Dict[str, Any]:
    """Read duo_song/config.yml if present. Returns {} if missing or yaml
    isnt installed (host ships PyYAML so that should never happen)."""
    path = plugin_dir / "config.yml"
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore
    except Exception as e:
        logger.warning(f"duo_song: PyYAML not available, skipping local config: {e}")
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            logger.warning(f"duo_song: {path} root must be a mapping, got {type(data).__name__}")
            return {}
        return data
    except Exception as e:
        logger.warning(f"duo_song: failed to read {path}: {e}")
        return {}


class DuoSongPlugin(Plugin):
    name = "duo_song"
    version = "0.1.0"
    description = "Two Gabriel instances on the same LAN play synced songs together, with shared chatbox display"
    author = "HoppouAI"

    def setup(self, ctx: PluginContext):
        plugin_dir = Path(__file__).parent
        local_cfg = _load_local_config(plugin_dir)

        def cfg(key: str, default=None):
            # local file wins over main config.yml
            if key in local_cfg and local_cfg[key] is not None:
                return local_cfg[key]
            return ctx.plugin_config(key, default)

        role = str(cfg("role", "host") or "host").lower()
        instance_name = str(cfg("instance_name", "gabriel") or "gabriel")
        bind = str(cfg("bind", "0.0.0.0") or "0.0.0.0")
        host_address = str(cfg("host_address", "127.0.0.1") or "127.0.0.1")
        port = int(cfg("port", 8765) or 8765)
        lead = float(cfg("schedule_lead_seconds", 1.2) or 1.2)
        volume = float(cfg("volume", 0.6) or 0.6)
        auto_reconnect = float(cfg("auto_reconnect_seconds", 5.0) or 5.0)
        chatbox_priority = int(cfg("chatbox_priority", 30) or 30)

        # which part this instance sings. Accepts 1, 2, "PT1", "PT2", "pt1"...
        # blank/None falls back to: host = 1, client = 2.
        local_part_raw = cfg("local_part", None)
        local_part: int | None = None
        if local_part_raw is not None and str(local_part_raw).strip() != "":
            s = str(local_part_raw).strip().lower().lstrip("pt").strip()
            try:
                v = int(s)
                if v in (1, 2):
                    local_part = v
                else:
                    ctx.logger.warning(f"duo_song: local_part must be 1 or 2, got {local_part_raw!r}, ignoring")
            except ValueError:
                ctx.logger.warning(f"duo_song: local_part must be 1 or 2, got {local_part_raw!r}, ignoring")

        lib_override = cfg("library_dir")
        if lib_override:
            library_dir = Path(str(lib_override)).expanduser()
        else:
            # default lives in the host's working dir, sister to the rest of
            # the music sfx tree, NOT under data/plugins/.
            library_dir = Path("sfx") / "music" / "duo"
        library_dir.mkdir(parents=True, exist_ok=True)

        engine = DuoEngine(
            role=role,
            instance_name=instance_name,
            bind=bind,
            host_address=host_address,
            port=port,
            library_dir=library_dir,
            schedule_lead_seconds=lead,
            volume=volume,
            auto_reconnect_seconds=auto_reconnect,
            local_part=local_part,
        )
        DuoSongTools._engine = engine
        ctx.register_tool(DuoSongTools)

        chatbox = DuoChatbox(engine)
        try:
            ctx.register_chatbox_source("duo_song", chatbox, priority=chatbox_priority)
        except Exception as e:
            ctx.logger.warning(f"duo_song: chatbox registration failed: {e}")

        # bring up the network once the host's loop is alive
        ctx.subscribe("startup", lambda: engine.start())
        ctx.subscribe("shutdown", lambda: engine.stop())

        self._engine = engine
        ctx.logger.info(
            f"duo_song ready, role={role}, name={instance_name}, sings PT{engine.local_part}, "
            f"{'listening on' if role == 'host' else 'will dial'} "
            f"{(bind if role == 'host' else host_address)}:{port}, "
            f"library={library_dir}, "
            f"local_config={'yes' if local_cfg else 'no'}"
        )
        if not engine.player.available():
            ctx.logger.warning("duo_song: pygame mixer not available, playback will be a noop. install pygame.")

    def teardown(self, ctx: PluginContext):
        try:
            if hasattr(self, "_engine") and self._engine is not None:
                self._engine.stop()
        except Exception as e:
            ctx.logger.warning(f"duo_song teardown failed: {e}")


plugin = DuoSongPlugin
