"""
selftest.py - Rundlauf-Test der Empire AI Bridge mit PASS/FAIL-Markern.

Starten, dann im Spiel eine Schlacht beginnen und laufen lassen (nichts beobachten noetig):
    python selftest.py
Das Skript wartet auf die Schlacht, steuert die von config.lua uebernommene Seite und prueft:
  A  Protokoll: Lagebild kommt an, Seite ist nach Schlachtbeginn uebernommen
  B  goto_location: Einheiten laufen 100 m seitlich (macht die Spiel-KI nie von sich aus)
  C  halt: Einheiten bleiben 20 s stehen
  D  Befehlsquittungen: alle Befehle ohne Fehler ausgefuehrt
  E  attack_unit (Info): eine Einheit greift die naechste feindliche Einheit an
  F  release: Kontrolle zurueck an die Spiel-KI
Ergebnis auf der Konsole und in ai_bridge/selftest_log.txt.
"""
from __future__ import annotations

import math
import sys
import time

from empire_bridge import EmpireBridge, Order, centroid, unit_by_id, units

SIDE_M = 100.0
br = EmpireBridge()
LOG = br.dir / "selftest_log.txt"
results: dict[str, str] = {}


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def mark(key: str, ok: bool | None, detail: str) -> None:
    val = "INFO" if ok is None else ("PASS" if ok else "FAIL")
    results[key] = val
    log(f"CHECK {key}: {val}  {detail}")


def finish() -> None:
    log("ERGEBNIS: " + " ".join(f"{k}={v}" for k, v in sorted(results.items())))
    sys.exit(0 if all(v != "FAIL" for v in results.values()) else 1)


log("=" * 60)
log(f"Selbsttest gestartet, Bruecken-Ordner {br.dir}")

# ---------- A: auf Schlacht warten ----------
old = br.dir / "state.json"
if old.exists():  # Lagebild einer frueheren Schlacht verwerfen, sonst startet der Test mit alten Daten
    old.unlink(missing_ok=True)
log("Warte auf eine neue Schlacht (state.json) ...")
st = br.wait_for_state(lambda s: True, timeout=None)
log(f"Lagebild empfangen: seq {st['seq']}, Phase {st['phase']}, Spielzeit {st['game_time']:.1f}")
log("Warte auf Schlachtbeginn (Phase Deployed) ...")
st = br.wait_for_state(lambda s: s["phase"] == "Deployed" and units(s, controlled=True), timeout=None)
ctrl = units(st, controlled=True)
log(f"Modus {st['control']}, Spieler-Allianz {st['player_alliance']} ({st['player_alliance_source']}), "
    f"uebernommen {st['controlled_alliances']}: {len(ctrl)} Einheiten")
mark("A", len(ctrl) > 0, f"{len(ctrl)} Einheiten unter Kontrolle der Bruecke")
if not ctrl:
    finish()

errors_total = 0


def send_and_ack(orders: list[Order], label: str) -> list[dict]:
    global errors_total
    seq = br.send(orders)
    res = br.wait_ack(seq, timeout=60) or []
    bad = [r for r in res if not r.get("ok")]
    errors_total += len(bad)
    log(f"{label}: seq {seq}, {len(res) - len(bad)}/{len(orders)} ok")
    for r in bad[:5]:
        log(f"   Fehler {r['unit']} {r['cmd']}: {r.get('err')}")
    return res


# ---------- B: 100 m seitlich ----------
ctrl_alliances = set(st["controlled_alliances"])
own = centroid(ctrl)
other = centroid([u for u in units(st) if not u.get("controlled")]) or (own[0], own[1] + 1)
dx, dz = other[0] - own[0], other[1] - own[1]
d = math.hypot(dx, dz) or 1.0
sx, sz = -dz / d * SIDE_M, dx / d * SIDE_M
start = {u["id"]: (u["x"], u["z"]) for u in ctrl}
targets = {u["id"]: (u["x"] + sx, u["z"] + sz) for u in ctrl}
send_and_ack([Order(u["id"], "goto_location", [{"x": targets[u["id"]][0], "z": targets[u["id"]][1]}]) for u in ctrl],
             "goto_location 100 m seitlich")
