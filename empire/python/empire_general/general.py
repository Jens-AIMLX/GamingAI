"""Der Feldherr: erzeugt alle paar Sekunden aus dem Lagebericht einen Plan.

Backends:
  claude - Claude ueber die offizielle Anthropic-SDK (strukturierte Ausgabe = Plan-Schema).
           Zugangsdaten aus der Umgebung (ANTHROPIC_API_KEY oder `ant auth login`).
  rule   - ohne Sprachmodell: einfacher Regel-Feldherr fuer Offline-Tests.

System-Prompt und Aufbau nach rome_ai/general.py (rome-battle-ai, MIT), an Empire angepasst.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from .plan import GroupOrder, Plan
from .units import is_cavalry
from .world import World, bearing_to, centroid, dist

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are the commanding general of an army in a real-time 18th-century battle (Total War: EMPIRE): line \
infantry with muskets, light infantry, cavalry and field artillery. You receive a report of the battlefield \
every few seconds and answer with a battle plan for your units.

How your plan is executed: a subordinate turns each group's task into game orders every second until your \
next plan, which is 5-15 s away. It computes formation positions from your parameters and runs only the \
reflexes you switch on. Units you leave out of the plan keep doing what they did last. Every decision - who \
goes where, in which shape, when to wait and when to strike - is yours, so give every unit a task.

Map: coordinates are metres, x = east, z = north; [x, z] points. Directions in degrees: 0 = north, 90 = east, \
180 = south, 270 = west. Unit ids are numbers like 203 (yours and the enemy's are listed in the report).

Tasks (one per group):
- move: go to target_point [x, z] and stand there. shape "line" = units side by side across facing_deg \
(unit_ids listed left to right as the troops see it when they face facing_deg), "column" = one behind the other \
(front to back), "none" = each unit goes to target_point itself. spacing_m = gap between neighbouring units. \
facing_deg omitted = towards the enemy army. waypoints = points to pass first (a flank march). Units on the move \
face the way they march; a line forms when they arrive.
- attack: every unit of the group attacks target_unit: infantry and artillery move into range and fire at it, \
cavalry charges it. run=true for a charge. Changing a target cancels a charge in progress, so keep targets \
steady. on_target_lost: "nearest" (default) = attack the nearest enemy when the target breaks, "halt" = stop.
- halt: stop where they are.
- withdraw: the units leave the battlefield (they march off the map and are saved).
Reflexes per group (the executor reacts within a second, without you):
- engage_within_m (move, halt): a unit attacks any enemy that comes this close to its place and returns when \
none is that close (e.g. 60-100 for cavalry guarding a flank).
- keep_range_m (not with attack): when an enemy comes closer than this the unit falls back to keep the distance - \
for skirmishers, artillery and the general's bodyguard.
- fire_at_will (missile units, default on in the game), skirmish (light infantry).
- reflexes: conditional orders "when <event> happens, the group switches to <order>": events near (within_m, \
optionally only enemy_units), routed (side ally/enemy), arrived, after (seconds); orders attack (the enemy that \
set it off), fall_back (distance_m), hold, move (target_point). Each fires once and holds until your next plan. \
Use the PREDICTED section to set them up before contact.

Note (required): end every plan with a short note to yourself - your next plans see your last notes, earlier \
reports are not kept. Write what you are trying and what the enemy keeps doing.

Attack or defend: battles have a time limit. Units standing in musket or canister range lose men every second; \
standing still out of range costs nothing. Cavalry going round a flank (watch the enemy's direction and speed) \
hits the side or rear.\
"""


def rule_lines(text: str) -> str:
    """Nur die Spiegelstriche aus rules.md / strategy.md gehen an das Modell; Ueberschriften sind fuer Menschen."""
    return "\n".join(line.strip() for line in text.splitlines() if line.strip().startswith(("- ", "* ")))


def system_prompt(rules: str = "", strategy: str = "", instructions: str = "") -> str:
    """Bleibt fuer die ganze Schlacht byte-gleich, damit der Prompt-Cache greift."""
    out = SYSTEM_PROMPT
    if instructions.strip():
        out += ("\n\nORDERS FROM YOUR SOVEREIGN for this battle (follow them unless the battle would be lost):\n"
                + instructions.strip())
    if rule_lines(rules):
        out += ("\n\nGAME RULES: facts about the game and hard rules of your command. Break one only when the battle "
                "is otherwise lost:\n" + rule_lines(rules))
    if rule_lines(strategy):
        out += ("\n\nSTRATEGY: advice from your commander, sound in most situations. Depart from it when the "
                "situation clearly calls for something else:\n" + rule_lines(strategy))
    return out


class General(Protocol):
    def plan(self, report: str, world: World, alliance: int) -> Plan: ...


