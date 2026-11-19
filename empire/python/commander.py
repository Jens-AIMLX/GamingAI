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
import queue
import sys
import threading
import time
from pathlib import Path

from empire_bridge import EmpireBridge, Order
from empire_general.describe import describe, describe_deployment
from empire_general.executor import Executor
from empire_general.general import General, load_text, make_general
from empire_general.plan import Plan
from empire_general.world import World, centroid, dist

HERE = Path(__file__).resolve().parent
COMMANDS = ("deploy", "sel_ai", "sel_player")
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


# ---------------- Hotkeys (Windows RegisterHotKey, ohne Zusatzpaket) ----------------
def hotkey_thread(q: queue.Queue, hotkeys: dict[str, tuple[str, int, int]]) -> None:
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return
    user32 = ctypes.windll.user32
    MOD_NOREPEAT, WM_HOTKEY = 0x4000, 0x0312
    ids = {}
    for i, (cmd, (label, mods, vk)) in enumerate(hotkeys.items(), start=1):
        if user32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
            ids[i] = cmd
        else:
            say(f"WARNUNG: Hotkey {label} konnte nicht registriert werden (von einem anderen Programm belegt?)")
    if ids:
        say("Hotkeys aktiv: " + ", ".join(f"{hotkeys[c][0]} = {c}" for c in ids.values()))
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == WM_HOTKEY and msg.wParam in ids:
            q.put(("hotkey", ids[msg.wParam]))


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
    return [l for l in lines if l in COMMANDS]


class Side:
    """Ein gesteuertes Heer: Executor + Feldherr + Planungs-Thread."""

    def __init__(self, alliance: int, general: General, log) -> None:
        self.alliance = alliance
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
                          errors=self.ex.errors[-5:] + self.ex.plan_warnings)
        self.last_men = men
        self.last_pos = {u.id: u.pos for u in w.units.values()}
        self.last_plan_t = w.time
        self.force_plan = False
        self._run("plan", lambda r: self.general.plan(r, w, self.alliance), report, w.time)

    def start_deploy(self, w: World, deploy_orders: str) -> None:
        report = describe_deployment(w, self.alliance, deploy_orders)
        self.deploy_done = False
        self._run("deploy", lambda r: self.general.plan_deployment(r, w, self.alliance), report, w.time)


def main() -> int:
    ap = argparse.ArgumentParser(description="Feldherr fuer Total War: EMPIRE")
    ap.add_argument("--backend", default="claude", choices=["claude", "rule"])
    ap.add_argument("--model", default=None, help="Claude-Modell (Standard claude-opus-5-5)")
    ap.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--interval", type=float, default=10.0, help="Sekunden Spielzeit zwischen Plaenen")
    ap.add_argument("--close-interval", type=float, default=5.0, help="... wenn die Armeen nah sind")
    ap.add_argument("--once", action="store_true", help="nach einer Schlacht beenden")
    ap.add_argument("--strategy", default=str(HERE / "strategies" / "standard.md"), help="Strategie-Datei (Spiegelstriche)")
    ap.add_argument("--instructions", default=None, help="Datei mit freien Anweisungen an den Feldherrn")
    ap.add_argument("--deploy-prompt", default=None, help="Datei mit dem Aufstellungsprompt (wird bei jedem Strg+Alt+A neu gelesen)")
    ap.add_argument("--no-hotkeys", action="store_true", help="keine globalen Hotkeys registrieren")
    ap.add_argument("--hotkeys", default=DEFAULT_HOTKEYS, help=f"z. B. '{DEFAULT_HOTKEYS}' oder 'deploy=Strg+Shift+F9,...'")
    args = ap.parse_args()

    rules, strategy = load_text(HERE / "rules.md"), load_text(Path(args.strategy))
    instructions = load_text(Path(args.instructions)) if args.instructions else ""
    say(f"Strategie: {Path(args.strategy).stem}" + (f", Zusatzanweisungen: {len(instructions)} Zeichen" if instructions.strip() else ""))
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
            sides[a] = Side(a, make_general(args.backend, args.model, args.effort, rules, strategy, instructions), log)
            say(f"Uebernehme Allianz {a} ({len(w.side(a))} Einheiten).")
        return sides[a]

    def handle(cmd: str, source: str) -> list[Order]:
        out: list[Order] = []
        if w is None or battle_over:
            say(f"{cmd} ({source}): keine laufende Schlacht")
            return out
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
            sel = w.selected()
            if not sel:
                say(f"{'Markierte -> KI' if cmd == 'sel_ai' else 'Markierte -> Spieler'}: keine Einheit markiert")
                return out
            for u in sel:
                out.append(Order(u.bid, "take" if cmd == "sel_ai" else "release"))
                if cmd == "sel_ai":
                    side_for(u.alliance).force_plan = True
                elif u.alliance in sides:
                    sides[u.alliance].force_plan = True
            ids = ", ".join(f"#{u.id}" for u in sel)
            log({"type": "info", "t": w.time,
                 "text": f"{'An die KI uebergeben' if cmd == 'sel_ai' else 'An den Spieler uebergeben'} ({source}): {ids}"})
        return out

    while True:
        # Befehle aus Hotkeys und commands.txt
        pending: list[Order] = []
        cmds = [(c, "Datei") for c in read_command_file(cmd_file)]
        while not q.empty():
            _, c = q.get_nowait()
            cmds.append((c, "Hotkey"))
        for c, src in cmds:
            pending += handle(c, src)

        st = br.read_state()
        if not st or st.get("seq") == last_seq:
            if pending:
                br.send(pending)
            time.sleep(0.1)
            continue
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
            say(f"Schlacht beendet nach {w.time:.0f} s. Plaene: " + ", ".join(f"Allianz {a}: {s.plans}" for a, s in sides.items()))
            if args.once:
                break
            continue

        if w.phase == "Deployed":
            for a in w.controlled_alliances:
                side_for(a)
        orders = pending
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
                    s.ex.set_plan(s.pending_deploy, w)
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
                s.deploying = False
                s.plans += 1
                if plan.note:
                    s.notes = (s.notes + [plan.note])[-5:]

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
