# Project Gabriel Plugins

Drop-in plugins for [Project Gabriel](https://github.com/HoppouAI/ProjectGabriel-Remastered),
the VRChat AI from [Hoppou.AI](https://hoppou.ai/).

This repo is just the plugins. Each folder here is a self contained plugin
you can drop into your Gabriel install's `plugins/` folder and it gets picked
up on next startup. If you'd like your plugin featured here, open a pull
request.

**Discord:** [discord.gg/ZNWTYTk4Vq](https://discord.gg/ZNWTYTk4Vq)

---

## What is a plugin?

Plugins extend Gabriel without touching the core code. A plugin can:

- Register Gemini function-calling tools the AI can use mid conversation
- Add custom TTS / STT providers
- Write to the VRChat chatbox (now playing displays, status banners)
- Inject extra text into the system prompt every session
- Hook lifecycle events (`startup`, `shutdown`, `message_in`, `message_out`)
- Persist its own state under `data/plugins/<name>/`

The full author guide lives in the host repo at
[plugins/README.md](https://github.com/HoppouAI/ProjectGabriel-Remastered/blob/main/plugins/README.md).
Read that first if you want to write your own.

---

## Installing a plugin

Plugins live in the `plugins/` folder of your Gabriel install (the
`ProjectGabriel-Remaster` repo, NOT this one). Easiest workflow is to
clone this repo right next to your Gabriel install so the paths stay
short:

```
your-projects/
  ProjectGabriel-Remaster/    <- your gabriel install
  ProjectGabriel-Plugins/     <- this repo, cloned next to it
```

Then from a PowerShell terminal inside `ProjectGabriel-Remaster/`:

1. **Copy the plugin folder** you want into your `plugins/` dir.

   ```powershell
   # one liner, swap `diary` for whatever plugin you want
   Copy-Item -Recurse -Force ..\ProjectGabriel-Plugins\diary plugins\
   ```

   To install all of them in one go:

   ```powershell
   Get-ChildItem ..\ProjectGabriel-Plugins -Directory |
     Where-Object { Test-Path "$($_.FullName)\plugin.yml" } |
     ForEach-Object { Copy-Item -Recurse -Force $_.FullName plugins\ }
   ```

2. **Make sure it's enabled.** Open `plugins\<name>\plugin.yml` and
   confirm `enabled: true` (it usually already is).

3. **Install python deps**, if the plugin's `plugin.yml` lists any
   under `requirements:`. Gabriel ships `uv` in `bin\` so you don't
   need a system pip:

   ```powershell
   # one package
   .\bin\uv.exe pip install resemblyzer

   # everything for a specific plugin in one shot
   .\bin\uv.exe pip install (Get-Content plugins\voiceid\plugin.yml |
       Select-String '^\s*-\s' | ForEach-Object { ($_ -split '-\s+')[1].Trim() })
   ```

   The host warns about missing deps on startup but never
   auto-installs. A lot of plugins ship with no extra deps.

4. **Optional config.** Each plugin's own README lists its knobs.
   Add them under `plugins.<name>:` in your `config.yml`. Plugins
   that need raw access to the host's gemini key (like `diary`) also
   need `plugins.trusted: true`, see the trust mode section below.

5. **Restart Gabriel.** You should see something like:

   ```
   [plugins] loaded plugin 'diary' v1.0.0
   ```

   in the log. If the plugin registers tools, they show up in
   `config\tools.yml` under `plugin_tools.<name>` set to `true` after
   the first run.

To remove a plugin: delete its folder under `plugins\` and restart.
To temporarily disable one without deleting: flip `enabled: false` in
its `plugin.yml`.

### Updating

Plugins update by re-copying from this repo. Run `git pull` here,
then re-run the copy command from step 1. Your plugin config in
`config.yml` stays put.

### Per tool toggles

After first run, every plugin tool ends up listed in `config/tools.yml`
under `plugin_tools.<plugin>.<tool_name>`. Flip any of those to `false`
to hide a single tool from the model without disabling the whole
plugin.

### Trust mode (for plugins that need the host api key)

Gabriel sandboxes `ctx.config` by default. Plugins can read their own
settings under `plugins.<name>.*` via `ctx.plugin_config()` but reads
of sensitive things like `ctx.config.api_key`, mongo strings, vrchat
password or discord token raise `PermissionError`.

A handful of plugins here (notably `diary/`) reuse the main gemini
key for a background sub-agent. To enable those, set:

```yaml
plugins:
  enabled: true
  trusted: true
```

Default is `false`. Only flip this on if you trust every plugin in
your `plugins/` folder, the toggle is global. Plugins should mention
in their own README if they need it.

---

## Plugins in this repo

### [`diary/`](diary/) -- Long term first person diary

A background sub-agent reads recent VRChat session transcripts every couple
of hours and writes a first person diary entry to a custom `.diary` file. The
AI gets tools to read its own diary back when it needs context the structured
memory system would not capture (vibes, ongoing jokes, how people made it
feel).

- Model: `gemini-3.1-flash-lite-preview` (configurable)
- Schedule: every 2 hours, after a 5 minute warmup
- File: `data/plugins/diary/gabriel.diary` (plain text, hand-editable)
- Tools: `readDiary`, `searchDiary`, `listDiaryDates`, `updateDiaryNow`
- Requires: `privacy.save_conversations: true` in main config

### [`mood/`](mood/) -- Persistent emotion + intensity

Two dimensional mood system. The AI has an `emotion` (happy, sad, scared,
angry, amused, lonely, ...) and an `intensity` from 1 to 10. Both get
injected into the system prompt at every session start, and the AI can
change its own mood mid-conversation by calling `setMood`. Mood persists
across restarts.

- Built in emotions: 21 (overridable via `emotions.json`)
- Intensity scale: 1-10 (overridable via `moods.json`)
- File: `data/plugins/mood/state.json`
- Tool: `setMood`

### [`duo_song/`](duo_song/) -- LAN duet, two halves of one song

Lets two Gabriel instances on the same local network sing a duet
together. Each duet song is a **pair of audio files**, one per singer:
`SongName PT1.mp3` and `SongName PT2.mp3` dropped into `sfx/music/duo/`
on both machines. The host plays PT1, the partner plays PT2, both
trigger at the exact same moment via a small TCP handshake plus a fast
ping/pong clock sync (typical drift under ~30ms on a quiet LAN).

- Audio: `pygame.mixer` (mp3 / ogg / wav / flac)
- Library: `sfx/music/duo/`, files named `<title> PT1.<ext>` and
  `<title> PT2.<ext>` (separators space, underscore, dash, dot all fine)
- Tools: `startDuoSong`, `stopDuoSong`, `listDuoSongs`, `duoStatus`
- Requires: `pygame>=2.5`

### [`midi_band/`](midi_band/) -- Multi-instance MIDI band

Turns a group of Gabriel instances on the same LAN into a live band.
The host loads a MIDI file, assigns tracks to bandmates (drums to one,
bass to another, lead to itself, etc), and on `startMidiBand` every
bandmate plays their assigned tracks at the exact same moment via
fluidsynth + a soundfont. A standalone client ships in the same folder
so non-Gabriel users can join the band too.

- Synthesis: `pyfluidsynth` + a `.sf2` soundfont (per machine)
- Library: `sfx/midi/` on the host, clients receive files on demand
- Tools: `listMidiSongs`, `loadMidiSong`, `listBandMembers`,
  `autoAssignBandTracks`, `assignBandTracks`, `startMidiBand`,
  `stopMidiBand`, `bandStatus`
- Standalone client: see [`midi_band/standalone/`](midi_band/standalone/) (uv / pip)
- Requires: `mido>=1.3`, `pyfluidsynth>=1.3`, native fluidsynth library

### [`example_hello/`](example_hello/) -- Reference plugin

Minimal demo. Registers a `sayHello` tool and hooks `startup` / `shutdown`
events. Disabled by default, flip `enabled: true` in its `plugin.yml` to
see it in action. Read this one first if you're learning the API.

---

## Contributing a plugin

PRs welcome. Rough rules:

1. One folder per plugin at the repo root.
2. Must include a `plugin.yml` manifest with `name`, `version`,
   `api_version`, `author`, `description`, `enabled`.
3. Include a short `README.md` in the plugin folder explaining what it
   does, what config it reads, and any external services it depends on.
4. List any pip deps in `plugin.yml :: requirements:` (do not bundle
   wheels or binaries).
5. Do not commit user data or secrets. `data/` style runtime state is
   the host's responsibility.
6. Keep it AGPL-3.0 compatible (matches the host project).

If your plugin needs a private or external service, say so up front
   in the README. It's fine to ship a plugin that only works for some
   users, just be honest about it.

---

## License

Same license as the host project: GNU Affero General Public License v3.0.
See [LICENSE](https://github.com/HoppouAI/ProjectGabriel-Remastered/blob/main/LICENSE)
in the host repo for details.
