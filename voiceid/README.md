# voiceid plugin (speechbrain branch)

> **Heads up: this branch is NOT compatible with the `omnivoice_tts`
> plugin.** SpeechBrain 1.x registers lazy module redirects for a bunch
> of optional integrations (k2_fsa, fasttext wordemb, spacy/flair nlp,
> numba transducer loss). Once SpeechBrain is loaded in the process,
> transformers' AutoX integration discovery (which OmniVoice triggers
> during model load) walks those lazy modules and crashes with
> `Could not import module 'AutoFeatureExtractor'`. We tried shimming
> them in 0.3.1 and 0.3.2 and never got it fully stable. **If you want
> the better ECAPA-TDNN accuracy, use `pocket_tts` (or any non
> transformers TTS provider) instead of `omnivoice_tts`.** If you need
> OmniVoice, install `voiceid` from the `main` branch which uses
> Resemblyzer and stays out of transformers entirely.

Voice fingerprinting for ProjectGabriel. Gives the AI tools to remember
people by their voice and look them up later. Uses
[SpeechBrain ECAPA-TDNN](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb)
speaker embeddings (the current SOTA for speaker recognition, EER
~0.69% on VoxCeleb-O).

## What changed in 0.3

If you used 0.2.x this is a hard upgrade.

- Encoder swapped from Resemblyzer (256-dim, 2017 GE2E) to ECAPA-TDNN
  (192-dim, current SOTA). Way tighter clusters, way fewer mixups.
- Multiple embeddings stored per saved person (up to 12, configurable).
  The match is the **max cosine** across all of them, not an averaged
  centroid, so a quiet voice and an excited voice from the same
  person both still match.
- Top-1 must beat top-2 by a margin (default 0.06), otherwise the
  result is `unknown` with an "ambiguous" reason. This is what stops
  similar sounding people getting confused.
- The `best_guess` field in the unknown response is gone. The model
  was using it as a real answer instead of asking for the name.
- Existing `voices.npz` from 0.2.x is moved aside as
  `voices.npz.legacy_256d` on first start. Re-save voices to refingerprint
  them with the new encoder.
- Better resampler (`scipy.signal.resample_poly` if scipy is around)
  and adaptive RMS silence trim before embedding.

## What it does

- Subscribes to the host `mic_chunk` event (plugin api v3+) and keeps
  the last few seconds of mic audio in a ring buffer.
- On demand, runs the buffered audio through ECAPA-TDNN to get a
  192-dim L2-normalized speaker embedding.
- Saves embeddings under usernames to
  `data/plugins/voiceid/voices.npz` plus `voices.json` metadata. No
  pickle, safe to copy around.

## Tools exposed to the model

| Tool | What it does |
|---|---|
| `saveVoice(username)` | Fingerprint the person currently talking under that name. Call again with the same name from a different moment to add another capture, more captures = better recognition. |
| `identifyCurrentSpeaker()` | Returns either a confident match (`username` + `confidence`) or `unknown` with a `reason`. If unknown, the model is told to treat them as unknown and ask for the name. |
| `listSavedVoices()` | List everyone the AI can recognize. |
| `forgetVoice(username)` | Delete a saved voice. |
| `renameVoice(old_name, new_name)` | Rename a saved voice. |

## Install

This plugin needs SpeechBrain + torch + scipy. From your Gabriel install
root:

```powershell
.\bin\uv.exe pip install speechbrain torch torchaudio scipy numpy
```

First call to `identifyCurrentSpeaker` (or the background preload) will
download the ECAPA model (~14MB) into
`data/plugins/voiceid/ecapa_model/`. After that it's instant.

## Config (optional)

Add to `config.yml` under `plugins:`:

```yaml
plugins:
  voiceid:
    similarity_threshold: 0.4         # cosine similarity needed to match
    disambig_margin: 0.06             # top-1 must beat top-2 by this
    buffer_seconds: 5.0               # how much recent audio to keep
    min_audio_seconds: 1.5            # need at least this much speech to embed
    max_embeddings_per_voice: 12      # how many captures kept per person
```

### Tuning

| Symptom | Fix |
|---|---|
| Says unknown for someone you definitely saved | Lower `similarity_threshold` to 0.35 or 0.30, or save them again with `saveVoice` from a different moment to add a fresh capture. |
| Mixes up two similar sounding people | Raise `disambig_margin` to 0.08 or 0.10. Or call `saveVoice` on each of them a few more times so each profile has more captures. |
| False matches against random people | Raise `similarity_threshold` to 0.45 or 0.50. |
| Identification feels slow on first call | Normal, the first call loads the encoder. The plugin preloads it in a background thread on startup so by the time you talk it should be ready. |

ECAPA-TDNN cosine same-speaker scores typically land in the 0.45-0.85
range, different-speaker in the 0.0-0.3 range. Default of 0.4 is a
balanced middle.

## Notes

- Embeddings are computed against the raw mic stream. If you have a
  noisy environment or multiple people talking at once, fingerprints
  will drift. Best practice: save voices when only one person is
  speaking clearly, and call `saveVoice` more than once to capture
  different tones / volumes.
- Storage is a numpy `.npz` archive plus a metadata json. Both live
  in `data/plugins/voiceid/` (gitignored).
- The host's `mic_chunk` event respects the mute toggle, so muted
  audio never reaches this plugin.
- ECAPA inference runs on CPU by default and is fast enough for
  realtime. If you have a CUDA-capable torch install, the recognizer
  will pick GPU automatically.
