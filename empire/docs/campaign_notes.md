# Kampagnenkarte – Erkenntnisse

Stand 2026-11-20. Quellen: Kampagnen-Probe im Spiel (`lua/campaign_addon.lua`, Ausgabe `ai_bridge/campaign_probe.txt`),
`EpisodicScripting.lua` aus `patch5.pack`, UI-Skripte (`ui\*.luac`) aus den Packs, ausgewertet mit
`tools/ui_signatures.py` (Lua-5.1-Bytecode-Leser `tools/luadis.py`) → `docs/campaign_ui_signatures.txt`.

## Was das Kampagnen-Lua kann

- Lua 5.1 mit `io`, `os`, `package.loadlib`, `debug` – Dateizugriff und Laden von `empire_bridge.dll` sind möglich
  (wie in Schlachten). **`math.huge` fehlt** (wie im Battle-Lua) – jeder Zugriff darauf bricht ab; Tests setzen
  `math.huge = nil`.
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

## Lese-Probe 2 – Ergebnisse (Preußen, Zug 172, Sommer 1786)

Aufgerufen zu Beginn des eigenen Zugs (`FactionTurnStart`, `conditions.FactionIsLocal`), nur auf Wunsch
(`ai_bridge/campaign_probe2.on`), mit Absturzprotokoll. **Regel: nur in der Form aufrufen, in der die UI-Skripte es
tun** – `TheatreList()` ohne Argument liess die Engine abstuerzen (2026-11-20 21:18); das Spiel ruft `TheatreList(true)`.
Mit den richtigen Argumenten liefen alle Aufrufe (21:23).

| Aufruf | Rückgabe |
|---|---|
| `PlayerFactionId()`, `PlayersFactionKey()` | Fraktionsschlüssel als Text (`"prussia"`) |
| `CurrentTurn()`, `CurrentYear()`, `CurrentSeasonString()`, `IsPlayersTurn()` | 172, 1786, "Sommer", true |
| `RetrieveFactionMilitaryForceLists(pid, true/false)` | Armeen bzw. Flotten (88): `Name`, `Location` (Ort), `PosX/PosY`, `Soldiers`, `ActionPoints(PerTurn)`, `AgentType`, `Abilities`, `Attributes` (Werte), `Address` (Pointer<CHARACTER>), `CommandedUnit` |
| `RetrieveFactionRegionList(pid)` | 103 Einträge mit `SettlementAddress` (Pointer<SETTLEMENT>) |
| `SettlementsRegion(SettlementAddress)` | Pointer<REGION> |
| `RetrieveFactionAgentsList(pid)` | Agenten (wie Armeen, ohne Truppen) |
| `FactionDetails(pid)` | `PowerRanking`, `PrestigeRanking`, `WealthRanking`, `VictoryConditions` (Regionen), `Leader` |
| `RetrieveDiplomacyDetails(pid)` | `Allies`, `AtWar`, `Protectorates`, `ProtectorOf`, `TradeRights` (Fraktionsschlüssel) |
| `RetrieveFactionListForDiplomacy()` | je Fraktion `Attitude(Value)`, `Relationship`, `Government`, `ReligionName`, `IsMajor`, `Trading` |
| `GovernorshipList(pid)` | Statthalterschaften (`europe`, `america` …) |
| `RetrieveGovernorshipDetails(Key)` | `AnnualIncome`, `ArmyUpkeep`, `NavyUpkeep`, `TaxIncomeTotal`, `Trade`, `Policing`, `Upper/LowerTaxRate`, `Automanage*`, `LowestPublicOrder`, `Governor` |
| `TheatreList(true)`, `HomeTheatre(pid)` | Kriegsschauplätze Amerika/Europa/Indien, Heimat = 2 (Europa) |
| `TradeInfo()`, `PrestigeDetails()`, `MonarchyInfo()` | Waren/Preise/Handelspartner, Prestige, Monarch/Erben |
| `RetrieveVisibleEnitityDetails()` | nur Siedlungen am Bildschirm (Labels) – **keine feindlichen Armeen** |

### Probe 3 (2026-11-20 21:29 / 21:42) – alle Aufrufe ohne Absturz

