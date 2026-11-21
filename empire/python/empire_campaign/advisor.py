"""Stufe 2: Claude als Berater auf der Kampagnenkarte.

- brief(st, include_fog): der Lagebericht kompakt als Text fuer das Modell (Kasse, Regionen, Bauplaetze, Forschung,
  Armeen mit Staerke/Zusammensetzung, Flotten, Agenten, Feinde, Diplomatie, Handel, Missionen, Ereignisse und die
  Empfehlungen nach festen Regeln). Nebel des Krieges wie im Bericht: ohne include_fog erfaehrt das Modell von
  verborgenen Feinden nichts, auch nicht ihre Anzahl.
- advise(...): Zugplan (Lage, Massnahmen mit Ort, Risiken) oder Antwort auf eine Frage, strukturiert ueber
  messages.parse. Fruehere Fragen und Antworten desselben Zugs gehen als Verlauf mit (Rueckfragen).
- targets(st): Ortsname -> Kamerasprung, damit jede Massnahme in der Liste angesprungen werden kann.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from .report import (EVENT_NAMES, _dict, _list, _money, _num, enemy_armies, finances, order_issues, order_totals,
                     recommendations, strength, unit_scale, target_pos, target_region, target_slot)

MAX_ROWS = 120             # je Liste hoechstens so viele Zeilen in den Bericht (Tokens)

SYSTEM = """You are the strategic advisor of the player in Total War: EMPIRE (grand campaign map, turn-based).
You get the player's current state of the realm as captured from the running game, plus rule-based findings.
Advise like an experienced general and minister: concrete, prioritised, feasible in THIS turn with the available
money, armies, fleets and agents. Name places, armies and fleets exactly as written in the report so the player can
find them (the UI jumps to the place you put into 'where').

Rules:
- Answer in German. Short, clear sentences. No filler.
- Only use facts from the report. If something is not in the report, say so instead of guessing.
- Enemy forces: only those listed. If the report says hidden enemies are excluded, do not speculate about them.
- Money: respect the treasury and the yearly balance; do not plan more spending than affordable.
- Each action: one concrete step (what, where, why). 'where' = exactly one place, army, fleet or region name from
  the report, or empty.
- Order actions by priority (1 = must do now, 2 = should do this turn, 3 = when convenient). At most 12 actions.
- For a question: answer it directly in 'summary' (may be several paragraphs), add actions only if they follow from
  the answer.
