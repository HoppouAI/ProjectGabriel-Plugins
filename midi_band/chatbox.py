"""Chatbox source for midi_band. Shows the song name, a divider, then the
names of the tracks/instruments THIS instance is playing. Each bandmate
shows their own slice so the chatbox is per-player.
"""
from __future__ import annotations

CHATBOX_MAX = 144


class BandChatbox:
    """Takes a no-arg callable that returns a status dict with at least
    'song', 'tracks' (list of names), and 'playing' / 'position' / 'duration'.
    The plugin entry wires up host vs client status sources before passing it.
    """

    def __init__(self, get_status):
        self._status = get_status

    def is_active(self) -> bool:
        s = self._status() or {}
        if s.get("playing"):
            return True
        # also stay on briefly when prepare landed but play hasnt fired yet
        return bool(s.get("song"))

    def render(self):
        s = self._status() or {}
        song = s.get("song")
        if not song:
            return None
        tracks = list(s.get("tracks") or [])
        # accept either ["Drums", "Bass"] or [{"name": "..."}, ...]
        if tracks and isinstance(tracks[0], dict):
            tracks = [t.get("name") or "?" for t in tracks]
        if not tracks:
            tracks = ["(no tracks assigned)"]
        body_lines = [f"midi: {song}", "-----"] + tracks
        out = "\n".join(body_lines)
        # let the host paginator handle the >144 char case
        return out
