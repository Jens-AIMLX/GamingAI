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
- Be decisive and unambiguous. For every problem give: cause (with the numbers from the report) -> concrete fix
  (which army/agent/building/setting, where, in which order) -> expected effect. Never answer "cannot be fixed from
  the report" when the game mechanics below offer a lever; say which lever and how strong it is likely to be.
- Distinguish quick fixes (this turn) from lasting fixes (several turns). The player needs results NEXT TURN:
  always list the fastest levers first, each with an estimated effect in order points, and add them up against the
  deficit (e.g. lower class -25 needs at least +25: garrison inside the capital +x, tax exemption +4, ...). If the
  quick levers do not close the gap, say so plainly and name the strongest remaining measure (e.g. demolish the
  school/university that drives the reform demand) or the military fallback (strong army inside the capital to crush
  a revolt). Never suggest moves that make the problem worse (e.g. sending a gentleman into a region with reform
  demand, assigning research there).

GAME MECHANICS (Empire: Total War) - use these levers:
Public order and rebellion
- Each region has order for the upper and the lower class; the report lists every factor with its value and the
  game's forecast for next turn. Below 0 the class is unhappy; staying negative leads to riots, then a rebellion
  (rebel army appears, the region can be lost). Fix the worst class first, starting with the largest negative factor.
- Garrison: only units INSIDE the regional capital give "Truppen in Garnison (Repression)"; armies standing elsewhere
  in the region give nothing. More units = more repression. Quick fix: move armies into the capital (end the move on
  the settlement) or recruit cheap units there.
- Taxes: "Steuerlast" falls if the region is made tax-exempt (region panel) or the theatre tax rate for that class is
  lowered (government screen; affects all regions of that theatre). Tax exemption also lets wealth grow.
- Repression also comes from the government type, the ministers (justice/police) and government buildings
  ("Regierungsgebäude") - upgrading the governor's/administrative building in the capital raises it.
- "Forderung nach Reformen" (demand for reform) comes from enlightenment: schools/universities in the region and
  gentlemen (Edelmann) nearby raise it, especially under absolute monarchy. Levers: keep gentlemen out of the
  region, do not build further schools there, in an emergency demolish or downgrade the school/university of the
  region (removes the source), add repression (garrison, governor building), lower taxes; long term a government
  reform (constitutional monarchy/republic) ends the pressure but changes the whole realm.
- "Widerstand gegen Fremdbesatzung" (occupation resistance) shrinks by itself over the turns after a conquest; bridge
  the time with garrison and low taxes.
- "Religiöse Unruhe": the region's religion differs from the state religion - send a missionary of the own faith
  (Missionar) into the region and keep him there; churches of the state religion help.
- "Unterhaltung und Kultur": entertainment/culture buildings (theatre, opera, coffee house etc.) please the upper
  class. "Industrialisierung": factories anger the lower class - counter with garrison/repression.
- A region in open rebellion: crush the rebel army with a strong army, then garrison the capital.
Economy and military
- Damaged buildings produce less until repaired; idle universities waste research; trade routes are lost when a
  port is blockaded - send warships. Armies refill only when standing in own territory and not moving.
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


