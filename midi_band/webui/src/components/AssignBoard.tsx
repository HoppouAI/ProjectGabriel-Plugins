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
import { faEllipsis, faRightLeft, faCheck, faUsers } from "@fortawesome/free-solid-svg-icons";
import type { Track, AssignmentMap, SfPreset, TrackProgram, ParseMode } from "../types";
import { familyFor, presetName, buildInstrumentOptions } from "../instruments";
import { api } from "../api";
import { useToast } from "./Toasts";
import { InstrumentPicker } from "./InstrumentPicker";

const POOL = "__pool__";

// normalize a track override to {bank, program}. legacy hosts send a bare
// GM program number, newer ones send the object.
function normProg(
  v: TrackProgram | number | undefined | null,
): TrackProgram | null {
  if (v == null) return null;
  if (typeof v === "number") return v >= 0 ? { bank: 0, program: v } : null;
  if (typeof v === "object" && typeof v.program === "number") {
    return { bank: typeof v.bank === "number" ? v.bank : 0, program: v.program };
  }
  return null;
}

interface Props {
  tracks: Track[];
  members: string[]; // [host, ...clients]
  hostName: string;
  serverHostTracks: number[];
  serverAssignments: AssignmentMap;
  trackPrograms?: Record<string, TrackProgram | number>;
  soundfont?: SfPreset[];
  currentSong?: string | null;
  audioMode?: boolean;
  parseMode?: ParseMode;
  resolvedParseMode?: "track" | "channel";
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
  laneId,
  inPool,
  onMenu,
  overrideLabel,
  shareCount,
}: {
  track: Track;
  laneId: string;
  inPool: boolean;
  onMenu: (e: React.MouseEvent, idx: number) => void;
  overrideLabel?: string | null;
  shareCount?: number;
}) {
  const overLabel = overrideLabel || null;
  const baseLabel = track.display_label || track.instrument || track.name || `track ${track.index}`;
  const fam = familyFor(overLabel || track.display_label || track.instrument);
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({
    id: `chip:${laneId}::${track.index}`,
  });
  const label = overLabel || baseLabel;
  const shared = (shareCount || 0) > 1;
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
      {shared && (
        <span className="chip__dup" title={`playing on ${shareCount} bandmates`}>
          <FontAwesomeIcon icon={faUsers} />
          {shareCount}
        </span>
      )}
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
  soundfont,
  shareCounts,
}: {
  id: string;
  title: string;
  subtitle?: string;
  badge?: string;
  tracks: Track[];
  inPool: boolean;
  onMenu: (e: React.MouseEvent, idx: number) => void;
  trackPrograms?: Record<string, TrackProgram | number>;
  soundfont?: SfPreset[];
  shareCounts?: Map<number, number>;
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
          tracks.map((t) => {
            const ov = normProg(trackPrograms?.[String(t.index)]);
            const ovLabel = ov ? presetName(soundfont, ov.bank, ov.program) : null;
            return (
              <Chip
                key={t.index}
                track={t}
                laneId={id}
                inPool={inPool}
                onMenu={onMenu}
                overrideLabel={ovLabel}
                shareCount={inPool ? 0 : shareCounts?.get(t.index)}
              />
            );
          })
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
  soundfont,
  currentSong,
  audioMode,
  parseMode,
  resolvedParseMode,
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
  const [reparsing, setReparsing] = useState(false);

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

  // how many members each track is on, so a doubled part can flag itself
  const shareCounts = useMemo(() => {
    const m = new Map<number, number>();
    for (const mem of members) for (const i of draft[mem] || []) m.set(i, (m.get(i) || 0) + 1);
    return m;
  }, [draft, members]);

  const poolTracks = playable.filter((t) => !assignedSet.has(t.index));

  function cloneDraft(prev: AssignmentMap): AssignmentMap {
    const next: AssignmentMap = {};
    for (const m of members) next[m] = [...(prev[m] || [])];
    return next;
  }

  function addTo(trackIndex: number, member: string) {
    if (member === POOL) return;
    setDraft((prev) => {
      const next = cloneDraft(prev);
      if (!next[member]) next[member] = [];
      if (!next[member].includes(trackIndex)) next[member] = [...next[member], trackIndex];
      return next;
    });
    setDirty(true);
  }

  function removeFrom(trackIndex: number, member: string) {
    setDraft((prev) => {
      const next = cloneDraft(prev);
      next[member] = (next[member] || []).filter((i) => i !== trackIndex);
      return next;
    });
    setDirty(true);
  }

  function moveBetween(trackIndex: number, from: string, to: string) {
    setDraft((prev) => {
      const next = cloneDraft(prev);
      next[from] = (next[from] || []).filter((i) => i !== trackIndex);
      if (!next[to]) next[to] = [];
      if (!next[to].includes(trackIndex)) next[to] = [...next[to], trackIndex];
      return next;
    });
    setDirty(true);
  }

  // toggle one member on/off for a track, the whole point of multi-assign
  function toggleMember(trackIndex: number, member: string) {
    const on = (draft[member] || []).includes(trackIndex);
    if (on) removeFrom(trackIndex, member);
    else addTo(trackIndex, member);
  }

  function assignEveryone(trackIndex: number) {
    setDraft((prev) => {
      const next = cloneDraft(prev);
      for (const m of members) if (!next[m].includes(trackIndex)) next[m] = [...next[m], trackIndex];
      return next;
    });
    setDirty(true);
  }

  function unassignAll(trackIndex: number) {
    setDraft((prev) => {
      const next = cloneDraft(prev);
      for (const m of members) next[m] = next[m].filter((i) => i !== trackIndex);
      return next;
    });
    setDirty(true);
  }

  function onDragStart(e: DragStartEvent) {
    const id = String(e.active.id);
    if (id.startsWith("chip:")) {
      const rest = id.slice(5);
      const sep = rest.indexOf("::");
      if (sep >= 0) setActiveIdx(Number(rest.slice(sep + 2)));
    }
  }
  function onDragEnd(e: DragEndEvent) {
    setActiveIdx(null);
    const overId = e.over ? String(e.over.id) : null;
    const actId = String(e.active.id);
    if (!overId || !actId.startsWith("chip:")) return;
    const rest = actId.slice(5);
    const sep = rest.indexOf("::");
    if (sep < 0) return;
    const srcLane = rest.slice(0, sep);
    const trackIndex = Number(rest.slice(sep + 2));
    const dstLane = overId.startsWith("lane:") ? overId.slice(5) : null;
    if (dstLane === null || dstLane === srcLane) return;
    // scoped to the chip you grabbed: a doubled track keeps its other copies
    if (srcLane === POOL) addTo(trackIndex, dstLane);
    else if (dstLane === POOL) removeFrom(trackIndex, srcLane);
    else moveBetween(trackIndex, srcLane, dstLane);
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

  // re-read the loaded midi by track vs by channel. resets assignments on
  // the host, so the board resyncs from the server after.
  async function changeParseMode(mode: ParseMode) {
    if (mode === (parseMode || "auto")) return;
    setReparsing(true);
    try {
      await api.setParseMode(mode);
      setDirty(false);
      toast.push(`reading midi by ${mode}`, "ok");
      onApplied();
    } catch (e: any) {
      toast.push(`reparse failed: ${e.message}`, "err");
    } finally {
      setReparsing(false);
    }
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

  async function onTrackInstrument(index: number, program: number | null, bank = 0) {
    try {
      await api.setTrackInstrument(index, program, bank);
      onApplied();
    } catch (e: any) {
      toast.push(`instrument: ${e.message}`, "err");
    }
  }

  const activeTrack = activeIdx != null ? byIndex.get(activeIdx) : null;
  const activeOv =
    activeIdx != null ? normProg(trackPrograms?.[String(activeIdx)]) : null;
  const activeOverLabel = activeOv
    ? presetName(soundfont, activeOv.bank, activeOv.program)
    : null;
  const activeFam = familyFor(activeOverLabel || activeTrack?.display_label);
  const menuTrack = menu ? byIndex.get(menu.idx) : null;
  const menuOv = menu ? normProg(trackPrograms?.[String(menu.idx)]) : null;
  const menuOverride = menuOv ? `${menuOv.bank}:${menuOv.program}` : "";
  const menuIsDrum = !!menuTrack?.channels?.includes(9);
  const menuOptions = buildInstrumentOptions(soundfont, menuIsDrum);
  const menuShare = menu ? shareCounts.get(menu.idx) || 0 : 0;

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

      {!audioMode && !!currentSong && (
        <div className="parsemode">
          <span className="parsemode__label">Read midi as</span>
          {(["auto", "track", "channel"] as ParseMode[]).map((m) => (
            <button
              key={m}
              type="button"
              className={`parsemode__opt${(parseMode || "auto") === m ? " is-on" : ""}`}
              onClick={() => changeParseMode(m)}
              disabled={disabled || reparsing}
              title={
                m === "channel"
                  ? "split by MIDI channel, fixes files that read as all Drums"
                  : m === "track"
                    ? "split by MIDI track, the usual layout"
                    : "pick automatically per file"
              }
            >
              {m === "auto"
                ? `Auto${(parseMode || "auto") === "auto" && resolvedParseMode ? ` · ${resolvedParseMode}` : ""}`
                : m === "track"
                  ? "By track"
                  : "By channel"}
            </button>
          ))}
        </div>
      )}

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
              subtitle={"drag a track onto a bandmate, or use its menu to play it on several at once"}
              tracks={poolTracks}
              inPool
              onMenu={(e, idx) => setMenu({ idx, x: e.clientX, y: e.clientY })}
              trackPrograms={trackPrograms}
              soundfont={soundfont}
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
                soundfont={soundfont}
                shareCounts={shareCounts}
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
                {!audioMode && (
                  <span className="menu__track-notes">{menuTrack.note_count} notes</span>
                )}
              </div>
            )}
            {!audioMode && (
              <div className="menu__inst">
                <span className="menu__inst-label">plays as</span>
                <InstrumentPicker
                  groups={menuOptions}
                  value={menuOverride}
                  valueLabel={
                    menuOv ? presetName(soundfont, menuOv.bank, menuOv.program) : ""
                  }
                  disabled={disabled}
                  emptyHint={
                    menuIsDrum && !menuOptions.length
                      ? "no soundfont kits available"
                      : "no matches"
                  }
                  onChange={(v) => {
                    if (!v) {
                      onTrackInstrument(menu.idx, null);
                    } else {
                      const [b, p] = v.split(":").map(Number);
                      onTrackInstrument(menu.idx, p, b);
                    }
                  }}
                />
                <span className="menu__inst-hint">
                  {menuIsDrum && !menuOptions.length
                    ? "no soundfont kits available"
                    : "applies on next play"}
                </span>
              </div>
            )}
            <div className="menu__head">
              plays on {menuShare === 0 ? "nobody yet" : `${menuShare} ${menuShare === 1 ? "bandmate" : "bandmates"}`}
            </div>
            <div className="menu__members">
              {members.map((m) => {
                const on = (draft[m] || []).includes(menu.idx);
                return (
                  <button
                    key={m}
                    className={`menu__item menu__item--toggle${on ? " is-on" : ""}`}
                    onClick={() => toggleMember(menu.idx, m)}
                  >
                    <span className="menu__check" aria-hidden>
                      {on && <FontAwesomeIcon icon={faCheck} />}
                    </span>
                    <span className="menu__member-name">{m === hostName ? `${m} (host)` : m}</span>
                  </button>
                );
              })}
            </div>
            {members.length > 1 && (
              <button
                className="menu__item menu__item--everyone"
                onClick={() => assignEveryone(menu.idx)}
              >
                <span className="menu__check" aria-hidden>
                  <FontAwesomeIcon icon={faUsers} />
                </span>
                <span className="menu__member-name">Play on everyone</span>
              </button>
            )}
            <button
              className="menu__item menu__item--danger"
              onClick={() => {
                unassignAll(menu.idx);
                setMenu(null);
              }}
            >
              Remove from all
            </button>
          </div>
        </>
      )}
    </section>
  );
}
