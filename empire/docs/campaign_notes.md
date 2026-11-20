# Kampagnenkarte – Erkenntnisse

Stand 2026-11-20. Quellen: Kampagnen-Probe im Spiel (`lua/campaign_addon.lua`, Ausgabe `ai_bridge/campaign_probe.txt`),
`EpisodicScripting.lua` aus `patch5.pack`, UI-Skripte (`ui\*.luac`) aus den Packs, ausgewertet mit
`tools/ui_signatures.py` (Lua-5.1-Bytecode-Leser `tools/luadis.py`) → `docs/campaign_ui_signatures.txt`.

## Was das Kampagnen-Lua kann

- Lua 5.1 mit `io`, `os`, `package.loadlib`, `debug` – Dateizugriff und Laden von `empire_bridge.dll` sind möglich
  (wie in Schlachten).
- `data/campaigns/<kampagne>/scripting.lua` liegt lose im Spielordner und wird geladen; `install.py` hängt dort die
  Probe an (Sicherung `scripting.lua.orig`).
- `EpisodicScripting.AddEventCallBack(event, func)` **hängt an** – zusätzliche Handler ersetzen die des Spiels nicht.
  `game_interface` entsteht bei `NewSession` (`GAME(context)`).
- 147 Ereignisse (`data.events`), u. a. `FactionTurnStart`, `CharacterTurnStart`, `RegionTurnStart`, `PreBattle`,
  `SettlementOccupied`, `CharacterCompletedBattle`, `ResearchCompleted`, `UnitTrained`, `BuildingCompleted`.
- 279 Abfragen (`conditions.*(..., context)`): Charakter, Armee, Region, Fraktion (z. B. `FactionTreasury`,
  `FactionIsLocal`, `CharacterInRegion`, `EnemyArmyGreaterCombatStrength`, `RegionTaxLevel`, `MapPosition`).
  Die Kontext-Objekte der Ereignisse sind geschlossen (Metatabelle mit `__index`-Funktion) – lesbar nur über diese
  Abfragen.
- `game_interface` (60 Funktionen): Missionen, Diplomatie erzwingen, Bewegung sperren, Kamera, Sichtbarkeit,
  Auslöser, `save_value/load_value`. **Keine** allgemeinen Befehle (bewegen, rekrutieren, bauen).
- `CampaignUI` (299 Funktionen, globale Tabelle): dieselbe Schnittstelle, die die Bedienoberfläche des Spiels nutzt –
  lesen **und** handeln.

## CampaignUI – Signaturen (aus den UI-Skripten)

Lesen:

| Funktion | Aufruf im Spiel |
|---|---|
| Armeen / Flotten | `RetrieveFactionMilitaryForceLists(PlayerFactionId(), true/false)` – Einträge: `item:LuaCall('Character')` |
| Regionen / Agenten | `RetrieveFactionRegionList(PlayerFactionId())`, `RetrieveFactionAgentsList(PlayerFactionId())` |
| Diplomatie | `RetrieveDiplomacyDetails(faction)`, `FactionDetails(faction)` |
| Sichtbare Einheiten | `RetrieveVisibleEnitityDetails()` |
| Handel, Prestige | `TradeInfo()`, `PrestigeDetails()` |
| Forschung | `TechnologyPlayerDetails(residence)`, `TechnologyResearchingDetails(character, residence)` |

Handeln:

| Funktion | Aufruf im Spiel |
|---|---|
| Rekrutieren | `RecruitUnit(character, manager, record)` |
| Bauen | `BeginConstruction(building_key, slot_key)` |
| Forschen | `BeginResearch(university.Address, key)` |
| Steuern | `SetGovernorshipTaxRate(governorship, bool, rate)`, `SetRegionTaxed(region, bool)`, `SetAutomanageTaxes(governorship, on)` |
| Armeen | `MergeUnits({...})`, `SplitFromForce({}, {}, {})`, `CancelOrderForSelectedCharacter()` |
| Vor der Schlacht | `PreBattleAttack(attacker, residence)`, `PreBattleAutoResolve(...)`, `PreBattleRetreat(character)` |
| Agenten | `MoveIntoTarget(src, target[, bool])` |
| Zug beenden | `EndTurn()` |

**Bewegen von Armeen auf der Karte gibt es nicht als Skriptfunktion** – das Spiel bewegt per Rechtsklick in der
Engine. Optionen: Empfehlung (Spieler zieht) oder später Maussteuerung.

## Nächste Schritte

1. Lese-Probe 2 (im Addon): beim ersten eigenen Zug Rückgaben der Lesefunktionen nach
   `ai_bridge/campaign_state_probe.txt` – zeigt die Datenstrukturen.
2. Strategischer Berater: je Zug Lagebericht an Claude, Empfehlungen im Overlay (DLL im Kampagnen-Lua laden).
3. Erste Handlungen (Forschung, Bau, Rekrutierung, Steuern), einzeln und abschaltbar.
