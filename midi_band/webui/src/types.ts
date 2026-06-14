// Shapes mirror what webui_server.py returns. Kept loose on purpose, the
// backend is the source of truth and we degrade gracefully on missing keys.

export interface Track {
  index: number;
  name: string;
  instrument: string;
  display_label: string;
  channels: number[];
  note_count: number;
  duration: number;
}

// a per-track instrument override: a soundfont bank + program. bank 0 is
// General MIDI, 128 is the drum kits, anything else is a variation bank.
export interface TrackProgram {
  bank: number;
  program: number;
}

// one instrument the loaded soundfont actually ships
export interface SfPreset {
  bank: number;
  program: number;
  name: string;
}

export interface SoundfontResponse {
  result: string;
  role?: "host" | "client";
  presets: SfPreset[];
  has_soundfont?: boolean;
}

export interface SongEntry {
  name: string;
  size: number;
}

export interface SongsResponse {
  result: string;
  instance?: string;
  library?: string;
  songs: SongEntry[];
}

export interface Status {
  result: string;
  instance?: string;
  role: "host" | "client";
  song?: string | null;
  tracks?: Track[];
  host_tracks?: number[];
  assignments?: Record<string, number[]>;
  duration?: number;
  position?: number;
  playing?: boolean;
  paused?: boolean;
  gain?: number;
  in_count_in?: boolean;
  count_in_remaining?: number;
  members?: string[];
  // track index (as string) -> instrument override the user picked.
  // legacy hosts may still send a bare GM program number here.
  track_programs?: Record<string, TrackProgram | number>;
  // false when this host has no soundfont, so its synth is silent
  has_soundfont?: boolean;
}

export interface SyncMember {
  name: string;
  is_host: boolean;
  tracks: string[];
  jitter_ms: number;
  rtt_ms: number;
  age_seconds: number | null;
  ready_session?: string | null;
  nack_reason?: string | null;
}

export interface SyncStatus {
  result: string;
  role: "host" | "client";
  host?: string;
  lead_seconds?: number;
  song?: string | null;
  members?: SyncMember[];
  message?: string;
}

// member name -> list of track indices. The host is always the first
// entry in Status.members and maps to host_tracks on the wire.
export type AssignmentMap = Record<string, number[]>;

export interface PresetSummary {
  name: string;
  song: string | null;
  members: string[];
  track_count: number;
  missing: string[];
  ready: boolean;
  song_loaded: boolean;
  song_available: boolean;
  updated?: number;
}

export interface PresetsResponse {
  result: string;
  presets: PresetSummary[];
  members?: string[];
}

export interface PresetLoadResult {
  result: string; // "ok" | "blocked" | "error"
  code?: string;
  preset?: string;
  song?: string;
  missing?: string[];
  orphan_tracks?: number[];
  forced?: boolean;
}

export interface ConductorResult {
  result: string; // "ok" | "error"
  message?: string;
  host_tracks?: number[];
  assignments?: Record<string, number[]>;
  reasoning?: string;
  unassigned_tracks?: number[];
  unknown_members?: string[];
}
