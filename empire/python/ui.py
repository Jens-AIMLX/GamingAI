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
import overlay_panel  # noqa: E402
from empire_campaign import report as campaign_report  # noqa: E402
from empire_campaign import views as campaign_views  # noqa: E402

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

IV_MIN, IV_MAX = 5, 120              # Planungsintervall je Seite (s Spielzeit)
CF_MIN, CF_MAX = 0.25, 1.0          # Faktor fuers Intervall im Nahkampf
DEFAULTS = {"model": next(iter(MODELS)), "effort": "low", "interval": 10, "close_interval": 5,
            "interval_player": 10, "interval_enemy": 10, "close_factor": 0.5,
            "strategy": "standard", "strategy_drafts": {}, "orders_player": "", "orders_enemy": "", "deploy_prompt": "",
            "hotkeys": dict(DEFAULT_HOTKEYS), "own_groups": False, "review_baselines": {},
            "gun_zone_on": True, "gun_zone_m": 30,
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
        loaded = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return json.loads(json.dumps(DEFAULTS))
    if "interval_player" not in loaded:      # frueher ein Intervall fuer beide + eines fuer den Nahkampf
        iv = int(loaded.get("interval") or 10)
        loaded["interval_player"] = loaded["interval_enemy"] = min(max(iv, IV_MIN), IV_MAX)
        cf = float(loaded.get("close_interval") or iv / 2) / max(iv, 1)
        loaded["close_factor"] = round(min(max(cf, CF_MIN), CF_MAX) * 20) / 20
    s = {**DEFAULTS, **loaded}
    # frueheres Feld "Zusatzbefehle" (galt fuer beide Seiten) -> Befehle meiner Armee
    old = (s.pop("instructions", "") or "").strip()
    if old and not (s.get("orders_player") or "").strip():
        s["orders_player"] = old
    return s


def settings_save(s: dict) -> None:
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, SETTINGS)


def read_config() -> tuple[str, bool, bool]:
    """(control, own_groups, overlay) aus <Spiel>/ai_bridge/config.lua."""
    try:
        text = (EmpireBridge().dir / "config.lua").read_text(encoding="utf-8")
        m = re.search(r'control\s*=\s*"(\w+)"', text)
        g = re.search(r'own_groups\s*=\s*(true|false)', text)
        o = re.search(r'overlay\s*=\s*(true|false)', text)
        return (m.group(1) if m else "enemy", bool(g and g.group(1) == "true"), not (o and o.group(1) == "false"))
    except (FileNotFoundError, SystemExit):
        return ("enemy", False, True)



