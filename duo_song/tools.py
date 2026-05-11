"""Tools the AI calls to control the duo. Engine is stashed as a class
attribute since ToolHandler instantiates with cls(handler) only.
"""
from __future__ import annotations

from google.genai import types

from src.tools._base import BaseTool


def _fmt_time(s: float) -> str:
    s = max(0, int(s))
    return f"{s // 60:02d}:{s % 60:02d}"


class DuoSongTools(BaseTool):
    tool_key = "duo_song"
    _engine = None  # set by the plugin entry before ToolHandler spawns us

    def declarations(self, config=None):
        return [
            types.FunctionDeclaration(
                name="startDuoSong",
                description=(
                    "Start a duet, you sing one part and your duo partner sings the other, both audio "
                    "tracks start at the exact same moment. Each duet song is a pair of files (PT1 "
                    "and PT2). Pick a song by title, substring match against the base name (no need "
                    "to include PT1/PT2).\n"
                    "**Invocation Condition:** Call when you or the user wants to perform a duet song "
                    "with your partner. Don't call for solo background music. Only call when both of "
                    "you should hear your respective parts in sync."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "title": {
                            "type": "STRING",
                            "description": "Song base title or substring. Use listDuoSongs first if youre unsure.",
                        },
                    },
                    "required": ["title"],
                },
            ),
            types.FunctionDeclaration(
                name="stopDuoSong",
                description=(
                    "Stop the current duet on both sides.\n"
                    "**Invocation Condition:** Call when you or the user wants to end the current "
                    "duet. Don't call when nothing is playing."
                ),
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="listDuoSongs",
                description=(
                    "List the duet songs available. Each entry shows whether both parts (PT1 + PT2) "
                    "are present, only complete pairs can actually be played.\n"
                    "**Invocation Condition:** Call when you need to know what duets you can play, "
                    "e.g. before picking one or when the user asks whats available."
                ),
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="duoStatus",
                description=(
                    "Check the duet state, whats playing right now, who is connected on the other "
                    "side, current playback position, which part this side sings.\n"
                    "**Invocation Condition:** Call only when the user asks about the duet state or "
                    "whether your partner is connected. Don't spam this between turns."
                ),
                parameters={"type": "OBJECT", "properties": {}},
            ),
        ]

    async def handle(self, name, args):
        if self._engine is None:
            return None
        if name == "startDuoSong":
            title = str(args.get("title") or "").strip()
            if not title:
                return {"result": "error", "message": "no title given"}
            return await self._engine.request_play(title)
        if name == "stopDuoSong":
            return await self._engine.request_stop()
        if name == "listDuoSongs":
            songs = self._engine.list_songs()
            complete = [s["title"] for s in songs if s.get("complete")]
            incomplete = [
                {"title": s["title"], "missing": ("PT1" if not s.get("have_pt1") else "PT2")}
                for s in songs if not s.get("complete")
            ]
            return {
                "result": "ok",
                "playable": complete,
                "incomplete": incomplete,
                "library_empty": len(songs) == 0,
            }
        if name == "duoStatus":
            st = self._engine.status()
            pretty = {
                "role": st["role"],
                "you_are": st["instance"],
                "you_sing": f"PT{st['local_part']}",
                "connected_to_partner": st["connected"],
                "peers": st["peers"],
                "playing": st["playing"],
                "title": st["title"],
                "position": _fmt_time(st["position"]),
                "duration": _fmt_time(st["duration"]),
            }
            return {"result": "ok", **pretty}
        return None
