"""Executor: setzt den Plan jede Sekunde in Einzelbefehle um und fuehrt die Reflexe aus.

Laeuft ohne Sprachmodell. Angelehnt an rome_ai/executor.py + reflexes.py (rome-battle-ai, MIT):
Formationspositionen, Wegpunkte, keep_range / engage_within, Ziel-Ersatz und bedingte Befehle.
Befehle gehen nur raus, wenn sich etwas aendert oder eine Einheit haengen bleibt (keine Befehlsflut).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from empire_bridge import Order

from .plan import GroupOrder, Plan, Reflex
from . import firelanes
from .units import frontage_m
from .world import Frame, Unit, World, bearing_to, centroid, dist

ARRIVE_M = 12.0
REISSUE_S = 6.0


def _dir(deg: float) -> tuple[float, float]:
    r = math.radians(deg)
    return math.sin(r), math.cos(r)          # Blickrichtung (x, z)


def _right(deg: float) -> tuple[float, float]:
    r = math.radians(deg)
    return math.cos(r), -math.sin(r)         # rechts aus Sicht der Truppe


@dataclass
class GroupRun:
    order: GroupOrder
    unit_ids: list[int]
    plan_time: float
    stages: dict[int, list[tuple[float, float]]] = field(default_factory=dict)   # Wegpunkte + Ziel je Einheit
    places: dict[int, tuple[float, float]] = field(default_factory=dict)         # Sollposition je Einheit
    alive_at_plan: set[int] = field(default_factory=set)
    enemies_alive_at_plan: set[int] = field(default_factory=set)
    fired: list[bool] = field(default_factory=list)
    override: tuple[Reflex, int | None] | None = None                             # (Reflex, ausloesender Feind)
    fallback_points: dict[int, tuple[float, float]] = field(default_factory=dict)


class Executor:
    def __init__(self, alliance: int) -> None:
        self.alliance = alliance
        self.plan: Plan | None = None
        self.groups: list[GroupRun] = []
        self.last: dict[int, tuple] = {}            # letzte gewuenschte Aktion je Einheit
        self.last_t: dict[int, float] = {}
        self.disabled: set[str] = set()             # Befehle, die das Spiel ablehnt
        self.fail_count: dict[str, int] = {}
        self.errors: list[str] = []
        self.plan_warnings: list[str] = []
        self._pending_flags: list[Order] = []
        self.cease_fire: set[int] = set()           # Artillerie, die wegen eigener Truppen am Ziel nicht feuert
        self.limber: dict[int, dict] = {}            # Auf-/Abprotz-Zustand je Geschuetz
        self.lanes: list = []                        # Schusskanaele der Feldgeschuetze (letzter Takt)
        self.reached: dict[int, list[tuple[float, float]]] = {}   # erreichte Wegpunkte je Einheit

    # ---------- neuer Plan ----------
    DEPLOY_MAX_M = 150.0      # Aufstellung: hoechstens so weit hinter die hinterste Einheit
    DEPLOY_SIDE_M = 100.0     # Aufstellung: hoechstens so weit seitlich ueber die aeusseren Fluegel hinaus
    DEPLOY_FRONT_M = 10.0     # Aufstellung: hoechstens so weit vor die aktuelle Front

    def set_plan(self, plan: Plan, w: World, deploy: bool = False) -> None:
        self.plan = plan
        self.groups = []
        self.plan_warnings = []
        mine = {u.id: u for u in w.side(self.alliance) if u.controlled}
        foes = w.enemies(self.alliance)
        ce = centroid(foes)
        fr = w.frame(self.alliance)
        taken: set[int] = set()
        resolved = self._resolve_relative(plan, w, mine, fr)
        for gi, g in enumerate(plan.groups):
            ids = []
            for uid in g.unit_ids:
                if uid in mine and uid not in taken:
                    ids.append(uid)
                    taken.add(uid)
                else:
                    self.plan_warnings.append(f"group '{g.name}': unit {uid} is not yours/alive or already in another group")
            if not ids:
                continue
            gr = GroupRun(order=g, unit_ids=ids, plan_time=w.time, alive_at_plan=set(ids),
                          enemies_alive_at_plan={u.id for u in foes}, fired=[False] * len(g.reflexes or []))
            target = None
            if g.task == "move" and gi in resolved:
                target = resolved[gi]
            elif g.task == "move" and g.target_point and len(g.target_point) >= 2:
                c = (float(g.target_point[0]), float(g.target_point[1]))
                face = g.facing_deg if g.facing_deg is not None else (bearing_to(c, ce) if ce else 0.0)
                target = (c, face, self._formation([mine[i] for i in ids], c, face, g.shape, g.spacing_m))
            if target:
                c, face, finals = target
                finals = {uid: finals[uid] for uid in ids if uid in finals}
                if deploy:
                    finals = self._deploy_guard(g.name, finals, mine, fr)
                close = 0
                for uid in ids:
                    off = (finals[uid][0] - c[0], finals[uid][1] - c[1])
                    wps = [] if deploy else [(float(p[0]) + off[0], float(p[1]) + off[1])
                                             for p in (g.waypoints or []) if len(p) >= 2]
                    # Neuplanung mit derselben Route: schon passierte Wegpunkte nicht noch einmal anlaufen
                    wps = self._skip_passed(uid, mine[uid].pos, wps)
                    # nur Rueckzuege pruefen (Ziel weiter vom Feind als jetzt) - Vormarsch an den Feind ist gewollt
                    if foes and w.nearest(finals[uid], foes)[1] > w.nearest(mine[uid].pos, foes)[1] + 20:
                        close += sum(1 for p in wps + [finals[uid]] if w.nearest(p, foes)[1] < self.ROUTE_WARN_M)
                    gr.stages[uid] = wps + [finals[uid]]
                    gr.places[uid] = finals[uid]
                if close and not deploy:
                    self.plan_warnings.append(
                        f"group '{g.name}': its retreat route passes within {self.ROUTE_WARN_M:.0f} m of enemy units "
                        "(musket range) - to get out of fire, first move straight away from the enemy, then sideways")
            else:
                for uid in ids:
                    gr.places[uid] = mine[uid].pos
                if g.task == "attack" and g.waypoints:
                    self._attack_route(gr, g, [mine[i] for i in ids], w)
            # Feuer-/Plaenkler-Einstellungen einmal je Plan
            for uid in ids:
                bid = mine[uid].bid
                if g.fire_at_will is not None and "fire_at_will" not in self.disabled:
                    self._pending_flags.append(Order(bid, "fire_at_will", [bool(g.fire_at_will)]))
                if g.skirmish is not None and "skirmish" not in self.disabled:
                    self._pending_flags.append(Order(bid, "skirmish", [bool(g.skirmish)]))
            self.groups.append(gr)
        if not deploy:              # in der Aufstellung schiesst niemand
            self._keep_out_of_lanes(w)
        # Neue Befehle sofort erzwingen
        self.last.clear()

    def _gun_targets(self) -> dict[int, int]:
        """Ziele der KI-gefuehrten Geschuetze: aus dem laufenden Befehl, sonst aus dem Plan"""
        out = {uid: want[1] for uid, want in self.last.items() if want and want[0] == "attack"}
        for g in self.groups:
            if g.order.task == "attack" and g.order.target_unit is not None:
                for uid in g.unit_ids:
                    out.setdefault(uid, g.order.target_unit)
        return out

    def _keep_out_of_lanes(self, w: World) -> None:
        """Plaetze und Wege der eigenen Einheiten aus den Schusskanaelen der Feldgeschuetze halten: Plaetze im Kanal
        werden seitlich herausgeschoben, Wege durch den Kanal bekommen einen Umweg (seitlich entlang oder hinter dem
        Geschuetz herum). Auch fuer attack: der Weg zum Ziel fuehrt nicht durch das eigene Schussfeld."""
        ls = firelanes.lanes(w, self.alliance, self._gun_targets())
        if not ls:
            return
        for gr in self.groups:
            moved = detoured = 0
            names: set[str] = set()
            for uid in gr.unit_ids:
                u = w.units.get(uid)
                if not u or u.role == "artillery":
                    continue
                own = [l for l in ls if l.gun.id != uid]
                if gr.order.task == "move" and uid in gr.places:
                    p = gr.places[uid]
                    for l in own:
                        near = (u.role not in ("general", "heavy_cavalry", "light_cavalry", "missile_cavalry")
                                and dist(p, l.gun.pos) <= firelanes.SCREEN_M)
                        if l.contains(p) and not near:
                            p = l.exit_point(p)
                            moved += 1
                            names.add(l.gun.name)
                    if p != gr.places[uid]:
                        gr.places[uid] = p
                        gr.stages[uid] = (gr.stages.get(uid) or [])[:-1] + [p]
                end = None
                if gr.order.task == "attack" and gr.order.target_unit in w.units:
                    end = w.units[gr.order.target_unit].pos
                stages = list(gr.stages.get(uid) or [])
                if gr.order.task not in ("move", "attack") or (not stages and end is None):
                    continue
                path = [u.pos] + stages + ([end] if end else [])
                new: list[tuple[float, float]] = []
                for a, b in zip(path, path[1:]):
                    for l in own:
                        det = l.detour(a, b)
                        if det:
                            new += det
                            detoured += 1
                            names.add(l.gun.name)
                            break
                    new.append(b)
                if end:
                    new = new[:-1]          # das Ziel selbst ist kein Wegpunkt
                if new != stages:
                    gr.stages[uid] = new
            if moved or detoured:
                self.plan_warnings.append(
                    f"group '{gr.order.name}': kept out of the line of fire of {', '.join(sorted(names))} "
                    f"({moved} place(s) moved beside it, {detoured} route(s) led around it)")

    def _attack_route(self, gr: GroupRun, g: GroupOrder, units: list[Unit], w: World) -> None:
        """attack mit Wegpunkten (Umfassung, Flankenangriff): jede Einheit laeuft erst die Wegpunkte ab - versetzt um
        ihren Abstand zur Gruppenmitte, damit die Gruppe nicht auf einen Punkt zusammenlaeuft - und greift dann an.
        Schon erreichte Wegpunkte entfallen bei Neuplanungen (_skip_passed)."""
        c = centroid(units)
        for u in units:
            off = (u.x - c[0], u.z - c[1]) if c else (0.0, 0.0)
            wps = [(float(p[0]) + off[0], float(p[1]) + off[1]) for p in g.waypoints or [] if len(p) >= 2]
            wps = self._skip_passed(u.id, u.pos, wps)
            if wps:
                gr.stages[u.id] = wps

    ROUTE_WARN_M = 100.0
    REACHED_M = 25.0

    def _skip_passed(self, uid: int, pos: tuple[float, float],
                     wps: list[tuple[float, float]]) -> list[tuple[float, float]]:
        """Neuplanung mit derselben Route: Wegpunkte, die die Einheit schon ERREICHT hat (oder an denen sie gerade
        steht), entfallen - sonst schickt jede Neuplanung sie zum ersten Wegpunkt zurueck. Wegpunkte, die sie noch nicht
        erreicht hat, bleiben immer, auch wenn sie "hinter" ihr liegen (Rueckzug: erst weg vom Feind, dann seitlich)."""
        reached = self.reached.get(uid, [])
        out = list(wps)
        while out and (dist(pos, out[0]) < ARRIVE_M * 2 or any(dist(out[0], r) < self.REACHED_M for r in reached)):
            out.pop(0)
        return out

    def _mark_reached(self, uid: int, p: tuple[float, float]) -> None:
        self.reached[uid] = (self.reached.get(uid, []) + [p])[-10:]

    def _resolve_relative(self, plan: Plan, w: World, mine: dict[int, Unit], fr: Frame | None
                          ) -> dict[int, tuple[tuple[float, float], float, dict[int, tuple[float, float]]]]:
        """Gruppen mit ref (Front, Nachhut oder Einheit) im Bezugssystem der Armee platzieren. Bezieht sich eine Gruppe
        auf eine Einheit, die im selben Plan verlegt wird, zaehlt deren neuer Platz (mehrere Durchlaeufe; bei
        Zirkelbezug die aktuelle Position)."""
        out: dict[int, tuple] = {}
        if fr is None:
            return out
        members: dict[int, list[int]] = {}
        owner: dict[int, int] = {}
        taken: set[int] = set()
        for gi, g in enumerate(plan.groups):
            ids = [u for u in g.unit_ids if u in mine and u not in taken]
            taken.update(ids)
            members[gi] = ids
            for u in ids:
                owner[u] = gi
        todo = [gi for gi, g in enumerate(plan.groups) if g.task == "move" and g.ref and members[gi]]
        planned: dict[int, tuple[float, float]] = {}
        force = False
        while todo:
            progress = False
            for gi in list(todo):
                g = plan.groups[gi]
                key = g.ref.strip().lower()
                half = 0.0
                if key == "front":
                    base_d, base_l = fr.front, 0.0
                elif key == "rear":
                    base_d, base_l = fr.rear, 0.0
                    if g.back_m < 0:
                        self.plan_warnings.append(
                            f"group '{g.name}': ref 'rear' with back_m {g.back_m:.0f} = {-g.back_m:.0f} m IN FRONT of "
                            "your rearmost units, i.e. towards the enemy. To pull units back use a POSITIVE back_m")
                else:
                    ru = w.find(g.ref)
                    if ru is None:
                        self.plan_warnings.append(f"group '{g.name}': unknown ref '{g.ref}' - placed relative to your front instead")
                        base_d, base_l = fr.front, 0.0
                    else:
                        og = owner.get(ru.id)
                        if og is not None and og != gi and og in todo and not force:
                            continue          # Bezugseinheit wird erst noch platziert
                        pos = planned.get(ru.id, ru.pos)
                        base_d, base_l = fr.depth(pos), fr.lateral(pos)
                        half = frontage_m(ru.role, ru.men) / 2
                face = g.facing_deg if g.facing_deg is not None else fr.face
                lat = base_l + (g.right_m + (half if g.right_m > 0 else -half) if g.right_m else 0.0)
                anchor = fr.point(base_d - g.back_m, lat)
                units = [mine[u] for u in members[gi]]
                if g.shape == "line" and len(units) > 1 and g.attach != "centre":
                    width = sum(frontage_m(u.role, u.men) for u in units) + g.spacing_m * (len(units) - 1)
                    rx, rz = _right(face)
                    s = width / 2 if g.attach == "left_end" else -width / 2
                    anchor = (anchor[0] + rx * s, anchor[1] + rz * s)
                finals = self._formation(units, anchor, face, g.shape, g.spacing_m)
                planned.update(finals)
                out[gi] = (anchor, face, finals)
                todo.remove(gi)
                progress = True
            if not progress:
                force = True           # Zirkelbezug: aktuelle Positionen verwenden
        return out

    def _deploy_guard(self, name: str, finals: dict[int, tuple[float, float]], mine: dict[int, Unit],
                      fr: Frame | None) -> dict[int, tuple[float, float]]:
        """Aufstellung: Ziele bleiben im Bereich, den die Armee heute einnimmt - nicht vor die aktuelle Front
        (Richtung Feind), hoechstens DEPLOY_MAX_M hinter die hinterste Einheit und hoechstens DEPLOY_SIDE_M seitlich
        ueber die aeusseren Fluegel hinaus. Seitliches Umgruppieren innerhalb der Armee bleibt erlaubt (eine 300 m
        breite Armee darf ihre Fluegel tauschen); sonst laufen Einheiten aus der Zone oder Reihen kippen nach vorn."""
        if fr is None:
            return dict(finals)
        out = {}
        ahead = back = side = 0
        for uid, p in finals.items():
            d, l = fr.depth(p), fr.lateral(p)
            if d > fr.front + self.DEPLOY_FRONT_M:
                d = fr.front + self.DEPLOY_FRONT_M
                ahead += 1
            if d < fr.rear - self.DEPLOY_MAX_M:
                d = fr.rear - self.DEPLOY_MAX_M
                back += 1
            if l < fr.left - self.DEPLOY_SIDE_M or l > fr.right_edge + self.DEPLOY_SIDE_M:
                l = min(max(l, fr.left - self.DEPLOY_SIDE_M), fr.right_edge + self.DEPLOY_SIDE_M)
                side += 1
            out[uid] = fr.point(d, l)
        if ahead:
            self.plan_warnings.append(f"deployment group '{name}': {ahead} target(s) lay beyond your front line towards "
                                      "the enemy and were pulled back to it")
        if back:
            self.plan_warnings.append(f"deployment group '{name}': {back} target(s) lay more than {self.DEPLOY_MAX_M:.0f} m "
                                      "behind your rearmost units and were moved forward")
        if side:
            self.plan_warnings.append(f"deployment group '{name}': {side} target(s) lay more than {self.DEPLOY_SIDE_M:.0f} m "
                                      "beyond your outer wings and were moved inwards")
        return out

    @staticmethod
    def _formation(units: list[Unit], c: tuple[float, float], face: float, shape: str, gap: float) -> dict[int, tuple[float, float]]:
        out: dict[int, tuple[float, float]] = {}
        if shape == "none" or len(units) == 1:
            return {u.id: c for u in units}
        # Plaetze nach der AKTUELLEN Position vergeben, nicht nach der Reihenfolge im Plan: sonst laufen Einheiten
        # quer durch die Linie, wenn das Modell sie gespiegelt aufzaehlt (z. B. Kartenreihenfolge bei Front nach Sueden
        # -> "Kreuzaufstellung", Schlacht 2026-11-19).
        if shape == "column":
            dx, dz = _dir(face)
            units = sorted(units, key=lambda u: -(u.x * dx + u.z * dz))      # vorderste Einheit an die Spitze
            for i, u in enumerate(units):
                out[u.id] = (c[0] - dx * i * max(gap, 20.0), c[1] - dz * i * max(gap, 20.0))
            return out
        rx, rz = _right(face)
        units = sorted(units, key=lambda u: u.x * rx + u.z * rz)               # links (aus Sicht der Truppe) zuerst
        fronts = [frontage_m(u.role, u.men) for u in units]
        width = sum(fronts) + gap * (len(units) - 1)
        pos = -width / 2
        for u, f in zip(units, fronts):
            off = pos + f / 2
            out[u.id] = (c[0] + rx * off, c[1] + rz * off)
            pos += f + gap
        return out

    # ---------- Rueckmeldungen der Bruecke ----------
    def note_results(self, results: list[dict], bids: set[str]) -> None:
        for r in results:
            if r.get("ok") or r.get("unit") not in bids:
                continue
            cmd = r.get("cmd") or "?"
            msg = f"{cmd} on {r.get('unit')}: {r.get('err')}"
            if msg not in self.errors:
                self.errors.append(msg)
                self.errors = self.errors[-20:]
            self.fail_count[cmd] = self.fail_count.get(cmd, 0) + 1
            if cmd in ("fire_at_will", "skirmish", "withdraw", "perform_special_ability") and self.fail_count[cmd] >= 2:
                self.disabled.add(cmd)

    SAFETY_M = 50.0

    def _artillery_safety(self, u: Unit, want: tuple, w: World) -> tuple:
        """Artillerie stellt das Feuer ein, solange eigene Einheiten naeher als SAFETY_M am Ziel sind
        (Nahkampf, eigene Linie davor) oder - bei Feldgeschuetzen - im Schusskanal zum Ziel stehen; sie feuert
        wieder, sobald es frei ist."""
        if u.role != "artillery" or want[0] != "attack":
            return want
        t = w.units.get(want[1])
        if not t:
            return want
        lane = None
        if firelanes.is_direct_fire(u):
            lane = next((l for l in firelanes.lanes(w, self.alliance, {u.id: t.id}) if l.gun.id == u.id), None)
        for o in w.side(self.alliance):
            if o.id == u.id or o.role == "artillery":
                continue
            if dist(o.pos, t.pos) < self.SAFETY_M or (lane is not None and firelanes.blocking(o, lane)):
                self.cease_fire.add(u.id)
                return ("halt",)
        self.cease_fire.discard(u.id)
        return want

    def _sidestep(self, u: Unit, want: tuple, g: "GroupRun", ls: list) -> tuple:
        """Einheit, die halten soll, steht im Schusskanal eines eigenen Feldgeschuetzes: seitlich heraustreten."""
        if want[0] != "halt" or u.role == "artillery":
            return want
        for l in firelanes.in_lanes(u, ls):
            p = l.exit_point(u.pos)
            g.places[u.id] = p
            return ("goto", p, False)
        return want

    # ---------- jede Sekunde ----------
    def tick(self, w: World) -> list[Order]:
        orders: list[Order] = self._pending_flags
        self._pending_flags = []
        foes = w.enemies(self.alliance)
        ls = firelanes.lanes(w, self.alliance, self._gun_targets())
        self.lanes = ls
        for g in self.groups:
            self._check_reflexes(g, w, foes)
            for uid in g.unit_ids:
                u = w.units.get(uid)
                if not u or not u.alive or not u.controlled:
                    continue
                want = self._desired(g, u, w, foes)
                if want is None:
                    continue
                want = self._artillery_safety(u, want, w)
                want = self._sidestep(u, want, g, ls)
                orders += self._limber(u, want, w)
                if self._should_issue(u, want, w):
                    o = self._to_order(u, want, w)
                    if o:
                        orders.append(o)
                        self.last[uid] = want
                        self.last_t[uid] = w.time
        return orders

    # ---------- Artillerie auf-/abprotzen ----------
    LIMBER_ABILITY = "unlimber"   # Spiel-Faehigkeit (Umschalter auf-/abprotzen), Liste aus Empire.exe
    LIMBER_RETRY_S = 10.0

    def _limber(self, u: Unit, want: tuple, w: World) -> list[Order]:
        """Abgeprotzte Geschuetze ignorieren Marschbefehle aus dem Skript (Schlacht 2026-11-20 08:30: Artillerie
        stand trotz move 150 s still). Steht ein Geschuetz mit Marschziel still, wird aufgeprotzt (Umschalter); faehrt
        es danach, gilt es als aufgeprotzt. Am Ziel oder bei Halt/Angriff wird wieder abgeprotzt. Nur EIN Versuch je
        Marschziel (Aufprotzen dauert einige Sekunden - ein zweiter Umschaltbefehl wuerde wieder abprotzen); lehnt das
        Spiel den Befehl zweimal ab, wird er abgeschaltet."""
        if u.role != "artillery" or "perform_special_ability" in self.disabled:
            return []
        st = self.limber.setdefault(u.id, {"limbered": False, "tries": 0, "t": -1e9, "target": None})
        if u.speed > 1.0 and st["tries"]:          # geschoben (abgeprotzt) sind es nur ~0,5 m/s
            st["limbered"] = True
        if want[0] == "goto" and dist(u.pos, want[1]) > ARRIVE_M * 2:
            if st["target"] is None or dist(st["target"], want[1]) > 30:
                st["target"], st["tries"] = want[1], 0
            if (not u.moving and not st["limbered"] and st["tries"] < 1
                    and w.time - self.last_t.get(u.id, -1e9) >= 3 and w.time - st["t"] >= self.LIMBER_RETRY_S):
                st["tries"] += 1
                st["t"] = w.time
                self.last.pop(u.id, None)       # Marschbefehl direkt danach erneut senden
                return [Order(u.bid, "perform_special_ability", [self.LIMBER_ABILITY])]
            return []
        if st["limbered"] and not u.moving:
            st.update(limbered=False, tries=0, target=None, t=w.time)
            return [Order(u.bid, "perform_special_ability", [self.LIMBER_ABILITY])]   # am Ziel / Halt: abprotzen
        return []

    def _desired(self, g: GroupRun, u: Unit, w: World, foes: list[Unit]) -> tuple | None:
        o = g.order
        if g.override:
            rf, trigger = g.override
            t = rf.order.type
            if t == "attack":
                tgt = w.units.get(trigger) if trigger else None
                if not tgt or not tgt.alive:
                    tgt, _ = w.nearest(u.pos, foes)
                return ("attack", tgt.id, rf.order.run) if tgt else ("halt",)
            if t == "hold":
                return ("halt",)
            if t == "move" and rf.order.target_point:
                return ("goto", (float(rf.order.target_point[0]), float(rf.order.target_point[1])), rf.order.run)
            if t == "fall_back":
                if u.id not in g.fallback_points:
                    n, _ = w.nearest(u.pos, foes)
                    away = bearing_to(n.pos, u.pos) if n else (u.bearing + 180) % 360
                    dx, dz = _dir(away)
                    dd = rf.order.distance_m or 60.0
                    g.fallback_points[u.id] = (u.x + dx * dd, u.z + dz * dd)
                return ("goto", g.fallback_points[u.id], True)
        # Reflex: Abstand halten
        if o.keep_range_m and o.task != "attack":
            n, d = w.nearest(u.pos, foes)
            if n and d < o.keep_range_m:
                dx, dz = _dir(bearing_to(n.pos, u.pos))
                step = o.keep_range_m - d + 15
                return ("goto", (u.x + dx * step, u.z + dz * step), True)
        if o.task == "attack":
            route = g.stages.get(u.id)
            while route and dist(u.pos, route[0]) < ARRIVE_M * 2:
                self._mark_reached(u.id, route.pop(0))
            if route:
                return ("goto", route[0], o.run)        # erst ueber die Wegpunkte, dann angreifen
            tgt = w.units.get(o.target_unit) if o.target_unit is not None else None
            if not tgt or not tgt.alive:
                if o.on_target_lost == "halt":
                    return ("halt",)
                tgt, _ = w.nearest(u.pos, foes)
            return ("attack", tgt.id, o.run) if tgt else ("halt",)
        if o.task == "withdraw":
            return ("withdraw",)
        # Reflex: Feinde in der Naehe des eigenen Platzes angreifen
        place = g.places.get(u.id, u.pos)
        if o.engage_within_m:
            n, d = w.nearest(place, foes)
            if n and d < o.engage_within_m:
                return ("attack", n.id, True)
        if o.task == "halt":
            if dist(u.pos, place) > 25 and o.engage_within_m:
                return ("goto", place, False)   # nach einem Gefecht zurueck an den Platz
            return ("halt",)
        if o.task == "move":
            stages = g.stages.get(u.id)
            if not stages:
                return ("halt",)
            while len(stages) > 1 and dist(u.pos, stages[0]) < ARRIVE_M:
                self._mark_reached(u.id, stages.pop(0))
            return ("goto", stages[0], o.run)
        return None

    def _should_issue(self, u: Unit, want: tuple, w: World) -> bool:
        last = self.last.get(u.id)
        since = w.time - self.last_t.get(u.id, -1e9)
        if last is None:
            return True
        if want[0] != last[0]:
            return True
        if want[0] == "goto":
            moved_target = dist(want[1], last[1]) > 5
            stuck = not u.moving and dist(u.pos, want[1]) > ARRIVE_M and since > REISSUE_S
            return moved_target or stuck or want[2] != last[2]      # auch wenn nur "rennen" dazukommt
        if want[0] == "attack":
            return want[1] != last[1] or want[2] != last[2] or (not u.moving and since > 15)
        return False   # halt / withdraw nur einmal

    def _to_order(self, u: Unit, want: tuple, w: World) -> Order | None:
        kind = want[0]
        if kind == "goto":
            x, z = want[1]
            # Signaturen wie in den Original-Kampagnenskripten (main.pack): goto_location(Ziel, rennen),
            # attack_unit(Ziel, true, rennen). Bis 2026-11-20 fehlte das Flag bzw. stand false -> Kavallerie trabte nur.
            return Order(u.bid, "goto_location", [{"x": round(x, 1), "z": round(z, 1)}, bool(want[2])])
        if kind == "attack":
            t = w.units.get(want[1])
            if not t:
                return None
            return Order(u.bid, "attack_unit", [{"unit": t.bid}, True, bool(want[2])])
        if kind == "halt":
            return Order(u.bid, "halt")
        if kind == "withdraw":
            return Order(u.bid, "withdraw") if "withdraw" not in self.disabled else Order(u.bid, "halt")
        return None

    # ---------- bedingte Befehle ----------
    def _check_reflexes(self, g: GroupRun, w: World, foes: list[Unit]) -> None:
        for i, rf in enumerate(g.order.reflexes or []):
            if g.fired[i]:
                continue
            ev = rf.event
            trigger: int | None = None
            hit = False
            units = [w.units[x] for x in g.unit_ids if x in w.units]
            if ev.type == "near" and ev.within_m:
                cand = [f for f in foes if not ev.enemy_units or f.id in ev.enemy_units]
                for u in units:
                    if not u.alive:
                        continue
                    n, d = w.nearest(u.pos, cand)
                    if n and d <= ev.within_m:
                        hit, trigger = True, n.id
                        break
            elif ev.type == "routed":
                if ev.side == "enemy":
                    watch = ev.enemy_units or list(g.enemies_alive_at_plan)
                    gone = [e for e in watch if e in w.units and not w.units[e].alive and e in g.enemies_alive_at_plan]
                    hit = bool(gone)
                else:
                    hit = any(not u.alive for u in units if u.id in g.alive_at_plan)
            elif ev.type == "arrived":
                hit = g.order.task == "move" and all(dist(u.pos, g.places[u.id]) < ARRIVE_M * 1.5
                                                     for u in units if u.alive and u.id in g.places)
            elif ev.type == "after" and ev.seconds is not None:
                hit = w.time - g.plan_time >= ev.seconds
            if hit:
                g.fired[i] = True
                g.override = (rf, trigger)
                g.fallback_points.clear()
                self.last.clear()

    # ---------- fuer den Bericht ----------
    def tasks(self, w: World) -> dict[int, str]:
        out: dict[int, str] = {}
        for g in self.groups:
            for uid in g.unit_ids:
                o = g.order
                s = f"group '{o.name}': {o.task}"
                if g.override:
                    s += f" -> REFLEX {g.override[0].order.type}"
                elif o.task == "move" and uid in g.places and uid in w.units:
                    left = dist(w.units[uid].pos, g.places[uid])
                    s += f", {left:.0f} m to go" if left > ARRIVE_M else ", in place"
                elif o.task == "attack" and o.target_unit is not None:
                    s += f" #{o.target_unit}"
                    if g.stages.get(uid):
                        s += f", marching via waypoints ({len(g.stages[uid])} left) before attacking"
                if uid in self.cease_fire:
                    s += " (HOLDING FIRE: own troops within 50 m of the target or in its line of fire)"
                out[uid] = s
        return out

    def reflex_status(self) -> list[str]:
        out = []
        for g in self.groups:
            for i, rf in enumerate(g.order.reflexes or []):
                out.append(f"'{g.order.name}' {rf.event.type}->{rf.order.type}: {'FIRED' if g.fired[i] else 'waiting'}")
        return out