"""


class Action(BaseModel):
    prio: int = Field(description="1 dringend, 2 wichtig, 3 bei Gelegenheit")
    area: str = Field(description="Bereich, z. B. Militär, Finanzen, Bau, Ordnung, Forschung, Diplomatie, Handel")
    action: str = Field(description="konkreter Schritt in diesem Zug")
    reason: str = Field(description="kurze Begründung")
    where: str = Field(default="", description="genau ein Ort/Armee/Flotte/Region aus dem Bericht oder leer")


class Advice(BaseModel):
    summary: str = Field(description="Lage in 2-4 Sätzen bzw. die Antwort auf die Frage")
    actions: list[Action] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list, description="Gefahren, die der Spieler im Blick behalten muss")


@dataclass
class Exchange:
    question: str                      # "" = Zugplan
    advice: Advice
    model: str = ""
    usage: dict = field(default_factory=dict)


# ---------------- Lagebericht fuer das Modell ----------------
def _cut(rows: list[str], label: str) -> list[str]:
    if len(rows) > MAX_ROWS:
        return rows[:MAX_ROWS] + [f"  ... {len(rows) - MAX_ROWS} weitere {label}"]
    return rows


def brief(st: dict, include_fog: bool = False) -> str:
    m, fa, sta = _dict(st.get("meta")), _dict(st.get("faction")), _dict(st.get("state"))
    fin = finances(st)
    L = [f"REALM: {fa.get('Name', m.get('faction'))} - turn {m.get('turn')}, {m.get('season')} {m.get('year')}, "
         f"ranking power {fa.get('PowerRanking')} / prestige {fa.get('PrestigeRanking')} / wealth {fa.get('WealthRanking')}",
         f"MONEY: treasury {_money(fin['treasury'])}, balance per year {_money(fin['net_year'])} "
         f"(income {_money(sta.get('AnnualIncome'))}, army upkeep {_money(sta.get('ArmyUpkeep'))}, "
         f"navy upkeep {_money(sta.get('NavyUpkeep'))})"]
    vic = _dict(fa.get("victory"))
    if vic:
        L.append(f"VICTORY: {vic.get('TotalRegionsNeeded', '?')} regions needed; "
                 + ", ".join(str(r.get("Name")) for r in _list(vic.get("regions"))[:30]))

    rows = []
    for r in sorted(_list(st.get("regions")), key=lambda r: min([x for x in order_totals(r) if x is not None] or [9])):
        up, lo = order_totals(r)
        tax = "untaxed" if r.get("Taxed") is False else (
            f"tax {_num(r.get('UpperTaxPercentage')) * 100:.0f}/{_num(r.get('LowerTaxPercentage')) * 100:.0f}%")
        worst = min([x for x in (up, lo) if x is not None] or [9])
        iss = order_issues(r) if worst < 3 else []           # Ursachen nur, wo die Ordnung knapp ist
        rows.append(f"  {r.get('Name')} ({r.get('Settlement')}): order {up:g}/{lo:g}, {tax}, wealth {r.get('Wealth')}"
                    if up is not None and lo is not None else f"  {r.get('Name')} ({r.get('Settlement')}): {tax}")
        if iss:
            rows[-1] += f"; unrest: {', '.join(iss[:3])}"
    L.append(f"REGIONS ({len(rows)}), worst order first (order = upper/lower class, negative = unrest):")
    L += _cut(rows, "regions")

    bl = _list(st.get("buildings"))
    dmg = [b for b in bl if isinstance(b.get("health"), (int, float)) and b["health"] < 100]
    empty: dict[str, list[str]] = {}
    for b in bl:
        if b.get("building_key") == "empty":
            empty.setdefault(str(b.get("region")), []).append(str(b.get("slot_type") or b.get("location")))
    upg = [b for b in bl if _num(b.get("level"), -1) >= 0 and _num(b.get("level")) < _num(b.get("max_level"))]
    L.append(f"BUILDINGS: {len(bl)} slots, {sum(len(v) for v in empty.values())} empty, {len(upg)} upgradable, "
             f"{len(dmg)} damaged")
    L += [f"  damaged: {b.get('name')} in {b.get('location')} ({b.get('region')}) {int(b['health'])}%" for b in dmg[:30]]
    L += _cut([f"  empty in {k}: {', '.join(v)}" for k, v in sorted(empty.items(), key=lambda kv: -len(kv[1]))],
              "regions with empty slots")
    by_reg: dict[str, list[str]] = {}
    for b in upg:
        by_reg.setdefault(str(b.get("region")), []).append(
            f"{b.get('name')} {int(_num(b.get('level')))}/{int(_num(b.get('max_level')))}")
    L += _cut([f"  upgradable in {k} ({len(v)}): {', '.join(v[:5])}" + (" ..." if len(v) > 5 else "")
               for k, v in sorted(by_reg.items(), key=lambda kv: -len(kv[1]))], "regions with upgradable buildings")

    L.append("RESEARCH:")
    L += [f"  {u.get('Name')} ({u.get('RegionKey')}): " + (f"{u.get('technology')}, {u.get('turns_left')} turns left"
                                                            if u.get("technology") else "IDLE")
          for u in _list(st.get("research"))]

    def force(c: dict, kind: str) -> str:
        sg = strength(c, unit_scale(st))
        comp = ", ".join(f"{n} {k}" for k, n in sg["classes"][:5]) if sg else ""
        s = (f"  {kind} {c.get('Name')} at {c.get('Location')}: {c.get('Soldiers', '?')} men, {c.get('Units')} units, "
             f"moves {c.get('ActionPoints')}/{c.get('ActionPointsPerTurn')}")
        if sg:
            s += f", {sg['pct']:.0f}%" + (f" ({sg['hurt']} weak)" if sg["hurt"] else "")
            if sg["cost"]:
                s += f", refill {_money(sg['cost'])}"
            s += f"; {comp}"
        return s
    armies = sorted(_list(st.get("armies")), key=lambda c: -_num(c.get("Soldiers")))
    L.append(f"OWN ARMIES ({len(armies)}):")
    L += _cut([force(c, "army") for c in armies], "armies")
    navies = _list(st.get("navies"))
    L.append(f"OWN FLEETS ({len(navies)}):")
    L += _cut([force(c, "fleet") for c in navies], "fleets")
    agents = _list(st.get("agents"))
    L.append(f"OWN AGENTS ({len(agents)}):")
    L += _cut([f"  {c.get('AgentType')} {c.get('Name')} at {c.get('Location')}, moves {c.get('ActionPoints')}/"
               f"{c.get('ActionPointsPerTurn')}" for c in agents], "agents")

    enemies = enemy_armies(st, include_fog)
    L.append("ENEMY FORCES (factions at war) - " + ("ALL, including those hidden in the fog of war:" if include_fog
             else "only those currently visible to the player (hidden ones are excluded on purpose):"))
    L += _cut([f"  {e.get('faction')} {'army' if e['kind'] == 'armies' else 'fleet'} {e.get('Name')} at "
               f"{e.get('Location')}: {e.get('Soldiers', '?')} men, {e.get('Units')} units"
               + (", IN OWN REGION" if e["in_own_region"] else f", distance {e['distance']:.0f}")
               + ("" if e["visible"] else " [hidden]") for e in sorted(enemies, key=lambda e: e["distance"])],
              "enemy forces")
    if not enemies:
        L.append("  none")

    d = _dict(st.get("diplomacy"))
    L.append("DIPLOMACY: " + "; ".join(f"{cat}: " + (", ".join(str(x.get("name")) for x in _list(d.get(cat))) or "-")
                                      for cat in ("AtWar", "Allies", "Protectorates", "ProtectorOf", "TradeRights")))
    hostile = [f for f in _list(st.get("factions")) if _num(f.get("AttitudeValue")) <= -50]
    if hostile:
        L.append("  hostile attitude: " + ", ".join(f"{f.get('Name')} ({f.get('Attitude')})" for f in hostile[:20]))

    tr = _dict(st.get("trade"))
    trows = []
    for kind in ("sources", "partners"):
        for r in _list(tr.get(kind)):
            for g in _list(r.get("goods")):
                lost = _num(g.get("value_blockaded")) + _num(g.get("value_raided"))
                tip = " ".join(str(g.get("tooltip", "")).split())[:80]
                trows.append(f"  {r.get('entry_name')}: {tip}" + (f", LOST {_money(lost)}" if lost else "")
                             + (f", piracy {r.get('percent_pilfered')}%" if _num(r.get("percent_pilfered")) else ""))
    if trows:
        L.append("TRADE:")
        L += _cut(trows, "trade goods")
    mis = _dict(st.get("missions"))
    if _list(mis.get("active")):
        L.append("ACTIVE MISSIONS:")
        L += [f"  {e}" for e in _list(mis.get("active"))[:10]]
    ev = _dict(st.get("events"))
    if ev:
        L.append("EVENTS SINCE LAST OWN TURN: " + ", ".join(
            f"{EVENT_NAMES.get(k, k)} {v.get('own', 0)}x own/{v.get('all', 0)}x total"
            for k, v in ev.items() if isinstance(v, dict) and v.get("own")))
    adv = [a for a in recommendations(st, include_fog) if a.prio <= 2]     # Hinweise stehen oben schon als Daten
    L.append(f"RULE-BASED FINDINGS, urgent and important ({len(adv)}):")
    L += [f"  [{a.prio}] {a.area}: {a.text}" for a in adv[:40]]
    if len(adv) > 40:
        L.append(f"  ... {len(adv) - 40} more")
    return "\n".join(L)


def targets(st: dict, include_fog: bool = False) -> dict[str, str]:
    """Namen aus dem Bericht -> Kamerasprung (Armeen/Flotten/Agenten/Feinde nach Name und Ort, Regionen, Siedlungen)."""
    t: dict[str, str] = {}
    for r in _list(st.get("regions")):
        for name in (r.get("Name"), r.get("Settlement")):
            if name:
                t.setdefault(str(name).lower(), target_region(r.get("Name")))
    for b in _list(st.get("buildings")):
        if b.get("location") and b.get("region_key") and b.get("slot_key"):
            t.setdefault(str(b["location"]).lower(), target_slot(b["region_key"], b["slot_key"]))
    for kind in ("armies", "navies", "agents"):
        for c in _list(st.get(kind)):
            tp = target_pos(c)
            if tp and c.get("Name"):
                t[str(c["Name"]).lower()] = tp
    for e in enemy_armies(st, include_fog):
        tp = target_pos(e)
        if tp and e.get("Name"):
            t.setdefault(str(e["Name"]).lower(), tp)
    return t


def target_for(where: str, tmap: dict[str, str]) -> str:
    """Ort aus der Antwort zuordnen: exakt, sonst der laengste bekannte Name, der darin vorkommt."""
    w = (where or "").strip().lower()
    if not w:
        return ""
    if w in tmap:
        return tmap[w]
    hits = [k for k in tmap if k and len(k) >= 4 and k in w]
    return tmap[max(hits, key=len)] if hits else ""


# ---------------- Aufruf ----------------
def _call(client, *, model: str, effort: str, system: str, messages: list) -> tuple[Advice, dict]:
    resp = client.messages.parse(
        model=model, max_tokens=16000,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=messages, output_format=Advice, output_config={"effort": effort})
    if resp.stop_reason == "refusal":
        raise RuntimeError("Das Modell hat die Antwort abgelehnt")
    if resp.parsed_output is None:
        raise RuntimeError(f"keine gültige Antwort (stop_reason {resp.stop_reason})")
    u = getattr(resp, "usage", None)
    usage = {k: getattr(u, k, 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                           "cache_creation_input_tokens")} if u else {}
    return resp.parsed_output, usage


def advise(st: dict, question: str = "", *, history: list[Exchange] | None = None, include_fog: bool = False,
           api_key: str | None = None, model: str = "claude-sonnet-5-5", effort: str = "medium",
           client=None) -> Exchange:
    """Zugplan (question leer) oder Antwort auf eine Frage. history: fruehere Wechsel desselben Zugs."""
    import anthropic

    # Verlauf ohne Lagebericht (nur Fragen und Antworten), der aktuelle Lagebericht einmal in der letzten Nachricht
    report = brief(st, include_fog)
    msgs: list[dict] = []
    for ex in history or []:
        msgs.append({"role": "user", "content": ex.question or "Give me the plan for this turn."})
        msgs.append({"role": "assistant", "content": ex.advice.model_dump_json()})
    q = question.strip() or "Give me the plan for this turn."
    msgs.append({"role": "user", "content": "CURRENT STATE OF THE REALM (live from the game"
                 + (", may have changed since the earlier answers" if history else "") + "):\n-----\n"
                 + report + "\n-----\n\n" + q})
    if client is None:
        client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=180.0)
    try:
        adv, usage = _call(client, model=model, effort=effort, system=SYSTEM, messages=msgs)
    except anthropic.AuthenticationError as e:
        raise RuntimeError("Anmeldung fehlgeschlagen – API-Schlüssel prüfen") from e
    except anthropic.RateLimitError as e:
        raise RuntimeError(f"Rate-Limit: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"API-Fehler {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise RuntimeError("Keine Verbindung zur Claude-API") from e
    return Exchange(question=question.strip(), advice=adv, model=model, usage=usage)
