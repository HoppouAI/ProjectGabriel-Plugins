import { useMemo, useRef, useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  faUpload,
  faRotate,
  faChevronRight,
  faChevronDown,
  faPen,
  faTrash,
} from "@fortawesome/free-solid-svg-icons";
import type { AudioSong, AudioStem } from "../types";
import { api } from "../api";
import { usePoll, fmtSize, fmtTime } from "../hooks";
import { useToast } from "./Toasts";

interface Props {
  currentSong: string | null;
  isHost: boolean;
  onChanged: () => void;
}

const MAX_STEMS = 12;

export function AudioLibrary({ currentSong, isHost, onChanged }: Props) {
  const toast = useToast();
  const { data, refresh } = usePoll(api.audioSongs, 2500);
  const [newName, setNewName] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const songs = useMemo(() => data?.songs || [], [data]);

  function bump() {
    refresh();
    onChanged();
  }

  async function create() {
    const n = newName.trim();
    if (!n) return;
    setBusy("__create__");
    try {
      await api.createAudioSong(n);
      setNewName("");
      setOpen(n);
      toast.push(`created ${n}`, "ok");
      bump();
    } catch (e: any) {
      toast.push(`create failed: ${e.message}`, "err");
    } finally {
      setBusy(null);
    }
  }

  async function load(name: string) {
    setBusy(name);
    try {
      await api.loadAudio(name);
      toast.push(`loaded ${name}`, "ok");
      bump();
    } catch (e: any) {
      toast.push(`load failed: ${e.message}`, "err");
    } finally {
      setBusy(null);
    }
  }

  async function delSong(name: string) {
    if (!confirm(`Delete song "${name}" and all its stems?`)) return;
    setBusy(name);
    try {
      await api.deleteAudioSong(name);
      toast.push(`deleted ${name}`, "ok");
      if (open === name) setOpen(null);
      bump();
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
          Stem Songs <span className="panel__count">{songs.length}</span>
        </h2>
      </div>

      {isHost && (
        <div className="presets__save">
          <input
            className="search presets__name"
            placeholder="new song name..."
            value={newName}
            maxLength={60}
            disabled={busy === "__create__"}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") create();
            }}
          />
          <button
            className="btn btn--accent"
            onClick={create}
            disabled={!newName.trim() || busy === "__create__"}
          >
            Add
          </button>
        </div>
      )}

      <ul className="songlist songlist--audio">
        {songs.length === 0 ? (
          <li className="songlist__empty">
            {isHost
              ? "no stem songs yet, add one above then upload its stems"
              : "no stem songs on this host"}
          </li>
        ) : (
          songs.map((s) => (
            <AudioSongRow
              key={s.name}
              song={s}
              isHost={isHost}
              active={s.name === currentSong}
              expanded={open === s.name}
              busy={busy === s.name}
              onToggle={() => setOpen(open === s.name ? null : s.name)}
              onLoad={() => load(s.name)}
              onDelete={() => delSong(s.name)}
              onChanged={bump}
            />
          ))
        )}
      </ul>
    </section>
  );
}

function AudioSongRow({
  song,
  isHost,
  active,
  expanded,
  busy,
  onToggle,
  onLoad,
  onDelete,
  onChanged,
}: {
  song: AudioSong;
  isHost: boolean;
  active: boolean;
  expanded: boolean;
  busy: boolean;
  onToggle: () => void;
  onLoad: () => void;
  onDelete: () => void;
  onChanged: () => void;
}) {
  const stems = song.stems || [];
  return (
    <li className={`song song--stack${active ? " song--active" : ""}`}>
      <div className="song__head">
        <button className="song__expand" onClick={onToggle} title="show stems">
          <FontAwesomeIcon icon={expanded ? faChevronDown : faChevronRight} />
        </button>
        <div className="song__main" title={song.name} onClick={onToggle}>
          <span className="song__name">{song.name}</span>
          <span className="song__size">
            {stems.length} stem{stems.length === 1 ? "" : "s"} &middot; {fmtTime(song.duration)}
          </span>
        </div>
        <div className="song__btns">
          {isHost && (
            <button
              className="btn btn--mini btn--accent"
              disabled={busy || !stems.length}
              onClick={onLoad}
              title={stems.length ? "load this song" : "upload stems first"}
            >
              Load
            </button>
          )}
          {isHost && (
            <button
              className="btn btn--mini btn--danger"
              disabled={busy}
              onClick={onDelete}
            >
              Del
            </button>
          )}
        </div>
      </div>

      {expanded && (
        <div className="stems">
          <div className="stems__list">
            {stems.map((st) => (
              <StemRow
                key={st.index}
                song={song.name}
                stem={st}
                isHost={isHost}
                onChanged={onChanged}
              />
            ))}
          </div>
          {isHost && (
            <StemUploader
              song={song.name}
              count={stems.length}
              onChanged={onChanged}
            />
          )}
        </div>
      )}
    </li>
  );
}

