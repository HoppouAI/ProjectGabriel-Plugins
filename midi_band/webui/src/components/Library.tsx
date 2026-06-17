import { useMemo, useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faPen, faCheck, faXmark } from "@fortawesome/free-solid-svg-icons";
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

function shown(s: SongEntry): string {
  return s.display || s.name.replace(/\.midi?$/i, "");
}

export function Library({ songs, currentSong, isHost, onChanged }: Props) {
  const toast = useToast();
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  const list = useMemo(() => {
    const needle = q.toLowerCase().trim();
    if (!needle) return songs;
    return songs.filter(
      (s) =>
        s.name.toLowerCase().includes(needle) ||
        shown(s).toLowerCase().includes(needle),
    );
  }, [songs, q]);

  async function load(name: string) {
    setBusy(name);
    try {
      await api.load(name);
      toast.push(`loaded ${name.replace(/\.midi?$/i, "")}`, "ok");
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

  function startEdit(s: SongEntry) {
    setEditing(s.name);
    setDraft(shown(s));
  }

  async function saveEdit(name: string) {
    const next = draft.trim();
    setBusy(name);
    try {
      await api.renameSong(name, next);
      toast.push("renamed", "ok");
      setEditing(null);
      onChanged();
    } catch (e: any) {
      toast.push(`rename failed: ${e.message}`, "err");
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
          list.map((s) => {
            const isEditing = editing === s.name;
            const renamed = shown(s) !== s.name;
            return (
              <li
                key={s.name}
                className={`song${s.name === currentSong ? " song--active" : ""}`}
              >
                {isEditing ? (
                  <div className="song__edit">
                    <input
                      className="search song__rename"
                      value={draft}
                      autoFocus
                      maxLength={80}
                      placeholder="display name (blank = filename)"
                      disabled={busy === s.name}
                      onChange={(e) => setDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") saveEdit(s.name);
                        if (e.key === "Escape") setEditing(null);
                      }}
                    />
                    <button
                      className="iconbtn iconbtn--accent"
                      title="save name"
                      disabled={busy === s.name}
                      onClick={() => saveEdit(s.name)}
                    >
                      <FontAwesomeIcon icon={faCheck} />
                    </button>
                    <button
                      className="iconbtn"
                      title="cancel"
                      disabled={busy === s.name}
                      onClick={() => setEditing(null)}
                    >
                      <FontAwesomeIcon icon={faXmark} />
                    </button>
                  </div>
                ) : (
                  <>
                    <div className="song__main" title={s.name}>
                      <span className="song__name">{shown(s)}</span>
                      {renamed && <span className="song__file">{s.name}</span>}
                      <span className="song__size">{fmtSize(s.size)}</span>
                    </div>
                    <div className="song__btns">
                      {isHost && (
                        <button
                          className="iconbtn"
                          title="rename"
                          disabled={busy === s.name}
                          onClick={() => startEdit(s)}
                        >
                          <FontAwesomeIcon icon={faPen} />
                        </button>
                      )}
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
                  </>
                )}
              </li>
            );
          })
        )}
      </ul>
    </section>
  );
}
