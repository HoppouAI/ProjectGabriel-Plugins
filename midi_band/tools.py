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
                    "client_assignments). You can put the same track index on more than one "
                    "bandmate (and yourself) to thicken that part into a section, great for "
                    "choirs, strings and big pads.\n"
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
                                "they will play, e.g. {\"gabriel_b\": [2, 3], \"gabriel_c\": [4]}. "
                                "The same track index can appear under several bandmates to double "
                                "it into a fuller section."
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
                name="pauseMidiBand",
                description=(
                    "Pause the current song for you and every bandmate. The song freezes where "
                    "it is and you can resume from the same spot later with resumeMidiBand.\n"
                    "**Invocation Condition:** Call when the user asks to pause, hold, or take a "
                    "break. Don't call if nothing is playing."
                ),
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="resumeMidiBand",
                description=(
                    "Resume a paused song so you and every bandmate pick up from where you "
                    "stopped, all at the same moment.\n"
                    "**Invocation Condition:** Call when the user wants to continue a paused song. "
                    "Don't call if no song is paused, or if a song is already playing."
                ),
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="setBandVolume",
                description=(
                    "Set how loud you and every bandmate play. 0.0 is silent, 0.5 is the normal "
                    "default, 1.0 is loud, 2.0 is as loud as it gets. Takes effect immediately "
                    "even mid-song.\n"
                    "**Invocation Condition:** Call when the user asks the band to be quieter, "
                    "louder, softer, or to mute. Pick a value that matches the request."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "level": {
                            "type": "NUMBER",
                            "description": "Volume from 0.0 (silent) to 2.0 (loudest). 0.5 is normal.",
                        },
                    },
                    "required": ["level"],
                },
            ),
            types.FunctionDeclaration(
                name="bandSoundcheck",
                description=(
                    "Quick sync check: every bandmate plays a short percussive tick on alternating "
                    "beats for a few seconds, like a click track passed around the room. Useful "
                    "before a real performance to confirm everyone is in time and to wake up "
                    "their instruments so the first song starts crisp.\n"
                    "**Invocation Condition:** Call when the user asks to test the band, hear if "
                    "everyone is in sync, or warm up. Don't call right before startMidiBand on the "
                    "same beat, give it a moment to finish first."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "duration_seconds": {
                            "type": "NUMBER",
                            "description": "How long the soundcheck runs. Default 10. Keep it under 30.",
                        },
                        "bpm": {
                            "type": "NUMBER",
                            "description": "Tempo of the click. Default 120.",
                        },
                    },
                },
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
            types.FunctionDeclaration(
                name="bandSyncStatus",
                description=(
                    "Check how well every bandmate is locked to your clock. Reports each "
                    "bandmate's clock jitter (how much the per-sample clock estimate wobbles, "
                    "in milliseconds) and round-trip latency. Tight numbers (jitter under 5ms, "
                    "rtt under 30ms) mean everyone will start a song together. Big or stale "
                    "numbers mean a bandmate's connection is bad.\n"
                    "**Invocation Condition:** Call when the user asks if the band is in sync, "
                    "asks about latency, or wonders if a specific bandmate is lagging. Host only."
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
        if name == "pauseMidiBand":
            return await srv.pause_playback()
        if name == "resumeMidiBand":
            return await srv.resume_playback()
        if name == "setBandVolume":
            try:
                lvl = float(args.get("level"))
            except Exception:
                return {"result": "error", "message": "level must be a number 0.0 to 2.0"}
            lvl = max(0.0, min(2.0, lvl))
            return await srv.set_volume(lvl)
        if name == "bandSoundcheck":
            try:
                dur = float(args.get("duration_seconds") or 10.0)
            except Exception:
                dur = 10.0
            try:
                bpm = float(args.get("bpm") or 120.0)
            except Exception:
                bpm = 120.0
            dur = max(2.0, min(30.0, dur))
            bpm = max(40.0, min(240.0, bpm))
            return await srv.soundcheck(duration=dur, bpm=bpm)
        if name == "bandSyncStatus":
            return {"result": "ok", **srv.get_sync_status()}
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
        "paused": ps.get("paused"),
        "position": ps.get("position"),
        "gain": ps.get("gain"),
    }
