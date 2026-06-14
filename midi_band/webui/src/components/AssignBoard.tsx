import { useEffect, useMemo, useRef, useState } from "react";
import {
  DndContext,
  DragOverlay,
  PointerSensor,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
  closestCenter,
} from "@dnd-kit/core";
import type { DragEndEvent, DragStartEvent } from "@dnd-kit/core";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faEllipsis, faRightLeft } from "@fortawesome/free-solid-svg-icons";
import type { Track, AssignmentMap } from "../types";
import { familyFor, GM_PROGRAMS, GM_GROUPS } from "../instruments";
import { api } from "../api";
import { useToast } from "./Toasts";

const POOL = "__pool__";

interface Props {
  tracks: Track[];
  members: string[]; // [host, ...clients]
  hostName: string;
  serverHostTracks: number[];
  serverAssignments: AssignmentMap;
  trackPrograms?: Record<string, number>;
  currentSong?: string | null;
  disabled: boolean;
  resyncToken?: number;
  onApplied: () => void;
}

function buildDraft(
  members: string[],
  hostName: string,
  hostTracks: number[],
  assignments: AssignmentMap,
): AssignmentMap {
  const draft: AssignmentMap = {};
  for (const m of members) draft[m] = [];
  draft[hostName] = [...hostTracks];
  for (const [name, idxs] of Object.entries(assignments)) {
    draft[name] = [...(idxs || [])];
  }
  // make sure every member has a key even if not in assignments
  for (const m of members) if (!draft[m]) draft[m] = [];
  return draft;
}

function Chip({
  track,
  inPool,
  onMenu,
  programOverride,
}: {
  track: Track;
  inPool: boolean;
  onMenu: (e: React.MouseEvent, idx: number) => void;
  programOverride?: number;
}) {
  const overLabel =
    typeof programOverride === "number" ? GM_PROGRAMS[programOverride] : null;
  const baseLabel = track.display_label || track.instrument || track.name || `track ${track.index}`;
  const fam = familyFor(overLabel || track.display_label || track.instrument);
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({
    id: `track:${track.index}`,
  });
  const label = overLabel || baseLabel;
  return (
    <div
      ref={setNodeRef}
      className={`chip${isDragging ? " chip--dragging" : ""}${inPool ? " chip--pool" : ""}`}
      style={{ ["--fam" as any]: fam.color }}
      {...listeners}
      {...attributes}
    >
      <span className="chip__glyph" aria-hidden>
        <FontAwesomeIcon icon={fam.icon} />
      </span>
      <span className="chip__label">{label}</span>
      {overLabel && (
        <span className="chip__swap" title={`overridden, playing as ${overLabel}`} aria-hidden>
          <FontAwesomeIcon icon={faRightLeft} />
        </span>
      )}
      <button
        className="chip__menu"
        title="assign"
        onPointerDown={(e) => e.stopPropagation()}
        onClick={(e) => onMenu(e, track.index)}
      >
        <FontAwesomeIcon icon={faEllipsis} />
      </button>
    </div>
  );
}

function Lane({
  id,
  title,
  subtitle,
  badge,
  tracks,
  inPool,
  onMenu,
  trackPrograms,
}: {
  id: string;
  title: string;
  subtitle?: string;
  badge?: string;
  tracks: Track[];
  inPool: boolean;
  onMenu: (e: React.MouseEvent, idx: number) => void;
  trackPrograms?: Record<string, number>;
}) {
  const { setNodeRef, isOver } = useDroppable({ id: `lane:${id}` });
  return (
    <div
      ref={setNodeRef}
      className={`lane${inPool ? " lane--pool" : ""}${isOver ? " lane--over" : ""}`}
    >
      <div className="lane__head">
        <div className="lane__title">
          {badge && <span className="lane__badge">{badge}</span>}
          <span>{title}</span>
        </div>
        <span className="lane__count">{tracks.length}</span>
      </div>
      {subtitle && <div className="lane__sub">{subtitle}</div>}
      <div className="lane__body">
        {tracks.length === 0 ? (
          <div className="lane__empty">{inPool ? "all tracks assigned" : "drop tracks here"}</div>
        ) : (
          tracks.map((t) => (
            <Chip
              key={t.index}
              track={t}
              inPool={inPool}
              onMenu={onMenu}
              programOverride={trackPrograms?.[String(t.index)]}
            />
          ))
        )}
      </div>
    </div>
  );
}

