import type {
  SongsResponse,
  Status,
  SyncStatus,
  PresetsResponse,
  PresetLoadResult,
  ConductorResult,
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
  setMemberVolume: (name: string, level: number) =>
    postJson("/api/member_volume", { name, level }),
  setTrackVolume: (index: number, level: number) =>
    postJson("/api/track_volume", { index, level }),
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
};
