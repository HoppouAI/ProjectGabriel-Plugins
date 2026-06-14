import { useCallback, useEffect, useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faMusic } from "@fortawesome/free-solid-svg-icons";
import { api } from "./api";
import { usePoll } from "./hooks";
import type { SongEntry } from "./types";
import { Transport } from "./components/Transport";
import { Library } from "./components/Library";
import { Presets } from "./components/Presets";
import { SyncPanel } from "./components/SyncPanel";
import { AssignBoard } from "./components/AssignBoard";
import { Conductor } from "./components/Conductor";
import { useToast } from "./components/Toasts";

export function App() {
  const toast = useToast();
  const { data: status, error: statusErr, refresh: refreshStatus } = usePoll(api.status, 1000);
  const { data: sync } = usePoll(api.sync, 1500);
  const { data: presetsData, refresh: refreshPresets } = usePoll(api.listPresets, 2500);
  const [songs, setSongs] = useState<SongEntry[]>([]);
  const [resyncToken, setResyncToken] = useState(0);

  const loadSongs = useCallback(async () => {
    try {
      const r = await api.songs();
      setSongs(r.songs || []);
    } catch (e: any) {
      toast.push(`library: ${e.message}`, "err");
    }
  }, [toast]);

  useEffect(() => {
    loadSongs();
  }, [loadSongs]);

  const isHost = status?.role === "host";
  const instance = status?.instance || "gabriel";
  const members = status?.members && status.members.length ? status.members : [instance];
  const hostName = instance;
  const tracks = status?.tracks || [];
  const currentSong = status?.song || null;
  const connected = !statusErr;
  const presets = presetsData?.presets || [];

  const onChanged = useCallback(() => {
    refreshStatus();
    loadSongs();
    refreshPresets();
  }, [refreshStatus, loadSongs, refreshPresets]);

  // a preset just landed: drop any draft on the board and pull fresh state
  const onPresetLoaded = useCallback(() => {
    setResyncToken((t) => t + 1);
    refreshStatus();
    refreshPresets();
    loadSongs();
  }, [refreshStatus, refreshPresets, loadSongs]);

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand__mark" aria-hidden>
            <FontAwesomeIcon icon={faMusic} />
          </span>
          <div className="brand__text">
            <span className="brand__name">midi_band</span>
            <span className="brand__sub">control room</span>
          </div>
        </div>
        <div className="topbar__right">
          <span className={`role role--${isHost ? "host" : "client"}`}>
            {isHost ? "HOST" : "CLIENT"}
          </span>
          <span className="topbar__instance">{instance}</span>
          <span className={`dot dot--${connected ? "on" : "off"}`} title={connected ? "connected" : "offline"} />
        </div>
      </header>

      {!isHost && status && (
        <div className="banner">
          This instance is a band <b>client</b>. Loading, assigning and playback live on the
          host's control room. You can still browse and upload to this machine's library.
        </div>
      )}

      <Transport status={status} isHost={!!isHost} onAction={refreshStatus} />

      <div className="workspace">
        <aside className="workspace__side">
          <Library songs={songs} currentSong={currentSong} isHost={!!isHost} onChanged={onChanged} />
          <Presets
            presets={presets}
            isHost={!!isHost}
            canSave={!!isHost && !!currentSong}
            onSaved={refreshPresets}
            onLoaded={onPresetLoaded}
          />
          <SyncPanel sync={sync} />
        </aside>
        <main className="workspace__main">
          <Conductor
            isHost={!!isHost}
            hasSong={!!currentSong}
            onApplied={refreshStatus}
          />
          <AssignBoard
            tracks={tracks}
            members={members}
            hostName={hostName}
            serverHostTracks={status?.host_tracks || []}
            serverAssignments={status?.assignments || {}}
            trackGains={status?.track_gains}
            memberGains={status?.member_gains}
            currentSong={currentSong}
            disabled={!isHost || !currentSong}
            resyncToken={resyncToken}
            onApplied={refreshStatus}
          />
        </main>
      </div>

      <footer className="foot">Project Gabriel &middot; midi_band</footer>
    </div>
  );
}
