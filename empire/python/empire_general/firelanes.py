"""Schusskanaele der eigenen Feldgeschuetze: wo darf keine eigene Einheit stehen oder durchreiten.

Feldgeschuetze (Kanonen, Haubitzen) schiessen flach - alles zwischen Geschuetz und Ziel bekommt Treffer ab
(Schlacht 2026-11-20 09:32: General 2 fast drei Minuten, die Kavallerie auf Hin- und Rueckweg im Schussfeld von
Artillerie 1). Moerser schiessen im Bogen; fuer sie gilt nur die 50-m-Regel am Ziel (Executor._artillery_safety).

Ein Kanal reicht vom Geschuetz zum Ziel: bei KI-gefuehrten Geschuetzen das befohlene Ziel, sonst (auch bei vom
Spieler gefuehrten) der naechste Feind in Reichweite im Blickfeld des Geschuetzes. Ohne Feind in Reichweite schiesst
das Geschuetz nicht - dann gibt es keinen Kanal. Die letzten TARGET_ZONE_M vor dem Ziel gehoeren nicht zum Kanal:
dort kaempfen eigene Truppen im Nahkampf, das regelt die Feuerpause am Ziel.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .world import Unit, World, dist

HALF_M = 25.0          # halbe Breite des Kanals
MUZZLE_M = 15.0        # Kanal beginnt vor der Muendung
TARGET_ZONE_M = 40.0   # Nahbereich am Ziel gehoert nicht zum Kanal
CONE_DEG = 30.0        # Blickfeld fuer Geschuetze ohne bekanntes Ziel
CLEAR_M = 30.0         # so weit neben dem Kanal sind Ersatzplaetze und Umwege
SCREEN_M = 120.0       # stehende Infanterie so nah vor dem Geschuetz: es schiesst darueber (Aufstellung "Artillerie
                       # hinter der Infanterielinie" ist gewollt) - Kavallerie, Generaele, Marschierende zaehlen immer


def is_direct_fire(u: Unit) -> bool:
    return u.role == "artillery" and "mortar" not in (u.type or "").lower()


@dataclass
class Lane:
    gun: Unit
    start: tuple[float, float]
    end: tuple[float, float]
    target: Unit | None

    @property
    def length(self) -> float:
        return dist(self.start, self.end)

    @property
    def dir(self) -> tuple[float, float]:
        n = max(self.length, 1e-6)
        return (self.end[0] - self.start[0]) / n, (self.end[1] - self.start[1]) / n

    def _local(self, p: tuple[float, float]) -> tuple[float, float]:
        dx, dz = self.dir
        rx, rz = p[0] - self.start[0], p[1] - self.start[1]
        return rx * dx + rz * dz, rx * dz - rz * dx          # (entlang, quer; + = rechts in Schussrichtung)

    def contains(self, p: tuple[float, float], margin: float = 0.0) -> bool:
        along, side = self._local(p)
        return -MUZZLE_M <= along <= self.length - TARGET_ZONE_M and abs(side) < HALF_M + margin

    def side_of(self, p: tuple[float, float]) -> float:
        return 1.0 if self._local(p)[1] >= 0 else -1.0

    def point(self, along: float, side: float) -> tuple[float, float]:
        dx, dz = self.dir
        # quer = (dz, -dx)
        return (self.start[0] + dx * along + dz * side, self.start[1] + dz * along - dx * side)

    def exit_point(self, p: tuple[float, float]) -> tuple[float, float]:
        """naechster Platz neben dem Kanal (seitlich heraus)"""
        along, side = self._local(p)
        s = 1.0 if side >= 0 else -1.0
        return self.point(along, s * (HALF_M + CLEAR_M))

    def inside_length(self, a: tuple[float, float], b: tuple[float, float]) -> float:
        """wie viele Meter der Strecke a->b im Kanal liegen (Abtastung alle 5 m)"""
        n = max(int(dist(a, b) / 5), 1)
        hits = sum(1 for i in range(n + 1) if self.contains((a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n)))
        return hits * dist(a, b) / n if n else 0.0

    def detour(self, a: tuple[float, float], b: tuple[float, float]) -> list[tuple[float, float]]:
        """Umweg fuer die Strecke a->b, wenn sie durch den Kanal fuehrt: seitlich am Kanal entlang (auf der Seite des
        Startpunkts) bzw. hinter dem Geschuetz herum, wenn die Strecke den Kanal kreuzt."""
        if self.inside_length(a, b) < 20:
            return []
        la, sa = self._local(a)
        lb, sb = self._local(b)
        side = 1.0 if (sa if abs(sa) >= HALF_M else sb) >= 0 else -1.0
        if abs(sa) >= HALF_M and abs(sb) >= HALF_M and (sa > 0) != (sb > 0):
            # kreuzt den Kanal: hinter dem Geschuetz herum
            behind = self.point(-MUZZLE_M - 40.0, 0.0)
            return [self.point(-MUZZLE_M - 40.0, side * (HALF_M + CLEAR_M)), behind,
                    self.point(-MUZZLE_M - 40.0, -side * (HALF_M + CLEAR_M))]
        lo = max(min(la, lb), -MUZZLE_M)
        hi = min(max(la, lb), self.length - TARGET_ZONE_M)
        pts = [self.point(lo, side * (HALF_M + CLEAR_M)), self.point(hi, side * (HALF_M + CLEAR_M))]
        return pts if la <= lb else pts[::-1]


def lanes(w: World, alliance: int, targets: dict[int, int] | None = None) -> list[Lane]:
    out: list[Lane] = []
    foes = w.enemies(alliance)
    for g in w.side(alliance):
        if not is_direct_fire(g) or g.speed > 0.8:
            continue
        rng = g.missile_range or 400.0
        tgt = w.units.get((targets or {}).get(g.id, -1))
        if not (tgt and tgt.alive and dist(g.pos, tgt.pos) <= rng * 1.1):
            fx, fz = math.sin(math.radians(g.bearing)), math.cos(math.radians(g.bearing))
            best = None
            for f in foes:
                d = dist(g.pos, f.pos)
                if d > rng or d < 1:
                    continue
                cosang = ((f.x - g.x) * fx + (f.z - g.z) * fz) / d
                if cosang >= math.cos(math.radians(CONE_DEG)) and (best is None or d < best[0]):
                    best = (d, f)
            tgt = best[1] if best else None
        if not tgt:
            continue
        d = dist(g.pos, tgt.pos)
        if d < MUZZLE_M + TARGET_ZONE_M + 10:
            continue
        dx, dz = (tgt.x - g.x) / d, (tgt.z - g.z) / d
        out.append(Lane(gun=g, start=(g.x + dx * MUZZLE_M, g.z + dz * MUZZLE_M), end=tgt.pos, target=tgt))
    return out


def screened(u: Unit, l: Lane) -> bool:
    """stehende Infanterie dicht vor dem Geschuetz - das Geschuetz feuert ueber sie hinweg"""
    from .units import is_cavalry
    return (not is_cavalry(u.role) and u.role != "artillery" and u.speed < 0.5
            and dist(u.pos, l.gun.pos) <= SCREEN_M)


def blocking(u: Unit, l: Lane) -> bool:
    """steht die Einheit so im Kanal, dass sie getroffen wuerde?"""
    return (l.gun.id != u.id and u.role != "artillery" and u.alive and l.contains(u.pos)
            and not screened(u, l))


def in_lanes(u: Unit, ls: list[Lane]) -> list[Lane]:
    return [l for l in ls if blocking(u, l)]
