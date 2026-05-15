"""midi_band standalone launcher GUI (CustomTkinter dark mode).

Spawn and manage multiple standalone_client.py instances from one
nice dark-themed window. Each row is one bandmate with its own audio
output device, soundfont and gain. Roster persists to bandmates.yml.

Run:
    uv run launcher_gui.py
or:
    python launcher_gui.py

Requires customtkinter (always) and optionally sounddevice for a real
output-device dropdown:
    pip install customtkinter sounddevice pyyaml
"""
from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import List, Optional


def _python_alias_for(name: str) -> str:
    """Return a python.exe path uniquely named for this bandmate so the
    Windows volume mixer treats it as its own app instead of lumping all
    bandmates under one shared 'python.exe' per-app routing entry.

    Hardlinks sys.executable inside its own folder so adjacent DLLs
    (python3XX.dll, vcruntime, ...) are still found. Falls back to
    sys.executable on non-Windows or if the link fails.
    """
    if not sys.platform.startswith("win"):
        return sys.executable
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "bandmate"
    src = Path(sys.executable)
    dst = src.parent / f"gabriel_{safe}.exe"
    if dst.exists():
        return str(dst)
    try:
        os.link(src, dst)
        return str(dst)
    except OSError as e:
        logging.getLogger("midi_band.launcher").warning(
            f"could not alias python.exe for '{name}' ({e}); "
            f"per-app audio routing will lump bandmates together."
        )
        return sys.executable

try:
    import customtkinter as ctk
except Exception:
    print("customtkinter is required: pip install customtkinter", file=sys.stderr)
    raise

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

DEFAULT_DRIVER = "wasapi" if sys.platform.startswith("win") else ""

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("launcher")

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

# palette pulled from dark-blue theme so everything matches
COL_BG = "#1a1a1a"
COL_CARD = "#242424"
COL_CARD_HI = "#2c2c2c"
COL_ACCENT = "#1f6aa5"
COL_GREEN = "#2fa84f"
COL_GREEN_HI = "#3bc861"
COL_RED = "#a83c2f"
COL_RED_HI = "#c64c3b"
COL_YELLOW = "#c98b1c"
COL_TEXT_DIM = "#888888"


