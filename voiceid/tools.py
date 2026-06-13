"""Tools the AI can call to manage and look up voice fingerprints."""
from google.genai import types

from src.tools._base import BaseTool


class VoiceIDTools(BaseTool):
    tool_key = "voiceid"
    # set by the plugin entry before ToolHandler instantiates the class
    _recognizer = None

    def __init__(self, handler):
        super().__init__(handler)

    def declarations(self, config=None):
        return [
            types.FunctionDeclaration(
                name="saveVoice",
                description=(
                    "Remember the voice currently being spoken so you can identify them later. "
                    "Captures whatever speech is in your recent audio buffer (last few seconds), "
                    "turns it into a speaker fingerprint, and saves it under the given username.\n\n"
                    "**Invocation Condition:** Call this when someone tells you their name and "
                    "they are actively speaking, OR when you have visually identified who is "
                    "talking and want to remember their voice for next time. Call again with the "
                    "same name from a different moment to add another capture, multiple captures "
                    "of the same person make recognition much more reliable. Do not call this "
                    "without enough recent speech, the tool will tell you if the buffer is too short."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "username": {
                            "type": "STRING",
                            "description": "Name to save the voice under. Use the name the person actually goes by.",
                        },
                    },
                    "required": ["username"],
                },
            ),
            types.FunctionDeclaration(
                name="identifyCurrentSpeaker",
                description=(
                    "Identify the speaker of the current speech using saved voice fingerprints. "
                    "Returns either a confident match (username + confidence) or unknown with "
                    "a reason. If unknown, treat them as unknown, do not guess based on context. "
                    "Just ask their name and call saveVoice when they answer.\n\n"
                    "**Invocation Condition:** Call this every time someone speaks to you to "
                    "verify who is speaking. Do not skip it, you need to know who is addressing "
                    "you for every turn."
                ),
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="listSavedVoices",
                description=(
                    "List every voice fingerprint you've saved, with how many samples each has.\n\n"
                    "**Invocation Condition:** Call when you want to know who you can recognize "
                    "by voice, or when the user asks who you remember."
                ),
                parameters={"type": "OBJECT", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="forgetVoice",
                description=(
                    "Delete a saved voice fingerprint.\n\n"
                    "**Invocation Condition:** Call when the user asks you to forget someone's "
                    "voice, or when a saved fingerprint is clearly bad and you want a clean slate "
                    "to re-record."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "username": {
                            "type": "STRING",
                            "description": "Name of the saved voice to delete.",
                        },
                    },
                    "required": ["username"],
                },
            ),
            types.FunctionDeclaration(
                name="renameVoice",
                description=(
                    "Rename a saved voice fingerprint, e.g. you saved someone as their VRChat "
                    "display name and now want to use their real name.\n\n"
                    "**Invocation Condition:** Call when the user explicitly asks to rename "
                    "one of your saved voices."
                ),
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "old_name": {"type": "STRING", "description": "Current name."},
                        "new_name": {"type": "STRING", "description": "New name."},
                    },
                    "required": ["old_name", "new_name"],
                },
            ),
        ]

    async def handle(self, name, args):
        rec = self._recognizer
        if rec is None:
            return {"error": "voiceid recognizer not initialized"}
        if name == "saveVoice":
            return rec.save_voice(str(args.get("username", "")))
        if name == "identifyCurrentSpeaker":
            return rec.identify_current()
        if name == "listSavedVoices":
            return {"voices": rec.list_voices(), "count": len(rec.list_voices())}
        if name == "forgetVoice":
            return rec.forget_voice(str(args.get("username", "")))
        if name == "renameVoice":
            return rec.rename_voice(str(args.get("old_name", "")), str(args.get("new_name", "")))
        return None
