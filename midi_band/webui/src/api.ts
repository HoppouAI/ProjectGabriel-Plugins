import type {
  SongsResponse,
  Status,
  SyncStatus,
  PresetsResponse,
  PresetLoadResult,
  ConductorEvent,
  SoundfontResponse,
  AudioSongsResponse,
  ModeResponse,
  BandMode,
  ParseMode,
} from "./types";

async function req<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const r = await fetch(path, opts);
  let data: any = {};
  try {
    data = await r.json();
  } catch {
    /* empty body is fine for some endpoints */
  }
  if (!r.ok || data?.result === "error") {
    throw new Error(data?.message || `HTTP ${r.status}`);
  }
  return data as T;
}

function postJson<T>(path: string, body: unknown): Promise<T> {
  return req<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export const api = {
  songs: () => req<SongsResponse>("/api/songs"),
  status: () => req<Status>("/api/status"),
  sync: () => req<SyncStatus>("/api/sync"),

  load: (title: string) => postJson("/api/load", { title }),
  play: () => req("/api/play", { method: "POST" }),
  stop: () => req("/api/stop", { method: "POST" }),
  pause: () => req("/api/pause", { method: "POST" }),
  resume: () => req("/api/resume", { method: "POST" }),
  autoAssign: () => req("/api/auto_assign", { method: "POST" }),
  setVolume: (level: number) => postJson("/api/volume", { level }),
  // program is 0-127, bank is the soundfont bank (0 = GM, 128 = drum kits),
  // or pass program null to clear back to the midi's own instrument
  setTrackInstrument: (index: number, program: number | null, bank = 0) =>
    postJson("/api/track_instrument", { index, program, bank }),
  // instruments the host's soundfont ships, for the per-track picker
  soundfont: () => req<SoundfontResponse>("/api/soundfont"),
  // chat with the AI conductor, streamed as newline-delimited JSON events.
  // onEvent fires for each event as it lands. resolves when the turn ends.
  conductStream: async (
    prompt: string,
    onEvent: (ev: ConductorEvent) => void,
    signal?: AbortSignal,
  ) => {
    const r = await fetch("/api/conductor/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
      signal,
    });
    if (!r.ok || !r.body) {
      let msg = `HTTP ${r.status}`;
      try {
        const d = await r.json();
        msg = d?.message || msg;
      } catch {
        /* no body */
      }
      throw new Error(msg);
    }
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    const flush = (chunk: string) => {
      buf += chunk;
      let nl: number;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        try {
          onEvent(JSON.parse(line) as ConductorEvent);
        } catch {
          /* ignore a partial / bad line */
        }
      }
    };
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      flush(decoder.decode(value, { stream: true }));
    }
    const tail = buf.trim();
    if (tail) {
      try {
        onEvent(JSON.parse(tail) as ConductorEvent);
      } catch {
        /* ignore */
      }
    }
  },
  conductorReset: () => req("/api/conductor/reset", { method: "POST" }),
  soundcheck: (duration = 8, bpm = 120) =>
    postJson("/api/soundcheck", { duration, bpm }),
  setTone: (on: boolean, gain: number) =>
    postJson("/api/tone", { on, gain }),

  // manual assignment: host_tracks plus { clientName: trackIndices }
  assign: (host_tracks: number[], client_assignments: Record<string, number[]>) =>
    postJson("/api/assign", { host_tracks, client_assignments }),

  // saved assignment layouts
  listPresets: () => req<PresetsResponse>("/api/presets"),
  savePreset: (name: string) => postJson("/api/presets/save", { name }),
  loadPreset: (name: string, force = false) =>
    postJson<PresetLoadResult>("/api/presets/load", { name, force }),
  renamePreset: (old: string, name: string) =>
    postJson("/api/presets/rename", { old, name }),
  deletePreset: (name: string) => postJson("/api/presets/delete", { name }),

  upload: async (file: File) => {
    const fd = new FormData();
    fd.append("file", file, file.name);
    return req<{ result: string; name: string; size: number }>("/api/upload", {
      method: "POST",
      body: fd,
    });
  },

  remove: (name: string) =>
    req(`/api/songs/${encodeURIComponent(name)}`, { method: "DELETE" }),

  // give a midi a friendly display name, empty clears back to the filename
  renameSong: (name: string, display: string) =>
    postJson("/api/songs/rename", { name, display }),

  // re-read the loaded midi by track vs by channel (auto/track/channel).
  // use channel for files that read as all "Drums".
  setParseMode: (mode: ParseMode) => postJson("/api/parse_mode", { mode }),

  // ----- audio band mode -----
  getMode: () => req<ModeResponse>("/api/mode"),
  setMode: (mode: BandMode) => postJson<ModeResponse>("/api/mode", { mode }),
  loadAudio: (title: string) => postJson("/api/load_audio", { title }),
  audioSongs: () => req<AudioSongsResponse>("/api/audio_songs"),
  createAudioSong: (name: string) =>
    postJson("/api/audio_songs/create", { name }),
  renameStem: (song: string, index: number, label: string) =>
    postJson("/api/audio_songs/rename_stem", { song, index, label }),
  deleteStem: (song: string, index: number) =>
    postJson("/api/audio_songs/delete_stem", { song, index }),
  deleteAudioSong: (name: string) =>
    req(`/api/audio_songs/${encodeURIComponent(name)}`, { method: "DELETE" }),
  // one stem per request, the host detects the part label from the filename
  uploadStem: async (song: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file, file.name);
    return req<{ result: string; song: string; stem: { label: string } }>(
      `/api/audio_songs/upload?song=${encodeURIComponent(song)}`,
      { method: "POST", body: fd },
    );
  },
};
