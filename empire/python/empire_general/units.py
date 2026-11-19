"""Einheiten: Rolle aus dem Schluessel der Spieldatenbank (unit:type()) ableiten.

Die Bruecke liefert nur den Schluessel (z. B. "euro_line_infantry_colonial", "12_lber_land_cannon"),
Mannstaerke und Fernkampf-Reichweite. Die Rolle wird per Stichwort bestimmt; unbekannte Schluessel
fallen ueber die Reichweite in eine sinnvolle Klasse.
"""
from __future__ import annotations

ROLES = {
    "general": "general's bodyguard (cavalry); losing him hurts the whole army's morale",
    "line_infantry": "line infantry with muskets: fires volleys (~70-90 m), strong in close line; can melee with bayonets",
    "light_infantry": "light infantry / skirmishers: longer range, accurate, fragile in melee, can skirmish",
    "grenadiers": "grenadiers: elite line infantry, can throw grenades at close range",
    "melee_infantry": "melee infantry (pikes, swords, warriors): no or weak firearms, must close in",
    "heavy_cavalry": "heavy cavalry (cuirassiers, heavy dragoons): devastating charge into flank/rear, keep out of musket volleys",
    "light_cavalry": "light cavalry (hussars, lancers, camels): fast, chase skirmishers and broken units, screen flanks",
    "missile_cavalry": "missile cavalry (dragoons, horse archers, carabiniers): shoot from horseback, avoid melee",
    "artillery": "field artillery: long range (400-1000 m), deadly against massed infantry, helpless in melee, slow to move",
    "infantry": "infantry (unknown type)",
    "unit": "unknown unit",
}

_RULES: list[tuple[tuple[str, ...], str]] = [
    (("general", "bodyguard"), "general"),
    (("cannon", "howitzer", "mortar", "lber", "artillery", "rocket", "organ", "swivel", "bombard"), "artillery"),
    (("horse_archer", "carabin", "dragoon", "mounted_musket", "mounted_rifle", "horse_rifle"), "missile_cavalry"),
    (("cuirass", "heavy_dragoon", "gendarme", "garde_du_corps", "horse_guard", "life_guard", "heavy_cav", "boyar"), "heavy_cavalry"),
    (("hussar", "lancer", "uhlan", "cossack", "camel", "light_cav", "light_horse", "chasseur_a_cheval", "sowar", "cavalry", "horse", "mamluk", "sipahi", "spahi"), "light_cavalry"),
    (("grenadier",), "grenadiers"),
    (("light_infantry", "jaeger", "jager", "ranger", "rifle", "skirmish", "chasseur", "voltigeur", "light_inf", "marksm", "fusilier_light"), "light_infantry"),
    (("pike", "sword", "warrior", "spear", "highland", "axe", "club", "dervish", "tribal", "berserk"), "melee_infantry"),
    (("line_infantry", "musket", "fusilier", "infantry", "guard", "militia", "sepoy", "regiment", "marine", "janissar"), "line_infantry"),
]


def role_of(type_key: str, missile_range: float | None = None) -> str:
    k = (type_key or "").lower()
    for words, role in _RULES:
        if any(w in k for w in words):
            # Kavallerie mit Schusswaffe als Fernkampf-Kavallerie fuehren
            if role in ("light_cavalry",) and (missile_range or 0) >= 40:
                return "missile_cavalry"
            return role
    if (missile_range or 0) >= 300:
        return "artillery"
    if (missile_range or 0) > 0:
        return "line_infantry"
    return "unit"


def is_cavalry(role: str) -> bool:
    return role in ("general", "heavy_cavalry", "light_cavalry", "missile_cavalry")


def is_ranged(role: str, missile_range: float | None) -> bool:
    return (missile_range or 0) > 0 and role != "melee_infantry"


def frontage_m(role: str, men: int) -> float:
    """Grobe Frontbreite einer Einheit in Formation (Meter) fuer Linienplanung."""
    men = max(int(men or 0), 1)
    if role == "artillery":
        return 12.0
    if is_cavalry(role):
        return max(10.0, men / 2 * 1.6)   # 2 Glieder, ~1,6 m pro Reiter
    if role == "light_infantry":
        return max(10.0, men / 2 * 1.2)   # aufgelockert
    return max(10.0, men / 3 * 0.8)       # 3 Glieder Linieninfanterie
