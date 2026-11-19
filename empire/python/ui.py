"""
ui.py - Steuerpult fuer den Empire-Feldherrn.

    python ui.py      (oder Doppelklick auf start_ui.bat)

- API-Schluessel: gespeichert in der Windows-Anmeldeinformationsverwaltung (keyring), nie in einer Datei.
  Wird dem Feldherrn beim Start nur als Umgebungsvariable des Kindprozesses uebergeben.
- Modus: welche Seite das Modell fuehrt (schreibt <Spielordner>/ai_bridge/config.lua, wirkt ab der naechsten Schlacht).
- Feldherr: Claude-Modell oder Regel-Feldherr, Denktiefe, Planungstakt.
- Strategie: Vorlagen aus strategies/*.md (bearbeitbar, als neue Vorlage speicherbar) + freie Zusatzbefehle.
- Aufstellungsprompt: wird bei jedem Strg+Alt+A (oder Button) neu gelesen.
- Aktionen in der Schlacht: Aufstellung uebernehmen, markierte Einheiten -> KI / -> Spieler
  (Buttons oder globale Hotkeys Strg+Alt+A / K / P, solange der Feldherr laeuft).
- Alle Einstellungen, Prompts und ungespeicherten Strategie-Entwuerfe werden laufend in settings.json gesichert
  (lokal, nicht im Repo) und beim naechsten Start wiederhergestellt.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
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
ACTIONS = [("Aufstellung übernehmen", "deploy"), ("Markierte → KI", "sel_ai"), ("Markierte → Spieler", "sel_player")]
DEFAULT_HOTKEYS = {"deploy": "F9", "sel_ai": "F10", "sel_player": "F11"}

DEFAULTS = {"model": next(iter(MODELS)), "effort": "low", "interval": 10, "close_interval": 5,
            "strategy": "standard", "strategy_drafts": {}, "orders_player": "", "orders_enemy": "", "deploy_prompt": "",
            "hotkeys": dict(DEFAULT_HOTKEYS), "own_groups": False, "review_baselines": {},
            "geometry": "1040x1000"}


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
        s = {**DEFAULTS, **json.loads(SETTINGS.read_text(encoding="utf-8"))}
    except (FileNotFoundError, json.JSONDecodeError):
        return json.loads(json.dumps(DEFAULTS))
    # frueheres Feld "Zusatzbefehle" (galt fuer beide Seiten) -> Befehle meiner Armee
    old = (s.pop("instructions", "") or "").strip()
    if old and not (s.get("orders_player") or "").strip():
        s["orders_player"] = old
    return s


def settings_save(s: dict) -> None:
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, SETTINGS)


def read_config() -> tuple[str, bool]:
    """(control, own_groups) aus <Spiel>/ai_bridge/config.lua."""
    try:
        text = (EmpireBridge().dir / "config.lua").read_text(encoding="utf-8")
        m = re.search(r'control\s*=\s*"(\w+)"', text)
        g = re.search(r'own_groups\s*=\s*(true|false)', text)
        return (m.group(1) if m else "enemy", bool(g and g.group(1) == "true"))
    except (FileNotFoundError, SystemExit):
        return ("enemy", False)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Empire Battle AI – Steuerpult")
        self.s = settings_load()
        self.geometry(self.s.get("geometry") or DEFAULTS["geometry"])
        self.minsize(860, 760)
        self.proc: subprocess.Popen | None = None
        self.q: queue.Queue[str] = queue.Queue()
        self.calls: queue.Queue = queue.Queue()   # Rueckrufe aus Hintergrund-Threads, ausgefuehrt im Hauptthread
        self._save_job: str | None = None
        self._loading = True
        self._build()
        self._refresh_key_status()
        self._load_strategy(self.s.get("strategy", "standard"))
        self._loading = False
        self._write_runtime()
        self._pump_job = self.after(150, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- Aufbau ----------
    def _text(self, parent, height: int, content: str) -> tk.Text:
        t = tk.Text(parent, height=height, wrap="word", undo=True, font=("Segoe UI", 9))
        t.insert("1.0", content)
        t.edit_reset()
        t.bind("<KeyRelease>", lambda e: self._schedule_save())
        t.bind("<<Paste>>", lambda e: self.after(50, self._schedule_save))
        return t

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
        control, own_groups = read_config()
        self.control_var = tk.StringVar(value=control)
        side_box = ttk.Frame(f)
        side_box.grid(row=0, column=1, columnspan=5, sticky="w")
        for label, val in SIDES:
            ttk.Radiobutton(side_box, text=label, value=val, variable=self.control_var,
                            command=self._write_control).pack(side="left", padx=(0, 12))
        ttk.Label(f, text="(gilt ab der nächsten Schlacht)", foreground="#777").grid(row=0, column=6, sticky="w")
        self.own_groups_var = tk.BooleanVar(value=own_groups)
        ttk.Checkbutton(f, text="Eigenes Gruppenmanagement – die KI übernimmt auch Einheiten in meinen Gruppen "
                                "(Versuch über add_group, Ergebnis im Protokoll)",
                        variable=self.own_groups_var, command=self._write_control).grid(
            row=3, column=0, columnspan=7, sticky="w", pady=(6, 0))

        ttk.Label(f, text="Feldherr:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.model_var = tk.StringVar(value=self.s["model"] if self.s["model"] in MODELS else DEFAULTS["model"])
        ttk.Combobox(f, textvariable=self.model_var, values=list(MODELS), state="readonly", width=44).grid(
            row=1, column=1, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(f, text="Denktiefe:").grid(row=1, column=4, sticky="e", padx=(12, 2), pady=(6, 0))
        self.effort_var = tk.StringVar(value=self.s["effort"] if self.s["effort"] in EFFORTS else "low")
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
        for v in (self.model_var, self.effort_var, self.int_var, self.cint_var):
            v.trace_add("write", lambda *a: self._schedule_save())

        # Prompts als Registerkarten (Befehle | Strategie | Aufstellung), damit das Protokoll Platz behaelt
        self.nb = ttk.Notebook(root)
        self.nb.grid(row=2, column=0, sticky="ew", **pad)

        # Strategie
        f = ttk.Frame(self.nb, padding=6)
        tab_strategy = f
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Vorlage:").grid(row=0, column=0, sticky="w")
        self.strat_var = tk.StringVar()
        self.strat_box = ttk.Combobox(f, textvariable=self.strat_var, state="readonly", width=30)
        self.strat_box.grid(row=0, column=1, sticky="w")
        self.strat_box.bind("<<ComboboxSelected>>", lambda e: self._switch_strategy())
        bb = ttk.Frame(f)
        bb.grid(row=0, column=2, sticky="e")
        self.draft_label = ttk.Label(bb, text="", foreground="#b35900")
        self.draft_label.pack(side="left", padx=6)
        ttk.Button(bb, text="Speichern", command=self._strategy_save).pack(side="left", padx=2)
        ttk.Button(bb, text="Als neue Vorlage…", command=self._strategy_save_as).pack(side="left", padx=2)
        ttk.Button(bb, text="Verwerfen", command=self._strategy_discard).pack(side="left", padx=2)
        ttk.Button(bb, text="Prüfen & verbessern…",
                   command=lambda: self._review("strategy", self.strat_text, "Strategie")).pack(side="left", padx=(10, 2))
        self.strat_text = self._text(f, 9, "")
        self.strat_text.grid(row=1, column=0, columnspan=3, sticky="ew", pady=4)
        ttk.Label(f, text="Vorlage = allgemeine Ratschläge für beide Seiten (nur Zeilen mit „- “). Verbindliche Taktik "
                          "gehört in den Reiter „Befehle“. Alles wirkt ab dem nächsten Plan, auch während der Schlacht.",
                  foreground="#555").grid(row=2, column=0, columnspan=3, sticky="w")

        # Befehle je Seite
        f = ttk.Frame(self.nb, padding=6)
        tab_orders = f
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)
        hp = ttk.Frame(f)
        hp.grid(row=0, column=0, sticky="ew")
        ttk.Label(hp, text="Meine Armee (wenn die KI sie führt):").pack(side="left")
        he = ttk.Frame(f)
        he.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Label(he, text="Gegner (wenn die KI ihn führt):").pack(side="left")
        self.orders_player_text = self._text(f, 10, self.s.get("orders_player", ""))
        self.orders_player_text.grid(row=1, column=0, sticky="ew", pady=4)
        self.orders_enemy_text = self._text(f, 10, self.s.get("orders_enemy", ""))
        self.orders_enemy_text.grid(row=1, column=1, sticky="ew", pady=4, padx=(8, 0))
        ttk.Button(hp, text="Prüfen & verbessern…",
                   command=lambda: self._review("orders_player", self.orders_player_text, "Befehle meine Armee")).pack(side="right")
        ttk.Button(he, text="Prüfen & verbessern…",
                   command=lambda: self._review("orders_enemy", self.orders_enemy_text, "Befehle Gegner")).pack(side="right")

        # Aufstellung
        f = ttk.Frame(self.nb, padding=6)
        tab_deploy = f
        f.columnconfigure(0, weight=1)
        hd = ttk.Frame(f)
        hd.grid(row=0, column=0, sticky="ew")
        ttk.Label(hd, text="Wie soll aufgestellt werden? z. B. „Infanterie in zwei Linien, Artillerie auf dem Hügel "
                           "rechts, Kavallerie als Reserve hinter dem linken Flügel“", foreground="#555").pack(side="left")
        ttk.Button(hd, text="Prüfen & verbessern…",
                   command=lambda: self._review("deploy", self.deploy_text, "Aufstellungsprompt")).pack(side="right")
        self.deploy_text = self._text(f, 10, self.s.get("deploy_prompt", ""))
        self.deploy_text.grid(row=1, column=0, sticky="ew", pady=4)

        # Einheiten: Liste mit Uebergabe KI <-> Spieler (ohne Auswahl im Spiel - uebernommene Einheiten sperrt das Spiel)
        tab_units = ttk.Frame(self.nb, padding=6)
        tab_units.columnconfigure(0, weight=1)
        ub = ttk.Frame(tab_units)
        ub.grid(row=0, column=0, sticky="ew")
        self.units_side = tk.StringVar(value="player")
        ttk.Radiobutton(ub, text="Meine Armee", value="player", variable=self.units_side,
                        command=self._refresh_units).pack(side="left")
        ttk.Radiobutton(ub, text="Gegner", value="enemy", variable=self.units_side,
                        command=self._refresh_units).pack(side="left", padx=(8, 16))
        ttk.Button(ub, text="Markierte → Spieler", command=lambda: self._give("give_player")).pack(side="left", padx=2)
        ttk.Button(ub, text="Markierte → KI", command=lambda: self._give("give_ai")).pack(side="left", padx=2)
        self.units_info = ttk.Label(ub, text="Liste erscheint, sobald der Feldherr eine Schlacht sieht.", foreground="#777")
        self.units_info.pack(side="left", padx=12)
        cols = ("name", "type", "men", "lage", "fuehrung", "status")
        self.units_tree = ttk.Treeview(tab_units, columns=cols, show="headings", height=9, selectmode="extended")
        for c, label, wd in (("name", "Name", 150), ("type", "Typ (Spieldaten)", 230), ("men", "Mann", 70),
                             ("lage", "Lage", 110), ("fuehrung", "Führung", 150), ("status", "Status", 90)):
            self.units_tree.heading(c, text=label)
            self.units_tree.column(c, width=wd, anchor="w")
        self.units_tree.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        usb = ttk.Scrollbar(tab_units, command=self.units_tree.yview)
        usb.grid(row=1, column=1, sticky="ns", pady=(6, 0))
        self.units_tree["yscrollcommand"] = usb.set
        ttk.Label(tab_units, text="Mehrere Einheiten mit Strg/Umschalt auswählen. „Mann“ entspricht der Zahl unter der "
                                  "Einheitenkarte im Spiel. Die Namen benutzt auch der Feldherr – im Chat z. B. "
                                  "„Kavallerie 3 greift gegn. Artillerie 1 an“.", foreground="#777").grid(
            row=2, column=0, sticky="w", pady=(4, 0))

        self.nb.add(tab_orders, text="  Befehle (verbindlich)  ")
        self.nb.add(tab_strategy, text="  Strategie (Ratschläge)  ")
        self.nb.add(tab_deploy, text="  Aufstellung (F9)  ")
        self.nb.add(tab_units, text="  Einheiten  ")
        ttk.Label(tab_deploy, text="Wird bei jedem „Aufstellung übernehmen“ neu gelesen.", foreground="#777").grid(
            row=2, column=0, sticky="w")

        # Steuerung + Protokoll
        f = ttk.LabelFrame(root, text="Feldherr", padding=6)
        f.grid(row=3, column=0, sticky="nsew", **pad)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(2, weight=1)
        bar = ttk.Frame(f)
        bar.grid(row=0, column=0, sticky="ew")
        self.start_btn = ttk.Button(bar, text="▶ Starten", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="■ Stoppen", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(bar, text="Logs öffnen", command=self._open_logs).pack(side="left", padx=4)
        self.run_status = ttk.Label(bar, text="gestoppt", foreground="#555")
        self.run_status.pack(side="left", padx=12)
        act = ttk.Frame(f)
        act.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.action_btns = []
        self.hotkey_vars: dict[str, tk.StringVar] = {}
        hk = {**DEFAULT_HOTKEYS, **(self.s.get("hotkeys") or {})}
        for label, cmd in ACTIONS:
            b = ttk.Button(act, text=label, command=lambda c=cmd: self._action(c), state="disabled")
            b.pack(side="left", padx=(0, 2))
            v = tk.StringVar(value=hk[cmd])
            ttk.Entry(act, textvariable=v, width=12).pack(side="left", padx=(0, 12))
            v.trace_add("write", lambda *a: self._schedule_save())
            self.hotkey_vars[cmd] = v
            self.action_btns.append(b)
        # Chat: Sofortbefehle an den Feldherrn waehrend der Schlacht
        chat = ttk.Frame(f)
        chat.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        chat.columnconfigure(2, weight=1)
        ttk.Label(chat, text="Befehl an").grid(row=0, column=0, sticky="w")
        self.chat_target = tk.StringVar(value="Meine Armee")
        ttk.Combobox(chat, textvariable=self.chat_target, values=["Meine Armee", "Gegner"], state="readonly",
                     width=12).grid(row=0, column=1, padx=4)
        self.chat_var = tk.StringVar()
        self.chat_entry = ttk.Entry(chat, textvariable=self.chat_var)
        self.chat_entry.grid(row=0, column=2, sticky="ew", padx=4)
        self.chat_entry.bind("<Return>", lambda e: self._send_order())
        ttk.Button(chat, text="Senden", command=self._send_order).grid(row=0, column=3, padx=2)
        ttk.Button(chat, text="Befehle aufheben", command=self._clear_orders).grid(row=0, column=4, padx=2)
        self.active_orders = ttk.Label(chat, text="", foreground="#1a5fb4", wraplength=900, justify="left")
        self.active_orders.grid(row=1, column=0, columnspan=5, sticky="w", pady=(2, 0))
        ttk.Label(f, text="Feld rechts neben jedem Button = Hotkey (z. B. F9, Pause, Num5, Strg+Shift+F9). Empire sieht "
                          "jede Taste ebenfalls – Tasten wählen, die das Spiel nicht belegt. Gilt ab dem nächsten Start.",
                  foreground="#777").grid(row=4, column=0, sticky="w", pady=(4, 0))
        self.log = tk.Text(f, height=10, wrap="word", state="disabled", font=("Consolas", 9), background="#fafafa")
        self.log.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        sb = ttk.Scrollbar(f, command=self.log.yview)
        sb.grid(row=2, column=1, sticky="ns", pady=(6, 0))
        self.log["yscrollcommand"] = sb.set

    # ---------- Schluessel ----------
    def _refresh_key_status(self) -> None:
        k = key_load()
        if k:
            self.key_status.config(text=f"Gespeichert in der Windows-Anmeldeinformationsverwaltung: {mask(k)} "
                                        "(bleibt zwischen Sitzungen erhalten)")
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
            self.calls.put(lambda: self.key_status.config(text=msg))

        threading.Thread(target=run, daemon=True).start()

    # ---------- Modus ----------
    def _write_control(self) -> None:
        try:
            EmpireBridge().write_config(control=self.control_var.get(), own_groups=self.own_groups_var.get())
            self._append(f"Modus gespeichert: KI führt '{self.control_var.get()}' (ab der nächsten Schlacht).\n")
        except (Exception, SystemExit) as e:
            messagebox.showerror("config.lua", str(e))

    # ---------- Strategie ----------
    def _strategy_names(self) -> list[str]:
        return sorted(p.stem for p in STRATEGIES.glob("*.md"))

    def _strategy_file_text(self, name: str) -> str:
        p = STRATEGIES / f"{name}.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def _load_strategy(self, name: str) -> None:
        names = self._strategy_names()
        self.strat_box["values"] = names
        if name not in names and names:
            name = "standard" if "standard" in names else names[0]
        self.strat_var.set(name)
        draft = (self.s.get("strategy_drafts") or {}).get(name)
        self.strat_text.delete("1.0", "end")
        self.strat_text.insert("1.0", draft if draft is not None else self._strategy_file_text(name))
        self.strat_text.edit_reset()
        self._update_draft_label()

    def _switch_strategy(self) -> None:
        self._load_strategy(self.strat_var.get())
        self._schedule_save()

    def _update_draft_label(self) -> None:
        name = self.strat_var.get()
        is_draft = name in (self.s.get("strategy_drafts") or {})
        self.draft_label.config(text="ungespeicherter Entwurf (wird trotzdem verwendet)" if is_draft else "")

    def _strategy_save(self) -> None:
        name = self.strat_var.get()
        if not name:
            return self._strategy_save_as()
        (STRATEGIES / f"{name}.md").write_text(self.strat_text.get("1.0", "end").rstrip() + "\n", encoding="utf-8")
        self.s.setdefault("strategy_drafts", {}).pop(name, None)
        self._save_now()
        self._append(f"Strategie '{name}' gespeichert.\n")

    def _strategy_save_as(self) -> None:
        name = simpledialog.askstring("Neue Vorlage", "Name der Strategie (nur Buchstaben, Zahlen, _ und -):", parent=self)
        if not name:
            return
        name = re.sub(r"[^\w-]", "_", name.strip())
        old = self.strat_var.get()
        (STRATEGIES / f"{name}.md").write_text(self.strat_text.get("1.0", "end").rstrip() + "\n", encoding="utf-8")
        self.s.setdefault("strategy_drafts", {}).pop(old, None)
        self._load_strategy(name)
        self._save_now()
        self._append(f"Neue Strategie '{name}' angelegt.\n")

    def _strategy_discard(self) -> None:
        name = self.strat_var.get()
        self.s.setdefault("strategy_drafts", {}).pop(name, None)
        self._load_strategy(name)
        self._save_now()

    # ---------- Prompt pruefen & verbessern ----------
    def _review(self, kind: str, widget: tk.Text, title: str, full: bool = False) -> None:
        """Claude analysiert den Text (Teil 1) und schlaegt eine bessere Fassung vor (Teil 2, bearbeitbar).
        'Übernehmen' setzt den (ggf. angepassten) Vorschlag ins Feld und merkt sich ihn als Grundlage: Zeilen, die
        danach unveraendert bleiben, sind bei der naechsten Pruefung geschuetzt (full=True prueft alles neu)."""
        text = widget.get("1.0", "end").strip()
        baseline = None if full else (self.s.get("review_baselines") or {}).get(kind)
        if not text:
            messagebox.showinfo(title, "Das Feld ist leer – es gibt nichts zu prüfen.")
            return
        key = key_load() or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            messagebox.showinfo(title, "Für die Prüfung wird ein API-Schlüssel gebraucht (oben eintragen und speichern).")
            return
        backend, model = MODELS.get(self.model_var.get(), ("claude", None))
        model = model or "claude-sonnet-5-5"   # Regel-Feldherr gewaehlt: Pruefung trotzdem mit Claude
        win = tk.Toplevel(self)
        win.title(f"Prüfen & verbessern – {title}")
        win.geometry("900x720")
        win.transient(self)
        frm = ttk.Frame(win, padding=8)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(1, weight=1)
        frm.rowconfigure(3, weight=1)
        situation = self._situation(kind)
        live = "mit Lagebild der laufenden Schlacht" if situation else "ohne Lagebild (keine Schlacht läuft)"
        status = ttk.Label(frm, text=f"Analysiere mit {model}, {live} … (dauert etwa 10–30 s)", foreground="#1a7f37")
        status.grid(row=0, column=0, sticky="w")
        ana = tk.Text(frm, height=12, wrap="word", font=("Segoe UI", 9), background="#f6f6f6")
        ana.grid(row=1, column=0, sticky="nsew", pady=(4, 8))
        ttk.Label(frm, text="Teil 2 – Vorschlag (bearbeitbar, „Übernehmen“ ersetzt den Text im Feld):").grid(row=2, column=0, sticky="w")
        sug = tk.Text(frm, height=12, wrap="word", undo=True, font=("Segoe UI", 9))
        sug.grid(row=3, column=0, sticky="nsew", pady=4)
        bar = ttk.Frame(frm)
        bar.grid(row=4, column=0, sticky="e")

        def apply():
            new = sug.get("1.0", "end").strip()
            if not new:
                return
            widget.delete("1.0", "end")
            widget.insert("1.0", new)
            # Grundlage fuer das Diff-Management: diese Fassung gilt ab jetzt als geprueft
            self.s.setdefault("review_baselines", {})[kind] = [l.rstrip() for l in new.splitlines()]
            self._save_now()
            self._append(f"{title}: Vorschlag übernommen (wirkt ab dem nächsten Plan).\n")
            win.destroy()

        def again_full():
            win.destroy()
            self._review(kind, widget, title, full=True)

        take = ttk.Button(bar, text="Übernehmen", command=apply, state="disabled")
        take.pack(side="left", padx=4)
        ttk.Button(bar, text="Alles neu prüfen", command=again_full).pack(side="left", padx=4)
        ttk.Button(bar, text="Schließen", command=win.destroy).pack(side="left")
        ana.insert("1.0", "Teil 1 – Analyse folgt …")
        ana.config(state="disabled")

        def done(res, err):
            if not win.winfo_exists():
                return
            ana.config(state="normal")
            ana.delete("1.0", "end")
            if err:
                status.config(text="Prüfung fehlgeschlagen", foreground="#b00020")
                ana.insert("1.0", err)
            else:
                scope = ("alle Zeilen geprüft" if full or baseline is None else
                         f"{res.protected} Zeilen geschützt (unverändert seit der letzten Überarbeitung), "
                         f"{res.open} neue/geänderte geprüft")
                status.config(text=f"Fertig ({model}, {live}) – {scope}.", foreground="#1a7f37")
                diff = []
                for old, new in res.changes:
                    if not old:
                        diff.append(f"+ {new}")
                    elif not new:
                        diff.append(f"− entfernt: {old}")
                    else:
                        diff.append(f"~ {old}\n  → {new}")
                ana.insert("1.0", "Teil 1 – Analyse\n\n" + res.analysis.strip()
                           + "\n\nÄnderungen gegenüber deinem Text:\n"
                           + ("\n".join(diff) if diff else "(keine – die geprüften Zeilen sind gut so)"))
                sug.insert("1.0", res.merged_text)
                take.config(state="normal")
            ana.config(state="disabled")

        def run():
            try:
                from empire_general.prompt_eval import review_lines
                res = review_lines(kind, text, baseline, api_key=key, model=model, situation=situation)
                self.calls.put(lambda: done(res, None))
            except Exception as e:  # Anzeige im Dialog
                msg = str(e).replace(key, "<Schlüssel>")
                self.calls.put(lambda: done(None, msg))

        threading.Thread(target=run, daemon=True).start()

    def _situation(self, kind: str) -> str:
        """Lagebild aus runtime/roster.json, wenn der Feldherr laeuft und die Liste frisch ist. Befehle/Aufstellung
        fuer den Spieler -> Armee des Spielers, Befehle fuer den Gegner -> gegnerische Armee, Strategie -> beide."""
        if not (self.proc and self.proc.poll() is None):
            return ""
        path = RUNTIME / "roster.json"
        try:
            if time.time() - path.stat().st_mtime > 15:
                return ""
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return ""
        sit = data.get("situation") or {}
        player = str(data.get("player_alliance") or 1)
        others = [a for a in sit if a != player]
        keys = [player] if kind in ("orders_player", "deploy") else (others if kind == "orders_enemy" else [player] + others)
        return "\n\n".join(sit[a] for a in keys if sit.get(a))

    # ---------- Sichern ----------
    def _schedule_save(self) -> None:
        if self._loading:
            return
        if self._save_job:
            self.after_cancel(self._save_job)
        self._save_job = self.after(600, self._save_now)

    def _collect(self) -> dict:
        name = self.strat_var.get()
        text = self.strat_text.get("1.0", "end").rstrip() + "\n"
        drafts = dict(self.s.get("strategy_drafts") or {})
        if name:
            if text.strip() != self._strategy_file_text(name).strip():
                drafts[name] = text
            else:
                drafts.pop(name, None)
        try:
            interval, cint = int(self.int_var.get()), int(self.cint_var.get())
        except (tk.TclError, ValueError):
            interval, cint = self.s.get("interval", 10), self.s.get("close_interval", 5)
        self.s.update({"model": self.model_var.get(), "effort": self.effort_var.get(), "interval": interval,
                       "close_interval": cint, "strategy": name, "strategy_drafts": drafts,
                       "control": self.control_var.get(),
                       "orders_player": self.orders_player_text.get("1.0", "end").strip(),
                       "orders_enemy": self.orders_enemy_text.get("1.0", "end").strip(),
                       "deploy_prompt": self.deploy_text.get("1.0", "end").strip(),
                       "hotkeys": {c: v.get().strip() for c, v in self.hotkey_vars.items()},
                       "own_groups": self.own_groups_var.get(),
                       "geometry": self.geometry()})
        return self.s

    def _save_now(self) -> None:
        self._save_job = None
        try:
            settings_save(self._collect())
            self._write_runtime()
        except Exception as e:
            self._append(f"Einstellungen konnten nicht gespeichert werden: {e}\n")
        self._update_draft_label()

    def _write_runtime(self) -> None:
        """Dateien, die der laufende Feldherr vor jedem Plan (bzw. jeder Aufstellung) neu liest - bei jeder Aenderung
        geschrieben, damit sie sofort wirken."""
        RUNTIME.mkdir(exist_ok=True)
        for name, widget in (("deploy_prompt.txt", self.deploy_text), ("orders_player.txt", self.orders_player_text),
                             ("orders_enemy.txt", self.orders_enemy_text), ("strategy.md", self.strat_text)):
            (RUNTIME / name).write_text(widget.get("1.0", "end").strip() + "\n", encoding="utf-8")

    # ---------- Start/Stopp ----------
    def _start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        self._save_now()
        s = self.s
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
        # Laufzeitdateien (Strategie, Befehle, Aufstellungsprompt) liest der Feldherr vor jedem Plan neu
        cmd = [sys.executable, str(HERE / "commander.py"), "--backend", backend, "--effort", s["effort"],
               "--interval", str(s["interval"]), "--close-interval", str(s["close_interval"]),
               "--strategy", str(RUNTIME / "strategy.md"),
               "--orders-player", str(RUNTIME / "orders_player.txt"), "--orders-enemy", str(RUNTIME / "orders_enemy.txt"),
               "--deploy-prompt", str(RUNTIME / "deploy_prompt.txt"), "--roster", str(RUNTIME / "roster.json"),
               "--hotkeys", ",".join(f"{c}={k}" for c, k in s["hotkeys"].items() if k)]
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
        for b in self.action_btns:
            b.config(state="normal")
        self.run_status.config(text="läuft – wartet auf eine Schlacht", foreground="#1a7f37")

    def _action(self, cmd: str) -> None:
        """Wie die Hotkeys: Befehl an den laufenden Feldherrn ueber <Spiel>/ai_bridge/commands.txt."""
        self._save_now()   # Aufstellungsprompt ist damit aktuell
        try:
            with (EmpireBridge().dir / "commands.txt").open("a", encoding="utf-8") as f:
                f.write(cmd.replace("\n", " ").replace("\r", " ") + "\n")
        except (Exception, SystemExit) as e:
            messagebox.showerror("Befehl", str(e))

    def _running(self) -> bool:
        if self.proc and self.proc.poll() is None:
            return True
        messagebox.showinfo("Feldherr", "Der Feldherr läuft nicht – erst „▶ Starten“.")
        return False

    # ---------- Chat ----------
    def _send_order(self) -> None:
        text = self.chat_var.get().strip()
        if not text or not self._running():
            return
        target = "enemy" if self.chat_target.get() == "Gegner" else "player"
        self._action(f"order {target} {text}")
        self.chat_var.set("")

    def _clear_orders(self) -> None:
        if self._running():
            self._action(f"order_clear {'enemy' if self.chat_target.get() == 'Gegner' else 'player'}")

    # ---------- Einheiten ----------
    def _give(self, cmd: str) -> None:
        sel = self.units_tree.selection()
        if not sel:
            messagebox.showinfo("Einheiten", "Bitte zuerst eine oder mehrere Einheiten in der Liste markieren.")
            return
        if self._running():
            self._action(f"{cmd} {','.join(sel)}")

    def _refresh_units(self) -> None:
        """Liste aus runtime/roster.json (schreibt der Feldherr jede Sekunde); Auswahl bleibt erhalten."""
        try:
            data = json.loads((RUNTIME / "roster.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        player = data.get("player_alliance") or 1
        want_player = self.units_side.get() == "player"
        rows = [u for u in data.get("units", []) if (u["alliance"] == player) == want_player]
        tree = self.units_tree
        keep = {u["bid"] for u in rows}
        for iid in tree.get_children():
            if iid not in keep:
                tree.delete(iid)
        for i, u in enumerate(rows):
            vals = (u["name"], u["type"], f"{u['men']}/{u['men_start']}", u.get("placement", ""), u["lead"], u.get("status", ""))
            if tree.exists(u["bid"]):
                tree.item(u["bid"], values=vals)
                tree.move(u["bid"], "", i)
            else:
                tree.insert("", i, iid=u["bid"], values=vals)
        ai = sum(1 for u in rows if u["lead"] == "KI")
        self.units_info.config(text=f"Spielzeit {data.get('time', 0):.0f} s, Phase {data.get('phase', '?')}: "
                                    f"{ai} von {len(rows)} Einheiten bei der KI")
        orders = data.get("orders") or {}
        lines = []
        for a, lst in orders.items():
            if lst:
                who = "meine Armee" if int(a) == player else "Gegner"
                lines.append(f"Aktive Befehle an {who}: " + " | ".join(lst))
        self.active_orders.config(text="\n".join(lines))

    def _reader(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            self.q.put(line)
        self.q.put(f"=== Feldherr beendet (Code {proc.wait()}) ===\n")
        self.q.put("\x00stopped")

    def _stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _pump(self) -> None:
        self._pump_n = getattr(self, "_pump_n", 0) + 1
        if self._pump_n % 7 == 0:   # etwa jede Sekunde: Einheitenliste und aktive Befehle aktualisieren
            try:
                self._refresh_units()
            except tk.TclError:
                pass
        while not self.calls.empty():
            try:
                self.calls.get_nowait()()
            except Exception as e:  # ein fehlerhafter Rueckruf darf das Steuerpult nicht anhalten
                self._append(f"Interner Fehler: {e}\n")
        try:
            while True:
                line = self.q.get_nowait()
                if line == "\x00stopped":
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    for b in self.action_btns:
                        b.config(state="disabled")
                    self.run_status.config(text="gestoppt", foreground="#555")
                    continue
                if "Neue Schlacht" in line or "Uebernehme" in line:
                    self.run_status.config(text="führt eine Schlacht", foreground="#1a7f37")
                self._append(line)
        except queue.Empty:
            pass
        self._pump_job = self.after(150, self._pump)

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
        self._save_now()
        self.destroy()

    def destroy(self) -> None:
        for job in (getattr(self, '_pump_job', None), self._save_job):
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        super().destroy()


if __name__ == "__main__":
    App().mainloop()
