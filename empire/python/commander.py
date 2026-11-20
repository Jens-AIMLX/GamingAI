"""
commander.py - startet den Feldherrn fuer Empire.

    python commander.py                       # Claude (claude-opus-5-5), Seite laut ai_bridge/config.lua
    python commander.py --model claude-sonnet-5-5 --effort low
    python commander.py --backend rule        # ohne Sprachmodell (Regel-Feldherr)

Laeuft dauerhaft und uebernimmt jede Schlacht, sobald sie beginnt. Welche Seite gesteuert wird, steht in
<Spielordner>/ai_bridge/config.lua (control = "enemy" | "player" | "both"). Jede Schlacht wird in
logs/battle_<Zeit>.jsonl protokolliert: Lageberichte, Plaene, Fehler, Token-Verbrauch.

Hotkeys (global, auch wenn Empire im Vordergrund ist; einstellbar mit --hotkeys, Standard F9/F10/F11):
    F9    Aufstellung uebernehmen: der Feldherr stellt die gesteuerten Seiten nach dem Aufstellungsprompt auf
    F10   markierte Einheiten -> KI
    F11   markierte Einheiten -> Spieler (die KI plant sie nicht mehr ein, bis sie zurueckgegeben werden)
Achtung: Empire liest die Tastatur selbst (DirectInput) und sieht jede Taste ebenfalls - Kombinationen wie Strg+Alt+A
kommen dort als Strg+A ("alle auswaehlen") an. Deshalb Tasten waehlen, die Empire nicht belegt.
Dieselben Befehle nimmt der Feldherr aus <Spielordner>/ai_bridge/commands.txt (eine Zeile: deploy | sel_ai |
sel_player) - so loesen die Buttons des Steuerpults sie aus.

Ablauf (nach rome-battle-ai): der Executor laeuft jede Sekunde (Bruecken-Takt) ohne Modell; der Feldherr
plant im Hintergrund neu, alle --interval Sekunden Spielzeit (--close-interval, sobald Armeen < 250 m
voneinander stehen), wenn sich etwas geaendert hat.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

from empire_bridge import EmpireBridge, Order
from empire_general.describe import describe, describe_deployment, situation_summary
from empire_general.executor import Executor
from empire_general.general import General, load_text, make_general, system_prompt
from empire_general.plan import Plan
from empire_general.world import World, centroid, dist

HERE = Path(__file__).resolve().parent
COMMANDS = ("deploy", "sel_ai", "sel_player",
            "give_ai", "give_player",   # give_ai 1.1.3,1.1.4  - Einheiten per Liste uebergeben (ohne Auswahl im Spiel)
            "order", "order_clear")     # order player|enemy <Text> - Sofortbefehl aus dem Chat; order_clear player|enemy
DEFAULT_HOTKEYS = "deploy=F9,sel_ai=F10,sel_player=F11"
_VK = {**{f"F{i}": 0x6F + i for i in range(1, 25)}, **{chr(c): c for c in range(ord("A"), ord("Z") + 1)},
       **{str(d): 0x30 + d for d in range(10)}, **{f"NUM{d}": 0x60 + d for d in range(10)},
       "PAUSE": 0x13, "SCROLLLOCK": 0x91, "INSERT": 0x2D, "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21, "PAGEDOWN": 0x22}
_MOD = {"STRG": 0x2, "CTRL": 0x2, "ALT": 0x1, "SHIFT": 0x4, "UMSCHALT": 0x4, "WIN": 0x8}


def parse_hotkeys(spec: str) -> dict[str, tuple[str, int, int]]:
    """'deploy=F9,sel_ai=Strg+Alt+K' -> {cmd: (Anzeige, Modifier, VK)}. Hinweis: Empire liest die Tastatur per
    DirectInput und sieht jede Taste trotzdem - Kombinationen mit Strg/Alt/Buchstaben loesen dort auch Spielbefehle aus."""
    out: dict[str, tuple[str, int, int]] = {}
    for part in (spec or "").split(","):
        if "=" not in part:
            continue
        cmd, key = (s.strip() for s in part.split("=", 1))
        if cmd not in COMMANDS or not key:
            continue
        mods, vk = 0, None
        for tok in key.upper().replace(" ", "").split("+"):
            if tok in _MOD:
                mods |= _MOD[tok]
            elif tok in _VK:
                vk = _VK[tok]
        if vk is None:
            say(f"WARNUNG: Hotkey '{key}' fuer {cmd} nicht verstanden - ignoriert")
            continue
        out[cmd] = (key, mods, vk)
    return out


def say(msg: str) -> None:
    print(msg, flush=True)


# ---------------- Hotkeys (Low-Level-Tastatur-Hook, ohne Zusatzpaket) ----------------
# RegisterHotKey kommt im Vollbild nicht an: Empire holt sich die Tastatur per DirectInput. Ein WH_KEYBOARD_LL-Hook
# sieht die Tasten vorher. Er laesst sie unveraendert ans Spiel weiter (nichts wird verschluckt).
def hotkey_thread(q: queue.Queue, hotkeys: dict[str, tuple[str, int, int]]) -> None:
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    WH_KEYBOARD_LL, WM_KEYDOWN, WM_SYSKEYDOWN, WM_KEYUP, WM_SYSKEYUP = 13, 0x100, 0x104, 0x101, 0x105
    VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x5B, 0x5C
    LRESULT = ctypes.c_ssize_t

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = LRESULT
    user32.GetAsyncKeyState.restype = ctypes.c_short
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    by_vk: dict[int, list[tuple[int, str]]] = {}
    for cmd, (label, mods, vk) in hotkeys.items():
        by_vk.setdefault(vk, []).append((mods, cmd))
    down: set[int] = set()

    def mods_now() -> int:
        pressed = lambda vk: bool(user32.GetAsyncKeyState(vk) & 0x8000)
        return ((0x2 if pressed(VK_CONTROL) else 0) | (0x1 if pressed(VK_MENU) else 0)
                | (0x4 if pressed(VK_SHIFT) else 0) | (0x8 if pressed(VK_LWIN) or pressed(VK_RWIN) else 0))

    def proc(n_code, w_param, l_param):
        if n_code == 0:
            kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = kb.vkCode
            if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                if vk in by_vk and vk not in down:          # nur beim ersten Druck, nicht bei Tastenwiederholung
                    m = mods_now()
                    for mods, cmd in by_vk[vk]:
                        if m == mods:
                            q.put(("hotkey", cmd))
                down.add(vk)
            elif w_param in (WM_KEYUP, WM_SYSKEYUP):
                down.discard(vk)
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    cb = HOOKPROC(proc)   # Referenz halten, sonst raeumt Python den Rueckruf weg
    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, cb, kernel32.GetModuleHandleW(None), 0)
    if not hook:
        say(f"WARNUNG: Tastatur-Hook konnte nicht gesetzt werden (Fehler {kernel32.GetLastError()}) - Hotkeys aus")
        return
    say("Hotkeys aktiv (Tastatur-Hook): " + ", ".join(f"{label} = {cmd}" for cmd, (label, _, _) in hotkeys.items()))
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:   # Nachrichtenschleife haelt den Hook am Leben
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def read_command_file(path: Path) -> list[str]:
    """commands.txt lesen und leeren (das Steuerpult haengt Zeilen an)."""
    try:
        tmp = path.with_suffix(".processing")
        path.replace(tmp)
    except (FileNotFoundError, PermissionError):
        return []
    try:
        lines = [l.strip() for l in tmp.read_text(encoding="utf-8").splitlines()]
    finally:
        tmp.unlink(missing_ok=True)
    return [l for l in lines if l.split(" ", 1)[0] in COMMANDS]


def write_roster(path: Path, w: World, sides: dict) -> None:
    """Einheitenliste fuer das Steuerpult: Name, Typ, Mannstaerke, Lage, wer fuehrt (atomar geschrieben)."""
    units = []
    for u in sorted(w.units.values(), key=lambda u: u.id):
        if u.manual:
            lead = "Spieler (übergeben)"
        elif u.grouped:
            lead = "Spieler (Gruppe)"
        elif u.controlled:
            lead = "KI"
        else:
            lead = "Spieler"
        units.append({"bid": u.bid, "id": u.id, "name": u.name, "type": u.type, "role": u.role, "alliance": u.alliance,
                      "men": u.men, "men_start": u.men_start, "lead": lead, "alive": u.alive,
                      "status": "flieht" if u.routing else ("vernichtet" if u.men == 0 else ""),
                      "placement": w.placement(u) if u.alive else ""})
    alls = sorted({u.alliance for u in w.units.values()})
    data = {"time": w.time, "phase": w.phase, "player_alliance": w.player_alliance or 1, "units": units,
            "situation": {str(a): situation_summary(w, a) for a in alls},
            "orders": {str(a): [t for _, t in s.immediate] for a, s in sides.items()}}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass   # Steuerpult liest gerade - naechste Sekunde wieder


HANDOVER_WINDOW_S = 60.0   # so lange nach einem Chat-Befehl duerfen Plaene Einheiten uebergeben


class Side:
    """Ein gesteuertes Heer: Executor + Feldherr + Planungs-Thread."""

    def __init__(self, alliance: int, general: General, log, system_for=None) -> None:
        self.alliance = alliance
        self.system_for = system_for or (lambda a: None)   # liefert vor jedem Plan den aktuellen System-Prompt
        self.general = general
        self.ex = Executor(alliance)
        self.log = log
        self.notes: list[str] = []
        self.busy = False
        self.ready: Plan | None = None
        self.ready_deploy: Plan | None = None
        self.pending_deploy: Plan | None = None
        self.pending_since = 0.0
        self.deploying = False
        self.deploy_start: dict[int, tuple[float, float]] = {}
        self.deploy_check_t: float | None = None
        self.deploy_done = False
        self.force_plan = False
        self.error: str | None = None
        self.last_plan_t = -1e9
        self.last_men: tuple[int, int] | None = None
        self.last_pos: dict[int, tuple[float, float]] = {}
        self.plans = 0
        self.immediate: list[tuple[float, str]] = []   # Sofortbefehle aus dem Chat: (Spielzeit, Text)
        self.ack_since: float | None = None             # naechster Plan nach diesem Zeitpunkt bestaetigt den Befehl
        # Uebergabe per Chat: Plaene bis HANDOVER_WINDOW_S nach dem neuesten Chat-Befehl duerfen Einheiten uebergeben
        # (der erste Plan danach vergisst es manchmal - Schlacht 2026-11-20 07:48), jede Einheit aber nur EINMAL je
        # Befehl: sonst gaebe der Feldherr eine Einheit, die der Spieler per Hotkey zurueckgeholt hat, erneut ab
        self.handover_since: float | None = None
        self.handover_done: set[int] = set()
        self.ready_handover: tuple[list[int], list[int]] | None = None

    def changed(self, w: World) -> bool:
        if self.last_men is None:
            return True
        men = (w.men_total(self.alliance), w.men_total(self.alliance, enemy=True))
        if men != self.last_men:
            return True
        return any(dist(u.pos, self.last_pos.get(u.id, u.pos)) > 20 for u in w.units.values())

    def _run(self, kind: str, fn, report: str, t_game: float) -> None:
        self.busy = True
        self.log({"type": "report" if kind == "plan" else "deploy_report", "alliance": self.alliance, "t": t_game, "text": report})

        def run():
            t0 = time.monotonic()
            try:
                plan = fn(report)
                if kind == "plan":
                    self.ready = plan
                else:
                    self.ready_deploy = plan
                self.log({"type": kind, "alliance": self.alliance, "t": t_game, "secs": round(time.monotonic() - t0, 1),
                          "usage": getattr(self.general, "last_usage", None), "plan": plan.model_dump(exclude_none=True)})
                fresh = (self.handover_since is not None and
                         0 <= t_game - self.handover_since <= HANDOVER_WINDOW_S)
                give = [i for i in plan.give_to_player if i not in self.handover_done]
                take = [i for i in plan.take_from_player if i not in self.handover_done]
                if kind == "plan" and fresh and (give or take):
                    old_g, old_t = self.ready_handover or ([], [])
                    self.ready_handover = (old_g + give, old_t + take)
                elif kind == "plan" and (give or take):
                    self.log({"type": "warning", "alliance": self.alliance, "t": t_game,
                              "text": "Uebergabe im Plan ignoriert (kein neuer Chat-Befehl): "
                                      f"an Spieler {plan.give_to_player}, zurueck {plan.take_from_player}"})
                if kind == "plan" and self.ack_since is not None and t_game >= self.ack_since:
                    self.ack_since = None
                    self.log({"type": "info", "t": t_game, "text": f"Feldherr Allianz {self.alliance} zum Befehl: {plan.intent}"})
            except Exception as e:  # Planung darf die Schlacht nie stoppen: Executor macht mit dem alten Plan weiter
                self.error = str(e)
                self.log({"type": "error", "alliance": self.alliance, "t": t_game, "error": str(e)})
            finally:
                self.busy = False

        threading.Thread(target=run, daemon=True).start()

    def start_plan(self, w: World) -> None:
        men = (w.men_total(self.alliance), w.men_total(self.alliance, enemy=True))
        losses = None
        if self.last_men:
            losses = (self.last_men[0] - men[0], self.last_men[1] - men[1])
        report = describe(w, self.alliance, previous=self.ex.plan, notes=self.notes, tasks=self.ex.tasks(w),
                          reflex_status=self.ex.reflex_status(), losses=losses,
                          errors=self.ex.errors[-5:] + self.ex.plan_warnings, immediate=self.immediate)
        self.last_men = men
        self.last_pos = {u.id: u.pos for u in w.units.values()}
        self.last_plan_t = w.time
        self.force_plan = False
        system = self.system_for(self.alliance)
        self._run("plan", lambda r: self.general.plan(r, w, self.alliance, system), report, w.time)

    def start_deploy(self, w: World, deploy_orders: str) -> None:
        report = describe_deployment(w, self.alliance, deploy_orders)
        self.deploy_done = False
        system = self.system_for(self.alliance)
        self._run("deploy", lambda r: self.general.plan_deployment(r, w, self.alliance, system), report, w.time)


def main() -> int:
    ap = argparse.ArgumentParser(description="Feldherr fuer Total War: EMPIRE")
    ap.add_argument("--backend", default="claude", choices=["claude", "rule"])
    ap.add_argument("--model", default=None, help="Claude-Modell (Standard claude-opus-5-5)")
    ap.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--interval", type=float, default=10.0, help="Sekunden Spielzeit zwischen Plaenen")
    ap.add_argument("--close-interval", type=float, default=5.0, help="... wenn die Armeen nah sind")
    ap.add_argument("--once", action="store_true", help="nach einer Schlacht beenden")
    ap.add_argument("--strategy", default=str(HERE / "strategies" / "standard.md"), help="Strategie-Datei (Spiegelstriche)")
    ap.add_argument("--instructions", default=None, help="Datei mit Befehlen fuer beide Seiten (aelteres Format)")
    ap.add_argument("--orders-player", default=None, help="Datei mit verbindlichen Befehlen fuer die Armee des Spielers")
    ap.add_argument("--orders-enemy", default=None, help="Datei mit verbindlichen Befehlen fuer die Gegnerarmee")
    ap.add_argument("--deploy-prompt", default=None, help="Datei mit dem Aufstellungsprompt (wird bei jedem Strg+Alt+A neu gelesen)")
    ap.add_argument("--no-hotkeys", action="store_true", help="keine globalen Hotkeys registrieren")
    ap.add_argument("--roster", default=str(HERE / "runtime" / "roster.json"),
                    help="Einheitenliste fuer das Steuerpult (wird jede Sekunde geschrieben)")
    ap.add_argument("--hotkeys", default=DEFAULT_HOTKEYS, help=f"z. B. '{DEFAULT_HOTKEYS}' oder 'deploy=Strg+Shift+F9,...'")
    args = ap.parse_args()

    rules = load_text(HERE / "rules.md")
    seen_texts: dict[tuple, str] = {}

    def texts() -> tuple[str, str, str, str]:
        """Strategie und Befehle werden vor JEDEM Plan neu gelesen - Aenderungen im Steuerpult wirken sofort."""
        both = load_text(Path(args.instructions)) if args.instructions else ""
        player = load_text(Path(args.orders_player)) if args.orders_player else ""
        enemy = load_text(Path(args.orders_enemy)) if args.orders_enemy else ""
        return load_text(Path(args.strategy)), both, player, enemy

    def system_for(a: int) -> str:
        strategy, both, player, enemy = texts()
        is_player = a == ((w.player_alliance if w else None) or 1)
        role = "player" if is_player else "enemy"
        orders = "\n".join(x.strip() for x in (both, player if is_player else enemy) if x.strip())
        key = (a, role)
        if seen_texts.get(key) != strategy + "|" + orders:
            if key in seen_texts:
                say(f"Allianz {a}: geaenderte Strategie/Befehle uebernommen (ab diesem Plan)")
            seen_texts[key] = strategy + "|" + orders
        return system_prompt(rules, strategy, orders, role)

    strategy0, both0, player0, enemy0 = texts()
    say(f"Strategie: {Path(args.strategy).stem}; Befehle Spielerarmee {len((both0 + player0).strip())} Zeichen, "
        f"Gegnerarmee {len((both0 + enemy0).strip())} Zeichen (werden vor jedem Plan neu gelesen)")
    br = EmpireBridge()
    logdir = HERE / "logs"
    logdir.mkdir(exist_ok=True)
    cmd_file = br.dir / "commands.txt"
    cmd_file.unlink(missing_ok=True)   # keine alten Befehle aus einer frueheren Sitzung ausfuehren

    q: queue.Queue = queue.Queue()
    if not args.no_hotkeys and sys.platform == "win32":
        threading.Thread(target=hotkey_thread, args=(q, parse_hotkeys(args.hotkeys)), daemon=True).start()

    say(f"Feldherr bereit ({args.backend}{' ' + (args.model or 'claude-opus-5-5') if args.backend == 'claude' else ''}), "
        f"Bruecke {br.dir}. Warte auf eine Schlacht ...")

    w: World | None = None
    sides: dict[int, Side] = {}
    logf = None
    last_seq, last_time = -1, -1.0
    battle_over = False
    reported_grouped: set[int] = set()

    def log(entry: dict) -> None:
        if logf:
            logf.write(json.dumps(entry, ensure_ascii=False) + "\n")
            logf.flush()
        if entry["type"] == "plan":
            say(f"[{entry['t']:6.0f}s] Plan Allianz {entry['alliance']} ({entry['secs']} s): {entry['plan']['intent']}")
        elif entry["type"] == "deploy":
            say(f"[{entry['t']:6.0f}s] Aufstellungsplan Allianz {entry['alliance']} ({entry['secs']} s): {entry['plan']['intent']}")
        elif entry["type"] == "error":
            say(f"[{entry['t']:6.0f}s] FEHLER Allianz {entry['alliance']}: {entry['error']}")
        elif entry["type"] == "info":
            say(f"[{entry.get('t', 0):6.0f}s] {entry['text']}")

    def side_for(a: int) -> Side:
        if a not in sides:
            sides[a] = Side(a, make_general(args.backend, args.model, args.effort, rules), log, system_for)
            role = "Spielerarmee" if a == (w.player_alliance or 1) else "Gegnerarmee"
            say(f"Allianz {a} ist die {role}.")
            say(f"Uebernehme Allianz {a} ({len(w.side(a))} Einheiten).")
        return sides[a]

    def handle(line: str, source: str) -> list[Order]:
        out: list[Order] = []
        cmd, _, arg = line.partition(" ")
        arg = arg.strip()
        if w is None or battle_over:
            say(f"{cmd} ({source}): keine laufende Schlacht")
            return out
        if cmd in ("order", "order_clear"):
            target, _, text = arg.partition(" ")
            player = w.player_alliance or 1
            others = sorted({u.alliance for u in w.units.values()} - {player})
            a = player if target != "enemy" else (others[0] if others else None)
            label = "Spielerarmee" if target != "enemy" else "Gegnerarmee"
            if a is None or (a not in w.wanted_alliances() and a not in sides):
                say(f"Befehl an die {label}: diese Seite wird nicht von der KI gefuehrt (Modus in 'KI fuehrt' pruefen)")
                return out
            s = side_for(a)
            if cmd == "order_clear":
                s.immediate.clear()
                s.force_plan = True
                log({"type": "info", "t": w.time, "text": f"Sofortbefehle an die {label} (Allianz {a}) aufgehoben"})
                return out
            if not text.strip():
                return out
            s.immediate = (s.immediate + [(w.time, text.strip())])[-5:]
            s.force_plan = True
            s.ack_since = w.time
            s.handover_since = w.time
            s.handover_done.clear()
            log({"type": "order", "alliance": a, "t": w.time, "text": text.strip()})
            say(f"[{w.time:6.0f}s] Befehl an die {label} (Allianz {a}): {text.strip()}"
                + ("" if w.phase == "Deployed" else " - wird mit dem ersten Plan nach Schlachtbeginn umgesetzt"))
            return out
        if cmd in ("give_ai", "give_player"):
            units = [w.by_bid[b] for b in arg.replace(",", " ").split() if b in w.by_bid]
            if not units:
                say(f"{cmd}: keine bekannte Einheit angegeben")
                return out
            cmd = "sel_ai" if cmd == "give_ai" else "sel_player"
            source = source if source == "Chat-Befehl" else "Liste"
        else:
            units = None
        if cmd == "deploy":
            alls = w.wanted_alliances()
            if not alls:
                say("Aufstellung: laut config.lua fuehrt die KI keine Seite (Modus 'Aus')")
                return out
            if w.phase != "Deployment":
                say(f"Hinweis: Aufstellung angefordert in Phase {w.phase} - wird als Bewegungsplan ausgefuehrt")
            text = load_text(Path(args.deploy_prompt)) if args.deploy_prompt else ""
            for a in alls:
                s = side_for(a)
                if s.busy:
                    say(f"Allianz {a}: Feldherr plant gerade, Aufstellung folgt danach nicht automatisch - bitte erneut ausloesen")
                    continue
                s.start_deploy(w, text)
            log({"type": "info", "t": w.time, "text": f"Aufstellung angefordert ({source}) fuer Allianz {', '.join(map(str, alls))}"
                                                   + (f", Aufstellungsprompt {len(text)} Zeichen" if text.strip() else ", ohne Aufstellungsprompt")})
        elif cmd in ("sel_ai", "sel_player"):
            sel = units if units is not None else w.selected()
            if not sel:
                say(f"{'Markierte -> KI' if cmd == 'sel_ai' else 'Markierte -> Spieler'}: keine Einheit markiert")
                return out
            for u in sel:
                out.append(Order(u.bid, "take" if cmd == "sel_ai" else "release"))
                if cmd == "sel_ai":
                    side_for(u.alliance).force_plan = True
                elif u.alliance in sides:
                    sides[u.alliance].force_plan = True
            ids = ", ".join(u.name or f"#{u.id}" for u in sel)
            log({"type": "info", "t": w.time,
                 "text": f"{'An die KI uebergeben' if cmd == 'sel_ai' else 'An den Spieler uebergeben'} ({source}): {ids}"})
        return out

    # Befehle aus Hotkeys/Liste werden gesammelt und GENAU EINMAL pro Bruecken-Takt mitgeschickt: orders.lua wird bei
    # jedem Senden ersetzt, und das Spiel liest sie nur einmal pro Takt - zwei Sendungen in einem Takt verlieren die erste.
    carry: list[Order] = []
    released_at: dict[str, float] = {}
    while True:
        # Befehle aus Hotkeys und commands.txt
        cmds = [(c, "Datei") for c in read_command_file(cmd_file)]
        while not q.empty():
            _, c = q.get_nowait()
            cmds.append((c, "Hotkey"))
        for c, src in cmds:
            carry += handle(c, src)

        st = br.read_state()
        if not st or st.get("seq") == last_seq:
            time.sleep(0.1)
            continue
        pending, carry = carry, []
        new_battle = w is None or st["seq"] < last_seq or st["game_time"] < last_time - 2
        last_seq, last_time = st["seq"], st["game_time"]
        if new_battle:
            if w is not None and args.once:
                break
            w, sides, battle_over = World(), {}, False
            reported_grouped = set()
            if logf:
                logf.close()
            logf = (logdir / time.strftime("battle_%Y%m%d_%H%M%S.jsonl")).open("w", encoding="utf-8")
            say("Neue Schlacht erkannt.")
        w.update(st)
        grouped = {u.id for u in w.units.values() if u.grouped and not u.manual}
        if grouped - reported_grouped:
            new = sorted(grouped - reported_grouped)
            reported_grouped |= grouped
            log({"type": "info", "t": w.time, "text": f"{len(new)} Einheit(en) sind im Spiel gruppiert und koennen nicht "
                 f"uebernommen werden ({', '.join('#' + str(i) for i in new)}). Gruppe im Spiel aufloesen - die KI uebernimmt "
                 "sie dann automatisch (Pruefung alle 10 s)."})
        if battle_over:
            continue
        if w.phase == "Complete":
            battle_over = True
            say(f"Schlacht beendet nach {w.battle_time:.0f} s Schlachtzeit. Plaene: " + ", ".join(f"Allianz {a}: {s.plans}" for a, s in sides.items()))
            if args.once:
                break
            continue

        if w.phase == "Deployed":
            for a in w.controlled_alliances:
                side_for(a)
        orders = list(pending)   # Kopie: orders += ... darf pending nicht veraendern
        for a, s in sides.items():
            mine = w.side(a, alive=False)
            s.ex.note_results(w.order_results, {u.bid for u in mine})

            # Aufstellungsplan: erst Kontrolle uebernehmen, dann Plan setzen
            if s.ready_deploy is not None:
                s.pending_deploy, s.ready_deploy = s.ready_deploy, None
                s.pending_since = w.time
                orders += [Order(u.bid, "take") for u in mine if not u.controlled and not u.manual]
            if s.pending_deploy is not None:
                ids = {uid for g in s.pending_deploy.groups for uid in g.unit_ids}
                ready = all(w.units[i].controlled for i in ids if i in w.units and not w.units[i].manual)
                if ready or w.time - s.pending_since > 3:
                    s.ex.set_plan(s.pending_deploy, w, deploy=True)
                    for wtxt in s.ex.plan_warnings:
                        log({"type": "warning", "alliance": a, "t": w.time, "text": wtxt})
                        say(f"[{w.time:6.0f}s] Allianz {a}: {wtxt}")
                    s.pending_deploy = None
                    s.deploying = True
                    s.plans += 1
                    s.last_plan_t = w.time
                    s.deploy_start = {u.id: u.pos for u in w.side(a) if u.controlled}
                    s.deploy_check_t = w.time + 12
                    log({"type": "info", "t": w.time, "text": f"Allianz {a}: Aufstellung wird ausgefuehrt ({len(s.deploy_start)} Einheiten)"})

            if s.ready is not None:
                plan, s.ready = s.ready, None
                s.ex.set_plan(plan, w)
                for wtxt in s.ex.plan_warnings:
                    log({"type": "warning", "alliance": a, "t": w.time, "text": wtxt})
                s.deploying = False
                s.plans += 1
                if plan.note:
                    s.notes = (s.notes + [plan.note])[-5:]
            if s.ready_handover is not None:
                give, take = s.ready_handover
                s.ready_handover = None
                # nur eigene Einheiten; abgeben nur, was die KI noch fuehrt - zuruecknehmen nur, was der Spieler fuehrt.
                # Die Befehle laufen ueber carry (naechster Takt) wie Hotkey/Liste, inkl. 3-s-Sperre nach der Freigabe.
                g_units = [w.units[i] for i in give if i in w.units and w.units[i].alliance == a and not w.units[i].manual]
                t_units = [w.units[i] for i in take if i in w.units and w.units[i].alliance == a and w.units[i].manual]
                if g_units:
                    carry += handle("give_player " + ",".join(u.bid for u in g_units), "Chat-Befehl")
                if t_units:
                    carry += handle("give_ai " + ",".join(u.bid for u in t_units), "Chat-Befehl")
                s.handover_done.update(give + take)     # je Befehl nur einmal - auch, wenn schon beim Ziel
                skipped = [i for i in give + take if i not in w.units or w.units[i].alliance != a]
                if skipped:
                    log({"type": "warning", "alliance": a, "t": w.time,
                         "text": f"Uebergabe: Einheit(en) {skipped} uebersprungen (nicht eigene oder unbekannt)"})

            if s.ex.plan is not None and (w.phase == "Deployed" or s.deploying):
                orders += s.ex.tick(w)

            # Rueckmeldung zur Aufstellung: bewegen sich Einheiten schon in der Aufstellungsphase?
            if s.deploying and not s.deploy_done and s.deploy_check_t is not None and w.time >= s.deploy_check_t:
                s.deploy_check_t = None
                moved = sum(1 for uid, p in s.deploy_start.items() if uid in w.units and dist(w.units[uid].pos, p) > 5)
                n = len(s.deploy_start)
                if w.phase == "Deployment" and moved == 0 and n:
                    txt = (f"Allianz {a}: in der Aufstellungsphase bewegt sich keine Einheit - das Spiel laesst Skript-Bewegungen "
                           "hier offenbar nicht zu. Die Einheiten marschieren direkt nach Schlachtbeginn an ihre Plaetze.")
                else:
                    txt = f"Allianz {a}: {moved}/{n} Einheiten bewegen sich zur Aufstellung"
                log({"type": "deploy_check", "alliance": a, "t": w.time, "moved": moved, "units": n, "phase": w.phase, "text": txt})
                say(f"[{w.time:6.0f}s] {txt}")
            if s.deploying and not s.deploy_done:
                tasks = s.ex.tasks(w)
                if tasks and all(t.endswith("in place") for t in tasks.values()):
                    s.deploy_done = True
                    log({"type": "info", "t": w.time, "text": f"Allianz {a}: Aufstellung abgeschlossen - alle Einheiten auf Position"})

            # normale Planung nur nach Schlachtbeginn
            if w.phase == "Deployed" and not s.busy:
                foes = w.enemies(a)
                cm, ce = centroid(w.side(a)), centroid(foes)
                close = bool(cm and ce and min((dist(u.pos, f.pos) for u in w.side(a) for f in foes), default=1e9) < 250)
                interval = args.close_interval if close else args.interval
                due = w.time - s.last_plan_t >= interval
                first = s.ex.plan is None and w.time - s.last_plan_t >= 3   # nach Fehler nicht sekuendlich wiederholen
                if s.force_plan or first or (due and (s.changed(w) or w.time - s.last_plan_t >= 3 * interval)):
                    s.start_plan(w)
        write_roster(Path(args.roster), w, sides)
        # gerade an den Spieler freigegebene Einheiten bekommen 3 s Spielzeit lang keine Executor-Befehle mehr
        # (das Lagebild zeigt sie noch ein bis zwei Takte lang als gesteuert)
        for o in pending:
            if o.cmd == "release":
                released_at[o.unit] = w.time
            elif o.cmd == "take":
                released_at.pop(o.unit, None)
        blocked = {b for b, t in released_at.items() if w.time - t < 3}
        if blocked:
            orders = pending + [o for o in orders[len(pending):] if o.unit not in blocked]
        if orders:
            br.send(orders)
    if logf:
        logf.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("beendet.")
