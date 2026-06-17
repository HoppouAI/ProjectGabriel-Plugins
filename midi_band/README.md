# midi_band

Turns a group of Project Gabriel instances on the same LAN into a live
band. The host loads a MIDI file, assigns tracks (drums to one bandmate,
bass to another, lead to itself, etc), and on `startMidiBand` every
bandmate plays their assigned tracks at the exact same moment. Each
side shows the song name and its own track list in the VRChat chatbox.

There are two modes, flipped from a toggle in the web control room:

- **MIDI band** (default): the host ships a MIDI file, every bandmate
  synthesizes their assigned tracks locally through a soundfont.
- **Audio band**: you upload real audio **stems** (vocals, bass, drums,
  guitar, synth, up to 12 per song, eg the split output of a stem
  separator), the host streams each bandmate their assigned stems and
  everyone plays them back in sync. No soundfont needed for this mode.

A standalone client ships in the same folder so people can join the
band without installing Project Gabriel at all, just Python plus a
soundfont (or, for audio band mode, not even that).

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

In **audio band mode** the flow is the same (assign, ready, synced
play) but instead of a MIDI file the host hands each bandmate a short
list of stems to fetch. Bandmates download only the stems assigned to
them over HTTP from the host's control room server, decode and mix them
locally, then play the mix against the same absolute start timestamp.
Stems are cached by content hash so they only download once.

## Install

On every machine that will be in the band:

1. Copy the `midi_band/` folder into your Gabriel install's `plugins/`
   directory.

2. Install the Python deps:
   ```powershell
   .\bin\uv.exe pip install mido pyfluidsynth sounddevice soundfile numpy imageio-ffmpeg
   ```
   (`sounddevice`, `soundfile`, `numpy` and `imageio-ffmpeg` are only
   needed for **audio band mode**. Skip them if you only care about MIDI
   band mode. `imageio-ffmpeg` is host-only, it transcodes uploaded
   mp3/m4a stems to wav.)

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
    audio_device: ""           # output device for audio band mode (name or index)
    chatbox_priority: 25
    webui_enabled: true        # web control room on the host
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

## Web control room

When `webui_enabled: true` the host serves a small DAW-style control
room at `http://<host_ip>:<webui_port>/` (default port 8783). It's a
single-page app, no build step needed, the host ships a prebuilt copy.

