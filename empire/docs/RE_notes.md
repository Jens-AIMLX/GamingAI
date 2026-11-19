# Empire.exe – Reverse-Engineering-Notizen (Battle-Script-Bridge)

Build: Steam 11084729, Empire.exe SHA-256 `9b130e708d01a60010df286d2e39867b8fc4f0814d544fcbfe3c1e352df04120`,
32-Bit, ImageBase 0x400000, ASLR aktiv → Laufzeitadresse = `GetModuleHandle(NULL) + (VA - 0x400000)`.
Alle Adressen unten sind VAs bei ImageBase 0x400000. Werkzeuge: `tools/empre.py` (Disassembler/Xrefs), `tools/calls.py` (Aufrufer).

## Probe-Ergebnisse (Lua)
- `data/battle_scripted.lua` läuft bei jedem Kampf, aber in einem **eigenen Advice-Lua-Zustand**
  (Objekt-ctor `0xd20fa0`, erzeugt im BATTLE_ENV-ctor bei `0x579de1`). Dort: io, os, debug, package.loadlib verfügbar,
  `package.cpath` enthält den Spielordner. **Keine** Battle-API (`empire_battle` fehlt), Zustand führt im Kampf kaum Code aus.

## Battle-Script-Umgebung
- `0x65b1a0`  registriert alle Battle-Lua-Klassen (ruft u. a. `0x65b910` → `battle.alliance`, `0x65cc10` → `empire_battle`).
- `0x655830`  ctor der Battle-Script-Umgebung (thiscall, 3 Args: BATTLE_ENV*, String* scriptpfad, 1), Objektgröße 0x468.
  Übergibt `0x65b1a0` an Lua-Basis `0xf6b330`.
- **Einzige relevante Erzeugung: BATTLE_ENV bei `0x57b8d5`:**
  ```
  0x57b8d5  cmp byte  [edi+0x28168], 0   ; Flag (unbekannt, evtl. MP/Replay) muss 0 sein
  0x57b8dc  jne 0x57b947                 ; sonst kein Skript
  0x57b8de  cmp word  [edi+0x280bc], 0   ; Länge des battle_script-Pfads
  0x57b8e6  jbe 0x57b947                 ; leer -> KEINE Battle-Script-Umgebung  <== Ursache
  0x57b8e8  push 0x468 ; call 0xff6630 (operator new)
  0x57b8fb  lea ecx,[edi+0x280b4] ; copy-ctor 0x4b7750 in Temp [esp+0x2f8]
  0x57b91b  call 0x655830        ; ctor(edi, &temp, 1)
  0x57b930  mov [edi+0x281ec], eax ; Zeiger auf Battle-Script-Umgebung
  0x57b942  call 0x4b7c10        ; dtor Temp
  ```
  Original-Bytes ab 0x57b8de: `66 83 bf bc 80 02 00 00 76 5f`
- BATTLE_ENV + 0x28058 = eingebettetes BATTLE_SETUP_INFO (Pfad-String bei Setup+0x5c, Länge Word bei Setup+0x64).
- Setup-XML-Parser liest `battle_script` bei `0x4febf8`, hängt `.battle_script` an, wenn keine Endung.

## Engine-String (CA-String, 8-Bit)
- `0x4b77c0`  ctor aus `const char*` (thiscall, ecx=this, 1 Arg)
- `0x4b7750`  copy-ctor
- `0x4b7c10`  dtor

## Lua 5.1 API (statisch gelinkt, aus Aufrufmustern abgeleitet, noch zu verifizieren)
- `0xf071d0` lua_createtable(L, narr, nrec)
- `0xf07a70` lua_pushstring(L, s)
- `0xf078f0` lua_pushcclosure(L, fn, n)
- `0xf07e20` lua_setfield(L, idx, k)   (idx 0xffffd8ee = LUA_GLOBALSINDEX)
- `0xf07560` lua_pushvalue? / `0xf07f30` lua_rawset? / `0xf07ad0` lua_settop? — unsicher

