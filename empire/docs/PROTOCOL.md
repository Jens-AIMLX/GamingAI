# Empire AI Bridge – Protokoll

Datei-Brücke zwischen dem Battle-Skript im Spiel (`data/ai_bridge.battle_script`) und einem externen
Feldherrn (Python, später Sprachmodell). Alle Dateien liegen in `<Spielordner>\ai_bridge\`.

| Datei | Schreiber | Leser | Inhalt |
|---|---|---|---|
| `config.lua` | Mensch / Python | Spiel (bei Schlachtbeginn) | Lua-Tabelle: welche Seite gesteuert wird, Takt |
| `state.json` | Spiel (jeder Takt) | Python | Lagebild als JSON |
| `orders.lua` | Python | Spiel (jeder Takt) | Befehle als Lua-Tabellenliteral (reine Daten) |
| `bridge_log.txt` | Spiel | Mensch | Ereignisse und Fehler |

Lua 5.1 im Spiel hat keinen JSON-Parser, deshalb schreibt Python Befehle als Lua-Literal. Das Spiel lädt
die Datei mit `loadstring` in einer leeren Umgebung (`setfenv(f, {})`): nur Daten, keine Funktionen.

## config.lua

```lua
return {
  control = "enemy",          -- "enemy" | "player" | "both" | "none"
  player_alliance = "auto",   -- "auto" (Klick auf eigene Einheit in der Aufstellung, sonst 1) oder Zahl
  tick_ms = 1000,             -- Takt für state.json und Befehlsabfrage (Spielzeit)
}
```

## state.json

```json
{
  "seq": 42, "game_time": 612.3, "phase": "Deployed", "battle_start": 40.2,
  "player_alliance": 1, "player_alliance_source": "auswahl",
  "control": "enemy", "controlled_alliances": [2],
  "last_order_seq": 7,
  "order_results": [{"seq": 7, "i": 1, "unit": "2.1.3", "cmd": "goto_location", "ok": true, "err": null}],
  "player_commands": [{"t": 600.1, "name": "...", "string1": "..."}],
  "alliances": [
    {"index": 1, "is_player": true, "controlled": false, "units": [
      {"id": "1.1.1", "type": "euro_line_infantry", "men": 118, "men_start": 120,
       "x": 12.3, "y": 25.1, "z": -130.0, "bearing": 180.0,
       "moving": false, "routing": false, "leaving": false, "missile_range": 70, "controlled": false}
    ]}
  ]
}
```

Einheiten-ID: `"<allianz>.<armee>.<einheit>"`, 1-basiert, stabil für die Dauer der Schlacht.
Koordinaten in Metern; y ist die Höhe, die Karte liegt in der x/z-Ebene.

## orders.lua

```lua
return { seq = 8, orders = {
  { unit = "2.1.3", cmd = "goto_location", args = { {x = 100, z = -50} } },
  { unit = "2.1.4", cmd = "attack_unit",   args = { {unit = "1.1.2"}, true, true } },
  { unit = "2.1.5", cmd = "halt" },
  { unit = "2.1.6", cmd = "release" },
}}
```

- Das Spiel führt eine Datei nur aus, wenn `seq` größer als die zuletzt ausgeführte ist.
- `cmd` ist eine Methode des Unit-Controllers (Whitelist im Skript) oder `take` / `release`.
- Argumente: Zahl, Bool und String werden durchgereicht, `{x=, z=}` wird zu einem `battle_vector`
  (y = aktuelle Höhe der Einheit), `{unit="a.r.u"}` wird zur Einheit.
- Jede Ausführung landet in `order_results` mit `ok`/`err`. Unbekannte Signaturen zeigen sich dort.

Bekannte Signaturen aus den Original-Skripten: `goto_location(vec[, rennen])`, `attack_unit(unit, true, rennen)` (so in main.pack, z. B. Kavallerie mit `true`),
`halt()`, `take_control()`, `release_control()`. Weitere Methoden: siehe `probe_runs/run3_ai_bridge_log.txt`.
