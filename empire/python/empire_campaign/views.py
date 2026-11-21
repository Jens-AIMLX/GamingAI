"""Listen fuer den Kampagnen-Tab (Steuerpult und Overlay) statt eines langen Berichts: eine Kopfzeile und je Ansicht
Spalten + Zeilen mit festem Schluessel (die Liste wird an Ort und Stelle aktualisiert, Auswahl/Bildlauf bleiben).
Nebel des Krieges wie im Bericht: ohne include_fog nur sichtbare Feinde, verborgene nirgends."""
from __future__ import annotations

from dataclasses import dataclass, field

from .report import (FULL_STACK, PRIO_NAME, _dict, _list, _money, _num, enemy_armies, finances, order_totals,
                     recommendations, strength, target_pos, target_region, target_slot)

VIEWS = ["Empfehlungen", "Militär", "Regionen", "Bauplätze", "Forschung", "Feinde", "Diplomatie"]


@dataclass
class View:
    columns: list[tuple[str, int]]          # (Ueberschrift, relative Breite)
    rows: list[tuple[str, tuple]]           # (fester Schluessel, Zellen als Text)
    tags: dict[str, str]                    # Schluessel -> "urgent" / "warn" / "muted"
    targets: dict[str, str] = field(default_factory=dict)   # Schluessel -> Kamerasprung (Zeile in campaign_zoom.txt)


def header(st: dict) -> str:
    """Kopfzeile: Zug, Kasse, Saldo, Rang, Stand der Daten."""
    m, fa = _dict(st.get("meta")), _dict(st.get("faction"))
    fin = finances(st)
    reg = st.get("regions_written")
    stand = f"Stand {str(m.get('written', '?'))[11:]}"
    if reg and str(reg) != str(m.get("written")):
        stand += f", Regionen/Bauten {str(reg)[11:]}"
    probs = len(_list(st.get("encode_errors"))) + len(_list(st.get("errors")))
    warn = f" · DATENFEHLER in {probs} Teilen (ai_bridge/campaign_capture.error)" if probs else ""
    return (f"Zug {m.get('turn')} · {m.get('season')} {m.get('year')} · Kasse {_money(fin['treasury'])} · "
            f"Saldo/Jahr {_money(fin['net_year'])} · Rang M{fa.get('PowerRanking', '?')} "
            f"P{fa.get('PrestigeRanking', '?')} R{fa.get('WealthRanking', '?')} · {stand}{warn}")


def _n(v) -> str:
    return f"{int(_num(v)):,}".replace(",", ".") if v not in (None, "") else ""


def _recommendations(st: dict, fog: bool) -> View:
    rows, tags, targets = [], {}, {}
    for i, a in enumerate(recommendations(st, fog)):
        key = f"r:{a.area}:{a.text[:60]}"
        if any(k == key for k, _ in rows):
            key += f":{i}"
        rows.append((key, (PRIO_NAME[a.prio], a.area, a.text, a.reason)))
        tags[key] = {1: "urgent", 2: "", 3: "muted"}[a.prio]
        if a.target:
            targets[key] = a.target
    return View([("Priorität", 9), ("Bereich", 10), ("Maßnahme", 50), ("Grund", 31)], rows, tags, targets)


def _composition(sg: dict | None) -> str:
    """Zusammensetzung kurz: "8 Linieninfanterie, 2 Kavallerie, 2 Artillerie" (Klassennamen aus dem Spiel)."""
    if not sg:
        return ""
    return ", ".join(f"{n} {k}" for k, n in sg["classes"][:4]) + (" …" if len(sg["classes"]) > 4 else "")


def _military(st: dict) -> View:
    rows, tags, targets = [], {}, {}
    for kind, label in (("armies", "Armee"), ("navies", "Flotte"), ("agents", None)):
        for i, c in enumerate(sorted(_list(st.get(kind)), key=lambda c: -_num(c.get("Soldiers")))):
            art = label or str(c.get("AgentType") or "Agent")
            key = f"{kind}:{c.get('Name')}:{i}"
            ap = (f"{int(_num(c.get('ActionPoints')))}/{int(_num(c.get('ActionPointsPerTurn')))}"
                  if c.get("ActionPointsPerTurn") else "")
            sg = strength(c) if label else None
            stren = ""
            if sg:
                stren = f"{sg['pct']:.0f} %" + (f" ({sg['hurt']}×)" if sg["hurt"] else "")
            rows.append((key, (str(c.get("Name", "")), art, str(c.get("Location", "")),
                               _n(c.get("Soldiers")) if label else "", _n(c.get("Units")) if label else "", stren,
                               _composition(sg), ap)))
            if label == "Armee" and 0 < _num(c.get("Units")) < FULL_STACK // 2:
                tags[key] = "warn"
            if sg and sg["pct"] < 70:
                tags[key] = "urgent" if sg["pct"] < 50 else "warn"
            t = target_pos(c)
            if t:
                targets[key] = t
    return View([("Name", 18), ("Art", 9), ("Ort", 16), ("Soldaten", 9), ("Einheiten", 8), ("Stärke", 9),
                 ("Zusammensetzung", 30), ("Bewegung", 9)], rows, tags, targets)


def _worst(r: dict) -> float:
    vals = [x for x in order_totals(r) if x is not None]
    return min(vals) if vals else 99


