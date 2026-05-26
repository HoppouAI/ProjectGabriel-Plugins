# OmniVoice TTS plugin

Wraps a local [OmniVoice](https://github.com/k2-fsa/OmniVoice) `omnivoice-serve`
REST server as a Gabriel TTS provider. Streams PCM audio chunk by chunk so
playback can start before generation finishes.

## Install

1. Copy this folder into `plugins/omnivoice/` of your Gabriel install.
2. Install OmniVoice and start the server somewhere:

   ```bash
   pip install omnivoice[serve]
   omnivoice-serve --model k2-fsa/OmniVoice
   ```

   By default it listens on `http://0.0.0.0:8000`.

3. (Optional but recommended) register a voice once so Gabriel always
   sounds the same:

   ```bash
   curl -X POST http://localhost:8000/v1/voices \
     -F "audio=@reference.wav" \
     -F "voice_id=gabriel"
   ```

4. In Gabriel's `config.yml`:

   ```yaml
   tts:
     external_provider: omnivoice

   plugins:
     omnivoice:
       base_url: "http://127.0.0.1:8000"
       voice_id: "gabriel"   # the id you registered
       num_steps: 24
       language: "English"
   ```

   Or copy `config.example.yml` to `plugins/omnivoice/config.yml` and put
   the same keys there. Local file wins over `config.yml`.

5. Restart Gabriel. The log should say:

   ```
   omnivoice tts registered. set tts.external_provider: omnivoice to use it.
   Using plugin TTS provider 'omnivoice' (Gemini audio will be discarded)
   ```

## Modes

- **Voice clone** -- set `voice_id`. Clones a previously registered voice.
- **Voice design** -- leave `voice_id` empty and set `instruct` (e.g.
  `"Female, Young Adult, High Pitch"`). OmniVoice generates a fresh voice
  from the description.
- **Auto** -- neither set. OmniVoice picks a voice itself.

## Notes

- Output is 16-bit PCM mono 24kHz, which matches Gabriel's default
  `audio.receive_sample_rate`. No resampling needed.
- The provider strips the WAV header that omnivoice-serve sends at the
  start of each streaming response and forwards only PCM frames to the
  playback pipeline.
- Sentences are split on the fly via `stream2sentence` and up to
  `max_concurrent` synthesise in parallel so sentence N+1 is ready by
  the time sentence N finishes playing.
- Barge-in cancels all in-flight requests and drains the queues. The
  next user turn starts cleanly.
