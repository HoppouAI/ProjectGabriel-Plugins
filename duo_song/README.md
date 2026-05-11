# duo_song

Lets two Gabriel instances on the same local network sing a duet
together. Each duet song is **two audio files**, one for each singer:

```
sfx/music/duo/
  AwesomeSong PT1.mp3       <- host plays this
  AwesomeSong PT2.mp3       <- partner plays this
  another-track_pt1.ogg
  another-track_pt2.ogg
```

The plugin scans `sfx/music/duo/` for filenames ending in `PT1` / `PT2`
(case insensitive, separators ` `, `_`, `-`, `.` all fine) and pairs
them up by base name. Both files are needed for the song to be playable.

When `startDuoSong` is called, the host plays `PT1` and tells the client
to play `PT2`. A small ping/pong handshake makes sure both pygame
mixers fire at the same shared timestamp, so the two halves of the duet
line up.

Each side only needs **its own part** on disk. The host needs both PT1
and PT2 (PT1 to play, PT2 so it can verify the song is complete before
asking the client to fire). The client only really needs PT2.

## Install

1. Copy the `duo_song/` folder into the host's `plugins/` directory on
   both machines.
2. `pip install pygame` on both.
3. Drop your duet pairs into `sfx/music/duo/` on both machines using
   the `<title> PT1.<ext>` / `<title> PT2.<ext>` naming. Filenames must
   match between the two machines.
4. Set `enabled: true` in `plugin.yml`.

## Config

Two equivalent ways to configure, pick whichever you prefer.

**Option A: main `config.yml`**

```yaml
plugins:
  duo_song:
    role: host
    local_part: 1             # 1 or 2 (defaults: host=1, client=2)
    bind: 0.0.0.0
    host_address: 192.168.1.50
    port: 8765
    instance_name: gabriel_a
    library_dir: ""           # blank = sfx/music/duo
    schedule_lead_seconds: 1.2
    chatbox_priority: 30
    volume: 0.6
    auto_reconnect_seconds: 5
```

**Option B: local `duo_song/config.yml`**

Copy `duo_song/config.yml.example` to `duo_song/config.yml` and edit
it. The local file is gitignored. Same keys as Option A but no
`plugins.duo_song:` wrapper. When both are set, the local file wins
per key.

Set `role: host` on one machine, `role: client` on the other. The host
also plays its part locally, it isnt only a relay.

If you want to lock which side sings which part regardless of role, set
`local_part: 1` or `local_part: 2` on each instance. The two sides must
not pick the same part, the other side will reject the prepare with a
`part mismatch` message in the logs.

## Tools the AI can call

- `startDuoSong(title)` -- start the named duet, host fires PT1, client
  fires PT2, both at the same scheduled instant.
- `stopDuoSong()` -- stop both sides.
- `listDuoSongs()` -- list playable pairs and any half-pairs that are
  missing a side.
- `duoStatus()` -- whats playing, who is connected, which part this
  instance sings.

## Chatbox

While a duet is playing, both instances show:

```
duo: <song title> [01:23/03:45]
```

Priority defaults to 30 so it sits below the host's own music displays
(10/20).

## Sync details

- On `startDuoSong`, host broadcasts `prepare`. Each client checks it
  has its part on disk, fires a 4-ping burst (~200ms) to tighten its
  clock offset against the host, then sends `ready`.
- Host waits up to 1.5s for those acks, then picks the absolute start
  time `schedule_lead_seconds` in the future and broadcasts `play`.
- Both sides schedule playback for that exact server timestamp. Typical
  drift on a quiet LAN is under ~30ms at the moment of start.
- We dont resync mid-song.

## Limitations

- One duet at a time, starting a new one stops the current one.
- Files must be PCM-decodeable by `pygame.mixer.music` (mp3 / ogg /
  wav / flac / m4a / opus depending on your SDL build).
- Filenames must match exactly between host and client (just the base,
  PT1/PT2 marker can use any of the supported separators).
- This isnt audio streaming, both sides need their own part already on
  disk.
