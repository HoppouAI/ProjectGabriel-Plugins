"""midi_band plugin entry. Spins up either a BandServer (host) or a
BandClient (client) plus a shared MidiPlayer, registers AI tools, and a
per-instance chatbox source.

Config sources in priority order (later wins):
  1. built-in defaults below
  2. main config.yml under plugins.midi_band.*
  3. midi_band/config.yml dropped next to this file (gitignored)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

try:
    from src.plugins import Plugin, PluginContext
    _HOST_AVAILABLE = True
except ImportError:
    # Allow `import midi_band.*` outside a Project Gabriel install (the
    # standalone client only needs the sibling modules, not the host
    # plugin entry).
    _HOST_AVAILABLE = False

    class Plugin:  # type: ignore[no-redef]
        pass

    class PluginContext:  # type: ignore[no-redef]
        pass

from .chatbox import BandChatbox
from .client import BandClient
from .player import MidiPlayer
from .server import BandServer
from .webui_server import WebUiServer

# Tools depend on Project Gabriel's BaseTool. Skip when the package is
# imported by the standalone client outside Gabriel.
if _HOST_AVAILABLE:
    from .tools import BandTools

logger = logging.getLogger(__name__)


def _load_local_config(plugin_dir: Path) -> Dict[str, Any]:
    path = plugin_dir / "config.yml"
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore
    except Exception as e:
        logger.warning(f"midi_band: PyYAML missing, skipping local config: {e}")
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            logger.warning(f"midi_band: {path} root must be a mapping")
            return {}
        return data
    except Exception as e:
        logger.warning(f"midi_band: failed to read {path}: {e}")
        return {}


class MidiBandPlugin(Plugin):
    name = "midi_band"
    version = "0.1.0"
    description = "Multiple Gabriel instances on a LAN form a band, each plays different MIDI tracks of the same song in sync via a soundfont"
    author = "HoppouAI"

    def setup(self, ctx: PluginContext):
        plugin_dir = Path(__file__).parent
        local = _load_local_config(plugin_dir)

        def cfg(key, default=None):
            if key in local and local[key] is not None:
                return local[key]
            return ctx.plugin_config(key, default)

        role = str(cfg("role", "host") or "host").lower()
        instance_name = str(cfg("instance_name", "gabriel") or "gabriel")
        bind = str(cfg("bind", "0.0.0.0") or "0.0.0.0")
        host_address = str(cfg("host_address", "127.0.0.1") or "127.0.0.1")
        port = int(cfg("port", 8766) or 8766)
        lead = float(cfg("schedule_lead_seconds", 1.5) or 1.5)
        gain = float(cfg("synth_gain", 0.5) or 0.5)
        driver = cfg("audio_driver")
        chatbox_priority = int(cfg("chatbox_priority", 25) or 25)

        sf = cfg("soundfont")
        soundfont = Path(str(sf)).expanduser() if sf else None

        lib = cfg("library_dir")
        if lib:
            library_dir = Path(str(lib)).expanduser()
        else:
            # Default: a local midi/ folder right next to this plugin so
            # users can just drop files in without touching anything else.
            library_dir = plugin_dir / "midi"
        library_dir.mkdir(parents=True, exist_ok=True)

        cache_dir = ctx.data_dir() / "received"
        fs_install_dir = ctx.data_dir() / "fluidsynth"
        auto_install = bool(cfg("auto_install_fluidsynth", True))

        player = MidiPlayer(
            soundfont=soundfont,
            gain=gain,
            driver=str(driver) if driver else None,
            auto_install_dir=fs_install_dir,
            auto_install=auto_install,
        )

        if role == "host":
            server = BandServer(
                bind=bind,
                port=port,
                instance_name=instance_name,
                player=player,
                library_dir=library_dir,
                schedule_lead_seconds=lead,
            )
            BandTools._server = server
            BandTools._client = None
            chatbox = BandChatbox(lambda: _host_chatbox_status(server))
            ctx.subscribe("startup", lambda: server.start())
            ctx.subscribe("shutdown", lambda: server.stop())
            self._server = server
            self._client = None

            webui_enabled = bool(cfg("webui_enabled", True))
            webui_bind = str(cfg("webui_bind", "0.0.0.0") or "0.0.0.0")
            webui_port = int(cfg("webui_port", 8767) or 8767)
            if webui_enabled:
                webui = WebUiServer(
                    bind=webui_bind, port=webui_port,
                    library_dir=library_dir, instance_name=instance_name,
                    band_server=server,
                )
                ctx.subscribe("startup", lambda: webui.start())
                ctx.subscribe("shutdown", lambda: webui.stop())
                self._webui = webui
            else:
                self._webui = None
        else:
            client = BandClient(
                host=host_address,
                port=port,
                name=instance_name,
                player=player,
                cache_dir=cache_dir,
            )
            BandTools._server = None
            BandTools._client = client
            chatbox = BandChatbox(lambda: client.status())
            ctx.subscribe("startup", lambda: client.start())
            ctx.subscribe("shutdown", lambda: client.stop())
            self._server = None
            self._client = client

        ctx.register_tool(BandTools)
        try:
            ctx.register_chatbox_source("midi_band", chatbox, priority=chatbox_priority)
        except Exception as e:
            ctx.logger.warning(f"midi_band: chatbox registration failed: {e}")

        ctx.subscribe("shutdown", lambda: player.shutdown())

        if not player.available():
            if soundfont is None:
                ctx.logger.warning(
                    "midi_band: no soundfont configured. set the 'soundfont' option to a .sf2 path."
                )
            else:
                ctx.logger.warning(
                    f"midi_band: player not available, soundfont={soundfont}, "
                    "make sure pyfluidsynth and the fluidsynth native library are installed."
                )

        ctx.logger.info(
            f"midi_band ready, role={role}, name={instance_name}, "
            f"{'listening on' if role == 'host' else 'will dial'} "
            f"{(bind if role == 'host' else host_address)}:{port}, "
            f"library={library_dir}, "
            f"local_config={'yes' if local else 'no'}"
        )

    def teardown(self, ctx: PluginContext):
        try:
            if getattr(self, "_server", None) is not None:
                self._server.stop()
            if getattr(self, "_client", None) is not None:
                self._client.stop()
            if getattr(self, "_webui", None) is not None:
                self._webui.stop()
        except Exception as e:
            ctx.logger.warning(f"midi_band teardown failed: {e}")


def _host_chatbox_status(server) -> dict:
    info = server.loaded_info()
    ps = server.player.status()
    track_names = ps.get("tracks") or []
    if not track_names:
        # no playback yet, fall back to the assigned host tracks
        host_tracks = info.get("host_tracks") or []
        all_tracks = info.get("tracks") or []
        track_names = [
            all_tracks[i]["name"] for i in host_tracks
            if 0 <= i < len(all_tracks)
        ]
    return {
        "song": ps.get("song") or info.get("song"),
        "tracks": track_names,
        "playing": ps.get("playing"),
        "position": ps.get("position"),
        "duration": ps.get("duration") or info.get("duration"),
    }


plugin = MidiBandPlugin