| Aufruf | Felder |
|---|---|
| `InitialiseRegionInfoDetails(SettlementsRegion(SettlementAddress))` | `Name`, `OwningFactionKey`, `Population(Number)`, `PopulationChange/Growth`, `UpperOrder`/`LowerOrder` (öffentliche Ordnung je Klasse, Liste von Faktoren mit `Total`, `Positive`, `Tooltip`), `UpperTax`/`LowerTax(Percentage)`, `Taxed`, `Wealth`, `WealthChange`, `RegionWealth`, `TownWealth`, `ReligionKey`, `ReligiousBreakdown`, `Governor`, `Settlement`, `Theatre`, `AdministrationCostPercentage` |
| `InitialiseGovernmentDetails()` | `Treasury` (Staatskasse), `AnnualIncome`, `ArmyUpkeep`, `NavyUpkeep`, `TaxIncomeTotal`, `Trade`, `Policing`, `OtherIncome`, `Government`, `DatabaseKey`, `Home`, `Ministers`, `MinisterPool`, `Popularity(Status)`, `Population`, `Prestige`, `Prosperity`, `Religion`, `NextElections`, `Governorships`, `Theatres` |
| Armee-Eintrag (eigene wie fremde) | zusätzlich `Units` (Anzahl Einheiten), `Soldiers`, `Title`, `Traits`, `Transitioning` |
| `RetrieveFactionMilitaryForceLists(<feind>, true)` | funktioniert mit dem Schlüssel jeder Fraktion (26 Armeen der Maratha) – **liefert auch Armeen im Nebel des Krieges** (Maratha-Armee in Guatemala war für den Spieler zu Zugbeginn nicht sichtbar) |

`RegionsPublicOrders` braucht `details.Region.Region` – das Feld gibt es nicht; die Ordnung steht direkt in
`UpperOrder`/`LowerOrder`. Die Zusammensetzung einer Armee (welche Einheiten) liefert die Liste nicht, nur die
Anzahl; dafür gäbe es `RetrieveContainedEntitiesFromCharacter(character, target)` (Austauschfenster), ungeprüft.

### Probe 4/5 (2026-11-20 21:58–22:10)

| Aufruf | Ergebnis |
|---|---|
| `ReviewPanelInfo()` (Rekrutierungsfenster) | **Absturz** – braucht wohl das offene Fenster; Rekrutierungsschlange bisher ohne Quelle |
| `FactionsUniversities(key)` | Universitäten: `Name`, `RegionKey`, `SlotKey`, `Level`, `Health`, `Address` |
| `UniversityResearching(.Address)` | `Technology` (`Name`, `Key`, …), `TurnsToComplete` – leer, wenn nichts erforscht wird |
| `BuildingBrowserDetails(nil)` / `(region)` | `region`, `region_name`, `slots` je Slot: `building_key` (`empty` = frei), `name`, `level`, `max_level`, `location`, `slot_type`, `type`, `region_key`, `slot_key`, `faction_key` |
| `NextAutoSettlementSelection(region)` | nächste eigene Siedlung; Umlauf über alle: 103 Regionen, 952 Slots (70 frei) in 1,5 s, endet am Anfang |
| `BuildingDetails(building, region, faction, slot)` | `Health` (Zustand 0–100) brauchbar; viele andere Felder außerhalb des Fensters Datenmüll |

Bauschlange: kein Feld in den Slots gefunden (es war nichts im Bau?) – die Erfassung meldet unbekannte Slot-Felder.

## Stufe 1: Datenerfassung und Bericht (umgesetzt)

- `lua/campaign_capture.lua` (von `install.py` mit dem Addon eingehängt): zu Beginn jedes eigenen Zugs →
  `ai_bridge/campaign_state.json`. Abschalter `ai_bridge/campaign_capture.off`; Absturzschutz je Schritt
  (`campaign_capture.progress`/`.skip`). Ereignisse der Gegnerzüge (Schäden, Überfälle, Unruhen, Schlachten, Bau-/
  Forschungsabschlüsse …) werden mitgezählt, eigene getrennt (`FactionIsLocal`, `CharacterIsLocalCampaign`,
  `SettlementIsLocal`, `RegionIsLocal`).
- `python/empire_campaign/report.py`: Bestandsbericht und Handlungsempfehlungen nach festen Regeln (Finanzen,
  Ordnung mit Ursachen, Reparaturen, freie Bauplätze, Ausbau, leere Universitäten, Bedrohungen, Diplomatie,
  Ereignisse).
- **Nebel des Krieges:** erfasst werden alle Feindarmeen; Bericht und Regeln nennen/verwenden ohne den Spezialschalter
  nur sichtbare (genähert: in eigener Region/Ortschaft oder näher als `VIS_RADIUS` an eigenen Armeen/Flotten/Agenten),
  verborgene auch nicht als Anzahl. Schalter im Steuerpult, Tab „Kampagne“ (`campaign_fog` in den Einstellungen).

## Nächste Schritte

1. Spieltest der Erfassung; Laufzeit je Schritt prüfen (`steps` im JSON).
2. Stufe 2: Claude-Berater auf Basis des Lageberichts; Overlay-Tab „Kampagne“ (DLL im Kampagnen-Lua laden).
3. Erste Handlungen (Forschung, Bau, Rekrutierung, Steuern), einzeln und abschaltbar.

- **Timer nie beim Laden stellen:** `add_time_trigger` in WorldCreated/NewSession/UICreated liess Empire 2 s nach Ladebeginn abstuerzen (c0000005, Empire.exe+0x1D54A9). Die Originalskripte stellen Timer nur ab Zugbeginn oder bei Eingaben - der Takt der Erfassung ebenso (erster Klick nach dem Laden startet ihn).