A **MIDI / Audio Band** toggle in the top-left flips the whole control
room between the two modes. Switching modes stops playback and clears
the loaded song (track numbers don't carry across modes). Presets are
filtered to the active mode so you only see ones you can actually load.

What you can do from it:

- Browse the song library and drag-drop `.mid` files in to upload. Hit
  the pencil on a song to give it a friendly display name, so a file
  called `Words and Music by... BLINDING LIGHTS.mid` can just read
  "Blinding Lights" everywhere, in the list, the player, and the VRChat
  chatbox. The chatbox also drops the `.mid` extension on its own. Clear
  the name to fall back to the filename.
- Load a song and see its tracks with instrument names and note counts.
  Type 0 (single track) midis, which pack every instrument onto one
  track, are split out per channel so each instrument is its own
  assignable track instead of the whole song landing on one player.
- Manually assign tracks to each bandmate by dragging chips between
  lanes, or hit Spread to round-robin the unassigned pool. Apply pushes
  the layout to the band. You can also hand the **same** track to several
  bandmates at once, so a single Choir Aahs or string part turns into a
  whole section instead of one lonely voice. Open a track's `...` menu
  and tick every bandmate who should play it (or "Play on everyone"), a
  doubled track shows a small count badge on its chips.
- Chat with the **AI conductor**: click it in the sidebar to pop open a
  chat window in the middle of the screen, tell it what you're going for
  ("stripped back and moody, keep me on piano", "make it huge, double the
  strings") and it streams a reply back while it works. It arranges the
  song for you, stacking several bandmates on one part when you ask for
  something big or choral, and it can re-voice a track too ("give the lead
  a warm pad", "turn the bass into a synth"), digging through your
  soundfont's own sounds to find the right one. It's a real back and
  forth, so keep refining and it remembers the conversation, hit the
  refresh icon for a clean slate. Needs `google-genai` and a Gemini API
  key, see `conductor_api_key` / `conductor_model` in `config.yml.example`.
  If the key or package is missing the rest of the control room still
  works, the conductor just reports it can't run.
- Swap a track's instrument: open its `...` menu and pick a different
  one, or "Default (from MIDI)" to fall back to whatever the song asked
  for. The picker is built from your soundfont's own presets, grouped by
  bank, so you get the real variation banks and named patches the .sf2
  ships, not just the 128 General MIDI names. Drum tracks list the
  soundfont's drum kits instead. Takes effect on the next play and
  follows the track to whichever bandmate is playing it. Overridden
  chips show the new instrument and a small swap marker. (Hosts without
  a readable soundfont fall back to the plain GM list.)
- Save the current layout as a named preset and reload it later. A
  preset loads straight away when its bandmates are connected, or you
  can force load without them and the missing parts drop back into the
  pool to reassign. Presets remember the track instrument swaps too, and
  are stored on the host under `data/plugins/midi_band/presets.json`.
  Rename one any time with its pencil button. While a preset is the live
  layout its name shows in the chatbox instead of the song's, so the band
  reads as "Crazy Train (Full Band)" rather than the raw file.
- Drive playback: play, pause, resume, stop, soundcheck, master volume.
- Flip on the **sync tone**: a soft continuous hum every band member plays
  at an adjustable volume. It exists for VRChat. VRChat gates each speaker's
  voice and re-buffers it whenever they go quiet, so on higher-ping lobbies
  the band drifts apart between phrases. A constant low hum keeps everyone's
  voice channel open and warm so the per-phrase drift mostly goes away. Set
  it just loud enough to clear VRChat's voice activation, low enough to bury
  under the music. Won't fix a bad-ping lobby on its own, but it noticeably
  steadies things. The toggle and its volume are host only and apply to the
  whole band at once.
- Watch per-member sync health (jitter, rtt, age) live.

No auth, so only enable it on a trusted LAN. Set
`webui_bind: 127.0.0.1` to keep it local-only.

## Audio band mode

Flip the **Audio Band** toggle in the control room to play real audio
stems instead of synthesized MIDI. Same band, same sync, same assign
board, you're just handing out audio files instead of MIDI tracks.

How to use it:

1. Toggle the control room to **Audio Band**.
2. In the Library tab, type a song name and hit **Add** to make an
   empty stem song.
3. Expand the song and drop its stems into the uploader (one row per
   file, up to 12). Accepts `.wav`, `.flac`, `.ogg`, plus `.mp3`,
   `.m4a`, `.aac` (those get transcoded to wav on upload, needs
   `imageio-ffmpeg`). Each stem's part name (vocals, bass, drums...) is
   auto-detected from the filename, eg a file like
   `song_(vocals)_BS-Roformer.wav` becomes the "vocals" stem. Click the
   pencil to rename a stem, the trash to remove one.
4. Hit **Load** on the song. Each stem shows up as an assignable track
   on the board.
5. Assign stems to bandmates exactly like MIDI tracks, drag chips or use
   the `...` menu (you can still stack the same stem on several
   bandmates, or let the AI conductor split them). The per-track
   instrument picker is hidden in this mode, stems are already audio.
6. Hit play. Each bandmate downloads just their assigned stems from the
   host, mixes them, and everyone starts together.

Notes:

- Stems are streamed over HTTP from the host's control room server, so
  `webui_enabled` must be on (it is by default) for clients to fetch
  them. Host-local playback works either way.
- Bandmates cache downloaded stems by content hash under
  `<cache_dir>/stems/`, so re-playing a song doesn't re-download.
- Stem songs live on the host under
  `data/plugins/midi_band/audio_songs/<song>/` with a `manifest.json`
  plus the stem files. Safe to back up or hand-edit.
- Pick the output device for audio band mode with `audio_device:` in
  config (name substring or device index), same idea as the MIDI
  `audio_driver`/device. Leave blank for the system default.
- Needs `sounddevice`, `soundfile` and `numpy` installed. If they're
  missing the toggle reports audio mode is unavailable and MIDI mode
  keeps working.

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
  want each musician to have their own tone.) Variation banks and drum
  kits picked in the instrument menu are bank+program pairs, so a
  bandmate whose soundfont lacks that exact bank quietly falls back to
  the General MIDI version of the patch instead of going silent.
- One song at a time. Calling `startMidiBand` while one is playing
  stops the current one first.
- MIDI files travel over a single JSON line as base64. Files larger
  than ~16MB will be rejected by the asyncio reader limit. Real songs
  are usually <200KB, this is rarely an issue. (Audio stems don't go
  through this path, they stream over HTTP instead.)
- Audio band mode mixes a bandmate's assigned stems down to one stereo
  buffer per machine, so individual stem volume isn't adjustable mid
  song, only the master. Split a part across machines if you want
  independent control.
