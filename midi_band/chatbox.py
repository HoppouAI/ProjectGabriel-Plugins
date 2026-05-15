"""Chatbox source for midi_band. Shows the song name, a divider, then the
names of the tracks/instruments THIS instance is playing. Each bandmate
shows their own slice so the chatbox is per-player.
"""
from __future__ import annotations

CHATBOX_MAX = 144
DIVIDER = "\u2500" * 14
BAR_WIDTH = 14


def _fmt_time(t: float) -> str:
    t = max(0, int(t))
    return f"{t // 60}:{t % 60:02d}"


def _progress_bar(pos: float, dur: float) -> str:
    if dur <= 0:
        return ""
    frac = max(0.0, min(1.0, pos / dur))
    filled = int(round(frac * BAR_WIDTH))
    bar = "\u2588" * filled + "\u2591" * (BAR_WIDTH - filled)
    return f"{bar} {_fmt_time(pos)}/{_fmt_time(dur)}"


class BandChatbox:
    """Takes a no-arg callable that returns a status dict with at least
    'song', 'tracks' (list of names), and 'playing' / 'position' / 'duration'.
    The plugin entry wires up host vs client status sources before passing it.
    """

    def __init__(self, get_status):
        self._status = get_status

    def is_active(self) -> bool:
        s = self._status() or {}
        if s.get("playing") or s.get("paused"):
            return True
        # also stay on briefly when prepare landed but play hasnt fired yet
        return bool(s.get("song"))

    def render(self):
        s = self._status() or {}
        song = s.get("song")
        if not song:
            return None
        tracks = list(s.get("tracks") or [])
        if tracks and isinstance(tracks[0], dict):
            tracks = [t.get("display_label") or t.get("instrument") or t.get("name") or "?" for t in tracks]
        if not tracks:
            tracks = ["(no tracks assigned)"]
        # count-in mode: big visible "starting in N..." countdown so
        # everyone in vrchat knows the band is about to drop
        if s.get("in_count_in"):
            remaining = float(s.get("count_in_remaining") or 0.0)
            n = max(1, int(remaining + 0.999))  # ceiling, never show 0
            lines = [
                f"midi: {song}",
                DIVIDER,
                f"\u25b6 starting in {n}...",
            ]
            lines.extend(tracks)
            return "\n".join(lines)
        prefix = "midi (paused): " if s.get("paused") else "midi: "
        lines = [f"{prefix}{song}", DIVIDER]
        bar = _progress_bar(float(s.get("position") or 0.0),
                            float(s.get("duration") or 0.0))
        if bar:
            lines.append(bar)
        lines.extend(tracks)
        return "\n".join(lines)
