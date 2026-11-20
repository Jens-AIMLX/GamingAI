"""Lagebericht fuer den Feldherrn (eine Nachricht pro Plan). Kompakt, aber vollstaendig.

Zwei Teile (Token sparen): battle_static() - Leseanleitung und Stammdaten jeder Einheit, fuer die ganze Schlacht
byte-gleich und deshalb im Prompt-Cache (kostet beim Lesen 1/20) - und describe(), die kurze Lage je Plan, die
sich auf die Stammdaten bezieht. Aufstellung und Lagebild fuer die Prompt-Pruefung bleiben ausfuehrlich (selten).

Angelehnt an rome_ai/describe.py (rome-battle-ai, MIT). Enthaelt eine einfache Vorhersage:
welche feindliche Einheit bei gleichbleibendem Kurs in den naechsten 30 s welche eigene erreicht.
"""
from __future__ import annotations

from . import firelanes
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
        s += (" | COMMANDED BY THE PLAYER - do not give it orders; if the sovereign hands it back to you (also with "
              "'take everything back' / 'du übernimmst wieder alles'), put it into take_from_player")
    elif u.grouped:
        s += " | IN A PLAYER GROUP, NOT UNDER YOUR COMMAND - do not give it orders"
    if task:
        s += f" | {task}"
    if u.alive and foes:
        n, d = w.nearest(u.pos, foes)
        if n:
            s += f" | nearest #{n.id} {n.role} {d:.0f} m {compass(bearing_to(u.pos, n.pos))}"
    return s


REPORT_GUIDE = """HOW TO READ YOUR SITUATION REPORTS (fixed for this battle):
- The UNIT ROSTER below gives each unit's fixed data once: id, name, kind (role and game unit key), men at the \
start and missile range. The situation reports refer to units by '#id name'.
- A unit line in a report: '#id name: <men now> men (x,z) h<ground height m> face <deg> width <frontage m> \
[<b> back, <r> right]', then the movement ('moving SE 3.1 m/s'; no movement note = standing still), state marks, \
' | ' its current task from your plan, and ' | near #id <m> <direction>' = the nearest unit of the other side. \
[b back, r right] is the position in your ARMY FRAME: metres behind your front line (negative = in front of it, \
towards the enemy) and metres right of your army centre. Enemy lines have no frame position.
- Marks: ROUTING, LEAVING, DESTROYED = broken units. PLAYER = commanded by your sovereign: do not give it orders; \
if the sovereign hands it back to you (also with 'take everything back' / 'du \u00fcbernimmst wieder alles'), put it \
into take_from_player. PLAYER GROUP = in a player group, not under your command: do not give it orders.
- IMMEDIATE ORDERS FROM YOUR SOVEREIGN (when present) were given during the battle: top priority, carry them out \
NOW and keep following them until done or replaced; units are named as in the roster, enemy units with 'gegn.'; \
descriptions like 'the enemy guns on the left' refer to the positions in the report, left/right as seen by your \
army facing the enemy. Start your intent with a one-line acknowledgement in German of how you carry out the \
newest order.
- POSITIONS NOW: resolve 'vordere/frontale/vorderste', 'hintere/hinterste', 'linke', 'rechte' ONLY with these \
lists; values are metres relative to your front line (+ = in front of it towards the enemy) resp. right of centre.
- LINES OF FIRE of your field guns: corridors ~{lane_w:.0f} m wide from the gun to its target - keep every unit OUT \
of them, also on the way: route behind the guns or beside the corridor; the subordinate moves places and routes \
out of them and a gun holds fire while own troops stand in its corridor; standing infantry within {screen:.0f} m in \
front of a gun is fired over and does not count. 'band right_m a..b' = on your side, from the gun up to your \
front, no unit may be placed with right_m in that band.
- SAFETY ZONE: no unit of yours (also generals and reserves) may stand or pass closer than the given distance to \
your own guns; the subordinate moves places that are too close."""


def _roster_line(u: Unit) -> str:
    s = f"#{u.id} {u.name} - {u.role} ({u.type}), {u.men_start} men at start"
    if u.missile_range:
        s += f", range {u.missile_range:.0f} m"
    return s


def battle_static(w: World, alliance: int) -> str:
    """Fester Teil fuer die ganze Schlacht (Prompt-Cache): Leseanleitung + Stammdaten aller bekannten Einheiten.
    Muss byte-gleich bleiben - deshalb nur einmal je Schlacht und Seite bilden."""
    mine = sorted(w.side(alliance, alive=False), key=lambda u: u.id)
    foes = sorted(w.enemies(alliance, alive=False), key=lambda u: u.id)
    lines = [REPORT_GUIDE.format(lane_w=2 * firelanes.HALF_M, screen=firelanes.SCREEN_M), "",
             f"UNIT ROSTER - you command alliance {alliance}.", "YOUR UNITS:"]
    lines += ["  " + _roster_line(u) for u in mine]
    lines.append("ENEMY UNITS:")
    lines += ["  " + _roster_line(u) for u in foes]
    return "\n".join(lines)


