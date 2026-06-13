# omnivoice_tts standalone server

Run the [OmniVoice](https://github.com/k2-fsa/OmniVoice) TTS model on one
machine and stream audio to a remote Project Gabriel instance over a
WebSocket. Useful when your Gabriel box doesn't have a GPU, or when you
want to keep the heavy model on a dedicated server and run Gabriel itself
on a laptop / NUC / mac mini.

This folder is self-contained. You can clone it out, copy it to another
PC, install with `uv sync`, and run. It does not need the rest of the
plugins repo or the Gabriel host installed.

## What it does

- Spins up a [FastAPI](https://fastapi.tiangolo.com/) app with one
  WebSocket route, `/tts`.
- On each new connection, builds a fresh `OmniVoiceProvider` (the same
  class the plugin uses locally) and starts it.
- Streams text from the client into the model, ships raw little-endian
  int16 mono PCM back as binary WebSocket frames.
- Handles mid-turn interruptions, multiple concurrent clients (they
  share the loaded model via a class-level warm cache, so client #2
  onward starts instantly), per-connection voice overrides.

## Install

You need [`uv`](https://docs.astral.sh/uv/) on PATH. Then:

```powershell
cd omnivoice_tts\standalone
uv sync
```

Torch needs the right CUDA build for your GPU. If `uv sync` fails on
torch, install it yourself first:

```powershell
uv pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
uv sync
```

(`cu128` for a 50-series / RTX 5090, `cu124` for 40-series, `cpu` for no
GPU, `cu121` for older drivers, etc.)

## Configure

Copy the example config and edit it:

```powershell
Copy-Item config.example.yml config.yml
notepad config.yml
```

The config has two sections: `server:` (host/port/permissions) and
`omnivoice_tts:` (all the engine knobs). The engine knobs are the same
as the plugin's `omnivoice_tts/config.example.yml` in the parent repo,
see there for what each one does.

If you don't make a `config.yml`, the server boots with sane defaults
(model auto-pulled from HuggingFace, voice = auto mode, port 8788).

## Run

```powershell
.\run.bat
```

or on linux / mac:

```bash
./run.sh
```

You can pass any CLI flag through:

```powershell
.\run.bat --port 9000 --instruct "female, low pitch, british accent"
```

CLI flags override anything in `config.yml`.

### CLI flags

| flag | what it does |
|---|---|
| `--config PATH` | yaml file to load. defaults to `config.yml` in this folder |
| `--host HOST` | bind host. default `0.0.0.0` |
| `--port PORT` | bind port. default `8788` |
| `--model REPO` | huggingface repo id or local path |
| `--device DEV` | `cuda`, `cuda:0`, `mps`, `cpu`. default auto |
| `--ref-audio PATH` | reference clip for voice cloning |
| `--instruct TEXT` | voice design prompt (eg "female, low, british") |
| `--output-sample-rate HZ` | resample output before sending. default 24000 |
| `--allow-overrides` / `--no-overrides` | allow / refuse per-client voice swaps |
| `-v` | verbose logging |

## Hooking it up to Gabriel

On the Gabriel machine, edit `config.yml` (in the Gabriel install, NOT
here):

```yaml
plugins:
  omnivoice_tts:
    remote:
      url: "ws://YOUR.SERVER.IP:8788/tts"
      reconnect: true
      timeout_seconds: 30
tts:
  external_provider: "omnivoice"
```

When `remote.url` is set the plugin skips loading the local model and
just talks to the server over WS. Everything else (voice, dtype, etc) is
read from the server's config, so you only configure the voice in ONE
place.

## Protocol

See [protocol.py](protocol.py) for the exact message types. Quick gist:

- All control messages are JSON text frames: `{"type": "feed_text", "text": "hi"}`.
- All audio is raw little-endian int16 mono PCM in binary frames.
- Server sends `hello` on connect with sample_rate/dtype/model/voice info.
- Server sends `ready` once the model finishes warming up.
- Server sends `audio_start` before the first chunk of a turn, `audio_end`
  when the turn finishes naturally, `interrupted` if the client cancelled it.
- Client sends `feed_text` (streaming sentences), `turn_complete` (commit
  the buffered tail), `interrupt` (drop the current turn).

That's it. Any websocket client can drive it, the plugin's
`remote_client.py` is just one consumer.

## Limits / gotchas

- Voice cloning prompts (`ref_audio`) need to live on the SERVER's disk,
  not the client's. The client just names the voice, the server resolves
  the path.
- Multiple clients share the loaded model. They each get their own
  request queue, but if the GPU is saturated they'll fight for slots.
- The server doesn't authenticate connections. Put it behind a reverse
  proxy + auth if you expose it to the public internet.
- Sample rate stays fixed for the lifetime of a connection. To change it,
  reconnect.

## Files

| file | what it is |
|---|---|
| `server.py` | the FastAPI app, WS handler, CLI entry point |
| `engine.py` | vendored copy of `omnivoice_tts/provider.py` (the actual TTS) |
| `perf.py` | flash-attn + cuda graph helpers, vendored from the plugin |
| `protocol.py` | shared WS message type constants |
| `pyproject.toml` | uv / pip project file |
| `requirements.txt` | flat pip-friendly version |
| `run.bat` / `run.sh` | one-shot launcher (does `uv sync` then `uv run server.py`) |
| `config.example.yml` | template config, copy to `config.yml` to use |
