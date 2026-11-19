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
from .units import frontage_m
from .world import Unit, World, bearing_to, centroid, dist

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

    # ---------- neuer Plan ----------
    def set_plan(self, plan: Plan, w: World) -> None:
        self.plan = plan
        self.groups = []
        self.plan_warnings = []
        mine = {u.id: u for u in w.side(self.alliance) if u.controlled}
        foes = w.enemies(self.alliance)
        ce = centroid(foes)
        taken: set[int] = set()
        for g in plan.groups:
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
            if g.task == "move" and g.target_point and len(g.target_point) >= 2:
                c = (float(g.target_point[0]), float(g.target_point[1]))
                face = g.facing_deg if g.facing_deg is not None else (bearing_to(c, ce) if ce else 0.0)
                finals = self._formation([mine[i] for i in ids], c, face, g.shape, g.spacing_m)
                for uid in ids:
                    off = (finals[uid][0] - c[0], finals[uid][1] - c[1])
                    wps = [(float(p[0]) + off[0], float(p[1]) + off[1]) for p in (g.waypoints or []) if len(p) >= 2]
                    gr.stages[uid] = wps + [finals[uid]]
                    gr.places[uid] = finals[uid]
            else:
                for uid in ids:
                    gr.places[uid] = mine[uid].pos
            # Feuer-/Plaenkler-Einstellungen einmal je Plan
            for uid in ids:
                bid = mine[uid].bid
                if g.fire_at_will is not None and "fire_at_will" not in self.disabled:
                    self._pending_flags.append(Order(bid, "fire_at_will", [bool(g.fire_at_will)]))
                if g.skirmish is not None and "skirmish" not in self.disabled:
                    self._pending_flags.append(Order(bid, "skirmish", [bool(g.skirmish)]))
            self.groups.append(gr)
        # Neue Befehle sofort erzwingen
        self.last.clear()

    @staticmethod
    def _formation(units: list[Unit], c: tuple[float, float], face: float, shape: str, gap: float) -> dict[int, tuple[float, float]]:
        out: dict[int, tuple[float, float]] = {}
        if shape == "none" or len(units) == 1:
            return {u.id: c for u in units}
        if shape == "column":
            dx, dz = _dir(face)
            for i, u in enumerate(units):
                out[u.id] = (c[0] - dx * i * max(gap, 20.0), c[1] - dz * i * max(gap, 20.0))
            return out
        rx, rz = _right(face)
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
            if cmd in ("fire_at_will", "skirmish", "withdraw") and self.fail_count[cmd] >= 2:
                self.disabled.add(cmd)

    SAFETY_M = 50.0

    def _artillery_safety(self, u: Unit, want: tuple, w: World) -> tuple:
        """Artillerie stellt das Feuer ein, solange eigene Einheiten naeher als SAFETY_M am Ziel sind
        (Nahkampf, eigene Linie davor) - und feuert wieder, sobald es frei ist."""
        if u.role != "artillery" or want[0] != "attack":
            return want
        t = w.units.get(want[1])
        if not t:
            return want
        for o in w.side(self.alliance):
            if o.id != u.id and o.role != "artillery" and dist(o.pos, t.pos) < self.SAFETY_M:
                self.cease_fire.add(u.id)
                return ("halt",)
        self.cease_fire.discard(u.id)
        return want

    # ---------- jede Sekunde ----------
    def tick(self, w: World) -> list[Order]:
        orders: list[Order] = self._pending_flags
        self._pending_flags = []
        foes = w.enemies(self.alliance)
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
                if self._should_issue(u, want, w):
                    o = self._to_order(u, want, w)
                    if o:
                        orders.append(o)
                        self.last[uid] = want
                        self.last_t[uid] = w.time
        return orders

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
                stages.pop(0)
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
            return moved_target or stuck
        if want[0] == "attack":
            return want[1] != last[1] or (not u.moving and since > 15)
        return False   # halt / withdraw nur einmal

    def _to_order(self, u: Unit, want: tuple, w: World) -> Order | None:
        kind = want[0]
        if kind == "goto":
            x, z = want[1]
            return Order(u.bid, "goto_location", [{"x": round(x, 1), "z": round(z, 1)}])
        if kind == "attack":
            t = w.units.get(want[1])
            if not t:
                return None
            return Order(u.bid, "attack_unit", [{"unit": t.bid}, bool(want[2]), False])
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
                if uid in self.cease_fire:
                    s += " (HOLDING FIRE: own troops within 50 m of the target)"
                out[uid] = s
        return out

    def reflex_status(self) -> list[str]:
        out = []
        for g in self.groups:
            for i, rf in enumerate(g.order.reflexes or []):
                out.append(f"'{g.order.name}' {rf.event.type}->{rf.order.type}: {'FIRED' if g.fired[i] else 'waiting'}")
        return out
