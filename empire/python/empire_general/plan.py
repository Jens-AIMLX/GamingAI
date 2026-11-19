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
