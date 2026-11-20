"""Meldungen fuer das Overlay im Spielbild (ai_bridge/overlay.txt).

Der Feldherr schreibt hier die letzten Meldungen hinein, die DLL (dll/overlay.c) liest die Datei jede Sekunde und
zeichnet sie unten links ins Spielbild. Format: UTF-8, eine Zeile je Bildschirmzeile, das erste Zeichen bestimmt
die Farbe und wird nicht angezeigt:
    >  eigener Befehl (hellblau)      *  Antwort des Feldherrn (weiss)
    !  Warnung / Fehler (orange)      -  Hinweis (grau)
Lange Meldungen werden hier umbrochen (hoechstens zwei Zeilen je Meldung), die DLL zeichnet die Zeilen nur.
"""
from __future__ import annotations

import os
import re
import textwrap
from collections import deque
from pathlib import Path

WIDTH = 74          # Zeichen je Zeile (Segoe UI 16 px: ~600 px)
MAX_LINES = 2       # Zeilen je Meldung
MAX_MESSAGES = 6

ACK = re.compile(r"Feldherr Allianz (\d+) zum Befehl .*?: (.*)$", re.S)


class OverlayFeed:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.msgs: deque[tuple[str, str]] = deque(maxlen=MAX_MESSAGES)   # (Farbzeichen, Text mit Zeit)

    def add(self, kind: str, text: str, battle_time: float | None = None) -> None:
        text = " ".join(str(text).split())
        if not text:
            return
        stamp = f"{int(battle_time) // 60:02d}:{int(battle_time) % 60:02d}  " if battle_time is not None else ""
        self.msgs.append((kind, stamp + text))
        self.write()

    def write(self) -> None:
        lines: list[str] = []
        for kind, text in self.msgs:
            parts = textwrap.wrap(text, WIDTH, subsequent_indent="       ") or [""]
            if len(parts) > MAX_LINES:
                parts = parts[:MAX_LINES]
                parts[-1] = parts[-1][:WIDTH - 1].rstrip() + "…"
            lines += [kind + p for p in parts]
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass   # DLL liest gerade - naechste Meldung schreibt erneut

    def from_log(self, entry: dict, player_alliance: int, battle_time: float | None) -> None:
        """Log-Eintrag des Feldherrn -> Overlay-Meldung (nur was fuer den Spieler bestimmt ist)."""
        t, typ = entry.get("type"), entry.get("text") or ""
        if t == "order":
            who = "meine Armee" if entry.get("alliance") == player_alliance else "Gegner"
            self.add(">", f"Befehl an {who}: {typ}", battle_time)
        elif t == "info":
            m = ACK.match(typ)
            if m:
                who = "Feldherr" if int(m.group(1)) == player_alliance else "Gegner-Feldherr"
                self.add("*", f"{who}: {m.group(2)}", battle_time)
            else:
                self.add("-", typ, battle_time)
        elif t == "warning":
            # Hinweise an das Modell (englisch, "group '...'") gehoeren nicht ins Spielbild
            if typ.startswith(("group '", "deployment group", "Uebergabe im Plan ignoriert")):
                return
            self.add("!", typ, battle_time)
        elif t == "error":
            self.add("!", f"Fehler Allianz {entry.get('alliance')}: {entry.get('error', '')}"[:160], battle_time)
        elif t == "deploy_check":
            self.add("-", typ, battle_time)