def list_output_devices() -> List[str]:
    if _sd is None:
        return []
    try:
        # filter to WASAPI host api on windows so device names match
        # what fluidsynth's wasapi driver enumerates. otherwise the names
        # come from MME/portaudio and fluidsynth rejects them silently
        # and falls back to the default device.
        wasapi_idx = None
        if sys.platform.startswith("win"):
            try:
                for i, api in enumerate(_sd.query_hostapis()):
                    if "wasapi" in str(api.get("name", "")).lower():
                        wasapi_idx = i
                        break
            except Exception:
                pass
        devs = _sd.query_devices()
        out: List[str] = []
        for d in devs:
            if d.get("max_output_channels", 0) <= 0:
                continue
            if wasapi_idx is not None and d.get("hostapi") != wasapi_idx:
                continue
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
        messagebox.showerror("missing dep", "PyYAML not installed: pip install pyyaml")
        return
    try:
        with open(ROSTER_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    except Exception as e:
        messagebox.showerror("save failed", f"could not write {ROSTER_PATH}: {e}")


class BandmateCard(ctk.CTkFrame):
    """One bandmate as a horizontal card."""

    def __init__(self, app: "LauncherApp", parent, data: dict, devices: List[str]):
        super().__init__(parent, fg_color=COL_CARD, corner_radius=10)
        self.app = app
        self.devices = devices
        self.proc: Optional[subprocess.Popen] = None

        self.grid_columnconfigure(1, weight=1)

        # -- status dot + name (column 0)
        left = ctk.CTkFrame(self, fg_color="transparent")
        left.grid(row=0, column=0, padx=(14, 8), pady=12, sticky="nsw")

        self.dot = ctk.CTkLabel(left, text="\u25cf", font=("Segoe UI", 18), text_color=COL_TEXT_DIM)
        self.dot.pack(side="left", padx=(0, 8))

        self.name_var = ctk.StringVar(value=str(data.get("name") or "bandmate"))
        ctk.CTkEntry(
            left, textvariable=self.name_var, width=140, height=32,
            font=("Segoe UI", 13, "bold"),
        ).pack(side="left")

        # -- device + soundfont + gain (column 1, expanded)
        mid = ctk.CTkFrame(self, fg_color="transparent")
        mid.grid(row=0, column=1, padx=8, pady=12, sticky="ew")
        mid.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(mid, text="Output", width=60, anchor="w", text_color=COL_TEXT_DIM).grid(
            row=0, column=0, sticky="w"
        )
        self.device_var = ctk.StringVar(value=str(data.get("device") or ""))
        if devices:
            self.device_widget = ctk.CTkComboBox(
                mid, values=devices, variable=self.device_var, width=320, height=30,
            )
        else:
            self.device_widget = ctk.CTkEntry(mid, textvariable=self.device_var, width=320, height=30)
        self.device_widget.grid(row=0, column=1, sticky="ew", padx=(8, 0))

        ctk.CTkLabel(mid, text="Soundfont", width=60, anchor="w", text_color=COL_TEXT_DIM).grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        sf_row = ctk.CTkFrame(mid, fg_color="transparent")
        sf_row.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        sf_row.grid_columnconfigure(0, weight=1)
        self.sf_var = ctk.StringVar(value=str(data.get("soundfont") or ""))
        ctk.CTkEntry(sf_row, textvariable=self.sf_var, height=30).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(
            sf_row, text="...", width=32, height=30, command=self._browse_sf,
        ).grid(row=0, column=1, padx=(6, 0))

        # -- gain slider (column 2)
        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=0, column=2, padx=8, pady=12, sticky="nse")
        ctk.CTkLabel(right, text="Gain", text_color=COL_TEXT_DIM).pack(anchor="w")
        gain_row = ctk.CTkFrame(right, fg_color="transparent")
        gain_row.pack(fill="x")
        self.gain_var = ctk.DoubleVar(value=float(data.get("gain", 0.5)))
        self.gain_label = ctk.CTkLabel(gain_row, text=f"{self.gain_var.get():.2f}", width=40)
        self.gain_label.pack(side="right")
        slider = ctk.CTkSlider(
            gain_row, from_=0.0, to=2.0, number_of_steps=40, variable=self.gain_var,
            command=self._on_gain, width=120,
        )
        slider.pack(side="left", padx=(0, 8))

        # -- action buttons (column 3)
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=0, column=3, padx=(8, 14), pady=12, sticky="nse")
        self.btn = ctk.CTkButton(
            actions, text="Start", width=88, height=34, command=self.toggle,
            fg_color=COL_GREEN, hover_color=COL_GREEN_HI,
        )
        self.btn.pack(pady=(0, 6))
        self.status_var = ctk.StringVar(value="stopped")
        ctk.CTkLabel(actions, textvariable=self.status_var, text_color=COL_TEXT_DIM).pack()
        ctk.CTkButton(
            actions, text="Remove", width=88, height=24,
            fg_color="transparent", border_width=1, hover_color=COL_CARD_HI,
            command=self._remove,
        ).pack(pady=(6, 0))

    # ----- callbacks -----

    def _on_gain(self, _val):
        self.gain_label.configure(text=f"{self.gain_var.get():.2f}")

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
        self.destroy()
        self.app.remove_card(self)

    # ----- model -----

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

        # use a per-bandmate python alias so windows volume mixer can
        # route each instance to its own audio device
        py_exe = _python_alias_for(name)
        cmd = [
            py_exe, str(CLIENT_PATH),
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
            creationflags = subprocess.CREATE_NEW_CONSOLE  # type: ignore

        try:
            self.proc = subprocess.Popen(cmd, cwd=str(HERE), creationflags=creationflags)
        except Exception as e:
            messagebox.showerror("launch failed", str(e))
            return

        self._set_running(True, f"running pid {self.proc.pid}")
        threading.Thread(target=self._monitor, daemon=True).start()
        log.info(f"started bandmate '{name}' pid {self.proc.pid}")

    def stop(self):
        if not self.is_running():
            return
        self._stopping = True
        try:
            if sys.platform.startswith("win"):
                self.proc.terminate()
            else:
                self.proc.send_signal(signal.SIGTERM)
        except Exception as e:
            log.warning(f"stop failed: {e}")
        try:
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self._set_running(False, "stopped")
        log.info(f"stopped bandmate '{self.name_var.get()}'")

    def _set_running(self, on: bool, status: str):
        try:
            self.status_var.set(status)
            if on:
                self.dot.configure(text_color=COL_GREEN_HI)
                self.btn.configure(text="Stop", fg_color=COL_RED, hover_color=COL_RED_HI)
            else:
                self.dot.configure(text_color=COL_TEXT_DIM)
                self.btn.configure(text="Start", fg_color=COL_GREEN, hover_color=COL_GREEN_HI)
        except Exception:
            # widget already destroyed, ignore
            pass

    def _monitor(self):
        if self.proc is None:
            return
        rc = self.proc.wait()
        try:
            self.after(0, lambda: self._on_exit(rc))
        except Exception:
            # tk root or this widget gone, nothing to update
            pass

    def _on_exit(self, rc: int):
        # if we exited because the user pressed Stop, don't override the
        # already-set "stopped" status with "exited (1)".
        if getattr(self, "_stopping", False):
            self._stopping = False
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        try:
            if rc == 0:
                self._set_running(False, "stopped")
            else:
                self.status_var.set(f"exited ({rc})")
                self.dot.configure(text_color=COL_YELLOW)
                self.btn.configure(text="Start", fg_color=COL_GREEN, hover_color=COL_GREEN_HI)
        except Exception:
            pass


class LauncherApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("midi_band launcher")
        self.geometry("1180x680")
        self.minsize(900, 480)
        self.configure(fg_color=COL_BG)

        self.devices = list_output_devices()
        roster = load_roster()
        shared = roster.get("shared", {})
        bandmates = roster.get("bandmates", [])

        self._build_header(shared)
        self._build_table(bandmates)
        self._build_footer()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----- layout -----

    def _build_header(self, shared: dict):
        header = ctk.CTkFrame(self, fg_color=COL_CARD, corner_radius=12)
        header.pack(fill="x", padx=14, pady=(14, 8))

        title = ctk.CTkFrame(header, fg_color="transparent")
        title.pack(fill="x", padx=14, pady=(10, 0))
        ctk.CTkLabel(
            title, text="midi_band launcher",
            font=("Segoe UI", 18, "bold"),
        ).pack(side="left")
        ctk.CTkLabel(
            title, text="  Project Gabriel  \u2022  multi-bandmate", text_color=COL_TEXT_DIM,
            font=("Segoe UI", 11),
        ).pack(side="left")

        body = ctk.CTkFrame(header, fg_color="transparent")
        body.pack(fill="x", padx=14, pady=(8, 12))
        for c in range(8):
            body.grid_columnconfigure(c, weight=0)
        body.grid_columnconfigure(7, weight=1)

        ctk.CTkLabel(body, text="Host", text_color=COL_TEXT_DIM).grid(row=0, column=0, padx=(0, 4), sticky="w")
        self.host_var = ctk.StringVar(value=str(shared.get("host", "")))
        ctk.CTkEntry(body, textvariable=self.host_var, width=160, height=30).grid(row=0, column=1, padx=(0, 12))

        ctk.CTkLabel(body, text="Port", text_color=COL_TEXT_DIM).grid(row=0, column=2, padx=(0, 4))
        self.port_var = ctk.StringVar(value=str(shared.get("port", 8766)))
        ctk.CTkEntry(body, textvariable=self.port_var, width=80, height=30).grid(row=0, column=3, padx=(0, 12))

        ctk.CTkLabel(body, text="Driver", text_color=COL_TEXT_DIM).grid(row=0, column=4, padx=(0, 4))
        self.driver_var = ctk.StringVar(value=str(shared.get("driver", DEFAULT_DRIVER)))
        ctk.CTkComboBox(
            body, variable=self.driver_var, width=130, height=30,
            values=["", "dsound", "wasapi", "alsa", "pulseaudio", "pipewire", "coreaudio"],
        ).grid(row=0, column=5, padx=(0, 12))

        ctk.CTkLabel(body, text="Default Soundfont", text_color=COL_TEXT_DIM).grid(row=0, column=6, padx=(0, 4))
        sf_box = ctk.CTkFrame(body, fg_color="transparent")
        sf_box.grid(row=0, column=7, sticky="ew")
        sf_box.grid_columnconfigure(0, weight=1)
        self.sf_var = ctk.StringVar(value=str(shared.get("soundfont", "")))
        ctk.CTkEntry(sf_box, textvariable=self.sf_var, height=30).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(sf_box, text="...", width=32, height=30, command=self._browse_default_sf).grid(
            row=0, column=1, padx=(6, 0)
        )

        if _sd is None:
            ctk.CTkLabel(
                header, text_color=COL_YELLOW,
                text="install 'sounddevice' for a real device dropdown   (pip install sounddevice)",
                font=("Segoe UI", 10),
            ).pack(anchor="w", padx=14, pady=(0, 10))

        # autosave shared header on edit
        for var in (self.host_var, self.port_var, self.driver_var, self.sf_var):
            try:
                var.trace_add("write", lambda *_: self._schedule_save())
            except Exception:
                pass

    def _build_table(self, bandmates: List[dict]):
        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=14, pady=4)

        self.scroller = ctk.CTkScrollableFrame(wrap, fg_color=COL_BG, corner_radius=0)
        self.scroller.pack(fill="both", expand=True)

        self.cards: List[BandmateCard] = []
        if not bandmates:
            bandmates = [{"name": "drummer", "device": "", "soundfont": "", "gain": 0.5}]
        for bm in bandmates:
            self._add_card_data(bm)

    def _build_footer(self):
        bottom = ctk.CTkFrame(self, fg_color=COL_CARD, corner_radius=12)
        bottom.pack(fill="x", padx=14, pady=(8, 14))

        ctk.CTkButton(
            bottom, text="+ Add bandmate", width=140, height=34, command=self.add_card,
        ).pack(side="left", padx=(14, 6), pady=10)
        ctk.CTkButton(
            bottom, text="Start all", width=100, height=34, command=self.start_all,
            fg_color=COL_GREEN, hover_color=COL_GREEN_HI,
        ).pack(side="left", padx=6, pady=10)
        ctk.CTkButton(
            bottom, text="Stop all", width=100, height=34, command=self.stop_all,
            fg_color=COL_RED, hover_color=COL_RED_HI,
        ).pack(side="left", padx=6, pady=10)
        ctk.CTkButton(
            bottom, text="Save roster", width=120, height=34, command=self.save,
            fg_color="transparent", border_width=1,
        ).pack(side="right", padx=(6, 14), pady=10)

    # ----- helpers -----

    def shared_settings(self) -> dict:
        try:
            port = int(self.port_var.get())
        except Exception:
            port = 8766
        return {
            "host": self.host_var.get().strip(),
            "port": port,
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

    def _add_card_data(self, data: dict):
        card = BandmateCard(self, self.scroller, data, self.devices)
        card.pack(fill="x", padx=4, pady=6)
        self.cards.append(card)
        # save whenever fields on the card change so device/name/sf/gain
        # are always persisted, no need to remember to hit Save.
        for var in (card.name_var, card.device_var, card.sf_var, card.gain_var):
            try:
                var.trace_add("write", lambda *_: self._schedule_save())
            except Exception:
                pass

    def add_card(self):
        self._add_card_data({
            "name": f"bandmate_{len(self.cards) + 1}",
            "device": "",
            "soundfont": "",
            "gain": 0.5,
        })
        self._schedule_save()

    def remove_card(self, card: BandmateCard):
        if card in self.cards:
            self.cards.remove(card)
        self._schedule_save()

    def _schedule_save(self):
        # debounce so a slider drag doesn't write the file 40 times.
        if getattr(self, "_save_after_id", None):
            try:
                self.after_cancel(self._save_after_id)
            except Exception:
                pass
        self._save_after_id = self.after(800, self.save)

    def save(self):
        self._save_after_id = None
        data = {"shared": self.shared_settings(), "bandmates": [c.to_dict() for c in self.cards]}
        save_roster(data)
        log.info(f"saved roster ({len(self.cards)} bandmates) -> {ROSTER_PATH}")

    def start_all(self):
        for c in self.cards:
            if not c.is_running():
                c.start()

    def stop_all(self):
        for c in self.cards:
            if c.is_running():
                c.stop()

    def _on_close(self):
        running = [c for c in self.cards if c.is_running()]
        if running:
            if not messagebox.askyesno("quit", f"{len(running)} bandmate(s) still running. stop them and quit?"):
                return
            for c in running:
                c.stop()
        self.save()
        self.destroy()


def main():
    if yaml is None:
        log.warning("PyYAML not installed, roster save/load disabled. pip install pyyaml")
    app = LauncherApp()
    app.mainloop()


if __name__ == "__main__":
    main()
