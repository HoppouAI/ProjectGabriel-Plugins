import { useCallback, useEffect, useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faMusic, faSliders, faRecordVinyl, faTowerBroadcast } from "@fortawesome/free-solid-svg-icons";
import { api } from "./api";
import { usePoll } from "./hooks";
import type { SongEntry, SfPreset, BandMode } from "./types";
import { Transport } from "./components/Transport";
import { Library } from "./components/Library";
import { AudioLibrary } from "./components/AudioLibrary";
import { Presets } from "./components/Presets";
import { SyncPanel } from "./components/SyncPanel";
import { AssignBoard } from "./components/AssignBoard";
import { Conductor } from "./components/Conductor";
import { useToast } from "./components/Toasts";

type TabId = "board" | "library" | "sync";
const TABS: { id: TabId; label: string; icon: typeof faMusic }[] = [
  { id: "board", label: "Board", icon: faSliders },
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
  const [tab, setTab] = useState<TabId>("board");
  const [soundfont, setSoundfont] = useState<SfPreset[]>([]);

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
  const mode: BandMode = status?.mode === "audio" ? "audio" : "midi";
  const audioMode = mode === "audio";
  const [modeBusy, setModeBusy] = useState(false);

  const switchMode = useCallback(
    async (m: BandMode) => {
      if (modeBusy || m === mode) return;
      setModeBusy(true);
      try {
        await api.setMode(m);
        setResyncToken((t) => t + 1);
        refreshStatus();
        refreshPresets();
      } catch (e: any) {
        toast.push(`mode: ${e.message}`, "err");
      } finally {
        setModeBusy(false);
      }
    },
    [mode, modeBusy, refreshStatus, refreshPresets, toast],
  );

  // soundfont presets only change when the host swaps its .sf2, so grab them
  // once we know we're the host and hold onto them
  useEffect(() => {
    if (!isHost || soundfont.length) return;
    let cancelled = false;
    api
      .soundfont()
      .then((r) => {
        if (!cancelled) setSoundfont(r.presets || []);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [isHost, soundfont.length]);

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
      <aside className="sidebar">
        <div className="brand">
          <span className="brand__mark" aria-hidden>
            <FontAwesomeIcon icon={faMusic} />
          </span>
          <div className="brand__text">
            <span className="brand__name">midi_band</span>
            <span className="brand__sub">control room</span>
          </div>
        </div>

        {isHost && (
          <div className="modeswitch" role="group" aria-label="band mode">
            <button
              className={`modeswitch__opt${!audioMode ? " is-on" : ""}`}
              onClick={() => switchMode("midi")}
              disabled={modeBusy}
            >
              MIDI
            </button>
            <button
              className={`modeswitch__opt${audioMode ? " is-on" : ""}`}
              onClick={() => switchMode("audio")}
              disabled={modeBusy}
            >
              Audio Band
            </button>
          </div>
        )}

        <nav className="sidebar__nav">
          {TABS.map((t) => (
            <button
              key={t.id}
              className={`navbtn${tab === t.id ? " navbtn--on" : ""}`}
              onClick={() => setTab(t.id)}
            >
              <FontAwesomeIcon icon={t.icon} />
              <span>{t.label}</span>
            </button>
          ))}
        </nav>

        <Conductor isHost={!!isHost} hasSong={!!currentSong} onApplied={refreshStatus} />

        <div className="sidebar__spacer" />

        <div className="sidebar__status">
          <div className="sidebar__statusrow">
            <span className={`role role--${isHost ? "host" : "client"}`}>
              {isHost ? "HOST" : "CLIENT"}
            </span>
            <span
              className={`dot dot--${connected ? "on" : "off"}`}
              title={connected ? "connected" : "offline"}
            />
          </div>
          <div className="sidebar__instance">{instance}</div>
          <div className="sidebar__mates">{members.length} on stage</div>
        </div>
      </aside>

      <main className="main">
        <div className="main__inner">
          {!isHost && status && (
            <div className="banner">
              This instance is a band <b>client</b>. Loading, assigning and playback live on the
              host's control room. You can still browse and upload to this machine's library.
            </div>
          )}

          {isHost && !audioMode && status?.has_soundfont === false && (
            <div className="banner banner--warn">
              No soundfont on this host, so its synth is <b>silent</b>. Volume changes won't be
              audible here until you point <code>soundfont:</code> at a <code>.sf2</code> file in
              the plugin config.
            </div>
          )}

          <Transport status={status} isHost={!!isHost} audioMode={audioMode} onAction={refreshStatus} />

          <div className={`view${tab === "board" ? "" : " view--hidden"}`}>
            <AssignBoard
              tracks={tracks}
              members={members}
              hostName={hostName}
              serverHostTracks={status?.host_tracks || []}
              serverAssignments={status?.assignments || {}}
              trackPrograms={status?.track_programs}
              soundfont={soundfont}
              currentSong={currentSong}
              audioMode={audioMode}
              parseMode={status?.parse_mode}
              resolvedParseMode={status?.resolved_parse_mode}
              disabled={!isHost || !currentSong}
              resyncToken={resyncToken}
              onApplied={refreshStatus}
            />
          </div>

          <div className={`view${tab === "library" ? "" : " view--hidden"}`}>
            <div className="view__cols">
              {audioMode ? (
                <AudioLibrary currentSong={currentSong} isHost={!!isHost} onChanged={onChanged} />
              ) : (
                <Library songs={songs} currentSong={currentSong} isHost={!!isHost} onChanged={onChanged} />
              )}
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
      </main>
    </div>
  );
}
