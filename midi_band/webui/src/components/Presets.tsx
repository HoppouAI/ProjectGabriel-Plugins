import { useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faXmark } from "@fortawesome/free-solid-svg-icons";
import type { PresetSummary } from "../types";
import { api } from "../api";
import { useToast } from "./Toasts";

interface Props {
  presets: PresetSummary[];
  isHost: boolean;
  canSave: boolean; // host with a song loaded
  onSaved: () => void; // refresh the preset list
  onLoaded: () => void; // resync the board + refresh state
}

export function Presets({ presets, isHost, canSave, onSaved, onLoaded }: Props) {
  const toast = useToast();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  if (!isHost) return null;

  async function save() {
    const n = name.trim();
    if (!n) return;
    setBusy("__save__");
    try {
      await api.savePreset(n);
      setName("");
      toast.push(`saved "${n}"`, "ok");
      onSaved();
    } catch (e: any) {
      toast.push(`save failed: ${e.message}`, "err");
    } finally {
      setBusy(null);
    }
  }

  async function load(p: PresetSummary, force: boolean) {
    setBusy(p.name);
    try {
      const res = await api.loadPreset(p.name, force);
      if (res.result === "blocked") {
        toast.push(`"${p.name}" needs ${(res.missing || []).join(", ")}, use Force`, "err");
        return;
      }
      const orphan = res.orphan_tracks?.length || 0;
      if (res.forced && orphan) {
        toast.push(`loaded "${p.name}", ${orphan} part${orphan > 1 ? "s" : ""} to reassign`, "ok");
      } else {
        toast.push(`loaded "${p.name}"`, "ok");
      }
      onLoaded();
    } catch (e: any) {
      toast.push(`load failed: ${e.message}`, "err");
    } finally {
      setBusy(null);
    }
  }

  async function del(p: PresetSummary) {
    if (!confirm(`Delete preset "${p.name}"?`)) return;
    setBusy(p.name);
    try {
      await api.deletePreset(p.name);
      toast.push(`deleted "${p.name}"`, "ok");
      onSaved();
    } catch (e: any) {
      toast.push(`delete failed: ${e.message}`, "err");
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="presets panel">
      <div className="panel__head">
        <h2 className="panel__title">
          Presets <span className="panel__count">{presets.length}</span>
        </h2>
      </div>

      <div className="presets__save">
        <input
          className="search presets__name"
          placeholder={canSave ? "name this layout..." : "load a song to save"}
          value={name}
          maxLength={60}
          disabled={!canSave || busy === "__save__"}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") save();
          }}
        />
        <button
          className="btn btn--accent"
          onClick={save}
          disabled={!canSave || !name.trim() || busy === "__save__"}
        >
          Save
        </button>
      </div>

      {presets.length === 0 ? (
        <div className="presets__empty">no presets yet, assign tracks then save the layout</div>
      ) : (
        <ul className="presets__list">
          {presets.map((p) => {
            const noSong = !p.song_available;
            const working = busy === p.name;
            return (
              <li key={p.name} className={`preset${p.ready && !noSong ? " preset--ready" : ""}`}>
                <div className="preset__top">
                  <span className="preset__name" title={p.name}>
                    {p.name}
                  </span>
                  <span className="preset__tracks">{p.track_count} trk</span>
                </div>
                <div className="preset__song" title={p.song || ""}>
                  {p.song || "no song"}
                </div>
                <div className="preset__row">
                  <span className="preset__state">
                    {noSong ? (
                      <span className="preset__warn">song missing</span>
                    ) : p.ready ? (
                      <span className="preset__ok">all bandmates here</span>
                    ) : (
                      <span className="preset__warn">needs {p.missing.join(", ")}</span>
                    )}
                  </span>
                  <span className="preset__actions">
                    {p.ready ? (
                      <button
                        className="btn btn--mini btn--accent"
                        onClick={() => load(p, false)}
                        disabled={noSong || working}
                      >
                        Load
                      </button>
                    ) : (
                      <button
                        className="btn btn--mini btn--warn"
                        onClick={() => load(p, true)}
                        disabled={noSong || working}
                        title={`force load without ${p.missing.join(", ")}`}
                      >
                        Force
                      </button>
                    )}
                    <button
                      className="btn btn--mini btn--ghost preset__del"
                      onClick={() => del(p)}
                      disabled={working}
                      title="delete preset"
                    >
                      <FontAwesomeIcon icon={faXmark} />
                    </button>
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
