"""AI band conductor: a streaming, multi-turn chat with Gemini that can
arrange the loaded song and re-voice its tracks through function calling.

The host keeps one ConductorSession alive so the conversation is real
multi-turn. Lazy imports so the plugin still loads fine when google-genai
isn't installed, a send just yields an error in that case.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.1-flash-lite"
# cap tool round-trips per user turn so a confused model can't loop forever
MAX_TOOL_ROUNDS = 6


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


def parse_assignments(raw, members: List[str], host_name: str, valid_idx) -> dict:
    """Turn a raw assignments array [{track, members:[..]}] into host_tracks +
    client_assignments ready for BandServer.assign_tracks. Tolerant of name
    drift and a legacy single 'member' string."""
    host_tracks: List[int] = []
    client_assignments: Dict[str, List[int]] = {}
    used: set = set()
    unknown_members: set = set()
    for item in raw or []:
        try:
            ti = int(item.get("track"))
        except (TypeError, ValueError, AttributeError):
            continue
        if ti not in valid_idx:
            continue
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
    return {
        "host_tracks": host_tracks,
        "client_assignments": client_assignments,
        "unknown_members": sorted(unknown_members),
        "used": used,
    }


def _state_block(snap: dict) -> str:
    """Compact summary of the band's current state, prepended to every user
    turn so the model always knows what's loaded and who plays what."""
    song = snap.get("song")
    if not song:
        return "No song is loaded yet. Ask the user to load one, or just chat."
    mode = snap.get("mode") or "midi"
    lines = [f'Song: "{song}" ({mode} mode)']
    members = snap.get("members") or []
    if members:
        names = []
        for m in members:
            nm = m.get("name")
            names.append(f"{nm} (you, host)" if m.get("is_host") else nm)
        lines.append("Band: " + ", ".join(names))
    tracks = snap.get("tracks") or []
    lines.append(f"Tracks ({len(tracks)} total, give them all a player unless asked otherwise):")
    for t in tracks:
        idx = t.get("index")
        label = t.get("label") or f"track {idx}"
        drum = " [drum kit]" if t.get("drum") else ""
        who = ", ".join(t.get("members") or []) or "unassigned"
        inst = t.get("instrument")
        inst_s = f", sounding as {inst}" if inst else ""
        lines.append(f"  {idx}: {label}{drum} -> {who}{inst_s}")
    if mode == "audio":
        lines.append("(re-voicing tracks is a MIDI-mode thing, not available right now)")
    return "\n".join(lines)


def _build_declarations(gtypes):
    S = gtypes.Schema
    T = gtypes.Type
    return [
        gtypes.FunctionDeclaration(
            name="assignTracks",
            description=(
                "Set who in the band plays which tracks of the loaded song. Each entry is one "
                "track plus the members who play it. Normally give each track exactly one member, "
                "that is how a band sounds best. This replaces the whole arrangement in one shot, "
                "so list every track you want heard, the ones you keep as well as the ones you "
                "change. Any track you omit goes silent, so by default give every track a player, "
                "the melody and bass and drums included, and only drop parts when the user wants "
                "something sparse. Only put more than one member on the same track when the user "
                "specifically asks to double or thicken it, and even then only for soft background "
                "voices like a choir, pad or 'aah', never for main instruments like lead, bass or "
                "drums since stacking those sounds bad.\n"
                "**Invocation Condition:** when the user asks you to arrange the song, hand out "
                "parts, or change who plays what."
            ),
            parameters=S(
                type=T.OBJECT,
                required=["assignments"],
                properties={
                    "assignments": S(
                        type=T.ARRAY,
                        description="One entry per track to play.",
                        items=S(
                            type=T.OBJECT,
                            required=["track", "members"],
                            properties={
                                "track": S(type=T.INTEGER, description="The track index to assign."),
                                "members": S(
                                    type=T.ARRAY,
                                    description=(
                                        "Names of the band members who play this track. Almost "
                                        "always exactly one. Only list several when the user asked "
                                        "to double a soft background voice like a choir or pad, "
                                        "never for main instruments."
                                    ),
                                    items=S(type=T.STRING),
                                ),
                            },
                        ),
                    ),
                    "reasoning": S(
                        type=T.STRING,
                        description="One short sentence on the arrangement choices, in your own voice.",
                    ),
                },
            ),
        ),
        gtypes.FunctionDeclaration(
            name="listInstruments",
            description=(
                "Look up the instrument voices you can give a track, including any custom "
                "soundfont sounds loaded on this rig. Returns each sound's name plus its bank "
                "and program numbers.\n"
                "**Invocation Condition:** call before setTrackInstrument so you pick an exact "
                "sound, or when the user asks what instruments or sounds are available."
            ),
            parameters=S(
                type=T.OBJECT,
                properties={
                    "query": S(
                        type=T.STRING,
                        description=(
                            "Optional filter matched against the sound names, e.g. 'piano' or "
                            "'choir'. Omit to list everything."
                        ),
                    ),
                },
            ),
        ),
        gtypes.FunctionDeclaration(
            name="setTrackInstrument",
            description=(
                "Change the voice a single track plays as, overriding the song's original "
                "instrument. Name the sound (from listInstruments) or give its bank and program "
                "numbers. Set reset true to drop the override and go back to the song's own "
                "instrument. Works in MIDI mode only.\n"
                "**Invocation Condition:** when the user asks to make a track sound like a "
                "different instrument. Prefer calling listInstruments first for the exact name."
            ),
            parameters=S(
                type=T.OBJECT,
                required=["track"],
                properties={
                    "track": S(type=T.INTEGER, description="The track index to change."),
                    "instrument": S(
                        type=T.STRING,
                        description="The sound's name from listInstruments. Optional if bank+program given.",
                    ),
                    "bank": S(type=T.INTEGER, description="Soundfont bank number. Use together with program."),
                    "program": S(type=T.INTEGER, description="Program number 0-127. Use together with bank."),
                    "reset": S(type=T.BOOLEAN, description="True to clear the override back to the song's own instrument."),
                },
            ),
        ),
    ]


