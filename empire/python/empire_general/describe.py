"""Lagebericht fuer den Feldherrn (eine Nachricht pro Plan). Kompakt, aber vollstaendig.

Angelehnt an rome_ai/describe.py (rome-battle-ai, MIT). Enthaelt eine einfache Vorhersage:
welche feindliche Einheit bei gleichbleibendem Kurs in den naechsten 30 s welche eigene erreicht.
"""
from __future__ import annotations

from .plan import Plan
from .units import is_cavalry
from .world import Unit, World, bearing_to, centroid, compass, dist


def _unit_line(w: World, u: Unit, foes: list[Unit], task: str | None = None) -> str:
    s = f"#{u.id} {u.role} ({u.type}) men {u.men}/{u.men_start} at ({u.x:.0f},{u.z:.0f}) facing {u.bearing:.0f}"
    if u.speed >= 0.5:
        s += f", moving {compass(bearing_to((0, 0), (u.vx, u.vz)))} {u.speed:.1f} m/s"
    else:
        s += ", standing"
    if u.missile_range:
        s += f", range {u.missile_range:.0f} m"
    if u.routing:
        s += ", ROUTING"
    if u.leaving:
        s += ", LEAVING"
    if u.men == 0:
        s += ", DESTROYED"
    if u.manual:
        s += " | COMMANDED BY THE PLAYER - do not give it orders"
    elif u.grouped:
        s += " | IN A PLAYER GROUP, NOT UNDER YOUR COMMAND - do not give it orders"
    if task:
        s += f" | {task}"
    if u.alive and foes:
        n, d = w.nearest(u.pos, foes)
        if n:
            s += f" | nearest #{n.id} {n.role} {d:.0f} m {compass(bearing_to(u.pos, n.pos))}"
    return s


def _predictions(w: World, mine: list[Unit], foes: list[Unit]) -> list[str]:
    out = []
    for e in foes:
        if e.speed < 0.8:
            continue
        best = None
        for m in mine:
            dx, dz = m.x - e.x, m.z - e.z
            d = (dx * dx + dz * dz) ** 0.5
            if d < 1:
                continue
            closing = (e.vx * dx + e.vz * dz) / d  # Annaeherungstempo
            if closing <= 0.5:
                continue
            eta = d / closing
            if eta <= 30 and (best is None or eta < best[0]):
                best = (eta, m, d)
        if best:
            eta, m, d = best
            # Trifft er Front, Flanke oder Ruecken? Vergleich Anmarschrichtung mit Blickrichtung der eigenen Einheit
            approach = bearing_to(m.pos, e.pos)
            rel = (approach - m.bearing + 360) % 360
            side = "front" if rel <= 45 or rel >= 315 else ("rear" if 135 <= rel <= 225 else "flank")
            kind = "CAVALRY " if is_cavalry(e.role) else ""
            out.append(f"enemy {kind}#{e.id} {e.role} closes on your #{m.id} ({d:.0f} m) - contact in ~{eta:.0f} s into its {side}")
    return sorted(out)


def describe(w: World, alliance: int, *, previous: Plan | None, notes: list[str], tasks: dict[int, str],
             reflex_status: list[str], losses: tuple[int, int] | None, errors: list[str]) -> str:
    mine = sorted(w.side(alliance, alive=False), key=lambda u: u.id)
    foes = sorted(w.enemies(alliance, alive=False), key=lambda u: u.id)
    mine_alive = [u for u in mine if u.alive]
    foes_alive = [u for u in foes if u.alive]
    lines = [f"TIME {w.time:.0f} s, phase {w.phase}. You command alliance {alliance}."]
    cm, ce = centroid(mine_alive), centroid(foes_alive)
    if cm and ce:
        lines.append(f"Your army centre ({cm[0]:.0f},{cm[1]:.0f}), enemy centre ({ce[0]:.0f},{ce[1]:.0f}): "
                     f"{dist(cm, ce):.0f} m, enemy lies {compass(bearing_to(cm, ce))} ({bearing_to(cm, ce):.0f} deg) of you.")
    lines.append(f"Strength: yours {sum(u.men for u in mine)} men in {len(mine_alive)} fighting units, "
                 f"enemy {sum(u.men for u in foes)} men in {len(foes_alive)} fighting units.")
    if losses:
        lines.append(f"Losses since your last plan: yours {losses[0]} men, enemy {losses[1]} men.")
    lines.append("")
    lines.append("YOUR UNITS:")
    lines += ["  " + _unit_line(w, u, foes_alive, tasks.get(u.id)) for u in mine]
    lines.append("ENEMY UNITS:")
    lines += ["  " + _unit_line(w, u, mine_alive) for u in foes]
    pred = _predictions(w, mine_alive, foes_alive)
    lines.append("PREDICTED (if everyone keeps course):")
    lines += ["  " + p for p in pred] if pred else ["  no contact expected within 30 s"]
    if previous:
        lines.append(f"YOUR CURRENT PLAN: {previous.intent}")
    if reflex_status:
        lines.append("YOUR REFLEXES: " + "; ".join(reflex_status))
    if errors:
        lines.append("ORDER ERRORS (the game rejected these): " + "; ".join(errors[-5:]))
    if notes:
        lines.append("YOUR LAST NOTES: " + " | ".join(notes[-3:]))
    return "\n".join(lines)


DEPLOY_RADIUS_M = 150.0


def describe_deployment(w: World, alliance: int, deploy_orders: str) -> str:
    """Bericht fuer die Aufstellung (vor Schlachtbeginn). Die Grenzen des Aufstellungsbereichs liefert die
    Spiel-API nicht; deshalb bleibt jede Einheit in der Naehe ihrer Startposition."""
    mine = sorted(w.side(alliance, alive=False), key=lambda u: u.id)
    foes = sorted(w.enemies(alliance, alive=False), key=lambda u: u.id)
    cm, ce = centroid(mine), centroid(foes)
    lines = [
        "DEPLOYMENT PHASE - the battle has not started yet. Deploy your army: give every unit a 'move' task to "
        "its starting position, in the shapes you want (lines, a second line, reserves, wings, artillery position). "
        f"The deployment zone is limited and its edges are unknown: keep every target point within about "
        f"{DEPLOY_RADIUS_M:.0f} m of the area your army occupies now, and do not move towards the enemy beyond your "
        "army's current front. Use only move tasks (no attack, no withdraw). Face the enemy unless the orders say otherwise.",
        f"You command alliance {alliance}.",
    ]
    if cm and ce:
        lines.append(f"Your army centre ({cm[0]:.0f},{cm[1]:.0f}), enemy centre ({ce[0]:.0f},{ce[1]:.0f}): "
                     f"{dist(cm, ce):.0f} m, the enemy lies {compass(bearing_to(cm, ce))} ({bearing_to(cm, ce):.0f} deg) of you.")
    if mine:
        xs, zs = [u.x for u in mine], [u.z for u in mine]
        lines.append(f"Your units currently stand between x {min(xs):.0f}..{max(xs):.0f} and z {min(zs):.0f}..{max(zs):.0f}.")
    if deploy_orders.strip():
        lines.append("")
        lines.append("DEPLOYMENT ORDERS FROM YOUR SOVEREIGN (follow them):")
        lines.append(deploy_orders.strip())
    lines.append("")
    lines.append("YOUR UNITS:")
    lines += ["  " + _unit_line(w, u, []) for u in mine]
    lines.append("ENEMY UNITS (as far as visible):")
    lines += ["  " + _unit_line(w, u, []) for u in foes]
    return "\n".join(lines)
