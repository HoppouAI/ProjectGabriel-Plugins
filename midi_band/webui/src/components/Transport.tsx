import { useEffect, useRef, useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  faCompactDisc,
  faPlay,
  faPause,
  faStop,
  faCheck,
  faVolumeHigh,
  faVolumeLow,
  faVolumeXmark,
  faTowerBroadcast,
} from "@fortawesome/free-solid-svg-icons";
import type { Status } from "../types";
import { api } from "../api";
import { fmtTime } from "../hooks";
import { useToast } from "./Toasts";
import { familyFor } from "../instruments";

interface Props {
  status: Status | null;
  isHost: boolean;
  audioMode?: boolean;
  onAction: () => void;
}

export function Transport({ status, isHost, audioMode, onAction }: Props) {
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const [vol, setVol] = useState(0.5);
  const lastVolEdit = useRef(0);
  const volTimer = useRef<number | null>(null);

  // follow the server gain unless the user touched the slider recently
  useEffect(() => {
    if (Date.now() - lastVolEdit.current > 1500 && typeof status?.gain === "number") {
      setVol(status.gain);
    }
  }, [status?.gain]);

  // keep-warm sync tone: a soft hum every member plays so VRChat's voice
  // gate stays open and the band stops drifting between phrases.
  const toneOn = !!status?.tone_on;
  const [toneVol, setToneVol] = useState(0.15);
  const lastToneEdit = useRef(0);
  const toneTimer = useRef<number | null>(null);

  useEffect(() => {
    if (Date.now() - lastToneEdit.current > 1500 && typeof status?.tone_gain === "number") {
      setToneVol(status.tone_gain);
    }
  }, [status?.tone_gain]);

  const playing = !!status?.playing;
  const paused = !!status?.paused;
  const countIn = !!status?.in_count_in;
  const song = status?.song || null;
  // friendly name (preset or display name) for the now-playing line
  const songLabel = status?.song_label || song;
  const dur = Number(status?.duration || 0);
  const pos = Number(status?.position || 0);
  const pct = dur > 0 ? Math.min(100, (pos / dur) * 100) : 0;

  let state = "Idle";
  let stateClass = "idle";
  if (countIn) {
    const n = Math.max(1, Math.ceil(Number(status?.count_in_remaining || 0)));
    state = `Count-in ${n}`;
    stateClass = "countin";
  } else if (playing) {
    state = "Playing";
    stateClass = "playing";
  } else if (paused) {
    state = "Paused";
    stateClass = "paused";
  } else if (song) {
    state = "Loaded";
    stateClass = "loaded";
  }

  async function run(label: string, fn: () => Promise<unknown>) {
    setBusy(label);
    try {
      await fn();
      onAction();
    } catch (e: any) {
      toast.push(`${label}: ${e.message}`, "err");
    } finally {
      setBusy(null);
    }
  }

  // one button covers the whole play lifecycle: start when idle, pause
  // while sounding, and resume from where it paused (not a restart).
  const active = playing || countIn;
  function onToggle() {
    if (active) return run("pause", api.pause);
    if (paused) return run("resume", api.resume);
    return run("play", api.play);
  }
  const toggleTitle = active ? "Pause" : paused ? "Resume" : "Play";

  function onVol(v: number) {
    setVol(v);
    lastVolEdit.current = Date.now();
    if (volTimer.current) window.clearTimeout(volTimer.current);
    volTimer.current = window.setTimeout(async () => {
      try {
        await api.setVolume(v);
      } catch (e: any) {
        toast.push(`volume: ${e.message}`, "err");
      }
    }, 180);
  }

  async function onToneToggle() {
    try {
      await api.setTone(!toneOn, toneVol);
      onAction();
    } catch (e: any) {
      toast.push(`sync tone: ${e.message}`, "err");
    }
  }

  function onToneVol(v: number) {
    setToneVol(v);
    lastToneEdit.current = Date.now();
    if (toneTimer.current) window.clearTimeout(toneTimer.current);
    toneTimer.current = window.setTimeout(async () => {
      try {
        await api.setTone(toneOn, v);
      } catch (e: any) {
        toast.push(`sync tone: ${e.message}`, "err");
      }
    }, 180);
  }

  const fam = familyFor(song || "");
  const memberCount = status?.members?.length || 0;
  const matesLabel = memberCount ? status?.members?.join(", ") : "nobody on stage yet";
  const volPct = Math.min(100, (vol / 2) * 100);
  const tonePct = Math.min(100, toneVol * 100);
  const volIcon = vol <= 0.001 ? faVolumeXmark : vol < 0.85 ? faVolumeLow : faVolumeHigh;

  return (
    <section className="controlbar">
      <div className="cb__row">
        <div className="cb-now">
          <div
            className="cb-now__disc"
            style={{ ["--fam" as any]: fam.color }}
            data-spin={playing}
          >
            <FontAwesomeIcon icon={faCompactDisc} />
          </div>
          <div className="cb-now__meta">
            <div className="cb-now__song" title={song || ""}>
              {songLabel || "no song loaded"}
            </div>
            <div className="cb-now__line">
              <span className={`pill pill--${stateClass}`}>{state}</span>
              <span className="cb-now__time">
                {fmtTime(pos)} / {fmtTime(dur)}
              </span>
              <span className="cb-now__mates" title={matesLabel}>
                {memberCount} on stage
              </span>
            </div>
          </div>
        </div>

        <div className="cb-actions">
          <div className="cb-transport">
            <button
              className="tbtn tbtn--play"
              disabled={!isHost || !!busy || (!song && !paused)}
              onClick={onToggle}
              title={toggleTitle}
            >
              <FontAwesomeIcon icon={active ? faPause : faPlay} />
            </button>
            <button
              className="tbtn tbtn--stop"
              disabled={!isHost || !!busy || (!playing && !paused && !countIn)}
              onClick={() => run("stop", api.stop)}
              title="Stop"
            >
              <FontAwesomeIcon icon={faStop} />
            </button>
            {!audioMode && (
              <button
                className="tbtn tbtn--check"
                disabled={!isHost || !!busy}
                onClick={() => run("soundcheck", () => api.soundcheck(8, 120))}
                title="Soundcheck (sync + warmup)"
              >
                <FontAwesomeIcon icon={faCheck} />
              </button>
            )}
          </div>

          <div className="cb-vol" title="Band volume">
            <span className="cb-vol__icon" aria-hidden>
              <FontAwesomeIcon icon={volIcon} />
            </span>
            <input
              className="slider"
              type="range"
              min={0}
              max={2}
              step={0.05}
              value={vol}
              disabled={!isHost}
              style={{ ["--pct" as any]: `${volPct}%` }}
              onChange={(e) => onVol(parseFloat(e.target.value))}
            />
            <span className="cb-vol__val">{vol.toFixed(2)}</span>
          </div>

          <div className={`cb-tone${toneOn ? " is-on" : ""}`}>
            <button
              className={`cb-tone__btn${toneOn ? " is-on" : ""}`}
              disabled={!isHost}
              onClick={onToneToggle}
              title="Keep-warm sync tone: a soft hum every member plays so VRChat's voice gate stays open and the band stops drifting"
            >
              <FontAwesomeIcon icon={faTowerBroadcast} />
              <span className="cb-tone__label">sync tone</span>
            </button>
            {toneOn && (
              <div className="cb-tone__gain">
                <input
                  className="slider slider--tone"
                  type="range"
                  min={0}
                  max={1}
                  step={0.01}
                  value={toneVol}
                  disabled={!isHost}
                  style={{ ["--pct" as any]: `${tonePct}%` }}
                  onChange={(e) => onToneVol(parseFloat(e.target.value))}
                  title="Sync tone volume"
                />
                <span className="cb-tone__val">{Math.round(toneVol * 100)}%</span>
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="cb-seek" title={`${fmtTime(pos)} / ${fmtTime(dur)}`}>
        <div className="cb-seek__fill" style={{ width: `${pct}%` }} />
      </div>
    </section>
  );
}
