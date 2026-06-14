import type {
  SongsResponse,
  Status,
  SyncStatus,
  PresetsResponse,
  PresetLoadResult,
  ConductorResult,
  SoundfontResponse,
  AudioSongsResponse,
  ModeResponse,
  BandMode,
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
  conduct: (prompt: string) =>
    postJson<ConductorResult>("/api/conductor", { prompt }),
  soundcheck: (duration = 8, bpm = 120) =>
    postJson("/api/soundcheck", { duration, bpm }),

  // manual assignment: host_tracks plus { clientName: trackIndices }
  assign: (host_tracks: number[], client_assignments: Record<string, number[]>) =>
    postJson("/api/assign", { host_tracks, client_assignments }),

  // saved assignment layouts
  listPresets: () => req<PresetsResponse>("/api/presets"),
  savePreset: (name: string) => postJson("/api/presets/save", { name }),
  loadPreset: (name: string, force = false) =>
    postJson<PresetLoadResult>("/api/presets/load", { name, force }),
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
