# ProjectGabriel-Plugins -- Copilot Instructions

> **Owner:** HoppouAI
> **Repo:** ProjectGabriel-Plugins (public plugin collection)
> **Sister repos:**
> - Host app: [HoppouAI/ProjectGabriel-Remastered](https://github.com/HoppouAI/ProjectGabriel-Remastered)

## What this repo is

This repo is **only** plugins for Project Gabriel, a real-time VRChat AI
powered by Gemini Live. The host app lives in `ProjectGabriel-Remastered`.
Each top-level folder here is one self-contained plugin you can drop into
the host's `plugins/` directory.

There is no host code, no main entry point, no shared library here. Just
plugin folders side by side. The host loads them dynamically from disk,
this repo just stores and ships them.

```
ProjectGabriel-Plugins/
  diary/                  -- long term first person diary
  mood/                   -- persistent emotion + intensity
  example_hello/          -- minimal reference plugin
  README.md               -- public docs, install instructions, plugin list
  .gitignore
```

## Audience

People who want to:
1. Install a plugin into their existing Gabriel install (copy folder into
   `plugins/`, set `enabled: true`, optionally tweak `config.yml`).
2. Write their own plugin and possibly contribute it back via PR.

When acting as Copilot in this repo, default to assuming the user is in
case 2 (writing or maintaining a plugin) unless they say otherwise.

## Plugin layout (mandatory)

Every plugin folder MUST have:

```
<plugin_name>/
  plugin.yml          -- manifest, machine-readable
  __init__.py         -- entry module, must define a `Plugin` subclass and `plugin = MyPlugin`
  README.md           -- human-readable docs, what it does + config it reads
```

Optional:

```
  tools.py            -- BaseTool subclasses for Gemini function calls
  scheduler.py        -- background tick loop
  *.json.example      -- user-editable overrides (gitignored real file)
```

### plugin.yml

```yaml
name: my_thing            # MUST match folder name
version: 0.1.0            # semver
api_version: 2            # current host API version (2 adds ctx.discord)
author: YourName
description: one line description, shows up in logs and docs
enabled: true             # set to false if it should ship in the off state
requirements:             # optional, pip deps. Loader only WARNS if missing, never installs.
  - httpx>=0.25
  - imageio-ffmpeg
```

`name:` MUST match the folder name. The host uses the folder name to
look up the plugin's config under `plugins.<name>.*` and its data dir
at `data/plugins/<name>/`.

### __init__.py

```python
from src.plugins import Plugin, PluginContext

class MyThing(Plugin):
    name = "my_thing"
    version = "0.1.0"
    description = "does the cool thing"
    author = "YourName"

    def setup(self, ctx: PluginContext):
        ctx.logger.info("hello from my_thing")
        # register tools, providers, event subs, prompt contributors here

    def teardown(self, ctx: PluginContext):
        # close sockets, stop threads, save state
        pass

plugin = MyThing  # OR an instance: plugin = MyThing()
```

The host's loader looks for an attribute called `plugin` on the module
and calls `setup(ctx)` on it. Either a class or an instance works.

## The PluginContext API

Inside `setup(ctx)` you get a `PluginContext`. Full reference:

### Registration

| method | purpose |
|---|---|
| `ctx.register_tool(ToolClass)` | adds a Gemini function-calling tool |
| `ctx.register_tts(name, factory)` | custom TTS provider, picked via `tts.external_provider` |
| `ctx.register_stt(name, factory)` | custom STT provider |
| `ctx.register_chatbox_source(name, source, priority=100)` | add a VRChat chatbox display |
| `ctx.unregister_chatbox_source(name)` | remove one (rarely needed) |
| `ctx.register_prompt_contributor(name, fn)` | inject text into the system prompt every build |
| `ctx.unregister_prompt_contributor(name)` | remove one |
| `ctx.subscribe(event, callback)` | hook a lifecycle event (sync or async) |
| `ctx.discord` | sub-context for the Discord bot's separate gemini live session, see below |

### Runtime messaging

| method | purpose |
|---|---|
| `await ctx.send_system_instruction(text)` | push a mid-session system instruction to the model, same path the WebUI uses. wraps as `System instruction update - <text>` and waits for the model to stop speaking before injecting. session must be live. |
| `await ctx.send_user_text(text)` | inject a user-style text turn into the live session. model responds like any other user message. session must be live. |

Both return `True` on success, `False` if the live session isn't up yet
or sending failed. Don't call these inside `setup()` (no session yet),
use them inside a tool handler, an event subscriber, or any time after
`startup` fires.

```python
async def on_user_msg(text, source):
    if "shut up" in text.lower():
        await ctx.send_system_instruction("Stop talking until further notice.")

ctx.subscribe("message_in", on_user_msg)
```

### Reading state

| method / attr | purpose |
|---|---|
| `ctx.plugin_config(key=None, default=None)` | read `plugins.<name>.<key>` from `config.yml` |
| `ctx.data_dir() -> Path` | per-plugin data dir at `data/plugins/<name>/`, mkdir on demand |
| `ctx.logger` | `logging.getLogger("plugin.<name>")` |
| `ctx.audio` | `AudioManager` reference, **lazy** (None during setup, available after startup) |
| `ctx.osc` | VRChat OSC client, lazy |
| `ctx.session` | `GeminiLiveSession`, lazy |
| `ctx.tool_handler` | `ToolHandler`, lazy |

The `audio / osc / session / tool_handler` references are `None` while
`setup()` runs because the rest of the app is still spinning up. Read
them lazily inside a tool handler, a startup event, or after the first
`message_in` event.

## Tool classes

Tool classes extend `src.tools._base.BaseTool` (importable from the host
when the plugin is dropped into the host's `plugins/` folder).

```python
from google.genai import types
from src.tools._base import BaseTool

class MyTool(BaseTool):
    tool_key = "my_thing"   # groups this tool under plugin_tools.my_thing in tools.yml

    def declarations(self, config=None):
        return [types.FunctionDeclaration(
            name="doMyThing",
            description=(
                "One sentence about what it does.\n"
                "**Invocation Condition:** when to call this. "
                "Required for the model to know when to fire it."
            ),
            parameters={
                "type": "OBJECT",
                "properties": {
                    "arg1": {"type": "STRING", "description": "..."},
                },
                "required": ["arg1"],
            },
        )]

    async def handle(self, name, args):
        if name == "doMyThing":
            return {"result": "ok"}
        return None  # MUST return None for names you dont own
```

Rules:

1. Do **NOT** decorate plugin tool classes with `@register_tool`. That
   decorator is for built-in host tools. Plugins call
   `ctx.register_tool(cls)` instead.
2. `handle()` MUST be `async`.
3. `handle()` MUST return `None` for tool names it doesn't own. The
   dispatcher tries every tool until one returns non-None.
4. Every `FunctionDeclaration.description` MUST include
   `**Invocation Condition:**` so the model knows when to fire.
5. Returns are dicts. Keep them small and JSON serializable. The host
   wraps them as `FunctionResponse` automatically.

### The `cls(handler)` instantiation gotcha

The host's `ToolHandler` instantiates every registered tool as
`cls(handler)` with no other args. So you can't pass dependencies
through `__init__`. The mood plugin's pattern is the canonical
solution:

```python
class MyTools(BaseTool):
    _store = None  # class attribute, populated by the plugin before register

class MyPlugin(Plugin):
    def setup(self, ctx):
        store = MyStore(ctx.data_dir() / "state.json")
        MyTools._store = store          # stash BEFORE register
        ctx.register_tool(MyTools)
```

Then inside `handle()` use `self._store` (instance access falls through
to the class attribute). Same pattern works for schedulers, http
clients, anything you need at call time.

## Events

Built-in events the host fires:

| event | args | when |
|---|---|---|
| `startup` | `()` | after the Gemini Live session is up |
| `shutdown` | `()` | on graceful shutdown |
| `message_in` | `(text: str, source: str)` | every transcribed user message |
| `message_out` | `(text: str)` | every AI reply |

Subscribers can be sync or async. Exceptions in any handler are
caught so one bad subscriber cannot break the rest.

```python
async def on_user_msg(text, source):
    ctx.logger.info(f"user said: {text} ({source})")

ctx.subscribe("message_in", on_user_msg)
```

## Prompt contributors

`fn()` is called every time the system prompt is rebuilt (session
start, reconnect, personality switch). Return a string to append,
return `None` or empty string to skip this build.

```python
def mood_block():
    if not store.has_mood():
        return None
    return f"**Mood:** {store.emotion} @ {store.level}/10"

ctx.register_prompt_contributor("mood", mood_block)
```

Contributor text gets appended AFTER all built-in appends, so it's the
last thing the model reads in the system prompt.

## Chatbox sources

The VRChat chatbox is shared between built-in displays (local music at
priority 10, lyria at priority 20) and plugin displays. Lower priority
wins when multiple are active.

```python
class MyDisplay:
    def __init__(self, mgr):
        self.mgr = mgr
    def is_active(self) -> bool:
        return self.mgr.has_pending_alert
    def render(self) -> str | None:
        # 144 char max, the host paginates anything longer with (1/N) suffix
        return f"\u26a0 {self.mgr.alert_text[:140]}"

ctx.register_chatbox_source("my_alert", MyDisplay(mgr), priority=80)
```

`is_active()` also signals "busy" to the host, suppressing the idle
banner while it returns True.

### Chatbox source lifecycle (host API v2+)

The host now centralizes chatbox source management in a
`ChatboxOrchestrator`. The guarantees it gives plugin sources:

- Same text isnt re-sent every tick. The host dedupes and only resends
  when text changes or after a force-refresh interval (about 6 sec)
  to keep the chatbox alive.
- When you flip from `is_active() == True` to `False`, the host stops
  writing your text immediately and lets the idle banner take over.
  No stale text lingers.
- If your `is_active()` or `render()` raises 5 times in a row the
  host suspends your source for the rest of the run and logs a
  warning. Other sources keep working.
- A source returning `None` from `render()` while still active falls
  through to the next source instead of blanking the chatbox.

Optional new method: `on_clear()`. Fires once when this source loses
the chatbox to a different winner OR transitions to inactive with
nothing to take over. Use it for any teardown (closing a UI overlay,
resetting state). Safe to omit.

```python
class MyDisplay:
    def is_active(self): return self.mgr.has_alert
    def render(self): return self.mgr.alert_text[:140]
    def on_clear(self):
        self.mgr.dismiss_alert()  # auto-dismiss when nothing else takes over
```

## Discord bot integration (`ctx.discord`)

Project Gabriel ships with a Discord selfbot module that runs its own
separate Gemini Live session. Plugins can extend the bot the same way
they extend the main VRChat session, via `ctx.discord.*`. The existing
main-session methods (`ctx.register_tool`, etc) are unchanged and
still target VRChat only.

Main and Discord registries are independent so a plugin that registers
on both gets two separate instances. They dont share state unless you
thread that yourself.

| method | purpose |
|---|---|
| `ctx.discord.register_tool(ToolClass)` | attach a tool to the discord bot's tool handler |
| `ctx.discord.register_prompt_contributor(name, fn)` | inject text into the bot's system prompt every build |
| `ctx.discord.unregister_prompt_contributor(name)` | remove one |
| `ctx.discord.subscribe(event, callback)` | hook a Discord-scoped event |
| `await ctx.discord.send_system_instruction(text)` | inject SYSTEM INSTRUCTION style turn into the bot's session |
| `await ctx.discord.send_user_text(text)` | inject a user-style text turn |
| `ctx.discord.session` | the bot's `GeminiTextSession`, or None if offline |
| `ctx.discord.tool_handler` | the `DiscordToolHandler`, or None if offline |

Discord-scoped events:

| event | args | when |
|---|---|---|
| `bot_ready` | `(client)` | the discord client connects |
| `dm_received` | `(message)` | raw `discord.Message` for a DM / group DM |
| `mention_received` | `(message)` | raw `discord.Message` for a mention or reply |
| `message_sent` | `(channel_id: str, text: str)` | the bot replied |

Safe to call all `ctx.discord.*` methods from `setup()` even if the
bot is disabled in config. Registrations are kept and used if the bot
later starts. `send_*` returns `False` while the bot is offline.

Discord tools follow the same shape as the bot's built-in tools in
`discord_bot/tools/`. They get instantiated as `cls(handler)` where
handler is the `DiscordToolHandler`. Same `cls(handler)` gotcha as
main-session tools applies, use class attrs to stash deps.

```python
from src.plugins import Plugin

class DiaryPlugin(Plugin):
    name = "diary"

    def setup(self, ctx):
        # Same diary tool works on both sides, two instances
        ctx.register_tool(DiaryTool)
        ctx.discord.register_tool(DiaryTool)

        # Same prompt context lands in both prompts
        ctx.register_prompt_contributor("diary_today", self._today_summary)
        ctx.discord.register_prompt_contributor("diary_today", self._today_summary)

        # React to incoming Discord DMs
        self._ctx = ctx
        ctx.discord.subscribe("dm_received", self._on_dm)

    async def _on_dm(self, message):
        if "remember this" in message.content.lower():
            await self._ctx.discord.send_system_instruction(
                "User just asked you to remember the last DM verbatim."
            )
```

## Per-plugin data

Use `ctx.data_dir()` for any state you want to persist:

```python
state_path = ctx.data_dir() / "state.json"
# data/plugins/<name>/state.json
```

Do NOT write outside `data/plugins/<name>/`. Anything else goes in the
host's gitignored data tree but is not your business.

## Per-plugin config

```yaml
# in config.yml
plugins:
  enabled: true              # master switch for the whole loader
  my_thing:
    api_key: "abc123"
    threshold: 0.5
    interval_seconds: 600
```

```python
api_key = ctx.plugin_config("api_key")
threshold = ctx.plugin_config("threshold", default=0.5)
all_my_config = ctx.plugin_config()  # whole dict
```

`config.yml` is the user's runtime knobs. Whether the plugin LOADS at
all is set by `enabled:` inside `plugin.yml`, NOT here. There is a
legacy `plugins.<name>.enabled` fallback for upgraders, but new plugins
should rely on the manifest.

## Per-tool toggles

After first run, every plugin tool ends up listed in
`config/tools.yml` under `plugin_tools.<plugin>.<tool_name>: true`.
The user can flip any of those to `false` to hide a single tool from
the model without disabling the whole plugin. The host auto-populates
this file every startup.

You don't need to do anything special for this to work, just register
your tools and they'll show up.

## Lifecycle order

```
host startup
  -> load plugins (this code runs setup(ctx))   <- ctx.audio / .osc / .session are None
  -> sync_tools_yml writes plugin_tools entries
  -> Gemini Live session connects (this is when 'startup' event fires)
  -> normal runtime
host shutdown
  -> 'shutdown' event fires
  -> teardown(ctx) on every plugin
```

So: do NOT touch `ctx.audio / .osc / .session / .tool_handler` inside
`setup()`. They're not wired yet. Use them lazily (inside a tool
handler, a `startup` subscriber, or any time after the session
connects).

## Style rules (matches host repo)

1. **No em dashes in code or comments.** Double dashes are fine in
   chat / docs, but in code use commas, periods, or natural phrasing.
2. **Casual, human tone in code comments.** Occasional typos are fine.
   Don't write polished AI-style essays in docstrings.
3. **Minimal comments.** One short line for the WHY when it's not
   obvious. Don't restate what the code does. No multi-paragraph
   docstrings.
4. **Don't add features not asked for.** If the user asks for a tool
   that does X, build a tool that does X. No extra "while I'm here"
   helpers, validation, abstractions.
5. **Don't add docstrings or type annotations to code you didn't
   change.**
6. **Match the existing tone and naming.** Look at `mood/` and `diary/`
   for canonical examples before writing new code.
7. **No `print()`.** Use `ctx.logger` or
   `logging.getLogger(__name__)`.
8. **No hardcoded secrets.** Pull api keys / urls from `ctx.plugin_config`.

## Style rules (vocabulary)

When writing tool descriptions and the AI's responses to tool calls,
**never leak implementation details to the model**. The diary plugin
learned this the hard way. Bad words to avoid in tool descriptions
that the AI sees:

- "background agent"
- "scheduler"
- "process"
- "system" (when referring to your own plugin)
- "subagent"
- "automatically updates"
- "the diary system" / "the mood system"

Frame everything as a thing the AI **itself** does. Eg "your diary",
"how you feel", "the song you started". The AI then narrates it as
its own action instead of leaking a pipeline.

## Privacy

Some host features are gated behind `privacy.save_conversations: true`
in the host config. The diary plugin requires it (no transcripts on
disk = nothing to summarize). If your plugin reads
`data/conversations/` you MUST check that flag and degrade gracefully
when it's off (return empty results, log a warning, never crash).

## Testing locally

There's no test harness in this repo. To test a plugin:

1. Symlink or copy the folder into a working
   `ProjectGabriel-Remaster` install:
   ```powershell
   New-Item -ItemType SymbolicLink `
            -Path E:\path\to\Remaster\plugins\my_thing `
            -Target E:\path\to\Plugins\my_thing
   ```
2. Make sure `enabled: true` in `plugin.yml`.
3. Run the host: `python supervisor.py` from the Remaster repo.
4. Watch the log for `loaded plugin 'my_thing' v0.1.0`.
5. Open `config/tools.yml` and confirm any tools landed under
   `plugin_tools.my_thing`.
6. Talk to Gabriel and make sure the model can call your tool.

## Submitting a plugin

PRs to this repo are welcome. Checklist:

- [ ] Folder name matches `name:` in `plugin.yml`
- [ ] `plugin.yml` has all required fields (`name`, `version`,
      `api_version`, `author`, `description`, `enabled`)
- [ ] `__init__.py` defines a `Plugin` subclass and exports `plugin`
- [ ] `README.md` in the plugin folder explains what it does, what
      config it reads, any external services it needs
- [ ] No bundled secrets / api keys / personal data
- [ ] No bundled wheels / binaries (pip deps go in `requirements:`)
- [ ] AGPL-3.0 compatible (matches host license)
- [ ] If it depends on a private/external service, say so up front in
      the README (it's fine to ship a "you need X to use this" plugin,
      just be honest about it)
- [ ] If the plugin can't be run publicly at all, send it to
      ProjectGabriel-Plugins-Private instead

## What NOT to do

- Don't import host modules at the top of `__init__.py` if they might
  not exist. The host imports the plugin module before it imports its
  own code in some loader paths. Use lazy imports inside `setup()`.
- Don't block in `setup()`. No HTTP calls, no file globs over big
  trees, no `time.sleep()`. The loader runs all plugins serially.
- Don't write outside `data/plugins/<name>/`.
- Don't shell out for things you can do with Python stdlib.
- Don't fight the host's lifecycle. If you need to do work on
  startup, subscribe to the `startup` event.
- Don't modify `config/tools.yml` from a plugin. The host owns that
  file. Tools are auto-registered by name.
- Don't add per-tool feature flags inside your tool's
  `declarations()` -- if the user disables the tool in
  `config/tools.yml` the host filters it out before the schema even
  reaches Gemini.

## When updating an existing plugin

- Bump `version` in `plugin.yml` (semver, breaking goes to a new
  major).
- If you change the manifest format or use a new host hook, also bump
  `api_version`.
- Update the plugin's own `README.md` if behavior or config changed.
- Update this repo's top-level `README.md` if the plugin's one-liner
  changes.
- Commit messages: short, lowercase, casual, mention the plugin name
  first. eg `diary: stop leaking 'scheduler' in tool descriptions`.

## Reference plugins, in order of complexity

1. `example_hello/` -- bare minimum, one tool, two event subs.
2. `mood/` -- persistent state, prompt contributor, custom JSON
   overrides.
3. `diary/` -- background scheduler, sub-agent calling another Gemini
   model, structured-output via response schema, multiple tools.

Read those before asking how to do anything. Most patterns are already
demonstrated there.