def problem_regions(st: dict, limit: int = 12) -> list[str]:
    """Regionen mit knapper Ordnung (eine Schicht < 3) im Detail: alle Faktoren beider Schichten mit Wert und
    Prognose des Spiels, Garnison IN der Siedlung (zaehlt) und Truppen nur in der Region (zaehlen nicht), Gebaeude,
    Religion, Steuer, Statthalter, eigene Agenten dort."""
    out = []
    regs = []
    for r in _list(st.get("regions")):
        vals = [x for x in order_totals(r) if x is not None]
        if vals and min(vals) < 3:
            regs.append((min(vals), r))
    for worst, r in sorted(regs, key=lambda x: x[0])[:limit]:
        name, sett = str(r.get("Name", "")), str(r.get("Settlement", ""))
        up, lo = order_totals(r)
        out.append(f"  == {name} (capital {sett}): order upper {up:g}, lower {lo:g}; religion {r.get('ReligionKey')}; "
                   + ("UNTAXED" if r.get("Taxed") is False else
                      f"tax {_num(r.get('UpperTaxPercentage')) * 100:.0f}/{_num(r.get('LowerTaxPercentage')) * 100:.0f}%")
                   + f"; governor {r.get('Governor', '-')}; wealth {r.get('Wealth')}")
        for cls, key in (("upper", "upper_order"), ("lower", "lower_order")):
            fs = _list(_dict(r.get(key)).get("factors"))
            if fs:
                out.append(f"     {cls} class factors: " + ", ".join(
                    f"{'+' if f.get('positive') else '-'}{f.get('value')} {f.get('name')}"
                    + (f" (next turn {f.get('predicted'):+g})" if _num(f.get('predicted')) else "") for f in fs))
        inside = [a for a in _list(st.get("armies")) if str(a.get("Location", "")) == sett]
        outside = [a for a in _list(st.get("armies")) if str(a.get("Location", "")) == name]
        out.append("     garrison INSIDE the capital (counts for repression): "
                   + (", ".join(f"{a.get('Name')} {a.get('Soldiers')} men/{a.get('Units')} units" for a in inside) or "none"))
        if outside:
            out.append("     armies in the region but OUTSIDE the capital (no repression effect): "
                       + ", ".join(f"{a.get('Name')} {a.get('Soldiers')} men/{a.get('Units')} units" for a in outside))
        ag = [a for a in _list(st.get("agents")) if str(a.get("Location", "")) in (sett, name)]
        if ag:
            out.append("     own agents here: " + ", ".join(f"{a.get('AgentType')} {a.get('Name')}" for a in ag))
        bs = [b for b in _list(st.get("buildings")) if str(b.get("region")) == name and b.get("building_key") != "empty"]
        if bs:
            out.append("     buildings: " + ", ".join(f"{b.get('name')} {int(_num(b.get('level')))}/"
                                                     f"{int(_num(b.get('max_level')))} ({b.get('location')})"
                                                     + (f" DAMAGED {int(b['health'])}%" if isinstance(b.get('health'), (int, float)) and b['health'] < 100 else "")
                                                     for b in bs))
        free = [str(b.get("slot_type") or b.get("location")) for b in _list(st.get("buildings"))
                if str(b.get("region")) == name and b.get("building_key") == "empty"]
        if free:
            out.append("     empty slots: " + ", ".join(free))
    if out:
        out.insert(0, "PROBLEM REGIONS IN DETAIL (order below 3 in a class):")
    return out


def brief(st: dict, include_fog: bool = False) -> str:
    m, fa, sta = _dict(st.get("meta")), _dict(st.get("faction")), _dict(st.get("state"))
    fin = finances(st)
    mtypes = sorted({str(a.get("AgentType")) for a in _list(st.get("agents")) if a.get("AgentType")})
    L = [f"GOVERNMENT: {sta.get('DatabaseKey')} (popularity {sta.get('Popularity')}); own agent types: "
         + (", ".join(mtypes) or "-")]
    L += [f"REALM: {fa.get('Name', m.get('faction'))} - turn {m.get('turn')}, {m.get('season')} {m.get('year')}, "
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
    L += problem_regions(st)

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


def mentions(text: str, tmap: dict[str, str]) -> list[tuple[str, str]]:
    """Bekannte Namen (Armeen, Flotten, Agenten, Regionen, Siedlungen, Orte) im Text, in Reihenfolge des Auftretens:
    (Name wie im Text, Kamerasprung). Nur ganze Woerter ab 4 Zeichen; laengere Namen gehen vor (\"Neu-Granada\"
    statt \"Granada\"); jedes Ziel einmal."""
    import re
    low = str(text or "").lower()
    found: list[tuple[int, int, str]] = []
    for k in sorted((k for k in tmap if len(k) >= 4), key=len, reverse=True):
        for m in re.finditer(r"(?<![\w-])" + re.escape(k) + r"(?![\w-])", low):
            if not any(s <= m.start() < e or s < m.end() <= e for s, e, _ in found):
                found.append((m.start(), m.end(), k))
    out, seen = [], set()
    for s, e, k in sorted(found):
        if tmap[k] not in seen:
            seen.add(tmap[k])
            out.append((str(text)[s:e], tmap[k]))
    return out


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
