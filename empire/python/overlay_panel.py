"""
overlay_panel.py - kompakte Bedienoberflaeche des Steuerpults fuer das Overlay im Spielbild.

Das Steuerpult (ui.py) zeichnet hier ein Bild seiner eigenen Daten - dieselben Texte, Listen und Zustaende wie im
Fenster, deshalb sind beide immer synchron. Die DLL (dll/overlay.c) zeigt das Bild an.

Austausch ueber den Bridge-Ordner (<Spiel>/ai_bridge):
  overlay_panel.bin   vom Steuerpult: Kopf (5 x uint32: 'EBOV', Breite, Hoehe, seq, flags) + BGRA-Pixel
                      flags bit 0 = aufgeklappt (sonst zeigt die DLL die Meldungsleiste aus overlay.txt)
  overlay_keys.txt    von der DLL: "f8=<Anzahl F8-Druecke>" - jeder Druck klappt auf bzw. wechselt den Tab

Stufe A: nur Anzeige. F8 schaltet: zu -> Feldherr -> Befehle -> Strategie -> Aufstellung -> Einheiten -> zu.
Ein Tab-Wechsel im Steuerpult-Fenster zeigt (bei offenem Overlay) denselben Tab.
"""
from __future__ import annotations

import os
import struct
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 560, 360
TABS = ["Feldherr", "Befehle", "Strategie", "Aufstellung", "Einheiten"]

BG = (20, 22, 28, 215)
PANEL = (34, 37, 46, 235)
EDGE = (70, 74, 88, 255)
GOLD = (214, 178, 96, 255)
TEXT = (232, 228, 216, 255)
MUTED = (160, 160, 160, 255)
BLUE = (150, 200, 255, 255)
GREEN = (170, 225, 150, 255)
ORANGE = (255, 170, 80, 255)
BUTTON = (58, 63, 78, 255)
BUTTON_OFF = (44, 47, 58, 255)

_FONTS: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(size: int = 12, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "seguisb.ttf" if bold else "segoeui.ttf"
    key = (name, size)
    if key not in _FONTS:
        try:
            _FONTS[key] = ImageFont.truetype(str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name), size)
        except OSError:
            _FONTS[key] = ImageFont.load_default()
    return _FONTS[key]


@dataclass
class PanelData:
    """Momentaufnahme der Steuerpult-Daten fuer ein Bild."""
    tab: str = "Feldherr"
    running: bool = False
    status: str = "gestoppt"
    model: str = ""
    effort: str = ""
    interval: str = ""
    control: str = ""
    log: list[str] = field(default_factory=list)
    orders_player: str = ""
    orders_enemy: str = ""
    strategy_name: str = ""
    strategy: str = ""
    deploy: str = ""
    units_side: str = "player"
    units: list[tuple] = field(default_factory=list)       # (Name, Typ, Mann, Lage, Fuehrung, Status)
    units_info: str = ""
    active_orders: str = ""
    chat_target: str = "Meine Armee"
    chat_text: str = ""
    hotkeys: dict = field(default_factory=dict)


