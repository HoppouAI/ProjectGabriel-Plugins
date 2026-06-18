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
  // friendly name to show instead of the filename, defaults to the name
  // minus its .mid extension when the host hasn't set a custom one
  display?: string;
}

export interface SongsResponse {
  result: string;
  instance?: string;
  library?: string;
  songs: SongEntry[];
}

// midi mode plays soundfont tracks, audio mode plays uploaded stems
export type BandMode = "midi" | "audio";

// how a loaded midi is split into assignable tracks. auto picks per-track
// vs per-channel, track forces per-track, channel forces per-channel (use
// it for channel-organized files that read as all "Drums").
export type ParseMode = "auto" | "track" | "channel";

// one uploaded stem inside an audio song folder
export interface AudioStem {
  index: number;
  label: string;
  file: string;
  sha: string;
  size: number;
  duration: number;
  samplerate: number;
  channels: number;
  original: string;
}

export interface AudioSong {
  name: string;
  stems: AudioStem[];
  duration: number;
  stem_count?: number;
}

export interface AudioSongsResponse {
  result: string;
  role?: "host" | "client";
  songs: AudioSong[];
}

export interface ModeResponse {
  result: string;
  mode: BandMode;
}

export interface Status {
  result: string;
  instance?: string;
  role: "host" | "client";
  mode?: BandMode;
  song?: string | null;
  // friendly label for what's playing: a live preset name or the song's
  // display name. falls back to song when absent.
  song_label?: string | null;
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
  // how the loaded midi was split. requested is what the user picked
  // (auto/track/channel), resolved is what auto settled on (track/channel).
  parse_mode?: ParseMode;
  resolved_parse_mode?: "track" | "channel";
  // keep-warm sync tone: a soft continuous hum every member plays to hold
  // VRChat's voice gate open. on/off plus a 0..1 level.
  tone_on?: boolean;
  tone_gain?: number;
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

// streaming conductor events, one JSON object per NDJSON line
export type ConductorEvent =
  | { type: "text"; delta: string }
  | { type: "tool"; tool: string; ok: boolean; summary: string }
  | { type: "applied" }
  | { type: "error"; message: string }
  | { type: "done" };

// a tool action the conductor took, shown as a chip under its reply
export interface ConductorToolNote {
  summary: string;
  ok: boolean;
}

// one message in the conductor chat transcript
export interface ConductorMessage {
  role: "user" | "assistant";
  text: string;
  tools?: ConductorToolNote[];
  error?: boolean;
}