function StemRow({
  song,
  stem,
  isHost,
  onChanged,
}: {
  song: string;
  stem: AudioStem;
  isHost: boolean;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [label, setLabel] = useState(stem.label);
  const [busy, setBusy] = useState(false);

  async function rename() {
    const n = label.trim();
    if (!n || n === stem.label) {
      setEditing(false);
      return;
    }
    setBusy(true);
    try {
      await api.renameStem(song, stem.index, n);
      toast.push(`renamed to ${n}`, "ok");
      setEditing(false);
      onChanged();
    } catch (e: any) {
      toast.push(`rename failed: ${e.message}`, "err");
    } finally {
      setBusy(false);
    }
  }

  async function del() {
    if (!confirm(`Remove stem "${stem.label}"?`)) return;
    setBusy(true);
    try {
      await api.deleteStem(song, stem.index);
      toast.push(`removed ${stem.label}`, "ok");
      onChanged();
    } catch (e: any) {
      toast.push(`delete failed: ${e.message}`, "err");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stem">
      {editing ? (
        <input
          className="stem__input"
          value={label}
          autoFocus
          maxLength={40}
          disabled={busy}
          onChange={(e) => setLabel(e.target.value)}
          onBlur={rename}
          onKeyDown={(e) => {
            if (e.key === "Enter") rename();
            if (e.key === "Escape") {
              setLabel(stem.label);
              setEditing(false);
            }
          }}
        />
      ) : (
        <span className="stem__label" title={stem.original}>
          {stem.label}
        </span>
      )}
      <span className="stem__meta">
        {fmtTime(stem.duration)} &middot; {fmtSize(stem.size)}
      </span>
      {isHost && !editing && (
        <div className="stem__btns">
          <button className="iconbtn" title="rename" onClick={() => setEditing(true)} disabled={busy}>
            <FontAwesomeIcon icon={faPen} />
          </button>
          <button className="iconbtn iconbtn--danger" title="remove" onClick={del} disabled={busy}>
            <FontAwesomeIcon icon={faTrash} />
          </button>
        </div>
      )}
    </div>
  );
}

function StemUploader({
  song,
  count,
  onChanged,
}: {
  song: string;
  count: number;
  onChanged: () => void;
}) {
  const toast = useToast();
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const room = MAX_STEMS - count;

  async function send(files: FileList | null) {
    if (!files || !files.length) return;
    if (room <= 0) {
      toast.push(`a song holds at most ${MAX_STEMS} stems`, "err");
      return;
    }
    setBusy(true);
    let ok = 0;
    for (const f of Array.from(files).slice(0, room)) {
      try {
        const r = await api.uploadStem(song, f);
        ok++;
        toast.push(`${r.stem?.label || f.name}`, "ok");
      } catch (e: any) {
        toast.push(`${f.name}: ${e.message}`, "err");
      }
    }
    setBusy(false);
    if (ok) onChanged();
  }

  return (
    <label className={`stem-drop${busy ? " stem-drop--busy" : ""}`}>
      <input
        ref={inputRef}
        type="file"
        accept=".wav,.flac,.ogg,.oga,.mp3,.m4a,.aac"
        multiple
        hidden
        disabled={busy || room <= 0}
        onChange={(e) => {
          send(e.target.files);
          if (inputRef.current) inputRef.current.value = "";
        }}
      />
      <span className="stem-drop__icon" aria-hidden>
        <FontAwesomeIcon icon={busy ? faRotate : faUpload} spin={busy} />
      </span>
      <span className="stem-drop__text">
        {busy
          ? "uploading..."
          : room <= 0
            ? `full (${MAX_STEMS} stems max)`
            : `add stems (${room} left)`}
      </span>
    </label>
  );
}
