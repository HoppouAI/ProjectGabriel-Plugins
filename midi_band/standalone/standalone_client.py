"""Standalone midi_band client. Connects to a midi_band host and plays
whatever MIDI tracks the host assigns through fluidsynth + a soundfont.

This script lives one folder below the midi_band package and reaches up
to import the shared client/player/protocol modules. No Project Gabriel
install required, just the deps in requirements.txt / pyproject.toml.

Usage (from this folder):

    # uv (recommended)
    uv sync
    uv run standalone_client.py --host 192.168.1.50 --name drummer \\
        --soundfont C:/sf2/GeneralUser.sf2

    # plain python
    pip install -r requirements.txt
    python standalone_client.py --host 192.168.1.50 --name drummer \\
        --soundfont C:/sf2/GeneralUser.sf2

Ctrl+C to quit.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import threading
import time
import logging
import signal
import sys
from pathlib import Path

# This file lives in midi_band/standalone/. Add midi_band's PARENT to
# sys.path so we can `from midi_band.* import ...` whether we're run as
# a script or via `uv run`.
_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent          # midi_band/
_PARENT = _PKG_ROOT.parent        # folder containing midi_band/
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from midi_band.client import BandClient  # noqa: E402
from midi_band.player import MidiPlayer  # noqa: E402
from midi_band.audio_player import AudioPlayer, audio_import_error  # noqa: E402
from midi_band import protocol as P      # noqa: E402

from cli_ui import (  # noqa: E402
    setup_logging,
    print_banner,
    make_status_printer,
)


DEFAULT_CONFIG_PATH = _HERE / "config.yml"


def _load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore
    except ImportError:
        print(
            f"warning: config.yml found at {path} but PyYAML isn't installed, "
            "ignoring it. install with: pip install pyyaml",
            file=sys.stderr,
        )
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"warning: failed to read {path}: {e}", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        print(f"warning: {path} root must be a mapping, ignoring", file=sys.stderr)
        return {}
    return data


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="midi_band-standalone",
        description=(
            "Standalone midi_band client. Joins a Project Gabriel midi_band host as a musician. "
            "Reads defaults from config.yml in this folder if present, CLI args override."
        ),
    )
    p.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to config.yml (default: {DEFAULT_CONFIG_PATH.name} next to this script).",
    )
    p.add_argument("--host", default=None, help="midi_band host address (LAN IP).")
    p.add_argument("--port", type=int, default=None, help="midi_band host port.")
    p.add_argument("--name", default=None, help="Display name for this client (your bandmate name).")
    p.add_argument("--soundfont", default=None, help="Path to the .sf2 soundfont to use for synthesis.")
    p.add_argument("--gain", type=float, default=None, help="Synth output gain 0.0 - 2.0.")
    p.add_argument(
        "--driver", default=None,
        help="fluidsynth audio driver (dsound, alsa, coreaudio, pulseaudio, ...). Default = autodetect.",
    )
    p.add_argument(
        "--device", default=None,
        help="Audio device name to send output to. Eg the name of a virtual audio cable. Default = system default.",
    )
    p.add_argument(
        "--audio-device", default=None,
        help="Output device for AUDIO BAND mode (stem playback via sounddevice). "
             "Name substring or index. Blank = use --device, then system default.",
    )
    p.add_argument(
        "--cache-dir", default=None,
        help="Folder to save MIDI files received from the host.",
    )
    p.add_argument(
        "--fluidsynth-dir", default=None,
        help="Folder for the auto-installed fluidsynth native library on Windows.",
    )
    p.add_argument(
        "--no-auto-install", action="store_true",
        help="Don't try to download fluidsynth if missing. Use only what is already on PATH.",
    )
    p.add_argument("--log-level", default=None, help="Python logging level.")
    p.add_argument(
        "--list-devices", action="store_true",
        help="Print all audio output device names this machine can route to and exit. "
             "Use the names exactly as printed for --device.",
    )
    return p


def _pick(cli_val, cfg, key, default):
    if cli_val is not None:
        return cli_val
    if isinstance(cfg, dict) and cfg.get(key) not in (None, ""):
        return cfg[key]
    return default


def _set_windows_app_id(name: str) -> None:
    """Tell windows this process is its own application for per-app audio
    routing. Without this, every bandmate inherits the launcher's
    AppUserModelID and the volume mixer lumps them all under one entry,
    so picking an output device for one applies to all of them.
    """
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        safe = re.sub(r"[^A-Za-z0-9.]+", ".", name).strip(".") or "bandmate"
        aumid = f"HoppouAI.ProjectGabriel.MidiBand.{safe}"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(aumid)
    except Exception:
        pass


def _rename_audio_session_loop(name: str, log: logging.Logger) -> None:
    """Rename this process's WASAPI audio session so the windows volume
    mixer shows the bandmate name instead of 'Python'. The session
    doesn't exist until fluidsynth opens an audio device, so retry for
    a while. Best-effort, no-op if pycaw isn't installed.
    """
    if not sys.platform.startswith("win"):
        return
    try:
        from pycaw.pycaw import AudioUtilities  # type: ignore
    except Exception as e:
        log.info(
            "pycaw not installed, the volume mixer will keep showing this "
            f"bandmate as 'Python' instead of '{name}'. install with: "
            "pip install pycaw   ({e})".format(e=e)
        )
        return
    my_pid = os.getpid()
    label = f"midi_band: {name}"
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        try:
            sessions = AudioUtilities.GetAllSessions()
            for s in sessions:
                try:
                    if s.Process and s.Process.pid == my_pid:
                        s._ctl.SetDisplayName(label, None)
                        log.info(f"renamed audio session to '{label}' in volume mixer")
                        return
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(1.0)
    log.debug("gave up renaming audio session, never showed up")


def _list_devices_and_exit():
    # on windows, prefer pycaw - it queries IMMDevice directly so we get
    # the FULL wasapi friendly name. sounddevice/portaudio truncates names
    # at 31 chars (PaDeviceInfo.name buffer) which makes long names like
    # "CABLE-B Input (VB-Audio Cable B)" appear as "CABLE-B Input (VB-Audio Cable B"
    # and fluidsynth then can't match them.
    if sys.platform.startswith("win"):
        try:
            from pycaw.pycaw import AudioUtilities  # type: ignore
            print("\nOUTPUT DEVICES (use the name exactly as printed for --device):\n")
            seen = set()
            for spk in AudioUtilities.GetAllDevices():
                try:
                    flow = int(getattr(spk, "dataFlow", 0))
                except Exception:
                    flow = 0
                if flow != 0:
                    continue
                name = str(getattr(spk, "FriendlyName", "") or "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                print(f"  [WASAPI] {name}")
            print(
                "\nThese are the exact names the wasapi driver accepts. Copy & paste\n"
                "into --device or the launcher dropdown verbatim, including parens.\n"
            )
            sys.exit(0)
        except ImportError:
            print("pycaw not installed, falling back to sounddevice (names may be truncated past 31 chars).\n")
    try:
        import sounddevice as sd  # type: ignore
        try:
            apis = sd.query_hostapis()
        except Exception:
            apis = []
        print("\nOUTPUT DEVICES (use the name exactly as printed for --device):\n")
        seen = set()
        for d in sd.query_devices():
            if d.get("max_output_channels", 0) <= 0:
                continue
            name = str(d.get("name", "")).strip()
            api_idx = d.get("hostapi", -1)
            api_name = apis[api_idx]["name"] if 0 <= api_idx < len(apis) else "?"
            line = f"  [{api_name:<14}] {name}"
            if line in seen:
                continue
            seen.add(line)
            print(line)
        print(
            "\nWindows tips:\n"
            "  - dsound driver: use the friendly name as shown by 'MME' or 'Windows DirectSound' rows.\n"
            "  - wasapi driver: use the friendly name as shown by 'Windows WASAPI' rows.\n"
            "  - virtual audio cables show up as 'Line 1 (Virtual Audio Cable)' or 'CABLE Input (VB-Audio Virtual Cable)'.\n"
        )
    except ImportError:
        print(
            "sounddevice isn't installed. install with:  pip install sounddevice\n"
            "or just leave --device blank to use the system default device."
        )
    sys.exit(0)


def main():
    args = _build_parser().parse_args()
    if args.list_devices:
        _list_devices_and_exit()
    cfg = _load_config(Path(args.config).expanduser())

    host = _pick(args.host, cfg, "host", None)
    port = int(_pick(args.port, cfg, "port", P.DEFAULT_PORT))
    name = _pick(args.name, cfg, "name", None)
    soundfont = _pick(args.soundfont, cfg, "soundfont", None)
    gain = float(_pick(args.gain, cfg, "gain", 0.5))
    driver = _pick(args.driver, cfg, "driver", None)
    device = _pick(args.device, cfg, "device", None)
    audio_device = _pick(args.audio_device, cfg, "audio_device", None)
    cache_dir = _pick(args.cache_dir, cfg, "cache_dir", "midi_cache")
    fluidsynth_dir = _pick(args.fluidsynth_dir, cfg, "fluidsynth_dir", "vendor/fluidsynth")
    # bool: cli flag forces False, otherwise config wins, default True
    if args.no_auto_install:
        auto_install = False
    else:
        auto_install = bool(cfg.get("auto_install_fluidsynth", True))
    log_level = _pick(args.log_level, cfg, "log_level", "INFO")

    setup_logging(str(log_level))
    log = logging.getLogger("midi_band.standalone")

    # set a unique windows app id BEFORE any audio session is opened so
    # the volume mixer can route this bandmate to its own device.
    if name:
        _set_windows_app_id(str(name))

    missing = [k for k, v in (("host", host), ("name", name)) if not v]
    if missing:
        log.error(
            f"missing required setting(s): {', '.join(missing)}. "
            f"Provide them via CLI args or in {args.config}."
        )
        sys.exit(2)

    # soundfont is only needed for midi band mode. audio band mode plays
    # stems the host streams over, no soundfont required. so it's optional
    # now, we just warn if it's missing or bad and let audio mode carry on.
    sf_path = None
    if soundfont:
        cand = Path(str(soundfont)).expanduser()
        if cand.exists():
            sf_path = cand
        else:
            log.warning(f"soundfont not found: {cand}, midi band mode will be silent")

    cache = Path(str(cache_dir)).expanduser()
    fs_dir = Path(str(fluidsynth_dir)).expanduser()

    player = MidiPlayer(
        soundfont=sf_path,
        gain=gain,
        driver=str(driver) if driver else None,
        device=str(device) if device else None,
        auto_install_dir=fs_dir,
        auto_install=auto_install,
    )
    # audio band mode can target its own output, falls back to the fluidsynth
    # device when not set so existing single-device setups keep working
    audio_out = audio_device if audio_device else device
    audio_player = AudioPlayer(gain=gain, device=audio_out if audio_out else None)

    midi_ok = player.available()
    audio_ok = audio_player.available()
    if not midi_ok and not audio_ok:
        err = audio_import_error()
        log.error(
            "this client can't make sound: no usable soundfont for midi mode "
            "and the audio band deps aren't installed"
            + (f" ({err})" if err else "")
            + ". install sounddevice + soundfile + numpy, or point --soundfont "
            "at a .sf2 file."
        )
        sys.exit(1)
    if not midi_ok:
        log.info("no soundfont, this bandmate only plays in audio band mode")
    if not audio_ok:
        log.info(
            "audio band deps missing (sounddevice/soundfile/numpy), this "
            "bandmate only plays in midi mode"
        )

    print_banner(
        name=str(name),
        host=str(host),
        port=port,
        soundfont=str(sf_path) if sf_path else "(none, audio band only)",
        gain=gain,
        driver=str(driver) if driver else None,
        device=str(device) if device else None,
        audio_device=str(audio_device) if audio_device else None,
        cache_dir=str(cache),
        auto_install=auto_install,
        fluidsynth_dir=str(fs_dir),
    )

    client = BandClient(
        host=str(host),
        port=port,
        name=str(name),
        player=player,
        cache_dir=cache,
        audio_player=audio_player,
    )
    client.on_change = make_status_printer(client.status, log)

    async def run():
        client.start()
        # rename the volume mixer entry once fluidsynth opens its session
        threading.Thread(
            target=_rename_audio_session_loop, args=(str(name), log), daemon=True
        ).start()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _on_sig(*_):
            log.info("shutting down")
            stop.set()

        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(s, _on_sig)
            except (NotImplementedError, RuntimeError):
                signal.signal(s, lambda *_: stop.set())

        log.info(
            f"midi_band standalone client '{name}' connecting to "
            f"{host}:{port}, soundfont={sf_path.name}"
        )
        await stop.wait()
        client.stop()
        player.shutdown()
        audio_player.shutdown()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
