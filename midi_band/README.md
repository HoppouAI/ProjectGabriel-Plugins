# midi_band

Turns a group of Project Gabriel instances on the same LAN into a live
band. The host loads a MIDI file, assigns tracks (drums to one bandmate,
bass to another, lead to itself, etc), and on `startMidiBand` every
bandmate plays their assigned tracks at the exact same moment. Each
side shows the song name and its own track list in the VRChat chatbox.

A standalone client ships in the same folder so people can join the
band without installing Project Gabriel at all, just Python plus a
soundfont.

## How it works

- **Host** runs a tiny TCP server, owns the song library, parses the
  MIDI, and decides who plays what.
- **Clients** (Gabriel instances or standalone) connect to the host,
  receive a copy of the MIDI file plus their assigned track indices in
  a `prepare` message, and ack `ready` after a fast clock-sync ping
  burst.
- Once everyone is ready the host broadcasts `play` with an absolute
  start timestamp. Both sides schedule their own MIDI events against
  that timestamp through fluidsynth. Result: synced downbeat, ~30ms
  drift on a quiet LAN.
- Audio is synthesized locally on each side from the same soundfont,
  so no audio streams cross the network, just the MIDI file (small,
  usually <100KB) and protocol messages.

## Install

On every machine that will be in the band:

1. Copy the `midi_band/` folder into your Gabriel install's `plugins/`
   directory.

2. Install the Python deps:
   ```powershell
   .venv\Scripts\python.exe -m pip install mido pyfluidsynth
   ```

3. Install the **native** fluidsynth library (pyfluidsynth is just
   bindings, the real synth is a C library). On Windows you can skip
   this step, the plugin will auto-download the latest official build
   from FluidSynth's GitHub releases on first run and stash it under
   `data/plugins/midi_band/fluidsynth/`. Disable with
   `auto_install_fluidsynth: false` if you'd rather install it
   yourself.

   - Windows: auto-installed (or `choco install fluidsynth` / drop the
     dll on PATH manually)
   - macOS: `brew install fluid-synth`
   - Debian/Ubuntu: `apt install libfluidsynth3`

4. Drop a `.sf2` soundfont somewhere and point the config at it. Free
   options: GeneralUser GS, FluidR3_GM, Arachno SoundFont. Recommended
   path: `sfx/soundfonts/default.sf2`.

5. On the host machine only, drop a few `.mid` / `.midi` files into
   the library folder. Default is `plugins/midi_band/midi/` (created
   automatically on first start), so the easiest path is just to drop
   them next to the plugin's code. You can override with
   `library_dir:` in config if you want to point at a different folder.

6. Set `enabled: true` in `plugin.yml` and configure (see below).

## Config

You have two ways to configure the plugin, pick whichever you prefer.

**Option A: main `config.yml`**

```yaml
plugins:
  midi_band:
    role: host
    bind: 0.0.0.0
    host_address: 192.168.1.50
    port: 8784
    instance_name: gabriel_a
    soundfont: "sfx/soundfonts/default.sf2"
    library_dir: "sfx/midi"
    schedule_lead_seconds: 1.5
    synth_gain: 0.5
    audio_driver: ""           # autodetect (forced to dsound on Windows)
    chatbox_priority: 25
    webui_enabled: true        # tiny upload UI on the host
    webui_bind: 0.0.0.0
    webui_port: 8783
```

**Option B: local `config.yml`** inside the plugin folder

Copy `midi_band/config.yml.example` to `midi_band/config.yml` and edit
it. Same keys without the `plugins.midi_band:` wrapper. The local file
is gitignored. Local file wins per key, anything missing falls through
to the main config (or built-in defaults).

Set `role: host` on one machine, `role: client` on every other Gabriel
that should join the band as an additional musician.

## Tools the AI can call (host only)

Run these in this order for the typical flow:

1. `listMidiSongs()` -- what songs are in the library.
2. `loadMidiSong(title)` -- pick one, returns the track list with
   instrument names and note counts so you know what's in the song.
3. `listBandMembers()` -- who is in the band right now.
4. `assignBandTracks(host_tracks, client_assignments)` -- assign
   specific tracks to specific bandmates. Or just call
   `autoAssignBandTracks()` to spread tracks round-robin.
5. `startMidiBand()` -- the downbeat. Everyone fires together.
6. `stopMidiBand()` -- stop the performance.
7. `bandStatus()` -- check what's loaded and who's playing what.

When the plugin runs as `client`, only `bandStatus` works, the rest
return an error since only the host can manage the band.

## Standalone client

A self-contained client lives in [`standalone/`](standalone/) so people
can join the band without installing Project Gabriel. It has its own
`requirements.txt` and a `pyproject.toml` with `uv` support. See
[`standalone/README.md`](standalone/README.md) for full instructions.

Quick version:

```powershell
cd midi_band/standalone
uv sync
uv run standalone_client.py `
    --host 192.168.1.50 `
    --port 8784 `
    --name drummer `
    --soundfont C:\sf2\GeneralUser.sf2
```

Standalone clients dont need a midi library of their own, the host
ships them the file each time you start a song. Files land in the
cache dir so you can inspect them later if you want.

## Chatbox

Each bandmate (host AND clients, including standalone) shows their
own slice in the VRChat chatbox while playing:

```
midi: never_gonna_give_you_up.mid
-----
Drums
Bass
```

Priority defaults to 25 so it sits below the host's built-in music
displays (10/20) but above generic alerts.

## Limitations

- Sync is at the **start** of the song. We dont resync mid-song, so on
  long pieces with very different machine clocks you may hear drift
  after several minutes.
- Soundfont must be the same on every machine if you want them to
  sound identical, otherwise each client uses whatever its own .sf2
  has for that program number. (Different soundfonts are fine if you
  want each musician to have their own tone.)
- One song at a time. Calling `startMidiBand` while one is playing
  stops the current one first.
- MIDI files travel over a single JSON line as base64. Files larger
  than ~16MB will be rejected by the asyncio reader limit. Real songs
  are usually <200KB, this is rarely an issue.
- No audio streaming. If you want vocals or sampled audio, use the
  `duo_song` plugin instead.
