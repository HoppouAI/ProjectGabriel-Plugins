"""Shared protocol constants for midi_band plugin and standalone client."""
from __future__ import annotations

import json

DEFAULT_PORT = 8766
PROTO_VERSION = 1
# midi files travel over a single newline JSON line as base64. bump the
# stream reader limit so multi-MB songs dont get truncated.
READER_LIMIT = 16 * 1024 * 1024

# message types
HELLO = "hello"
WELCOME = "welcome"
PING = "ping"
PONG = "pong"
PREPARE = "prepare"
READY = "ready"
NACK = "nack"
PLAY = "play"
STOP = "stop"
ASSIGNMENTS = "assignments"
SOUNDCHECK = "soundcheck"
ERROR = "error"


def encode(msg: dict) -> bytes:
    return (json.dumps(msg) + "\n").encode("utf-8")


def decode(line: bytes) -> dict:
    return json.loads(line.decode("utf-8"))
