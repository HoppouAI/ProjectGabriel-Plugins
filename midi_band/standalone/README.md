# midi_band standalone client

This is a tiny self-contained client for joining a Project Gabriel
[`midi_band`](../README.md) host as a musician. You don't need to
install Project Gabriel itself, just Python plus a soundfont.

The script reaches up one folder to the sibling `midi_band/` Python
package for the shared protocol, player and client code, so keep this
folder right next to the `midi_band/` folder when you ship it to a
friend (the easiest way is to copy the whole `midi_band/` directory).

## Install

You also need the **native fluidsynth library** on the system. The
Python `pyfluidsynth` package is just bindings.

On Windows you can skip this and the standalone client will
auto-download the latest official build from
[FluidSynth GitHub releases](https://github.com/FluidSynth/fluidsynth/releases)
the first time you run it, extracting it under `./vendor/fluidsynth/`.
Pass `--no-auto-install` to disable that, or `--fluidsynth-dir <path>`
to change where it goes.

For macOS / Linux install via your package manager:

- macOS: `brew install fluid-synth`
- Debian/Ubuntu: `apt install libfluidsynth3`

Then install the Python deps with whichever toolchain you prefer.

### uv (recommended)

```powershell
cd midi_band/standalone
uv sync
uv run standalone_client.py `
    --host 192.168.1.50 `
    --port 8766 `
    --name drummer `
    --soundfont C:\sf2\GeneralUser.sf2
```

### plain pip

```powershell
cd midi_band/standalone
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python standalone_client.py `
    --host 192.168.1.50 `
    --port 8766 `
    --name drummer `
    --soundfont C:\sf2\GeneralUser.sf2
```

## Configure once, run with no args

Copy `config.yml.example` to `config.yml` next to this README and fill
in your `host`, `name`, and `soundfont` (the rest have sane defaults).
Then the script needs no CLI args:

```powershell
uv run standalone_client.py
```

CLI args still override config values, so you can keep `name: drummer`
in the file and one-shot a different role with
`uv run standalone_client.py --name bassist` whenever you want.

The local `config.yml` is gitignored.

## Args

All args are optional if the corresponding key is set in `config.yml`.
Missing required values (`host`, `name`, `soundfont`) error out with
a clear message.

- `--config` -- path to a config.yml. Defaults to `./config.yml`.
- `--host` / `--port` -- the band host's address. Port defaults to 8766.
- `--name` -- shows up in `listBandMembers` on the host's AI. Pick
  something the AI will recognize (e.g. `drummer`, `bassist`,
  `bandmate_alice`).
- `--soundfont` -- path to a `.sf2` file. **Required.** Use the same
  soundfont as the rest of the band if you want them to sound
  identical, or a different one if you want your own tone.
- `--gain` -- 0.0 to 2.0, default 0.5.
- `--driver` -- fluidsynth audio driver. Blank = autodetect.
  - windows: `dsound`, `wasapi`
  - linux: `alsa`, `pulseaudio`, `pipewire`
  - mac: `coreaudio`
- `--device` -- output device name. Blank = system default. Set to a
  specific device (e.g. a virtual audio cable) to route this client's
  audio somewhere specific. See "Multiple instances" below.

## Multiple instances (one bandmate per VRChat window)

Each running instance is one bandmate. To have several you need
each one's audio to land on its own input so VRChat instances pick up
different players.

### The easy way: launcher GUI

Run the included tkinter launcher to manage all your bandmates from
one window:

```powershell
uv run launcher_gui.py
```

It lets you add as many bandmate rows as you want, each with its own
name, output device dropdown, soundfont and gain. Start/stop them
individually or all at once. The roster persists to `bandmates.yml`.

For the device dropdown to be populated with real device names, also
install the `gui` extra:

```powershell
uv pip install -e ".[gui]"
```

(without it the device field becomes a free-text entry, which still
works, you just have to type the device name yourself.)

### The manual way

1. Install [VB-CABLE](https://vb-audio.com/Cable/) (free). For more
   than one extra cable get the A+B and C+D bundles too, or use
   Voicemeeter Banana / Potato.
2. Make a folder per bandmate, copy this `standalone/` folder into
   each, and give each its own `config.yml`:

   ```yaml
   # drummer/config.yml
   host: "192.168.1.50"
   name: "drummer"
   soundfont: "C:/sf2/GeneralUser.sf2"
   driver: "dsound"
   device: "CABLE Input (VB-Audio Virtual Cable)"
   ```

   ```yaml
   # bassist/config.yml
   host: "192.168.1.50"
   name: "bassist"
   soundfont: "C:/sf2/GeneralUser.sf2"
   driver: "dsound"
   device: "CABLE-A Input (VB-Audio Cable A)"
   ```

3. In each VRChat instance, set its mic input to the matching cable's
   *Output* device (e.g. `CABLE Output` for drummer's instance).
4. Launch the clients:

   ```powershell
   Start-Process -WorkingDirectory drummer  uv -ArgumentList "run","standalone_client.py"
   Start-Process -WorkingDirectory bassist  uv -ArgumentList "run","standalone_client.py"
   ```

   Each spawns its own console window so you can see each client's
   sync status independently.

To find the exact device name strings, run `python -m sounddevice` or
look in Windows' Sound settings under "Recording" for the cable
*Output* names and "Playback" for the cable *Input* names. fluidsynth
expects the playback (input) side, since that's where it sends audio.
- `--cache-dir` -- where to save MIDI files received from the host.
  Default `./midi_cache/`.
- `--fluidsynth-dir` -- where to extract the auto-downloaded
  fluidsynth on Windows. Default `./vendor/fluidsynth/`.
- `--no-auto-install` -- skip auto-downloading fluidsynth.
- `--log-level` -- Python logging level, default INFO.

## How it works

1. Connect to the host over TCP.
2. Receive a `prepare` message: full MIDI file (base64) + the list of
   track indices the host assigned to you + display names.
3. Save the MIDI to the cache dir, run a quick clock-sync ping burst,
   send back `ready`.
4. Receive a `play` message with an absolute server timestamp.
5. Decode the assigned tracks into a sorted event list and schedule
   them through fluidsynth so the very first note lines up with the
   host (and every other bandmate) within ~30ms on a quiet LAN.

Ctrl+C to quit.

## License

AGPL-3.0, same as Project Gabriel.
