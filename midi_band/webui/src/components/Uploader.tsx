import { useRef, useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faUpload, faRotate } from "@fortawesome/free-solid-svg-icons";
import { api } from "../api";
import { fmtSize } from "../hooks";
import { useToast } from "./Toasts";

export function Uploader({ onUploaded }: { onUploaded: () => void }) {
  const toast = useToast();
  const inputRef = useRef<HTMLInputElement>(null);
  const [hover, setHover] = useState(false);
  const [busy, setBusy] = useState(false);

  async function send(files: FileList | null) {
    if (!files || !files.length) return;
    setBusy(true);
    let ok = 0;
    for (const f of Array.from(files)) {
      try {
        const r = await api.upload(f);
        ok++;
        toast.push(`${r.name} (${fmtSize(r.size)})`, "ok");
      } catch (e: any) {
        toast.push(`${f.name}: ${e.message}`, "err");
      }
    }
    setBusy(false);
    if (ok) onUploaded();
  }

  return (
    <label
      className={`drop${hover ? " drop--hover" : ""}${busy ? " drop--busy" : ""}`}
      onDragEnter={(e) => {
        e.preventDefault();
        setHover(true);
      }}
      onDragOver={(e) => e.preventDefault()}
      onDragLeave={() => setHover(false)}
      onDrop={(e) => {
        e.preventDefault();
        setHover(false);
        send(e.dataTransfer.files);
      }}
    >
      <input
        ref={inputRef}
        type="file"
        accept=".mid,.midi"
        multiple
        hidden
        onChange={(e) => {
          send(e.target.files);
          if (inputRef.current) inputRef.current.value = "";
        }}
      />
      <span className="drop__icon" aria-hidden>
        <FontAwesomeIcon icon={busy ? faRotate : faUpload} spin={busy} />
      </span>
      <span className="drop__text">
        {busy ? "uploading..." : "drop .mid files here"}
      </span>
      <span className="drop__hint">or click to browse</span>
    </label>
  );
}