CAMP_EMPTY = ("Noch kein Lagebericht dieser Sitzung - er entsteht zu Beginn jedes eigenen Zugs und nach dem ersten "
              "Klick auf der Kampagnenkarte.")


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
        # Overlay im Spielbild: kompakte Ansicht DIESER Daten (overlay_panel.py), F8 im Spiel schaltet sie
        self._ov_view = 1               # 0 = zugeklappt (Meldungsleiste), 1..n = Tab overlay_panel.TABS[view - 1]
        self._ov_last_view = 1          # Tab, den F8 beim Aufklappen wieder zeigt
        self._ov_mode: str | None = None   # "battle" / "campaign" (automatisch erkannt)
        self._ov_f8: int | None = None  # zuletzt gesehene F8-Zaehlung der DLL
        self._ov_seq = 0
        self._ov_open_written = False
        self._ov_last = 0.0
        self._ov_key = None
        self._ov_dir: Path | None = None
        self._ov_hots: list = []                  # klickbare Bereiche des zuletzt gezeichneten Bilds
        self._ov_menu = False                     # Menue (Einstellungen) offen
        self._ov_scroll: dict = {}                # Feld -> erste sichtbare Zeile (log: None = folgt dem Ende)
        self._ov_pressed: tuple | None = None     # (Aktion, Zeit) fuer die Klick-Rueckmeldung
        self._ov_drag = None                      # Scrollbalken, der gerade gezogen wird
        self._ov_input = None                     # overlay_panel.InputReader
        self._ov_focus: str | None = None         # Feld mit Tastaturfokus im Overlay (Stufe C)
        self._ov_caret = 0                        # Cursor darin (Zeichen ab Anfang)
        self._ov_review: dict | None = None       # offene Pruefung (dasselbe Fenster wie im Steuerpult)
        self._ov_typed = False                    # Eingabe seit dem letzten Bild -> sofort neu zeichnen
        self._ov_hook = None                      # overlay_kbd.KeyboardHook (Tasten vor Steam/Spiel abfangen)
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self._overlay_follow_tab())
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
        control, own_groups, overlay = read_config()
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
        self.overlay_var = tk.BooleanVar(value=overlay)
        ttk.Checkbutton(f, text="Overlay im Spiel (Feld unten links im Spielbild, auch im Vollbild; wirkt nach etwa 2 s)",
                        variable=self.overlay_var, command=self._write_control).grid(
            row=4, column=0, columnspan=7, sticky="w", pady=(2, 0))
        # Sicherheitsabstand um die eigene Artillerie: feste Regel im Feldherrn (wirkt sofort, auch in der Aufstellung)
        zf = ttk.Frame(f)
        zf.grid(row=5, column=0, columnspan=7, sticky="w", pady=(2, 0))
        self.zone_on_var = tk.BooleanVar(value=bool(self.s.get("gun_zone_on", True)))
        self.zone_m_var = tk.IntVar(value=int(self.s.get("gun_zone_m", 30)))
        ttk.Checkbutton(zf, text="Sicherheitsabstand um die eigene Artillerie:", variable=self.zone_on_var,
                        command=self._zone_changed).pack(side="left")
        ttk.Spinbox(zf, from_=10, to=100, increment=5, width=5, textvariable=self.zone_m_var,
                    command=self._zone_changed).pack(side="left", padx=4)
        ttk.Label(zf, text="m (keine Einheit n\u00e4her an einem eigenen Geschütz; wirkt sofort)",
                  foreground="#777").pack(side="left")

        ttk.Label(f, text="Feldherr:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.model_var = tk.StringVar(value=self.s["model"] if self.s["model"] in MODELS else DEFAULTS["model"])
        ttk.Combobox(f, textvariable=self.model_var, values=list(MODELS), state="readonly", width=44).grid(
            row=1, column=1, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(f, text="Denktiefe:").grid(row=1, column=4, sticky="e", padx=(12, 2), pady=(6, 0))
        self.effort_var = tk.StringVar(value=self.s["effort"] if self.s["effort"] in EFFORTS else "low")
        ttk.Combobox(f, textvariable=self.effort_var, values=EFFORTS, state="readonly", width=8).grid(
            row=1, column=5, sticky="w", pady=(6, 0))

        ttk.Label(f, text="Neuer Plan frühestens alle").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.ipl_var = tk.IntVar(value=int(self.s["interval_player"]))
        self.ien_var = tk.IntVar(value=int(self.s["interval_enemy"]))
        self.cf_var = tk.DoubleVar(value=float(self.s["close_factor"]))
        tb = ttk.Frame(f)
        tb.grid(row=2, column=1, columnspan=6, sticky="w", pady=(6, 0))
        self.iv_labels = {}
        for text, var in (("meine Armee", self.ipl_var), ("Gegner", self.ien_var)):
            ttk.Label(tb, text=text).pack(side="left", padx=(0, 2))
            ttk.Scale(tb, from_=IV_MIN, to=IV_MAX, variable=var, length=120,
                      command=lambda v, var=var: self._iv_snap(var)).pack(side="left")
            self.iv_labels[str(var)] = ttk.Label(tb, text="", width=6)
            self.iv_labels[str(var)].pack(side="left", padx=(2, 8))
        ttk.Label(tb, text="Spielzeit;  im Nahkampf \u00d7").pack(side="left")
        ttk.Scale(tb, from_=CF_MIN, to=CF_MAX, variable=self.cf_var, length=110,
                  command=lambda v: self._cf_snap()).pack(side="left", padx=(2, 2))
        self.cf_label = ttk.Label(tb, text="", width=5)
        self.cf_label.pack(side="left")
        ttk.Label(f, text="(5–120 s, wirkt sofort; geplant wird nur bei Ereignissen wie Verlusten, Annäherung, "
                          "Feindbewegung, erledigten Zügen - sonst spätestens nach dem Dreifachen)",
                  foreground="#777").grid(row=6, column=0, columnspan=7, sticky="w")
        self._cf_snap()
        self._iv_snap(self.ipl_var)
        self._iv_snap(self.ien_var)
        for v in (self.model_var, self.effort_var):
            v.trace_add("write", lambda *a: self._schedule_save())
        for v in (self.ipl_var, self.ien_var):
            v.trace_add("write", lambda *a: self._iv_changed())

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
        self.nb.add(self._build_campaign_tab(), text="  Kampagne  ")
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
        # Protokoll in Tabs: "Alles" und sortenreine Ansichten (Plaene je Allianz, Befehle, Meldungen)
        self.log_nb = ttk.Notebook(f)
        self.log_nb.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
        self.log_views: dict[str, tk.Text] = {}
        for name in overlay_panel.LOG_VIEWS:
            fr_ = ttk.Frame(self.log_nb)
            fr_.columnconfigure(0, weight=1)
            fr_.rowconfigure(0, weight=1)
            t = tk.Text(fr_, height=10, wrap="word", state="disabled", font=("Consolas", 9), background="#fafafa")
            t.grid(row=0, column=0, sticky="nsew")
            sb = ttk.Scrollbar(fr_, command=t.yview)
            sb.grid(row=0, column=1, sticky="ns")
            t["yscrollcommand"] = sb.set
            self.log_nb.add(fr_, text=f"  {name}  ")
            self.log_views[name] = t
        self.log = self.log_views["Alles"]

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

    # ---------- Planungsintervall je Seite ----------
    def _iv(self, var) -> int:
        try:
            return min(max(int(var.get()), IV_MIN), IV_MAX)
        except (tk.TclError, ValueError):
            return 10

    def _cf(self) -> float:
        try:
            return round(min(max(float(self.cf_var.get()), CF_MIN), CF_MAX) * 20) / 20
        except (tk.TclError, ValueError):
            return 0.5

    def _iv_snap(self, var) -> None:
        """Intervall-Schieber in 5-s-Schritten; Anzeige aktualisieren (Speichern ueber den Variablen-Trace)."""
        try:
            raw = float(self.getvar(str(var)))              # Rohwert (der Regler liefert Kommazahlen)
        except (tk.TclError, ValueError):
            raw = 10.0
        v = int(min(max(round(raw / 5) * 5, IV_MIN), IV_MAX))
        if abs(raw - v) > 1e-9:
            var.set(v)
        lbl = getattr(self, "iv_labels", {}).get(str(var))
        if lbl is not None:
            lbl.config(text=f"{v} s")

    def _cf_snap(self) -> None:
        """Schieber in 0,05er-Schritten; Anzeige und Feldherr (options.json) sofort aktualisieren."""
        cf = self._cf()
        if abs(float(self.cf_var.get()) - cf) > 1e-9:
            self.cf_var.set(cf)
        self.cf_label.config(text=f"{cf:.2f}".replace(".", ","))
        self._iv_changed()

    def _iv_changed(self) -> None:
        if getattr(self, "_loading", False) or not hasattr(self, "zone_on_var"):
            return
        try:
            self._write_options()
        except OSError:
            pass
        self._schedule_save()

    # ---------- Modus ----------
    def _ctl_sides(self) -> tuple[bool, bool]:
        c = self.control_var.get()
        return c in ("player", "both"), c in ("enemy", "both")

    def _toggle_side(self, which: str) -> None:
        """Overlay-Schalter: eigene Armee bzw. Gegner von der KI fuehren lassen ein/aus (wie 'KI fuehrt' oben)."""
        own, foe = self._ctl_sides()
        if which == "player":
            own = not own
        else:
            foe = not foe
        self.control_var.set("both" if own and foe else "player" if own else "enemy" if foe else "none")
        self._write_control()

    def _write_control(self) -> None:
        try:
            EmpireBridge().write_config(control=self.control_var.get(), own_groups=self.own_groups_var.get(),
                                        overlay=self.overlay_var.get())
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
        # dieselbe Pruefung im Overlay (Anzeige, Vorschlag bearbeiten, Knoepfe) - verschwindet mit dem Fenster
        self._ov_review = {"win": win, "title": title, "status": status, "ana": ana, "sug": sug, "take": take,
                           "apply": apply, "again_full": again_full}

        def closed(e):
            if e.widget is win and self._ov_review and self._ov_review["win"] is win:
                self._ov_review = None
                if self._ov_focus == "rev_sug":
                    self._ov_focus = None
        win.bind("<Destroy>", closed, add="+")

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
        ipl, ien, cf = self._iv(self.ipl_var), self._iv(self.ien_var), self._cf()
        self.s.update({"model": self.model_var.get(), "effort": self.effort_var.get(),
                       "interval_player": ipl, "interval_enemy": ien, "close_factor": cf,
                       "interval": ipl, "close_interval": max(1, round(ipl * cf)),
                       "strategy": name, "strategy_drafts": drafts,
                       "control": self.control_var.get(),
                       "orders_player": self.orders_player_text.get("1.0", "end").strip(),
                       "orders_enemy": self.orders_enemy_text.get("1.0", "end").strip(),
                       "deploy_prompt": self.deploy_text.get("1.0", "end").strip(),
                       "hotkeys": {c: v.get().strip() for c, v in self.hotkey_vars.items()},
                       "own_groups": self.own_groups_var.get(),
                       "gun_zone_on": self.zone_on_var.get(), "gun_zone_m": self._zone_m(),
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
        self._write_options()

    def _zone_m(self) -> int:
        try:
            return max(10, min(100, int(self.zone_m_var.get())))
        except (tk.TclError, ValueError):
            return 30

    def _write_options(self) -> None:
        """runtime/options.json: Einstellungen, die der laufende Feldherr jede Sekunde neu liest."""
        opts = {"gun_zone_m": self._zone_m() if self.zone_on_var.get() else 0,
                "interval_player": self._iv(self.ipl_var), "interval_enemy": self._iv(self.ien_var),
                "close_factor": self._cf()}
        tmp = RUNTIME / "options.json.tmp"
        tmp.write_text(json.dumps(opts), encoding="utf-8")
        os.replace(tmp, RUNTIME / "options.json")

    def _zone_changed(self) -> None:
        self._write_options()
        self._schedule_save()

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

    # ---------- Overlay im Spielbild ----------
    def _overlay_dir(self) -> Path | None:
        if self._ov_dir is None:
            try:
                self._ov_dir = EmpireBridge().dir
            except (Exception, SystemExit):
                return None
        return self._ov_dir

    def _overlay_follow_tab(self) -> None:
        """Tab-Wechsel im Fenster: das offene Overlay zeigt denselben Tab (Befehle .. Kampagne)."""
        if self._ov_view:
            try:
                idx = self.nb.index(self.nb.select())
                if idx + 2 <= len(overlay_panel.TABS):
                    self._ov_view = idx + 2
            except tk.TclError:
                pass

    # ---------- Kampagne (Stufe 1: Bestandsbericht + Empfehlungen nach festen Regeln) ----------
    def _build_campaign_tab(self) -> ttk.Frame:
        """Kampagne als Liste (wie die Einheitenliste): Kopfzeile, Ansichtswahl, Liste mit Farben nach Dringlichkeit."""
        tab = ttk.Frame(self.nb, padding=6)
        tab.columnconfigure(0, weight=1)
        self.camp_header = ttk.Label(tab, text=CAMP_EMPTY, foreground="#777")
        self.camp_header.grid(row=0, column=0, columnspan=2, sticky="w")
        bar = ttk.Frame(tab)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        view = self.s.get("campaign_view")
        self.camp_view_var = tk.StringVar(value=view if view in campaign_views.VIEWS else campaign_views.VIEWS[0])
        for name in campaign_views.VIEWS:
            ttk.Radiobutton(bar, text=name, value=name, variable=self.camp_view_var,
                            command=self._campaign_view_changed).pack(side="left", padx=(0, 8))
        ttk.Button(bar, text="Aktualisieren", command=self._campaign_request).pack(side="right")
        self.camp_fog_var = tk.BooleanVar(value=bool(self.s.get("campaign_fog", False)))
        ttk.Checkbutton(bar, text="Nebel einbeziehen (Spezialschalter)", variable=self.camp_fog_var,
                        command=self._campaign_fog_changed).pack(side="right", padx=12)
        # Filter und Sortierung (gelten auch fuer das Overlay): Suchtext ueber alle Spalten, "nur wichtige",
        # Sortierung per Klick auf die Spaltenueberschrift (zweiter Klick kehrt um, dritter hebt auf)
        fbar = ttk.Frame(tab)
        fbar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Label(fbar, text="Filter:").pack(side="left")
        self.camp_filter_var = tk.StringVar(value="")
        ttk.Entry(fbar, textvariable=self.camp_filter_var, width=28).pack(side="left", padx=(4, 12))
        self.camp_filter_var.trace_add("write", lambda *_: self._campaign_show())
        self.camp_important_var = tk.BooleanVar(value=bool(self.s.get("campaign_important", False)))
        ttk.Checkbutton(fbar, text="nur dringende und wichtige", variable=self.camp_important_var,
                        command=self._campaign_important_changed).pack(side="left")
        self._camp_sort: dict[str, tuple[int, bool]] = {}
        self.camp_tree = ttk.Treeview(tab, show="headings", height=9, selectmode="extended")
        self.camp_tree.grid(row=3, column=0, sticky="nsew", pady=(6, 0))
        sb = ttk.Scrollbar(tab, command=self.camp_tree.yview)
        sb.grid(row=3, column=1, sticky="ns", pady=(6, 0))
        self.camp_tree["yscrollcommand"] = sb.set
        self.camp_tree.bind("<<TreeviewSelect>>", self._campaign_select)
        self.camp_tree.tag_configure("urgent", foreground="#b00020")
        self.camp_tree.tag_configure("warn", foreground="#9a6700")
        self.camp_tree.tag_configure("muted", foreground="#888888")
        self._camp_mtime = None
        self._camp_since = time.time()          # Berichte von vor dem Start des Steuerpults sind alt: nie anzeigen
        self._camp_views: dict = {}
        self._camp_advice = (None, [])          # Meldungsleiste: "noch kein Lagebericht" statt alter Empfehlungen
        self._camp_header_text = ""
        self._camp_cols = None
        self._camp_turn = None
        self._camp_feed_key = None
        return tab

    def _campaign_request(self) -> None:
        """Knopf "Aktualisieren": das Spiel erfasst beim naechsten Takt (1 s) alles neu, auch Regionen und Bauten."""
        d = self._overlay_dir()
        if d is None:
            return
        try:
            (d / "campaign_request.txt").write_text("1", encoding="ascii")
            self._overlay_note("Kampagne: Aktualisierung angefordert (dauert 1-3 s, nur im eigenen Zug)")
        except OSError as e:
            self._overlay_note(f"Kampagne: Anforderung nicht moeglich: {e}")

    def _campaign_select(self, _event=None) -> None:
        """Zeilenklick: Kamera im Spiel auf Armee/Flotte/Agent/Feind, Region oder Bauplatz (beim naechsten Takt, 1 s)."""
        sel = self.camp_tree.selection()
        if len(sel) != 1:
            return
        v = self._camp_views.get(self.camp_view_var.get())
        target = v.targets.get(sel[0]) if v else None
        d = self._overlay_dir()
        if not target or d is None or sel[0] == getattr(self, "_camp_zoomed", None):
            return
        self._camp_zoomed = sel[0]
        try:
            (d / "campaign_zoom.txt").write_text(target + "\n", encoding="utf-8")
        except OSError:
            pass

    def _campaign_view_changed(self) -> None:
        self.s["campaign_view"] = self.camp_view_var.get()
        self._schedule_save()
        self._ov_scroll["camp"] = 0
        self._campaign_show()

    def _campaign_feed(self, force: bool = False) -> None:
        """Meldungsleiste im Spielbild (Overlay zugeklappt) auf der Kampagnenkarte: die wichtigsten Empfehlungen.
        Nur ausserhalb von Schlachten - dort gehoert die Leiste dem Feldherrn (state.json wird jede Sekunde neu
        geschrieben). Neu geschrieben nur, wenn sich die Empfehlungen aendern."""
        d = self._overlay_dir()
        adv = getattr(self, "_camp_advice", None)
        if d is None or not adv:
            return
        try:
            if time.time() - (d / "state.json").stat().st_mtime < 15:
                self._camp_feed_key = None               # Schlacht laeuft; danach die Leiste neu schreiben
                return
        except OSError:
            pass
        turn, advice = adv
        key = (turn, bool(self.camp_fog_var.get()), tuple(a.line() for a in advice[:6]), len(advice))
        if not force and key == self._camp_feed_key:
            return
        self._camp_feed_key = key
        if turn is None:                                 # kein Bericht dieser Sitzung
            lines = ["-Kampagne: noch kein Lagebericht dieser Sitzung (Zugbeginn oder erster Klick auf der Karte)"]
        else:
            import textwrap
            urgent = sum(1 for a in advice if a.prio == 1)
            lines = [f"-Kampagne Zug {turn}: {urgent} dringend, {len(advice) - urgent} weitere Empfehlungen (F8)"]
            for a in advice[:5]:
                kind = {1: "!", 2: "*"}.get(a.prio, "-")
                parts = textwrap.wrap(f"{a.area}: {a.text}", 74)[:2] or [""]
                lines.append(kind + parts[0])
                lines += [kind + " " + q for q in parts[1:]]
        tmp = d / "overlay.txt.tmp"
        try:
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(tmp, d / "overlay.txt")
        except OSError:
            pass

    def _campaign_fog_changed(self) -> None:
        self.s["campaign_fog"] = bool(self.camp_fog_var.get())
        self._schedule_save()
        self._append("Kampagne: Feinde im Nebel des Krieges " + ("EINBEZOGEN" if self.camp_fog_var.get()
                                                                   else "ausgeblendet (nur sichtbare)") + "\n")
        self._campaign_refresh(force=True)

    def _campaign_state_path(self) -> Path | None:
        d = self._overlay_dir()
        return d / "campaign_state.json" if d is not None else None

    def _campaign_refresh(self, force: bool = False) -> None:
        """Neuer Lagebericht vom Spiel (oder Schalter geaendert): Ansichten neu berechnen, Liste an Ort und Stelle
        aktualisieren. Ins Protokoll nur bei einem neuen Zug."""
        path = self._campaign_state_path()
        if path is None:
            return
        try:
            mt = path.stat().st_mtime
        except OSError:
            mt = None                           # vom Spiel beim Laden verworfen
        if mt is not None and mt < self._camp_since:
            mt = None                           # aus einer frueheren Sitzung
        if mt is None:
            self._campaign_clear()
            return
        if not force and mt == self._camp_mtime:
            return
        st = campaign_report.load_state(path)
        if st is None:
            return
        self._camp_mtime = mt
        fog = bool(self.camp_fog_var.get())
        self._camp_views = campaign_views.build(st, fog)
        self._camp_header_text = campaign_views.header(st)
        self.camp_header.config(text=self._camp_header_text, foreground="#1a7f37")
        m = st.get("meta") or {}
        advice = campaign_report.recommendations(st, fog)
        self._camp_advice = (m.get("turn"), advice)
        self._campaign_feed()
        self._campaign_show()
        if m.get("turn") != self._camp_turn:
            self._camp_turn = m.get("turn")
            urgent = sum(1 for a in advice if a.prio == 1)
            self._append(f"Kampagne: Zug {m.get('turn')} - {urgent} dringende von {len(advice)} Empfehlungen "
                         "(Tab „Kampagne“)\n")

    def _campaign_clear(self) -> None:
        """Kein aktueller Bericht: Liste, Kopfzeile und Empfehlungen leeren (Steuerpult und Overlay)."""
        if self._camp_mtime is None and not self._camp_views:
            return
        self._camp_mtime = None
        self._camp_views = {}
        self._camp_header_text = ""
        self._camp_advice = (None, [])
        self._camp_turn = None
        self.camp_tree.delete(*self.camp_tree.get_children())
        self.camp_header.config(text=CAMP_EMPTY, foreground="#777")
        self._campaign_feed()

    @staticmethod
    def _camp_sortkey(text) -> tuple:
        """Zahlen als Zahlen sortieren ("12.345", "55 %", "-3 / 2"), sonst Text."""
        t = str(text).strip()
        m = re.search(r"-?\d+(?:,\d+)?", t.replace(".", ""))
        if m:
            return (0, float(m.group(0).replace(",", ".")), t.lower())
        return (1, 0.0, t.lower())

    def _camp_rows(self, v) -> list:
        """Zeilen einer Ansicht nach Filter (Text, nur wichtige) und Sortierung - fuer Steuerpult und Overlay gleich."""
        rows = list(v.rows)
        q = self.camp_filter_var.get().strip().lower()
        if q:
            rows = [r for r in rows if q in " ".join(str(c) for c in r[1]).lower()]
        if self.camp_important_var.get():
            rows = [r for r in rows if v.tags.get(r[0]) in ("urgent", "warn")
                    or (self.camp_view_var.get() == "Empfehlungen" and v.tags.get(r[0]) != "muted")]
        srt = self._camp_sort.get(self.camp_view_var.get())
        if srt:
            col, desc = srt
            rows.sort(key=lambda r: self._camp_sortkey(r[1][col] if col < len(r[1]) else ""), reverse=desc)
        return rows

    def _campaign_sort(self, col: int) -> None:
        """Klick auf eine Spaltenueberschrift: aufsteigend -> absteigend -> unsortiert (Reihenfolge des Berichts)."""
        view = self.camp_view_var.get()
        cur = self._camp_sort.get(view)
        if cur is None or cur[0] != col:
            self._camp_sort[view] = (col, False)
        elif not cur[1]:
            self._camp_sort[view] = (col, True)
        else:
            self._camp_sort.pop(view, None)
        self._campaign_show()

    def _campaign_important_changed(self) -> None:
        self.s["campaign_important"] = bool(self.camp_important_var.get())
        self._schedule_save()
        self._campaign_show()

    def _campaign_show(self) -> None:
        """Gewaehlte Ansicht in die Liste: Spalten nur beim Ansichtswechsel neu, Zeilen an Ort und Stelle (Auswahl und
        Bildlauf bleiben)."""
        tree = self.camp_tree
        v = self._camp_views.get(self.camp_view_var.get())
        if v is None:
            return
        cols = tuple(c for c, _ in v.columns)
        if cols != self._camp_cols:
            tree.delete(*tree.get_children())
            ids = [f"c{i}" for i in range(len(cols))]
            tree["columns"] = ids
            for i, (cid, (label, rel)) in enumerate(zip(ids, v.columns)):
                tree.heading(cid, text=label, command=lambda i=i: self._campaign_sort(i))
                tree.column(cid, width=int(rel * 9), anchor="w")
            self._camp_cols = cols
        srt = self._camp_sort.get(self.camp_view_var.get())
        for i, (label, _) in enumerate(v.columns):          # Pfeil an der sortierten Spalte
            arrow = (" ▼" if srt[1] else " ▲") if srt and srt[0] == i else ""
            tree.heading(f"c{i}", text=label + arrow)
        rows = self._camp_rows(v)
        keep = {k for k, _ in rows}
        for iid in tree.get_children():
            if iid not in keep:
                tree.delete(iid)
        for i, (key, cells) in enumerate(rows):
            tag = v.tags.get(key) or ""
            if tree.exists(key):
                if tuple(tree.item(key, "values")) != tuple(cells):
                    tree.item(key, values=cells, tags=(tag,))
                tree.move(key, "", i)
            else:
                tree.insert("", i, iid=key, values=cells, tags=(tag,))

    def _overlay_data(self) -> "overlay_panel.PanelData":
        model = self.model_var.get().split(" (")[0].replace("Claude ", "")
        control = next((label for label, val in SIDES if val == self.control_var.get()), self.control_var.get())
        log = self.log_views[self._log_view()].get("end-200l", "end").splitlines()
        units = [tuple(self.units_tree.item(i, "values")) + (i,) for i in self.units_tree.get_children()]
        return overlay_panel.PanelData(
            tab=overlay_panel.TABS[self._ov_view - 1], running=bool(self.proc and self.proc.poll() is None),
            status=self.run_status.cget("text"), model=model, effort=self.effort_var.get(),
            control=control,
            log=[l for l in log if l.strip()], orders_player=self.orders_player_text.get("1.0", "end-1c"),
            orders_enemy=self.orders_enemy_text.get("1.0", "end-1c"), strategy_name=self.strat_var.get(),
            strategy=self.strat_text.get("1.0", "end-1c"), deploy=self.deploy_text.get("1.0", "end-1c"),
            units_side=self.units_side.get(), units=units, units_info=self.units_info.cget("text"),
            active_orders=self.active_orders.cget("text"), chat_target=self.chat_target.get(),
            chat_text=self.chat_var.get(), hotkeys={k: v.get() for k, v in self.hotkey_vars.items()},
            control_val=self.control_var.get(), own_groups=self.own_groups_var.get(),
            iv_player=self._iv(self.ipl_var), iv_enemy=self._iv(self.ien_var), close_f=self._cf(),
            selected=tuple(self.units_tree.selection()), scroll=dict(self._ov_scroll),
            menu_open=self._ov_menu, log_view=self._log_view(), zone_on=self.zone_on_var.get(), zone_m=self._zone_m(),
            pressed=self._ov_pressed[0] if self._ov_pressed and time.monotonic() - self._ov_pressed[1] < 0.3 else None,
            camp_header=self._camp_header_text, camp_view=self.camp_view_var.get(),
            camp_cols=tuple(self._camp_views[self.camp_view_var.get()].columns)
            if self.camp_view_var.get() in self._camp_views else (),
            camp_rows=tuple((cells, self._camp_views[self.camp_view_var.get()].tags.get(k, ""), k,
                             bool(self._camp_views[self.camp_view_var.get()].targets.get(k)))
                            for k, cells in self._camp_rows(self._camp_views[self.camp_view_var.get()]))
            if self.camp_view_var.get() in self._camp_views else (),
            camp_fog=bool(self.camp_fog_var.get()), camp_sel=tuple(self.camp_tree.selection()),
            camp_sort=self._camp_sort.get(self.camp_view_var.get()), camp_important=bool(self.camp_important_var.get()),
            camp_filter=self.camp_filter_var.get().strip(),
            focus=self._ov_focus, caret=self._ov_caret, review=self._ov_review_data(),
            width=self._ov_geom()[2], height=self._ov_geom()[3])

    def _ov_geom(self) -> list[int]:
        """Lage des Overlays [x, y, Breite, Hoehe] (x/y = -1: Standard unten links), gespeichert in den Einstellungen."""
        g = self.s.get("overlay_geom")
        if not (isinstance(g, list) and len(g) == 4):
            g = [-1, -1, 560, 360]
            self.s["overlay_geom"] = g
        return g

    def _ov_review_data(self) -> dict | None:
        r = self._ov_review
        if not r:
            return None
        try:
            return {"title": r["title"], "status": r["status"].cget("text"), "analysis": r["ana"].get("1.0", "end-1c"),
                    "proposal": r["sug"].get("1.0", "end-1c"), "done": str(r["take"].cget("state")) != "disabled"}
        except tk.TclError:
            return None

    def _overlay_mode(self, d: Path) -> None:
        """Schlacht oder Kampagnenkarte? In Schlachten schreibt das Battle-Skript state.json jede Sekunde. Beim Start
        und bei jedem Wechsel zeigt das Overlay IMMER das volle Interface mit dem passenden Tab (Feldherr bzw.
        Kampagne); nur F8 klappt es zur Meldungsleiste zu."""
        try:
            battle = time.time() - (d / "state.json").stat().st_mtime < 15
        except OSError:
            battle = False
        mode = "battle" if battle else "campaign"
        if mode == self._ov_mode:
            return
        first = self._ov_mode is None
        self._ov_mode = mode
        tab = "Feldherr" if battle else "Kampagne"
        self._ov_view = self._ov_last_view = overlay_panel.TABS.index(tab) + 1
        self._ov_menu = False
        self._ov_focus = None
        if self._ov_view >= 2:                        # Fenster zeigt denselben Tab
            try:
                self.nb.select(self._ov_view - 2)
            except tk.TclError:
                pass
        self._overlay_note(("Start" if first else "Wechsel") + ": " + ("Schlacht erkannt - Overlay zeigt den Feldherrn"
                           if battle else "Kampagnenkarte erkannt - Overlay zeigt die Kampagne"))

    def _overlay_tick(self) -> None:
        """F8-Zaehlung der DLL lesen, bei offenem Overlay das Bild neu zeichnen (bei Aenderung, sonst jede Sekunde -
        so erkennt die DLL, dass das Steuerpult noch lebt)."""
        d = self._overlay_dir()
        if d is None:
            return
        if self._ov_pressed and time.monotonic() - self._ov_pressed[1] > 0.5:
            self._ov_pressed = None             # Klick-Rueckmeldung verfallen lassen
        n = overlay_panel.read_f8(d / "overlay_keys.txt")
        if n is not None:
            if self._ov_f8 is None:
                self._ov_f8 = n                     # alter Stand beim Start: nicht reagieren
            elif n != self._ov_f8:
                restarted = n < self._ov_f8
                presses = n - self._ov_f8 if not restarted else n   # Empire neu gestartet: Zaehler von vorn
                self._ov_f8 = n
                if restarted:
                    self._ov_mode = None                # neues Spiel: wieder mit vollem Interface beginnen
                for _ in range(presses):                # F8 schaltet nur um: volles Interface <-> Meldungsleiste
                    if self._ov_view:
                        self._ov_last_view, self._ov_view = self._ov_view, 0
                    else:
                        self._ov_view = self._ov_last_view or 1
        self._overlay_mode(d)
        expanded = bool(self._ov_view) and self.overlay_var.get()
        path = d / "overlay_panel.bin"
        self._ov_typed = False
        if not expanded or not self._ov_focus_ok():
            self._ov_focus = None               # Feld nicht sichtbar -> Tastatur zurueck ans Spiel
        self._ov_hook_want(bool(expanded and self._ov_focus))
        if not expanded:
            if self._ov_open_written:
                self._ov_seq += 1
                overlay_panel.write_panel(None, path, self._ov_seq, False)
                self._ov_open_written = False
                self._ov_key = None             # beim Aufklappen sofort neu zeichnen (nicht erst nach 1 s)
            return
        data = self._overlay_data()
        key = repr(data)
        now = time.monotonic()
        if key != self._ov_key or now - self._ov_last > 1.0:
            self._ov_key, self._ov_last = key, now
            self._ov_seq += 1
            img, self._ov_hots = overlay_panel.render(data)
            g = self._ov_geom()
            overlay_panel.write_panel(img, path, self._ov_seq, True, keyboard=bool(data.focus), pos=(g[0], g[1]),
                                      hots=self._ov_hots)
            self._ov_open_written = True

    # ---------- Overlay: Maus (Stufe B) ----------
    def _overlay_note(self, text: str) -> None:
        """Hinweis ins Protokoll - erscheint auch im Overlay (Meldefenster waeren im Vollbild unsichtbar)."""
        self._append(f"[Overlay] {text}\n")

    def _overlay_events(self) -> None:
        d = self._overlay_dir()
        if d is None or not self._ov_view:
            return
        if self._ov_input is None:
            self._ov_input = overlay_panel.InputReader(d / "overlay_input.txt")
            return
        for kind, x, y, delta in self._ov_input.read():
            if kind == "down":
                self._ov_down(x, y)
            elif kind == "drag" and self._ov_drag is not None and self._ov_drag.action[0] == "slider":
                self._ov_slider(self._ov_drag, x)
            elif kind == "drag" and self._ov_drag is not None:
                key = self._ov_drag.action[1]
                self._ov_scroll[key] = overlay_panel.scroll_to(self._ov_drag, y)
            elif kind == "up":
                self._ov_drag = None
            elif kind == "moved":                       # im Spiel verschoben (DLL hat es schon umgesetzt)
                g = self._ov_geom()
                g[0], g[1] = x, y
                self._ov_key = None                     # Kopf mit der neuen Position schreiben
                self._schedule_save()
            elif kind == "resize":                      # im Spiel aufgezogen: in der neuen Groesse zeichnen
                g = self._ov_geom()
                g[2] = min(max(x, overlay_panel.MIN_W), overlay_panel.MAX_W)
                g[3] = min(max(y, overlay_panel.MIN_H), overlay_panel.MAX_H)
                self._ov_typed = True
                self._schedule_save()
            elif kind in ("key", "char"):
                self._ov_typed = True
                if self._ov_focus:
                    self._ov_key_in(kind, x, y)
            elif kind == "wheel":
                hot = next((h for h in self._ov_hots if h.action[0] == "scroll" and h.hit(x, y)), None)
                if hot is not None:
                    steps = -int(delta / 120 * 3) or (-3 if delta > 0 else 3)
                    first = hot.first + steps
                    self._ov_scroll[hot.action[1]] = max(0, min(first, max(hot.total - hot.visible, 0)))

    def _ov_down(self, x: int, y: int) -> None:
        hot = next((h for h in self._ov_hots if h.hit(x, y)), None)
        if self._ov_menu and (hot is None or not hot.action[0].startswith("m_")) and hot is not None \
                and hot.action != ("menu",):
            self._ov_menu = False             # Klick neben das Menue schliesst es
            return
        if hot is None:
            self._ov_focus = None
            return
        a = hot.action
        if a[0] == "scroll":
            if hot.bar and x >= hot.bar[0] - 3:                 # auf dem Scrollbalken: springen und ziehen
                self._ov_scroll[a[1]] = overlay_panel.scroll_to(hot, y)
                self._ov_drag = hot
            elif a[1] in overlay_panel.EDITABLE:                # in den Text: Fokus und Cursor an die Klickstelle
                self._ov_focus = a[1]
                off = hot.offset_at(x, y)
                self._ov_caret = len(self._ov_get(a[1])) if off is None else off
            else:
                self._ov_focus = None
            return
        if a[0] == "slider":                                    # Schieber: springen und ziehen
            self._ov_slider(hot, x)
            self._ov_drag = hot
            self._ov_focus = None
            return
        if a[0] == "chat_focus":
            self._ov_focus = "chat"
            self._ov_caret = len(self.chat_var.get())
            return
        if a[0] not in ("send", "chat_target"):
            self._ov_focus = None                               # anderer Klick: Tastatur zurueck ans Spiel
        self._ov_pressed = (a, time.monotonic())
        try:
            self._ov_act(a)
        except Exception as e:                                  # Aktion darf das Steuerpult nicht anhalten
            self._overlay_note(f"Aktion {a[0]} fehlgeschlagen: {e}")

    def _ov_slider(self, hot, x: int) -> None:
        """Schieber im Overlay: Intervall je Seite (5..120 s, 5er Schritte) oder Nahkampf-Faktor (0,25..1, 0,05)."""
        frac = min(max((x - hot.bar[0]) / max(hot.bar[2] - hot.bar[0], 1), 0.0), 1.0)
        key = hot.action[1]
        if key in ("iv_player", "iv_enemy"):
            var = self.ipl_var if key == "iv_player" else self.ien_var
            v = int(round((IV_MIN + frac * (IV_MAX - IV_MIN)) / 5) * 5)
            if self._iv(var) != v:
                var.set(v)
            self._iv_snap(var)
            return
        self.cf_var.set(round((CF_MIN + frac * (CF_MAX - CF_MIN)) * 20) / 20)
        self._cf_snap()

    def _ov_act(self, a: tuple) -> None:
        running = bool(self.proc and self.proc.poll() is None)
        k = a[0]
        if k == "tab":
            self._ov_view = overlay_panel.TABS.index(a[1]) + 1
            if self._ov_view >= 2:                              # Fenster zeigt denselben Tab (Befehle..Einheiten)
                self.nb.select(self._ov_view - 2)
        elif k == "menu":
            self._ov_menu = not self._ov_menu
        elif k == "start":
            if MODELS[self.s["model"]][0] == "claude" and not key_load() and not os.environ.get("ANTHROPIC_API_KEY"):
                self._overlay_note("Kein API-Schl\u00fcssel gespeichert - bitte im Steuerpult-Fenster eintragen.")
            else:
                self._start()
        elif k == "stop":
            self._stop()
        elif k == "action" and running:
            self._action(a[1])
        elif k == "clear_orders" and running:
            self._clear_orders()
        elif k == "review":
            widgets = {"orders_player": (self.orders_player_text, "Befehle meine Armee"),
                       "orders_enemy": (self.orders_enemy_text, "Befehle Gegner"),
                       "strategy": (self.strat_text, "Strategie"), "deploy": (self.deploy_text, "Aufstellungsprompt")}
            widget, title = widgets[a[1]]
            if not widget.get("1.0", "end").strip():
                self._overlay_note(f"{title}: das Feld ist leer.")
            elif not (key_load() or os.environ.get("ANTHROPIC_API_KEY")):
                self._overlay_note("F\u00fcr die Pr\u00fcfung wird ein API-Schl\u00fcssel gebraucht.")
            else:
                self._review(a[1], widget, title)
        elif k in ("rev_apply", "rev_again", "rev_close") and self._ov_review:
            r = self._ov_review
            if k == "rev_apply":
                if str(r["take"].cget("state")) != "disabled":
                    r["apply"]()
            elif k == "rev_again":
                r["again_full"]()
            else:
                r["win"].destroy()
        elif k == "units_side":
            self.units_side.set(a[1])
            self._refresh_units()
            self._ov_scroll["units"] = 0
        elif k == "unit":
            tree = self.units_tree
            if a[1] in tree.selection():
                tree.selection_remove(a[1])
            elif tree.exists(a[1]):
                tree.selection_add(a[1])
        elif k == "give" and running:
            if not self.units_tree.selection():
                self._overlay_note("Bitte zuerst Einheiten in der Liste markieren.")
            else:
                self._give(a[1])
        elif k == "chat_target":
            self.chat_target.set("Gegner" if self.chat_target.get() == "Meine Armee" else "Meine Armee")
        elif k == "send" and running and self.chat_var.get().strip():
            self._send_order()
        elif k == "m_control":
            self.control_var.set(a[1])
            self._write_control()
        elif k == "m_model":
            names = list(MODELS)
            cur = self.model_var.get()
            self.model_var.set(names[(names.index(cur) + 1) % len(names)] if cur in names else names[0])
            self._schedule_save()
        elif k == "m_effort":
            self.effort_var.set(a[1])
            self._schedule_save()
        elif k == "camp_sort":                              # Kampagne: Spaltenkopf im Overlay -> sortieren
            self._campaign_sort(a[1])
        elif k == "camp_important":                         # Kampagne: nur dringende und wichtige
            self.camp_important_var.set(not self.camp_important_var.get())
            self._campaign_important_changed()
        elif k == "camp_row":                               # Kampagne: Zeilenklick im Overlay -> Kamera
            tree = self.camp_tree
            if tree.exists(a[1]):
                self._camp_zoomed = None                    # erneuter Klick springt erneut
                tree.selection_set(a[1])
                tree.see(a[1])
                self._campaign_select()
        elif k == "camp_view":                              # Kampagne: Ansicht der Liste
            self.camp_view_var.set(a[1])
            self._campaign_view_changed()
        elif k == "camp_refresh":                           # Kampagne: Aktualisierung im Spiel anfordern
            self._campaign_request()
        elif k == "camp_fog":                               # Kampagne: Spezialschalter Nebel des Krieges
            self.camp_fog_var.set(not self.camp_fog_var.get())
            self._campaign_fog_changed()
        elif k == "ctl":                                    # KI fuehrt: eigene / Gegner ein-aus
            self._toggle_side(a[1])
        elif k == "m_groups":
            self.own_groups_var.set(not self.own_groups_var.get())
            self._write_control()
        elif k == "logview":
            self.log_nb.select(overlay_panel.LOG_VIEWS.index(a[1]))
            self._ov_scroll["log"] = None
        elif k == "m_zone_toggle":
            self.zone_on_var.set(not self.zone_on_var.get())
            self._zone_changed()
        elif k == "m_zone":
            self.zone_m_var.set(max(10, min(100, self._zone_m() + a[1])))
            self._zone_changed()
        elif k == "m_close_menu":
            self._ov_menu = False

    # ---------- Overlay: Tastatur (Stufe C) ----------
    def _ov_hook_want(self, want: bool) -> None:
        """Tastaturhook (overlay_kbd) ein/aus: solange ein Overlay-Feld den Fokus hat, sehen Steam und Spiel keine
        Tasten. Ohne Hook (Fehler) bleibt die Rueckfallebene der DLL (Tasten ueber das Spielfenster)."""
        if want and self._ov_hook is None and os.name == "nt":
            try:
                import overlay_kbd
                self._ov_hook = overlay_kbd.KeyboardHook(
                    lambda kind, code, mods: self.calls.put(lambda: self._ov_hook_event(kind, code, mods)))
            except Exception as e:
                self._ov_hook = False
                self._overlay_note(f"Tastaturhook nicht verf\u00fcgbar ({e}) - Tasten gehen \u00fcber das Spielfenster.")
        if self._ov_hook:
            self._ov_hook.want = want
            if self._ov_hook.error and not getattr(self, "_ov_hook_err_shown", False):
                self._ov_hook_err_shown = True
                self._overlay_note(f"Tastaturhook: {self._ov_hook.error}")

    def _ov_hook_event(self, kind: str, code: int, mods: int) -> None:
        self._ov_typed = True
        if self._ov_focus:
            self._ov_key_in(kind, code, mods)

    def _ov_widget(self, key: str):
        if key == "rev_sug":
            return self._ov_review["sug"] if self._ov_review else None
        return {"orders_player": self.orders_player_text, "orders_enemy": self.orders_enemy_text,
                "strategy": self.strat_text, "deploy": self.deploy_text}.get(key)

    def _ov_get(self, key: str) -> str:
        if key == "chat":
            return self.chat_var.get()
        w = self._ov_widget(key)
        return w.get("1.0", "end-1c") if w is not None else ""

    def _ov_focus_ok(self) -> bool:
        """Fokusfeld ist im Overlay gerade sichtbar (sonst gehoert die Tastatur wieder dem Spiel)."""
        f = self._ov_focus
        if f is None or f == "chat":
            return True
        if self._ov_review:
            return f == "rev_sug"
        tab = overlay_panel.TABS[self._ov_view - 1] if self._ov_view else ""
        return {"orders_player": "Befehle", "orders_enemy": "Befehle", "strategy": "Strategie",
                "deploy": "Aufstellung"}.get(f) == tab

    def _ov_edit(self, key: str, start: int, end: int, new: str) -> None:
        """Zeichen start..end durch new ersetzen - im selben Tk-Feld wie im Steuerpult (beide bleiben synchron)."""
        if key == "chat":
            t = self.chat_var.get()
            self.chat_var.set(t[:start] + new + t[end:])
        else:
            w = self._ov_widget(key)
            if w is None:
                return
            if end > start:
                w.delete(f"1.0 + {start} chars", f"1.0 + {end} chars")
            if new:
                w.insert(f"1.0 + {start} chars", new)
            if key != "rev_sug":
                self._schedule_save()
        self._ov_caret = start + len(new)

    def _ov_key_in(self, kind: str, code: int, mods: int) -> None:
        key = self._ov_focus
        text = self._ov_get(key)
        c = min(max(self._ov_caret, 0), len(text))
        self._ov_caret = c
        if kind == "char":
            ch = chr(code)
            if ch.isprintable():
                self._ov_edit(key, c, c, ch)
            return
        ctrl = bool(mods & 1)
        hot = next((h for h in self._ov_hots if h.action == ("scroll", key)), None)
        if code == 27:                                   # Esc: fertig
            self._ov_focus = None
        elif code == 13:                                 # Enter: Chat sendet, sonst neue Zeile
            if key == "chat":
                if not self.chat_var.get().strip():
                    pass
                elif self.proc and self.proc.poll() is None:
                    self._send_order()
                    self._ov_caret = 0
                else:
                    self._overlay_note("Der Feldherr l\u00e4uft nicht - Befehl nicht gesendet.")
            else:
                self._ov_edit(key, c, c, "\n")
        elif code == 8 and c > 0:                        # Backspace
            self._ov_edit(key, c - 1, c, "")
        elif code == 46 and c < len(text):               # Entf
            self._ov_edit(key, c, c + 1, "")
        elif code == 37:
            self._ov_caret = max(c - 1, 0)
        elif code == 39:
            self._ov_caret = min(c + 1, len(text))
        elif code in (36, 35):                           # Pos1 / Ende (Strg: ganzer Text)
            if ctrl or key == "chat":
                self._ov_caret = 0 if code == 36 else len(text)
            else:
                self._ov_caret = overlay_panel.caret_move(text, c, hot, "home" if code == 36 else "end")
        elif code in (38, 40) and key != "chat":         # hoch / runter
            self._ov_caret = overlay_panel.caret_move(text, c, hot, "up" if code == 38 else "down")
        elif code == 86 and ctrl:                        # Strg+V
            try:
                clip = self.clipboard_get()
            except tk.TclError:
                clip = ""
            if key == "chat":
                clip = " ".join(clip.split())
            if clip:
                self._ov_edit(key, c, c, clip.replace("\r\n", "\n"))

    def _pump(self) -> None:
        self._pump_n = getattr(self, "_pump_n", 0) + 1
        try:
            self._overlay_events()          # Mausklicks aus dem Overlay (jede Runde, ~0,15 s)
        except Exception as e:
            self._overlay_note(f"Eingabe: {e}")
        if self._pump_n % 2 == 0 or self._ov_pressed or self._ov_drag or self._ov_typed:   # Overlay neu zeichnen
            try:
                self._overlay_tick()
            except Exception as e:  # das Overlay darf das Steuerpult nie stoeren
                if not getattr(self, "_ov_err_shown", False):
                    self._ov_err_shown = True
                    self._append(f"Overlay: {e}\n")
        if self._pump_n % 7 == 3:   # etwa jede Sekunde: neuer Kampagnen-Lagebericht vom Spiel?
            try:
                self._campaign_refresh()
                self._campaign_feed()                   # nach einer Schlacht die Leiste wieder uebernehmen
            except Exception as e:  # Bericht darf das Steuerpult nie stoeren
                if not getattr(self, "_camp_err_shown", False):
                    self._camp_err_shown = True
                    self._append(f"Kampagne: Bericht fehlgeschlagen: {e}\n")
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
        """Ins Protokoll: jede Zeile nach "Alles" und in ihre Sorte (Plan Allianz 1/2, Befehle, Meldungen)."""
        for line in text.splitlines(keepends=True):
            targets = [self.log]
            if line.strip():
                targets.append(self.log_views[overlay_panel.log_category(line)])
            for t in targets:
                t.config(state="normal")
                t.insert("end", line)
                t.see("end")
                t.config(state="disabled")

    def _log_view(self) -> str:
        try:
            return overlay_panel.LOG_VIEWS[self.log_nb.index(self.log_nb.select())]
        except (tk.TclError, IndexError):
            return "Alles"

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