export function AssignBoard({
  tracks,
  members,
  hostName,
  serverHostTracks,
  serverAssignments,
  trackPrograms,
  currentSong,
  disabled,
  resyncToken,
  onApplied,
}: Props) {
  const toast = useToast();
  const [draft, setDraft] = useState<AssignmentMap>({});
  const [dirty, setDirty] = useState(false);
  const [activeIdx, setActiveIdx] = useState<number | null>(null);
  const [menu, setMenu] = useState<{ idx: number; x: number; y: number } | null>(null);
  const [applying, setApplying] = useState(false);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
  );

  // playable tracks only, drums/empty meta tracks with no notes are noise
  const playable = useMemo(
    () => tracks.filter((t) => t.note_count > 0),
    [tracks],
  );
  const byIndex = useMemo(() => {
    const m = new Map<number, Track>();
    for (const t of playable) m.set(t.index, t);
    return m;
  }, [playable]);

  // resync the draft from the server whenever the user isn't mid-edit
  const serverDraft = useMemo(
    () => buildDraft(members, hostName, serverHostTracks, serverAssignments),
    [members, hostName, serverHostTracks, serverAssignments],
  );
  const serverKey = JSON.stringify(serverDraft);
  const lastServerKey = useRef("");
  useEffect(() => {
    if (!dirty && serverKey !== lastServerKey.current) {
      lastServerKey.current = serverKey;
      setDraft(serverDraft);
    }
  }, [serverKey, serverDraft, dirty]);

  // a loaded preset overrides whatever is on the board, even unsaved edits
  useEffect(() => {
    if (!resyncToken) return;
    setDirty(false);
    lastServerKey.current = "";
  }, [resyncToken]);

  // wipe the board when the song changes so old picks dont map onto the
  // new song's tracks (the server already clears its side on load)
  const songRef = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    const song = currentSong ?? null;
    if (songRef.current === undefined) {
      songRef.current = song;
      return;
    }
    if (song !== songRef.current) {
      songRef.current = song;
      setDirty(false);
      lastServerKey.current = "";
      setDraft(() => {
        const empty: AssignmentMap = {};
        for (const m of members) empty[m] = [];
        return empty;
      });
    }
  }, [currentSong, members]);

  const assignedSet = useMemo(() => {
    const s = new Set<number>();
    for (const m of members) for (const i of draft[m] || []) s.add(i);
    return s;
  }, [draft, members]);

  const poolTracks = playable.filter((t) => !assignedSet.has(t.index));

  function moveTo(trackIndex: number, lane: string) {
    setDraft((prev) => {
      const next: AssignmentMap = {};
      for (const m of members) next[m] = (prev[m] || []).filter((i) => i !== trackIndex);
      if (lane !== POOL) next[lane] = [...(next[lane] || []), trackIndex];
      return next;
    });
    setDirty(true);
  }

  function onDragStart(e: DragStartEvent) {
    const id = String(e.active.id);
    if (id.startsWith("track:")) setActiveIdx(Number(id.slice(6)));
  }
  function onDragEnd(e: DragEndEvent) {
    setActiveIdx(null);
    const overId = e.over ? String(e.over.id) : null;
    const actId = String(e.active.id);
    if (!overId || !actId.startsWith("track:")) return;
    const trackIndex = Number(actId.slice(6));
    const lane = overId.startsWith("lane:") ? overId.slice(5) : null;
    if (lane === null) return;
    moveTo(trackIndex, lane);
  }

  async function apply() {
    setApplying(true);
    try {
      const hostTracks = draft[hostName] || [];
      const clients: AssignmentMap = {};
      for (const m of members) {
        if (m === hostName) continue;
        clients[m] = draft[m] || [];
      }
      await api.assign(hostTracks, clients);
      setDirty(false);
      toast.push("assignments applied", "ok");
      onApplied();
    } catch (e: any) {
      toast.push(`assign failed: ${e.message}`, "err");
    } finally {
      setApplying(false);
    }
  }

  function reset() {
    setDraft(serverDraft);
    setDirty(false);
  }

  function autoFill() {
    // round-robin the pool across every member, host included
    const pool = poolTracks.map((t) => t.index);
    if (!pool.length) return;
    setDraft((prev) => {
      const next: AssignmentMap = {};
      for (const m of members) next[m] = [...(prev[m] || [])];
      pool.forEach((idx, i) => {
        const who = members[i % members.length];
        next[who] = [...next[who], idx];
      });
      return next;
    });
    setDirty(true);
  }

  function clearAll() {
    setDraft(() => {
      const next: AssignmentMap = {};
      for (const m of members) next[m] = [];
      return next;
    });
    setDirty(true);
  }

  async function onTrackInstrument(index: number, program: number | null) {
    try {
      await api.setTrackInstrument(index, program);
      onApplied();
    } catch (e: any) {
      toast.push(`instrument: ${e.message}`, "err");
    }
  }

  const activeTrack = activeIdx != null ? byIndex.get(activeIdx) : null;
  const activeOverride =
    activeIdx != null ? trackPrograms?.[String(activeIdx)] : undefined;
  const activeOverLabel =
    typeof activeOverride === "number" ? GM_PROGRAMS[activeOverride] : null;
  const activeFam = familyFor(activeOverLabel || activeTrack?.display_label);
  const menuTrack = menu ? byIndex.get(menu.idx) : null;
  const menuOverride =
    menu && typeof trackPrograms?.[String(menu.idx)] === "number"
      ? String(trackPrograms[String(menu.idx)])
      : "";

  return (
    <section className="board panel">
      <div className="panel__head">
        <h2 className="panel__title">Assign</h2>
        <div className="board__actions">
          {dirty && <span className="board__dirty">unsaved</span>}
          <button className="btn btn--ghost" onClick={autoFill} disabled={disabled || !poolTracks.length}>
            Spread pool
          </button>
          <button className="btn btn--ghost" onClick={clearAll} disabled={disabled}>
            Clear
          </button>
          <button className="btn btn--ghost" onClick={reset} disabled={disabled || !dirty}>
            Reset
          </button>
          <button className="btn btn--accent" onClick={apply} disabled={disabled || !dirty || applying}>
            {applying ? "Applying..." : "Apply"}
          </button>
        </div>
      </div>

      {!playable.length ? (
        <div className="board__empty">load a song to see its tracks</div>
      ) : (
        <DndContext
          sensors={sensors}
          collisionDetection={closestCenter}
          onDragStart={onDragStart}
          onDragEnd={onDragEnd}
        >
          <div className="board__grid">
            <Lane
              id={POOL}
              title="Unassigned"
              subtitle={"drag a track onto a bandmate, or use its menu button"}
              tracks={poolTracks}
              inPool
              onMenu={(e, idx) => setMenu({ idx, x: e.clientX, y: e.clientY })}
              trackPrograms={trackPrograms}
            />
            {members.map((m) => (
              <Lane
                key={m}
                id={m}
                title={m}
                badge={m === hostName ? "HOST" : "MATE"}
                tracks={(draft[m] || []).map((i) => byIndex.get(i)).filter(Boolean) as Track[]}
                inPool={false}
                onMenu={(e, idx) => setMenu({ idx, x: e.clientX, y: e.clientY })}
                trackPrograms={trackPrograms}
              />
            ))}
          </div>

          <DragOverlay dropAnimation={null}>
            {activeTrack ? (
              <div
                className="chip chip--overlay"
                style={{ ["--fam" as any]: activeFam.color }}
              >
                <span className="chip__glyph" aria-hidden>
                  <FontAwesomeIcon icon={activeFam.icon} />
                </span>
                <span className="chip__label">
                  {activeOverLabel || activeTrack.display_label || activeTrack.name}
                </span>
              </div>
            ) : null}
          </DragOverlay>
        </DndContext>
      )}

      {menu && (
        <>
          <div className="menu-scrim" onClick={() => setMenu(null)} />
          <div
            className="menu"
            style={{ left: Math.min(menu.x, window.innerWidth - 248), top: Math.min(menu.y + 6, window.innerHeight - 440) }}
          >
            {menuTrack && (
              <div className="menu__track">
                <span className="menu__track-name" title={menuTrack.display_label || menuTrack.name}>
                  {menuTrack.display_label || menuTrack.instrument || menuTrack.name || `track ${menu.idx}`}
                </span>
                <span className="menu__track-notes">{menuTrack.note_count} notes</span>
              </div>
            )}
            <div className="menu__inst">
              <span className="menu__inst-label">plays as</span>
              <select
                className="menu__select"
                value={menuOverride}
                disabled={disabled}
                onChange={(e) => {
                  const v = e.target.value;
                  onTrackInstrument(menu.idx, v === "" ? null : Number(v));
                }}
              >
                <option value="">Default (from MIDI)</option>
                {GM_GROUPS.map((g, gi) => {
                  const end = GM_GROUPS[gi + 1]?.start ?? GM_PROGRAMS.length;
                  return (
                    <optgroup key={g.label} label={g.label}>
                      {GM_PROGRAMS.slice(g.start, end).map((nm, j) => (
                        <option key={g.start + j} value={g.start + j}>
                          {nm}
                        </option>
                      ))}
                    </optgroup>
                  );
                })}
              </select>
              <span className="menu__inst-hint">applies on next play</span>
            </div>
            <div className="menu__head">assign track to</div>
            {members.map((m) => (
              <button
                key={m}
                className="menu__item"
                onClick={() => {
                  moveTo(menu.idx, m);
                  setMenu(null);
                }}
              >
                {m === hostName ? `${m} (host)` : m}
              </button>
            ))}
            <button
              className="menu__item menu__item--danger"
              onClick={() => {
                moveTo(menu.idx, POOL);
                setMenu(null);
              }}
            >
              Unassign
            </button>
          </div>
        </>
      )}
    </section>
  );
}
