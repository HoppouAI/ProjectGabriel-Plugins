"""AI tools for midi_band. Mostly a thin shim over BandServer methods.

When the plugin runs as 'client' role most tools refuse, only `bandStatus`
works (status of what the host told us to play).
"""
from __future__ import annotations

from google.genai import types

from src.tools._base import BaseTool


class BandTools(BaseTool):
    tool_key = "midi_band"
    # populated by the plugin entry before ToolHandler instantiates the class
    _server = None  # BandServer when role=host
    _client = None  # BandClient when role=client

    def declarations(self, config=None):
        return [
            types.FunctionDeclaration(
                name="listMidiSongs",
                description=(
                    "List the MIDI songs available in your band library that you can perform "
                    "with your bandmates.\n"
                    "**Invocation Condition:** Call when you need to know what songs the band can play, "
                    "or before picking one for a performance."
                ),
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="loadMidiSong",
                description=(
                    "Pick a song for the band and inspect its tracks. The response lists every track "
                    "with its name, instrument, channel and note count, which you use as input to "
                    "assignBandTracks. Loading a song clears any previous assignments.\n"
                    "**Invocation Condition:** Call after deciding on a song, before assigning tracks "
                    "or starting playback."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "title": {
                            "type": "STRING",
                            "description": "MIDI filename or substring. Use listMidiSongs first if unsure.",
                        },
                    },
                    "required": ["title"],
                },
            ),
            types.FunctionDeclaration(
                name="listBandMembers",
                description=(
                    "List the bandmates currently connected and ready to perform with you, plus your "
                    "own name. Use these names when calling assignBandTracks.\n"
                    "**Invocation Condition:** Call when you need to know who is in the band, "
                    "especially before assigning tracks."
                ),
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="autoAssignBandTracks",
                description=(
                    "Automatically distribute the loaded song's playable tracks across you and every "
                    "connected bandmate, round robin. Quick way to get going if you dont care who "
                    "plays what.\n"
                    "**Invocation Condition:** Call after loadMidiSong when you want a fast automatic "
                    "split. Use assignBandTracks instead if you want specific people on specific "
                    "instruments."
                ),
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="assignBandTracks",
                description=(
                    "Manually assign which tracks of the loaded song each bandmate plays. Use the "
                    "track indices returned by loadMidiSong, and the bandmate names returned by "
                    "listBandMembers. Pass your own tracks under host_tracks (you do not appear in "
                    "client_assignments).\n"
                    "**Invocation Condition:** Call after loadMidiSong when you want to assign "
                    "specific instruments to specific bandmates. Skip if you already called "
                    "autoAssignBandTracks."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "host_tracks": {
                            "type": "ARRAY",
                            "items": {"type": "INTEGER"},
                            "description": "Track indices YOU will play.",
                        },
                        "client_assignments": {
                            "type": "OBJECT",
                            "description": (
                                "Object mapping each bandmate's name to an array of track indices "
                                "they will play, e.g. {\"gabriel_b\": [2, 3], \"gabriel_c\": [4]}."
                            ),
                        },
                    },
                    "required": ["host_tracks", "client_assignments"],
                },
            ),
            types.FunctionDeclaration(
                name="startMidiBand",
                description=(
                    "Start the performance. Every bandmate begins their assigned tracks at the exact "
                    "same moment, like a live band hitting the downbeat together.\n"
                    "**Invocation Condition:** Call after loadMidiSong (and optionally an assignment "
                    "tool). Don't call if a song is already playing, call stopMidiBand first."
                ),
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="stopMidiBand",
                description=(
                    "Stop the band's current performance immediately on every bandmate.\n"
                    "**Invocation Condition:** Call when you or the user wants to end the song."
                ),
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="bandStatus",
                description=(
                    "Check what the band is doing right now: loaded song, who is connected, what "
                    "tracks are assigned to whom, whether playback is in progress, and your own "
                    "role.\n"
                    "**Invocation Condition:** Call only when the user asks about the band state. "
                    "Don't poll between turns."
                ),
                parameters={"type": "OBJECT", "properties": {}},
            ),
        ]

    async def handle(self, name, args):
        srv = self._server
        cli = self._client

        if name == "bandStatus":
            if srv is not None:
                return {"result": "ok", "role": "host", **_host_status(srv)}
            if cli is not None:
                return {"result": "ok", "role": "client", **cli.status()}
            return None

        # client mode: only status applies, everything else is host-only
        if srv is None:
            if cli is not None:
                return {
                    "result": "error",
                    "message": "this instance is a band client, only the host can manage the band.",
                }
            return None

        if name == "listMidiSongs":
            songs = srv.list_songs()
            return {
                "result": "ok",
                "count": len(songs),
                "songs": songs,
                "library_empty": not songs,
            }
        if name == "loadMidiSong":
            return srv.load_song(str(args.get("title") or ""))
        if name == "listBandMembers":
            mates = srv.list_clients()
            return {
                "result": "ok",
                "you": srv.instance_name,
                "bandmates": mates,
                "count": 1 + len(mates),
            }
        if name == "autoAssignBandTracks":
            return srv.auto_assign()
        if name == "assignBandTracks":
            host_tracks = args.get("host_tracks") or []
            client_assignments = args.get("client_assignments") or {}
            if not isinstance(client_assignments, dict):
                return {"result": "error", "message": "client_assignments must be an object/map"}
            return srv.assign_tracks(list(host_tracks), client_assignments)
        if name == "startMidiBand":
            return await srv.start_playback()
        if name == "stopMidiBand":
            return await srv.stop_playback()
        return None


def _host_status(srv) -> dict:
    info = srv.loaded_info()
    ps = srv.player.status()
    return {
        "you": srv.instance_name,
        "bandmates": srv.list_clients(),
        "song": ps.get("song") or info.get("song"),
        "song_duration": info.get("duration"),
        "tracks": info.get("tracks"),
        "host_tracks": info.get("host_tracks"),
        "assignments": info.get("assignments"),
        "playing": ps.get("playing"),
        "position": ps.get("position"),
    }
