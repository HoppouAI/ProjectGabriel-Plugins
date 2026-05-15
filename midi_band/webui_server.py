"""Tiny stdlib HTTP server for the midi_band library.

Serves three static files (index.html, style.css, app.js) and two JSON
endpoints: GET /api/songs and POST /api/upload (multipart). Runs in a
background thread so it doesnt block the asyncio band server. No extra
deps so it works the same in the host plugin and the standalone client.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

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
        self.send_error(404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/upload":
            return self._handle_upload()
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


class _Store:
    def __init__(self, library_dir: Path, instance_name: str):
        self.library_dir = library_dir
        self.instance_name = instance_name


class WebUiServer:
    def __init__(self, bind: str, port: int, library_dir: Path, instance_name: str):
        self.bind = bind or "0.0.0.0"
        self.port = int(port)
        self.library_dir = Path(library_dir)
        self.instance_name = instance_name or "gabriel"
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
        self._httpd._store = _Store(self.library_dir, self.instance_name)  # type: ignore[attr-defined]
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