class ClaudeGeneral:
    """Claude mit strukturierter Ausgabe (Plan-Schema). Jede Planung ist ein eigener Aufruf: der feste
    System-Prompt kommt aus dem Prompt-Cache, die Erinnerung laeuft ueber die Notizen im Bericht."""

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "low", rules: str = "", strategy: str = "",
                 instructions: str = "") -> None:
        import anthropic

        self.anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=2, timeout=60.0)
        self.model = model
        self.effort = effort
        self.system = system_prompt(rules, strategy, instructions)
        self.last_usage: dict | None = None

    def plan(self, report: str, world: World, alliance: int) -> Plan:
        a = self.anthropic
        try:
            resp = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=16000,
                system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": report}],
                output_format=Plan,
                output_config={"effort": self.effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",  # bei einer Ablehnung uebernimmt serverseitig ein anderes Modell
            )
        except a.RateLimitError as e:
            raise RuntimeError(f"Rate-Limit: {e.message}") from e
        except a.AuthenticationError as e:
            raise RuntimeError("Anmeldung fehlgeschlagen: ANTHROPIC_API_KEY setzen oder `ant auth login`") from e
        except a.APIStatusError as e:
            raise RuntimeError(f"API-Fehler {e.status_code}: {e.message}") from e
        except a.APIConnectionError as e:
            raise RuntimeError("Keine Verbindung zur Claude-API") from e
        u = resp.usage
        self.last_usage = {"in": u.input_tokens, "out": u.output_tokens,
                           "cache_read": getattr(u, "cache_read_input_tokens", 0),
                           "cache_write": getattr(u, "cache_creation_input_tokens", 0)}
        if resp.stop_reason == "refusal":
            raise RuntimeError("Modell hat den Plan abgelehnt (refusal)")
        if resp.parsed_output is None:
            raise RuntimeError(f"kein gueltiger Plan (stop_reason {resp.stop_reason})")
        return resp.parsed_output


class RuleGeneral:
    """Ohne Sprachmodell: Artillerie haelt und feuert, Infanterie rueckt in Linie bis 120 m vor und greift dann
    die naechste Einheit an, Kavallerie schuetzt die Flanken. Fuer Offline-Tests der ganzen Kette."""

    def plan(self, report: str, world: World, alliance: int) -> Plan:
        mine = world.side(alliance)
        foes = world.enemies(alliance)
        cm, ce = centroid(mine), centroid(foes)
        if not mine or not cm or not ce:
            return Plan(intent="nothing to command", note="", groups=[])
        art = [u for u in mine if u.role == "artillery"]
        cav = [u for u in mine if is_cavalry(u.role)]
        inf = [u for u in mine if u not in art and u not in cav]
        groups: list[GroupOrder] = []
        d = dist(cm, ce)
        if art:
            groups.append(GroupOrder(name="artillery", unit_ids=[u.id for u in art], task="halt", keep_range_m=80))
        if inf:
            cm = centroid(inf) or cm          # Abstand von der Infanterie aus, nicht vom Heeresschwerpunkt
            d = dist(cm, ce)
            face = bearing_to(cm, ce)
            near, nd = world.nearest(cm, foes)
            if nd > 160:
                import math
                step = min(nd - 120, 80)        # bis ~120 m an den naechsten Feind heran
                tx = cm[0] + (ce[0] - cm[0]) / d * step
                tz = cm[1] + (ce[1] - cm[1]) / d * step
                # Reihenfolge links -> rechts quer zur Blickrichtung
                rx, rz = math.cos(math.radians(face)), -math.sin(math.radians(face))
                inf.sort(key=lambda u: (u.x - cm[0]) * rx + (u.z - cm[1]) * rz)
                groups.append(GroupOrder(name="infantry line", unit_ids=[u.id for u in inf], task="move",
                                         target_point=[tx, tz], facing_deg=face, shape="line", spacing_m=6))
            else:
                groups.append(GroupOrder(name="infantry line", unit_ids=[u.id for u in inf], task="attack",
                                         target_unit=near.id if near else None))
        if cav:
            groups.append(GroupOrder(name="cavalry", unit_ids=[u.id for u in cav], task="halt", engage_within_m=100))
        return Plan(intent=f"advance the line to musket range ({d:.0f} m apart), artillery holds",
                    note="rule-based general", groups=groups)


def make_general(backend: str, model: str | None, effort: str, rules: str, strategy: str,
                 instructions: str = "") -> General:
    if backend == "claude":
        return ClaudeGeneral(model or "claude-opus-5-5", effort, rules, strategy, instructions)
    if backend == "rule":
        return RuleGeneral()
    raise SystemExit(f"unbekanntes Backend {backend!r}")


def load_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
