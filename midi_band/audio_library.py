"""Folder-based audio stem library for the band's Audio mode.

Each "song" is a folder under the library root holding up to a handful of
stem files (vocals, bass, drums, guitar...) plus a manifest.json that maps
on-disk files to display labels. Stems play back as the audio-mode answer
to MIDI tracks: each one is an assignable part.

Pure stdlib + soundfile for probing. mp3/m4a/aac get transcoded to wav on
upload via the ffmpeg that ships with imageio-ffmpeg, everything else
(wav/flac/ogg) is stored as-is.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# decoded natively by libsndfile, stored untouched
NATIVE_EXT = {".wav", ".flac", ".ogg", ".oga"}
# need a pass through ffmpeg to land as wav
TRANSCODE_EXT = {".mp3", ".m4a", ".aac"}
ALLOWED_EXT = NATIVE_EXT | TRANSCODE_EXT

MAX_STEMS = 12

# canonical names a few common variants collapse to
_LABEL_ALIASES = {
    "vocal": "vocals",
    "vox": "vocals",
    "voc": "vocals",
    "drum": "drums",
    "gtr": "guitar",
    "key": "keys",
    "inst": "instrumental",
    "perc": "percussion",
    "synths": "synth",
}
# tokens we look for when there's no (parenthetical) hint in the filename
_LABEL_KEYWORDS = [
    "vocals", "vocal", "vox", "bass", "drums", "drum", "guitar", "gtr",
    "piano", "keys", "synth", "strings", "brass", "organ", "other",
    "instrumental", "inst", "melody", "lead", "backing", "percussion",
    "perc", "fx",
]


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", str(text or "").strip().lower()).strip("_")
    return s or "stem"


def detect_label(filename: str) -> str:
    """Best-guess a stem's part name from its filename. UVR-style outputs
    put it in parens, e.g. 'Song_(vocals)_BS-Roformer.wav'. Falls back to a
    keyword scan, then the bare filename."""
    stem = Path(str(filename)).stem
    paren = re.findall(r"\(([^)]+)\)", stem)
    if paren:
        token = re.sub(r"[^A-Za-z0-9]+", " ", paren[-1]).strip().lower()
        token = token.split()[-1] if token else ""
        if token:
            return _LABEL_ALIASES.get(token, token)
    low = stem.lower()
    for kw in _LABEL_KEYWORDS:
        if re.search(rf"(^|[^a-z]){re.escape(kw)}([^a-z]|$)", low):
            return _LABEL_ALIASES.get(kw, kw)
    cleaned = re.sub(r"[^A-Za-z0-9]+", " ", stem).strip()
    return (cleaned or "stem")[:32]


def _ffmpeg_exe() -> Optional[str]:
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        logger.warning(f"midi_band: imageio-ffmpeg not available for transcode: {e}")
        return None


def _probe(path: Path) -> dict:
    try:
        import soundfile as sf  # type: ignore
        info = sf.info(str(path))
        return {
            "duration": float(info.frames) / float(info.samplerate or 1),
            "samplerate": int(info.samplerate or 0),
            "channels": int(info.channels or 0),
        }
    except Exception as e:
        logger.warning(f"midi_band: probe failed {path}: {e}")
        return {"duration": 0.0, "samplerate": 0, "channels": 0}


def _sha(path: Path) -> str:
    h = hashlib.sha1()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except Exception:
        return ""
    return h.hexdigest()[:16]


def _clean_song_name(name: str) -> str:
    raw = str(name or "").replace("\\", "/").split("/")[-1].strip()
    raw = re.sub(r"[\x00-\x1f<>:\"|?*]", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip(". ")
    return raw[:80].strip()


class AudioLibrary:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ----- paths / manifest -----

    def _song_dir(self, name: str) -> Optional[Path]:
        clean = _clean_song_name(name)
        if not clean:
            return None
        d = (self.root / clean).resolve()
        try:
            d.relative_to(self.root.resolve())
        except ValueError:
            return None
        return d

    def _manifest_path(self, song_dir: Path) -> Path:
        return song_dir / "manifest.json"

    def _read_manifest(self, song_dir: Path) -> dict:
        p = self._manifest_path(song_dir)
        try:
            if p.exists():
                data = json.loads(p.read_text("utf-8")) or {}
                if isinstance(data, dict):
                    data.setdefault("stems", [])
                    return data
        except Exception as e:
            logger.warning(f"midi_band: bad audio manifest {p}: {e}")
        return {"name": song_dir.name, "stems": []}

    def _write_manifest(self, song_dir: Path, data: dict):
        p = self._manifest_path(song_dir)
        try:
            song_dir.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), "utf-8")
            tmp.replace(p)
        except Exception as e:
            logger.warning(f"midi_band: could not write audio manifest {p}: {e}")

    def _duration(self, stems: List[dict]) -> float:
        return max([float(s.get("duration") or 0.0) for s in stems], default=0.0)

    # ----- queries -----

    def list_songs(self) -> List[dict]:
        out = []
        for d in sorted(self.root.iterdir() if self.root.exists() else []):
            if not d.is_dir():
                continue
            man = self._read_manifest(d)
            stems = man.get("stems", [])
            out.append({
                "name": d.name,
                "stem_count": len(stems),
                "duration": round(self._duration(stems), 2),
            })
        return out

    def song_info(self, name: str) -> Optional[dict]:
        d = self._song_dir(name)
        if d is None or not d.is_dir():
            return None
        man = self._read_manifest(d)
        stems = man.get("stems", [])
        return {
            "name": d.name,
            "stems": stems,
            "duration": round(self._duration(stems), 2),
        }

    def stem_path(self, name: str, index: int) -> Optional[Path]:
        d = self._song_dir(name)
        if d is None:
            return None
        man = self._read_manifest(d)
        for s in man.get("stems", []):
            if int(s.get("index", -1)) == int(index):
                p = (d / s.get("file", "")).resolve()
                try:
                    p.relative_to(d.resolve())
                except ValueError:
                    return None
                return p if p.is_file() else None
        return None

    # ----- mutations -----

    def create_song(self, name: str) -> dict:
        clean = _clean_song_name(name)
        if not clean:
            return {"result": "error", "message": "song name required"}
        d = self._song_dir(clean)
        if d is None:
            return {"result": "error", "message": "bad song name"}
        if d.exists():
            return {"result": "error", "message": f"'{clean}' already exists"}
        now = time.time()
        d.mkdir(parents=True, exist_ok=True)
        self._write_manifest(d, {"name": clean, "created": now, "updated": now, "stems": []})
        return {"result": "ok", "song": clean}

    def delete_song(self, name: str) -> dict:
        d = self._song_dir(name)
        if d is None or not d.is_dir():
            return {"result": "error", "message": "not found"}
        try:
            for child in d.iterdir():
                try:
                    child.unlink()
                except Exception:
                    pass
            d.rmdir()
        except Exception as e:
            return {"result": "error", "message": str(e)}
        return {"result": "ok", "deleted": d.name}

    def add_stem(self, name: str, filename: str, data: bytes) -> dict:
        """Drop one uploaded file into a song folder, transcoding to wav if
        needed, and register it in the manifest with an auto-detected label."""
        d = self._song_dir(name)
        if d is None:
            return {"result": "error", "message": "bad song name"}
        if not d.exists():
            res = self.create_song(name)
            if res.get("result") != "ok":
                return res
        man = self._read_manifest(d)
        stems = man.get("stems", [])
        if len(stems) >= MAX_STEMS:
            return {"result": "error", "message": f"a song holds at most {MAX_STEMS} stems"}
        ext = Path(str(filename)).suffix.lower()
        if ext not in ALLOWED_EXT:
            return {"result": "error", "message": f"unsupported format: {ext or 'unknown'}"}

        label = detect_label(filename)
        index = (max([int(s.get("index", -1)) for s in stems], default=-1)) + 1
        base = f"{index}_{_slug(label)}"

        try:
            if ext in TRANSCODE_EXT:
                final = d / f"{base}.wav"
                if not self._transcode_to_wav(data, ext, final):
                    return {"result": "error", "message": "transcode failed (ffmpeg missing?)"}
            else:
                final = d / f"{base}{ext}"
                final.write_bytes(data)
        except Exception as e:
            return {"result": "error", "message": str(e)}

        probe = _probe(final)
        stem = {
            "index": index,
            "label": label,
            "file": final.name,
            "sha": _sha(final),
            "size": final.stat().st_size if final.exists() else 0,
            "duration": round(probe["duration"], 3),
            "samplerate": probe["samplerate"],
            "channels": probe["channels"],
            "original": Path(str(filename)).name,
        }
        stems.append(stem)
        man["stems"] = stems
        man["updated"] = time.time()
        self._write_manifest(d, man)
        return {"result": "ok", "song": d.name, "stem": stem}

    def rename_stem(self, name: str, index: int, label: str) -> dict:
        d = self._song_dir(name)
        if d is None or not d.is_dir():
            return {"result": "error", "message": "not found"}
        clean_label = re.sub(r"\s+", " ", str(label or "").strip())[:40]
        if not clean_label:
            return {"result": "error", "message": "label required"}
        man = self._read_manifest(d)
        changed = False
        for s in man.get("stems", []):
            if int(s.get("index", -1)) == int(index):
                s["label"] = clean_label
                changed = True
                break
        if not changed:
            return {"result": "error", "message": "stem not found"}
        man["updated"] = time.time()
        self._write_manifest(d, man)
        return {"result": "ok", "song": d.name, "index": int(index), "label": clean_label}

    def delete_stem(self, name: str, index: int) -> dict:
        d = self._song_dir(name)
        if d is None or not d.is_dir():
            return {"result": "error", "message": "not found"}
        man = self._read_manifest(d)
        stems = man.get("stems", [])
        keep = []
        removed = None
        for s in stems:
            if int(s.get("index", -1)) == int(index):
                removed = s
            else:
                keep.append(s)
        if removed is None:
            return {"result": "error", "message": "stem not found"}
        try:
            (d / removed.get("file", "")).unlink()
        except Exception:
            pass
        man["stems"] = keep
        man["updated"] = time.time()
        self._write_manifest(d, man)
        return {"result": "ok", "song": d.name, "index": int(index)}

    def _transcode_to_wav(self, data: bytes, src_ext: str, dest: Path) -> bool:
        exe = _ffmpeg_exe()
        if not exe:
            return False
        tmp_in = dest.with_name(dest.stem + "_in" + src_ext)
        try:
            tmp_in.write_bytes(data)
            proc = subprocess.run(
                [exe, "-y", "-i", str(tmp_in), "-map_metadata", "-1", str(dest)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return proc.returncode == 0 and dest.exists()
        except Exception as e:
            logger.warning(f"midi_band: transcode error: {e}")
            return False
        finally:
            try:
                tmp_in.unlink()
            except Exception:
                pass
