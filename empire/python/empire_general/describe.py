"""Lagebericht fuer den Feldherrn (eine Nachricht pro Plan). Kompakt, aber vollstaendig.

Angelehnt an rome_ai/describe.py (rome-battle-ai, MIT). Enthaelt eine einfache Vorhersage:
welche feindliche Einheit bei gleichbleibendem Kurs in den naechsten 30 s welche eigene erreicht.
"""
from __future__ import annotations

from .plan import Plan
from .units import frontage_m, is_cavalry
from .world import Frame, Unit, World, bearing_to, centroid, compass, dist


def _unit_line(w: World, u: Unit, foes: list[Unit], task: str | None = None, fr: Frame | None = None) -> str:
    s = (f"#{u.id} '{u.name}' {u.role} ({u.type}) men {u.men}/{u.men_start} at ({u.x:.0f},{u.z:.0f}) "
         f"height {u.y:.0f} m, frontage ~{frontage_m(u.role, u.men):.0f} m, facing {u.bearing:.0f}")
    if fr is not None:
        back = fr.front - fr.depth(u.pos)
        where = f"{back:.0f} m behind your front" if back >= 0 else f"{-back:.0f} m in front of your front"
        s += f" [{where}, {fr.lateral(u.pos):+.0f} m right of centre]"
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


def _frame_lines(w: World, alliance: int, fr: Frame | None) -> list[str]:
    """Bezugssystem der Armee und Hoehenverlauf (das Spiel liefert kein Gelaende, nur die Hoehe unter jeder Einheit)."""
    if fr is None:
        return []
    fp = fr.point(fr.front, 0.0)
    out = [f"ARMY FRAME: your army faces {fr.face:.0f} deg ({compass(fr.face)}, towards the enemy). Your current front "
           f"line (foremost units) runs through ({fp[0]:.0f},{fp[1]:.0f}); your rearmost units stand "
           f"{fr.front - fr.rear:.0f} m behind it. 'Behind' = away from the enemy, 'right' = right as your troops "
           "face the enemy. Positions in [ ] below are in this frame - use ref/back_m/right_m to place groups."]
    us = [u for u in w.side(alliance)]
    if len(us) >= 3:
        hi = max(us, key=lambda u: u.y)
        # Hoehe ~ a + b*Tiefe + c*Seite (kleinste Quadrate) -> grobe Neigung des Gelaendes
        n = len(us)
        ds = [fr.depth(u.pos) for u in us]; ls = [fr.lateral(u.pos) for u in us]; ys = [u.y for u in us]
        md, ml, my = sum(ds) / n, sum(ls) / n, sum(ys) / n
        sdd = sum((d - md) ** 2 for d in ds); sll = sum((l - ml) ** 2 for l in ls)
        sdl = sum((d - md) * (l - ml) for d, l in zip(ds, ls))
        sdy = sum((d - md) * (y - my) for d, y in zip(ds, ys)); sly = sum((l - ml) * (y - my) for l, y in zip(ls, ys))
        det = sdd * sll - sdl * sdl
        trend = ""
        if abs(det) > 1e-6:
            b = (sdy * sll - sly * sdl) / det
            c = (sly * sdd - sdy * sdl) / det
            parts = []
            if abs(b) * 100 >= 1:
                parts.append(f"{abs(b) * 100:.0f} m per 100 m {'towards the enemy' if b > 0 else 'towards your rear'}")
            if abs(c) * 100 >= 1:
                parts.append(f"{abs(c) * 100:.0f} m per 100 m {'to your right' if c > 0 else 'to your left'}")
            if parts:
                trend = " Ground rises " + " and ".join(parts) + " (estimated from unit heights)."
            else:
                trend = " The ground under your army is roughly level."
        out.append(f"TERRAIN: not reported by the game - only the ground height under each unit is known (heights "
                   f"{min(ys):.0f}..{max(ys):.0f} m; highest: '{hi.name}' at {hi.y:.0f} m).{trend} A hill crest "
                   "can only be estimated from these heights.")
    return out


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
             reflex_status: list[str], losses: tuple[int, int] | None, errors: list[str],
             immediate: list[tuple[float, str]] | None = None) -> str:
    mine = sorted(w.side(alliance, alive=False), key=lambda u: u.id)
    foes = sorted(w.enemies(alliance, alive=False), key=lambda u: u.id)
    mine_alive = [u for u in mine if u.alive]
    foes_alive = [u for u in foes if u.alive]
    lines = [f"TIME {w.time:.0f} s, phase {w.phase}. You command alliance {alliance}."]
    if immediate:
        lines.append("")
        lines.append("IMMEDIATE ORDERS FROM YOUR SOVEREIGN (given during the battle - top priority, carry them out NOW "
                     "and keep following them until done or replaced; units are named as in the unit list, e.g. "
                     "'Kavallerie 3', enemy units with 'gegn.'; descriptions like 'the enemy guns on the left' refer to "
                     "the positions in the lists below, left/right as seen by your army facing the enemy). Start your "
                     "intent with a one-line acknowledgement in German of how you carry out the newest order:")
        for t0, text in immediate:
            lines.append(f"  - at {t0:.0f} s ({w.time - t0:.0f} s ago): {text}")
        lines.append("")
    cm, ce = centroid(mine_alive), centroid(foes_alive)
    if cm and ce:
        lines.append(f"Your army centre ({cm[0]:.0f},{cm[1]:.0f}), enemy centre ({ce[0]:.0f},{ce[1]:.0f}): "
                     f"{dist(cm, ce):.0f} m, enemy lies {compass(bearing_to(cm, ce))} ({bearing_to(cm, ce):.0f} deg) of you.")
    lines.append(f"Strength: yours {sum(u.men for u in mine)} men in {len(mine_alive)} fighting units, "
                 f"enemy {sum(u.men for u in foes)} men in {len(foes_alive)} fighting units.")
    if losses:
        lines.append(f"Losses since your last plan: yours {losses[0]} men, enemy {losses[1]} men.")
    lines.append("")
    fr = w.frame(alliance)
    lines += _frame_lines(w, alliance, fr)
    lines.append("")
    lines.append("YOUR UNITS:")
    lines += ["  " + _unit_line(w, u, foes_alive, tasks.get(u.id), fr) for u in mine]
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
        f"The deployment zone is limited and its edges are unknown: stay in the area your army occupies now - "
        "not beyond your current front towards the enemy, at most about 100 m beyond your outer wings and "
        f"{DEPLOY_RADIUS_M:.0f} m behind your rearmost units (the subordinate pulls other targets back). Shifting "
        "units sideways within the army is fine. Rows further back (second line, reserves, "
        "artillery behind the line) lie BEHIND the front, i.e. away from the enemy: place them with ref 'front' or a "
        "unit and a positive back_m. Use only move tasks (no attack, no withdraw). Face the enemy unless the orders "
        "say otherwise.",
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
    fr = w.frame(alliance)
    lines += _frame_lines(w, alliance, fr)
    lines.append("")
    lines.append("YOUR UNITS:")
    lines += ["  " + _unit_line(w, u, [], fr=fr) for u in mine]
    lines.append("ENEMY UNITS (as far as visible):")
    lines += ["  " + _unit_line(w, u, [], fr=fr) for u in foes]
    return "\n".join(lines)


