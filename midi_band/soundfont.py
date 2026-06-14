"""Read the preset table out of a .sf2 soundfont so the band UI can offer
the real instruments a soundfont ships with (variation banks, drum kits,
whatever custom stuff the author baked in) instead of just the 128 GM
names.

Pure stdlib. Seeks straight to the pdta LIST so we never read the giant
sample chunk, a 300MB+ soundfont still parses in a blink. No Project
Gabriel imports so the standalone client can pull this in too.
"""
from __future__ import annotations

import logging
import struct
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

# each phdr record is a fixed 38 bytes:
#   char achPresetName[20]
#   uint16 wPreset (program), uint16 wBank
#   uint16 wPresetBagNdx
#   uint32 dwLibrary, dwGenre, dwMorphology
_PHDR_REC = 38


def read_presets(path) -> List[dict]:
    """Return [{bank, program, name}, ...] sorted by (bank, program).
    Empty list on any trouble, callers fall back to plain GM."""
    presets: List[dict] = []
    try:
        p = Path(path)
        with p.open("rb") as f:
            if f.read(4) != b"RIFF":
                return presets
            f.read(4)  # riff size, dont care
            if f.read(4) != b"sfbk":
                return presets
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                cid = hdr[:4]
                csize = struct.unpack("<I", hdr[4:8])[0]
                data_start = f.tell()
                nxt = data_start + csize + (csize & 1)
                if cid == b"LIST" and f.read(4) == b"pdta":
                    sub_end = data_start + csize
                    while f.tell() + 8 <= sub_end:
                        sh = f.read(8)
                        if len(sh) < 8:
                            break
                        sid = sh[:4]
                        ssize = struct.unpack("<I", sh[4:8])[0]
                        sstart = f.tell()
                        if sid == b"phdr":
                            raw = f.read(ssize)
                            for i in range(ssize // _PHDR_REC):
                                rec = raw[i * _PHDR_REC:i * _PHDR_REC + _PHDR_REC]
                                name = rec[:20].split(b"\x00", 1)[0]
                                name = name.decode("latin-1", "replace").strip()
                                prog, bank = struct.unpack("<HH", rec[20:24])
                                if not name or name.upper() == "EOP":
                                    continue
                                presets.append({
                                    "bank": int(bank),
                                    "program": int(prog),
                                    "name": name,
                                })
                            break  # got the table, stop scanning pdta
                        f.seek(sstart + ssize + (ssize & 1))
                    break  # pdta handled, done
                f.seek(nxt)
    except Exception as e:
        logger.warning(f"midi_band: could not read soundfont presets: {e}")
        return []
    # dedupe (some soundfonts list the same bank:prog twice) and sort
    seen = set()
    out = []
    for pr in sorted(presets, key=lambda x: (x["bank"], x["program"])):
        key = (pr["bank"], pr["program"])
        if key in seen:
            continue
        seen.add(key)
        out.append(pr)
    return out
