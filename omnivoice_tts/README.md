<div align="center">

# OmniVoice TTS for Project Gabriel

GPU TTS provider for Gabriel, powered by [k2-fsa OmniVoice](https://github.com/k2-fsa/OmniVoice).
600+ languages, voice cloning, voice design, and chunked streaming with batch sentence generation.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%E2%80%933.14-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-green)](https://github.com/k2-fsa/OmniVoice/blob/master/LICENSE)
[![GPU](https://img.shields.io/badge/GPU-required-red?logo=nvidia&logoColor=white)](#install)
[![Streaming](https://img.shields.io/badge/output-streaming-orange)](#streaming)
[![Voice cloning](https://img.shields.io/badge/voice%20cloning-yes-purple)](#voice-cloning)
[![Voice design](https://img.shields.io/badge/voice%20design-yes-blueviolet)](#voice-design)
[![Languages](https://img.shields.io/badge/languages-600%2B-yellow)](https://github.com/k2-fsa/OmniVoice/blob/master/docs/languages.md)
[![Plugin api v2](https://img.shields.io/badge/api__version-2-lightgrey)](#)

</div>

---

## Why this exists

Gabriel ships with a handful of TTS providers (Gemini native, Qwen3, Pocket TTS on CPU, Chirp 3 HD, Hoppou cloud, TikTok). OmniVoice fills a slot none of them cover well: a **fast GPU diffusion TTS** with state of the art voice cloning quality across 600+ languages, plus a unique "voice design" mode where you describe a voice with attributes (`female, low pitch, british accent`) without any reference audio.

This plugin glues the model into the host's `register_tts(...)` slot with the same warmup, streaming and voice cache patterns the other providers use. Drop it in, set one config key, restart, talk.

> [!IMPORTANT]
> OmniVoice is a diffusion model. CPU inference works but is unusably slow for realtime chat. **You want at least a 6 GB nvidia GPU.** Apple Silicon (mps) and Intel Arc (xpu) are supported by the upstream model but not regularly tested by this plugin.

---

## Features

| | |
| :-- | :-- |
| **GPU diffusion** | RTF as low as 0.025 (40x realtime) on a 4090 at `num_step: 16`. ~3-5x realtime on a 4060 / 3060 12 GB. |
| **Voice cloning** | Point at any clean 3-10 sec audio clip and the plugin extracts a reusable `VoiceClonePrompt`, cached on disk so restarts are instant. |
| **Voice design** | Describe a voice without any reference (`female, low pitch, british accent`). |
| **Auto voice + anchor sentence** | Leave both empty and the plugin generates the first sentence of every reply solo, then locks the voice for the rest of the reply. No more "4 different voices reading 4 sentences". |
| **Sentence batching** | Streams sentence by sentence, batches 2 at a time to saturate the GPU without crushing first-chunk latency. Tunable. |
| **Background warmup** | Model loads on a daemon thread the moment the plugin registers, so the first session that picks `omnivoice_tts` gets a hot model. Process wide cache survives session reconnects. |
| **600+ languages** | Same model, no per-language reload. Optional language hint speeds things up a touch. |
| **Optional CUDA graphs** | Opt-in shape-keyed CUDA graph cache on the inner LLM forward. ~1.6x on the isolated LLM, only a few percent end to end, costs ~2 GB extra vram. Off by default. |
| **Optional flash-attn 2** | Opt-in FA2 swap on the LLM attention impl. Faster on long sequences, historically flaky on some varlen paths, hence opt-in. |
| **Low-vram autodetect** | Cards under 8.5 GB get more aggressive `empty_cache` between sentences automatically. |

---

## Table of contents

1. [Install](#install)
2. [Pick a voice mode](#pick-a-voice-mode)
3. [Voice cloning](#voice-cloning)
4. [Voice design](#voice-design)
5. [Auto voice](#auto-voice)
6. [Streaming](#streaming)
7. [Perf knobs](#perf-knobs)
8. [Config reference](#config-reference)
9. [Troubleshooting](#troubleshooting)
10. [Credits](#credits)

---

## Install

From a PowerShell terminal inside your Gabriel install root (the `ProjectGabriel-Remastered` repo), with this repo cloned next to it:

```powershell
# 1. Drop the plugin folder into your install
Copy-Item -Recurse -Force ..\ProjectGabriel-Plugins\omnivoice_tts plugins\

# 2. Install python deps. Pulls in torch via the omnivoice package.
.\bin\uv.exe pip install omnivoice stream2sentence nltk scipy
```

> [!NOTE]
> The host repo already ships torch with CUDA support. If you installed the cpu-only torch wheel separately, force the CUDA build before installing `omnivoice`:
> ```powershell
> .\bin\uv.exe pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
> .\bin\uv.exe pip install omnivoice stream2sentence nltk scipy
> ```

Then in your Gabriel `config.yml`:

```yaml
tts:
  external_provider: omnivoice_tts

plugins:
  enabled: true
  omnivoice_tts:
    # one of these three modes:
    ref_audio: null         # voice cloning   eg "E:/voices/me.wav"
    instruct: null          # voice design    eg "female, british accent"
    #  (leave both null for auto voice + anchor sentence trick)
```

Restart Gabriel. The log should print:

```
omnivoice_tts registered (warming up in background). set tts.external_provider: omnivoice_tts to use it.
omnivoice_tts: warming up model=k2-fsa/OmniVoice device=cuda dtype=float16 in background ...
omnivoice_tts: model loaded in 12.4s (sr=24000)
omnivoice_tts: warmup done, first session will be hot
```

When you actually talk to Gabriel:

```
Using plugin TTS provider 'omnivoice_tts' (Gemini audio will be discarded)
omnivoice_tts started (model=k2-fsa/OmniVoice device=cuda dtype=float16 voice=auto)
omnivoice_tts: reusing pre-warmed model + voice (hot start)
omnivoice_tts sentence: 'hello there, how are you doing today?'
omnivoice_tts: locked voice from anchor sentence (38 chars)
```

> [!TIP]
> A local plugin override file is also supported. Copy `config.example.yml` to `plugins/omnivoice_tts/config.yml` and any keys there beat the values in the main `config.yml`. Useful for keeping a long voice clone path out of your main config.

---

## Pick a voice mode

Three modes, pick one. Don't set both `ref_audio` and `instruct` at the same time.

<table>
<tr>
<th>Mode</th>
<th>Config</th>
<th>When to use</th>
</tr>
<tr>
<td><b>Voice cloning</b></td>
<td>

```yaml
ref_audio: "E:/voices/me.wav"
ref_text: null  # or the transcript
```

</td>
<td>You have a clean reference clip of the voice you want Gabriel to use. Best quality cloning method.</td>
</tr>
<tr>
<td><b>Voice design</b></td>
<td>

```yaml
instruct: "female, low pitch, british accent"
```

</td>
<td>You want a specific kind of voice but don't have a reference clip. English and Chinese work best.</td>
</tr>
<tr>
<td><b>Auto voice</b></td>
<td>

```yaml
ref_audio: null
instruct: null
```

</td>
<td>You don't care, let the model pick. The plugin locks the voice on the first sentence of each reply so it stays consistent.</td>
</tr>
</table>

---

## Voice cloning

The plugin handles the full lifecycle. You give it a clip, it does the rest.

### What makes a good reference clip

| | |
| :-- | :-- |
| **Length** | 3-10 seconds is the sweet spot. Longer clips slow extraction and can hurt cloning quality (upstream guidance). |
| **Format** | WAV, MP3, FLAC, OGG, whatever torchaudio decodes. Mono or stereo. |
| **Content** | Natural connected speech, varied prosody, no music or background noise. |
| **Language** | Same language as the speech you want Gabriel to produce, otherwise the output will carry an accent from the reference language. |
| **Cleanup** | If your clip has hiss or echo, run it through [Adobe Podcast Enhance](https://podcast.adobe.com/en/enhance) (free) first. The model reproduces the room tone of the reference. |

### Cloning workflow

```yaml
plugins:
  omnivoice_tts:
    ref_audio: "E:/voices/me.wav"
    ref_text: null   # null = whisper auto-transcribes
    cache_voice: true
```

First start logs:

```
omnivoice_tts: encoding voice clone prompt from E:\voices\me.wav ...
omnivoice_tts: cached voice prompt to data\plugins\omnivoice_tts\voices\me_a1b2c3d4e5f6g7h8.pt
```

Every subsequent start logs:

```
omnivoice_tts: loaded cached voice prompt me_a1b2c3d4e5f6g7h8.pt
omnivoice_tts: reusing pre-warmed model + voice (hot start)
```

The cache is keyed on the file path, mtime, model, dtype, and ref_text. Editing the clip or swapping the model triggers a fresh encode automatically. To skip the whisper auto-transcribe step (and save the vram for whisper-large-v3-turbo), set `ref_text:` to the literal transcript of your clip.

---

## Voice design

Describe the voice with comma-separated attributes. No reference audio needed.

```yaml
plugins:
  omnivoice_tts:
    instruct: "female, low pitch, british accent"
```

Supported attribute categories (from upstream docs):

| Category | Values |
| :-- | :-- |
| **Gender** | `male`, `female` |
| **Age** | `child`, `teen`, `young`, `adult`, `elderly` |
| **Pitch** | `very low`, `low`, `normal`, `high`, `very high` |
| **Style** | `whisper` |
| **English accent** | `american`, `british`, `indian`, `australian`, `canadian`, ... |
| **Chinese dialect** | `sichuanese`, `northeastern`, `cantonese`, ... |

Attributes from different categories combine freely. See [upstream voice-design docs](https://github.com/k2-fsa/OmniVoice/blob/master/docs/voice-design.md) for the full reference.

> [!NOTE]
> Voice design is trained on English and Chinese data only. It generalizes to other languages but may be unstable for low-resource ones. For non English/Chinese, voice cloning is more reliable.

---

## Auto voice

Leave both `ref_audio` and `instruct` empty. The plugin's `anchor_first_sentence` flag (on by default) makes the very first sentence of every reply act as the voice anchor:

1. Sentence 1 generates solo with no voice prompt. The model picks a voice.
2. The plugin grabs the resulting audio and calls `create_voice_clone_prompt(...)` on it.
3. Every subsequent sentence in the reply uses that anchor prompt.

Result: every reply gets a fresh randomized voice but it stays the same voice across the whole reply. Turn off `anchor_first_sentence` if you actually want the voice to drift.

---

## Streaming

OmniVoice is a diffusion model, not autoregressive, so a single `generate()` call covers a full sentence in one forward pass. There's no chunk-by-chunk streaming inside a single sentence. The plugin gives you "streaming" by:

1. Sentence splitting the LLM's text output as it arrives via `stream2sentence`.
2. Pulling sentences off the queue and **batching** them up to `stream_batch_size` per GPU call.
3. Yielding the resulting PCM chunks back to Gabriel as soon as each batch finishes.

Tune `stream_batch_size`:

| Value | First-chunk latency | Throughput | VRAM |
| :--: | :-- | :-- | :-- |
| 1 | Lowest | Lower | Lowest |
| **2 (default)** | Low | Good | Moderate |
| 3-4 | Higher | Best | Highest |

For very chatty replies on a 12+ GB card, try `stream_batch_size: 3`. On a 6 GB card, stay at 1 or 2.

---

## Perf knobs

Two opt-in knobs in addition to the defaults. Both off because each has a real tradeoff.

### `use_cuda_graphs: true`

Wraps `model.llm.forward` with a shape-keyed `torch.cuda.CUDAGraph` cache. The diffusion loop calls `llm.forward` many times per sentence at the exact same shapes, so capturing one graph per `(batch, seq_len, dtype)` and replaying it skips the per-kernel launch overhead.

- Measured ~1.6x on the isolated LLM, only a few percent end to end.
- Costs ~2 GB extra vram for the captured buffers.
- Capped at `max_graph_cache` (default 8) distinct shapes to bound the vram cost.
- Falls back to eager forward if capture fails on a given shape.

### `use_flash_attn: true`

Switches the inner LLM attention impl to flash-attn 2.

- Requires `pip install flash-attn` (Linux only in practice).
- Faster on long sequences.
- Historically crashes on some varlen paths in `generate()`, so opt-in. Try it, fall back if it breaks.

```yaml
plugins:
  omnivoice_tts:
    use_cuda_graphs: true
    max_graph_cache: 8
    # use_flash_attn: true   # only if you have flash-attn installed
```

### Defaults that are always on

These run regardless of the opt-in flags:

- `torch.set_grad_enabled(False)` at warmup.
- `torch.inference_mode()` wrapping every `generate()` call.
- `low_vram` auto-detect for GPUs under 8.5 GB (more aggressive `empty_cache` between sentences).
- Background warmup thread so the first session is hot.

---

## Config reference

The full set of keys is documented inline in [config.example.yml](config.example.yml). Quick reference:

| Key | Default | What |
| :-- | :-- | :-- |
| `model` | `k2-fsa/OmniVoice` | HF repo id or local path. |
| `device` | `null` (auto) | `"cuda"`, `"cuda:0"`, `"mps"`, `"cpu"`. |
| `dtype` | `"float16"` | `"float16"`, `"bfloat16"`, `"float32"`. |
| `ref_audio` | `null` | Path to a clip for voice cloning. |
| `ref_text` | `null` | Transcript of `ref_audio`. `null` = whisper auto-transcribe. |
| `instruct` | `null` | Voice design attribute string. |
| `language` | `null` (auto) | Language hint, eg `"English"` or `"en"`. |
| `num_step` | `16` | Diffusion steps per sentence. `16` fast, `32` quality. |
| `guidance_scale` | `2.0` | CFG scale. `2.0` is the upstream default. |
| `speed` | `null` | Speaking speed factor. |
| `denoise` | `true` | Apply the `<\|denoise\|>` token. |
| `stream_batch_size` | `2` | Sentences per GPU call. |
| `anchor_first_sentence` | `true` | Lock voice on sentence 1 in auto mode. |
| `first_chunk_min_samples` | `1920` | Skip past tiny first chunks. |
| `use_flash_attn` | `false` | FA2 swap (opt-in). |
| `use_cuda_graphs` | `false` | CUDA graph cache (opt-in). |
| `max_graph_cache` | `8` | Cap on distinct cached graph shapes. |
| `asr_model` | `openai/whisper-small` | Whisper model for auto-transcribing `ref_audio`. |
| `cache_voice` | `true` | Cache voice clone prompts to disk. |
| `low_vram` | `false` | Force the aggressive `empty_cache` path. |

---

## Troubleshooting

**The model takes a long time to load on first run.**
That's the HuggingFace download. ~1.5 GB of weights plus whisper if you don't set `ref_text`. They land in `~/.cache/huggingface/` and reload from disk on every restart after that.

**Audio comes out garbled / robotic / clipped.**
Lower `num_step` is the most common cause. `16` is fine for the fast preset but `8` is risky. Try `num_step: 32` and see if quality jumps back. Also confirm `dtype: float16` works on your gpu, switch to `bfloat16` or `float32` if not.

**Voice drifts between sentences in auto mode.**
Make sure `anchor_first_sentence: true` (default). If you set it to `false`, the model picks a fresh voice per sentence.

**Cloned voice sounds nothing like the reference.**
Check the clip length (3-10 sec ideal), clean it up if it has hiss or echo. Then delete the cached `.pt` under `data/plugins/omnivoice_tts/voices/` and let the plugin re-extract.

**OOM on a small GPU.**
- Lower `stream_batch_size` to `1`.
- Make sure `dtype: float16` (not `float32`).
- Set `low_vram: true` to force the aggressive empty_cache path.
- Use a smaller `asr_model` like `openai/whisper-base` (or set `ref_text` explicitly so whisper is never loaded).

**CUDA graphs make things worse.**
That happens. Turn off `use_cuda_graphs:` and you're back to the default eager path. The capture itself takes a few seconds the first time each shape is seen.

**flash-attn crashes.**
That's why it's opt-in. Turn `use_flash_attn:` back off.

---

## Credits

- [k2-fsa OmniVoice](https://github.com/k2-fsa/OmniVoice) — the actual model (Apache 2.0). All credit to the authors (Han Zhu et al).
- Streaming + perf patterns mirrored from a fork of the upstream `omnivoice-serve` (CUDA graph cache, anchor sentence trick, sentence batching).
- Plugin glue, warmup, config wiring by HoppouAI for Project Gabriel.

> [!CAUTION]
> Voice cloning is powerful and easy to abuse. Don't clone real people without their consent and don't use this to impersonate anyone. See the [upstream disclaimer](https://github.com/k2-fsa/OmniVoice#disclaimer).
