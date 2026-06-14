import { useEffect, useMemo, useRef, useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  faMagnifyingGlass,
  faChevronDown,
  faCheck,
  faXmark,
} from "@fortawesome/free-solid-svg-icons";
import type { InstGroup, InstOption } from "../instruments";
import { filterInstruments, instMatches } from "../instruments";

interface Props {
  groups: InstGroup[];
  value: string; // "bank:program" or "" for default
  valueLabel: string; // human label of the current pick
  disabled?: boolean;
  emptyHint?: string;
  onChange: (value: string | null) => void; // null clears back to default
}

const DEFAULT_OPT: InstOption = { value: "", label: "Default (from MIDI)" };

type Row =
  | { type: "group"; label: string }
  | { type: "opt"; opt: InstOption; i: number };

export function InstrumentPicker({
  groups,
  value,
  valueLabel,
  disabled,
  emptyHint,
  onChange,
}: Props) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [hi, setHi] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  const filtered = useMemo(() => filterInstruments(groups, q), [groups, q]);
  const showDefault = instMatches(DEFAULT_OPT.label, q);

  // flat list drives keyboard nav, rows drive rendering, both share indices
  const { rows, flat } = useMemo(() => {
    const flat: InstOption[] = [];
    const rows: Row[] = [];
    if (showDefault) {
      rows.push({ type: "opt", opt: DEFAULT_OPT, i: flat.length });
      flat.push(DEFAULT_OPT);
    }
    for (const g of filtered) {
      rows.push({ type: "group", label: g.label });
      for (const o of g.options) {
        rows.push({ type: "opt", opt: o, i: flat.length });
        flat.push(o);
      }
    }
    return { rows, flat };
  }, [filtered, showDefault]);

  useEffect(() => {
    if (!open) return;
    setQ("");
    setHi(0);
    const t = setTimeout(() => inputRef.current?.focus(), 0);
    panelRef.current?.scrollIntoView({ block: "nearest" });
    return () => clearTimeout(t);
  }, [open]);

  useEffect(() => setHi(0), [q]);

  useEffect(() => {
    if (!open) return;
    listRef.current
      ?.querySelector<HTMLElement>(`[data-i="${hi}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [hi, open]);

  function pick(v: string) {
    onChange(v ? v : null);
    setOpen(false);
  }

  function onKey(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHi((h) => Math.min(h + 1, flat.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHi((h) => Math.max(h - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const o = flat[hi];
      if (o) pick(o.value);
    } else if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
    }
  }

  return (
    <div className={`instpick${open ? " is-open" : ""}`}>
      <button
        type="button"
        className={`instpick__btn${value ? " is-set" : ""}`}
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="instpick__current">
          {value ? valueLabel : "Default (from MIDI)"}
        </span>
        <FontAwesomeIcon icon={faChevronDown} className="instpick__caret" />
      </button>

      {open && (
        <div className="instpick__panel" ref={panelRef}>
          <div className="instpick__search">
            <FontAwesomeIcon
              icon={faMagnifyingGlass}
              className="instpick__search-icon"
            />
            <input
              ref={inputRef}
              className="instpick__input"
              placeholder="search instruments..."
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={onKey}
            />
            {q && (
              <button
                type="button"
                className="instpick__clear"
                tabIndex={-1}
                onClick={() => {
                  setQ("");
                  inputRef.current?.focus();
                }}
              >
                <FontAwesomeIcon icon={faXmark} />
              </button>
            )}
          </div>

          <div className="instpick__list" ref={listRef}>
            {!flat.length ? (
              <div className="instpick__empty">{emptyHint || "no matches"}</div>
            ) : (
              rows.map((r) =>
                r.type === "group" ? (
                  <div className="instpick__group-label" key={`g:${r.label}`}>
                    {r.label}
                  </div>
                ) : (
                  <button
                    type="button"
                    key={r.opt.value || "__default"}
                    data-i={r.i}
                    className={
                      "instpick__opt" +
                      (value === r.opt.value ? " is-sel" : "") +
                      (hi === r.i ? " is-hi" : "") +
                      (r.opt.value === "" ? " instpick__opt--default" : "")
                    }
                    onMouseEnter={() => setHi(r.i)}
                    onClick={() => pick(r.opt.value)}
                  >
                    <span className="instpick__opt-label">{r.opt.label}</span>
                    {value === r.opt.value && (
                      <FontAwesomeIcon
                        icon={faCheck}
                        className="instpick__opt-check"
                      />
                    )}
                  </button>
                ),
              )
            )}
          </div>
        </div>
      )}
    </div>
  );
}
