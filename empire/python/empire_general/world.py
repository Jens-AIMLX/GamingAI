"""Lagebild-Modell: macht aus state.json eine handliche Sicht fuer Bericht und Executor.

- Kurze Einheiten-IDs fuer das Sprachmodell: Allianz * 100 + laufende Nummer (z. B. 203).
- Bewegungsverlauf der letzten Sekunden -> Kurs und Tempo jeder Einheit.
- Koordinaten: x = Osten, z = Norden (Meter). Richtung in Grad: 0 = Norden (+z), 90 = Osten (+x).
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from .units import role_of

HISTORY_S = 12.0


@dataclass
class Unit:
    id: int                 # kurze ID fuer das Modell
    bid: str                # ID der Bruecke "a.r.u"
    alliance: int
    type: str
    role: str
    men: int
    men_start: int
    x: float
    z: float
    bearing: float
    moving: bool
    routing: bool
    leaving: bool
    missile_range: float
    controlled: bool
    selected: bool = False  # im Spiel markiert
    manual: bool = False    # per Hotkey an den Spieler gegeben
    vx: float = 0.0         # m/s
    vz: float = 0.0

    @property
    def alive(self) -> bool:
        return self.men > 0 and not self.routing and not self.leaving

    @property
    def pos(self) -> tuple[float, float]:
        return (self.x, self.z)

    @property
    def speed(self) -> float:
        return math.hypot(self.vx, self.vz)


def dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def bearing_to(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Richtung von a nach b in Grad (0 = Norden, 90 = Osten)."""
    return math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 360


def compass(deg: float) -> str:
    names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return names[int(((deg % 360) + 22.5) // 45) % 8]


def centroid(units: list[Unit]) -> tuple[float, float] | None:
    us = [u for u in units if u.alive] or units
    if not us:
        return None
    return (sum(u.x for u in us) / len(us), sum(u.z for u in us) / len(us))


@dataclass
class World:
    time: float = 0.0
    phase: str = "?"
    seq: int = 0
    player_alliance: int | None = None
    control: str = "enemy"
    controlled_alliances: list[int] = field(default_factory=list)
    units: dict[int, Unit] = field(default_factory=dict)
    by_bid: dict[str, Unit] = field(default_factory=dict)
    order_results: list[dict] = field(default_factory=list)
    _ids: dict[str, int] = field(default_factory=dict)
    _hist: dict[str, deque] = field(default_factory=dict)

    def update(self, st: dict) -> None:
        self.time = float(st.get("game_time") or 0.0)
        self.phase = st.get("phase") or "?"
        self.seq = int(st.get("seq") or 0)
        self.player_alliance = st.get("player_alliance")
        self.control = st.get("control") or "enemy"
        self.controlled_alliances = list(st.get("controlled_alliances") or [])
        self.order_results = list(st.get("order_results") or [])
        counters: dict[int, int] = {}
        for al in st.get("alliances", []):
            a = int(al["index"])
            for u in al.get("units", []):
                bid = u["id"]
                if bid not in self._ids:
                    counters[a] = max([i % 100 for i in self._ids.values() if i // 100 == a] + [0]) + 1
                    self._ids[bid] = a * 100 + counters[a]
                uid = self._ids[bid]
                x, z = float(u.get("x") or 0.0), float(u.get("z") or 0.0)
                h = self._hist.setdefault(bid, deque())
                h.append((self.time, x, z))
                while h and self.time - h[0][0] > HISTORY_S:
                    h.popleft()
                vx = vz = 0.0
                # Tempo aus den letzten ~4 s
                old = next((p for p in h if self.time - p[0] <= 4.0), None)
                if old and self.time - old[0] >= 1.0:
                    dt = self.time - old[0]
                    vx, vz = (x - old[1]) / dt, (z - old[2]) / dt
                rng = float(u.get("missile_range") or 0.0)
                unit = Unit(
                    id=uid, bid=bid, alliance=a, type=u.get("type") or "?", role=role_of(u.get("type") or "", rng),
                    men=int(u.get("men") or 0), men_start=int(u.get("men_start") or 0), x=x, z=z,
                    bearing=float(u.get("bearing") or 0.0), moving=bool(u.get("moving")),
                    routing=bool(u.get("routing")), leaving=bool(u.get("leaving")), missile_range=rng,
                    controlled=bool(u.get("controlled")), selected=bool(u.get("selected")),
                    manual=bool(u.get("manual")), vx=vx, vz=vz)
                self.units[uid] = unit
                self.by_bid[bid] = unit

    # ---------- Sichten ----------
    def wanted_alliances(self) -> list[int]:
        """Allianzen, die laut config.lua von der KI gefuehrt werden (auch schon in der Aufstellung)."""
        if self.controlled_alliances:
            return list(self.controlled_alliances)
        player = self.player_alliance or 1
        alls = sorted({u.alliance for u in self.units.values()})
        if self.control == "both":
            return alls
        if self.control == "player":
            return [player]
        if self.control == "enemy":
            return [a for a in alls if a != player]
        return []

    def selected(self) -> list[Unit]:
        return [u for u in self.units.values() if u.selected]

    def side(self, alliance: int, alive: bool = True) -> list[Unit]:
        return [u for u in self.units.values() if u.alliance == alliance and (u.alive or not alive)]

    def enemies(self, alliance: int, alive: bool = True) -> list[Unit]:
        return [u for u in self.units.values() if u.alliance != alliance and (u.alive or not alive)]

    def nearest(self, pos: tuple[float, float], units: list[Unit]) -> tuple[Unit | None, float]:
        best, bd = None, float("inf")
        for u in units:
            d = dist(pos, u.pos)
            if d < bd:
                best, bd = u, d
        return best, bd

    def men_total(self, alliance: int, enemy: bool = False) -> int:
        us = self.enemies(alliance, alive=False) if enemy else self.side(alliance, alive=False)
        return sum(u.men for u in us)
