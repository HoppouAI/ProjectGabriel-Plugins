import { useMemo, useState } from "react";
import type { SongEntry } from "../types";
import { api } from "../api";
import { fmtSize } from "../hooks";
import { useToast } from "./Toasts";
import { Uploader } from "./Uploader";

interface Props {
  songs: SongEntry[];
  currentSong: string | null;
  isHost: boolean;
  onChanged: () => void;
}

export function Library({ songs, currentSong, isHost, onChanged }: Props) {
  const toast = useToast();
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const list = useMemo(() => {
    const needle = q.toLowerCase().trim();
    return needle ? songs.filter((s) => s.name.toLowerCase().includes(needle)) : songs;
  }, [songs, q]);

  async function load(name: string) {
    setBusy(name);
    try {
      await api.load(name);
      toast.push(`loaded ${name}`, "ok");
      onChanged();
    } catch (e: any) {
      toast.push(`load failed: ${e.message}`, "err");
    } finally {
      setBusy(null);
    }
  }

  async function remove(name: string) {
    if (!confirm(`Delete ${name}?`)) return;
    setBusy(name);
    try {
      await api.remove(name);
      toast.push(`deleted ${name}`, "ok");
      onChanged();
    } catch (e: any) {
      toast.push(`delete failed: ${e.message}`, "err");
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="library panel">
      <div className="panel__head">
        <h2 className="panel__title">
          Library <span className="panel__count">{list.length}</span>
        </h2>
      </div>

      <Uploader onUploaded={onChanged} />

      <input
        className="search"
        type="search"
        placeholder="filter songs..."
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />

      <ul className="songlist">
        {list.length === 0 ? (
          <li className="songlist__empty">
            {songs.length ? "no matches" : "library is empty, drop a .mid above"}
          </li>
        ) : (
          list.map((s) => (
            <li
              key={s.name}
              className={`song${s.name === currentSong ? " song--active" : ""}`}
            >
              <div className="song__main" title={s.name}>
                <span className="song__name">{s.name}</span>
                <span className="song__size">{fmtSize(s.size)}</span>
              </div>
              <div className="song__btns">
                {isHost && (
                  <button
                    className="btn btn--mini btn--accent"
                    disabled={busy === s.name}
                    onClick={() => load(s.name)}
                  >
                    Load
                  </button>
                )}
                <button
                  className="btn btn--mini btn--danger"
                  disabled={busy === s.name}
                  onClick={() => remove(s.name)}
                >
                  Del
                </button>
              </div>
            </li>
          ))
        )}
      </ul>
    </section>
  );
}
