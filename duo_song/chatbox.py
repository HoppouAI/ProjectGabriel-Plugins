"""Chatbox source. Shows the current duet song while one is playing."""
from __future__ import annotations


def _fmt_time(s: float) -> str:
    s = max(0, int(s))
    return f"{s // 60:02d}:{s % 60:02d}"


class DuoChatbox:
    def __init__(self, engine):
        self._engine = engine

    def is_active(self) -> bool:
        st = self._engine.status()
        return bool(st.get("playing") or st.get("title"))

    def render(self):
        st = self._engine.status()
        title = st.get("title")
        if not title:
            return None
        pos = _fmt_time(st.get("position") or 0.0)
        dur = _fmt_time(st.get("duration") or 0.0)
        if st.get("duration"):
            return f"duo: {title} [{pos}/{dur}]"
        return f"duo: {title} [{pos}]"
