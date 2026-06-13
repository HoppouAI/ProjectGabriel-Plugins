"""WebSocket protocol shared between the standalone omnivoice_tts server
and the plugin's remote client.

Wire format:
  control messages travel as JSON in WS text frames.
  audio chunks travel as raw little endian int16 PCM in WS binary frames.

Either side can send TYPE_PING and the peer replies with TYPE_PONG.

A typical session looks like:

    server -> client : {"type": "hello", "protocol": 1, "sample_rate": 24000,
                        "channels": 1, "dtype": "int16",
                        "model": "k2-fsa/OmniVoice", "device": "cuda",
                        "voice": "auto"}
    server -> client : {"type": "ready"}        (after model warmup)
    client -> server : {"type": "feed_text", "text": "hello "}
    client -> server : {"type": "feed_text", "text": "world."}
    client -> server : {"type": "turn_complete"}
    server -> client : {"type": "audio_start", "turn_id": 1}
    server -> client : <binary PCM chunk>
    server -> client : <binary PCM chunk>
    server -> client : {"type": "audio_end", "turn_id": 1}

If the client decides mid-turn that it wants to stop:

    client -> server : {"type": "interrupt"}
    server -> client : {"type": "interrupted", "turn_id": 1}

The server may emit log lines if the client passes "subscribe_logs": true
in its initial config message. Off by default to avoid spamming the wire.
"""
from __future__ import annotations

PROTOCOL_VERSION = 1

# control message types
TYPE_HELLO = "hello"
TYPE_CONFIG = "config"
TYPE_READY = "ready"
TYPE_FEED_TEXT = "feed_text"
TYPE_TURN_COMPLETE = "turn_complete"
TYPE_INTERRUPT = "interrupt"
TYPE_AUDIO_START = "audio_start"
TYPE_AUDIO_END = "audio_end"
TYPE_INTERRUPTED = "interrupted"
TYPE_ERROR = "error"
TYPE_PING = "ping"
TYPE_PONG = "pong"
TYPE_LOG = "log"

# default network
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8788
WS_PATH = "/tts"

# upper bound on how much audio we'll buffer on the server side before
# forcing a yield to the WS write loop. keeps a long sentence from
# blocking the event loop while it copies multi-MB into the socket.
MAX_AUDIO_CHUNK_BYTES = 64 * 1024
