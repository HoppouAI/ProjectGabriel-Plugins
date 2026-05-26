# voiceid plugin

Voice fingerprinting for ProjectGabriel. Gives the AI tools to remember
people by their voice and look them up later. Built on
[Resemblyzer](https://github.com/resemble-ai/Resemblyzer) speaker
embeddings.

## What it does

- Subscribes to the host `mic_chunk` event (plugin api v3+) and keeps the
  last few seconds of mic audio in a ring buffer.
- On demand, runs the buffered audio through a Resemblyzer encoder to
  get a 256-dim speaker embedding.
- Saves embeddings under usernames to `data/plugins/voiceid/voices.npz`
  plus `voices.json` metadata. No pickle, safe to copy around.

## Tools exposed to the model

- `saveVoice(username)` -- fingerprint the person currently talking under that name. Call again with the same name to refine.
- `identifyCurrentSpeaker()` -- returns the closest matching saved voice and confidence, or `unknown` with a fallback instruction telling the model to use the vision context.
- `listSavedVoices()` -- list everyone the AI can recognize.
- `forgetVoice(username)` -- delete a saved voice.
- `renameVoice(old_name, new_name)` -- rename a saved voice.

## Install

This plugin needs resemblyzer + numpy. From the repo root:

```
uv pip install resemblyzer numpy
```

First call to any tool will download the encoder weights (~17MB) and
warm up the model. After that it's instant.

## Config (optional)

Add to `config.yml` under `plugins:`:

```yaml
plugins:
  voiceid:
    similarity_threshold: 0.75       # cosine similarity needed to match
    buffer_seconds: 5.0              # how much recent audio to keep
    min_audio_seconds: 1.5           # need at least this much speech to embed

    # auto-announce -- if on, plugin watches for the rising edge of
    # speech and pushes "[System: current speaker is X]" inline with the
    # audio via the host's send_realtime_text path, so the model sees
    # the label as part of the same turn as the audio. Works on both
    # 2.5 native-audio and 3.1 flash-live models.
    auto_announce: true
    announce_delay_seconds: 1.6      # wait this long after speech start before identifying
    announce_cooldown_seconds: 8.0   # min gap between announcements
    energy_threshold: 500.0          # int16 rms above this counts as voiced
    silence_gap_seconds: 0.8         # this much silence resets the edge detector
    announce_unknown: false          # also announce unknown speakers (off by default)
```

Raise `similarity_threshold` if you get false matches, lower it if it
keeps saying "unknown" for people it should know.

## Notes

- Embeddings are computed against the raw mic stream. If you have a
  noisy environment or multiple people talking at once, fingerprints
  will drift. Best practice: save voices when only one person is
  speaking clearly.
- Storage is a numpy `.npz` archive plus a metadata json. Both live in
  `data/plugins/voiceid/` (gitignored).
- The host's `mic_chunk` event respects the mute toggle, so muted audio
  never reaches this plugin.
