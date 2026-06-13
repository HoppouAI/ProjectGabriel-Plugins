<div align="center">

# Project Gabriel Plugins

**Drop in plugins for [Project Gabriel](https://github.com/HoppouAI/ProjectGabriel-Remastered),
the real time VRChat AI from [Hoppou.AI](https://hoppou.ai/).**

[![Host repo](https://img.shields.io/badge/host-ProjectGabriel--Remastered-1f6feb?style=for-the-badge&logo=github&logoColor=white)](https://github.com/HoppouAI/ProjectGabriel-Remastered)
[![Discord](https://img.shields.io/badge/discord-join-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/ZNWTYTk4Vq)
[![Website](https://img.shields.io/badge/site-hoppou.ai-ff66c4?style=for-the-badge)](https://hoppou.ai/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-d22128?style=for-the-badge)](https://github.com/HoppouAI/ProjectGabriel-Remastered/blob/main/LICENSE)
[![Plugins](https://img.shields.io/badge/plugins-8-9333ea?style=for-the-badge)](#plugin-matrix)
[![API](https://img.shields.io/badge/api__version-2--3-2ea44f?style=for-the-badge)](#)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen?style=for-the-badge)](#contributing-a-plugin)

</div>

---

This repo is just plugins. Each top level folder is one self contained
plugin you can drop into your Gabriel install's `plugins/` folder and
it gets picked up on next startup. The host code itself lives in the
[ProjectGabriel-Remastered](https://github.com/HoppouAI/ProjectGabriel-Remastered)
repo. Want yours featured here? Open a PR.

> [!TIP]
> New here? Skim **[What is a plugin?](#what-is-a-plugin)** then jump
> to the **[plugin matrix](#plugin-matrix)** to see what's on offer.

---

## Table of contents

- [What is a plugin?](#what-is-a-plugin)
- [Plugin matrix](#plugin-matrix)
- [Plugins in detail](#plugins-in-detail)
  - [`diary/`](#diary--long-term-first-person-diary)
  - [`mood/`](#mood--persistent-emotion--intensity)
  - [`duo_song/`](#duo_song--lan-duet-two-halves-of-one-song)
  - [`midi_band/`](#midi_band--multi-instance-midi-band)
  - [`pocket_tts/`](#pocket_tts--local-cpu-tts-via-kyutai-pocket-tts)
  - [`omnivoice_tts/`](#omnivoice_tts--gpu-tts-via-k2-fsa-omnivoice)
  - [`voiceid/`](#voiceid--voice-fingerprinting-and-recognition)
  - [`example_hello/`](#example_hello--reference-plugin)
- [Installing plugins](#installing-plugins)
- [Updating, removing, disabling](#updating-removing-disabling)
- [Per tool toggles](#per-tool-toggles)
- [Trust mode](#trust-mode-for-plugins-that-need-the-host-api-key)
- [Contributing a plugin](#contributing-a-plugin)
- [License](#license)

---

## What is a plugin?

Plugins extend Gabriel without touching the core code. A plugin can:

| | Capability |
|---|---|
| **Tools** | Register Gemini function-calling tools the AI can use mid conversation |
| **TTS / STT** | Add custom text to speech or speech to text providers |
| **Chatbox** | Write to the VRChat chatbox (now playing displays, status banners) |
| **Prompt** | Inject extra text into the system prompt every session |
| **Events** | Hook lifecycle events (`startup`, `shutdown`, `message_in`, `message_out`) |
| **Discord** | Extend the optional Discord selfbot via `ctx.discord.*` (api v2+) |
| **State** | Persist their own data under `data/plugins/<name>/` |

Author guide for writing your own:
[plugins/README.md in the host repo](https://github.com/HoppouAI/ProjectGabriel-Remastered/blob/main/plugins/README.md).
Read that first.

---

## Plugin matrix

| Plugin | Type | What it does | Headline |
|---|---|---|---|
| [`diary/`](diary/) | Memory + Tools | A background sub-agent reads recent VRChat sessions every couple hours and writes first person diary entries the AI can read back later. | `readDiary`, `searchDiary` |
| [`mood/`](mood/) | Prompt + Tool | Two-axis mood (emotion + 1-10 intensity) that survives restarts and is injected into every system prompt. | `setMood` |
| [`duo_song/`](duo_song/) | Tools + Audio | Two Gabriel instances on the same LAN sing duets in sync, one half per machine. | `startDuoSong` |
| [`midi_band/`](midi_band/) | Tools + Audio | A whole band of Gabriel instances plays a MIDI together over LAN via fluidsynth. Standalone client included. | `startMidiBand` |
| [`pocket_tts/`](pocket_tts/) | TTS Provider | Local CPU TTS via Kyutai Pocket TTS. Streaming, ~6x realtime, persistent voice cloning from a clip. | `tts.external_provider: pocket_tts` |
| [`omnivoice_tts/`](omnivoice_tts/) | TTS Provider | GPU TTS via k2-fsa OmniVoice. 600+ languages, voice cloning, voice design (`female, british accent`), sentence-batched streaming with optional CUDA graph cache. | `tts.external_provider: omnivoice_tts` |
| [`voiceid/`](voiceid/) | Tools + Audio | Voice fingerprinting via SpeechBrain ECAPA-TDNN. The AI learns who is speaking and can identify them later, with multi-embedding storage and a margin check so similar voices dont get confused. | `saveVoice`, `identifyCurrentSpeaker` |
| [`example_hello/`](example_hello/) | Reference | Minimal demo. Read this first when learning the plugin API. | `sayHello` |

---

## Plugins in detail

### [`diary/`](diary/) -- Long term first person diary

![version](https://img.shields.io/badge/version-1.1.0-9333ea) ![api](https://img.shields.io/badge/api-v2-2ea44f) ![enabled](https://img.shields.io/badge/default-enabled-2ea44f) ![deps](https://img.shields.io/badge/deps-none-grey)

A background sub-agent reads recent VRChat session transcripts every couple
of hours and writes a first person diary entry to a custom `.diary` file.
Gabriel gets tools to read his own diary back when he needs context the
structured memory system would not capture (vibes, ongoing jokes, how
people made him feel).

> [!IMPORTANT]
> Requires `privacy.save_conversations: true` in the host config and
> `plugins.trusted: true` so the sub-agent can reuse the main Gemini key.
> See the [Trust mode](#trust-mode-for-plugins-that-need-the-host-api-key) section.

| | |
|---|---|
| **Model** | `gemini-3.1-flash-lite-preview` (configurable) |
| **Schedule** | every 2 hours, after a 5 minute warmup |
| **File** | `data/plugins/diary/gabriel.diary` (plain text, hand-editable) |
| **Tools** | `readDiary`, `searchDiary`, `listDiaryDates`, `updateDiaryNow` |

---

### [`mood/`](mood/) -- Persistent emotion + intensity

![version](https://img.shields.io/badge/version-1.1.0-9333ea) ![api](https://img.shields.io/badge/api-v2-2ea44f) ![enabled](https://img.shields.io/badge/default-enabled-2ea44f) ![deps](https://img.shields.io/badge/deps-none-grey)

Two dimensional mood system. Gabriel has an `emotion` (happy, sad, scared,
angry, amused, lonely, ...) and an `intensity` from 1 to 10. Both get
injected into the system prompt at every session start, and the AI can
change his own mood mid conversation by calling `setMood`. Mood persists
across restarts.

| | |
|---|---|
| **Built in emotions** | 21, overridable via `emotions.json` |
| **Intensity scale** | 1 to 10, overridable via `moods.json` |
| **File** | `data/plugins/mood/state.json` |
| **Tool** | `setMood` |

---

### [`duo_song/`](duo_song/) -- LAN duet, two halves of one song

![version](https://img.shields.io/badge/version-0.2.0-9333ea) ![api](https://img.shields.io/badge/api-v2-2ea44f) ![enabled](https://img.shields.io/badge/default-disabled-grey) ![deps](https://img.shields.io/badge/deps-pygame-blue)

Two Gabriel instances on the same local network sing a duet together. Each
duet song is a pair of audio files, one per singer: `SongName PT1.mp3` and
`SongName PT2.mp3` dropped into `sfx/music/duo/` on both machines. The host
plays PT1, the partner plays PT2, both trigger at the exact same moment via
a small TCP handshake plus a fast ping/pong clock sync (typical drift under
~30 ms on a quiet LAN).

| | |
|---|---|
| **Audio engine** | `pygame.mixer` (mp3 / ogg / wav / flac) |
| **Library path** | `sfx/music/duo/`, files named `<title> PT1.<ext>` / `<title> PT2.<ext>` |
| **Tools** | `startDuoSong`, `stopDuoSong`, `listDuoSongs`, `duoStatus` |
| **Requires** | `pygame>=2.5` |

---

### [`midi_band/`](midi_band/) -- Multi-instance MIDI band

![version](https://img.shields.io/badge/version-0.7.13-9333ea) ![api](https://img.shields.io/badge/api-v1-2ea44f) ![enabled](https://img.shields.io/badge/default-disabled-grey) ![deps](https://img.shields.io/badge/deps-mido%20%2B%20fluidsynth-blue) ![extras](https://img.shields.io/badge/extras-standalone%20client-ff66c4)

Turns a group of Gabriel instances on the same LAN into a live band. The
host loads a MIDI file, assigns tracks to bandmates (drums to one, bass to
another, lead to itself, etc), and on `startMidiBand` every bandmate plays
their assigned tracks at the exact same moment via fluidsynth + a
soundfont. A standalone client ships in the same folder so non-Gabriel
users can join the band too.

| | |
|---|---|
| **Synthesis** | `pyfluidsynth` + a `.sf2` soundfont (per machine) |
| **Library path** | `sfx/midi/` on the host, clients receive files on demand |
| **Tools** | `listMidiSongs`, `loadMidiSong`, `listBandMembers`, `autoAssignBandTracks`, `assignBandTracks`, `startMidiBand`, `stopMidiBand`, `bandStatus` |
| **Standalone client** | [`midi_band/standalone/`](midi_band/standalone/) (uv / pip) |
| **Requires** | `mido>=1.3`, `pyfluidsynth>=1.3`, native fluidsynth library |

---

### [`pocket_tts/`](pocket_tts/) -- Local CPU TTS via Kyutai Pocket TTS

![version](https://img.shields.io/badge/version-0.1.1-9333ea) ![api](https://img.shields.io/badge/api-v2-2ea44f) ![enabled](https://img.shields.io/badge/default-enabled-2ea44f) ![cpu](https://img.shields.io/badge/CPU-only-blue) ![cloning](https://img.shields.io/badge/voice%20cloning-yes-ff66c4)

Drop in TTS provider backed by [kyutai-labs/pocket-tts](https://github.com/kyutai-labs/pocket-tts).
Runs entirely on the CPU, no GPU, no api keys, no separate server. Streams
audio chunk by chunk through the same sentence pipeline the built in
providers use. Supports the full pretrained voice catalog plus persistent
voice cloning from any clean ~10 to 30 second audio clip, with the
extracted voice state cached to `.safetensors` so restarts are instant.

| | |
|---|---|
| **Provider id** | `pocket_tts` (set `tts.external_provider: pocket_tts`) |
| **Languages** | English (distilled, default), French, German, Italian, Spanish, Portuguese (24L variants) |
| **Output** | 16-bit PCM mono 24 kHz, ~200 ms first chunk, ~6x realtime on M4 |
| **Cache** | `data/plugins/pocket_tts/voices/<name>_<hash>.safetensors` |
| **Requires** | `pocket-tts>=2.1`, `torch>=2.5`, `stream2sentence`, `nltk`, `safetensors` |

---

### [`omnivoice_tts/`](omnivoice_tts/) -- GPU TTS via k2-fsa OmniVoice

![version](https://img.shields.io/badge/version-0.1.0-9333ea) ![api](https://img.shields.io/badge/api-v2-2ea44f) ![enabled](https://img.shields.io/badge/default-enabled-2ea44f) ![gpu](https://img.shields.io/badge/GPU-required-red) ![cloning](https://img.shields.io/badge/voice%20cloning-yes-ff66c4) ![design](https://img.shields.io/badge/voice%20design-yes-blueviolet) ![languages](https://img.shields.io/badge/languages-600%2B-yellow)

GPU TTS provider backed by [k2-fsa/OmniVoice](https://github.com/k2-fsa/OmniVoice),
a 0.6B parameter diffusion language model. State of the art voice cloning
quality, plus a unique voice design mode where you describe a voice with
attributes (`female, low pitch, british accent`) without any reference
audio. 600+ language support out of one model. The plugin streams
sentence by sentence with batched generation to saturate the GPU, plus
an anchor sentence trick so auto-voice mode stays consistent across a
full reply. Background warmup at plugin load, optional FA2 + shape-keyed
CUDA graph cache on the LLM forward, voice clone prompts cached to disk.

| | |
|---|---|
| **Provider id** | `omnivoice_tts` (set `tts.external_provider: omnivoice_tts`) |
| **Model** | `k2-fsa/OmniVoice` (0.6B diffusion LM, ~1.5 GB weights from HF) |
| **Modes** | Voice cloning (`ref_audio`), voice design (`instruct`), or auto with anchor sentence |
| **Output** | 16-bit PCM mono, native 24 kHz, resampled to host `audio.receive_sample_rate` if different |
| **Cache** | `data/plugins/omnivoice_tts/voices/<stem>_<hash>.pt` |
| **Perf** | Opt-in CUDA graph cache (~1.6x on LLM), opt-in flash-attn 2, low-vram auto-detect under 8.5 GB |
| **Requires** | `omnivoice>=0.1.5`, `torch>=2.5`, `torchaudio`, `stream2sentence`, `nltk`, `scipy` |

---

### [`voiceid/`](voiceid/) -- Voice fingerprinting and recognition

![version](https://img.shields.io/badge/version-0.3.0-9333ea) ![api](https://img.shields.io/badge/api-v3-2ea44f) ![enabled](https://img.shields.io/badge/default-enabled-2ea44f) ![encoder](https://img.shields.io/badge/encoder-ECAPA--TDNN-ff66c4) ![deps](https://img.shields.io/badge/deps-speechbrain%20%2B%20torch-blue)

Voice fingerprinting for Gabriel. Subscribes to the host `mic_chunk` event
(plugin api v3+) and keeps a few seconds of recent mic audio in a ring
buffer. On demand, runs the buffer through SpeechBrain ECAPA-TDNN to get
a 192-dim speaker embedding. Multiple captures per person are stored and
scored by max cosine, plus a margin check between top-1 and top-2 so
similar voices dont get confused. The AI gets tools to save voices under
usernames and identify whoever is currently talking.

| | |
|---|---|
| **Encoder** | [SpeechBrain ECAPA-TDNN](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb) (~14 MB weights, downloaded on first use) |
| **Storage** | `data/plugins/voiceid/voices.npz` + `voices.json` (no pickle, safe to copy) |
| **Tools** | `saveVoice`, `identifyCurrentSpeaker`, `listSavedVoices`, `forgetVoice`, `renameVoice` |
| **Requires** | `speechbrain>=1.0`, `torch>=2.0`, `torchaudio>=2.0`, `scipy>=1.10`, `numpy`, host with api v3+ |

---

### [`example_hello/`](example_hello/) -- Reference plugin

![version](https://img.shields.io/badge/version-1.1.0-9333ea) ![api](https://img.shields.io/badge/api-v2-2ea44f) ![enabled](https://img.shields.io/badge/default-disabled-grey) ![deps](https://img.shields.io/badge/deps-none-grey)

Minimal demo. Registers a `sayHello` tool and hooks `startup` / `shutdown`
events. Disabled by default, flip `enabled: true` in its `plugin.yml` to
see it in action. **Read this one first if you're learning the API.**

---

## Installing plugins

Plugins live in the `plugins/` folder of your Gabriel install (the
`ProjectGabriel-Remastered` repo, NOT this one). Easiest workflow is to
clone this repo right next to your Gabriel install so the paths stay
short:

```
your-projects/
  ProjectGabriel-Remastered/   <- your gabriel install
  ProjectGabriel-Plugins/      <- this repo, cloned next to it
```

Then from a PowerShell terminal inside `ProjectGabriel-Remastered/`:

<details>
<summary><b>1. Copy the plugin folder you want into <code>plugins/</code></b></summary>

One liner, swap `diary` for whatever plugin you want:

```powershell
Copy-Item -Recurse -Force ..\ProjectGabriel-Plugins\diary plugins\
```

To install all of them in one go:

```powershell
Get-ChildItem ..\ProjectGabriel-Plugins -Directory |
  Where-Object { Test-Path "$($_.FullName)\plugin.yml" } |
  ForEach-Object { Copy-Item -Recurse -Force $_.FullName plugins\ }
```

</details>

<details>
<summary><b>2. Make sure it's enabled</b></summary>

Open `plugins\<name>\plugin.yml` and confirm `enabled: true`. Most ship
that way already, except `duo_song`, `midi_band`, and `example_hello`
which default to off.

</details>

<details>
<summary><b>3. Install python deps (if any)</b></summary>

Gabriel ships `uv` in `bin\` so you don't need a system pip:

```powershell
# one package
.\bin\uv.exe pip install resemblyzer

# everything for a specific plugin in one shot
.\bin\uv.exe pip install (Get-Content plugins\voiceid\plugin.yml |
    Select-String '^\s*-\s' | ForEach-Object { ($_ -split '-\s+')[1].Trim() })
```

The host warns about missing deps on startup but never auto-installs.
A lot of plugins ship with no extra deps.

</details>

<details>
<summary><b>4. Optional config</b></summary>

Each plugin's own README lists its knobs. Add them under `plugins.<name>:`
in your `config.yml`. Some plugins also support a sidecar `config.yml`
inside their own folder (`plugins\<name>\config.yml`), which wins over
the main config for that plugin.

Plugins that need raw access to the host's gemini key (like `diary`) also
need `plugins.trusted: true`. See [Trust mode](#trust-mode-for-plugins-that-need-the-host-api-key).

</details>

<details>
<summary><b>5. Restart Gabriel</b></summary>

You should see something like:

```
[plugins] loaded plugin 'diary' v1.1.0
```

in the log. If the plugin registers tools, they show up in
`config\tools.yml` under `plugin_tools.<name>` set to `true` after the
first run.

</details>

---

## Updating, removing, disabling

**Update** by re-copying from this repo. Run `git pull` here, then re-run
the copy command above. Your plugin config in `config.yml` stays put.

**Remove** a plugin: delete its folder under `plugins\` and restart.

**Disable** without deleting: flip `enabled: false` in its `plugin.yml`.

---

## Per tool toggles

After first run, every plugin tool ends up listed in `config/tools.yml`
under `plugin_tools.<plugin>.<tool_name>`. Flip any of those to `false`
to hide a single tool from the model without disabling the whole plugin.

```yaml
plugin_tools:
  diary:
    readDiary: true
    searchDiary: true
    listDiaryDates: false   # the model won't see this one anymore
    updateDiaryNow: true
```

---

## Trust mode (for plugins that need the host api key)

Gabriel sandboxes `ctx.config` by default. Plugins can read their own
settings under `plugins.<name>.*` via `ctx.plugin_config()` but reads of
sensitive things like `ctx.config.api_key`, mongo strings, vrchat password
or discord token raise `PermissionError`.

A handful of plugins here (notably `diary/`) reuse the main gemini key
for a background sub-agent. To enable those, set:

```yaml
plugins:
  enabled: true
  trusted: true
```

Default is `false`. Only flip this on if you trust every plugin in your
`plugins/` folder, the toggle is global. Plugins should mention in their
own README if they need it.

---

## Contributing a plugin

PRs welcome. Rough rules:

| Rule | |
|---|---|
| Layout | One folder per plugin at the repo root, folder name matches `name:` in `plugin.yml`. |
| Manifest | `plugin.yml` with `name`, `version`, `api_version`, `author`, `description`, `enabled`. |
| Docs | Short `README.md` in the plugin folder explaining what it does, what config it reads, any external services it depends on. |
| Deps | List pip deps in `plugin.yml :: requirements:`. Don't bundle wheels or binaries. |
| Privacy | No bundled secrets, api keys, or personal data. `data/` style runtime state is the host's responsibility. |
| License | AGPL-3.0 compatible (matches the host project). |
| Honesty | If your plugin needs a private or external service, say so up front in the README. |

Reference plugins in order of complexity:

1. [`example_hello/`](example_hello/) -- bare minimum, one tool, two event subs.
2. [`mood/`](mood/) -- persistent state, prompt contributor, JSON overrides.
3. [`pocket_tts/`](pocket_tts/) -- CPU TTS provider with streaming + voice cloning + sidecar config + background warmup.
4. [`omnivoice_tts/`](omnivoice_tts/) -- GPU TTS provider with sentence-batched streaming, voice cloning, voice design, anchor sentence trick, opt-in CUDA graph cache.
5. [`diary/`](diary/) -- background scheduler, sub-agent calling another Gemini model, structured output, multiple tools.

Read those before asking how to do anything. Most patterns are already
demonstrated there.

The full author guide and all `PluginContext` surface details live in the
host repo at
[plugins/README.md](https://github.com/HoppouAI/ProjectGabriel-Remastered/blob/main/plugins/README.md).

---

## License

Same license as the host project: GNU Affero General Public License v3.0.
See [LICENSE](https://github.com/HoppouAI/ProjectGabriel-Remastered/blob/main/LICENSE)
in the host repo for details.

<div align="center">

---

Made with care for [Project Gabriel](https://github.com/HoppouAI/ProjectGabriel-Remastered)
&middot;
[Hoppou.AI](https://hoppou.ai/)
&middot;
[Discord](https://discord.gg/ZNWTYTk4Vq)

</div>
