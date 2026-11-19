"""
sim_deploy.py - Offline-Test: Aufstellung per Befehl und Uebergabe markierter Einheiten (ohne Spiel, ohne Modell).

Ablauf in der Simulation (Modus "both", Regel-Feldherr):
  1. Aufstellungsphase, Befehl "deploy" ueber commands.txt (wie Button/Strg+Alt+A)
     -> beide Seiten werden uebernommen und stellen sich auf (Linie, Artillerie dahinter, Kavallerie an den Fluegeln)
  2. Spieler markiert Einheit 1.1.1, Befehl "sel_player" -> Einheit freigegeben, KI plant sie nicht mehr ein,
     sie bleibt auch bei Schlachtbeginn beim Spieler
  3. Schlachtbeginn, danach "sel_ai" -> Einheit wieder bei der KI
    python sim_deploy.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from lupa import lua51

from sim_commander import MOCK, SCRIPT

HERE = Path(__file__).resolve().parent

EXTRA = r"""
function SIM_group(alliance_index, unit_index, on)
  alls[alliance_index].armies():item(1):units():item(unit_index).grouped = on
end
function SIM_select_unit(alliance_index, unit_index, on)
  local a = alls[alliance_index]
  local u = a.armies():item(1):units():item(unit_index)
  _G[SIM.handlers.sel](u, on)
