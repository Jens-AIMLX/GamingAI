# Empire Battle AI – Sprachmodell als Feldherr in Total War: EMPIRE

Experiment: Ein Sprachmodell (Claude) führt eine Armee in Empire-Schlachten. Architektur nach
[rome-battle-ai](https://github.com/Cegli865/rome-battle-ai) (MIT, © 2026 Cegli865), an Empire angepasst.

```
Empire.exe ── empire_bridge.dll (Patch: jede Schlacht bekommt ai_bridge.battle_script)
   │
   └─ data/ai_bridge.battle_script (Lua, offizielle Battle-API: Controller, Befehle, Timer)
          │  ai_bridge/state.json  (Lagebild, jede Sekunde)
          │  ai_bridge/orders.lua  (Befehle)
   python/commander.py
     ├─ Executor (jede Sekunde, ohne Modell): Formationen, Wegpunkte, Reflexe
     └─ Feldherr (alle 5–10 s): Lagebericht → Claude → Plan (festes Schema)
```

## Aufbau

| Ordner | Inhalt |
|---|---|
| `dll/` | `empire_bridge.c` (C, 32-Bit) + `build.bat` (MSVC) – patcht Empire.exe an einer Stelle (`0x57b8de`) |
| `lua/` | `ai_bridge.battle_script` (Bridge im Spiel), `battle_scripted_addon.lua` (lädt die DLL) |
| `python/` | Feldherr (`commander.py`, `empire_general/`), Brücke (`empire_bridge.py`), Steuerpult (`ui.py`), Tests |
| `python/strategies/` | Strategie-Vorlagen für den System-Prompt |
| `docs/` | `PROTOCOL.md` (Datei-Protokoll), `RE_notes.md` (Reverse-Engineering-Befunde, Adressen) |
| `tools/` | Analyse-Werkzeuge (Disassembler-Hilfen, Pack-Leser) und die Prüfskripte der Machbarkeitstests |
| `install.py` | baut bei Bedarf die DLL und installiert alles in den Spielordner (oder entfernt es) |

## Voraussetzungen

- Windows, Total War: EMPIRE – Definitive Edition, **Steam-Build 11084729**
  (Empire.exe SHA-256 `9b130e708d01a60010df286d2e39867b8fc4f0814d544fcbfe3c1e352df04120`).
  Bei einer anderen Version schaltet sich die DLL ab (Byte-Prüfung), das Spiel läuft normal.
- **Visual Studio 2019/2022 oder die Build Tools** mit „Desktopentwicklung mit C++“ (für die DLL).
- **Python 3.11+**.
- Für Claude: ein API-Schlüssel (im Steuerpult eintragen) oder `ant auth login`.

## Bauen und installieren

```bat
cd empire
python -m pip install -r requirements.txt
dll\build.bat
python install.py
```

`install.py` findet den Spielordner über Steam (sonst `--game-dir "...\Empire Total War"`), kopiert DLL und
Battle-Skript, sichert `data\battle_scripted.lua` als `.orig`, hängt den Lade-Block an, legt
`ai_bridge\config.lua` an und merkt sich den Spielordner in `python\local_settings.json` (nicht eingecheckt).
Nach jeder Änderung an `dll/` oder `lua/` einfach erneut `python install.py` ausführen
(Empire dabei beenden, die DLL ist während des Spiels gesperrt).

Entfernen: `python install.py --uninstall`. Eine Dateiprüfung über Steam stellt das Original ebenfalls her.

## Steuerpult

Doppelklick auf `start_ui.bat` (oder `python python\ui.py`):

- **API-Schlüssel** eintragen → *Speichern* (Windows-Anmeldeinformationsverwaltung, nie in einer Datei) →
  *Prüfen* (kostenlose Abfrage der Modell-Liste).
- **KI führt:** Gegner / meine Armee / beide / aus – gilt ab der nächsten Schlacht.
- **Feldherr:** Claude Opus 5.5, Sonnet 5.5, Haiku 5.5 oder Regel-Feldherr; Denktiefe; Planungstakt.
- **Strategie:** Vorlage mit allgemeinen Ratschlägen für beide Seiten wählen, bearbeiten, speichern oder als neue
  Vorlage anlegen.
- **Befehle – Meine Armee / Gegner:** verbindliche Taktik je Seite. Sie steht im System-Prompt vor den Ratschlägen
  und gilt auch dann, wenn das Modell sich überlegen fühlt; das Modell erfährt außerdem, welche Seite es führt
  (Spielerarmee oder Gegner).
- Strategie, Befehle und Aufstellungsprompt werden **vor jedem Plan neu gelesen** – Änderungen wirken während der
  laufenden Schlacht ab dem nächsten Plan.
- **Aufstellung:** freier Prompt, wie aufgestellt werden soll (wird bei jedem Auslösen neu gelesen).
- **▶ Starten** startet den Feldherrn, das Protokoll läuft live mit.
- Alle Einstellungen, Prompts und ungespeicherte Strategie-Entwürfe werden laufend in `python\settings.json`
  gesichert (lokal, nicht im Repo) und beim nächsten Start wiederhergestellt; der Schlüssel bleibt in der
  Windows-Anmeldeinformationsverwaltung.

In der Aufstellung **eine eigene Einheit anklicken** – so erkennt die Brücke deine Seite (sonst gilt Allianz 1
als deine). Ab Schlachtbeginn übernimmt der Feldherr.

### Hotkeys (solange der Feldherr läuft, auch wenn Empire im Vordergrund ist)

| Hotkey | Button | Wirkung |
|---|---|---|
| **F9** | Aufstellung übernehmen | Der Feldherr stellt die gesteuerten Seiten nach dem Aufstellungsprompt auf. Er übernimmt die Einheiten schon in der Aufstellungsphase; ob das Spiel dort Bewegungen zulässt, meldet das Protokoll – sonst marschieren sie direkt nach Schlachtbeginn an ihre Plätze. |
| **F10** | Markierte → KI | Die im Spiel markierten Einheiten werden übernommen und von der KI eingeplant. |
| **F11** | Markierte → Spieler | Die markierten Einheiten werden freigegeben; die KI plant sie nicht mehr ein, auch nicht bei Schlachtbeginn, bis sie mit F10 zurückgegeben werden. |

Die Tasten sind im Steuerpult einstellbar (Feld neben jedem Button, z. B. `F9`, `Pause`, `Num5`, `Strg+Shift+F9`).
**Achtung:** Empire liest die Tastatur selbst (DirectInput) und sieht jede Taste ebenfalls – Kombinationen wie
Strg+Alt+A kommen im Spiel als Strg+A („alle auswählen“) an. Deshalb Tasten wählen, die Empire nicht belegt.
Die Buttons schreiben in `<Spiel>\ai_bridge\commands.txt`; der Feldherr liest die Datei laufend.

### Einheiten in Gruppen des Spielers

Einheiten, die im Spiel gruppiert sind, kann die Brücke nicht einzeln übernehmen (das Spiel meldet „unit is
already in another group“). Sie werden im Protokoll gemeldet und automatisch übernommen, sobald die Gruppe im Spiel
aufgelöst ist (Prüfung alle 10 s). Mit dem Schalter **„Eigenes Gruppenmanagement“** versucht die Brücke stattdessen,
die Einheit samt ihrer Gruppe über `add_group` zu übernehmen; ob das im Spiel funktioniert, steht im Brücken-Log
(`own_groups: add_group fuer …: OK/FEHLER`).

## Ohne Steuerpult

```bat
cd empire\python
python commander.py                                  # Claude Opus 5.5, effort low
python commander.py --model claude-sonnet-5-5        # schneller/günstiger
python commander.py --backend rule                   # ohne Sprachmodell
python commander.py --strategy strategies\defensiv.md --orders-player meine_armee.txt --orders-enemy gegner.txt
```

### Was der Feldherr nicht befehlen kann

Die Script-API des Spiels hat keine Befehle für die **Munitionsart** der Artillerie (Kartätschen, Spreng-/Vollkugel)
und den **Formationstyp** der Infanterie (Karree, Kolonne) – das wählt das Einheitenverhalten des Spiels; das Modell
weiß das. Automatisch: Artillerie stellt das Feuer ein, solange eigene Truppen näher als 50 m am Ziel sind.

Welche Seite geführt wird, steht in `<Spiel>\ai_bridge\config.lua`. Jede Schlacht wird in
`python\logs\battle_<Zeit>.jsonl` protokolliert (Lageberichte, Pläne, Fehler, Token-Verbrauch).

## Tests

| Befehl (im Ordner `python`) | prüft |
|---|---|
| `python sim_test.py` | Brücke offline (echtes Lua 5.1 gegen nachgebaute API) |
| `python sim_commander.py` | ganze Kette offline mit Regel-Feldherr |
| `python sim_deploy.py` | Aufstellung per Befehl und Übergabe markierter Einheiten (KI ↔ Spieler) offline |
| `python selftest.py` | Brücke im echten Spiel, PASS/FAIL-Marker |

## Bekannte Grenzen

- Die API liefert keine Moral, Ermüdung, Munition, Formation; Ausrichtung beim Marsch = Marschrichtung
  (`goto_location_angle_width` ist noch ungetestet).
- Spielerbefehle werden protokolliert, aber auch Befehle der Brücke landen dort.
- Der System-Prompt ist kürzer als die Mindestgröße für Prompt-Caching – er wird voll berechnet.
- Rollen der Einheiten werden aus dem Datenbank-Schlüssel geraten (`empire_general/units.py`).
- Im Spiel-Lua fehlen Teile der Standardbibliothek (z. B. `math.huge`); das Battle-Skript nutzt `math` gar nicht.
