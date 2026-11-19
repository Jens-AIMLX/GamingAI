"""
commander.py - startet den Feldherrn fuer Empire.

    python commander.py                       # Claude (claude-opus-5-5), Seite laut ai_bridge/config.lua
    python commander.py --model claude-sonnet-5-5 --effort low
    python commander.py --backend rule        # ohne Sprachmodell (Regel-Feldherr)

Laeuft dauerhaft und uebernimmt jede Schlacht, sobald sie beginnt. Welche Seite gesteuert wird, steht in
<Spielordner>/ai_bridge/config.lua (control = "enemy" | "player" | "both"). Jede Schlacht wird in
logs/battle_<Zeit>.jsonl protokolliert: Lageberichte, Plaene, Fehler, Token-Verbrauch.

Ablauf (nach rome-battle-ai): der Executor laeuft jede Sekunde (Bruecken-Takt) ohne Modell; der Feldherr
plant im Hintergrund neu, alle --interval Sekunden Spielzeit (--close-interval, sobald Armeen < 250 m
voneinander stehen), wenn sich etwas geaendert hat.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from empire_bridge import EmpireBridge
from empire_general.describe import describe
from empire_general.executor import Executor
from empire_general.general import General, load_text, make_general
from empire_general.plan import Plan
from empire_general.world import World, centroid, dist

HERE = Path(__file__).resolve().parent


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
        self.busy = True
        t_game = w.time
        self.log({"type": "report", "alliance": self.alliance, "t": t_game, "text": report})

        def run():
            t0 = time.monotonic()
            try:
                plan = self.general.plan(report, w, self.alliance)
                self.ready = plan
                self.log({"type": "plan", "alliance": self.alliance, "t": t_game, "secs": round(time.monotonic() - t0, 1),
                          "usage": getattr(self.general, "last_usage", None), "plan": plan.model_dump(exclude_none=True)})
            except Exception as e:  # Planung darf die Schlacht nie stoppen: Executor macht mit dem alten Plan weiter
                self.error = str(e)
                self.log({"type": "error", "alliance": self.alliance, "t": t_game, "error": str(e)})
            finally:
                self.busy = False

        threading.Thread(target=run, daemon=True).start()


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
    args = ap.parse_args()

    rules, strategy = load_text(HERE / "rules.md"), load_text(Path(args.strategy))
    instructions = load_text(Path(args.instructions)) if args.instructions else ""
    print(f"Strategie: {Path(args.strategy).stem}" + (f", Zusatzanweisungen: {len(instructions)} Zeichen" if instructions.strip() else ""), flush=True)
    br = EmpireBridge()
    logdir = HERE / "logs"
    logdir.mkdir(exist_ok=True)

    print(f"Feldherr bereit ({args.backend}{' ' + (args.model or 'claude-opus-5-5') if args.backend == 'claude' else ''}), "
          f"Bruecke {br.dir}. Warte auf eine Schlacht ...", flush=True)

    w: World | None = None
    sides: dict[int, Side] = {}
    logf = None
    last_seq, last_time = -1, -1.0
    battle_over = False

    def log(entry: dict) -> None:
        if logf:
            logf.write(json.dumps(entry, ensure_ascii=False) + "\n")
            logf.flush()
        if entry["type"] == "plan":
            p = entry["plan"]
            print(f"[{entry['t']:6.0f}s] Plan Allianz {entry['alliance']} ({entry['secs']} s): {p['intent']}", flush=True)
        elif entry["type"] == "error":
            print(f"[{entry['t']:6.0f}s] FEHLER Allianz {entry['alliance']}: {entry['error']}", flush=True)

    while True:
        st = br.read_state()
        if not st or st.get("seq") == last_seq:
            time.sleep(0.1)
            continue
        new_battle = w is None or st["seq"] < last_seq or st["game_time"] < last_time - 2
        last_seq, last_time = st["seq"], st["game_time"]
        if new_battle:
            if w is not None and args.once:
                break
            w, sides, battle_over = World(), {}, False
            if logf:
                logf.close()
            logf = (logdir / time.strftime("battle_%Y%m%d_%H%M%S.jsonl")).open("w", encoding="utf-8")
            print("Neue Schlacht erkannt.", flush=True)
        w.update(st)
        if battle_over:
            continue
        if w.phase == "Complete":
            battle_over = True
            print(f"Schlacht beendet nach {w.time:.0f} s. Plaene: " + ", ".join(f"Allianz {a}: {s.plans}" for a, s in sides.items()), flush=True)
            if args.once:
                break
            continue
        if w.phase != "Deployed":
            continue

        for a in w.controlled_alliances:
            if a not in sides:
                sides[a] = Side(a, make_general(args.backend, args.model, args.effort, rules, strategy, instructions), log)
                print(f"Uebernehme Allianz {a} ({len(w.side(a))} Einheiten).", flush=True)
        orders = []
        bids = set()
        for a, s in sides.items():
            mine = w.side(a, alive=False)
            bids |= {u.bid for u in mine}
            s.ex.note_results(w.order_results, {u.bid for u in mine})
            if s.ready is not None:
                plan, s.ready = s.ready, None
                s.ex.set_plan(plan, w)
                s.plans += 1
                if plan.note:
                    s.notes = (s.notes + [plan.note])[-5:]
            orders += s.ex.tick(w)
            if not s.busy:
                foes = w.enemies(a)
                cm, ce = centroid(w.side(a)), centroid(foes)
                close = bool(cm and ce and min((dist(u.pos, f.pos) for u in w.side(a) for f in foes), default=1e9) < 250)
                interval = args.close_interval if close else args.interval
                due = w.time - s.last_plan_t >= interval
                first = s.ex.plan is None and w.time - s.last_plan_t >= 3   # nach Fehler nicht sekuendlich wiederholen
                if first or (due and (s.changed(w) or w.time - s.last_plan_t >= 3 * interval)):
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
        print("beendet.")
