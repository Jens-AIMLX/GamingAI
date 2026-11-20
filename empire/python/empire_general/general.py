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

from .plan import GroupOrder, Plan, WirePlan, to_plan
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
(the subordinate gives each unit the slot nearest to where it stands, so the order of unit_ids does not matter; \
to put certain units on a wing, give them their own group and target_point), "column" = one behind the other \
(the foremost unit leads), "none" = each unit goes to target_point itself. spacing_m = gap between neighbouring units. \
facing_deg -1 = towards the enemy army. waypoints = points to pass first (a flank march). Units on the move \
face the way they march; a line forms when they arrive. run=true makes the units run (move and attack) - \
for charges, pursuits, escapes and every order with "schnell", "rennen", "im Galopp", "Eilmarsch"; running \
tires them.
  Getting units OUT OF FIRE (retreat, "aus der Schussweite", "zurückziehen", "ausweichen"): the first leg must \
lead straight AWAY from the nearest enemies (more than 150 m from every enemy unit); only then turn sideways or \
towards an edge. Never route along the enemy's line or through points closer than ~150 m to enemy units - \
riding along an enemy front is the deadliest thing a unit can do. Keep a route once given: send the same \
waypoints and target_point in your next plans (points already passed are skipped automatically); changing them \
every plan makes the units turn back and forth under fire.
  Placing groups relative to your army (preferred, especially for deployment - no sign errors): set ref and leave target_point []. The army frame: "forward" = towards the enemy army, "behind" = away from it, "right"/"left" as your troops see it facing the enemy. ref "front" = the centre of your current front line (the foremost infantry and artillery of your main body; \
cavalry and units that have gone far ahead do not count), ref "rear" = the rearmost infantry/artillery of your \
main body, or ref = a unit name from the report (e.g. "Artillerie 1", also one you move in this same plan: then its new place counts). back_m = metres behind the reference (negative = in front of it), right_m = metres to the right (negative = left); with a unit as ref, right_m is measured from that unit's side edge. attach = which point of this group's line is put at that point: "centre", "left_end" or "right_end". Examples: second line 60 m behind the front: ref "front", back_m 60, right_m 0, attach "centre". Infantry directly left of "Artillerie 1" with a 5 m gap: ref "Artillerie 1", back_m 0, right_m -5, attach "right_end" (the group's right end touches the gap). The same on the right: right_m 5, attach "left_end". Reserve 40 m behind "Infanterie 2": ref "Infanterie 2", back_m 40, right_m 0. Pulling units back (e.g. the \
generals or the cavalry behind everything): ref "rear", back_m POSITIVE (e.g. 50 = 50 m behind your rearmost \
units); a negative back_m with ref "rear" would send them forward towards the enemy. ref "absolute" = use target_point [x, z]. Each unit's frontage (width of its line) is in the report; a line is as wide as the sum of its units' frontages plus the gaps.
- attack: every unit of the group attacks target_unit: infantry and artillery move into range and fire at it, \
cavalry charges it. run=true for a charge. Changing a target cancels a charge in progress, so keep targets \
steady. on_target_lost: "nearest" (default) = attack the nearest enemy when the target breaks, "halt" = stop. \
waypoints work with attack too: the units first march through them (each keeping its distance from the group's \
centre), then attack - use this for envelopments, flank and rear attacks and "first out to the edge, then \
attack". Without waypoints the units go straight at the target. Only promise a route in your intent if you \
give these waypoints; repeat the same waypoints in later plans (points already passed are skipped).
- halt: stop where they are. Use halt whenever units are to STAY where they stand ("hold the position", "stay in \
line", "remain unchanged", "keep the current place"), with engage_within_m if they may strike back. Never give \
move to the place where a group already stands: move re-forms the group as a new line around the target point \
(ref "front" = the centre of the front), so the units would leave their places and bunch up or swap places.
- withdraw: the units leave the battlefield for good (they march off the map and take no further part). Use it \
ONLY when the sovereign explicitly orders them off the field ("vom Schlachtfeld abziehen", "das Schlachtfeld \
verlassen", "flee", "all flee"). "Zurückziehen", "zurücknehmen", "ausweichen", "zum Rand" or "aus der \
Schussweite" mean a move back (or to the named edge) with run=true - the units stay in the battle.
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

Handing units over: your sovereign can take units over to command them personally, and hand them back to you, \
with an immediate order (e.g. "gib mir Kavallerie 1", "ich übernehme die Artillerie", "die Generäle führe ich \
selbst", "nimm Infanterie 4 wieder", "du übernimmst wieder alles"). Then put those unit ids into give_to_player \
(units the sovereign takes) or take_from_player (units that come back to you; in the report they are marked \
COMMANDED BY THE PLAYER). Resolve descriptions such as "the cavalry on the left" to the listed units. Do this \
ONLY for such an explicit order, never on your own initiative and never for orders that only tell units what to \
do; otherwise leave both lists []. Do not give orders to units you hand over in the same plan. The handover is \
carried out once for the newest order; you need not repeat it in later plans.

The sovereign's designations: the sovereign names sets of units in their own words ("frontale Kavallerie", \
"Feldartillerie", "Fernartillerie", "linker Flügel"). The first time you resolve such words to units, list them \
in designations (term exactly as written, the unit ids). From then on they appear under SOVEREIGN'S DESIGNATIONS \
in every report and mean EXACTLY those units for the rest of the battle - even when the units have moved, so that \
"frontale Kavallerie" no longer describes where they stand. Never reinterpret them. Only if the newest order \
explicitly assigns the words to other units ("frontale Kavallerie sind jetzt Kavallerie 1 und 4") report the term \
again with redefined=true. Name the units in your acknowledgement (e.g. "Frontale Kavallerie (Kavallerie 2, 5) ..."). \
Typical meanings when nothing else is fixed: "Feldartillerie"/"Frontartillerie"/"normale Artillerie" = the field \
guns (cannons), "Fernartillerie"/"weitreichende Artillerie"/"Mörser" = the mortars and long-range guns.

Answer format: every field of every group is required. For fields that do not apply use 0 (numbers), -1 \
(facing_deg = towards the enemy), [] (lists and points), "absolute" (ref, when not used), "centre" (attach), \
"none" (side) or "unchanged" (fire_at_will, skirmish).

Note (required): end every plan with a short note to yourself - your next plans see your last notes, earlier \
reports are not kept. Write what you are trying and what the enemy keeps doing.

Attack or defend: battles have a time limit. Units standing in musket or canister range lose men every second; \
standing still out of range costs nothing. Cavalry going round a flank (watch the enemy's direction and speed) \
hits the side or rear.

Limits of your command (do not plan or promise these, they cannot be ordered): the artillery's ammunition type \
(round shot, canister, shell) and the infantry's formation type (square, column, fire by rank) are chosen by the \
game's own unit behaviour. The terrain (hills, woods, rivers) is not reported - only the ground height under each unit; a "hill crest" can only be estimated from those heights. Artillery firing at a target automatically ceases fire while your own units are within \
50 m of that target, and resumes when they are clear.

Moving artillery: guns must be limbered to move. When you give artillery a move task, the subordinate limbers \
the guns automatically and unlimbers them on arrival or when you switch them to halt/attack. Limbered guns cannot \
fire and travel slowly, so move them in few, clear bounds. "Aufprotzen und mitziehen" = move task (only the guns \
the sovereign named). To follow the infantry, give the guns ONE fixed placement relative to the infantry (e.g. \
ref "Infanterie 3", back_m 40) and keep exactly that placement in the following plans; do not change the distance \
or the reference every plan, and do not switch the guns between move and attack while they are on the way.\
"""


def rule_lines(text: str) -> str:
    """Nur die Spiegelstriche aus rules.md / strategy.md gehen an das Modell; Ueberschriften sind fuer Menschen."""
    return "\n".join(line.strip() for line in text.splitlines() if line.strip().startswith(("- ", "* ")))


def side_text(role: str) -> str:
    """Wen das Modell fuehrt - steht im System-Prompt, damit es seine Befehle eindeutig zuordnen kann."""
    if role == "player":
        return ("YOUR SIDE: you command the PLAYER'S ARMY - the army of the human who gave you the orders below. "
                "The enemy army is commanded by someone else.")
    if role == "enemy":
        return ("YOUR SIDE: you command the ENEMY ARMY - the opponent of the human player. "
                "The orders below are the ones meant for the enemy side.")
    return ""


def system_prompt(rules: str = "", strategy: str = "", orders: str = "", role: str = "") -> str:
    """Wird vor jedem Plan neu gebaut (Strategie/Befehle koennen sich aendern). Solange sich nichts aendert, bleibt
    er byte-gleich und kommt aus dem Prompt-Cache."""
    out = SYSTEM_PROMPT
    if side_text(role):
        out += "\n\n" + side_text(role)
    if orders.strip():
        out += ("\n\nORDERS FROM YOUR SOVEREIGN for this battle - BINDING. Follow them to the letter and put them above "
                "the strategy advice and above your own judgement of the situation (also when you are much stronger). "
                "Deviate only when following them would clearly lose the battle, and then say so in your note. "
                "Times in them (\"for the first N minutes\", \"0 to 1800 s\", \"from 1800 s\") count from the start of "
                "the battle: measure them only with the BATTLE TIME in each report:\n"
                + orders.strip())
    if rule_lines(rules):
        out += ("\n\nGAME RULES: facts about the game and hard rules of your command. Break one only when the battle "
                "is otherwise lost:\n" + rule_lines(rules))
    if rule_lines(strategy):
        out += ("\n\nSTRATEGY: general advice, sound in most situations. The ORDERS above take precedence wherever "
                "they say something different:\n" + rule_lines(strategy))
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
        self.system = system_prompt(rules, strategy, instructions)   # Standard; der Feldherr uebergibt pro Plan einen aktuellen
        self.last_usage: dict | None = None

    def plan(self, report: str, world: World, alliance: int, system: str | None = None) -> Plan:
        a = self.anthropic
        try:
            resp = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=16000,
                system=[{"type": "text", "text": system or self.system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": report}],
                output_format=WirePlan,   # vereinfachtes Schema, siehe plan.py
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
        return to_plan(resp.parsed_output)

    def plan_deployment(self, report: str, world: World, alliance: int, system: str | None = None) -> Plan:
        """Aufstellung: gleicher Aufruf, der Bericht (describe_deployment) enthaelt Aufgabe und Aufstellungsprompt."""
        return self.plan(report, world, alliance, system)


class RuleGeneral:
    """Ohne Sprachmodell: Artillerie haelt und feuert, Infanterie rueckt in Linie bis 120 m vor und greift dann
    die naechste Einheit an, Kavallerie schuetzt die Flanken. Fuer Offline-Tests der ganzen Kette."""

    def plan(self, report: str, world: World, alliance: int, system: str | None = None) -> Plan:
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

    def plan_deployment(self, report: str, world: World, alliance: int, system: str | None = None) -> Plan:
        """Ohne Modell: Infanterie-Linie am Heeresschwerpunkt mit Front zum Feind, Artillerie 40 m dahinter,
        Kavallerie geteilt auf beide Fluegel."""
        import math
        # in der Aufstellung ist noch nichts uebernommen: alle Einheiten ausser den dem Spieler ueberlassenen
        mine = [u for u in world.side(alliance) if not u.manual and not u.grouped]
        foes = world.enemies(alliance)
        cm, ce = centroid(mine), centroid(foes)
        if not mine or not cm or not ce:
            return Plan(intent="nothing to deploy", note="", groups=[])
        face = bearing_to(cm, ce)
        fx, fz = math.sin(math.radians(face)), math.cos(math.radians(face))
        rx, rz = math.cos(math.radians(face)), -math.sin(math.radians(face))
        art = [u for u in mine if u.role == "artillery"]
        cav = [u for u in mine if is_cavalry(u.role)]
        inf = [u for u in mine if u not in art and u not in cav]
        side = lambda u: (u.x - cm[0]) * rx + (u.z - cm[1]) * rz
        groups: list[GroupOrder] = []
        if inf:
            inf.sort(key=side)
            groups.append(GroupOrder(name="infantry line", unit_ids=[u.id for u in inf], task="move",
                                     target_point=[cm[0], cm[1]], facing_deg=face, shape="line", spacing_m=6))
        if art:
            groups.append(GroupOrder(name="artillery", unit_ids=[u.id for u in art], task="move",
                                     target_point=[cm[0] - fx * 40, cm[1] - fz * 40], facing_deg=face, shape="line",
                                     spacing_m=10))
        width = 30.0 * max(len(inf), 1)
        for name, units, sgn in (("left cavalry", cav[: len(cav) // 2], -1), ("right cavalry", cav[len(cav) // 2:], 1)):
            if units:
                groups.append(GroupOrder(name=name, unit_ids=[u.id for u in units], task="move",
                                         target_point=[cm[0] + rx * sgn * (width / 2 + 40), cm[1] + rz * sgn * (width / 2 + 40)],
                                         facing_deg=face, shape="line", spacing_m=6))
        return Plan(intent="deploy: infantry line facing the enemy, artillery behind, cavalry on both wings",
                    note="rule-based deployment", groups=groups)


def make_general(backend: str, model: str | None, effort: str, rules: str = "", strategy: str = "",
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