def _brief_line(w: World, u: Unit, others: list[Unit], known: set[int], task: str | None = None,
                fr: Frame | None = None) -> str:
    """Kurze Lagezeile; die festen Daten stehen in den Stammdaten (battle_static)."""
    s = f"#{u.id} {u.name}: {u.men} men ({u.x:.0f},{u.z:.0f}) h{u.y:.0f} face {u.bearing:.0f} " \
        f"width {frontage_m(u.role, u.men):.0f} m"
    if u.id not in known:                      # nach Schlachtbeginn hinzugekommen: Stammdaten hier
        s += f" (new unit: {u.role}, {u.type}, {u.men_start} men at start"
        s += f", range {u.missile_range:.0f} m)" if u.missile_range else ")"
    if fr is not None:
        s += f" [{fr.front - fr.depth(u.pos):.0f} back, {fr.lateral(u.pos):+.0f} right]"
    if u.speed >= 0.5:
        s += f" moving {compass(bearing_to((0, 0), (u.vx, u.vz)))} {u.speed:.1f} m/s"
    for flag, mark in ((u.routing, "ROUTING"), (u.leaving, "LEAVING"), (u.men == 0, "DESTROYED")):
        if flag:
            s += " " + mark
    if u.manual:
        s += " PLAYER"
    elif u.grouped:
        s += " PLAYER GROUP"
    if task:
        s += f" | {task}"
    if u.alive and others:
        n, d = w.nearest(u.pos, others)
        if n:
            s += f" | near #{n.id} {d:.0f} m {compass(bearing_to(u.pos, n.pos))}"
    return s


