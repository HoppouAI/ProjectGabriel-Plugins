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
