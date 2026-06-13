"""Standalone omnivoice_tts WebSocket server.

Runs the OmniVoice diffusion TTS on this machine and serves audio over a
WebSocket so a remote Project Gabriel instance can use it as a TTS
provider without having to install torch + omnivoice locally.

Run with:

    uv sync
    uv run server.py --host 0.0.0.0 --port 8788

Or just:

    uv run server.py

if you've copied config.example.yml to config.yml and filled in your
voice config there.

The server is a thin FastAPI app with one route, /tts, that hands the
connection off to a per-session OmniVoiceProvider. Multiple concurrent
clients share the same loaded model via the engine's process-wide warm
cache, so the second client onward starts instantly.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

import yaml

# this file lives in omnivoice_tts/standalone/. add this folder itself
# to sys.path so `from engine import OmniVoiceProvider` works whether we
# were started via `uv run server.py` or `python server.py`.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from engine import OmniVoiceProvider, _autodetect_device  # noqa: E402
import protocol as P  # noqa: E402


logger = logging.getLogger("omnivoice_tts.server")


# ── fake config adapter ──────────────────────────────────────────────
# OmniVoiceProvider takes a config object that quacks like the host
# config: `config.get(*path, default=...)`. Build one from a plain dict.


class FakeConfig:
    """Stand-in for the host config object the engine expects.

    Backed by a plain dict structured like
        {"plugins": {"omnivoice_tts": {...}}, "audio": {"receive_sample_rate": N}}
    so the engine's `config.get("plugins", "omnivoice_tts", key, default=...)`
    calls Just Work.
    """

    def __init__(self, data: dict):
        self._data = data

    def get(self, *keys, default=None):
        cur = self._data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur if cur is not None else default


def _build_provider(server_config: dict, overrides_from_client: dict | None = None) -> OmniVoiceProvider:
    """Glue: turn (server config + optional per-connection overrides) into
    a fully constructed OmniVoiceProvider ready to .start()."""
    # server_config holds all the OmniVoice knobs at top level (model,
    # device, voice clone, etc). Merge in any client overrides on top.
    cfg = dict(server_config)
    if overrides_from_client:
        for k, v in overrides_from_client.items():
            if v is not None:
                cfg[k] = v

    sr = int(cfg.pop("output_sample_rate", None) or 24000)
    data_dir = Path(cfg.pop("data_dir", None) or str(_HERE / "data"))

    # strip None values so the providers cfg() doesnt see explicit nulls
    # from the yaml (eg ref_audio: null) as real values
    cfg = {k: v for k, v in cfg.items() if v is not None}

    host_cfg = {
        "plugins": {"omnivoice_tts": cfg},
        "audio": {"receive_sample_rate": sr},
    }
    return OmniVoiceProvider(FakeConfig(host_cfg), data_dir=data_dir)


# ── WS session glue ─────────────────────────────────────────────────


class WSSession:
    """One WebSocket connection. Owns its own OmniVoiceProvider, runs the
    audio pump in the background, dispatches control messages from the
    client."""

    def __init__(self, ws, server_config: dict, *, allow_overrides: bool):
        self.ws = ws
        self.server_config = server_config
        self.allow_overrides = allow_overrides
        self.provider: OmniVoiceProvider | None = None
        self.turn_id = 0
        self.audio_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._sent_audio_this_turn = False
        self._turn_in_flight = False
        # messages received during the handshake that we couldnt
        # process pre-start (eg an early feed_text). drained after the
        # provider is built.
        self._deferred: list[dict] = []

    async def run(self):
        # send a hello as soon as we accept the WS so the client can
        # confirm protocol + server-side params before doing anything.
        await self._send_hello_minimal()

        # collect any pre-start messages: config overrides, ref_audio
        # upload, etc. the client gets a short window to send these
        # before we lock in and build the provider.
        client_overrides = await self._handshake()

        try:
            self.provider = await asyncio.get_running_loop().run_in_executor(
                None, _build_provider, self.server_config, client_overrides,
            )
        except Exception as e:
            await self._send_json({"type": P.TYPE_ERROR, "message": f"failed to build provider: {e}"})
            return

        # bind the provider to this event loop, then start it.
        # provider.start() spawns background threads, returns instantly.
        self.provider._loop = asyncio.get_running_loop()
        try:
            self.provider.start()
        except Exception as e:
            await self._send_json({"type": P.TYPE_ERROR, "message": f"failed to start provider: {e}"})
            return

        # send a richer hello now that we know the real sample rate
        await self._send_json({
            "type": P.TYPE_HELLO,
            "protocol": P.PROTOCOL_VERSION,
            "sample_rate": int(self.provider._target_sr),
            "channels": 1,
            "dtype": "int16",
            "model": str(self.provider._model_path),
            "device": str(self.provider._device),
            "voice": self.provider._describe_voice(),
        })

        # poll model readiness in the background, fire TYPE_READY once
        # the warmup finishes (or warn the client if it crashed).
        asyncio.create_task(self._readiness_watch())

        # start pumping audio chunks from the provider's asyncio.Queue
        # over the WS as binary frames.
        self.audio_task = asyncio.create_task(self._audio_pump())

        # flush anything the client sent during the handshake that we
        # couldnt handle without the provider yet
        for msg in self._deferred:
            await self._handle_control(msg)
        self._deferred.clear()

        # main read loop
        try:
            while not self._stop_event.is_set():
                msg = await self.ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if "text" in msg and msg["text"] is not None:
                    try:
                        data = json.loads(msg["text"])
                    except json.JSONDecodeError:
                        await self._send_json({"type": P.TYPE_ERROR, "message": "bad json"})
                        continue
                    await self._handle_control(data)
                elif "bytes" in msg and msg["bytes"] is not None:
                    # we don't accept inbound audio mid-session, the
                    # client only sends text and uploads happen during
                    # the handshake
                    await self._send_json({"type": P.TYPE_ERROR, "message": "binary frames not accepted after handshake"})
        except Exception as e:
            logger.warning("ws read loop crashed: %s", e)
        finally:
            await self.close()

    async def _send_hello_minimal(self):
        # before the provider exists we can still tell the client the
        # protocol version + that we're alive
        await self._send_json({
            "type": P.TYPE_HELLO,
            "protocol": P.PROTOCOL_VERSION,
            "ready": False,
            "model": str(self.server_config.get("model", "k2-fsa/OmniVoice")),
        })

    async def _handshake(self) -> dict:
        """Pre-start message pump. Reads a short burst of pre-start
        messages (config overrides, ref_audio upload) and merges them
        into a single overrides dict. Stops as soon as anything
        non-prestart comes in (which gets deferred to after provider
        start) or the client falls silent for a beat.
        """
        overrides: dict[str, Any] = {}
        # first frame: wait up to 1s. the plugin always sends something
        # right after connect so this only really fires when a manual
        # client (curl test, etc) connects.
        first_timeout = 1.0
        next_timeout = 0.1

        while True:
            try:
                msg = await asyncio.wait_for(self.ws.receive(), timeout=first_timeout)
            except asyncio.TimeoutError:
                break
            first_timeout = next_timeout

            if msg.get("type") == "websocket.disconnect":
                raise RuntimeError("client disconnected during handshake")

            if "text" in msg and msg["text"] is not None:
                try:
                    data = json.loads(msg["text"])
                except json.JSONDecodeError:
                    await self._send_json({"type": P.TYPE_ERROR, "message": "bad json"})
                    continue

                t = data.get("type")
                if t == P.TYPE_CONFIG:
                    if not self.allow_overrides:
                        logger.info("ws: dropping client overrides (--no-overrides)")
                        continue
                    extra = data.get("overrides") or {}
                    if isinstance(extra, dict):
                        overrides.update(extra)
                    continue
                if t == P.TYPE_REF_AUDIO_UPLOAD:
                    if not self.allow_overrides:
                        await self._send_json({
                            "type": P.TYPE_ERROR,
                            "message": "ref_audio upload rejected, server has --no-overrides",
                        })
                        # drain the upcoming binary frame so we dont
                        # leave it sitting in the receive queue
                        await self._drain_pending_binary()
                        continue
                    saved = await self._receive_ref_audio_upload(data)
                    if saved is not None:
                        overrides["ref_audio"] = str(saved)
                        rt = data.get("ref_text")
                        if rt:
                            overrides["ref_text"] = str(rt)
                    continue
                if t == P.TYPE_PING:
                    await self._send_json({"type": P.TYPE_PONG})
                    continue
                # something we cant handle pre-start. stash for after the
                # provider is up.
                self._deferred.append(data)
                break
            elif "bytes" in msg and msg["bytes"] is not None:
                # stray binary frame outside of an upload. just drop it.
                logger.info("ws: dropping stray binary frame during handshake (%d bytes)", len(msg["bytes"]))
                continue

        return overrides

    async def _receive_ref_audio_upload(self, header: dict) -> Path | None:
        """Handle a ref_audio_upload control message + the binary frame
        that follows it. Saves to data_dir/uploads/<hash>.<ext> so the
        OmniVoiceProvider voice cache can key on a stable filename.
        Returns the saved path or None on failure.
        """
        size = int(header.get("size_bytes") or 0)
        filename = str(header.get("filename") or "ref_audio.wav")
        if size <= 0 or size > P.MAX_REF_AUDIO_BYTES:
            await self._send_json({
                "type": P.TYPE_ERROR,
                "message": f"ref_audio upload size {size} out of range (0, {P.MAX_REF_AUDIO_BYTES}]",
            })
            await self._drain_pending_binary()
            return None

        # receive exactly one binary frame
        try:
            msg = await asyncio.wait_for(self.ws.receive(), timeout=15.0)
        except asyncio.TimeoutError:
            await self._send_json({
                "type": P.TYPE_ERROR,
                "message": "timed out waiting for ref_audio binary frame",
            })
            return None

        if msg.get("type") == "websocket.disconnect":
            raise RuntimeError("client disconnected mid-upload")
        if "bytes" not in msg or msg["bytes"] is None:
            await self._send_json({
                "type": P.TYPE_ERROR,
                "message": "expected binary frame for ref_audio, got text",
            })
            return None

        blob = bytes(msg["bytes"])
        if len(blob) != size:
            logger.warning("ref_audio size mismatch: header=%d actual=%d", size, len(blob))

        import hashlib
        digest = hashlib.sha256(blob).hexdigest()[:16]
        # honor the client-supplied extension when sensible so the cache
        # hits across reconnects and torchaudio picks the right backend.
        ext = Path(filename).suffix.lower()
        if ext not in (".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aac", ".opus"):
            ext = ".wav"

        uploads_dir = _HERE / "data" / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        saved_path = uploads_dir / f"{digest}{ext}"

        cached = saved_path.exists() and saved_path.stat().st_size == len(blob)
        if not cached:
            saved_path.write_bytes(blob)
            logger.info("ref_audio: saved %s (%d bytes)", saved_path.name, len(blob))
        else:
            logger.info("ref_audio: cache hit on %s, reusing on disk", saved_path.name)

        await self._send_json({
            "type": P.TYPE_REF_AUDIO_ACK,
            "saved_as": str(saved_path),
            "cached": cached,
            "size_bytes": len(blob),
            "sha256": digest,
        })
        return saved_path

    async def _drain_pending_binary(self):
        """If we rejected an upload header, the client may have already
        queued the binary frame. Pull it off and discard so it doesnt
        confuse the main loop."""
        try:
            msg = await asyncio.wait_for(self.ws.receive(), timeout=2.0)
        except asyncio.TimeoutError:
            return
        if msg.get("type") == "websocket.disconnect":
            return
        # if it's a text frame defer it instead of dropping
        if "text" in msg and msg["text"] is not None:
            try:
                self._deferred.append(json.loads(msg["text"]))
            except Exception:
                pass

    async def _readiness_watch(self):
        loop = asyncio.get_running_loop()
        # provider._ready is a threading.Event, poll it from the loop
        while not self._stop_event.is_set():
            if self.provider._ready.is_set():
                if self.provider._model is None:
                    await self._send_json({
                        "type": P.TYPE_ERROR,
                        "message": f"model failed to load: {self.provider._load_error or 'unknown error'}",
                    })
                else:
                    await self._send_json({"type": P.TYPE_READY})
                return
            await asyncio.sleep(0.1)

    async def _handle_control(self, data: dict):
        t = data.get("type")
        if t == P.TYPE_FEED_TEXT:
            text = str(data.get("text") or "")
            if not text:
                return
            if not self._turn_in_flight:
                self.turn_id += 1
                self._turn_in_flight = True
                self._sent_audio_this_turn = False
                await self._send_json({"type": P.TYPE_AUDIO_START, "turn_id": self.turn_id})
            self.provider.feed_text(text)
        elif t == P.TYPE_TURN_COMPLETE:
            self.provider.turn_complete()
        elif t == P.TYPE_INTERRUPT:
            self.provider.interrupt()
            current_turn = self.turn_id
            self._turn_in_flight = False
            await self._send_json({"type": P.TYPE_INTERRUPTED, "turn_id": current_turn})
        elif t == P.TYPE_PING:
            await self._send_json({"type": P.TYPE_PONG})
        elif t == P.TYPE_CONFIG:
            # mid-session config swap not supported. log it so we know.
            logger.info("ws: ignoring mid-session config message")
        else:
            logger.warning("ws: unknown control type %r", t)

    async def _audio_pump(self):
        """Drain provider._audio_queue and ship every chunk to the client
        as a binary frame. Also fires audio_end when a turn falls quiet."""
        silence_count = 0
        try:
            while not self._stop_event.is_set():
                chunk = await self.provider.get_audio()
                if chunk is None:
                    # provider yields None on a 100ms timeout when no
                    # audio is queued. use that as the "turn done" signal:
                    # if we sent audio earlier this turn and then went
                    # quiet for ~600ms, fire audio_end and reset.
                    silence_count += 1
                    if self._turn_in_flight and self._sent_audio_this_turn and silence_count >= 6:
                        await self._send_json({"type": P.TYPE_AUDIO_END, "turn_id": self.turn_id})
                        self._turn_in_flight = False
                        self._sent_audio_this_turn = False
                        silence_count = 0
                    continue
                silence_count = 0
                if not self._turn_in_flight:
                    # rare: audio drained from a turn we already closed
                    self.turn_id += 1
                    self._turn_in_flight = True
                    await self._send_json({"type": P.TYPE_AUDIO_START, "turn_id": self.turn_id})
                self._sent_audio_this_turn = True
                await self.ws.send_bytes(chunk)
        except Exception as e:
            logger.warning("audio pump crashed: %s", e)

    async def _send_json(self, data: dict):
        try:
            await self.ws.send_text(json.dumps(data))
        except Exception as e:
            logger.debug("ws send_json failed: %s", e)

    async def close(self):
        self._stop_event.set()
        if self.audio_task is not None:
            self.audio_task.cancel()
        if self.provider is not None:
            try:
                self.provider.stop()
            except Exception:
                pass
        try:
            await self.ws.close()
        except Exception:
            pass


# ── FastAPI app ─────────────────────────────────────────────────────


def make_app(server_config: dict, *, allow_overrides: bool):
    # fastapi import is lazy so the module is cheap to import (eg from
    # tests). Starlettes raw WebSocketRoute is used instead of the
    # @app.websocket() decorator because fastapis decorator runs the
    # endpoint signature through its DI machinery, and under
    # `from __future__ import annotations` the `ws: WebSocket` hint is
    # a plain string the DI cant resolve (the `WebSocket` symbol lives
    # in this functions locals, not module globals, so get_type_hints
    # gives up and treats `ws` as a missing query param, which makes
    # fastapi close the socket with 1008 before we ever accept it,
    # which uvicorn logs as a generic 403). Going through starlette
    # directly skips all of that.
    from fastapi import FastAPI
    from starlette.routing import WebSocketRoute

    app = FastAPI(title="omnivoice_tts server", version="0.1.0")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "model": server_config.get("model")}

    async def tts_socket(websocket):
        await websocket.accept()
        session = WSSession(websocket, server_config, allow_overrides=allow_overrides)
        try:
            await session.run()
        except Exception:
            logger.exception("ws session crashed")
            try:
                await websocket.close(code=1011)
            except Exception:
                pass

    app.router.routes.append(WebSocketRoute(P.WS_PATH, tts_socket))

    return app


# ── config loading ──────────────────────────────────────────────────


DEFAULT_CONFIG_PATH = _HERE / "config.yml"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("failed to read %s: %s", path, e)
        return {}


def _resolve_server_config(args) -> tuple[dict, str, int, bool]:
    file_cfg = _load_yaml(Path(args.config))
    server = dict(file_cfg.get("omnivoice_tts") or {})
    net = file_cfg.get("server") or {}

    # cli overrides
    host = args.host or net.get("host") or P.DEFAULT_HOST
    port = int(args.port or net.get("port") or P.DEFAULT_PORT)
    allow_overrides = (
        args.allow_overrides
        if args.allow_overrides is not None
        else bool(net.get("allow_overrides", True))
    )

    # cli model overrides
    if args.model:
        server["model"] = args.model
    if args.device:
        server["device"] = args.device
    if args.ref_audio:
        server["ref_audio"] = args.ref_audio
    if args.instruct:
        server["instruct"] = args.instruct
    if args.output_sample_rate:
        server["output_sample_rate"] = args.output_sample_rate

    # sensible defaults so an empty config still boots. fill in for both
    # missing keys AND keys explicitly set to null in yaml.
    _defaults = {
        "model": "k2-fsa/OmniVoice",
        "device": _autodetect_device(),
        "dtype": "float16",
        "output_sample_rate": 24000,
        "data_dir": str(_HERE / "data"),
    }
    for k, v in _defaults.items():
        if server.get(k) in (None, ""):
            server[k] = v

    return server, host, port, allow_overrides


# ── warmup ──────────────────────────────────────────────────────────


def _kick_off_warmup(server_config: dict) -> None:
    """Pre-load the model + voice clone into OmniVoiceProvider's
    process-wide cache so the first WS client doesnt eat the ~5s
    diffusion load. Runs in a daemon thread, the WS server is free to
    accept connections while it warms (WSSession._readiness_watch
    handles the race)."""
    data_dir = Path(server_config.get("data_dir") or str(_HERE / "data"))
    warm_kwargs = dict(
        model_path=str(server_config.get("model") or "k2-fsa/OmniVoice"),
        device=str(server_config.get("device") or _autodetect_device()),
        dtype_name=str(server_config.get("dtype") or "float16"),
        ref_audio=server_config.get("ref_audio") or None,
        ref_text=server_config.get("ref_text") or None,
        instruct=server_config.get("instruct") or None,
        language=server_config.get("language") or None,
        asr_model=str(server_config.get("asr_model") or "openai/whisper-base"),
        use_flash_attn=bool(server_config.get("use_flash_attn", False)),
        use_cuda_graphs=bool(server_config.get("use_cuda_graphs", False)),
        max_graph_cache=int(server_config.get("max_graph_cache", 8)),
        cache_voice=bool(server_config.get("cache_voice", True)),
        voice_cache_dir=data_dir / "voices",
        low_vram=bool(server_config.get("low_vram", False)),
    )

    def _warm():
        try:
            OmniVoiceProvider.warmup(**warm_kwargs)
        except Exception as e:
            logger.warning("warmup thread crashed: %s", e)

    threading.Thread(target=_warm, daemon=True, name="omnivoice_tts-warmup").start()
    logger.info("warming up model in background (device=%s, dtype=%s) ...",
                warm_kwargs["device"], warm_kwargs["dtype_name"])


def _build_parser():
    p = argparse.ArgumentParser(
        prog="omnivoice_tts-server",
        description="WebSocket TTS server backed by k2-fsa OmniVoice.",
    )
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                   help=f"Path to config.yml (default: {DEFAULT_CONFIG_PATH.name} next to this script).")
    p.add_argument("--host", default=None, help="Bind host. Default 0.0.0.0.")
    p.add_argument("--port", type=int, default=None, help="Bind port. Default 8788.")
    p.add_argument("--model", default=None, help="HuggingFace repo id or local path of the OmniVoice model.")
    p.add_argument("--device", default=None, help="cuda / cuda:0 / mps / cpu. Default auto.")
    p.add_argument("--ref-audio", default=None, dest="ref_audio",
                   help="Path to a reference clip for voice cloning. Mutually exclusive with --instruct.")
    p.add_argument("--instruct", default=None,
                   help="Voice design prompt, eg 'female, low pitch, british accent'.")
    p.add_argument("--output-sample-rate", type=int, default=None, dest="output_sample_rate",
                   help="Resample server output to this rate before sending. Default 24000 (model native).")
    p.add_argument("--allow-overrides", dest="allow_overrides", action="store_true", default=None,
                   help="Let clients send a config message to override voice/model at connect time.")
    p.add_argument("--no-overrides", dest="allow_overrides", action="store_false",
                   help="Reject client config overrides, use server config only.")
    p.add_argument("--no-warmup", action="store_true",
                   help="Skip the startup warmup, load the model lazily on the first connection instead.")
    p.add_argument("--ws-impl", dest="ws_impl",
                   choices=["auto", "wsproto", "websockets", "websockets-sansio"],
                   default="auto",
                   help="Which uvicorn websocket impl to use. Default 'auto' prefers wsproto if available "
                        "(wsproto is the most compat-friendly). Use 'websockets-sansio' if you've pinned a "
                        "matching uvicorn/websockets pair.")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug-level logs.")
    return p


def main():
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    server_config, host, port, allow_overrides = _resolve_server_config(args)

    print()
    print(" ┌─ omnivoice_tts server ────────────────────────────────────")
    print(f" │  model    {server_config.get('model')}")
    print(f" │  device   {server_config.get('device')} (dtype {server_config.get('dtype')})")
    print(f" │  voice    {server_config.get('ref_audio') or server_config.get('instruct') or 'auto'}")
    print(f" │  binding  ws://{host}:{port}{P.WS_PATH}")
    print(f" │  overrides {'allowed' if allow_overrides else 'rejected'}")
    print(" └───────────────────────────────────────────────────────────")
    print()

    try:
        import uvicorn
    except ImportError:
        print("ERROR: uvicorn isn't installed. Run `uv sync` or `pip install -r requirements.txt`.", file=sys.stderr)
        sys.exit(2)

    if not args.no_warmup:
        _kick_off_warmup(server_config)

    app = make_app(server_config, allow_overrides=allow_overrides)

    # let ctrl+c kill the process cleanly on windows (uvicorn already
    # installs handlers, this just makes sure they fire)
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    # pin the ws impl to wsproto. uvicorns default `auto` picks the
    # legacy `websockets` impl which 403s every connect with websockets
    # >=14 (the new sansio API broke uvicorn<0.34). wsproto is rock
    # solid and unaffected.
    ws_impl = args.ws_impl
    if ws_impl == "auto":
        try:
            import wsproto  # noqa: F401
            ws_impl = "wsproto"
        except ImportError:
            ws_impl = "auto"  # let uvicorn figure it out

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="debug" if args.verbose else "info",
        ws=ws_impl,
        ws_max_size=16 * 1024 * 1024,
        access_log=False,
    )


if __name__ == "__main__":
    main()
