"""Diary plugin entry.

Spins up a DiaryStore + DiaryScheduler, registers four tools (read, search,
list, force-update). Background scheduler ticks every couple hours and feeds
the most recent VRChat session transcripts through gemini-3.1-flash-lite
to produce a first person diary entry, appended to `data/plugins/diary/gabriel.diary`.
"""
from __future__ import annotations

import logging
from pathlib import Path

from src.plugins import Plugin, PluginContext

from .diary import DiaryStore, format_recent_for_prompt
from .scheduler import DiaryScheduler
from .summarizer import DEFAULT_MODEL
from .tools import DiaryTools

logger = logging.getLogger(__name__)

# fallback location, must match src.gemini_live.conversation_logger.CONVERSATION_DIR
_DEFAULT_CONV_DIR = Path("data/conversations")
_DEFAULT_INTERVAL_HOURS = 2.0
_DEFAULT_MAX_SESSIONS = 5
_DEFAULT_INITIAL_DELAY_SECONDS = 300.0
_DEFAULT_PROMPT_ENTRIES = 2
_DEFAULT_PROMPT_BODY_CHARS = 500


class DiaryPlugin(Plugin):
    name = "diary"
    version = "1.3.0"
    description = "Background diary writer + read tools, summarizes recent VRChat sessions into a long term first person diary."
    author = "HoppouAI"

    def setup(self, ctx: PluginContext):
        # config knobs all live under plugins.diary.* in config.yml
        interval_hours = float(ctx.plugin_config("interval_hours", _DEFAULT_INTERVAL_HOURS) or _DEFAULT_INTERVAL_HOURS)
        max_sessions = int(ctx.plugin_config("max_sessions", _DEFAULT_MAX_SESSIONS) or _DEFAULT_MAX_SESSIONS)
        model = str(ctx.plugin_config("model", DEFAULT_MODEL) or DEFAULT_MODEL)
        initial_delay = float(ctx.plugin_config("initial_delay_seconds", _DEFAULT_INITIAL_DELAY_SECONDS) or _DEFAULT_INITIAL_DELAY_SECONDS)
        diary_filename = str(ctx.plugin_config("filename", "gabriel.diary") or "gabriel.diary")
        conv_dir_str = ctx.plugin_config("conversation_dir")
        conv_dir = Path(conv_dir_str) if conv_dir_str else _DEFAULT_CONV_DIR

        # how many recent entries to keep injected into the system prompt each
        # build. this is the bit that makes him actually remember recent days
        # without waiting to be told to go read his diary.
        inject_recent = bool(ctx.plugin_config("inject_recent", True))
        prompt_entries = int(ctx.plugin_config("prompt_recent_entries", _DEFAULT_PROMPT_ENTRIES) or _DEFAULT_PROMPT_ENTRIES)
        prompt_body_chars = int(ctx.plugin_config("prompt_body_chars", _DEFAULT_PROMPT_BODY_CHARS) or _DEFAULT_PROMPT_BODY_CHARS)

        # one screenshot of what he's looking at gets grabbed the moment he sits
        # down to journal and handed to the diarizer for visual color. single
        # frame per entry, not a stream, so it stays cheap. needs host api v4.
        attach_frame = bool(ctx.plugin_config("attach_frame", True))
        frame_max_size = ctx.plugin_config("frame_max_size", None)
        frame_quality = ctx.plugin_config("frame_quality", None)

        store = DiaryStore(ctx.data_dir() / diary_filename)
        # ToolHandler instantiates with cls(handler) so we cant pass the store
        # via __init__, stash it as a class attr the same way mood does.
        DiaryTools._store = store

        def _resolve_persona() -> str:
            """Pull the active base persona from prompts.yml fresh each tick,
            so prompt edits take effect without restarting. Mirrors the lookup
            in Config.build_system_instruction() but skips appends/memories,
            the diary only wants the raw character voice."""
            cfg = ctx.config
            if cfg is None:
                return ""
            try:
                prompt_name = cfg.get("gemini", "prompt", default="normal")
                raw = (getattr(cfg, "_prompts", {}) or {}).get(prompt_name, "")
                if isinstance(raw, dict):
                    return str(raw.get("prompt", "")).strip()
                return str(raw or "").strip()
            except Exception as e:
                ctx.logger.warning(f"diary: failed to resolve persona: {e}")
                return ""

        async def _grab_frame():
            """Grab a single current screenshot for the diarizer. Returns JPEG
            bytes or None. Soft-fails on anything (host without api v4, capture
            error) so a missing frame never blocks an entry."""
            fn = getattr(ctx, "capture_vision_frame", None)
            if fn is None:
                return None
            kwargs = {}
            if frame_max_size is not None:
                kwargs["max_size"] = int(frame_max_size)
            if frame_quality is not None:
                kwargs["quality"] = int(frame_quality)
            try:
                return await fn(**kwargs)
            except Exception as e:
                ctx.logger.warning(f"diary: capture_vision_frame failed: {e}")
                return None

        scheduler = DiaryScheduler(
            store=store,
            conv_dir=conv_dir,
            get_api_key=lambda: getattr(ctx.config, "api_key", "") or "",
            interval_seconds=interval_hours * 3600,
            max_sessions=max_sessions,
            model=model,
            initial_delay_seconds=initial_delay,
            get_persona=_resolve_persona,
            capture_frame=_grab_frame if attach_frame else None,
        )
        DiaryTools._scheduler = scheduler
        ctx.register_tool(DiaryTools)
        # also expose the diary tools to the Discord bot's gemini session
        # so it can answer "how was your week" type questions over DMs.
        # Tool gets a separate instance there but shares the same _store
        # class attr so reads stay consistent.
        try:
            ctx.discord.register_tool(DiaryTools)
        except Exception as e:
            ctx.logger.warning(f"diary: discord tool register failed: {e}")

        # inject his most recent diary entries straight into the system prompt
        # every build so recent days are always in mind. this is the reliable
        # fix for "he never checks the diary unless told", now he doesnt have
        # to call a tool to remember the latest stuff, it's already there.
        if inject_recent:
            def _recent_block():
                return format_recent_for_prompt(
                    store, max_entries=prompt_entries, body_chars=prompt_body_chars
                )

            ctx.register_prompt_contributor("diary_recent", _recent_block)
            # mirror onto the discord bot's prompt too so DMs get the same recall
            try:
                ctx.discord.register_prompt_contributor("diary_recent", _recent_block)
            except Exception as e:
                ctx.logger.warning(f"diary: discord prompt contributor register failed: {e}")

        # start the background loop once the host's asyncio loop is up
        def _on_startup():
            scheduler.start()

        ctx.subscribe("startup", _on_startup)
        ctx.subscribe("shutdown", lambda: scheduler.stop())

        # keep handles for debug pokes
        self._store = store
        self._scheduler = scheduler

        ctx.logger.info(
            f"diary plugin ready, interval={interval_hours}h, max_sessions={max_sessions}, "
            f"model={model}, file={store.path}, "
            f"inject_recent={inject_recent} (entries={prompt_entries}), attach_frame={attach_frame}"
        )

    def teardown(self, ctx: PluginContext):
        try:
            if hasattr(self, "_scheduler") and self._scheduler is not None:
                self._scheduler.stop()
        except Exception as e:
            ctx.logger.warning(f"diary teardown failed: {e}")


plugin = DiaryPlugin
