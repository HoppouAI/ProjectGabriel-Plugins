"""Tiny stdlib HTTP server for the midi_band library.

Serves static UI files and a small JSON control API: list/upload/delete
songs and control playback (load, play, stop, pause, resume, volume,
soundcheck, auto-assign). Runs in a background thread so it doesn't
block the asyncio band server. No extra deps.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Any
from urllib.parse import unquote

logger = logging.getLogger(__name__)

WEBUI_DIR = Path(__file__).parent / "webui"
ALLOWED_EXT = {".mid", ".midi"}
MAX_UPLOAD = 32 * 1024 * 1024  # 32 MiB cap per request


def _safe_name(name: str) -> Optional[str]:
    # strip path bits, keep base filename only
    name = name.replace("\\", "/").split("/")[-1].strip()
    if not name or name in (".", ".."):
        return None
    # block control chars and weird stuff
    if re.search(r"[\x00-\x1f<>:\"|?*]", name):
        return None
    if Path(name).suffix.lower() not in ALLOWED_EXT:
        return None
    return name


class _Handler(BaseHTTPRequestHandler):
    server_version = "midi_band-webui/1.0"

    # silence the default per-request stderr spam, route to logger instead
    def log_message(self, fmt, *args):
        logger.debug("midi_band webui: " + fmt, *args)

    def _json(self, code: int, body: dict):
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, rel: str):
        # rel is already validated to be one of the three known files
        path = WEBUI_DIR / rel
        try:
            data = path.read_bytes()
        except Exception:
            self.send_error(404)
            return
        ct = {
            "html": "text/html; charset=utf-8",
            "css": "text/css; charset=utf-8",
            "js": "application/javascript; charset=utf-8",
        }.get(path.suffix.lstrip(".").lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            return self._static("index.html")
        if path == "/style.css":
            return self._static("style.css")
        if path == "/app.js":
            return self._static("app.js")
        if path == "/api/songs":
            return self._handle_list()
        if path == "/api/status":
            return self._handle_status()
        if path == "/api/sync":
            return self._handle_sync()
        self.send_error(404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/upload":
            return self._handle_upload()
        if path == "/api/load":
            return self._handle_load()
        if path == "/api/auto_assign":
            return self._call_async("auto_assign_sync")
        if path == "/api/play":
            return self._call_async("start_playback")
        if path == "/api/stop":
            return self._call_async("stop_playback")
        if path == "/api/pause":
            return self._call_async("pause_playback")
        if path == "/api/resume":
            return self._call_async("resume_playback")
        if path == "/api/volume":
            return self._handle_volume()
        if path == "/api/soundcheck":
            return self._handle_soundcheck()
        self.send_error(404)

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        m = re.match(r"^/api/songs/(.+)$", path)
        if m:
            return self._handle_delete(unquote(m.group(1)))
        self.send_error(404)

    def _handle_list(self):
        store = getattr(self.server, "_store", None)
        if store is None:
            return self._json(500, {"result": "error", "message": "no library"})
        try:
            entries = []
            for p in sorted(store.library_dir.glob("*.mid")):
                try:
                    entries.append({"name": p.name, "size": p.stat().st_size})
                except Exception:
                    continue
            for p in sorted(store.library_dir.glob("*.midi")):
                try:
                    entries.append({"name": p.name, "size": p.stat().st_size})
                except Exception:
                    continue
            return self._json(200, {
                "result": "ok",
                "instance": store.instance_name,
                "library": str(store.library_dir),
                "songs": entries,
            })
        except Exception as e:
            return self._json(500, {"result": "error", "message": str(e)})

    def _handle_upload(self):
        store = getattr(self.server, "_store", None)
        if store is None:
            return self._json(500, {"result": "error", "message": "no library"})
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r"boundary=([^\s;]+)", ctype)
        if not m:
            return self._json(400, {"result": "error", "message": "no multipart boundary"})
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except Exception:
            length = 0
        if length <= 0 or length > MAX_UPLOAD:
            return self._json(413, {"result": "error", "message": "upload too large or empty"})
        body = self.rfile.read(length)
        boundary = ("--" + m.group(1)).encode("utf-8")
        # split by boundary, drop empty first chunk and trailing -- chunk
        parts = body.split(boundary)
        saved_name = None
        saved_size = 0
        for part in parts:
            part = part.strip(b"\r\n")
            if not part or part == b"--":
                continue
            try:
                head, _, content = part.partition(b"\r\n\r\n")
            except Exception:
                continue
            headers = head.decode("utf-8", "replace")
            disp_match = re.search(
                r'filename="([^"]+)"', headers, re.IGNORECASE
            )
            if not disp_match:
                continue
            raw_name = disp_match.group(1)
            name = _safe_name(raw_name)
            if not name:
                return self._json(400, {
                    "result": "error",
                    "message": f"rejected filename: {raw_name!r}",
                })
            # trim trailing CRLF that always sits before the next boundary
            data = content[:-2] if content.endswith(b"\r\n") else content
            try:
                store.library_dir.mkdir(parents=True, exist_ok=True)
                target = store.library_dir / name
                target.write_bytes(data)
                saved_name = name
                saved_size = len(data)
                logger.info(f"midi_band webui: saved {name} ({saved_size} bytes)")
            except Exception as e:
                return self._json(500, {"result": "error", "message": str(e)})
            break  # one file per request, frontend posts them one-by-one
        if not saved_name:
            return self._json(400, {"result": "error", "message": "no file in request"})
        return self._json(200, {
            "result": "ok", "name": saved_name, "size": saved_size,
        })

    def _handle_delete(self, name: str):
        store = getattr(self.server, "_store", None)
        if store is None:
            return self._json(500, {"result": "error", "message": "no library"})
        safe = _safe_name(name)
        if not safe:
            return self._json(400, {"result": "error", "message": "bad filename"})
        target = store.library_dir / safe
        if not target.exists():
            return self._json(404, {"result": "error", "message": "not found"})
        try:
            target.unlink()
        except Exception as e:
            return self._json(500, {"result": "error", "message": str(e)})
        logger.info(f"midi_band webui: deleted {safe}")
        return self._json(200, {"result": "ok", "deleted": safe})

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except Exception:
            length = 0
        if length <= 0:
            return {}
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8")) or {}
        except Exception:
            return {}

    def _store(self):
        return getattr(self.server, "_store", None)

    def _server_obj(self):
        store = self._store()
        if store is None:
            return None
        return getattr(store, "band_server", None)

    def _handle_status(self):
        store = self._store()
        if store is None:
            return self._json(500, {"result": "error", "message": "no library"})
        srv = self._server_obj()
        out = {
            "result": "ok",
            "instance": store.instance_name,
            "role": "host" if srv is not None else "client",
        }
        if srv is None:
            return self._json(200, out)
        try:
            info = srv.loaded_info()
            ps = srv.player.status()
            out.update({
                "song": ps.get("song") or info.get("song"),
                "tracks": info.get("tracks"),
                "host_tracks": info.get("host_tracks"),
                "assignments": info.get("assignments"),
                "duration": ps.get("duration") or info.get("duration"),
                "position": ps.get("position"),
                "playing": ps.get("playing"),
                "paused": ps.get("paused"),
                "gain": ps.get("gain"),
                "members": [srv.instance_name] + srv.list_clients(),
            })
        except Exception as e:
            out["error"] = str(e)
        return self._json(200, out)

    def _handle_sync(self):
        store = self._store()
        if store is None:
            return self._json(500, {"result": "error", "message": "no library"})
        srv = self._server_obj()
        if srv is None:
            return self._json(200, {
                "result": "ok",
                "role": "client",
                "instance": store.instance_name,
                "message": "sync stats are only visible from the band host",
            })
        try:
            data = srv.get_sync_status()
        except Exception as e:
            return self._json(500, {"result": "error", "message": str(e)})
        data["result"] = "ok"
        data["role"] = "host"
        return self._json(200, data)

    def _handle_load(self):
        srv = self._server_obj()
        if srv is None:
            return self._json(400, {"result": "error", "message": "this instance is not a band host"})
        body = self._read_json()
        title = str(body.get("title") or "").strip()
        if not title:
            return self._json(400, {"result": "error", "message": "title required"})
        try:
            res = srv.load_song(title)
        except Exception as e:
            return self._json(500, {"result": "error", "message": str(e)})
        return self._json(200, res)

    def _handle_volume(self):
        srv = self._server_obj()
        if srv is None:
            return self._json(400, {"result": "error", "message": "host only"})
        body = self._read_json()
        try:
            level = float(body.get("level"))
        except Exception:
            return self._json(400, {"result": "error", "message": "level must be a number"})
        return self._call_async("set_volume", level)

    def _handle_soundcheck(self):
        srv = self._server_obj()
        if srv is None:
            return self._json(400, {"result": "error", "message": "host only"})
        body = self._read_json()
        try:
            dur = float(body.get("duration") or 10.0)
        except Exception:
            dur = 10.0
        try:
            bpm = float(body.get("bpm") or 120.0)
        except Exception:
            bpm = 120.0
        return self._call_async("soundcheck", dur, bpm)

    def _call_async(self, method_name: str, *args: Any):
        """Invoke an async method on the band server from this thread by
        bouncing the coroutine onto the server's asyncio loop."""
        srv = self._server_obj()
        if srv is None:
            return self._json(400, {"result": "error", "message": "host only"})
        # auto_assign is sync, special-case it
        if method_name == "auto_assign_sync":
            try:
                return self._json(200, srv.auto_assign())
            except Exception as e:
                return self._json(500, {"result": "error", "message": str(e)})
        method = getattr(srv, method_name, None)
        if method is None:
            return self._json(500, {"result": "error", "message": f"unknown method {method_name}"})
        loop = getattr(srv, "_loop", None)
        if loop is None or not loop.is_running():
            return self._json(503, {"result": "error", "message": "band server not running"})
        try:
            fut = asyncio.run_coroutine_threadsafe(method(*args), loop)
            res = fut.result(timeout=10.0)
        except Exception as e:
            return self._json(500, {"result": "error", "message": str(e)})
        return self._json(200, res or {"result": "ok"})


class _Store:
    def __init__(self, library_dir: Path, instance_name: str, band_server=None):
        self.library_dir = library_dir
        self.instance_name = instance_name
        self.band_server = band_server


class WebUiServer:
    def __init__(self, bind: str, port: int, library_dir: Path, instance_name: str,
                 band_server=None):
        self.bind = bind or "0.0.0.0"
        self.port = int(port)
        self.library_dir = Path(library_dir)
        self.instance_name = instance_name or "gabriel"
        self.band_server = band_server
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        if self._httpd is not None:
            return True
        try:
            self._httpd = ThreadingHTTPServer((self.bind, self.port), _Handler)
        except Exception as e:
            logger.error(f"midi_band webui: bind {self.bind}:{self.port} failed: {e}")
            self._httpd = None
            return False
        self._httpd._store = _Store(  # type: ignore[attr-defined]
            self.library_dir, self.instance_name, self.band_server,
        )
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="midi_band-webui", daemon=True)
        self._thread.start()
        logger.info(f"midi_band: webui on http://{self.bind}:{self.port}")
        return True

    def stop(self):
        if self._httpd is None:
            return
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        try:
            self._httpd.server_close()
        except Exception:
            pass
        self._httpd = None
        self._thread = None
