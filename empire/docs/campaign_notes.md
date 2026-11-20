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

## Nächste Schritte

1. Strategischer Berater: je eigenem Zug Lagebericht aus diesen Daten an Claude, Empfehlungen im Overlay und
   Steuerpult (DLL im Kampagnen-Lua laden oder Datei-Brücke wie in Schlachten).
2. Fehlende Daten (Feinde, Regionen) mit weiteren Aufrufen in Spiel-Signatur ergänzen.
3. Erste Handlungen (Forschung, Bau, Rekrutierung, Steuern), einzeln und abschaltbar.
