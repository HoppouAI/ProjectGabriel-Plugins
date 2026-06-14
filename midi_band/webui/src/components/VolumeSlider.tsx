import { useEffect, useRef, useState } from "react";

interface Props {
  value: number;
  onCommit: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
  disabled?: boolean;
  className?: string;
  format?: (v: number) => string;
  title?: string;
}

// Small debounced range input that follows the server value unless the user
// touched it recently. Same idea as the master fader in Transport, factored
// out so the per-member and per-track mixers can reuse it.
export function VolumeSlider({
  value,
  onCommit,
  min = 0,
  max = 2,
  step = 0.05,
  disabled,
  className,
  format,
  title,
}: Props) {
  const [v, setV] = useState(value);
  const lastEdit = useRef(0);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    if (Date.now() - lastEdit.current > 1500) setV(value);
  }, [value]);

  function change(nv: number) {
    setV(nv);
    lastEdit.current = Date.now();
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => onCommit(nv), 180);
  }

  const label = format ? format(v) : v.toFixed(2);
  return (
    <div
      className={`vslider${disabled ? " vslider--off" : ""}${className ? " " + className : ""}`}
      title={title}
    >
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={v}
        disabled={disabled}
        onChange={(e) => change(parseFloat(e.target.value))}
        onClick={(e) => e.stopPropagation()}
        onPointerDown={(e) => e.stopPropagation()}
      />
      <span className="vslider__val">{label}</span>
    </div>
  );
}