def _frame_lines(w: World, alliance: int, fr: Frame | None) -> list[str]:
    """Bezugssystem der Armee und Hoehenverlauf (das Spiel liefert kein Gelaende, nur die Hoehe unter jeder Einheit)."""
    if fr is None:
        return []
    fp = fr.point(fr.front, 0.0)
    out = [f"ARMY FRAME: your army faces {fr.face:.0f} deg ({compass(fr.face)}, towards the enemy). Your current front "
           f"line (foremost infantry/artillery of your main body; cavalry and units far ahead do not count) runs "
           f"through ({fp[0]:.0f},{fp[1]:.0f}); the rearmost infantry/artillery ('rear') stand "
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


ARMS = [("cavalry", "Kavallerie"), ("infantry", "Infanterie"), ("field guns", "Feldartillerie"),
        ("mortars", "Moerser/Fernartillerie"), ("generals", "Generaele")]
SPLIT_DEPTH_M = 40.0      # Luecke von vorn nach hinten, ab der zwei Gruppen entstehen
SPLIT_SIDE_M = 60.0


def _arm(u: Unit) -> str:
    if u.role == "general":
        return "generals"
    if is_cavalry(u.role):
        return "cavalry"
    if u.role == "artillery":
        return "mortars" if "mortar" in u.type.lower() else "field guns"
    return "infantry"


def _groups(units: list[Unit], key, gap: float) -> list[list[Unit]]:
    out: list[list[Unit]] = []
    for u in sorted(units, key=key):
        if out and key(u) - key(out[-1][-1]) > gap:
            out.append([u])
        elif out:
            out[-1].append(u)
        else:
            out.append([u])
    return out


def _labels(n: int, first: str, mid: str, last: str) -> list[str]:
    if n == 2:
        return [first, last]
    return [first] + [mid] * (n - 2) + [last]


def position_lines(w: World, alliance: int, fr: Frame | None) -> list[str]:
    """Lage JETZT je Waffengattung, von vorn nach hinten und von links nach rechts in Gruppen - damit "vordere",
    "hintere", "linke" Kavallerie immer nach der aktuellen Lage aufgeloest werden (Schlacht 2026-11-20 09:32:
    vordere/hintere Kavallerie genau vertauscht)."""
    if fr is None:
        return []
    out = ["POSITIONS NOW:"]
    def tag(u: Unit) -> str:
        d = fr.depth(u.pos) - fr.front
        own = " [player]" if u.manual else ""
        return f"{u.name}{own} ({d:+.0f} m)"
    def tag_side(u: Unit) -> str:
        own = " [player]" if u.manual else ""
        return f"{u.name}{own} ({fr.lateral(u.pos):+.0f} m)"
    for arm, label in ARMS:
        us = [u for u in w.side(alliance) if _arm(u) == arm]
        if len(us) < 2:
            continue
        front = _groups(us, lambda u: -fr.depth(u.pos), SPLIT_DEPTH_M)
        if len(front) > 1:
            names = _labels(len(front), "FRONT group (vordere)", "middle group", "REAR group (hintere)")
            out.append(f"  {label} front to back: " + " | ".join(f"{n}: " + ", ".join(tag(u) for u in g)
                                                                for n, g in zip(names, front)))
        else:
            o = sorted(us, key=lambda u: -fr.depth(u.pos))
            out.append(f"  {label} front to back: one group, no clear front/rear split - " + ", ".join(tag(u) for u in o))
        side = _groups(us, lambda u: fr.lateral(u.pos), SPLIT_SIDE_M)
        if len(side) > 1:
            names = _labels(len(side), "LEFT group (linke)", "centre group", "RIGHT group (rechte)")
            out.append(f"  {label} left to right: " + " | ".join(f"{n}: " + ", ".join(tag_side(u) for u in g)
                                                                for n, g in zip(names, side)))
    return out


def lane_lines(w: World, alliance: int, ls: list) -> list[str]:
    """Schusskanaele der eigenen Feldgeschuetze und wer gerade darin steht."""
    if not ls:
        return []
    out = ["LINES OF FIRE:"]
    for l in ls:
        inside = [u for u in w.side(alliance) if firelanes.blocking(u, l)]
        tgt = f"{l.target.name} (#{l.target.id})" if l.target else "?"
        who = ", ".join(u.name + (" [player]" if u.manual else "") for u in inside) or "clear"
        line = f"  {l.gun.name} -> {tgt}, {l.length:.0f} m: {'IN IT NOW: ' + who if inside else 'clear'}"
        fr = w.frame(alliance)
        if fr is not None:
            # Lage im Bezugssystem der Armee: welche Plaetze vor dem Geschuetz (bis zur eigenen Front) im Kanal liegen -
            # damit das Modell z. B. Generaele nicht "32 m hinter der Front" genau vor das Geschuetz stellt
            gun_back = fr.front - fr.depth(l.gun.pos)
            lat = [fr.lateral(l.start)]
            if gun_back > 0:
                k = min(gun_back / max(fr.depth(l.end) - fr.depth(l.start), 1e-6), 1.0)
                lat.append(fr.lateral((l.start[0] + (l.end[0] - l.start[0]) * k, l.start[1] + (l.end[1] - l.start[1]) * k)))
            lo, hi = min(lat) - firelanes.HALF_M, max(lat) + firelanes.HALF_M
            line += f" | band right_m {lo:+.0f}..{hi:+.0f} from the gun ({gun_back:.0f} m behind your front) to the front"
            if lo < 0 < hi and gun_back > 0:
                line += f" (ref 'front' with right_m 0 and back_m 0..{gun_back:.0f} is IN it)"
        out.append(line)
    return out


def zone_lines(w: World, alliance: int, fr, r: float) -> list[str]:
    """Sperrzone um die eigenen Geschuetze (Steuerpult-Einstellung)."""
    gs = firelanes.guns(w, alliance)
    if r <= 0 or not gs:
        return []
    where = []
    for g in gs:
        if fr is not None:
            where.append(f"{g.name} [{fr.front - fr.depth(g.pos):.0f} m behind your front, {fr.lateral(g.pos):+.0f} m right]")
        else:
            where.append(g.name)
    return [f"SAFETY ZONE {r:.0f} m around: " + "; ".join(where)]


def describe(w: World, alliance: int, *, previous: Plan | None, notes: list[str], tasks: dict[int, str],
             reflex_status: list[str], losses: tuple[int, int] | None, errors: list[str],
             immediate: list[tuple[float, str]] | None = None, lanes: list | None = None,
             gun_zone: float = 0.0, known: set[int] | None = None) -> str:
    """Lage je Plan. known = Einheiten, deren Stammdaten im festen Teil (battle_static) stehen."""
    known = known or set()
    mine = sorted(w.side(alliance, alive=False), key=lambda u: u.id)
    foes = sorted(w.enemies(alliance, alive=False), key=lambda u: u.id)
    mine_alive = [u for u in mine if u.alive]
    foes_alive = [u for u in foes if u.alive]
    lines = [f"BATTLE TIME {w.battle_time:.0f} s since the battle started (phase {w.phase}). "
             f"You command alliance {alliance}."]
    if immediate:
        lines.append("")
        lines.append("IMMEDIATE ORDERS FROM YOUR SOVEREIGN (top priority, acknowledge the newest in German):")
        for t0, text in immediate:
            lines.append(f"  - at battle time {w.battle_time_at(t0):.0f} s ({w.time - t0:.0f} s ago): {text}")
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
    lines += position_lines(w, alliance, fr)
    lines += lane_lines(w, alliance, lanes if lanes is not None else firelanes.lanes(w, alliance))
    lines += zone_lines(w, alliance, fr, gun_zone)
    lines.append("")
    lines.append("YOUR UNITS:")
    lines += ["  " + _brief_line(w, u, foes_alive, known, tasks.get(u.id), fr) for u in mine]
    lines.append("ENEMY UNITS:")
    lines += ["  " + _brief_line(w, u, mine_alive, known) for u in foes]
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
    lines = [f"Alliance {alliance} = {who}; phase {w.phase}, battle time {w.battle_time:.0f} s. "
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
