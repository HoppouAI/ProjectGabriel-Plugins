# breeze_tts

Breeze TTS 2 text to speech for Project Gabriel, running on
[Breeze-TTS-2.cpp](https://github.com/HoppouAI/Breeze-TTS-2.cpp), a C++/GGUF
implementation of [Breeze TTS 2](https://huggingface.co/BreezeBlue/Breeze-TTS-2).

The model lives in a separate `breeze-server` process and this plugin talks to
it over that server's websocket API. Nothing heavy loads inside Gabriel, and
because the server is doing the sentence splitting itself, Gabriel's reply gets
piped straight in as it streams with no local splitter in the way.

Bilingual English and Mandarin, 24 kHz, about 1.2x realtime at Q8_0 on a 3060.

| | |
|---|---|
| runtime | separate `breeze-server` process, ggml + Vulkan |
| weights | GGUF, q4_k through f16 |
| sentence splitting | done by the server, not locally |
| voice cloning | yes, from a clip plus its exact transcript |
| voice design | yes, from a plain text description |
| barge in | real, `cancel` stops mid sentence |
| time to first audio | about 280 ms on a saved voice, about 900 ms if the clip is encoded per session |

## What you need

A running `breeze-server`. Grab a prebuilt or build it from the
[repo](https://github.com/HoppouAI/Breeze-TTS-2.cpp), and weights from
[HoppouAI/Breeze-TTS-2.cpp on HuggingFace](https://huggingface.co/HoppouAI/Breeze-TTS-2.cpp).
`q8_0` is the one to grab.

```
breeze-server.exe breeze-tts-2-q8_0.gguf --port 8080 --chunk-max 40
```

Leave it running and Gabriel connects to it. Or set `auto_start: true` and the
plugin launches and stops it for you.

> [!NOTE]
> The websocket must be on. It is by default (http port plus one). Only
> `--ws-port -1` turns it off, and this plugin cannot work without it.

## Quick start

```yaml
# config.yml
tts:
  provider: breeze_tts

plugins:
  breeze_tts:
    voice_id: "gabriel"          # a voice saved in the server's voices folder
```

Letting the plugin run the server instead:

```yaml
plugins:
  breeze_tts:
    auto_start: true
    exe: "N:\\prebuilt\\breeze-tts-2-cpp-vulkan-win64\\bin\\breeze-server.exe"
    model: "N:\\prebuilt\\breeze-tts-2-cpp-vulkan-win64\\models\\breeze-tts-2-q8_0.gguf"
    voice_id: "gabriel"
```

Every key can also live in `plugins/breeze_tts/config.yml` instead, which is
gitignored. See `config.example.yml`.

## Voice

Three ways to pick one, in the order the plugin checks them.

**A saved voice on the server.** Cheapest by a wide margin, the reference is
already encoded so there is nothing to redo per session.

```yaml
plugins:
  breeze_tts:
    voice_id: "gabriel"
```

Make one with the CLI once and it is there for good:

```
breeze-cli model.gguf --ref-audio gabriel.wav --ref-text "exact transcript here" --save-voice gabriel
```

**A clip the plugin uploads for you.** `ref_text` is required and has to be the
exact transcript, the server has no speech recognition to work it out. Add
`voice_name` and the server writes it to its voices folder so later runs can
just use `voice_id`.

```yaml
plugins:
  breeze_tts:
    ref_audio: "D:\\voices\\gabriel.wav"
    ref_text: "So I was hanging out in the Mcdonald's world and this dude just came up and started throwing fries at me."
    voice_name: "gabriel"
```

**Voice design.** No clip at all, the model invents a voice to fit the
description. Keep `seed` pinned or you get a different person every session.

```yaml
plugins:
  breeze_tts:
    instruction: "A warm young woman with a slight rasp, unhurried."
    seed: 42
```

`instruction` also works alongside a cloned voice, where it steers delivery
rather than the voice itself. The model deliberately protects a cloned timbre,
so treat it as a nudge.

## Vocal events

Gabriel writes stage directions like `[laughs]` or `*sighs*`. By default those
are stripped, because anything left in the text gets read out letter by letter.

Turn `vocal_events` on and they are converted into Breeze vocal events instead,
so `[laughs]` becomes `(laugh)` and you get a real chuckle rather than the word.
The vocabulary is free form, so `(nervous chuckle)` works as well as `(sigh)`.

```yaml
plugins:
  breeze_tts:
    vocal_events: true
    cfg_scale: 2.5
```

Two things to know before turning it on.

They need `cfg_scale` around 2 to 3. At the default of 1.0 the model reads
straight past them. Measured on a cloned voice: `(laugh)` fires at 2.5, and at
1.0 nothing happens at all. That cfg costs some naturalness on ordinary speech,
which is the actual tradeoff here.

The base form fires far more reliably than the inflected one. `(laugh)` works
where `(laughs)` does nothing, so the plugin folds the common endings down for
you (`laughs`, `laughing`, `sighs`, `gasps` and friends). Anything it does not
recognise is passed through as written.

## Config

| key | default | meaning |
|---|---|---|
| `host` | `127.0.0.1` | Where breeze-server is listening. |
| `port` | `8080` | Its HTTP port. |
| `ws_port` | from `/health` | Only set this if you started the server with an explicit `--ws-port`. |
| `auto_start` | `false` | Launch the server if nothing answers, and stop it on shutdown. Needs `exe` and `model`. |
| `exe` | none | Path to `breeze-server`. |
| `model` | none | Path to the GGUF. |
| `extra_args` | `[]` | Extra flags for the launch. Host, port and model are already passed. |
| `voice_id` | none | A voice already on the server. |
| `ref_audio` | none | Clip to upload once at startup. Needs `ref_text`. |
| `ref_text` | none | Exact transcript of `ref_audio`. |
| `voice_name` | none | Save the uploaded clip under this name so it survives restarts. |
| `instruction` | `Speak clearly and naturally.` | Voice design, or delivery direction on a cloned voice. |
| `cfg_scale` | `1.0` | Classifier free guidance. `1.0` disables it and sounds best. |
| `seed` | `42` | Pins the voice. Matters most for voice design. |
| `temperature` | `0` | `0` keeps whatever the GGUF was built with. |
| `top_k` | `0` | Same. |
| `vocal_events` | `false` | Convert stage directions into vocal events instead of stripping them. |

## Notes

The plugin holds one websocket open across the whole run rather than one per
turn, so the reference is encoded once and every turn after the first starts in
about half a second. If the socket drops it reconnects on its own and re-sends
the session setup.

Interrupting is the server's `cancel`, so it stops part way through a sentence
instead of finishing it. Audio already played stays valid and the session
carries straight on.

Audio comes back as 24 kHz mono PCM and is resampled only if your
`audio.receive_sample_rate` differs, which by default it does not.

## Requires

`websockets`, `numpy` and `requests`, all of which Gabriel already installs.