def situation_summary(w: World, alliance: int) -> str:
    """Lagebild fuer die Pruefung der Prompts im Steuerpult: Armee, Bezugssystem, Gelaendehoehen, Feind."""
    mine = sorted(w.side(alliance, alive=False), key=lambda u: u.id)
    foes = sorted(w.enemies(alliance, alive=False), key=lambda u: u.id)
    if not mine:
        return ""
    fr = w.frame(alliance)
    who = "the PLAYER'S army" if alliance == (w.player_alliance or 1) else "the ENEMY army (the player's opponent)"
    lines = [f"Alliance {alliance} = {who}; phase {w.phase}, time {w.time:.0f} s. "
             f"{len([u for u in mine if u.alive])} units, {sum(u.men for u in mine)} men."]
    if fr is not None:
        widths = [fr.lateral(u.pos) for u in mine if u.alive]
        if widths:
            lines.append(f"The army is {max(widths) - min(widths):.0f} m wide and {fr.front - fr.rear:.0f} m deep.")
    lines += _frame_lines(w, alliance, fr)
    lines.append("UNITS:")
    lines += ["  " + _unit_line(w, u, [], fr=fr) for u in mine]
    lines.append("ITS ENEMY:")
    lines += ["  " + _unit_line(w, u, [], fr=fr) for u in foes]
    return "\n".join(lines)
