"""standalone server package for omnivoice_tts.

This is a sub-package of the plugin so the plugin's remote_client.py can
do `from .standalone import protocol`. It's ALSO a self-contained folder
you can copy out and run on a different machine via `uv sync && uv run
server.py`. The pyproject.toml here doesn't treat it as a package
(`[tool.uv] package = false`) so the dual role doesn't fight.
"""
