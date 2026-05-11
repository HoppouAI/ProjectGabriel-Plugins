"""Library helpers. A duo song is a PAIR of audio files, one part per
singer. We detect pairs by looking for filenames ending in PT1 / PT2
(case insensitive, optional separator). Examples that pair up:

    SongName PT1.mp3 + SongName PT2.mp3
    song_name_pt1.ogg + song_name_pt2.ogg
    song-name-Pt1.flac + song-name-Pt2.flac

Files without a PT1/PT2 marker are ignored. The library lives at
sfx/music/duo/ by default (relative to the host's working directory).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

AUDIO_EXTS = {".mp3", ".ogg", ".wav", ".flac", ".m4a", ".opus"}

# matches a trailing "PT1" / "PT2" with optional separator before it.
# capture group 1 = the base name, group 2 = '1' or '2'.
_PART_RE = re.compile(r"^(.*?)[\s_\-\.]*pt\s*([12])$", re.IGNORECASE)


def _parse_part(stem: str) -> Optional[Tuple[str, int]]:
    m = _PART_RE.match(stem.strip())
    if not m:
        return None
    base = m.group(1).strip(" _-.")
    part = int(m.group(2))
    return (base, part)


class _Slot(dict):
    """dict subclass so we can attach a non-int 'display' key without typing pain."""


def list_pairs(library: Path) -> Dict[str, _Slot]:
    """Walk the library and group files by their base name. Returns a dict
    keyed by lowercased base, value has int keys 1/2 mapping to Path plus
    a 'display' key for the original casing."""
    out: Dict[str, _Slot] = {}
    if not library.exists():
        return out
    for p in sorted(library.iterdir()):
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
            continue
        parsed = _parse_part(p.stem)
        if parsed is None:
            continue
        base, part = parsed
        key = base.lower()
        slot = out.get(key)
        if slot is None:
            slot = _Slot()
            slot["display"] = base
            out[key] = slot
        slot[part] = p
    return out


def list_songs(library: Path) -> List[Dict[str, object]]:
    """Human-friendly listing for the AI / status. Marks pairs vs lonely
    halves so the AI knows which ones are actually playable."""
    pairs = list_pairs(library)
    out: List[Dict[str, object]] = []
    for key in sorted(pairs.keys()):
        slot = pairs[key]
        out.append({
            "title": str(slot.get("display") or key),
            "complete": (1 in slot) and (2 in slot),
            "have_pt1": 1 in slot,
            "have_pt2": 2 in slot,
        })
    return out


def find_pair(library: Path, query: str) -> Optional[Dict[str, object]]:
    """Substring match against base names. Returns {title, pt1, pt2} or None.
    Either part may be None if its not on disk yet."""
    if not query:
        return None
    q = query.strip().lower()
    pairs = list_pairs(library)
    if not pairs:
        return None
    chosen_key: Optional[str] = q if q in pairs else None
    if chosen_key is None:
        for key in sorted(pairs.keys()):
            if q in key:
                chosen_key = key
                break
    if chosen_key is None:
        return None
    slot = pairs[chosen_key]
    return {
        "title": str(slot.get("display") or chosen_key),
        "pt1": slot.get(1),
        "pt2": slot.get(2),
    }
