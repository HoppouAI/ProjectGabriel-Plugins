import { useEffect, useRef, useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  faCompactDisc,
  faPlay,
  faPause,
  faStop,
  faCheck,
  faVolumeHigh,
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

  const playing = !!status?.playing;
  const paused = !!status?.paused;
  const countIn = !!status?.in_count_in;
  const song = status?.song || null;
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

  const fam = familyFor(song || "");

  return (
    <section className="transport panel">
      <div className="transport__now">
        <div className="transport__disc" style={{ ["--fam" as any]: fam.color }} data-spin={playing}>
          <FontAwesomeIcon icon={faCompactDisc} />
        </div>
        <div className="transport__meta">
          <div className="transport__song" title={song || ""}>
            {song || "no song loaded"}
          </div>
          <div className="transport__state">
            <span className={`pill pill--${stateClass}`}>{state}</span>
            <span className="transport__mates">
              {(status?.members?.length || 0)} on stage
              {status?.members?.length ? `: ${status.members.join(", ")}` : ""}
            </span>
          </div>
        </div>
      </div>

      <div className="transport__bar">
        <span className="transport__t">{fmtTime(pos)}</span>
        <div className="scrub">
          <div className="scrub__fill" style={{ width: `${pct}%` }} />
          <div className="scrub__head" style={{ left: `${pct}%` }} />
        </div>
        <span className="transport__t">{fmtTime(dur)}</span>
      </div>

      <div className="transport__controls">
        <div className="transport__buttons">
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
              <FontAwesomeIcon icon={faCheck} /> check
            </button>
          )}
        </div>

        <div className="transport__vol">
          <span className="transport__vol-icon" aria-hidden>
            <FontAwesomeIcon icon={faVolumeHigh} />
          </span>
          <input
            type="range"
            min={0}
            max={2}
            step={0.05}
            value={vol}
            disabled={!isHost}
            onChange={(e) => onVol(parseFloat(e.target.value))}
          />
          <span className="transport__vol-val">{vol.toFixed(2)}</span>
        </div>
      </div>
    </section>
  );
}
