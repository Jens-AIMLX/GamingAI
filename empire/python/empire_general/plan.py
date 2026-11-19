"""Das Plan-Schema, mit dem der Feldherr (Sprachmodell) antwortet.

Angelehnt an rome_ai/plan.py (rome-battle-ai, MIT), reduziert auf das, was die Empire-Bruecke
ausfuehren und messen kann: keine Belagerungen/Stadtgraph, keine Moral-/Munitionswerte.
Koordinaten: [x, z] in Metern, x = Osten, z = Norden. Richtungen in Grad: 0 = Norden, 90 = Osten.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Task = Literal["move", "attack", "halt", "withdraw"]


class ReflexEvent(BaseModel):
    type: Literal["near", "routed", "arrived", "after"] = Field(
        description="near = an enemy (of enemy_units, or any) comes within within_m of a unit of this group; "
                    "routed = a unit of this group (side=ally) or of enemy_units (side=enemy) routs or dies; "
                    "arrived = the group has reached its move target; after = seconds have passed since this plan")
    within_m: float | None = Field(default=None, description="near: distance in metres")
    side: Literal["ally", "enemy"] | None = Field(default=None, description="routed: whose unit")
    seconds: float | None = Field(default=None, description="after: seconds")
    enemy_units: list[int] | None = Field(default=None, description="near/routed(enemy): only these enemy unit ids; omitted = any enemy")


class ReflexOrder(BaseModel):
    type: Literal["attack", "fall_back", "hold", "move"] = Field(
        description="attack = attack the enemy that set the event off (or the nearest enemy); fall_back = back away "
                    "distance_m from the nearest enemy; hold = stop; move = go to target_point")
    distance_m: float | None = Field(default=None, description="fall_back: metres (default 60)")
    target_point: list[float] | None = Field(default=None, description="move: [x, z]")
    run: bool = False


class Reflex(BaseModel):
    """Conditional order: when the event happens before your next plan, the whole group switches to the order
    (until your next plan). Checked every second by the executor, fires once."""
    event: ReflexEvent
    order: ReflexOrder


class GroupOrder(BaseModel):
    name: str = Field(description="Short label, e.g. 'centre line', 'right cavalry'")
    unit_ids: list[int] = Field(description="Your unit ids. shape=line: listed left to right as seen by the troops "
                                            "looking along facing_deg; shape=column: front to back")
    task: Task = Field(description="move = go to target_point and stand there in the shape; attack = every unit "
                                   "attacks target_unit (infantry and artillery fire at it, cavalry charges it); "
                                   "halt = stop where they are; withdraw = leave the battlefield (game order)")
    target_point: list[float] | None = Field(default=None, description="move: [x, z] centre of the line / head of the column")
    facing_deg: float | None = Field(default=None, description="move: direction the front faces on arrival (0 = north, 90 = east); "
                                                               "omitted = towards the enemy army")
    shape: Literal["line", "column", "none"] = Field(default="line", description="move: line = side by side across facing_deg, "
                                                                                 "column = one behind the other, none = every unit to target_point")
    spacing_m: float = Field(default=6.0, description="move: line = gap between neighbouring units' fronts; column = distance between unit centres")
    waypoints: list[list[float]] | None = Field(default=None, description="move: [[x, z], ...] to pass through first (e.g. round a flank)")
    target_unit: int | None = Field(default=None, description="attack: enemy unit id")
    run: bool = Field(default=False, description="true only for charges and urgent moves: running tires the men")
    on_target_lost: Literal["nearest", "halt"] = Field(default="nearest", description="attack: when the target dies or routs")
    engage_within_m: float | None = Field(default=None, description="move/halt reflex: a unit attacks any enemy that comes this "
                                                                    "close to its place, and returns when none is that close")
    keep_range_m: float | None = Field(default=None, description="reflex for any task except attack: when an enemy comes closer "
                                                                 "than this the unit falls back to keep the distance (skirmishers, "
                                                                 "artillery, the general)")
    fire_at_will: bool | None = Field(default=None, description="missile units: fire by themselves at enemies in range")
    skirmish: bool | None = Field(default=None, description="light infantry: the game's skirmish mode (stay out of melee)")
    reflexes: list[Reflex] | None = Field(default=None, description="conditional orders for this group")


class Plan(BaseModel):
    intent: str = Field(description="One or two sentences: the overall idea of the plan")
    note: str = Field(description="A short note to yourself for the next plans: what you are trying, what the enemy keeps "
                                  "doing, what worked or failed. Your last notes are shown in every report")
    groups: list[GroupOrder]


# ---------------------------------------------------------------------------------------------------------------
# Antwortschema fuer die Claude-API (strukturierte Ausgabe).
# Die API begrenzt optionale Felder und Vereinigungstypen ("X oder null") - das reiche Plan-Schema oben wurde mit
# "Schema is too complex" abgelehnt. Hier sind alle Felder Pflicht, ohne null; "nicht gesetzt" ist 0, -1, "" oder
# eine leere Liste. to_plan() macht daraus den internen Plan.
# ---------------------------------------------------------------------------------------------------------------
class WireReflexEvent(BaseModel):
    type: Literal["near", "routed", "arrived", "after"] = Field(
        description="near = an enemy comes within within_m of a unit of this group; routed = a unit of this group "
                    "(side=ally) or of enemy_units (side=enemy) routs or dies; arrived = the group reached its move "
                    "target; after = seconds passed since this plan")
    within_m: float = Field(description="near: metres; otherwise 0")
    side: Literal["ally", "enemy", "none"] = Field(description="routed: whose unit; otherwise none")
    seconds: float = Field(description="after: seconds; otherwise 0")
    enemy_units: list[int] = Field(description="near/routed(enemy): only these enemy ids; [] = any enemy")


class WireReflexOrder(BaseModel):
    type: Literal["attack", "fall_back", "hold", "move"]
    distance_m: float = Field(description="fall_back: metres (e.g. 60); otherwise 0")
    target_point: list[float] = Field(description="move: [x, z]; otherwise []")
    run: bool


class WireReflex(BaseModel):
    event: WireReflexEvent
    order: WireReflexOrder


class WireGroup(BaseModel):
    name: str = Field(description="short label, e.g. 'centre line'")
    unit_ids: list[int] = Field(description="your unit ids (order does not matter: each unit gets the slot nearest to where it stands)")
    task: Literal["move", "attack", "halt", "withdraw"]
    target_point: list[float] = Field(description="move: [x, z] centre of the line / head of the column; otherwise []")
    facing_deg: float = Field(description="move: front direction on arrival, 0 = north, 90 = east; -1 = towards the enemy army")
    shape: Literal["line", "column", "none"]
    spacing_m: float = Field(description="move: gap between units in a line / distance in a column, e.g. 6")
    waypoints: list[list[float]] = Field(description="move: [[x, z], ...] to pass first; [] = straight there")
    target_unit: int = Field(description="attack: enemy unit id; otherwise 0")
    run: bool = Field(description="true only for charges and urgent moves")
    on_target_lost: Literal["nearest", "halt"]
    engage_within_m: float = Field(description="move/halt reflex: attack enemies coming this close to the unit's place; 0 = off")
    keep_range_m: float = Field(description="reflex (not with attack): fall back when an enemy comes closer; 0 = off")
    fire_at_will: Literal["on", "off", "unchanged"]
    skirmish: Literal["on", "off", "unchanged"]
    reflexes: list[WireReflex] = Field(description="conditional orders for this group; [] = none")


class WirePlan(BaseModel):
    intent: str = Field(description="one or two sentences: the overall idea")
    note: str = Field(description="short note to yourself for the next plans")
    groups: list[WireGroup]


def _pt(v: list[float]) -> list[float] | None:
    return [float(v[0]), float(v[1])] if v and len(v) >= 2 else None


def _flag(v: str) -> bool | None:
    return {"on": True, "off": False}.get(v)


def to_plan(w: WirePlan) -> Plan:
    groups = []
    for g in w.groups:
        reflexes = []
        for r in g.reflexes:
            e, o = r.event, r.order
            reflexes.append(Reflex(
                event=ReflexEvent(type=e.type, within_m=e.within_m or None,
                                  side=None if e.side == "none" else e.side, seconds=e.seconds or None,
                                  enemy_units=e.enemy_units or None),
                order=ReflexOrder(type=o.type, distance_m=o.distance_m or None, target_point=_pt(o.target_point), run=o.run)))
        groups.append(GroupOrder(
            name=g.name, unit_ids=g.unit_ids, task=g.task, target_point=_pt(g.target_point),
            facing_deg=None if g.facing_deg < 0 else g.facing_deg % 360, shape=g.shape,
            spacing_m=g.spacing_m if g.spacing_m > 0 else 6.0,
            waypoints=[p for p in (_pt(x) for x in g.waypoints) if p] or None,
            target_unit=g.target_unit or None, run=g.run, on_target_lost=g.on_target_lost,
            engage_within_m=g.engage_within_m or None, keep_range_m=g.keep_range_m or None,
            fire_at_will=_flag(g.fire_at_will), skirmish=_flag(g.skirmish), reflexes=reflexes or None))
    return Plan(intent=w.intent, note=w.note, groups=groups)
