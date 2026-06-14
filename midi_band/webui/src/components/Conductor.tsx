import { useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faWandMagicSparkles } from "@fortawesome/free-solid-svg-icons";
import { api } from "../api";
import { useToast } from "./Toasts";

interface Props {
  isHost: boolean;
  hasSong: boolean;
  onApplied: () => void;
}

// AI conductor: describe the vibe, the model assigns every track to a
// bandmate via function calling on the host. Applies straight to the board.
export function Conductor({ isHost, hasSong, onApplied }: Props) {
  const toast = useToast();
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);

  const ready = isHost && hasSong && !busy;

  async function conduct() {
    if (!prompt.trim() || !ready) return;
    setBusy(true);
    try {
      const res = await api.conduct(prompt.trim());
      if (res.result === "ok") {
        toast.push(res.reasoning ? `conductor: ${res.reasoning}` : "conductor set the arrangement", "ok");
        if (res.unassigned_tracks && res.unassigned_tracks.length) {
          toast.push(`left ${res.unassigned_tracks.length} track(s) out`, "ok");
        }
        onApplied();
      } else {
        toast.push(`conductor: ${res.message || "no arrangement"}`, "err");
      }
    } catch (e: any) {
      toast.push(`conductor: ${e.message}`, "err");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="conductor panel">
      <div className="conductor__head">
        <span className="conductor__icon" aria-hidden>
          <FontAwesomeIcon icon={faWandMagicSparkles} />
        </span>
        <div className="conductor__title">
          <h2 className="panel__title">AI conductor</h2>
          <span className="conductor__sub">describe the sound, it splits the parts</span>
        </div>
      </div>
      <textarea
        className="conductor__input"
        rows={2}
        placeholder={
          hasSong
            ? "e.g. stripped back and moody, keep me on piano, give the drums to one person"
            : "load a song first, then describe the arrangement you want"
        }
        value={prompt}
        disabled={!isHost || !hasSong || busy}
        onChange={(e) => setPrompt(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) conduct();
        }}
      />
      <div className="conductor__row">
        <span className="conductor__hint">
          {isHost ? "Ctrl+Enter to run" : "host only"}
        </span>
        <button className="btn btn--accent" onClick={conduct} disabled={!ready || !prompt.trim()}>
          {busy ? "Conducting..." : "Conduct"}
        </button>
      </div>
    </section>
  );
}
