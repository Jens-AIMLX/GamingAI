"""Meldungen fuer das Overlay im Spielbild (ai_bridge/overlay.txt).

Der Feldherr schreibt hier die letzten Meldungen hinein, die DLL (dll/overlay.c) liest die Datei jede Sekunde und
zeichnet sie unten links ins Spielbild. Format: UTF-8, eine Zeile je Bildschirmzeile, das erste Zeichen bestimmt
die Farbe und wird nicht angezeigt:
    +  aktueller Plan meiner Armee (hellgruen)   -  Plan des Gegners / Hinweis (grau)
    >  eigener Befehl (hellblau)                 *  Antwort des Feldherrn (weiss)
    !  Warnung / Fehler (orange)
Oben stehen fest die aktuellen Plaene (eigene Armee, Gegner), darunter die letzten Ereignisse (Befehle, Antworten,
Warnungen, Hinweise) - sonst wuerden die Plaene alle paar Sekunden die Ereignisse verdraengen bzw. ohne Plaene bliebe
das Feld nach der Aufstellung stehen (Schlacht 2026-11-20 12:41).
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
MAX_MESSAGES = 4    # Ereignisse unter den Plaenen

ACK = re.compile(r"Feldherr Allianz (\d+) zum Befehl .*?: (.*)$", re.S)


class OverlayFeed:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.msgs: deque[tuple[str, str]] = deque(maxlen=MAX_MESSAGES)   # (Farbzeichen, Text mit Zeit)
        self.plans: dict[str, tuple[str, str]] = {}                       # "mine"/"enemy" -> (Farbzeichen, Text)

    def add(self, kind: str, text: str, battle_time: float | None = None) -> None:
        text = " ".join(str(text).split())
        if not text:
            return
        self.msgs.append((kind, self._stamp(battle_time) + text))
        self.write()

    @staticmethod
    def _stamp(battle_time: float | None) -> str:
        return f"{int(battle_time) // 60:02d}:{int(battle_time) % 60:02d}  " if battle_time is not None else ""

    def set_plan(self, side: str, kind: str, text: str, battle_time: float | None = None) -> None:
        text = " ".join(str(text).split())
        if text:
            self.plans[side] = (kind, self._stamp(battle_time) + text)
            self.write()

    def write(self) -> None:
        lines: list[str] = []
        entries = [self.plans[k] for k in ("mine", "enemy") if k in self.plans] + list(self.msgs)
        for kind, text in entries:
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
        if t in ("plan", "deploy"):
            mine = entry.get("alliance") == player_alliance
            label = ("Plan" if t == "plan" else "Aufstellung") + ("" if mine else " Gegner")
            intent = (entry.get("plan") or {}).get("intent", "")
            self.set_plan("mine" if mine else "enemy", "+" if mine else "-", f"{label}: {intent}", battle_time)
        elif t == "order":
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