def _regions(st: dict) -> View:
    rows, tags, targets = [], {}, {}
    for r in sorted(_list(st.get("regions")), key=_worst):
        up, lo = order_totals(r)
        key = f"reg:{r.get('Name')}"
        if r.get("Taxed") is False:
            tax = "steuerfrei"
        elif r.get("UpperTaxPercentage") is not None:
            tax = f"{_num(r.get('UpperTaxPercentage')) * 100:.0f}/{_num(r.get('LowerTaxPercentage')) * 100:.0f} %"
        else:
            tax = ""
        wealth = _n(r.get("Wealth")) + (f" ({_num(r.get('WealthChange')):+.0f})" if r.get("WealthChange") is not None else "")
        rows.append((key, (str(r.get("Name", "")), str(r.get("Settlement", "")), str(r.get("Population", "")),
                           f"{'?' if up is None else int(up)} / {'?' if lo is None else int(lo)}", tax, wealth)))
        w = _worst(r)
        if w < 0:
            tags[key] = "urgent" if w <= -3 else "warn"
        if r.get("Name"):
            targets[key] = target_region(r.get("Name"))
    return View([("Region", 22), ("Siedlung", 18), ("Bevölkerung", 14), ("Ordnung O/U", 14), ("Steuern O/U", 14),
                 ("Wohlstand", 18)], rows, tags, targets)


def _bclass(b: dict) -> int:
    if isinstance(b.get("health"), (int, float)) and b["health"] < 100:
        return 0                                    # beschaedigt
    if b.get("building_key") == "empty":
        return 1                                    # frei
    if _num(b.get("level"), -1) >= 0 and _num(b.get("level")) < _num(b.get("max_level")):
        return 2                                    # ausbaufaehig
    return 3


def _buildings(st: dict) -> View:
    rows, tags, targets = [], {}, {}
    for i, b in enumerate(sorted(_list(st.get("buildings")),
                                 key=lambda b: (_bclass(b), str(b.get("region")), str(b.get("location"))))):
        cls = _bclass(b)
        key = f"slot:{b.get('slot_key') or i}"
        if cls == 1:
            name = "frei" + (f" ({b.get('slot_type')})" if b.get("slot_type") else "")
            level = ""
        else:
            name = str(b.get("name", ""))
            level = f"{int(_num(b.get('level')))}/{int(_num(b.get('max_level')))}"
        health = f"{int(b['health'])} %" if isinstance(b.get("health"), (int, float)) else ""
        rows.append((key, (str(b.get("region", "")), str(b.get("location", "")), name, level, health)))
        if cls == 0:
            tags[key] = "urgent" if b["health"] < 50 else "warn"
        elif cls == 3:
            tags[key] = "muted"
        t = target_slot(b.get("region_key"), b.get("slot_key"))
        if t:
            targets[key] = t
    return View([("Region", 22), ("Ort", 20), ("Gebäude", 32), ("Stufe", 10), ("Zustand", 10)], rows, tags, targets)


def _research(st: dict) -> View:
    rows, tags, targets = [], {}, {}
    for i, u in enumerate(_list(st.get("research"))):
        key = f"uni:{u.get('RegionKey')}:{i}"
        rows.append((key, (str(u.get("Name", "")), str(u.get("RegionKey", "")), str(u.get("technology") or "- nichts -"),
                           str(u.get("turns_left", "")) if u.get("technology") else "")))
        if not u.get("technology"):
            tags[key] = "urgent"
        t = target_slot(u.get("RegionKey"), u.get("SlotKey"))
        if t:
            targets[key] = t
    return View([("Universität", 26), ("Region", 22), ("Technologie", 38), ("Züge", 10)], rows, tags, targets)


def _enemies(st: dict, fog: bool) -> View:
    rows, tags, targets = [], {}, {}
    for i, e in enumerate(sorted(enemy_armies(st, fog), key=lambda e: (not e["in_own_region"], e["distance"]))):
        key = f"en:{e.get('faction')}:{e.get('Name')}:{i}"
        where = "in eigener Region" if e["in_own_region"] else ("nahe eigener Truppen" if e["visible"] else "im Nebel")
        rows.append((key, (str(e.get("faction", "")), str(e.get("Name", "")), str(e.get("Location", "")),
                           _n(e.get("Soldiers")), _n(e.get("Units")), where)))
        tags[key] = "urgent" if e["in_own_region"] else ("muted" if not e["visible"] else "warn")
        t = target_pos(e)
        if t:
            targets[key] = t
    return View([("Fraktion", 14), ("Name", 22), ("Ort", 20), ("Soldaten", 12), ("Einheiten", 10), ("Lage", 20)],
                rows, tags, targets)


def _diplomacy(st: dict) -> View:
    rows, tags = [], {}
    d = _dict(st.get("diplomacy"))
    status: dict[str, str] = {}
    for cat, label in (("Protectorates", "Protektorat"), ("ProtectorOf", "Schutzmacht"), ("TradeRights", "Handel"),
                       ("Allies", "Verbündet"), ("AtWar", "KRIEG")):
        for x in _list(d.get(cat)):
            status[str(x.get("key"))] = label
    for f in sorted(_list(st.get("factions")), key=lambda f: _num(f.get("AttitudeValue"))):
        key = f"fac:{f.get('Key')}"
        s = status.get(str(f.get("Key")), "")
        rows.append((key, (str(f.get("Name", "")), s, str(f.get("Attitude", "")), _n(f.get("AttitudeValue")),
                           str(f.get("Government", "")))))
        if s == "KRIEG":
            tags[key] = "urgent"
        elif _num(f.get("AttitudeValue")) <= -50:
            tags[key] = "warn"
    return View([("Fraktion", 26), ("Status", 14), ("Haltung", 20), ("Wert", 10), ("Regierung", 30)], rows, tags)


def build(st: dict, include_fog: bool = False) -> dict[str, View]:
    return {"Empfehlungen": _recommendations(st, include_fog), "Militär": _military(st), "Regionen": _regions(st),
            "Bauplätze": _buildings(st), "Forschung": _research(st), "Feinde": _enemies(st, include_fog),
            "Diplomatie": _diplomacy(st)}
