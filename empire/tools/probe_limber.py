"""
probe_limber.py - Testschlacht: kann das Skript Artillerie aufprotzen und fahren lassen?

Ablauf (laufende Schlacht nach Schlachtbeginn, Feldherr im Steuerpult ANGEHALTEN, KI-Modus beliebig):
  1. erstes eigenes Geschuetz uebernehmen, Marschbefehl 60 m nach hinten -> faehrt es (ohne Aufprotzen)?
  2. perform_special_ability("unlimber") + derselbe Marschbefehl -> faehrt es jetzt?
  3. perform_special_ability("unlimber") erneut -> bleibt es stehen (wieder abgeprotzt)?
  4. Einheit an den Spieler zurueckgeben.
Ergebnis als PASS/FAIL je Schritt, mit Strecke und Tempo; Befehlsfehler des Spiels werden angezeigt.

    python ..\tools\probe_limber.py            (aus empire\python)
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))
from empire_bridge import EmpireBridge, Order, unit_by_id  # noqa: E402


def main() -> int:
    br = EmpireBridge()
    st = br.wait_for_state(lambda s: s.get("phase") == "Deployed", timeout=30)
    if not st:
        print("Keine laufende Schlacht (Phase Deployed) gefunden.")
        return 1
    player = st.get("player_alliance") or 1
    guns = [u for al in st["alliances"] if al["index"] == player for u in al["units"]
            if any(k in u["type"] for k in ("cannon", "lber", "howitzer", "mortar")) and u["men"] and not u["routing"]]
    if not guns:
        print("Kein eigenes Geschuetz gefunden.")
        return 1
    g = sorted(guns, key=lambda u: ("mortar" in u["type"], u["id"]))[0]
    bid = g["id"]
    b = math.radians(g["bearing"])
    back = {"x": g["x"] - math.sin(b) * 60, "z": g["z"] - math.cos(b) * 60}
    print(f"Geschuetz {bid} ({g['type']}) bei ({g['x']:.0f},{g['z']:.0f}), Ziel 60 m nach hinten ({back['x']:.0f},{back['z']:.0f})")

    def pos():
        u = unit_by_id(br.read_state(), bid)
        return u["x"], u["z"]

    def step(name, orders, wait_s):
        seq = br.send(orders)
        res = br.wait_ack(seq, timeout=20) or []
        errs = [f"{r['cmd']}: {r['err']}" for r in res if not r.get("ok")]
        p0 = pos()
        br.wait_game_time(wait_s, timeout=wait_s * 4 + 20)
        p1 = pos()
        d = math.dist(p0, p1)
        print(f"{name}: {d:.0f} m in {wait_s:.0f} s ({d / wait_s:.1f} m/s)" + (f"  FEHLER: {errs}" if errs else ""))
        return d, errs

    step("0 uebernehmen", [Order(bid, "take"), Order(bid, "halt")], 3)
    d1, _ = step("1 Marsch ohne Aufprotzen", [Order(bid, "goto_location", [back, False])], 15)
    d2, e2 = step("2 Aufprotzen + Marsch", [Order(bid, "perform_special_ability", ["unlimber"]),
                                            Order(bid, "goto_location", [back, False])], 15)
    d3, e3 = step("3 erneut 'unlimber' (Abprotzen?)", [Order(bid, "perform_special_ability", ["unlimber"])], 8)
    br.send([Order(bid, "release")])
    print()
    print(("PASS" if d1 < 5 else "INFO"), "ohne Aufprotzen bleibt das Geschuetz stehen" if d1 < 5 else "faehrt auch ohne Aufprotzen")
    print(("PASS" if d2 > 15 and not e2 else "FAIL"), "nach 'unlimber' faehrt es (aufgeprotzt)")
    print(("PASS" if d3 < 10 and not e3 else "FAIL"), "zweites 'unlimber' haelt es an (abgeprotzt)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