end
"""


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="empire_deploy_sim_"))
    bridge = work / "ai_bridge"
    bridge.mkdir()
    (bridge / "config.lua").write_text('return { control = "both", player_alliance = 1, tick_ms = 1000 }\n')
    os.chdir(work)
    lua = lua51.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(MOCK.replace("local alls =", "alls ="))
    lua.execute(EXTRA)
    lua.execute(SCRIPT.read_text(encoding="latin-1"))
    g = lua.globals()
    prompt = work / "deploy_prompt.txt"
    prompt.write_text("Infanterie in einer Linie, Artillerie dahinter.\n", encoding="utf-8")

    env = dict(os.environ, EMPIRE_BRIDGE_DIR=str(bridge), PYTHONIOENCODING="utf-8")
    out = open(work / "commander.out", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(HERE / "commander.py"), "--backend", "rule", "--once", "--no-hotkeys",
                             "--deploy-prompt", str(prompt), "--roster", str(work / "roster.json")],
                            env=env, cwd=str(HERE), stdout=out, stderr=subprocess.STDOUT)

    def run(seconds: float):
        t_end = g.SIM.t + seconds
        while g.SIM.t < t_end:
            g.SIM_step(0.25)
            time.sleep(0.0125)

    def state():
        return json.loads((bridge / "state.json").read_text(encoding="utf-8"))

    def unit(st, bid):
        for al in st["alliances"]:
            for u in al["units"]:
                if u["id"] == bid:
                    return u

    def wait_unit(bid, pred, max_sim=60):
        """bis zu max_sim Spielsekunden warten, bis pred(einheit) gilt (Simulation laeuft ~20x Echtzeit)"""
        t_end = g.SIM.t + max_sim
        while g.SIM.t < t_end:
            u = unit(state(), bid)
            if u and pred(u):
                return u
            run(1)
        return unit(state(), bid)

    results = {}
    g.SIM_group(2, 2, True)   # Einheit 2.1.2 steckt in einer Gruppe des Spielers
    g.SIM_phase("Startup"); g.SIM_phase("Deployment")
    run(5)
    # erst Befehle schicken, wenn der Feldherr die Schlacht erkannt hat (beim Start loescht er alte Befehle)
    t0 = time.monotonic()
    while "Neue Schlacht erkannt" not in (work / "commander.out").read_text(encoding="utf-8") and time.monotonic() - t0 < 30:
        run(1)
    start = {u["id"]: (u["x"], u["z"]) for al in state()["alliances"] for u in al["units"]}
    (bridge / "commands.txt").write_text("deploy\n", encoding="utf-8")
    run(60)
    st = state()
    moved = sum(1 for al in st["alliances"] for u in al["units"]
                if ((u["x"] - start[u["id"]][0]) ** 2 + (u["z"] - start[u["id"]][1]) ** 2) ** 0.5 > 5)
    ctrl = sum(1 for al in st["alliances"] for u in al["units"] if u["controlled"])
    results["A Aufstellung (Einheiten bewegt / uebernommen)"] = (moved >= 5 and ctrl == 8, f"{moved} bewegt, {ctrl}/9 uebernommen (1 gruppiert)")
    u = unit(st, "2.1.2")
    results["F gruppierte Einheit gemeldet, nicht uebernommen"] = (u["grouped"] and not u["controlled"], f"grouped={u['grouped']} controlled={u['controlled']}")
    g.SIM_group(2, 2, False)  # Spieler loest die Gruppe auf
    u = wait_unit("2.1.2", lambda u: u["controlled"])
    results["G nach Aufloesen automatisch uebernommen"] = (u["controlled"] and not u["grouped"], f"grouped={u['grouped']} controlled={u['controlled']}")

    g.SIM_select_unit(1, 1, True)
    run(3)
    (bridge / "commands.txt").write_text("sel_player\n", encoding="utf-8")
    u = wait_unit("1.1.1", lambda u: u["manual"])
    results["B Markierte -> Spieler"] = (u["controlled"] is False and u["manual"] is True, f"controlled={u['controlled']} manual={u['manual']}")

    g.SIM_phase("Deployed")
    run(20)
    u = unit(state(), "1.1.1")
    results["C bleibt bei Schlachtbeginn beim Spieler"] = (u["controlled"] is False, f"controlled={u['controlled']}")

    (bridge / "commands.txt").write_text("sel_ai\n", encoding="utf-8")
    u = wait_unit("1.1.1", lambda u: u["controlled"])
    results["D Markierte -> KI"] = (u["controlled"] is True and u["manual"] is False, f"controlled={u['controlled']} manual={u['manual']}")

    # Uebergabe per Einheitenliste (ohne Auswahl im Spiel) und zurueck
    (bridge / "commands.txt").write_text("give_player 1.1.2,1.1.3\n", encoding="utf-8")
    u = wait_unit("1.1.3", lambda u: u["manual"])
    u2 = unit(state(), "1.1.2")
    results["H Liste -> Spieler (2 Einheiten)"] = (u["manual"] and u2["manual"], f"1.1.2 manual={u2['manual']} 1.1.3 manual={u['manual']}")
    (bridge / "commands.txt").write_text("give_ai 1.1.2,1.1.3\n", encoding="utf-8")
    u = wait_unit("1.1.3", lambda u: u["controlled"])
    results["I Liste -> KI"] = (u["controlled"] and not u["manual"], f"controlled={u['controlled']}")

    # Einheitenliste fuer das Steuerpult
    try:
        roster = json.loads((work / "roster.json").read_text(encoding="utf-8"))
        names = [r["name"] for r in roster["units"]]
    except (FileNotFoundError, json.JSONDecodeError):
        names = []
    results["J Einheitenliste mit Namen"] = ("Kavallerie 1" in names and "gegn. Artillerie 1" in names, ", ".join(names))

    # Chat-Befehl: landet als Sofortbefehl im naechsten Lagebericht
    (bridge / "commands.txt").write_text("order player Kavallerie 1 greift gegn. Artillerie 1 an\n", encoding="utf-8")
    t_end = time.monotonic() + 20
    found = False
    while time.monotonic() < t_end and not found:
        run(1)
        logs = sorted((HERE / "logs").glob("battle_*.jsonl"), key=lambda p: p.stat().st_mtime)
        txt = logs[-1].read_text(encoding="utf-8") if logs else ""
        found = "IMMEDIATE ORDERS" in txt and "Kavallerie 1 greift gegn. Artillerie 1 an" in txt
    results["K Chat-Befehl im Lagebericht"] = (found, "gefunden" if found else "nicht im Bericht")

    # erwartet: die Uebernahme der gruppierten Einheit wird abgelehnt - das ist kein Fehler
    bad = [r for r in state()["order_results"] if not r["ok"] and "another group" not in (r.get("err") or "")]
    results["E keine Befehlsfehler"] = (not bad, f"{len(bad)} Fehler" + (f": {bad[:2]}" if bad else ""))

    g.SIM_phase("Complete")
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
    out.close()
    print((work / "commander.out").read_text(encoding="utf-8"))
    ok = True
    for k, (passed, detail) in results.items():
        ok &= passed
        print(f"{'PASS' if passed else 'FAIL'}  {k}: {detail}")
    print("ERGEBNIS:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
