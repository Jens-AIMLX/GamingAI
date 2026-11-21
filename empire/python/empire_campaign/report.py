"""Bestandsbericht und Handlungsempfehlungen (feste Regeln, Stufe 1) aus ai_bridge/campaign_state.json.

Nebel des Krieges: die Erfassung liefert die Armeen aller Kriegsgegner, auch die verborgenen. Ohne den Schalter
include_fog nennt und verwendet der Bericht nur Feinde in Sichtweite - genaehert, weil das Spiel keine
Sichtbarkeitsabfrage hat: eine Feindarmee gilt als sichtbar, wenn sie in einer eigenen Region/Siedlung/Ortschaft steht
oder naeher als VIS_RADIUS (Kartenkoordinaten) an einer eigenen Armee, Flotte oder einem Agenten. Verborgene Feinde
tauchen ohne Schalter nirgends auf, auch nicht als Anzahl.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

VIS_RADIUS = 25.0          # Kartenkoordinaten (eine Armee zieht je Zug grob 20-30)
FULL_STACK = 20            # Einheiten einer vollen Armee
TURNS_PER_YEAR = 2         # Sommer/Winter

PRIO_NAME = {1: "dringend", 2: "wichtig", 3: "Hinweis"}

EVENT_NAMES = {
    "CampaignBuildingDamaged": "Gebäude beschädigt", "CampaignSettlementAttacked": "Siedlung angegriffen",
    "CampaignSlotAttacked": "Ortschaft angegriffen", "SiegeLifted": "Belagerung aufgehoben",
    "LandTradeRouteRaided": "Handelsweg zu Land überfallen", "SeaTradeRouteRaided": "Handelsweg zur See überfallen",
    "RegionRiots": "Unruhen", "RegionRebels": "Aufstand", "RegionIssuesDemands": "Forderungen einer Region",
    "SettlementOccupied": "Siedlung besetzt", "GarrisonResidenceCaptured": "Garnisonsort erobert",
    "BuildingCompleted": "Bau fertiggestellt", "ResearchCompleted": "Forschung abgeschlossen",
    "UnitTrained": "Einheit ausgebildet", "CharacterCompletedBattle": "Schlacht geschlagen",
    "CharacterCreated": "neue Person", "CharacterPromoted": "Beförderung", "MissionIssued": "neuer Auftrag",
    "MissionSucceeded": "Auftrag erfüllt", "MissionFailed": "Auftrag gescheitert",
    "MissionNearingExpiry": "Auftrag läuft bald ab", "FactionGovernmentTypeChanged": "Regierungsform geändert",
    "DuelFought": "Duell", "AssassinationAttemptSuccess": "Attentat gelungen",
    "SufferAssassinationAttempt": "Attentat auf eigene Person", "SabotageAttemptSuccess": "Sabotage gelungen",
    "ArmySabotageAttemptSuccess": "Heeressabotage gelungen", "SpyingAttemptSuccess": "Spionage gelungen",
    "SufferSpyingAttempt": "fremde Spionage", "EspionageAgentApprehended": "Agent gefasst",
    "TradeRouteEstablished": "Handelsweg eröffnet", "TradeLinkEstablished": "Handelsverbindung",
    "SlotOpens": "neuer Bauplatz", "VictoryConditionMet": "Siegbedingung erfüllt",
    "VictoryConditionFailed": "Siegbedingung verfehlt", "CampaignArmiesMerge": "Armeen vereinigt",
}


@dataclass
class Advice:
    prio: int           # 1 dringend, 2 wichtig, 3 Hinweis
    area: str           # Finanzen, Ordnung, Bau, Forschung, Militaer, Diplomatie, Lage
    text: str
    reason: str = ""
    target: str = ""    # Kamerasprung (campaign_zoom.txt): "pos\tX\tY", "slot\tregion_key\tslot_key", "region\tName"

    def line(self) -> str:
        return f"[{PRIO_NAME[self.prio]}] {self.area}: {self.text}" + (f" - {self.reason}" if self.reason else "")


# ---------------- Laden und Hilfen ----------------
def target_pos(c: dict) -> str:
    """Sprungziel einer Figur (Armee, Flotte, Agent, Feind) - nur Kartenposition, kein Zeiger."""
    x, y = c.get("PosX"), c.get("PosY")
    return f"pos\t{x}\t{y}" if isinstance(x, (int, float)) and isinstance(y, (int, float)) else ""


def target_slot(region_key, slot_key) -> str:
    return f"slot\t{region_key}\t{slot_key}" if region_key and slot_key else ""


def target_region(name) -> str:
    return f"region\t{name}" if name else ""


def unit_scale(st: dict) -> float:
    """Einheitengroesse des Spiels relativ zur Sollstaerke im Einheitenrekord (MaxMen gilt fuer die groesste
    Einstellung; bei 'gross' sind volle Einheiten z. B. 120 von 160 = 0,75): haeufigstes Verhaeltnis Men/MaxMen
    ueber alle eigenen Einheiten (nur Werte <= 1)."""
    if "_unit_scale" in st:
        return st["_unit_scale"]
    counts: dict[float, int] = {}
    for kind in ("armies", "navies"):
        for c in _list(st.get(kind)):
            for u in _list(c.get("units")):
                mx = _num(u.get("MaxMen")) if isinstance(u, dict) else 0
                if mx > 0 and 0 < _num(u.get("Men")) <= mx:
                    r = round(_num(u.get("Men")) / mx, 2)
                    counts[r] = counts.get(r, 0) + 1
    scale = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0] if counts else 1.0
    st["_unit_scale"] = scale
    return scale


def strength(c: dict, scale: float = 1.0) -> dict | None:
    """Staerke einer Armee/Flotte aus ihren Einheiten (Erfassung 'armeeinhalt'): Maenner gegen die volle Staerke bei
    der Einheitengroesse des Spiels (scale * MaxMen, siehe unit_scale), Anteil, unterbesetzte Einheiten, Kosten zum
    Auffuellen/Reparieren, Zusammensetzung nach Klasse (Spielnamen)."""
    units = [u for u in _list(c.get("units")) if isinstance(u, dict)]
    if not units:
        return None
    men = full = 0.0
    hurt = 0
    for u in units:
        m, mx = _num(u.get("Men")), _num(u.get("MaxMen")) * scale
        if mx <= 0 or m > mx * 1.05:                 # ohne Soll oder ungewoehnlich (z. B. Geschuetze): als voll werten
            mx = m
        men += min(m, mx)
        full += mx
        if m < mx * 0.97:
            hurt += 1
    classes: dict[str, int] = {}
    for u in units:
        k = str(u.get("Class") or u.get("Name") or "?")
        classes[k] = classes.get(k, 0) + 1
    return {"men": men, "full": full, "pct": 100 * men / full if full else 100.0, "hurt": hurt,
            "cost": sum(_num(u.get("RepairCost")) for u in units if u.get("NeedsRepair")),
            "classes": sorted(classes.items(), key=lambda kv: -kv[1]),
            "upkeep": sum(_num(u.get("UpkeepCost")) for u in units)}


def load_state(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def _list(v) -> list:
    return v if isinstance(v, list) else []


def _dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _money(v) -> str:
    return f"{_num(v):,.0f}".replace(",", ".")


def _pos(c: dict) -> tuple[float, float] | None:
    if isinstance(c.get("PosX"), (int, float)) and isinstance(c.get("PosY"), (int, float)):
        return float(c["PosX"]), float(c["PosY"])
    return None


def own_places(st: dict) -> set[str]:
    places = set()
    for r in _list(st.get("regions")):
        for k in ("Name", "Settlement"):
            if r.get(k):
                places.add(str(r[k]).lower())
    for b in _list(st.get("buildings")):
        if b.get("location"):
            places.add(str(b["location"]).lower())
    return places


def own_positions(st: dict) -> list[tuple[float, float]]:
    out = []
    for key in ("armies", "navies", "agents"):
        for c in _list(st.get(key)):
            p = _pos(c)
            if p:
                out.append(p)
    return out


def enemy_armies(st: dict, include_fog: bool) -> list[dict]:
    """Feindarmeen/-flotten mit Feld 'visible'; ohne include_fog nur die sichtbaren."""
    places, positions = own_places(st), own_positions(st)
    out = []
    for e in _list(st.get("enemies")):
        for kind in ("armies", "navies"):
            for c in _list(e.get(kind)):
                p = _pos(c)
                near = min((math.dist(p, q) for q in positions), default=1e9) if p else 1e9
                in_own = str(c.get("Location", "")).lower() in places
                visible = in_own or near <= VIS_RADIUS
                if visible or include_fog:
                    out.append(dict(c, faction=e.get("faction"), kind=kind, visible=visible, in_own_region=in_own,
                                    distance=near))
    return out


def order_issues(r: dict) -> list[str]:
    """Negative Ordnungsfaktoren (beide Schichten), groesste zuerst."""
    neg = []
    for cls, key in (("Oberschicht", "upper_order"), ("Unterschicht", "lower_order")):
        for f in _list(_dict(r.get(key)).get("factors")):
            if f.get("positive") is False and _num(f.get("value")) > 0:
                neg.append((_num(f.get("value")), f"{f.get('name')} {int(_num(f.get('value')))} ({cls})"))
    return [t for _, t in sorted(neg, reverse=True)]


def order_totals(r: dict) -> tuple[float | None, float | None]:
    up = _dict(r.get("upper_order")).get("total")
    lo = _dict(r.get("lower_order")).get("total")
    return (_num(up) if up is not None else None, _num(lo) if lo is not None else None)


def finances(st: dict) -> dict:
    s = _dict(st.get("state"))
    income = _num(s.get("AnnualIncome"))
    upkeep = _num(s.get("ArmyUpkeep")) + _num(s.get("NavyUpkeep")) + _num(s.get("Policing"))
    return {"treasury": _num(s.get("Treasury")), "income": income, "upkeep": upkeep, "net_year": income - upkeep,
            "net_turn": (income - upkeep) / TURNS_PER_YEAR}


# ---------------- Bestandsbericht ----------------
def asset_report(st: dict, include_fog: bool = False) -> str:
    m, s, fa = _dict(st.get("meta")), _dict(st.get("state")), _dict(st.get("faction"))
    fin = finances(st)
    L: list[str] = []
    L.append(f"BESTANDSBERICHT - {fa.get('Name') or m.get('faction')}, Zug {m.get('turn')}, "
             f"{m.get('season')} {m.get('year')}")
    L.append(f"Rang: Macht {fa.get('PowerRanking', '?')}, Prestige {fa.get('PrestigeRanking', '?')}, "
             f"Reichtum {fa.get('WealthRanking', '?')}")
    vic = _dict(fa.get("victory"))
    if vic.get("TotalRegionsNeeded"):
        L.append(f"Siegbedingung: {vic.get('TotalRegionsNeeded')} Regionen"
                 + (f", darunter {len(_list(vic.get('regions')))} bestimmte" if _list(vic.get("regions")) else ""))
    L.append("")
    L.append("STAAT")
    L.append(f"  Kasse {_money(fin['treasury'])} | Einnahmen/Jahr {_money(fin['income'])} (Steuern "
             f"{_money(s.get('TaxIncomeTotal'))}, Handel {_money(s.get('Trade'))}, Sonstiges {_money(s.get('OtherIncome'))})")
    L.append(f"  Unterhalt/Jahr {_money(fin['upkeep'])} (Heer {_money(s.get('ArmyUpkeep'))}, Flotte "
             f"{_money(s.get('NavyUpkeep'))}, Polizei {_money(s.get('Policing'))}) | Saldo/Jahr {_money(fin['net_year'])}")
    extra = [f"{k} {s[k]}" for k in ("Popularity", "PopularityStatus", "Prosperity", "Prestige", "NextElections") if k in s]
    if extra:
        L.append("  " + ", ".join(extra))
    for g in _list(st.get("governorships")):
        L.append(f"  Statthalterschaft {g.get('Name')}: Steuersatz Ober/Unter {g.get('UpperTaxRate')}/{g.get('LowerTaxRate')}"
                 f"{', Steuerautomatik' if g.get('AutomanageTaxes') else ''}"
                 f"{', Bauautomatik' if g.get('AutomanageConstruction') else ''}"
                 + (f", Statthalter {g['Governor']}" if g.get("Governor") else ""))
    regions = _list(st.get("regions"))
    L.append("")
    L.append(f"REGIONEN ({len(regions)})")
    ranked = sorted(regions, key=lambda r: min(x for x in order_totals(r) if x is not None) if any(
        x is not None for x in order_totals(r)) else 99)
    for r in ranked[:12]:
        up, lo = order_totals(r)
        L.append(f"  {r.get('Name')} ({r.get('Settlement')}): Bevölkerung {r.get('Population')}, Ordnung Ober/Unter "
                 f"{'?' if up is None else int(up)}/{'?' if lo is None else int(lo)}"
                 f"{'' if r.get('Taxed', True) else ', steuerfrei'}")
    if len(regions) > 12:
        L.append(f"  ... {len(regions) - 12} weitere mit ruhigerer Lage")
    bl = _list(st.get("buildings"))
    empty = [b for b in bl if b.get("building_key") == "empty"]
    damaged = [b for b in bl if isinstance(b.get("health"), (int, float)) and b["health"] < 100]
    upgr = [b for b in bl if _num(b.get("level"), -1) >= 0 and _num(b.get("level")) < _num(b.get("max_level"))]
    L.append("")
    L.append(f"GEBÄUDE: {len(bl)} Plätze, davon {len(empty)} frei, {len(damaged)} beschädigt, {len(upgr)} ausbaufähig")
    for b in sorted(damaged, key=lambda b: b["health"])[:10]:
        L.append(f"  beschädigt: {b.get('name')} in {b.get('location')} ({b.get('region')}) - Zustand {int(b['health'])}%")
    unknown = sorted({f for b in bl for f in _list(b.get("unknown_fields"))})
    if unknown:
        L.append(f"  neue Slot-Felder erkannt (vermutlich Bau im Gang): {', '.join(unknown)}")
    L.append("")
    L.append("FORSCHUNG")
    for u in _list(st.get("research")):
        cur = f"{u['technology']} (noch {u.get('turns_left', '?')} Züge)" if u.get("technology") else "FORSCHT NICHTS"
        L.append(f"  {u.get('Name')} ({u.get('RegionKey')}): {cur}")
    armies, navies, agents = _list(st.get("armies")), _list(st.get("navies")), _list(st.get("agents"))
    L.append("")
    L.append(f"MILITÄR: {len(armies)} Armeen ({sum(int(_num(a.get('Soldiers'))) for a in armies)} Soldaten), "
             f"{len(navies)} Flotten, {len(agents)} Agenten")
    for a in sorted(armies, key=lambda a: -_num(a.get("Soldiers")))[:15]:
        L.append(f"  {a.get('Name')} in {a.get('Location')}: {int(_num(a.get('Soldiers')))} Soldaten, "
                 f"{int(_num(a.get('Units')))} Einheiten")
    if len(armies) > 15:
        L.append(f"  ... {len(armies) - 15} weitere Armeen")
    types: dict[str, int] = {}
    for a in agents:
        types[str(a.get("AgentType"))] = types.get(str(a.get("AgentType")), 0) + 1
    if types:
        L.append("  Agenten: " + ", ".join(f"{n}x {t}" for t, n in sorted(types.items())))
    d = _dict(st.get("diplomacy"))
    L.append("")
    L.append("DIPLOMATIE")
    for cat, label in (("AtWar", "Krieg mit"), ("Allies", "Verbündete"), ("Protectorates", "Protektorate"),
                       ("ProtectorOf", "Schutzmacht von"), ("TradeRights", "Handelsrechte")):
        names = [x.get("name") or x.get("key") for x in _list(d.get(cat))]
        L.append(f"  {label}: {', '.join(names) if names else '-'}")
    hostile = sorted((f for f in _list(st.get("factions")) if isinstance(f.get("AttitudeValue"), (int, float))),
                     key=lambda f: f["AttitudeValue"])[:5]
    if hostile:
        L.append("  Haltung (am feindseligsten): " + ", ".join(
            f"{f.get('Name')} {f.get('Attitude')} ({int(f['AttitudeValue'])})" for f in hostile))
    en = enemy_armies(st, include_fog)
    L.append("")
    L.append("FEINDE" + (" (Nebel des Krieges EINBEZOGEN)" if include_fog else " (nur sichtbare)"))
    if not en:
        L.append("  keine feindliche Armee in Sichtweite" if not include_fog else "  keine feindlichen Armeen")
    for e in sorted(en, key=lambda e: (not e["in_own_region"], e["distance"]))[:15]:
        tag = "" if e["visible"] else " [im Nebel]"
        where = "in EIGENER Region " if e["in_own_region"] else ""
        L.append(f"  {e.get('faction')}: {e.get('Name')} {where}{e.get('Location')}, {int(_num(e.get('Soldiers')))} "
                 f"Soldaten, {int(_num(e.get('Units')))} Einheiten{tag}")
    ev = _dict(st.get("events"))
    own = [(EVENT_NAMES.get(k, k), v) for k, v in ev.items() if isinstance(v, dict) and v.get("own")]
    L.append("")
    L.append("SEIT DEM LETZTEN ZUG (eigene Fraktion)")
    if not own:
        L.append("  keine besonderen Ereignisse")
    for name, v in sorted(own):
        won = f" (gewonnen {v.get('own_won', 0)}, verloren {v.get('own_lost', 0)})" if "own_won" in v or "own_lost" in v else ""
        L.append(f"  {name}: {v['own']}x{won}")
    probs = _list(st.get("errors")) + [f"ausgelassen: {x}" for x in _list(st.get("skipped"))]
    if probs:
        L.append("")
        L.append("DATENLAGE: " + "; ".join(str(p) for p in probs))
    return "\n".join(L)


# ---------------- Handlungsempfehlungen (feste Regeln) ----------------
def recommendations(st: dict, include_fog: bool = False) -> list[Advice]:
    A: list[Advice] = []
    fin = finances(st)
    if fin["treasury"] < 0:
        A.append(Advice(1, "Finanzen", f"Kasse im Minus ({_money(fin['treasury'])})",
                        "Ausgaben kürzen (Truppen entlassen), Steuern prüfen, keine neuen Bauten"))
    if fin["net_year"] < 0:
        turns = fin["treasury"] / -fin["net_turn"] if fin["net_turn"] < 0 and fin["treasury"] > 0 else 0
        A.append(Advice(1 if turns < 5 else 2, "Finanzen",
                        f"Unterhalt übersteigt die Einnahmen um {_money(-fin['net_year'])} im Jahr",
                        f"die Kasse reicht noch etwa {turns:.0f} Züge" if turns else "Kasse aufgebraucht"))
    # Ordnung
    for r in _list(st.get("regions")):
        up, lo = order_totals(r)
        worst = min((x for x in (up, lo) if x is not None), default=None)
        if worst is None:
            continue
        issues = order_issues(r)
        if worst < 0:
            fix = []
            if any(i.startswith("Steuerlast") for i in issues) and r.get("Taxed", True):
                fix.append("Steuern senken oder Region steuerfrei stellen")
            fix.append("Garnison verstärken / Ordnungsgebäude bauen")
            A.append(Advice(1 if worst <= -3 else 2, "Ordnung",
                            f"{r.get('Name')} ({r.get('Settlement')}): Unzufriedenheit {int(worst)} - " + "; ".join(fix),
                            "Ursachen: " + ", ".join(issues[:3]) if issues else "", target_region(r.get("Name"))))
        elif not r.get("Taxed", True) and worst >= 3:
            A.append(Advice(3, "Finanzen", f"{r.get('Name')} ist steuerfrei, aber ruhig (Ordnung {int(worst)})",
                            "wieder besteuern bringt Einnahmen"))
    # Gebaeude
    bl = _list(st.get("buildings"))
    for b in sorted((b for b in bl if isinstance(b.get("health"), (int, float)) and b["health"] < 100),
                    key=lambda b: b["health"]):
        A.append(Advice(1 if b["health"] < 50 else 2, "Bau",
                        f"{b.get('name')} in {b.get('location')} ({b.get('region')}) reparieren - Zustand {int(b['health'])}%",
                        "beschädigte Gebäude bringen weniger oder nichts",
                        target_slot(b.get("region_key"), b.get("slot_key"))))
    empty_by_region: dict[str, list[str]] = {}
    for b in bl:
        if b.get("building_key") == "empty":
            empty_by_region.setdefault(str(b.get("region")), []).append(str(b.get("slot_type") or b.get("location")))
    if empty_by_region:
        rich = fin["treasury"] > 5000
        top = sorted(empty_by_region.items(), key=lambda kv: -len(kv[1]))
        A.append(Advice(2 if rich else 3, "Bau",
                        f"{sum(len(v) for v in empty_by_region.values())} freie Bauplätze in {len(empty_by_region)} Regionen"
                        + (f" (Kasse {_money(fin['treasury'])})" if rich else ""),
                        "meiste: " + ", ".join(f"{k} {len(v)}" for k, v in top[:5])))
    upgr = [b for b in bl if _num(b.get("level"), -1) >= 0 and _num(b.get("level")) < _num(b.get("max_level"))]
    if upgr and fin["net_year"] > 0:
        A.append(Advice(3, "Bau", f"{len(upgr)} Gebäude können noch ausgebaut werden",
                        "z. B. " + ", ".join(f"{b.get('name')} {int(_num(b.get('level')))}/{int(_num(b.get('max_level')))} "
                                             f"({b.get('location')})" for b in upgr[:4])))
    unknown = sorted({f for b in bl for f in _list(b.get("unknown_fields"))})
    if unknown:
        A.append(Advice(3, "Lage", f"neue Slot-Felder: {', '.join(unknown)}",
                        "vermutlich Bau im Gang - für die Bauschlange auswerten"))
    # Forschung
    for u in _list(st.get("research")):
        if not u.get("technology"):
            A.append(Advice(1, "Forschung", f"{u.get('Name')} ({u.get('RegionKey')}) forscht nichts",
                            "Forschung zuweisen - ungenutzte Universitäten verschenken Fortschritt",
                            target_slot(u.get("RegionKey"), u.get("SlotKey"))))
    # Militaer
    for a in _list(st.get("armies")):
        units = int(_num(a.get("Units")))
        if 0 < units < FULL_STACK // 2:
            A.append(Advice(3, "Militär", f"Armee {a.get('Name')} in {a.get('Location')} hat nur {units} Einheiten",
                            "auffüllen oder mit einer anderen Armee vereinigen", target_pos(a)))
        sg = strength(a, unit_scale(st))
        if sg and sg["pct"] < 70:
            A.append(Advice(2 if sg["pct"] < 50 else 3, "Militär",
                            f"Armee {a.get('Name')} in {a.get('Location')} nur bei {sg['pct']:.0f} % Sollstärke "
                            f"({sg['hurt']} Einheiten unterbesetzt)",
                            "in eigener Region stehen lassen, damit sie sich auffüllt"
                            + (f" - Auffüllen kostet {_money(sg['cost'])}" if sg["cost"] else ""), target_pos(a)))
    for n in _list(st.get("navies")):
        sg = strength(n, unit_scale(st))
        if sg and sg["hurt"]:
            A.append(Advice(2 if sg["pct"] < 60 else 3, "Militär",
                            f"Flotte {n.get('Name')} bei {n.get('Location')}: {sg['hurt']} Schiffe beschädigt "
                            f"({sg['pct']:.0f} % Besatzung)",
                            "in einen eigenen Hafen zur Reparatur"
                            + (f" - kostet {_money(sg['cost'])}" if sg["cost"] else ""), target_pos(n)))
    own_by_place: dict[str, float] = {}
    for a in _list(st.get("armies")):
        own_by_place[str(a.get("Location", "")).lower()] = own_by_place.get(str(a.get("Location", "")).lower(), 0) + _num(a.get("Soldiers"))
    for e in enemy_armies(st, include_fog):
        if e["kind"] != "armies":
            continue
        loc = str(e.get("Location", ""))
        mine = own_by_place.get(loc.lower(), 0)
        if e["in_own_region"]:
            prio = 1 if _num(e.get("Soldiers")) > mine else 2
            A.append(Advice(prio, "Militär", f"Feindarmee {e.get('Name')} ({e.get('faction')}, {int(_num(e.get('Soldiers')))} "
                            f"Soldaten) steht in eigener Region {loc}" + ("" if e["visible"] else " [im Nebel]"),
                            f"eigene Truppen dort: {int(mine)} Soldaten", target_pos(e)))
        elif e["distance"] <= VIS_RADIUS and _num(e.get("Soldiers")) >= 500:
            A.append(Advice(2, "Militär", f"Feindarmee {e.get('Name')} ({e.get('faction')}, {int(_num(e.get('Soldiers')))} "
                            f"Soldaten) nahe eigener Truppen bei {loc}" + ("" if e["visible"] else " [im Nebel]"),
                            "", target_pos(e)))
    # Handel: blockierte/ueberfallene Waren, Piraterie je Gebiet/Partner
    tr = _dict(st.get("trade"))
    for kind, label in (("sources", "Handelsgebiet"), ("partners", "Handel mit")):
        for r in _list(tr.get(kind)):
            goods = _list(r.get("goods"))
            total = sum(_num(g.get("value")) for g in goods)
            lost = sum(_num(g.get("value_blockaded")) + _num(g.get("value_raided")) for g in goods)
            pil = _num(r.get("percent_pilfered"))
            if lost > 0:
                A.append(Advice(1 if lost >= 0.25 * max(total, 1) else 2, "Handel",
                                f"{label} {r.get('entry_name')}: {_money(lost)} von {_money(total)} blockiert oder überfallen",
                                "Flotte schicken, Blockade brechen, Handelsweg sichern"))
            elif pil > 0:
                A.append(Advice(2, "Handel", f"{label} {r.get('entry_name')}: {pil:.0f} % Piraterie",
                                "Kriegsschiffe zum Schutz der Handelsflotte"))
    # Missionen
    for e in _list(_dict(st.get("missions")).get("active")):
        name = next((str(e[k]) for k in ("Name", "Title", "name", "title", "Key", "key") if e.get(k)), "?")
        A.append(Advice(3, "Mission", f"Mission aktiv: {name}", "Belohnung bei Erfüllung - Frist beachten"))
    # Ereignisse
    ev = _dict(st.get("events"))
    for key, prio, text in (("RegionRebels", 1, "Aufstand in einer eigenen Region"),
                            ("RegionRiots", 1, "Unruhen in einer eigenen Region"),
                            ("CampaignSettlementAttacked", 1, "eigene Siedlung angegriffen"),
                            ("CampaignBuildingDamaged", 2, "eigene Gebäude beschädigt"),
                            ("LandTradeRouteRaided", 2, "eigener Handelsweg zu Land überfallen"),
                            ("SeaTradeRouteRaided", 2, "eigener Handelsweg zur See überfallen - Flotte zum Schutz"),
                            ("SufferAssassinationAttempt", 2, "Attentat auf eine eigene Person"),
                            ("MissionNearingExpiry", 3, "ein Auftrag läuft bald ab")):
        v = _dict(ev.get(key))
        if v.get("own"):
            A.append(Advice(prio, "Lage", f"{text} ({v['own']}x seit dem letzten Zug)"))
    # Diplomatie
    at_war = {x.get("key") for x in _list(_dict(st.get("diplomacy")).get("AtWar"))}
    for f in _list(st.get("factions")):
        if isinstance(f.get("AttitudeValue"), (int, float)) and f["AttitudeValue"] <= -50 and f.get("Key") not in at_war:
            A.append(Advice(3, "Diplomatie", f"{f.get('Name')} ist sehr feindselig ({int(f['AttitudeValue'])})",
                            "Grenzen sichern oder Beziehungen verbessern (Geschenk, Handelsabkommen)"))
    A.sort(key=lambda a: (a.prio, a.area))
    return A


def recommendations_text(st: dict, include_fog: bool = False) -> str:
    adv = recommendations(st, include_fog)
    m = _dict(st.get("meta"))
    head = f"HANDLUNGSEMPFEHLUNGEN - Zug {m.get('turn')} (feste Regeln, Stufe 1)" + (
        " - Nebel des Krieges einbezogen" if include_fog else "")
    return head + "\n" + ("\n".join(a.line() for a in adv) if adv else "keine Auffälligkeiten")
