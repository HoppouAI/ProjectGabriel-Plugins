import type { SyncMember, SyncStatus } from "../types";

function quality(m: SyncMember): "host" | "tight" | "ok" | "loose" | "stale" {
  if (m.is_host) return "host";
  if (m.age_seconds != null && m.age_seconds > 10) return "stale";
  const j = Math.abs(m.jitter_ms);
  if (j < 5 && m.rtt_ms < 30) return "tight";
  if (j < 15 && m.rtt_ms < 80) return "ok";
  return "loose";
}

export function SyncPanel({ sync }: { sync: SyncStatus | null }) {
  const members = sync?.members || [];
  const isHostView = sync?.role === "host";
  const remotes = members.filter((m) => !m.is_host);
  const tight = remotes.filter((m) => quality(m) === "tight").length;

  return (
    <section className="syncp panel">
      <div className="panel__head">
        <h2 className="panel__title">Sync</h2>
        <span className="syncp__summary">
          {!isHostView
            ? "host only"
            : `${remotes.length} mate${remotes.length === 1 ? "" : "s"} \u00B7 ${tight} tight \u00B7 lead ${(sync?.lead_seconds || 0).toFixed(2)}s`}
        </span>
      </div>

      {!isHostView ? (
        <div className="syncp__empty">{sync?.message || "client mode, no sync stats"}</div>
      ) : members.length === 0 ? (
        <div className="syncp__empty">no bandmates connected</div>
      ) : (
        <ul className="syncp__list">
          {members.map((m) => {
            const q = quality(m);
            const age = m.is_host
              ? "you"
              : m.age_seconds == null
                ? "no data"
                : `${m.age_seconds.toFixed(1)}s ago`;
            return (
              <li key={m.name} className={`smember smember--${q}`}>
                <div className="smember__top">
                  <span className="smember__name">
                    {m.name}
                    {m.is_host && <span className="smember__tag">host</span>}
                  </span>
                  <span className={`spill spill--${q}`}>{q}</span>
                </div>
                <div className="smember__stats">
                  {m.is_host ? (
                    <span className="smember__dim">host clock</span>
                  ) : (
                    <>
                      <span>
                        <b>jit</b> {m.jitter_ms.toFixed(1)}ms
                      </span>
                      <span>
                        <b>rtt</b> {m.rtt_ms.toFixed(1)}ms
                      </span>
                    </>
                  )}
                  <span className="smember__dim">{age}</span>
                </div>
                <div className="smember__tracks">
                  {m.tracks.length ? (
                    m.tracks.map((t, i) => (
                      <span key={i} className="smember__instr">
                        {t}
                      </span>
                    ))
                  ) : (
                    <span className="smember__instr smember__instr--none">no tracks</span>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
