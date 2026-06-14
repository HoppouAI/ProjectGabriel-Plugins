import { useEffect, useRef, useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  faWandMagicSparkles,
  faPaperPlane,
  faXmark,
  faRotateRight,
} from "@fortawesome/free-solid-svg-icons";
import { api } from "../api";
import { useToast } from "./Toasts";
import type { ConductorMessage } from "../types";

interface Props {
  isHost: boolean;
  hasSong: boolean;
  onApplied: () => void;
}

const EXAMPLES = [
  "strip it back, just piano and a soft pad",
  "make it huge, everyone in, double the strings",
  "give the lead a warm analog synth",
  "put me on drums, hand the bass to someone",
];

// AI conductor: a centered chat window. Tell it what you want, it streams a
// reply and arranges the band / re-voices tracks live on the host through
// function calling. Multi-turn, so you can keep refining.
export function Conductor({ isHost, hasSong, onApplied }: Props) {
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<ConductorMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const canChat = isHost && hasSong;

  // keep the transcript pinned to the newest message
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, open]);

  // esc closes the window
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  // edit the trailing assistant message in place as the stream lands
  const patchLast = (fn: (m: ConductorMessage) => ConductorMessage) =>
    setMessages((list) => {
      if (!list.length) return list;
      const last = list[list.length - 1];
      if (last.role !== "assistant") return list;
      const copy = list.slice();
      copy[copy.length - 1] = fn(last);
      return copy;
    });

  async function send() {
    const text = input.trim();
    if (!text || busy || !canChat) return;
    setInput("");
    setMessages((list) => [
      ...list,
      { role: "user", text },
      { role: "assistant", text: "", tools: [] },
    ]);
    setBusy(true);
    try {
      await api.conductStream(text, (ev) => {
        if (ev.type === "text") {
          patchLast((m) => ({ ...m, text: m.text + ev.delta }));
        } else if (ev.type === "tool") {
          patchLast((m) => ({
            ...m,
            tools: [...(m.tools || []), { summary: ev.summary, ok: ev.ok }],
          }));
        } else if (ev.type === "applied") {
          onApplied();
        } else if (ev.type === "error") {
          patchLast((m) => ({ ...m, error: true, text: m.text || ev.message }));
          toast.push(`conductor: ${ev.message}`, "err");
        }
      });
    } catch (e: any) {
      const msg = e?.message ?? "stream failed";
      patchLast((m) => ({ ...m, error: true, text: m.text || msg }));
      toast.push(`conductor: ${msg}`, "err");
    } finally {
      // never leave an empty assistant bubble hanging
      patchLast((m) =>
        m.text || (m.tools && m.tools.length) || m.error ? m : { ...m, text: "(done)" },
      );
      setBusy(false);
    }
  }

  async function newChat() {
    if (busy) return;
    try {
      await api.conductorReset();
    } catch {
      /* best effort, a fresh send re-inits anyway */
    }
    setMessages([]);
    setInput("");
  }

  return (
    <>
      <button
        className="conductor-launch"
        onClick={() => setOpen(true)}
        disabled={!isHost}
        title={isHost ? "Open the AI conductor" : "host only"}
      >
        <span className="conductor-launch__icon" aria-hidden>
          <FontAwesomeIcon icon={faWandMagicSparkles} />
        </span>
        <span className="conductor-launch__text">
          <span className="conductor-launch__name">AI conductor</span>
          <span className="conductor-launch__sub">
            {isHost ? "arrange + re-voice" : "host only"}
          </span>
        </span>
      </button>

      {open && (
        <div className="cond-overlay" onMouseDown={() => !busy && setOpen(false)}>
          <div
            className="cond-modal"
            onMouseDown={(e) => e.stopPropagation()}
            role="dialog"
            aria-label="AI conductor"
          >
            <header className="cond-modal__head">
              <span className="cond-modal__icon" aria-hidden>
                <FontAwesomeIcon icon={faWandMagicSparkles} />
              </span>
              <div className="cond-modal__titles">
                <h2 className="cond-modal__title">AI conductor</h2>
                <span className="cond-modal__sub">
                  arrange the band and re-voice tracks, just ask
                </span>
              </div>
              <button
                className="cond-modal__act"
                onClick={newChat}
                disabled={busy || !messages.length}
                title="New chat"
              >
                <FontAwesomeIcon icon={faRotateRight} />
              </button>
              <button
                className="cond-modal__act"
                onClick={() => setOpen(false)}
                title="Close"
              >
                <FontAwesomeIcon icon={faXmark} />
              </button>
            </header>

            <div className="cond-modal__body" ref={scrollRef}>
              {messages.length === 0 && (
                <div className="cond-empty">
                  {canChat ? (
                    <>
                      <p className="cond-empty__lead">
                        Tell the conductor what you want to hear.
                      </p>
                      <div className="cond-empty__chips">
                        {EXAMPLES.map((ex) => (
                          <button
                            key={ex}
                            className="cond-eg"
                            onClick={() => setInput(ex)}
                          >
                            {ex}
                          </button>
                        ))}
                      </div>
                    </>
                  ) : (
                    <p className="cond-empty__lead">
                      {isHost
                        ? "Load a song first, then ask for an arrangement."
                        : "The conductor runs on the host."}
                    </p>
                  )}
                </div>
              )}

              {messages.map((m, i) => (
                <div
                  key={i}
                  className={`cond-msg cond-msg--${m.role}${m.error ? " is-err" : ""}`}
                >
                  {(m.text ||
                    (m.role === "assistant" &&
                      busy &&
                      i === messages.length - 1)) && (
                    <div className="cond-msg__bubble">
                      {m.text ? (
                        m.text
                      ) : (
                        <span className="cond-typing">
                          <i />
                          <i />
                          <i />
                        </span>
                      )}
                    </div>
                  )}
                  {m.tools && m.tools.length > 0 && (
                    <div className="cond-tools">
                      {m.tools.map((t, ti) => (
                        <span
                          key={ti}
                          className={`cond-chip${t.ok ? "" : " is-bad"}`}
                        >
                          {t.summary}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>

            <div className="cond-modal__foot">
              <textarea
                className="cond-input"
                rows={1}
                placeholder={
                  canChat
                    ? "Ask for an arrangement or a sound change..."
                    : "host loads a song first"
                }
                value={input}
                disabled={!canChat || busy}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    send();
                  }
                }}
              />
              <button
                className="cond-send"
                onClick={send}
                disabled={!canChat || busy || !input.trim()}
                title="Send"
              >
                <FontAwesomeIcon icon={faPaperPlane} />
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
