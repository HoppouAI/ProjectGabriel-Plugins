<div align="center">

# Pocket TTS for Project Gabriel

<img src="https://raw.githubusercontent.com/kyutai-labs/pocket-tts/main/docs/assets/pocket-tts-logo-v2-transparent.png" alt="pocket tts logo" height="120" />

A local CPU TTS provider for Gabriel, powered by [Kyutai Labs Pocket TTS](https://github.com/kyutai-labs/pocket-tts).
No GPU, no cloud, no api keys. Streams chunk by chunk and clones any voice from a single audio file.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%E2%80%933.14-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/kyutai-labs/pocket-tts/blob/main/LICENSE)
[![CPU only](https://img.shields.io/badge/runs%20on-CPU-success?logo=intel&logoColor=white)](#)
[![Streaming](https://img.shields.io/badge/output-streaming-orange)](#)
[![Voice cloning](https://img.shields.io/badge/voice%20cloning-yes-purple)](#voice-cloning)
[![Real time factor](https://img.shields.io/badge/RTF-~6x%20on%20M4-yellow)](#)
[![Plugin api v2](https://img.shields.io/badge/api__version-2-lightgrey)](#)

</div>

---

## Why this exists

Gabriel can talk through any of the built in TTS providers (Gemini native, Qwen3, Chirp 3 HD, Hoppou cloud, TikTok), but they all want either a GPU, an api key, or a permanent server. Pocket TTS is a 100M parameter model that runs fast enough on a single CPU core to do realtime streaming, and it ships with builtin voices plus a clean voice cloning pipeline. This plugin glues all of that into the host's `register_tts(...)` slot so you set one config key and Gabriel speaks locally with whatever voice you want.

> [!TIP]
> First time you run it the model weights (~400MB) and any cloning reference clips download from HuggingFace into your local cache. After that everything is offline.

---

## Features

| | |
| :-- | :-- |
| Local | Runs entirely on your CPU. No GPU, no cloud, no api keys. |
| Fast | ~6x realtime on a MacBook Air M4 CPU, ~200ms first audio chunk on warmed up models. |
| Streaming | True chunk by chunk output via `generate_audio_stream`, paginated through the same sentence pipeline the other Gabriel providers use. |
| Persistent voice cloning | Drop any clean ~10 to 30 sec audio clip and the plugin extracts a voice state once, then caches it as a `.safetensors` file under `data/plugins/pocket_tts/voices/`. Reloads are basically instant. |
| Multilingual | English (default, distilled), plus 24 layer preview models for French, German, Italian, Spanish, Portuguese. |
| Hot swap friendly | Plays nicely with the host's `switchTTSProvider` voice tool. Stop and start the provider mid session without any cleanup ceremony. |
| Zero ceremony | One config block, no separate server to babysit. |

---

## Table of contents

1. [Install](#install)
2. [Pick a model](#pick-a-model)
3. [Pick a voice](#pick-a-voice)
4. [Voice cloning](#voice-cloning)
5. [Config reference](#config-reference)
6. [Built in voice catalog](#built-in-voice-catalog)
7. [Performance and latency](#performance-and-latency)
8. [Troubleshooting](#troubleshooting)
9. [Notes and limits](#notes-and-limits)
10. [Credits](#credits)

---

## Install

From a PowerShell terminal inside your Gabriel install root (the `ProjectGabriel-Remastered` repo), with this repo cloned next to it:

```powershell
# 1. Drop the plugin folder into your install
Copy-Item -Recurse -Force ..\ProjectGabriel-Plugins\pocket_tts plugins\

# 2. Install python deps. pocket-tts pulls in torch automatically.
.\bin\uv.exe pip install pocket-tts torch numpy stream2sentence nltk safetensors
```

Then in your Gabriel `config.yml`:

```yaml
tts:
  external_provider: pocket_tts

plugins:
  enabled: true
  pocket_tts:
    language: "english"   # see the full list below
    voice: "alba"         # built in voice OR a path to a wav/mp3
```

Restart Gabriel. The log should print:

```
pocket_tts registered. set tts.external_provider: pocket_tts to use it.
Using plugin TTS provider 'pocket_tts' (Gemini audio will be discarded)
pocket_tts: loading model (language=english, quantize=False) ...
pocket_tts: model loaded in 7.8s (sr=24000)
pocket_tts: using built in voice 'alba'
```

That's it. Talk to Gabriel and his voice will come from your CPU.

> [!NOTE]
> Local plugin override file is also supported. Copy `config.example.yml` to `plugins/pocket_tts/config.yml` and any keys there beat the values in the main `config.yml`. Useful for keeping a long voice cloning path out of your main config.

---

## Pick a model

Pocket TTS ships several model variants. The English distilled model is the fastest. The "24l" variants are larger non-distilled previews that sound better but are roughly 4x slower.

| `language:` value | Audio | Latency | Notes |
| :-- | :-- | :-- | :-- |
| `english` | Excellent | Lowest | Alias for `english_2026-04`, recommended default. |
| `english_2026-04` | Excellent | Lowest | Latest distilled English model. |
| `english_2026-01` | Very good | Low | Older English model, kept for reproducibility. |
| `french_24l` | Excellent | Higher | 24 layer preview, French. |
| `german_24l` | Excellent | Higher | 24 layer preview, German. |
| `italian_24l` | Excellent | Higher | 24 layer preview, Italian. |
| `spanish_24l` | Excellent | Higher | 24 layer preview, Spanish. |
| `portuguese_24l` | Excellent | Higher | 24 layer preview, Portuguese. |

> [!IMPORTANT]
> If you set a non English language model, also set `voice:` to a voice that exists in that language (see the catalog below) or use a clone of a speaker of that language. Mixing for example `language: french_24l` with `voice: alba` (English) sounds wonky.

If you want to squeeze memory or push a tiny bit more speed:

```yaml
plugins:
  pocket_tts:
    quantize: true   # int8 quant, ~half memory, very small quality drop
```

---

## Pick a voice

Three ways to set `voice:`. Pick whichever fits your taste.

<table>
<tr>
<th>Mode</th>
<th>Set <code>voice:</code> to</th>
<th>What happens</th>
</tr>
<tr>
<td><b>Built in</b></td>
<td><code>"alba"</code>, <code>"marius"</code>, <code>"giovanni"</code>, ...</td>
<td>The model uses one of Kyutai's pretrained voices. Lowest setup effort, no cloning needed.</td>
</tr>
<tr>
<td><b>Clone from file</b></td>
<td><code>"E:/voices/my_clip.wav"</code></td>
<td>The plugin extracts a voice state from your clip on the first start, saves it under <code>data/plugins/pocket_tts/voices/&lt;name&gt;_&lt;hash&gt;.safetensors</code>, then loads from that cache on every restart.</td>
</tr>
<tr>
<td><b>Clone from URL</b></td>
<td><code>"hf://kyutai/tts-voices/expresso/ex01.wav"</code></td>
<td>pocket-tts downloads the file once into its HF cache and uses it directly. Good for trying many voices fast.</td>
</tr>
<tr>
<td><b>Reuse a cached state</b></td>
<td><code>"E:/voices/my_clip.safetensors"</code></td>
<td>Loads instantly, no extraction. Use this if you exported a state with <code>pocket-tts export-voice</code> elsewhere or want to ship a voice between machines.</td>
</tr>
</table>

---

## Voice cloning

The plugin handles the full lifecycle for you. You give it an audio clip, it does the rest.

### What makes a good reference clip

> [!TIP]
> Quality of the clone is roughly equal to quality of the clip. Clean studio audio sounds better than a phone recording.

| | |
| :-- | :-- |
| **Length** | 10 to 30 seconds works best. Longer is fine but processed faster if you set `truncate_clone: true`. |
| **Format** | WAV, MP3, FLAC, OGG. Mono or stereo, anything pocket-tts can decode. |
| **Content** | Natural speech, varied prosody, no music or background noise. |
| **Cleanup** | If your clip has hiss or echo, run it through [Adobe Podcast Enhance](https://podcast.adobe.com/en/enhance) (free) or any decent denoiser first. The model **reproduces** the room tone of the reference clip. |

### Cloning workflow

```yaml
plugins:
  pocket_tts:
    language: "english"
    voice: "E:/refs/gabriel_reference.wav"
    truncate_clone: false   # set true if your clip is over 30s and you want fast loads
    cache_voice: true       # default. set false to force re-extraction every restart
```

First start logs:

```
pocket_tts: extracting voice state from E:\refs\gabriel_reference.wav (this can take a few seconds)
pocket_tts: cached voice state to data\plugins\pocket_tts\voices\gabriel_reference_a1b2c3d4e5f6g7h8.safetensors
```

Every subsequent start logs:

```
pocket_tts: loading cached voice state for gabriel_reference.wav -> gabriel_reference_a1b2c3d4e5f6g7h8.safetensors
```

The cache key includes the file path, its modification time, and the loaded language model. Editing or replacing the wav, or switching to a different model, automatically forces a fresh extraction.

> [!CAUTION]
> Voice cloning is for voices you have rights to clone. Don't impersonate real people without their consent. Pocket TTS ships with a [prohibited use clause](https://github.com/kyutai-labs/pocket-tts#prohibited-use), it applies here too.

---

## Config reference

Every key lives under `plugins.pocket_tts.*` in your main `config.yml`, or alongside the plugin in `plugins/pocket_tts/config.yml` (gitignored, overrides the main config).

| Key | Type | Default | What it does |
| :-- | :-- | :-- | :-- |
| `language` | string | `english` | Which model to load. See [Pick a model](#pick-a-model). |
| `voice` | string | `alba` | Built in name, local path, `.safetensors`, or `hf://` / `https://` URL. |
| `quantize` | bool | `false` | int8 quantization at load time. Saves memory. |
| `temperature` | float | `0.7` | Sampling temperature, 0.0 to 1.0. |
| `lsd_decode_steps` | int | `1` | LSD decode steps. 1 is correct for the distilled English model. Try 3 to 5 only on `_24l` variants. |
| `eos_threshold` | float | `-4.0` | End of sequence threshold. Lower stops sooner. |
| `noise_clamp` | float or null | `null` | Optional noise clamp to suppress rare audible glitches. |
| `frames_after_eos` | int or null | `null` | Extra frames to emit after EOS. Leave unset for the model's default. |
| `truncate_clone` | bool | `false` | Cap reference clips at 30 seconds for faster cloning. |
| `cache_voice` | bool | `true` | Cache extracted voice state as `.safetensors` between runs. |
| `first_chunk_min_samples` | int | `1920` | Coalesce the first audio frames so the playback path doesn't stutter on tiny initial chunks. Set 0 to disable. |

---

## Built in voice catalog

Names map straight to pocket-tts. Just put the bare name in `voice:`, no path, no extension.

<details>
<summary><b>English voices</b> (alba, marius, fantine, eponine, ...)</summary>

| Voice | Vibe |
| :-- | :-- |
| `alba` | Casual, warm, mid-pitch female |
| `anna` | Young female, neutral |
| `azelma` | Young female, soft |
| `bill_boerst` | Older male, narrator |
| `caro_davy` | Young female, expressive |
| `charles` | Male, calm |
| `cosette` | Young female, soft, expressive |
| `eponine` | Young female, intense |
| `eve` | Mid female, neutral |
| `fantine` | Young female, melancholic |
| `george` | Male, casual |
| `jane` | Mid female, calm |
| `jean` | Male, conversational |
| `javert` | Older male, authoritative |
| `marius` | Young male, warm |
| `mary` | Mid female, lively |
| `michael` | Male, broadcaster style |
| `paul` | Older male, gentle |
| `peter_yearsley` | Older male, narrator |
| `stuart_bell` | Older male, narrator |
| `vera` | Mid female, warm |

</details>

<details>
<summary><b>Other languages</b> (one default voice per language)</summary>

| Voice | Language |
| :-- | :-- |
| `estelle` | French (`fr`) |
| `giovanni` | Italian (`it`) |
| `lola` | Spanish (`es`) |
| `juergen` | German (`de`) |
| `rafael` | Portuguese (`pt`) |

</details>

> The voice list above mirrors the [official pocket-tts catalog](https://github.com/kyutai-labs/pocket-tts#trying-it-with-the-cli). For licenses on individual voices see [kyutai/tts-voices](https://huggingface.co/kyutai/tts-voices).

---

## Performance and latency

Numbers from Kyutai's own measurements, reproduced for context. Your mileage will depend on CPU, memory bandwidth, and how aggressively other apps fight for cores.

| Hardware | First chunk | Real time factor |
| :-- | :-- | :-- |
| MacBook Air M4 (CPU only) | ~200ms | ~6x |
| Modern desktop x86 (8 perf cores) | ~150ms | 4x to 8x |
| Modest laptop (4 cores) | 300 to 500ms | 2x to 3x |

Pocket TTS only uses **2 cores** by default which is part of why it's so polite to the rest of the system.

> [!TIP]
> If you also run Gabriel's local STT (Moonshine + Silero VAD) on the same CPU, profile both together. They share cache and bandwidth so isolated benchmarks can be optimistic.

---

## Troubleshooting

<details>
<summary><b>"pocket_tts: synth loop bailing, model not loaded"</b></summary>

The model failed to load on start. Common causes:
- Missing or wrong `language:` value (check the [model table](#pick-a-model)).
- pip install of `pocket-tts` didn't finish, or torch is too old (need 2.5+).
- HuggingFace download blocked by a firewall or hit a rate limit. Retry with `huggingface-cli login` if your network is gated.

The full error string lives in the same log line on level ERROR.

</details>

<details>
<summary><b>"voice path not found: ..."</b></summary>

Your `voice:` is a string that the plugin couldn't resolve to a built in name, a URL, or an existing file. Fix the path or fall back to a built in voice for testing:

```yaml
plugins:
  pocket_tts:
    voice: "alba"
```

Built in names are bare alphanumeric tokens (no dots, no slashes). If your voice file happens to be named like one, give it an extension or absolute path so the plugin treats it as a file.

</details>

<details>
<summary><b>Audio sounds pitch shifted or chipmunk</b></summary>

Pocket TTS emits at 24000 Hz. Gabriel's `audio.receive_sample_rate` is 24000 Hz by default. If you changed the host's audio config to something else (16000 or 48000), reset it back to 24000 or expect resampling artifacts. The plugin does not resample on its own.

</details>

<details>
<summary><b>First sentence is fast, later ones get slower</b></summary>

Two likely causes:
- `lsd_decode_steps` is set higher than 1 on the distilled English model. Lower it back to 1.
- You set `quantize: true` on a CPU that doesn't have great int8 throughput. Try `quantize: false`.

</details>

<details>
<summary><b>Clone sounds nothing like the reference</b></summary>

The reference clip's audio quality is more important than its length. Run it through a denoiser or [Adobe Podcast Enhance](https://podcast.adobe.com/en/enhance) and try again. Also make sure the language matches: cloning a French speaker with the English model gives weird results.

If the reference is over 30 seconds and you set `truncate_clone: true`, only the first 30s are used. Try `truncate_clone: false` to use the whole clip.

</details>

<details>
<summary><b>Stale voice state after editing the reference clip</b></summary>

The cache is keyed on path + mtime + language. Saving over the file should invalidate it automatically. If it doesn't (some editors preserve mtime), delete the matching `.safetensors` under `data/plugins/pocket_tts/voices/` to force re-extraction, or set `cache_voice: false` temporarily.

</details>

---

## Notes and limits

- This is a **CPU only** path. Pocket TTS is small enough that GPU offload didn't actually help (per Kyutai), so we don't expose a `device:` knob.
- Output is 16 bit PCM mono at 24000 Hz, the native rate of the model and the host's audio pipeline.
- Sentences are split via `stream2sentence` so individual chunks coming back from the model overlap with sentence N+1's preroll, same pipeline used by the other built in providers.
- The provider is single threaded inside (pocket-tts itself is not thread safe). High concurrency text in is buffered and processed serially. For Gabriel's interactive use case this is fine, the bottleneck is the model not the dispatcher.
- Hot swap is supported: the host's `switchTTSProvider` tool can move from gemini / qwen3 / pocket_tts and back without a session restart.

---

## Credits

- [Kyutai Labs Pocket TTS](https://github.com/kyutai-labs/pocket-tts) by Manu Orsini, Simon Rouard, Gabriel De Marmiesse, Václav Volhejn, Neil Zeghidour, and Alexandre Défossez. MIT licensed.
- [tts-voices](https://huggingface.co/kyutai/tts-voices) catalog and [pocket-tts](https://huggingface.co/kyutai/pocket-tts) model card on HuggingFace.
- This plugin glue: written for [Project Gabriel](https://github.com/HoppouAI/ProjectGabriel-Remastered), AGPL 3.0 to match the host repo.