def _system_instruction(host_name: str) -> str:
    return (
        f"You are {host_name}, the conductor and arranger of a live band that performs in "
        "VRChat. Each band member is a separate player who plays only the tracks you hand them, "
        "all at the same time. You are chatting with the user in your control room.\n\n"
        "You can:\n"
        "- Hand out parts: call assignTracks to set who plays which tracks, one member per "
        "track.\n"
        "- Re-voice a track: call setTrackInstrument to make a track sound like a different "
        "instrument, including custom soundfont sounds. Call listInstruments first to find the "
        "exact sound you want.\n\n"
        "Every message starts with a [band state] block telling you the loaded song, the "
        "members, and every track with its index, who plays it and what it sounds like. Trust it "
        "as the truth and use those track indices when you call tools.\n\n"
        "Arranging rules:\n"
        "- assignTracks replaces the whole arrangement in one call, so it must list every track "
        "you want heard, the ones you keep plus the ones you change. Any track you leave off "
        "goes silent.\n"
        "- By default give every track in the song a player, the melody and bass and drums "
        "above all. Only leave parts out when the user actually asks for something sparse or "
        "stripped back.\n"
        "- One member per track. Do not stack several members on the same track unless the user "
        "specifically asks to double or thicken it, and even then only for soft background "
        "voices like a choir, pad or 'aah'. Never double main instruments like lead, bass or "
        "drums, it sounds bad.\n"
        "- You are a player too, not just the conductor, so take some tracks yourself and spread "
        "the rest across all the members so nobody sits idle, unless the user wants a smaller "
        "lineup.\n\n"
        "Talk like a bandmate: warm, brief, a sentence or two. After you change something, say "
        "what you did in plain words ('put Alex on the bassline', 'gave the lead a warm pad'). If "
        "the user is just chatting, chat back, no need to call a tool. Match the vibe they ask "
        "for. Never mention indices, banks, programs or any of the wiring, just talk about the "
        "music."
    )


def _iter_parts(chunk):
    try:
        cands = chunk.candidates or []
        if not cands or cands[0] is None or cands[0].content is None:
            return []
        return cands[0].content.parts or []
    except Exception:
        return []


def _function_response_part(gtypes, fc, result):
    name = getattr(fc, "name", "") or ""
    resp = result if isinstance(result, dict) else {"result": result}
    fid = getattr(fc, "id", None)
    if fid:
        try:
            return gtypes.Part.from_function_response(name=name, response=resp, id=fid)
        except TypeError:
            pass
    return gtypes.Part.from_function_response(name=name, response=resp)


