"""midi_band standalone launcher GUI.

Spawn and manage multiple standalone_client.py instances from a single
window. Each row is one bandmate, with its own name, audio output
device, soundfont and gain. Start/stop them independently. The whole
roster persists to bandmates.yml next to this file.

Run:
    uv run launcher_gui.py
or:
    python launcher_gui.py

Requires only stdlib (tkinter ships with Python on Windows). If the
'sounddevice' package is installed it gets used for a real device
dropdown, otherwise the device field is free text and you type the
name yourself.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional

try:
    import yaml
except Exception:
    yaml = None  # type: ignore

try:
    import sounddevice as _sd
except Exception:
    _sd = None

HERE = Path(__file__).resolve().parent
ROSTER_PATH = HERE / "bandmates.yml"
CLIENT_PATH = HERE / "standalone_client.py"

DEFAULT_DRIVER = "dsound" if sys.platform.startswith("win") else ""

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("launcher")


def list_output_devices() -> List[str]:
    """Best-effort enumeration of system audio output device names. Returns
    [] if sounddevice isn't installed; the GUI then falls back to a
    plain text entry."""
    if _sd is None:
        return []
    try:
        devs = _sd.query_devices()
        out = []
        for d in devs:
            if d.get("max_output_channels", 0) > 0:
                name = d.get("name") or ""
                if name and name not in out:
                    out.append(name)
        return out
    except Exception as e:
        log.warning(f"could not enumerate audio devices: {e}")
        return []


def load_roster() -> dict:
    if not ROSTER_PATH.exists() or yaml is None:
        return {"shared": {}, "bandmates": []}
    try:
        with open(ROSTER_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            return {"shared": {}, "bandmates": []}
        data.setdefault("shared", {})
        data.setdefault("bandmates", [])
        return data
    except Exception as e:
        log.warning(f"could not read {ROSTER_PATH}: {e}")
        return {"shared": {}, "bandmates": []}


def save_roster(data: dict):
    if yaml is None:
        messagebox.showerror("missing dep", "PyYAML is not installed, can't save roster. pip install pyyaml")
        return
    try:
        with open(ROSTER_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    except Exception as e:
        messagebox.showerror("save failed", f"could not write {ROSTER_PATH}: {e}")


class BandmateRow:
    """One row in the launcher table representing a single client process."""

    def __init__(self, app: "LauncherApp", parent: tk.Widget, idx: int, data: dict, devices: List[str]):
        self.app = app
        self.idx = idx
        self.devices = devices
        self.proc: Optional[subprocess.Popen] = None
        self._monitor_thread: Optional[threading.Thread] = None

        self.frame = ttk.Frame(parent, padding=(6, 4))
        self.frame.grid(row=idx + 1, column=0, sticky="ew", pady=2)
        for col in range(7):
            self.frame.columnconfigure(col, weight=1 if col in (0, 1, 2) else 0)

        # Name
        self.name_var = tk.StringVar(value=str(data.get("name") or f"bandmate_{idx + 1}"))
        ttk.Entry(self.frame, textvariable=self.name_var, width=14).grid(row=0, column=0, padx=2, sticky="ew")

        # Audio device: combobox if we enumerated, else plain entry
        self.device_var = tk.StringVar(value=str(data.get("device") or ""))
        if devices:
            ttk.Combobox(self.frame, textvariable=self.device_var, values=devices, width=38).grid(
                row=0, column=1, padx=2, sticky="ew"
            )
        else:
            ttk.Entry(self.frame, textvariable=self.device_var, width=38).grid(row=0, column=1, padx=2, sticky="ew")

        # Soundfont path + browse
        self.sf_var = tk.StringVar(value=str(data.get("soundfont") or ""))
        ttk.Entry(self.frame, textvariable=self.sf_var, width=24).grid(row=0, column=2, padx=2, sticky="ew")
        ttk.Button(self.frame, text="...", width=3, command=self._browse_sf).grid(row=0, column=3, padx=1)

        # Gain
        self.gain_var = tk.DoubleVar(value=float(data.get("gain", 0.5)))
        ttk.Spinbox(self.frame, from_=0.0, to=2.0, increment=0.05, textvariable=self.gain_var, width=5).grid(
            row=0, column=4, padx=2
        )

        # Start/stop button + status
        self.btn = ttk.Button(self.frame, text="Start", width=8, command=self.toggle)
        self.btn.grid(row=0, column=5, padx=2)

        self.status_var = tk.StringVar(value="stopped")
        ttk.Label(self.frame, textvariable=self.status_var, width=10, anchor="w").grid(
            row=0, column=6, padx=2, sticky="w"
        )

        # Remove button
        ttk.Button(self.frame, text="X", width=2, command=self._remove).grid(row=0, column=7, padx=1)

    def _browse_sf(self):
        path = filedialog.askopenfilename(
            title="select soundfont",
            filetypes=[("SoundFont", "*.sf2 *.sf3"), ("all files", "*.*")],
        )
        if path:
            self.sf_var.set(path)

    def _remove(self):
        if self.is_running():
            if not messagebox.askyesno("stop and remove?", f"{self.name_var.get()} is running. stop it and remove?"):
                return
            self.stop()
        self.frame.destroy()
        self.app.remove_row(self)

    def to_dict(self) -> dict:
        return {
            "name": self.name_var.get().strip(),
            "device": self.device_var.get().strip(),
            "soundfont": self.sf_var.get().strip(),
            "gain": float(self.gain_var.get()),
        }

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def toggle(self):
        if self.is_running():
            self.stop()
        else:
            self.start()

    def start(self):
        shared = self.app.shared_settings()
        host = shared.get("host", "").strip()
        port = shared.get("port", 8766)
        driver = shared.get("driver", DEFAULT_DRIVER)
        sf = self.sf_var.get().strip() or shared.get("soundfont", "").strip()
        name = self.name_var.get().strip()
        device = self.device_var.get().strip()

        if not host or not name or not sf:
            messagebox.showerror(
                "missing fields",
                "host, name and soundfont are required (host comes from the top bar, soundfont per-bandmate or shared).",
            )
            return
        if not Path(sf).exists():
            messagebox.showerror("no soundfont", f"soundfont not found:\n{sf}")
            return
        if not CLIENT_PATH.exists():
            messagebox.showerror("missing client", f"can't find {CLIENT_PATH}. keep launcher in standalone/ folder.")
            return

        cmd = [
            sys.executable, str(CLIENT_PATH),
            "--host", host,
            "--port", str(port),
            "--name", name,
            "--soundfont", sf,
            "--gain", str(float(self.gain_var.get())),
        ]
        if driver:
            cmd += ["--driver", driver]
        if device:
            cmd += ["--device", device]

        creationflags = 0
        if sys.platform.startswith("win"):
            # spawn each in its own console window so you can see logs
            # for each bandmate independently. CREATE_NEW_CONSOLE = 0x10
            creationflags = subprocess.CREATE_NEW_CONSOLE  # type: ignore

        try:
            self.proc = subprocess.Popen(cmd, cwd=str(HERE), creationflags=creationflags)
        except Exception as e:
            messagebox.showerror("launch failed", str(e))
            return

        self.status_var.set(f"running pid {self.proc.pid}")
        self.btn.config(text="Stop")
        self._monitor_thread = threading.Thread(target=self._monitor, daemon=True)
        self._monitor_thread.start()
        log.info(f"started bandmate '{name}' pid {self.proc.pid}")

    def stop(self):
        if not self.is_running():
            return
        try:
            if sys.platform.startswith("win"):
                self.proc.terminate()
            else:
                self.proc.send_signal(signal.SIGTERM)
        except Exception as e:
            log.warning(f"stop failed: {e}")
        # give it a moment, then force
        try:
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.status_var.set("stopped")
        self.btn.config(text="Start")
        log.info(f"stopped bandmate '{self.name_var.get()}'")

    def _monitor(self):
        if self.proc is None:
            return
        rc = self.proc.wait()
        # update from the tk thread
        self.app.after(0, lambda: (self.status_var.set(f"exited ({rc})"), self.btn.config(text="Start")))


class LauncherApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("midi_band launcher")
        self.geometry("1100x500")

        self.devices = list_output_devices()
        roster = load_roster()
        shared = roster.get("shared", {})
        bandmates = roster.get("bandmates", [])

        # ----- shared settings strip -----
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="Host:").grid(row=0, column=0, padx=(0, 4))
        self.host_var = tk.StringVar(value=str(shared.get("host", "")))
        ttk.Entry(top, textvariable=self.host_var, width=18).grid(row=0, column=1, padx=4)

        ttk.Label(top, text="Port:").grid(row=0, column=2, padx=(8, 4))
        self.port_var = tk.IntVar(value=int(shared.get("port", 8766)))
        ttk.Spinbox(top, from_=1, to=65535, textvariable=self.port_var, width=7).grid(row=0, column=3, padx=4)

        ttk.Label(top, text="Driver:").grid(row=0, column=4, padx=(8, 4))
        self.driver_var = tk.StringVar(value=str(shared.get("driver", DEFAULT_DRIVER)))
        ttk.Combobox(
            top, textvariable=self.driver_var, width=10,
            values=["", "dsound", "wasapi", "alsa", "pulseaudio", "pipewire", "coreaudio"],
        ).grid(row=0, column=5, padx=4)

        ttk.Label(top, text="Default Soundfont:").grid(row=0, column=6, padx=(8, 4))
        self.sf_var = tk.StringVar(value=str(shared.get("soundfont", "")))
        ttk.Entry(top, textvariable=self.sf_var, width=28).grid(row=0, column=7, padx=4)
        ttk.Button(top, text="...", width=3, command=self._browse_default_sf).grid(row=0, column=8)

        ttk.Button(top, text="Save", command=self.save).grid(row=0, column=9, padx=(12, 4))

        if _sd is None:
            ttk.Label(
                top, foreground="#aa6600",
                text="(install 'sounddevice' for a real device dropdown)",
            ).grid(row=1, column=0, columnspan=10, sticky="w", pady=(4, 0))

        # ----- bandmate table -----
        table_frame = ttk.Frame(self, padding=(6, 4))
        table_frame.pack(fill="both", expand=True)

        header = ttk.Frame(table_frame, padding=(6, 2))
        header.grid(row=0, column=0, sticky="ew")
        for col, (txt, w) in enumerate([
            ("Name", 14), ("Output Device", 38), ("Soundfont", 24), ("", 3), ("Gain", 5), ("Action", 8),
            ("Status", 10), ("", 2),
        ]):
            ttk.Label(header, text=txt, width=w, anchor="w", font=("", 9, "bold")).grid(row=0, column=col, padx=2, sticky="w")

        self._table_parent = table_frame
        self.rows: List[BandmateRow] = []
        if not bandmates:
            bandmates = [{"name": "drummer", "device": "", "soundfont": "", "gain": 0.5}]
        for i, bm in enumerate(bandmates):
            self._add_row_data(bm)

        # ----- bottom bar -----
        bottom = ttk.Frame(self, padding=8)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="+ Add bandmate", command=self.add_row).pack(side="left")
        ttk.Button(bottom, text="Start all", command=self.start_all).pack(side="left", padx=(8, 0))
        ttk.Button(bottom, text="Stop all", command=self.stop_all).pack(side="left", padx=(4, 0))

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def shared_settings(self) -> dict:
        return {
            "host": self.host_var.get().strip(),
            "port": int(self.port_var.get()),
            "driver": self.driver_var.get().strip(),
            "soundfont": self.sf_var.get().strip(),
        }

    def _browse_default_sf(self):
        path = filedialog.askopenfilename(
            title="select default soundfont",
            filetypes=[("SoundFont", "*.sf2 *.sf3"), ("all files", "*.*")],
        )
        if path:
            self.sf_var.set(path)

    def _add_row_data(self, data: dict):
        row = BandmateRow(self, self._table_parent, len(self.rows), data, self.devices)
        self.rows.append(row)

    def add_row(self):
        self._add_row_data({"name": f"bandmate_{len(self.rows) + 1}", "device": "", "soundfont": "", "gain": 0.5})

    def remove_row(self, row: BandmateRow):
        if row in self.rows:
            self.rows.remove(row)
        # re-grid remaining rows
        for i, r in enumerate(self.rows):
            r.idx = i
            r.frame.grid_configure(row=i + 1)

    def save(self):
        data = {
            "shared": self.shared_settings(),
            "bandmates": [r.to_dict() for r in self.rows],
        }
        save_roster(data)
        log.info(f"saved roster ({len(self.rows)} bandmates) -> {ROSTER_PATH}")

    def start_all(self):
        for r in self.rows:
            if not r.is_running():
                r.start()

    def stop_all(self):
        for r in self.rows:
            if r.is_running():
                r.stop()

    def _on_close(self):
        running = [r for r in self.rows if r.is_running()]
        if running:
            if not messagebox.askyesno("quit", f"{len(running)} bandmate(s) still running. stop them and quit?"):
                return
            for r in running:
                r.stop()
        self.save()
        self.destroy()


def main():
    if yaml is None:
        log.warning("PyYAML not installed, roster save/load disabled. pip install pyyaml")
    app = LauncherApp()
    app.mainloop()


if __name__ == "__main__":
    main()
