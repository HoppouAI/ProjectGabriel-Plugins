"""AI band conductor: hands a song's track list to Gemini and lets it
assign every part to a band member via function calling.

Same google-genai pattern the diary plugin uses. Lazy imports so the
plugin still loads fine when google-genai isn't installed, the conduct
call just returns an error in that case.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.1-flash-lite"


def _match_member(name: str, members: List[str]) -> Optional[str]:
    """Map a name the model spat out back to a real member, tolerant of
    case and minor spelling drift."""
    if not name:
        return None
    n = str(name).strip()
    for m in members:
        if m == n:
            return m
    low = n.lower()
    for m in members:
        if m.lower() == low:
            return m
    for m in members:
        ml = m.lower()
        if ml.startswith(low) or low.startswith(ml):
            return m
    return None


def _build_user_prompt(song: str, tracks: List[dict], members: List[str],
                       host_name: str, vibe: str) -> str:
    lines = [f'Song: "{song}"', "", "Tracks you can assign:"]
    for t in tracks:
        idx = t.get("index")
        label = t.get("display_label") or t.get("instrument") or t.get("name") or f"track {idx}"
        notes = t.get("note_count", 0)
        ch = t.get("channel")
        drum = " (drum kit)" if ch == 9 else ""
        lines.append(f"  - track {idx}: {label}{drum}, {notes} notes")
    lines.append("")
    lines.append("Band members available to play (assign every part to one of these names):")
    for m in members:
        tag = " (you, the host)" if m == host_name else ""
        lines.append(f"  - {m}{tag}")
    lines.append("")
    lines.append(f"What the user is going for: {vibe.strip() or 'a balanced, full sounding arrangement'}")
    return "\n".join(lines)


def _build_declarations(gtypes):
    return [
        gtypes.FunctionDeclaration(
            name="assignTracks",
            description=(
                "Assign the song's tracks to the band. Call this exactly once. Each entry is "
                "one track plus the list of members who play it. Put several members on the "
                "same track to make that part sound bigger, like a choir or a string section, "
                "each extra member adds another voice to it. Leave a track out entirely to keep "
                "it silent. Spread the parts so the arrangement matches what the user asked for."
            ),
            parameters=gtypes.Schema(
                type=gtypes.Type.OBJECT,
                required=["assignments"],
                properties={
                    "assignments": gtypes.Schema(
                        type=gtypes.Type.ARRAY,
                        description="One entry per track to play.",
                        items=gtypes.Schema(
                            type=gtypes.Type.OBJECT,
                            required=["track", "members"],
                            properties={
                                "track": gtypes.Schema(
                                    type=gtypes.Type.INTEGER,
                                    description="The track index to assign.",
                                ),
                                "members": gtypes.Schema(
                                    type=gtypes.Type.ARRAY,
                                    description=(
                                        "Names of the band members who all play this track at "
                                        "once. Usually one, but list several to thicken the part "
                                        "into a section (choir, strings, big pad or lead)."
                                    ),
                                    items=gtypes.Schema(type=gtypes.Type.STRING),
                                ),
                            },
                        ),
                    ),
                    "reasoning": gtypes.Schema(
                        type=gtypes.Type.STRING,
                        description="One short sentence on the arrangement choices you made.",
                    ),
                },
            ),
        )
    ]


async def conduct(
    api_key: str,
    song: str,
    tracks: List[dict],
    members: List[str],
    host_name: str,
    vibe: str,
    model: str = DEFAULT_MODEL,
) -> dict:
    """Ask the model to assign tracks. Returns a dict with host_tracks +
    client_assignments ready to feed BandServer.assign_tracks, or an error."""
    if not api_key:
        return {"result": "error", "message": "no Gemini API key set for the conductor"}
    if not members:
        return {"result": "error", "message": "no band members to assign to"}
    playable = [t for t in tracks if t.get("note_count", 0) > 0]
    if not playable:
        return {"result": "error", "message": "this song has no playable tracks"}

    try:
        from google import genai
        from google.genai import types as gtypes
    except Exception:
        return {"result": "error", "message": "google-genai is not installed, cannot run the conductor"}

    valid_idx = {int(t["index"]) for t in playable}
    system = (
        "You are the conductor of a live band. Each member is a separate player who can only "
        "play the tracks you give them, all at once. Assign every meaningful track to a member "
        "so the song sounds full. Match the user's described vibe: if they want it stripped "
        "back, leave busy tracks out; if they want it big, use everyone. Keep one player from "
        "being buried under too many parts unless it makes sense. Drums usually go to a single "
        "player. You can put several members on the same track to thicken it into a section, "
        "this is how you turn one voice into a real choir, string or pad section, do it when the "
        "user wants something big, lush or choral but dont double every part or it turns to "
        "mush. Call assignTracks once with your full plan."
    )
    user = _build_user_prompt(song, playable, members, host_name, vibe)

    try:
        client = genai.Client(api_key=api_key)
        response = await client.aio.models.generate_content(
            model=model,
            contents=user,
            config=gtypes.GenerateContentConfig(
                system_instruction=[gtypes.Part.from_text(text=system)],
                temperature=0.7,
                thinking_config=gtypes.ThinkingConfig(thinking_level="MINIMAL"),
                tools=[gtypes.Tool(function_declarations=_build_declarations(gtypes))],
                tool_config=gtypes.ToolConfig(
                    function_calling_config=gtypes.FunctionCallingConfig(
                        mode="ANY", allowed_function_names=["assignTracks"],
                    )
                ),
                safety_settings=[
                    gtypes.SafetySetting(category=c, threshold="BLOCK_NONE")
                    for c in (
                        "HARM_CATEGORY_HARASSMENT",
                        "HARM_CATEGORY_HATE_SPEECH",
                        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                        "HARM_CATEGORY_DANGEROUS_CONTENT",
                    )
                ],
            ),
        )
    except Exception as e:
        logger.error(f"midi_band: conductor model call failed: {e}")
        return {"result": "error", "message": f"conductor failed: {e}"}

    call = None
    for fc in (response.function_calls or []):
        if fc.name == "assignTracks":
            call = fc
            break
    if call is None:
        return {"result": "error", "message": "the conductor didn't return an arrangement"}

    args = dict(call.args or {})
    raw = args.get("assignments") or []
    reasoning = str(args.get("reasoning") or "").strip()

    host_tracks: List[int] = []
    client_assignments: Dict[str, List[int]] = {}
    used: set = set()
    unknown_members: set = set()
    for item in raw:
        try:
            ti = int(item.get("track"))
        except (TypeError, ValueError, AttributeError):
            continue
        if ti not in valid_idx:
            continue
        # accept the members array, or a legacy single "member" string
        who_list = item.get("members")
        if who_list is None:
            single = item.get("member")
            who_list = [single] if single else []
        elif isinstance(who_list, str):
            who_list = [who_list]
        for raw_who in who_list:
            who = _match_member(raw_who, members)
            if who is None:
                unknown_members.add(str(raw_who))
                continue
            used.add(ti)
            if who == host_name:
                if ti not in host_tracks:
                    host_tracks.append(ti)
            else:
                lst = client_assignments.setdefault(who, [])
                if ti not in lst:
                    lst.append(ti)

    if not host_tracks and not client_assignments:
        return {"result": "error", "message": "the conductor's arrangement was empty"}

    return {
        "result": "ok",
        "host_tracks": host_tracks,
        "client_assignments": client_assignments,
        "reasoning": reasoning,
        "unassigned_tracks": sorted(valid_idx - used),
        "unknown_members": sorted(unknown_members),
    }