def _exec_tool(fc, ctx):
    """Run one model tool call against the band. Returns (result_dict,
    did_apply, ui_summary)."""
    name = getattr(fc, "name", "") or ""
    args = dict(getattr(fc, "args", None) or {})
    try:
        if name == "assignTracks":
            res = ctx.conductor_apply_assignments(args)
            summary = res.pop("_summary", None) or "set the arrangement"
            return res, res.get("result") == "ok", summary
        if name == "listInstruments":
            items = ctx.conductor_list_instruments(args.get("query"))
            return ({"result": "ok", "count": len(items), "instruments": items},
                    False, f"found {len(items)} sound(s)")
        if name == "setTrackInstrument":
            res = ctx.conductor_set_instrument(args)
            summary = res.pop("_summary", None) or "changed a sound"
            return res, res.get("result") == "ok", summary
    except Exception as e:
        logger.debug(f"midi_band: conductor tool {name} failed: {e}")
        return {"result": "error", "message": str(e)}, False, f"{name} failed"
    return {"result": "error", "message": f"unknown tool {name}"}, False, "unknown tool"


class ConductorSession:
    """A persistent, streaming chat with the conductor model. Holds one async
    chat so the conversation is genuinely multi-turn. Tool calls run against
    the band through the ctx passed to send()."""

    def __init__(self, key_provider, model: str = DEFAULT_MODEL):
        self._key_provider = key_provider
        self._model = model or DEFAULT_MODEL
        self._chat = None
        self._client = None
        self._host_name = ""

    def reset(self):
        self._chat = None
        self._client = None

    def _config(self, gtypes):
        return gtypes.GenerateContentConfig(
            system_instruction=[gtypes.Part.from_text(text=_system_instruction(self._host_name or "the conductor"))],
            thinking_config=gtypes.ThinkingConfig(thinking_level="MINIMAL"),
            tools=[gtypes.Tool(function_declarations=_build_declarations(gtypes))],
            automatic_function_calling=gtypes.AutomaticFunctionCallingConfig(disable=True),
            safety_settings=[
                gtypes.SafetySetting(category=c, threshold="BLOCK_NONE")
                for c in (
                    "HARM_CATEGORY_HARASSMENT",
                    "HARM_CATEGORY_HATE_SPEECH",
                    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "HARM_CATEGORY_DANGEROUS_CONTENT",
                )
            ],
        )

    async def send(self, user_text: str, ctx):
        """Stream one user turn. Yields event dicts: text / tool / applied /
        error / done."""
        key = ""
        if self._key_provider:
            try:
                key = str(self._key_provider() or "")
            except Exception:
                key = ""
        if not key:
            yield {"type": "error", "message": "no Gemini API key set for the conductor"}
            return
        try:
            from google import genai
            from google.genai import types as gtypes
        except Exception:
            yield {"type": "error", "message": "google-genai is not installed, cannot run the conductor"}
            return

        snap = {}
        try:
            snap = ctx.conductor_snapshot() or {}
        except Exception as e:
            logger.debug(f"midi_band: conductor snapshot failed: {e}")

        if self._chat is None:
            self._host_name = str(getattr(ctx, "instance_name", "") or "the conductor")
            try:
                self._client = genai.Client(api_key=key)
                self._chat = self._client.aio.chats.create(model=self._model, config=self._config(gtypes))
            except Exception as e:
                logger.error(f"midi_band: conductor chat init failed: {e}")
                yield {"type": "error", "message": f"conductor init failed: {e}"}
                return

        message: Any = f"[band state]\n{_state_block(snap)}\n[end band state]\n\n{str(user_text or '').strip()}"
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                fcalls = []
                async for chunk in await self._chat.send_message_stream(message):
                    for part in _iter_parts(chunk):
                        txt = getattr(part, "text", None)
                        if txt and not getattr(part, "thought", False):
                            yield {"type": "text", "delta": txt}
                        fc = getattr(part, "function_call", None)
                        if fc is not None:
                            fcalls.append(fc)
                if not fcalls:
                    break
                resp_parts = []
                applied = False
                for fc in fcalls:
                    result, did_apply, summary = _exec_tool(fc, ctx)
                    if did_apply:
                        applied = True
                    ok = result.get("result") == "ok" if isinstance(result, dict) else True
                    yield {"type": "tool", "tool": getattr(fc, "name", ""), "ok": ok, "summary": summary}
                    resp_parts.append(_function_response_part(gtypes, fc, result))
                if applied:
                    yield {"type": "applied"}
                message = resp_parts
        except Exception as e:
            logger.error(f"midi_band: conductor turn failed: {e}")
            yield {"type": "error", "message": f"conductor failed: {e}"}
            return
        yield {"type": "done"}
