#!/usr/bin/env bash
# Standalone omnivoice_tts server launcher (unix).
# Creates a local .venv via `uv sync` on first run, then starts the server.
# Forwards any extra args to server.py.
#
# Usage:
#   ./run.sh
#   ./run.sh --port 9000
#   ./run.sh --instruct "female, low pitch, british accent"

set -euo pipefail

cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv isn't on PATH. Install from https://docs.astral.sh/uv/" >&2
    echo "or with: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 2
fi

if [ ! -d .venv ]; then
    echo "first run, creating .venv and installing deps via uv sync ..."
    if ! uv sync; then
        echo "" >&2
        echo "uv sync failed. you may need to install torch for your platform first, eg:" >&2
        echo "  uv pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128" >&2
        exit 1
    fi
fi

exec uv run server.py "$@"