def _wrap(text: str, f: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """Text in Zeilen, die in width Pixel passen (Absaetze bleiben erhalten)."""
    out: list[str] = []
    for para in (text or "").splitlines() or [""]:
        if not para.strip():
            out.append("")
            continue
        line = ""
        for word in para.split(" "):
            cand = (line + " " + word) if line else word
            if f.getlength(cand) <= width:
                line = cand
            else:
                if line:
                    out.append(line)
                while f.getlength(word) > width and len(word) > 1:      # ueberlange Woerter hart trennen
                    cut = len(word)
                    while cut > 1 and f.getlength(word[:cut]) > width:
                        cut -= 1
                    out.append(word[:cut])
                    word = word[cut:]
                line = word
        out.append(line)
    return out


def _button(d: ImageDraw.ImageDraw, x: int, y: int, label: str, enabled: bool = True, accent: bool = False) -> int:
    f = font(11, True)
    w = int(f.getlength(label)) + 14
    d.rounded_rectangle((x, y, x + w, y + 20), 4, fill=BUTTON if enabled else BUTTON_OFF,
                        outline=GOLD if accent else EDGE)
    d.text((x + 7, y + 3), label, font=f, fill=TEXT if enabled else MUTED)
    return x + w + 6


def _scrollbar(d: ImageDraw.ImageDraw, x: int, y: int, h: int, total: int, visible: int, first: int) -> None:
    d.rectangle((x, y, x + 6, y + h), fill=(28, 30, 38, 255))
    if total <= visible or total <= 0:
        d.rectangle((x + 1, y + 1, x + 5, y + h - 1), fill=(70, 74, 88, 255))
        return
    th = max(14, int(h * visible / total))
    ty = y + int((h - th) * first / max(total - visible, 1))
    d.rounded_rectangle((x + 1, ty, x + 5, ty + th), 2, fill=(130, 134, 150, 255))


def _textbox(d: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str, *, title: str = "",
             bottom: bool = False, colors=None, f=None) -> None:
    """Feld mit Rahmen, umbrochenem Text und Scrollbalken. bottom=True zeigt das Ende (Protokoll)."""
    x0, y0, x1, y1 = box
    f = f or font(12)
    if title:
        d.text((x0, y0), title, font=font(11, True), fill=GOLD)
        y0 += 17
    d.rectangle((x0, y0, x1, y1), fill=PANEL, outline=EDGE)
    lh = 16
    lines = _wrap(text, f, x1 - x0 - 20)
    visible = max((y1 - y0 - 6) // lh, 1)
    first = max(len(lines) - visible, 0) if bottom else 0
    for i, line in enumerate(lines[first:first + visible]):
        col = colors(line) if colors else TEXT
        d.text((x0 + 6, y0 + 3 + i * lh), line, font=f, fill=col)
    _scrollbar(d, x1 - 9, y0 + 2, y1 - y0 - 4, len(lines), visible, first)


def _log_color(line: str):
    if " Plan Allianz" in line or "Aufstellungsplan" in line:
        return GREEN
    if "Befehl an die" in line:
        return BLUE
    if "FEHLER" in line or "ACHTUNG" in line or "Fehler" in line:
        return ORANGE
    if "zum Befehl" in line:
        return TEXT
    return MUTED


def render(p: PanelData) -> Image.Image:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, W - 1, H - 1), 6, fill=BG, outline=(90, 80, 50, 255))
    d.rectangle((0, 6, 3, H - 7), fill=GOLD)

    # Kopf: Tabs + Menue
    x = 12
    for t in TABS:
        f = font(12, True)
        w = int(f.getlength(t)) + 18
        active = t == p.tab
        d.rounded_rectangle((x, 6, x + w, 28), 4, fill=(60, 55, 40, 255) if active else (30, 32, 40, 255),
                            outline=GOLD if active else EDGE)
        d.text((x + 9, 9), t, font=f, fill=GOLD if active else MUTED)
        x += w + 4
    d.text((W - 66, 8), "F8", font=font(11, True), fill=MUTED)
    d.rounded_rectangle((W - 40, 6, W - 12, 28), 4, fill=(30, 32, 40, 255), outline=EDGE)
    d.text((W - 31, 6), "\u2261", font=font(16, True), fill=TEXT)          # Menue (Einstellungen)

    top, bottom_y = 36, H - 36
    if p.tab == "Feldherr":
        x = 12
        x0 = x
        x = _button(d, x, top, "\u25a0 Stoppen" if p.running else "    Starten", accent=True)
        if not p.running:      # Segoe UI hat kein Abspielsymbol: Dreieck selbst zeichnen
            d.polygon([(x0 + 8, top + 5), (x0 + 8, top + 15), (x0 + 16, top + 10)], fill=TEXT)
        for label, cmd in (("Aufstellung", "deploy"), ("Markierte \u2192 KI", "sel_ai"),
                           ("Markierte \u2192 Spieler", "sel_player")):
            hk = p.hotkeys.get(cmd, "")
            x = _button(d, x, top, f"{label} {hk}".strip(), enabled=p.running)
        st = f"{p.status}  |  {p.model}, {p.effort}, {p.interval}  |  KI f\u00fchrt: {p.control}"
        d.text((12, top + 26), st, font=font(11), fill=GREEN if p.running else MUTED)
        log_top = top + 46
        if p.active_orders:
            ao = _wrap(p.active_orders, font(11), W - 24)[:2]
            for i, line in enumerate(ao):
                d.text((12, log_top + i * 14), line, font=font(11), fill=BLUE)
            log_top += 14 * len(ao) + 4
        _textbox(d, (12, log_top, W - 12, bottom_y - 4), "\n".join(p.log[-200:]), bottom=True,
                 colors=_log_color, f=font(11))
    elif p.tab == "Befehle":
        mid = (top + bottom_y) // 2
        _textbox(d, (12, top, W - 12, mid - 4), p.orders_player, title="Meine Armee (verbindlich)")
        _textbox(d, (12, mid + 2, W - 12, bottom_y - 4), p.orders_enemy, title="Gegner (verbindlich)")
        _button(d, W - 140, top - 2, "Pr\u00fcfen & verbessern")
        _button(d, W - 140, mid, "Pr\u00fcfen & verbessern")
    elif p.tab == "Strategie":
        _textbox(d, (12, top, W - 12, bottom_y - 4), p.strategy, title=f"Vorlage: {p.strategy_name or '-'}")
        _button(d, W - 140, top - 2, "Pr\u00fcfen & verbessern")
    elif p.tab == "Aufstellung":
        _textbox(d, (12, top, W - 12, bottom_y - 30), p.deploy, title="Aufstellungsprompt")
        x = _button(d, 12, bottom_y - 24, f"Aufstellung \u00fcbernehmen {p.hotkeys.get('deploy', '')}".strip(),
                    enabled=p.running, accent=True)
        _button(d, x, bottom_y - 24, "Pr\u00fcfen & verbessern")
    elif p.tab == "Einheiten":
        x = 12
        for label, val in (("Meine Armee", "player"), ("Gegner", "enemy")):
            x = _button(d, x, top, label, accent=p.units_side == val)
        x = _button(d, x + 8, top, "Markierte \u2192 Spieler", enabled=p.running)
        _button(d, x, top, "Markierte \u2192 KI", enabled=p.running)
        d.text((12, top + 24), p.units_info, font=font(10), fill=MUTED)
        y0, y1 = top + 42, bottom_y - 4
        d.rectangle((12, y0, W - 12, y1), fill=PANEL, outline=EDGE)
        cols = [("Name", 14, 0), ("Mann", 160, 2), ("Lage", 230, 3), ("F\u00fchrung", 330, 4), ("Status", 470, 5)]
        for label, cx, _ in cols:
            d.text((cx + 6, y0 + 2), label, font=font(11, True), fill=GOLD)
        lh = 16
        visible = max((y1 - y0 - 22) // lh, 1)
        for i, row in enumerate(p.units[:visible]):
            yy = y0 + 20 + i * lh
            for _, cx, idx in cols:
                val = str(row[idx]) if idx < len(row) else ""
                col = BLUE if idx == 4 and val == "KI" else (ORANGE if idx == 5 and val else TEXT)
                d.text((cx + 6, yy), val[:22], font=font(11), fill=col)
        _scrollbar(d, W - 21, y0 + 20, y1 - y0 - 22, len(p.units), visible, 0)

    # Chat-Zeile (immer sichtbar)
    cy = H - 30
    d.text((12, cy + 3), "Befehl an", font=font(11), fill=MUTED)
    tx = _button(d, 68, cy, f"{p.chat_target}    ")
    d.polygon([(tx - 20, cy + 8), (tx - 12, cy + 8), (tx - 16, cy + 13)], fill=TEXT)   # Pfeil der Auswahl
    d.rectangle((tx, cy, W - 86, cy + 20), fill=PANEL, outline=EDGE)
    ct = p.chat_text or "Befehl eingeben \u2026"
    d.text((tx + 6, cy + 3), ct[-60:], font=font(11), fill=TEXT if p.chat_text else MUTED)
    _button(d, W - 80, cy, "Senden")
    return img


def write_panel(img: Image.Image | None, path: Path, seq: int, expanded: bool) -> None:
    """Bild fuer die DLL schreiben (atomar). Zugeklappt: nur Kopf mit flags=0, keine Pixel."""
    w, h = (img.size if img is not None and expanded else (0, 0))
    head = struct.pack("<4sIIII", b"EBOV", w, h, seq, 1 if expanded else 0)
    data = head + (img.tobytes("raw", "BGRA") if img is not None and expanded else b"")
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:          # DLL liest gerade
            import time
            time.sleep(0.01)


def read_f8(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="ascii", errors="ignore")
    except (FileNotFoundError, OSError):
        return None
    for part in text.split():
        if part.startswith("f8="):
            try:
                return int(part[3:])
            except ValueError:
                return None
    return None
