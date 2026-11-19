"""
ui.py - Steuerpult fuer den Empire-Feldherrn.

    python ui.py      (oder Doppelklick auf start_ui.bat)

- API-Schluessel: gespeichert in der Windows-Anmeldeinformationsverwaltung (keyring), nie in einer Datei.
  Wird dem Feldherrn beim Start nur als Umgebungsvariable des Kindprozesses uebergeben.
- Modus: welche Seite das Modell fuehrt (schreibt <Spielordner>/ai_bridge/config.lua, wirkt ab der naechsten Schlacht).
- Feldherr: Claude-Modell oder Regel-Feldherr, Denktiefe, Planungstakt.
- Strategie: Vorlagen aus strategies/*.md (bearbeitbar, als neue Vorlage speicherbar) + freie Zusatzbefehle.
- Start/Stopp von commander.py mit Live-Protokoll.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

HERE = Path(__file__).resolve().parent
STRATEGIES = HERE / "strategies"
RUNTIME = HERE / "runtime"
SETTINGS = HERE / "settings.json"
KEYRING_SERVICE = "empire-battle-ai"
KEYRING_USER = "anthropic-api-key"

sys.path.insert(0, str(HERE))
from empire_bridge import EmpireBridge  # noqa: E402

MODELS = {
    "Claude Opus 5.5 (stärkstes Modell)": ("claude", "claude-opus-5-5"),
    "Claude Sonnet 5.5 (schneller, günstiger)": ("claude", "claude-sonnet-5-5"),
    "Claude Haiku 5.5 (am schnellsten, am günstigsten)": ("claude", "claude-haiku-5-5"),
    "Regel-Feldherr (ohne KI, kostenlos)": ("rule", None),
}
SIDES = [("Gegner", "enemy"), ("Meine Armee", "player"), ("Beide Seiten", "both"), ("Aus", "none")]
EFFORTS = ["low", "medium", "high"]

DEFAULTS = {"model": next(iter(MODELS)), "effort": "low", "interval": 10, "close_interval": 5,
            "strategy": "standard", "control": "enemy"}


# ---------------- Schluessel ----------------
def key_load() -> str | None:
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception:
        return None


def key_save(k: str) -> None:
    import keyring
    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, k)


def key_delete() -> None:
    import keyring
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception:
        pass


def mask(k: str) -> str:
    return k[:7] + "…" + k[-4:] if len(k) > 12 else "…"


# ---------------- Einstellungen ----------------
def settings_load() -> dict:
    try:
        return {**DEFAULTS, **json.loads(SETTINGS.read_text(encoding="utf-8"))}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULTS)


def settings_save(s: dict) -> None:
    SETTINGS.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")


def read_control() -> str:
    try:
        text = (EmpireBridge().dir / "config.lua").read_text(encoding="utf-8")
        m = re.search(r'control\s*=\s*"(\w+)"', text)
        return m.group(1) if m else "enemy"
    except FileNotFoundError:
        return "enemy"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Empire Battle AI – Steuerpult")
        self.geometry("980x780")
        self.minsize(820, 640)
        self.s = settings_load()
        self.proc: subprocess.Popen | None = None
        self.q: queue.Queue[str] = queue.Queue()
        self._build()
        self._refresh_key_status()
        self._load_strategy(self.s.get("strategy", "standard"))
        self.after(150, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- Aufbau ----------
    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)

        # Zugang
        f = ttk.LabelFrame(root, text="Zugang zu Claude", padding=6)
        f.grid(row=0, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="API-Schlüssel:").grid(row=0, column=0, sticky="w")
        self.key_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.key_var, show="•").grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(f, text="Speichern", command=self._key_save).grid(row=0, column=2, padx=2)
        ttk.Button(f, text="Prüfen", command=self._key_test).grid(row=0, column=3, padx=2)
        ttk.Button(f, text="Löschen", command=self._key_delete).grid(row=0, column=4, padx=2)
        self.key_status = ttk.Label(f, text="", foreground="#555")
        self.key_status.grid(row=1, column=0, columnspan=5, sticky="w", pady=(4, 0))

        # Schlacht
        f = ttk.LabelFrame(root, text="Schlacht", padding=6)
        f.grid(row=1, column=0, sticky="ew", **pad)
        ttk.Label(f, text="KI führt:").grid(row=0, column=0, sticky="w")
        self.control_var = tk.StringVar(value=read_control())
        side_box = ttk.Frame(f)
        side_box.grid(row=0, column=1, columnspan=5, sticky="w")
        for label, val in SIDES:
            ttk.Radiobutton(side_box, text=label, value=val, variable=self.control_var,
                            command=self._write_control).pack(side="left", padx=(0, 12))
        ttk.Label(f, text="(gilt ab der nächsten Schlacht)", foreground="#777").grid(row=0, column=6, sticky="w")

        ttk.Label(f, text="Feldherr:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.model_var = tk.StringVar(value=self.s["model"] if self.s["model"] in MODELS else DEFAULTS["model"])
        ttk.Combobox(f, textvariable=self.model_var, values=list(MODELS), state="readonly", width=44).grid(
            row=1, column=1, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(f, text="Denktiefe:").grid(row=1, column=4, sticky="e", padx=(12, 2), pady=(6, 0))
        self.effort_var = tk.StringVar(value=self.s["effort"])
        ttk.Combobox(f, textvariable=self.effort_var, values=EFFORTS, state="readonly", width=8).grid(
            row=1, column=5, sticky="w", pady=(6, 0))

        ttk.Label(f, text="Neuer Plan alle").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.int_var = tk.IntVar(value=int(self.s["interval"]))
        self.cint_var = tk.IntVar(value=int(self.s["close_interval"]))
        tb = ttk.Frame(f)
        tb.grid(row=2, column=1, columnspan=5, sticky="w", pady=(6, 0))
        ttk.Spinbox(tb, from_=3, to=60, textvariable=self.int_var, width=4).pack(side="left")
        ttk.Label(tb, text=" s Spielzeit, im Nahkampf alle ").pack(side="left")
        ttk.Spinbox(tb, from_=2, to=30, textvariable=self.cint_var, width=4).pack(side="left")
        ttk.Label(tb, text=" s   (kürzer = reaktiver, aber mehr Aufrufe/Kosten)", foreground="#777").pack(side="left")

        # Strategie
        f = ttk.LabelFrame(root, text="Strategie (System-Prompt)", padding=6)
        f.grid(row=2, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Vorlage:").grid(row=0, column=0, sticky="w")
        self.strat_var = tk.StringVar()
        self.strat_box = ttk.Combobox(f, textvariable=self.strat_var, state="readonly", width=30)
        self.strat_box.grid(row=0, column=1, sticky="w")
        self.strat_box.bind("<<ComboboxSelected>>", lambda e: self._load_strategy(self.strat_var.get()))
        bb = ttk.Frame(f)
        bb.grid(row=0, column=2, sticky="e")
        ttk.Button(bb, text="Speichern", command=self._strategy_save).pack(side="left", padx=2)
        ttk.Button(bb, text="Als neue Vorlage…", command=self._strategy_save_as).pack(side="left", padx=2)
        self.strat_text = tk.Text(f, height=9, wrap="word", undo=True, font=("Segoe UI", 9))
        self.strat_text.grid(row=1, column=0, columnspan=3, sticky="ew", pady=4)
        ttk.Label(f, text="Nur Zeilen mit „- “ gehen an das Modell. Zusatzbefehle für diese Schlacht (freier Text, "
                          "z. B. „Halte den Hügel im Westen um jeden Preis“):", foreground="#555").grid(
            row=2, column=0, columnspan=3, sticky="w")
        self.instr_text = tk.Text(f, height=3, wrap="word", undo=True, font=("Segoe UI", 9))
        self.instr_text.grid(row=3, column=0, columnspan=3, sticky="ew", pady=4)
        self.instr_text.insert("1.0", self.s.get("instructions", ""))

        # Steuerung + Protokoll
        f = ttk.LabelFrame(root, text="Feldherr", padding=6)
        f.grid(row=3, column=0, sticky="nsew", **pad)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        bar = ttk.Frame(f)
        bar.grid(row=0, column=0, sticky="ew")
        self.start_btn = ttk.Button(bar, text="▶ Starten", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="■ Stoppen", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(bar, text="Logs öffnen", command=self._open_logs).pack(side="left", padx=4)
        self.run_status = ttk.Label(bar, text="gestoppt", foreground="#555")
        self.run_status.pack(side="left", padx=12)
        self.log = tk.Text(f, height=12, wrap="word", state="disabled", font=("Consolas", 9), background="#fafafa")
        self.log.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        sb = ttk.Scrollbar(f, command=self.log.yview)
        sb.grid(row=1, column=1, sticky="ns", pady=(6, 0))
        self.log["yscrollcommand"] = sb.set

    # ---------- Schluessel ----------
    def _refresh_key_status(self) -> None:
        k = key_load()
        if k:
            self.key_status.config(text=f"Gespeichert in der Windows-Anmeldeinformationsverwaltung: {mask(k)}")
        elif os.environ.get("ANTHROPIC_API_KEY"):
            self.key_status.config(text="Kein Schlüssel gespeichert – nutze ANTHROPIC_API_KEY aus der Umgebung.")
        else:
            self.key_status.config(text="Kein Schlüssel gespeichert. Für Claude bitte einen API-Schlüssel eintragen "
                                        "(oder `ant auth login`). Der Regel-Feldherr braucht keinen.")

    def _key_save(self) -> None:
        k = self.key_var.get().strip()
        if not k:
            messagebox.showinfo("API-Schlüssel", "Bitte zuerst einen Schlüssel eintragen.")
            return
        key_save(k)
        self.key_var.set("")
        self._refresh_key_status()

    def _key_delete(self) -> None:
        if messagebox.askyesno("API-Schlüssel", "Gespeicherten Schlüssel aus der Windows-Anmeldeinformations"
                                                "verwaltung löschen?"):
            key_delete()
            self._refresh_key_status()

    def _key_test(self) -> None:
        k = self.key_var.get().strip() or key_load() or os.environ.get("ANTHROPIC_API_KEY")
        if not k:
            messagebox.showinfo("API-Schlüssel", "Kein Schlüssel vorhanden.")
            return
        self.key_status.config(text="Prüfe …")

        def run():
            try:
                import anthropic
                anthropic.Anthropic(api_key=k, max_retries=0, timeout=15).models.list(limit=1)
                msg = "Schlüssel gültig ✓ (Verbindung zur Claude-API steht)"
            except Exception as e:  # Anzeige im UI, kein Abbruch
                name = type(e).__name__
                if "Authentication" in name:
                    msg = "Schlüssel ungültig ✗"
                else:
                    # eigentliche Ursache zeigen (z. B. verpackt die SDK lokale Fehler als "Connection error")
                    cause = e.__cause__ or e.__context__
                    detail = f" – Ursache: {type(cause).__name__}: {str(cause)[:120]}" if cause else ""
                    msg = f"Prüfung fehlgeschlagen: {name}{detail}"
                msg = msg.replace(k, "<Schlüssel>")
            self.after(0, lambda: self.key_status.config(text=msg))

        threading.Thread(target=run, daemon=True).start()

    # ---------- Modus ----------
    def _write_control(self) -> None:
        try:
            EmpireBridge().write_config(control=self.control_var.get())
            self._append(f"Modus gespeichert: KI führt '{self.control_var.get()}' (ab der nächsten Schlacht).\n")
        except Exception as e:
            messagebox.showerror("config.lua", str(e))

    # ---------- Strategie ----------
    def _strategy_names(self) -> list[str]:
        return sorted(p.stem for p in STRATEGIES.glob("*.md"))

    def _load_strategy(self, name: str) -> None:
        names = self._strategy_names()
        self.strat_box["values"] = names
        if name not in names and names:
            name = "standard" if "standard" in names else names[0]
        self.strat_var.set(name)
        self.strat_text.delete("1.0", "end")
        p = STRATEGIES / f"{name}.md"
        if p.exists():
            self.strat_text.insert("1.0", p.read_text(encoding="utf-8"))

    def _strategy_save(self) -> None:
        name = self.strat_var.get()
        if not name:
            return self._strategy_save_as()
        (STRATEGIES / f"{name}.md").write_text(self.strat_text.get("1.0", "end").rstrip() + "\n", encoding="utf-8")
        self._append(f"Strategie '{name}' gespeichert.\n")

    def _strategy_save_as(self) -> None:
        name = simpledialog.askstring("Neue Vorlage", "Name der Strategie (nur Buchstaben, Zahlen, _ und -):", parent=self)
        if not name:
            return
        name = re.sub(r"[^\w-]", "_", name.strip())
        (STRATEGIES / f"{name}.md").write_text(self.strat_text.get("1.0", "end").rstrip() + "\n", encoding="utf-8")
        self._load_strategy(name)
        self._append(f"Neue Strategie '{name}' angelegt.\n")

    # ---------- Start/Stopp ----------
    def _collect(self) -> dict:
        s = {"model": self.model_var.get(), "effort": self.effort_var.get(), "interval": int(self.int_var.get()),
             "close_interval": int(self.cint_var.get()), "strategy": self.strat_var.get(),
             "control": self.control_var.get(), "instructions": self.instr_text.get("1.0", "end").strip()}
        settings_save(s)
        return s

    def _start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        s = self._collect()
        backend, model = MODELS[s["model"]]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        if backend == "claude":
            k = key_load()
            if k:
                env["ANTHROPIC_API_KEY"] = k
            elif not os.environ.get("ANTHROPIC_API_KEY"):
                if not messagebox.askyesno("Kein API-Schlüssel", "Es ist kein Schlüssel gespeichert. Trotzdem starten "
                                                                 "(z. B. mit `ant auth login`)?"):
                    return
        RUNTIME.mkdir(exist_ok=True)
        # aktuelle (ggf. ungespeicherte) Strategie und Zusatzbefehle fuer diesen Lauf
        strat_file = RUNTIME / f"{s['strategy'] or 'eigene'}.md"
        strat_file.write_text(self.strat_text.get("1.0", "end"), encoding="utf-8")
        instr_file = RUNTIME / "instructions.txt"
        instr_file.write_text(s["instructions"], encoding="utf-8")
        cmd = [sys.executable, str(HERE / "commander.py"), "--backend", backend, "--effort", s["effort"],
               "--interval", str(s["interval"]), "--close-interval", str(s["close_interval"]),
               "--strategy", str(strat_file), "--instructions", str(instr_file)]
        if model:
            cmd += ["--model", model]
        self._write_control()
        self._append(f"\n=== Start: {s['model']}, Denktiefe {s['effort']}, Strategie {s['strategy']} ===\n")
        self.proc = subprocess.Popen(cmd, cwd=str(HERE), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace",
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.run_status.config(text="läuft – wartet auf eine Schlacht", foreground="#1a7f37")

    def _reader(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            self.q.put(line)
        self.q.put(f"=== Feldherr beendet (Code {proc.wait()}) ===\n")
        self.q.put("\x00stopped")

    def _stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _pump(self) -> None:
        try:
            while True:
                line = self.q.get_nowait()
                if line == "\x00stopped":
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.run_status.config(text="gestoppt", foreground="#555")
                    continue
                if "Neue Schlacht" in line or "Uebernehme" in line:
                    self.run_status.config(text="führt eine Schlacht", foreground="#1a7f37")
                self._append(line)
        except queue.Empty:
            pass
        self.after(150, self._pump)

    def _open_logs(self) -> None:
        (HERE / "logs").mkdir(exist_ok=True)
        os.startfile(HERE / "logs")

    def _append(self, text: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.config(state="disabled")

    def _on_close(self) -> None:
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("Beenden", "Der Feldherr läuft noch. Beenden und Fenster schließen?"):
                return
            self.proc.terminate()
        self._collect()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