## Geplanter Patch (DLL, geladen per package.loadlib aus battle_scripted.lua)
Bei `0x57b8de` (10 Bytes) auf Stub springen: Pfadlänge > 0 → Originalweg (0x57b8e8). Leer →
Temp-String per `0x4b77c0` aus `"<bridge>.battle_script"` bauen, `new 0x468`, `0x655830(edi,&temp,1)`,
Ergebnis nach `[edi+0x281ec]`, Temp per `0x4b7c10` freigeben, weiter bei `0x57b947`.
Vor dem Patchen Bytes prüfen (Versions-Check wie rome-battle-ai).

## Ergebnis Lauf 3 (19.11.2026, Kampagnenschlacht) – Patch funktioniert
- empire_bridge.dll geladen, Patch angewendet, `ai_bridge.battle_script` eingesetzt, Schlacht lief normal bis „Complete“.
- Battle-Zustand (`decoda_name = "Parent State"`): io, os, package.loadlib, debug verfügbar → Datei-Brücke direkt aus Lua möglich.
- `empire_battle` voll nutzbar: Allianzen/Armeen/Einheiten, Timer, Phasen-Events (Startup, Deployment, Deployed, Complete), Controller.
- Einheiten-Methoden: bearing initial_number_of_men is_leaving_battle is_moving is_routing missile_range name number_of_men_alive position type unit_in_range
  (`name()` liefert nur den Index; `type()` noch ungetestet). Keine Moral/Ermüdung/Munition.
- Controller-Methoden: siehe probe_runs/run3_ai_bridge_log.txt (u. a. take_control, goto_location(_angle_width), attack_unit, change_formation, fire_at_will, skirmish, withdraw).
- Offen: welche Allianz der Spieler ist (API hat dafür keine Methode).

## Lauf 4 (19.11.2026) – Absturz, behoben
- WER: Absturz in `empire_bridge.dll_unloaded` + 0x14d0 (= Stub), 0xc0000005. Lua 5.1 gibt per loadlib geladene
  DLLs beim Schließen des Lua-Zustands frei (FreeLibrary), der Patch zeigte danach ins Leere.
- Fix: DLL pinnt sich mit `GetModuleHandleExW(FROM_ADDRESS | PIN)`; vorhandener Patch wird auf den aktuellen Stub neu ausgerichtet.

## Lauf 5 (19.11.2026) – Steuerung funktioniert
- PIN-Fix wirkt, Schlacht lief bis „Complete“ ohne Absturz.
- Spieler-Allianz per `register_unit_selection_handler` + `unit ==` erkannt (Klick in der Aufstellung).
- take_control + goto_location (150 m seitlich): alle 11 Einheiten liefen gleichmäßig zum Ziel (~1,4 m/s), vom Spieler sichtbar bestätigt.
  CHECK A meldete FAIL nur wegen zu strenger Schwelle (60 % in 50 s) – Check auf Richtung/Fortschritt umgestellt.
- halt: 10/11 Einheiten 30 s still (max. 3 m) → Spiel-KI greift bei take_control nicht ein. release_control: KI übernimmt wieder (9/11 bewegt).
- unit:type() liefert den Einheitenschlüssel (z. B. euro_line_infantry_colonial, 12_lber_land_cannon).

## Lauf 6 (19.11.2026) – Datei-Brücke, Lua-Unterschied gefunden
- Neues Battle-Skript ohne Empire-Neustart geladen; Spieler-Allianz per Klick erkannt; 11 Gegner-Einheiten übernommen.
- `write_state` scheiterte: **`math.huge` fehlt im Spiel-Lua**. Fix: 1/0; Bridge-Skript nutzt keine math-Bibliothek mehr.
  sim_test.py bildet das nach (math nur mit sqrt).

## Lauf 7 (19.11.2026) – Datei-Brücke im Spiel funktioniert
- selftest.py über die Brücke: A=PASS B=PASS C=FAIL* D=PASS E=INFO F=PASS, 26/26 Befehle quittiert ohne Fehler.
  *C: max. 5,6 m in 20 s unter Artilleriebeschuss (Reihen ordnen sich neu), kein KI-Eingriff → Grenze auf 15 m geändert.
- E: attack_unit(Ziel, true, false) – Kamelreiter rücken auf Ziel-Kanone vor (340 m → 287 m in 30 s).
- register_command_handler meldet auch Skript-Befehle (Halt/Attack Unit zeitgleich mit unseren Befehlen), nicht nur Spielerbefehle.
