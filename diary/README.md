# Diary plugin

Long term life diary for Gabriel. A background sub-agent reads recent VRChat
session transcripts every couple hours and writes a first person diary entry
to a custom `.diary` file. The AI gets tools to read its own diary back when
it needs context the structured memory system would not capture.

## What it does

- Background scheduler runs every 2 hours (configurable) inside the host event loop.
- Each tick gathers the **last N session transcripts from today** (default 5) from `data/conversations/`.
- Passes them to `gemini-3.1-flash-lite` (configurable) along with any
  earlier diary entries from today, so the new entry builds forward instead of
  repeating itself.
- Writes a structured entry to `data/plugins/diary/gabriel.diary`.
- A diary "day" can have multiple "parts" (one per scheduler tick that wrote
  something new), so a busy day looks like:

```
=== 2026-05-08 part 1 (written May 8, 2026 at 2:30:11 PM) ===
...

=== 2026-05-08 part 2 (written May 8, 2026 at 4:30:42 PM) ===
...
```

## Recent days are always in his head

Reading tools alone weren't enough, the AI rarely went and checked his diary
unless you told him to, so he kept forgetting recent context. Now the plugin
injects his **most recent entries straight into the system prompt** on every
build (the same way the `mood` plugin injects mood). So his latest days are
always in front of him without needing a tool call, and he references them
naturally. The read tools are still there for digging into OLDER days. Their
descriptions also now push him to search the diary before claiming he forgot a
person or moment.

This applies to both the VRChat session and the Discord bot's session.

## A screenshot rides along with each entry

When it's time to journal, the plugin grabs **one** screenshot of what Gabriel
is looking at right then (`ctx.capture_vision_frame()`, host api v4) and hands
it to the diarizer model alongside the transcripts. Just a single frame per
entry, not a stream, so it's cheap. The diarizer actually writes about it,
usually a present-tense "right now, as I write this..." bit near the end
describing where he is, the world or room around him, who's nearby, and the
vibe, while being told to treat it as his current view, not as proof of what
happened earlier. The grab is lazy: it only fires once the plugin already
knows it's going to write something, and it soft-fails to no image if capture
isn't available, so a missing frame never blocks an entry. Set
`attach_frame: false` to turn it off.

## Tools the AI can call

| name | purpose |
|---|---|
| `readDiary` | read entries by date or get the most recent N |
| `searchDiary` | substring search across all entries |
| `listDiaryDates` | list every date that has at least one entry |
| `updateDiaryNow` | force the scheduler to run a tick immediately |

## Config (optional, all fields default)

```yaml
plugins:
  diary:
    enabled: true
    interval_hours: 2          # how often the background scheduler runs
    max_sessions: 5            # how many recent today-sessions to summarize per tick
    model: "gemini-3.1-flash-lite"
    initial_delay_seconds: 300 # warmup delay after startup before first tick
    filename: "gabriel.diary"  # name of the diary file under data/plugins/diary/
    conversation_dir: "data/conversations"  # where session transcripts live
    inject_recent: true        # put his most recent entries in the system prompt every build
    prompt_recent_entries: 2   # how many recent entries to inject
    prompt_body_chars: 500     # max body chars per injected entry (keeps the prompt small)
    attach_frame: true         # grab one screenshot at write time and send it to the diarizer (host api v4)
    frame_max_size: null       # optional max image dimension, null uses the host vision default
    frame_quality: null        # optional JPEG quality, null uses the host vision default
```

## File format

Plain text, easy to open in any editor. Each entry is bracketed by
`=== DATE part N (written FRIENDLY_TIMESTAMP) ===` and `=== END ===` markers,
with a small metadata block, the body paragraphs, and an optional bullet point
highlights list. Timestamps use US 12-hour format (e.g. `May 8, 2026 at 2:30:11 PM`)
to match what the AI's time tool returns, so the model doesnt get confused
flipping between 24h and 12h. Parser is lenient: hand edits and missing fields are fine.

## Notes

- Requires `privacy.save_conversations: true` in the main `config.yml`,
  otherwise no transcripts are written and the diary stays empty.
- **Requires `plugins.trusted: true` in `config.yml`.** The diary
  sub-agent reuses the host's gemini api key, which is sandboxed
  away from plugins by default. Without trust mode you'll see
  `plugin 'diary' is not allowed to read sensitive config attribute
  'api_key'` in the logs and the scheduler will skip every tick.
- The diary is meant to capture **vibes and threads** that the structured
  memory tools miss. Names, ongoing jokes, how people made you feel.
- The plugin never edits or deletes past entries, only appends new ones.
- API key comes from the same `Config.api_key` rotation used by the main session.
