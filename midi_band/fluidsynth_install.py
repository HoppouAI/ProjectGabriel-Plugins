"""Auto-install / locate the fluidsynth native library.

On Windows we can grab a prebuilt zip from the official GitHub releases
and drop it in a local folder, then teach the DLL loader to look there
before pyfluidsynth tries to import.

On macOS / Linux there is no canonical generic binary (build vs distro
vs brew variants), so we fall back to a clear instruction message and
let the user run `brew install fluid-synth` / `apt install libfluidsynth3`
themselves.
"""
from __future__ import annotations

import ctypes.util
import io
import json
import logging
import os
import platform
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

GH_RELEASES_LATEST = "https://api.github.com/repos/FluidSynth/fluidsynth/releases/latest"

# Names ctypes.util.find_library tries on each platform, in priority order.
# pyfluidsynth itself probes a similar set, so if any of these resolve the
# bindings will be happy.
_LIB_NAMES_WINDOWS = ("libfluidsynth-3", "libfluidsynth-2", "libfluidsynth", "fluidsynth")
_LIB_NAMES_OTHER = ("fluidsynth", "libfluidsynth")


def _candidate_names() -> tuple:
    return _LIB_NAMES_WINDOWS if platform.system() == "Windows" else _LIB_NAMES_OTHER


def _has_native() -> bool:
    for name in _candidate_names():
        if ctypes.util.find_library(name):
            return True
    return False


def _add_dll_dir(p: Path):
    p_str = str(p)
    if platform.system() == "Windows":
        try:
            os.add_dll_directory(p_str)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass
    sep = os.pathsep
    cur = os.environ.get("PATH", "")
    if p_str and p_str not in cur.split(sep):
        os.environ["PATH"] = p_str + sep + cur


def _find_local_dll(install_dir: Path) -> Optional[Path]:
    if not install_dir.exists():
        return None
    for stem in ("libfluidsynth-3.dll", "libfluidsynth-2.dll", "fluidsynth.dll"):
        for hit in install_dir.rglob(stem):
            return hit.parent
    return None


def _select_windows_asset(assets: list) -> Optional[dict]:
    is_64bit = sys.maxsize > 2 ** 32
    arch_tag = "win10-x64" if is_64bit else "win10-x86"
    # primary match: zip with win10-<arch>
    for a in assets:
        name = (a.get("name") or "").lower()
        if name.endswith(".zip") and arch_tag in name and "-static" not in name:
            return a
    # fallback: any windows zip that mentions x64/x86
    short = "x64" if is_64bit else "x86"
    for a in assets:
        name = (a.get("name") or "").lower()
        if name.endswith(".zip") and ("win" in name or "windows" in name) and short in name:
            return a
    return None


def _download_windows(install_dir: Path) -> bool:
    try:
        req = urllib.request.Request(
            GH_RELEASES_LATEST,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "midi_band/0.1 (+ProjectGabriel)",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.error(f"midi_band: failed to query fluidsynth releases: {e}")
        return False

    asset = _select_windows_asset(data.get("assets") or [])
    if asset is None:
        logger.error("midi_band: no suitable fluidsynth windows asset found")
        return False

    url = asset.get("browser_download_url")
    name = asset.get("name") or "fluidsynth.zip"
    logger.info(f"midi_band: downloading {name} from {url}")
    try:
        with urllib.request.urlopen(url, timeout=180) as resp:
            blob = resp.read()
    except Exception as e:
        logger.error(f"midi_band: download failed: {e}")
        return False

    install_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            z.extractall(install_dir)
    except Exception as e:
        logger.error(f"midi_band: extract failed: {e}")
        return False
    logger.info(f"midi_band: fluidsynth extracted under {install_dir}")
    return True


def _appease_pyfluidsynth_hardcoded_path():
    """pyfluidsynth's __init__.py unconditionally calls
    os.add_dll_directory(r'C:\\tools\\fluidsynth\\bin') on Windows. If
    that exact directory does not exist the import raises FileNotFoundError
    before we get a chance to do anything. Make sure the path exists
    (empty is fine, our own _add_dll_dir handles the actual library
    location). No-op on non-Windows.
    """
    if platform.system() != "Windows":
        return
    try:
        Path(r"C:\tools\fluidsynth\bin").mkdir(parents=True, exist_ok=True)
    except Exception:
        # might be permission denied on locked-down boxes, not fatal,
        # the user can still install fluidsynth manually.
        pass


def ensure_fluidsynth(install_dir: Optional[Path], allow_download: bool = True) -> bool:
    """Make sure libfluidsynth is loadable.

    Order of attempts:
      1. Already on the system search path.
      2. Already extracted under install_dir from a prior run.
      3. (Windows + allow_download) download latest GitHub release, extract,
         then add the bin folder to the DLL search path.

    Returns True if the lib is reachable after this call. Safe to call
    repeatedly, becomes a no-op once it succeeds.
    """
    _appease_pyfluidsynth_hardcoded_path()

    if _has_native():
        return True

    if install_dir is not None:
        bin_dir = _find_local_dll(install_dir)
        if bin_dir is not None:
            _add_dll_dir(bin_dir)
            if _has_native():
                return True

    if platform.system() != "Windows":
        logger.warning(
            "midi_band: fluidsynth native library not found. install via your "
            "package manager: 'brew install fluid-synth' on macOS, "
            "'apt install libfluidsynth3' on Debian/Ubuntu, etc."
        )
        return False

    if not allow_download or install_dir is None:
        return False

    if not _download_windows(install_dir):
        return False

    bin_dir = _find_local_dll(install_dir)
    if bin_dir is None:
        logger.error(
            f"midi_band: download succeeded but no libfluidsynth dll found under {install_dir}"
        )
        return False
    _add_dll_dir(bin_dir)
    return _has_native()