st = br.wait_game_time(40)
ok_n, n = 0, 0
for uid, (x0, z0) in start.items():
    u = unit_by_id(st, uid)
    if not u or not u.get("men") or u.get("routing"):
        continue
    n += 1
    mx, mz = u["x"] - x0, u["z"] - z0
    along = (mx * sx + mz * sz) / SIDE_M
    moved = math.hypot(mx, mz)
    cos = along / moved if moved > 0 else 0
    if along >= 25 and cos >= 0.866:
        ok_n += 1
mark("B", n > 0 and ok_n >= 0.8 * n, f"{ok_n}/{n} Einheiten >= 25 m in Zielrichtung (<= 30 Grad) nach 40 s")

# ---------- C: halt ----------
send_and_ack([Order(u["id"], "halt") for u in units(st, controlled=True)], "halt")
st = br.wait_game_time(3)
snap = {u["id"]: (u["x"], u["z"]) for u in units(st, controlled=True)}
st = br.wait_game_time(20)
still, n, maxd = 0, 0, 0.0
for uid, (x0, z0) in snap.items():
    u = unit_by_id(st, uid)
    if not u or not u.get("men") or u.get("routing"):
        continue
    n += 1
    dd = math.hypot(u["x"] - x0, u["z"] - z0)
    maxd = max(maxd, dd)
    still += dd <= 3
# Unter Beschuss ordnen sich Einheiten leicht neu (Lauf 7: max. 5,6 m). Eingreifen der Spiel-KI
# waeren Marschbewegungen von zig Metern -> PASS, solange keine Einheit mehr als 15 m wandert.
mark("C", n > 0 and maxd <= 15, f"{still}/{n} Einheiten <= 3 m, max. {maxd:.1f} m in 20 s (Grenze 15 m)")

# ---------- E: attack_unit (Info) ----------
ctrl_ids = {u["id"] for al in st["alliances"] if al["index"] in ctrl_alliances for u in al["units"]}
enemies = [u for u in units(st) if u["id"] not in ctrl_ids]
mine = units(st, controlled=True)
if enemies and mine:
    att = mine[0]
    tgt = min(enemies, key=lambda e: math.hypot(e["x"] - att["x"], e["z"] - att["z"]))
    d0 = math.hypot(tgt["x"] - att["x"], tgt["z"] - att["z"])
    res = send_and_ack([Order(att["id"], "attack_unit", [{"unit": tgt["id"]}, True, False])], "attack_unit")
    st = br.wait_game_time(30)
    a2, t2 = unit_by_id(st, att["id"]), unit_by_id(st, tgt["id"])
    d1 = math.hypot(t2["x"] - a2["x"], t2["z"] - a2["z"])
    mark("E", None, f"{att['id']} ({att['type']}) -> {tgt['id']} ({tgt['type']}): Abstand {d0:.0f} m -> {d1:.0f} m, "
                    f"Ziel-Mannstaerke {tgt['men']} -> {t2['men']}, Quittung {'ok' if res and res[0]['ok'] else res}")
else:
    mark("E", None, "keine passende Einheit/Ziel gefunden")

# ---------- F: release ----------
send_and_ack([Order(u["id"], "release") for u in units(st, controlled=True, alive=False)], "release")
st = br.wait_game_time(2)
left = units(st, controlled=True, alive=False)
mark("F", len(left) == 0, f"{len(left)} Einheiten noch unter Kontrolle")

mark("D", errors_total == 0, f"{errors_total} fehlgeschlagene Befehle insgesamt")
pc = st.get("player_commands") or []
log(f"Info: {len(pc)} Spielerbefehle protokolliert" + (f", z. B. {pc[-1]}" if pc else ""))
finish()
