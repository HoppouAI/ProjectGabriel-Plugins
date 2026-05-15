"""CLI formatting for the midi_band standalone client. Mirrors the look of
the main Project Gabriel CLI: colorama-fixed ANSI palette, compact
ColoredFormatter for logs, key/value banner with component dots, and a
state-change printer that the client calls every time something
interesting happens.
"""
from __future__ import annotations

import logging
import sys
from typing import Callable, Optional

try:
    from colorama import just_fix_windows_console
except Exception:
    just_fix_windows_console = None  # type: ignore


def _enable_ansi():
    if sys.platform == "win32":
        if just_fix_windows_console is not None:
            try:
                just_fix_windows_console()
            except Exception:
                pass
        # force the console's output code page to UTF-8 so unicode
        # box-drawing / dots in the banner render correctly even when
        # the user's default cp is 437 / 1252.
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    if hasattr(sys.stdout, "reconfigure"):
        try:
            if (sys.stdout.encoding or "").lower() != "utf-8":
                sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass


class C:
    RST = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"
    B_GREEN = "\033[92m"
    B_YELLOW = "\033[93m"
    B_RED = "\033[91m"
    B_CYAN = "\033[96m"
    B_WHITE = "\033[97m"
    B_MAGENTA = "\033[95m"


_W = 49


class ColoredFormatter(logging.Formatter):
    LEVELS = {
        logging.DEBUG:    (C.GRAY,           "DEBUG"),
        logging.INFO:     (C.B_GREEN,        " INFO"),
        logging.WARNING:  (C.B_YELLOW,       " WARN"),
        logging.ERROR:    (C.B_RED,          "ERROR"),
        logging.CRITICAL: (C.B_RED + C.BOLD, "FATAL"),
    }

    def format(self, record):
        color, label = self.LEVELS.get(record.levelno, (C.RST, record.levelname[:5]))
        ts = self.formatTime(record, "%H:%M:%S")
        return (
            f"{C.DIM}{ts}{C.RST} "
            f"{color}{label}{C.RST} "
            f"{C.CYAN}{record.name}{C.RST}  "
            f"{record.getMessage()}"
        )


def setup_logging(level: str = "INFO"):
    _enable_ansi()
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(lvl)
    for h in root.handlers[:]:
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColoredFormatter())
    root.addHandler(handler)
    # quiet asyncio noise on disconnects
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def _kv(key, value, color=C.B_WHITE):
    print(f"  {C.DIM}{key:<14}{C.RST} {color}{value}{C.RST}")


def print_banner(
    *,
    name: str,
    host: str,
    port: int,
    soundfont: str,
    gain: float,
    driver: Optional[str],
    cache_dir: str,
    auto_install: bool,
    fluidsynth_dir: str,
):
    print()
    print(f"  {C.B_CYAN}{C.BOLD}midi_band{C.RST} {C.DIM}standalone client{C.RST}  {C.GRAY}\u2022{C.RST}  "
          f"{C.DIM}Project Gabriel{C.RST}")
    print(f"  {C.DIM}{'\u2500' * _W}{C.RST}")
    print()
    _kv("Bandmate", name, C.B_CYAN)
    _kv("Host", f"{host}:{port}", C.B_YELLOW)
    _kv("Soundfont", _short_path(soundfont), C.B_MAGENTA)
    _kv("Gain", f"{gain:.2f}", C.B_WHITE)
    _kv("Audio Driver", driver or "auto", C.B_WHITE)
    _kv("Cache", _short_path(cache_dir))
    print()

    components = [
        ("Auto-install fluidsynth", auto_install),
    ]
    parts = []
    for label, on in components:
        if on:
            parts.append(f"{C.B_GREEN}\u25cf{C.RST} {label}")
        else:
            parts.append(f"{C.DIM}\u25cb {label}{C.RST}")
    if parts:
        for i in range(0, len(parts), 2):
            print(f"  {'   '.join(parts[i:i + 2])}")
        print()
    if auto_install:
        print(f"  {C.DIM}fluidsynth dir: {_short_path(fluidsynth_dir)}{C.RST}")
        print()

    print(f"  {C.DIM}{'\u2500' * _W}{C.RST}")
    print()


def _short_path(p: str, max_len: int = 60) -> str:
    s = str(p)
    if len(s) <= max_len:
        return s
    return "..." + s[-(max_len - 3):]


def make_status_printer(get_status: Callable[[], dict], log: logging.Logger) -> Callable[[], None]:
    """Returns an on_change callback that diffs the previous status dict
    and emits a single colored line per meaningful transition. Idempotent
    if nothing changed."""
    state = {
        "connected": None,
        "song": None,
        "playing": False,
    }

    def cb():
        try:
            s = get_status() or {}
        except Exception:
            return
        connected = bool(s.get("connected"))
        song = s.get("song")
        playing = bool(s.get("playing"))
        tracks = s.get("tracks") or []

        if connected != state["connected"]:
            if connected:
                log.info(f"{C.B_GREEN}connected to host{C.RST}")
            else:
                log.warning(f"{C.B_YELLOW}disconnected, reconnecting...{C.RST}")
            state["connected"] = connected

        if song != state["song"]:
            if song:
                names = ", ".join(str(t) for t in tracks) if tracks else "(no tracks assigned)"
                log.info(f"{C.B_CYAN}prepare:{C.RST} {C.B_WHITE}{song}{C.RST}  "
                         f"{C.DIM}tracks:{C.RST} {names}")
            state["song"] = song

        if playing and not state["playing"]:
            log.info(f"{C.B_GREEN}\u25b6 playing{C.RST}")
        elif not playing and state["playing"]:
            log.info(f"{C.DIM}\u25a0 stopped{C.RST}")
        state["playing"] = playing

    return cb
