# omnivoice_cpp_tts

OmniVoice text to speech for Project Gabriel, running on the native
[omnivoice.cpp](https://github.com/ServeurpersoCom/omnivoice.cpp) engine
instead of python + torch.

This is the fast sibling of the [`omnivoice_tts`](../omnivoice_tts) plugin.
Same model (k2-fsa OmniVoice), but the heavy lifting happens in a C++/GGML
library that loads quantized GGUF weights and is pulled in-process through
ctypes. No torch, no diffusion stack to install, no separate server. Lower
VRAM and noticeably faster, at the cost of needing the native lib built for
your GPU.

| | omnivoice_tts (python) | omnivoice_cpp_tts (this) |
|---|---|---|
| runtime | torch + diffusers | native omnivoice.cpp via ctypes |
| weights | fp16/bf16 safetensors | quantized GGUF (Q4_K_M default) |
| install | pip (heavy) | one native lib + auto GGUF download |
| voice cloning | yes, auto transcribes with whisper | yes, but you must supply `ref_text` |
| voice design | yes | yes |
| setup effort | low | low on Windows (auto-downloads), manual build elsewhere |

## What you need

On **Windows: nothing**. On first run the plugin auto-downloads both pieces:

1. The native **omnivoice.cpp engine**. A prebuilt Vulkan build (runs on any
   GPU: NVIDIA / AMD / Intel) is pulled from
   [ProjectGabriel-Plugin-Resources](https://github.com/HoppouAI/ProjectGabriel-Plugin-Resources)
   and unzipped into `data/plugins/omnivoice_cpp_tts/native/`.
2. The **GGUF models**, pulled from
   [Serveurperso/OmniVoice-GGUF](https://huggingface.co/Serveurperso/OmniVoice-GGUF)
   into `data/plugins/omnivoice_cpp_tts/models/`.

So on Windows you just set the provider and talk. On Linux/macOS, or if you
want a faster CUDA build, you build the native lib yourself (see below) and
point `lib_dir` at it.

## Quick start

```yaml
# config.yml
tts:
  external_provider: omnivoice_cpp_tts

plugins:
  omnivoice_cpp_tts:
    instruct: "female, young adult, british accent"   # or clone, see Voice
```

That's the whole config on Windows. The Vulkan engine and the models download
themselves on first run.

If you'd rather use your own build (eg CUDA for speed, or a non-Windows
build), point it at the dll folder:

```yaml
plugins:
  omnivoice_cpp_tts:
    lib_dir: "N:\\prebuilt\\omnivoice-cpp-cuda-win64\\bin"   # folder with omnivoice.dll
```

`lib_dir` is the folder holding `omnivoice.dll` plus its `ggml-*.dll` (and
any cuda runtime dlls). You can also set the `OMNIVOICE_CPP_DIR` env var, or
drop the dlls into a `native/` folder inside this plugin. Set
`auto_download_lib: false` to disable the download entirely.

First run downloads two GGUFs (about 410 MB + 250 MB for Q4_K_M) and warms
the engine in the background. After that, starts are hot.

## Voice

Three modes, in priority order:

- **Cloning**: set `ref_audio` (a wav/flac clip) **and** `ref_text` (its exact
  transcript). The cpp engine has no built in speech recognition, so the
  transcript is mandatory. Without `ref_text` the plugin logs a warning and
  falls back to design / auto voice.
- **Design**: set `instruct` to an attribute string like
  `"male, elderly, low pitch, american accent"`. The model invents a voice.
- **Auto**: set neither and let the model pick.

```yaml
plugins:
  omnivoice_cpp_tts:
    lib_dir: "..."
    ref_audio: "C:\\voices\\me.wav"
    ref_text: "This is exactly what I say in the reference clip."
```

### Nonverbal tags

OmniVoice understands a handful of inline nonverbal tags. The plugin keeps
the supported ones and folds common variants the AI writes (`[laughs]`,
`[chuckle]`, `[giggle]`, `[sighs]`, `[gasp]`, ...) into the nearest real
tag. Anything it does not recognise gets stripped so it is not spelled out
letter by letter. Supported set:

```
[laughter] [sigh] [confirmation-en]
[question-en] [question-ah] [question-oh] [question-ei] [question-yi]
[surprise-ah] [surprise-oh] [surprise-wa] [surprise-yo]
[dissatisfaction-hnn]
```

## Building the native engine for your GPU

The native lib is **architecture specific**. A CUDA build compiled for one
GPU family will not run on another unless you build it multi-arch. Pick the
path for your hardware.

### CUDA (NVIDIA)

```bash
git clone --recurse-submodules https://github.com/ServeurpersoCom/omnivoice.cpp.git
cd omnivoice.cpp

cmake -S . -B build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_CUDA=ON \
  -DOMNIVOICE_SHARED=ON \
  -DCMAKE_CUDA_ARCHITECTURES=native
cmake --build build -j
```

Two flags matter for this plugin:

- **`-DOMNIVOICE_SHARED=ON`** builds the shared library (`omnivoice.dll` /
  `libomnivoice.so`) that ctypes loads. Without it you only get the CLI.
- **`-DCMAKE_CUDA_ARCHITECTURES`** selects the GPU target. `native`
  auto-detects the card in the build machine (needs CMake 3.24+ and a recent
  CUDA toolkit) and is the easiest "build it for this box" option.

If `native` is not available, or you want a binary that runs on several
cards, pass explicit compute capabilities:

| GPU family | Cards | Compute cap |
|---|---|---|
| Turing | RTX 20xx, GTX 16xx, Titan RTX | `75` |
| Ampere (data center) | A100 | `80` |
| Ampere (consumer) | RTX 30xx, A10, A40, RTX A6000 | `86` |
| Ada Lovelace | RTX 40xx, L4, L40 | `89` |
| Hopper | H100, H200 | `90` |
| Blackwell (data center) | B100, B200 | `100` |
| Blackwell (consumer) | RTX 50xx | `120` |

Single card example (RTX 4090, Ada):

```bash
  -DCMAKE_CUDA_ARCHITECTURES=89-real
```

Fat multi-arch binary that runs on Ampere + Ada + Blackwell, with a PTX
fallback so future drivers can JIT for newer cards:

```bash
  -DCMAKE_CUDA_ARCHITECTURES="86-real;89-real;120-real;120-virtual"
```

`N-real` bakes in native SASS for that arch. `N-virtual` keeps PTX so a
newer GPU can JIT-compile at load time (slower first run, but it runs). A
build with only `N-real` and no matching card just fails to bring up the
CUDA backend.

> Heads up: the cuda runtime dlls (`cudart64_*`, `cublas64_*`,
> `cublasLt64_*`) must sit next to `omnivoice.dll` in your `lib_dir`. The
> ggml dlls (`ggml.dll`, `ggml-base.dll`, `ggml-cpu.dll`, `ggml-cuda.dll`)
> too. Keep the whole `bin/` folder together. On a clean target box you only
> need the NVIDIA driver, not the full CUDA toolkit.

### Vulkan (AMD / Intel / cross vendor)

No CUDA on your card? Build the Vulkan backend instead. One binary runs on
any Vulkan 1.2+ GPU, no per-arch flag needed. This is also the build to
ship if you want a single download that runs on anyone's GPU: a CUDA build
is locked to the arch it was compiled for, a Vulkan build is not.

```bash
cmake -S . -B build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_VULKAN=ON \
  -DOMNIVOICE_SHARED=ON
cmake --build build -j
```

### CPU only

Slow but dependency free, good for a smoke test.

```bash
cmake -S . -B build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DOMNIVOICE_SHARED=ON
cmake --build build -j
```

### After building

Find `omnivoice.dll` (or `libomnivoice.so`) under `build/`, make sure all
the sibling dlls are next to it, and point `lib_dir` at that folder. To
force a specific backend regardless of what is detected, set the
`GGML_BACKEND` env var (`CUDA0`, `Vulkan0`, `Metal`, `CPU`).

## Config reference

Every key is read from `plugins.omnivoice_cpp_tts.*` in `config.yml`, or
from a local `config.yml` next to this plugin (copy `config.example.yml`).
The local file wins. Full annotated list lives in
[config.example.yml](config.example.yml). The important ones:

| key | default | what it does |
|---|---|---|
| `lib_dir` | none | folder with `omnivoice.dll` + dlls. optional, auto-downloads on Windows |
| `lib_url` | hosted Vulkan zip | where to fetch the prebuilt engine from |
| `auto_download_lib` | `true` | pull the prebuilt engine when no `lib_dir` is found (Windows) |
| `model_repo` | `Serveurperso/OmniVoice-GGUF` | HF repo to pull GGUFs from |
| `model_variant` | `Q4_K_M` | `F32` / `BF16` / `Q8_0` / `Q4_K_M` |
| `base_model` / `codec_model` | none | local GGUF paths, skip the download |
| `use_fa` | `true` | flash attention on a gpu backend |
| `clamp_fp16` | `false` | guard fp16 matmul on sub-Ampere cuda |
| `instruct` | none | voice design attribute string |
| `ref_audio` / `ref_text` | none | voice cloning clip + transcript |
| `language` | auto | `""` auto, `en`, `zh`, ... |
| `num_step` | `8` | diffusion steps, fewer = faster (see perf) |
| `guidance_scale` | `2.0` | classifier free guidance |
| `seed` | random | int pins output (reproducible), null = fresh seed each line |

## Requirements

The pip side is light (no torch):

```
numpy  stream2sentence  nltk  huggingface_hub  soundfile
```

`soundfile` is only needed for voice cloning (reading the reference clip).
Everything else is for sentence splitting and the model download.

## Performance

Generation time per sentence scales about linearly with `num_step`. Measured
on an RTX 3060 (12 GB) with the `Q4_K_M` weights, voice design, `use_fa` on:

| num_step | short sentence | medium sentence |
|---|---|---|
| 8 (default) | ~340 ms (6x realtime) | ~590 ms (8x realtime) |
| 12 | ~505 ms (4x) | ~865 ms (6x) |
| 16 | ~660 ms (3x) | ~1190 ms (4.5x) |
| 24 | ~1005 ms (2x) | ~1810 ms (2.7x) |
| 32 | ~1300 ms (1.5x) | ~2350 ms (2.2x) |

Each sentence is generated in full then streamed, so that gen time is the
per-sentence latency. 8 steps sounds good and keeps latency low. Bump it up
if you want a bit more consistency on tricky text.

## Notes and limits

- omnivoice.cpp is alpha. Tokenizer and seed parity against the python
  runtime are not certified, so output can differ slightly from
  `omnivoice_tts`. For most voices it is indistinguishable.
- Cloning requires `ref_text`. No ASR fallback, by design.
- There is no `speed` control in the native ABI. Use `num_step` for the
  quality/latency trade. Sentence pacing is handled upstream by
  stream2sentence, so the native long-form chunker effectively never fires
  (it stays at its default 30s safety threshold and is not exposed).
- The engine is shared process-wide and kept warm across sessions, so a
  reconnect or personality switch does not reload the model.
