"""Chatbox source. Shows the current duet song while one is playing."""
from __future__ import annotations


def _fmt_time(s: float) -> str:
    s = max(0, int(s))
    return f"{s // 60:02d}:{s % 60:02d}"


class DuoChatbox:
    def __init__(self, engine):
        self._engine = engine

    def _ended(self, st: dict) -> bool:
        dur = float(st.get("duration") or 0.0)
        pos = float(st.get("position") or 0.0)
        # if we know the length, hide once we're past it. small 1s grace so
        # the last second isnt cut off.
        if dur > 0 and pos > dur + 1.0:
            return True
        return False

    def is_active(self) -> bool:
        st = self._engine.status()
        if not st.get("title"):
            return False
        if self._ended(st):
            # opportunistically tell the engine to forget the track so the
            # tools / status calls also stop reporting it.
            try:
                self._engine.notify_track_ended()
            except Exception:
                pass
            return False
        return True

    def render(self):
        st = self._engine.status()
        title = st.get("title")
        if not title or self._ended(st):
            return None
        pos = _fmt_time(st.get("position") or 0.0)
        dur = _fmt_time(st.get("duration") or 0.0)
        if st.get("duration"):
            return f"duo: {title} [{pos}/{dur}]"
        return f"duo: {title} [{pos}]"

    def on_clear(self):
        # api v2 hook: host calls this when we lose the chatbox to another
        # source or transition to inactive with nothing taking over. tell
        # the engine to drop its current track if it hasnt already, so the
        # tools dont keep reporting a song that ended.
        try:
            self._engine.notify_track_ended()
        except Exception:
            pass
