import { useCallback, useEffect, useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faMusic, faSliders, faRecordVinyl, faTowerBroadcast } from "@fortawesome/free-solid-svg-icons";
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

type TabId = "mixer" | "library" | "sync";
const TABS: { id: TabId; label: string; icon: typeof faMusic }[] = [
  { id: "mixer", label: "Mixer", icon: faSliders },
  { id: "library", label: "Library", icon: faRecordVinyl },
  { id: "sync", label: "Sync", icon: faTowerBroadcast },
];

export function App() {
  const toast = useToast();
  const { data: status, error: statusErr, refresh: refreshStatus } = usePoll(api.status, 1000);
  const { data: sync } = usePoll(api.sync, 1500);
  const { data: presetsData, refresh: refreshPresets } = usePoll(api.listPresets, 2500);
  const [songs, setSongs] = useState<SongEntry[]>([]);
  const [resyncToken, setResyncToken] = useState(0);
  const [tab, setTab] = useState<TabId>("mixer");

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
        <nav className="nav">
          {TABS.map((t) => (
            <button
              key={t.id}
              className={`nav__tab${tab === t.id ? " nav__tab--on" : ""}`}
              onClick={() => setTab(t.id)}
            >
              <FontAwesomeIcon icon={t.icon} />
              <span>{t.label}</span>
            </button>
          ))}
        </nav>
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

      {isHost && status?.has_soundfont === false && (
        <div className="banner banner--warn">
          No soundfont on this host, so its synth is <b>silent</b>. Volume changes won't be
          audible here until you point <code>soundfont:</code> at a <code>.sf2</code> file in the
          plugin config.
        </div>
      )}

      <Transport status={status} isHost={!!isHost} onAction={refreshStatus} />

      <div className={`view${tab === "mixer" ? "" : " view--hidden"}`}>
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
      </div>

      <div className={`view${tab === "library" ? "" : " view--hidden"}`}>
        <div className="view__cols">
          <Library songs={songs} currentSong={currentSong} isHost={!!isHost} onChanged={onChanged} />
          <Presets
            presets={presets}
            isHost={!!isHost}
            canSave={!!isHost && !!currentSong}
            onSaved={refreshPresets}
            onLoaded={onPresetLoaded}
          />
        </div>
      </div>

      <div className={`view${tab === "sync" ? "" : " view--hidden"}`}>
        <SyncPanel sync={sync} />
      </div>

      <footer className="foot">Project Gabriel &middot; midi_band</footer>
    </div>
  );
}
