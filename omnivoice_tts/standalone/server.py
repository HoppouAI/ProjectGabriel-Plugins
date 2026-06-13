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

    sr = int(cfg.pop("output_sample_rate", 24000))
    data_dir = Path(cfg.pop("data_dir", str(_HERE / "data")))

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

    async def run(self):
        # send a hello as soon as we accept the WS so the client can
        # confirm protocol + server-side params before doing anything.
        await self._send_hello_minimal()

        # build the provider with whatever server defaults + (if
        # allowed) the client's initial config message.
        client_overrides = await self._maybe_read_initial_config()

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
                    # we don't accept inbound audio, the client only sends text
                    await self._send_json({"type": P.TYPE_ERROR, "message": "binary frames not accepted"})
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

    async def _maybe_read_initial_config(self) -> dict | None:
        # client MAY send a config message as its first frame to override
        # voice / model. give it 250ms then move on if it didn't.
        try:
            msg = await asyncio.wait_for(self.ws.receive(), timeout=0.25)
        except asyncio.TimeoutError:
            return None
        if msg.get("type") == "websocket.disconnect":
            raise RuntimeError("client disconnected before send")
        if "text" not in msg or msg["text"] is None:
            logger.info("ws: dropped non-text first frame, expected a config")
            return None
        try:
            data = json.loads(msg["text"])
        except json.JSONDecodeError:
            return None
        if data.get("type") != P.TYPE_CONFIG:
            logger.info("ws: first frame wasn't a config message, dropped %r", data.get("type"))
            return None
        if not self.allow_overrides:
            logger.info("ws: client tried to send config overrides but server has --no-overrides set")
            return None
        return data.get("overrides") or {}

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
    from fastapi import FastAPI, WebSocket

    app = FastAPI(title="omnivoice_tts server", version="0.1.0")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "model": server_config.get("model")}

    @app.websocket(P.WS_PATH)
    async def tts_socket(ws: WebSocket):
        await ws.accept()
        session = WSSession(ws, server_config, allow_overrides=allow_overrides)
        await session.run()

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

    # sensible defaults so an empty config still boots
    server.setdefault("model", "k2-fsa/OmniVoice")
    server.setdefault("device", _autodetect_device())
    server.setdefault("dtype", "float16")
    server.setdefault("output_sample_rate", 24000)
    server.setdefault("data_dir", str(_HERE / "data"))

    return server, host, port, allow_overrides


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

    app = make_app(server_config, allow_overrides=allow_overrides)

    # let ctrl+c kill the process cleanly on windows (uvicorn already
    # installs handlers, this just makes sure they fire)
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="debug" if args.verbose else "info",
        ws_max_size=16 * 1024 * 1024,
        access_log=False,
    )


if __name__ == "__main__":
    main()
