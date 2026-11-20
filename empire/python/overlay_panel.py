"""
overlay_panel.py - kompakte Bedienoberflaeche des Steuerpults fuer das Overlay im Spielbild.

Das Steuerpult (ui.py) zeichnet hier ein Bild seiner eigenen Daten - dieselben Texte, Listen und Zustaende wie im
Fenster, deshalb sind beide immer synchron. Die DLL (dll/overlay.c) zeigt das Bild an und meldet Mauseingaben.

Austausch ueber den Bridge-Ordner (<Spiel>/ai_bridge):
  overlay_panel.bin   vom Steuerpult: Kopf (5 x uint32: 'EBOV', Breite, Hoehe, seq, flags) + BGRA-Pixel
                      flags bit 0 = aufgeklappt (sonst zeigt die DLL die Meldungsleiste aus overlay.txt)
  overlay_keys.txt    von der DLL: "f8=<Anzahl F8-Druecke>" - jeder Druck klappt auf bzw. wechselt den Tab
  overlay_input.txt   von der DLL (wird fortgeschrieben): "<n> down|up|drag|wheel <x> <y> [<delta>]" in
                      Panel-Koordinaten (Stufe B); "<n> key <vk> <mods>" und "<n> char <code>" (Stufe C)
                      flags bit 1 im Bildkopf = ein Feld hat den Fokus -> die DLL leitet die Tastatur hierher um

render() liefert das Bild UND die klickbaren Bereiche (Hot), damit das Steuerpult einen Klick der richtigen Aktion
zuordnen kann - Bild und Trefferflaechen stammen so aus derselben Zeichnung.
"""
from __future__ import annotations

import os
import re
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 560, 360                    # aktuelle Groesse (render() setzt sie aus PanelData.width/height)
MIN_W, MIN_H, MAX_W, MAX_H = 480, 300, 1400, 1000   # wie PANEL_MIN/MAX in dll/overlay.c
# Protokoll-Ansichten (wie die Tabs im Steuerpult-Fenster); Spezialansichten zeigen nur ihre Sorte
LOG_VIEWS = ["Alles", "Plan Allianz 1", "Plan Allianz 2", "Befehle", "Meldungen"]
LOG_SHORT = {"Alles": "Alles", "Plan Allianz 1": "Plan 1", "Plan Allianz 2": "Plan 2", "Befehle": "Befehle",
             "Meldungen": "Meldungen"}
TABS = ["Feldherr", "Befehle", "Strategie", "Aufstellung", "Einheiten"]
CONTROLS = [("Gegner", "enemy"), ("Meine Armee", "player"), ("Beide", "both"), ("Aus", "none")]
EFFORTS = ["low", "medium", "high"]

PLAN_RE = re.compile(r"(?:Plan|Aufstellungsplan) Allianz (\d+)")

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
SELECT = (70, 90, 130, 255)
LINE_H = 16

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
    control: str = ""            # Anzeige ("Beide Seiten")
    control_val: str = ""        # Wert ("both")
    own_groups: bool = False
    iv_player: int = 10          # Planungsintervall eigene Armee (s)
    iv_enemy: int = 10           # ... Gegner
    close_f: float = 0.5         # Faktor im Nahkampf
    log: list[str] = field(default_factory=list)
    orders_player: str = ""
    orders_enemy: str = ""
    strategy_name: str = ""
    strategy: str = ""
    deploy: str = ""
    units_side: str = "player"
    units: list[tuple] = field(default_factory=list)       # (Name, Typ, Mann, Lage, Fuehrung, Status, bid)
    selected: tuple = ()                                   # markierte bids (wie im Fenster)
    units_info: str = ""
    active_orders: str = ""
    chat_target: str = "Meine Armee"
    chat_text: str = ""
    hotkeys: dict = field(default_factory=dict)
    scroll: dict = field(default_factory=dict)             # Feld -> erste sichtbare Zeile (log: None = Ende)
    menu_open: bool = False
    log_view: str = "Alles"
    zone_on: bool = True
    zone_m: int = 30
    pressed: tuple | None = None                           # gerade gedrueckte Aktion (Rueckmeldung beim Klick)
    focus: str | None = None                               # Feld mit Tastaturfokus (EDITABLE oder "chat")
    caret: int = 0                                         # Cursor im Fokusfeld (Zeichen ab Anfang)
    review: dict | None = None                             # offene Pruefung: title, status, analysis, proposal, done
    width: int = 560                                       # Groesse des Overlays (mit der Maus aenderbar)
    height: int = 360


@dataclass
class Hot:
    """Klickbarer Bereich: Rechteck + Aktion. Scrollbereiche tragen ihre Kennzahlen fuer Rad und Balken."""
    x0: int
    y0: int
    x1: int
    y1: int
    action: tuple
    total: int = 0
    visible: int = 0
    first: int = 0
    bar: tuple | None = None        # (x0, y0, x1, y1) des Scrollbalkens
    lines: list | None = None       # Textfeld: sichtbare Zeilen [(Text, Startzeichen)] fuer Klick -> Cursor
    tx: int = 0                     # linke Kante des Texts
    ty: int = 0                     # Oberkante der ersten sichtbaren Zeile
    fsize: int = 12

    def offset_at(self, x: int, y: int) -> int | None:
        """Klick im Textfeld -> Zeichenposition (fuer den Cursor)."""
        if not self.lines:
            return None
        i = min(max((y - self.ty) // LINE_H, 0), len(self.lines) - 1)
        line, start = self.lines[i]
        f = font(self.fsize)
        col = 0
        while col < len(line) and f.getlength(line[:col + 1]) - f.getlength(line[col]) / 2 < x - self.tx:
            col += 1
        return start + col

    def hit(self, x: int, y: int) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1


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


EDITABLE = ("orders_player", "orders_enemy", "strategy", "deploy", "rev_sug")


def _wrap_off(text: str, f: ImageFont.FreeTypeFont, width: int) -> list[tuple[str, int]]:
    """Wie _wrap, aber mit der Startposition jeder Zeile im Text (fuer Cursor und Klick)."""
    out: list[tuple[str, int]] = []
    pos = 0
    for para in (text or "").split("\n"):
        if not para:
            out.append(("", pos))
        else:
            line, lstart, i = "", pos, 0
            while i < len(para):
                j = para.find(" ", i)
                word = para[i:] if j < 0 else para[i:j + 1]          # Wort mit folgendem Leerzeichen
                if line and f.getlength(line + word.rstrip()) > width:
                    out.append((line, lstart))
                    line, lstart = "", pos + i
                while not line and f.getlength(word.rstrip()) > width and len(word) > 1:   # ueberlang
                    cut = len(word)
                    while cut > 1 and f.getlength(word[:cut]) > width:
                        cut -= 1
                    out.append((word[:cut], pos + i))
                    i += cut
                    word = word[cut:]
                    lstart = pos + i
                line += word
                i += len(word)
            out.append((line, lstart))
        pos += len(para) + 1
    return out


class Painter:
    def __init__(self, p: PanelData) -> None:
        self.p = p
        self.img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.img)
        self.hots: list[Hot] = []

    def button(self, x: int, y: int, label: str, action: tuple | None, enabled: bool = True,
               accent: bool = False) -> int:
        f = font(11, True)
        w = int(f.getlength(label)) + 14
        pressed = action is not None and self.p.pressed == action
        fill = (90, 80, 50, 255) if pressed else (BUTTON if enabled else BUTTON_OFF)
        self.d.rounded_rectangle((x, y, x + w, y + 20), 4, fill=fill, outline=GOLD if accent else EDGE)
        self.d.text((x + 7, y + 3), label, font=f, fill=TEXT if enabled else MUTED)
        if action is not None and enabled:
            self.hots.append(Hot(x, y, x + w, y + 20, action))
        return x + w + 6

    def scrollbar(self, x: int, y: int, h: int, total: int, visible: int, first: int) -> None:
        d = self.d
        d.rectangle((x, y, x + 6, y + h), fill=(28, 30, 38, 255))
        if total <= visible or total <= 0:
            d.rectangle((x + 1, y + 1, x + 5, y + h - 1), fill=(70, 74, 88, 255))
            return
        th = max(14, int(h * visible / total))
        ty = y + int((h - th) * first / max(total - visible, 1))
        d.rounded_rectangle((x + 1, ty, x + 5, ty + th), 2, fill=(130, 134, 150, 255))

    def textbox(self, key: str, box: tuple[int, int, int, int], text: str, *, title: str = "",
                follow_end: bool = False, colors=None, f=None, fsize: int = 12) -> None:
        """Feld mit Rahmen, umbrochenem Text und Scrollbalken; Scrollstand aus p.scroll[key]. Hat das Feld den
        Fokus: goldener Rahmen und Cursor (das Feld scrollt so, dass der Cursor sichtbar bleibt)."""
        x0, y0, x1, y1 = box
        f = f or font(fsize)
        d = self.d
        focused = self.p.focus == key
        if title:
            d.text((x0, y0), title, font=font(11, True), fill=GOLD)
            if key in EDITABLE:
                hint = "Esc = fertig" if focused else "klicken zum Bearbeiten"
                d.text((x0 + f.getlength(title) + 26, y0 + 1), hint, font=font(10), fill=MUTED if not focused else BLUE)
            y0 += 17
        d.rectangle((x0, y0, x1, y1), fill=PANEL, outline=GOLD if focused else EDGE, width=2 if focused else 1)
        lines = _wrap_off(text, f, x1 - x0 - 20)
        visible = max((y1 - y0 - 6) // LINE_H, 1)
        maxfirst = max(len(lines) - visible, 0)
        first = self.p.scroll.get(key)
        if first is None:
            first = maxfirst if follow_end else 0
        cline = None
        if focused:
            caret = min(max(self.p.caret, 0), len(text))
            cline = max((i for i, (_, st) in enumerate(lines) if st <= caret), default=0)
            if cline < first:
                first = cline
            elif cline >= first + visible:
                first = cline - visible + 1
        first = min(max(first, 0), maxfirst)
        shown = lines[first:first + visible]
        for i, (line, _) in enumerate(shown):
            d.text((x0 + 6, y0 + 3 + i * LINE_H), line, font=f, fill=colors(line) if colors else TEXT)
        if focused and cline is not None and first <= cline < first + visible:
            line, st = lines[cline]
            cx = x0 + 6 + int(f.getlength(line[:caret - st]))
            cy = y0 + 3 + (cline - first) * LINE_H
            d.line((cx, cy + 1, cx, cy + LINE_H - 2), fill=GOLD, width=2)
        bar = (x1 - 9, y0 + 2, x1 - 3, y1 - 2)
        self.scrollbar(x1 - 9, y0 + 2, y1 - y0 - 4, len(lines), visible, first)
        self.hots.append(Hot(x0, y0, x1, y1, ("scroll", key), total=len(lines), visible=visible, first=first,
                             bar=bar, lines=shown, tx=x0 + 6, ty=y0 + 3, fsize=fsize))


def log_category(line: str) -> str:
    """Sorte einer Protokollzeile: 'Plan Allianz N', 'Befehle' oder 'Meldungen'."""
    m = PLAN_RE.search(line)
    if m and m.group(1) in ("1", "2"):
        return f"Plan Allianz {m.group(1)}"
    if ("Befehl an die" in line or "zum Befehl" in line or "uebergeben" in line or "Sofortbefehle" in line
            or "Aufstellung angefordert" in line):
        return "Befehle"
    return "Meldungen"


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


def render(p: PanelData) -> tuple[Image.Image, list[Hot]]:
    global W, H
    W = min(max(int(p.width), MIN_W), MAX_W)
    H = min(max(int(p.height), MIN_H), MAX_H)
    pt = Painter(p)
    d = pt.d
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
        pt.hots.append(Hot(x, 6, x + w, 28, ("tab", t)))
        x += w + 4
    # Greiffeld zum Verschieben (zwischen Tabs und Menue) - die DLL verschiebt beim Ziehen
    gx0, gx1 = x + 4, W - 44
    if gx1 - gx0 > 30:
        cxm = (gx0 + W - 70) // 2
        for i in range(-3, 4):
            for yy in (13, 20):
                d.ellipse((cxm + i * 7 - 1, yy - 1, cxm + i * 7 + 1, yy + 1), fill=(120, 110, 80, 255))
        pt.hots.append(Hot(gx0, 4, gx1, 30, ("grip_move",)))
    d.text((W - 66, 8), "F8", font=font(11, True), fill=MUTED)
    d.rounded_rectangle((W - 40, 6, W - 12, 28), 4, fill=(60, 55, 40, 255) if p.menu_open else (30, 32, 40, 255),
                        outline=GOLD if p.menu_open else EDGE)
    d.text((W - 31, 6), "\u2261", font=font(16, True), fill=TEXT)          # Menue (Einstellungen)
    pt.hots.append(Hot(W - 40, 6, W - 12, 28, ("menu",)))

    top, bottom_y = 36, H - 36
    if p.review:
        _review_view(pt, top, bottom_y)
    elif p.tab == "Feldherr":
        x = x0 = 12
        x = pt.button(x, top, "\u25a0 Stoppen" if p.running else "    Starten", ("stop",) if p.running else ("start",),
                      accent=True)
        if not p.running:      # Segoe UI hat kein Abspielsymbol: Dreieck selbst zeichnen
            d.polygon([(x0 + 8, top + 5), (x0 + 8, top + 15), (x0 + 16, top + 10)], fill=TEXT)
        for label, cmd in (("Aufstellung", "deploy"), ("Markierte \u2192 KI", "sel_ai"),
                           ("Markierte \u2192 Spieler", "sel_player")):
            hk = p.hotkeys.get(cmd, "")
            x = pt.button(x, top, f"{label} {hk}".strip(), ("action", cmd), enabled=p.running)
        # KI fuehrt: eigene Armee / Gegner (Schalter mit Symbol) - neben den Knoepfen, sonst in der Zeile darunter
        own, foe = p.control_val in ("player", "both"), p.control_val in ("enemy", "both")
        if x + 70 > W - 12:
            x, row = 12, top + 26
        else:
            x, row = x + 4, top
        x = _toggle(pt, x, row, "player", own)
        _toggle(pt, x, row, "enemy", foe)
        # Planungsintervall je Seite + Nahkampf-Faktor
        y2 = top + 26 if row == top else top + 50
        x = 12
        for side, val, on in (("player", p.iv_player, own), ("enemy", p.iv_enemy, foe)):
            _side_icon(d, x + 2, y2 + 3, side, on, 14)
            x = pt.button(x + 20, y2, "\u2212", ("iv", side, -5))
            d.text((x, y2 + 3), f"{val} s", font=font(11), fill=TEXT if on else MUTED)
            x = pt.button(x + 34, y2, "+", ("iv", side, 5)) + 6
        d.text((x, y2 + 3), "Nahkampf \u00d7" + f"{p.close_f:.2f}".replace(".", ","), font=font(11), fill=MUTED)
        sx0 = x + 98
        sx1 = min(sx0 + 110, W - 16)
        if sx1 - sx0 >= 40:
            cy = y2 + 10
            d.rounded_rectangle((sx0, cy - 2, sx1, cy + 2), 2, fill=EDGE)
            frac = (p.close_f - 0.25) / 0.75
            kx = int(sx0 + frac * (sx1 - sx0))
            d.rounded_rectangle((sx0, cy - 2, kx, cy + 2), 2, fill=GOLD)
            d.ellipse((kx - 6, cy - 6, kx + 6, cy + 6), fill=GOLD, outline=(90, 80, 50, 255))
            pt.hots.append(Hot(sx0 - 6, y2, sx1 + 6, y2 + 20, ("slider", "close_f"), bar=(sx0, y2, sx1, y2 + 20)))
        st = f"{p.status}  |  {p.model}, {p.effort}"
        d.text((12, y2 + 24), st, font=font(11), fill=GREEN if p.running else MUTED)
        log_top = y2 + 44
        if p.active_orders:
            ao = _wrap(p.active_orders, font(11), W - 130)[:2]
            for i, line in enumerate(ao):
                d.text((12, log_top + i * 14), line, font=font(11), fill=BLUE)
            pt.button(W - 116, log_top - 2, "Befehle aufheben", ("clear_orders",), enabled=p.running)
            log_top += max(14 * len(ao), 20) + 4
        x = 12
        for v in LOG_VIEWS:                       # Filter wie die Protokoll-Tabs im Fenster
            x = pt.button(x, log_top, LOG_SHORT[v], ("logview", v), accent=p.log_view == v)
        log_top += 24
        pt.textbox("log", (12, log_top, W - 12, bottom_y - 4), "\n".join(p.log[-300:]), follow_end=True,
                   colors=_log_color, f=font(11))
    elif p.tab == "Befehle":
        mid = (top + bottom_y) // 2
        pt.textbox("orders_player", (12, top, W - 12, mid - 4), p.orders_player, title="Meine Armee (verbindlich)")
        pt.textbox("orders_enemy", (12, mid + 2, W - 12, bottom_y - 4), p.orders_enemy, title="Gegner (verbindlich)")
        pt.button(W - 140, top - 2, "Pr\u00fcfen & verbessern", ("review", "orders_player"))
        pt.button(W - 140, mid, "Pr\u00fcfen & verbessern", ("review", "orders_enemy"))
    elif p.tab == "Strategie":
        pt.textbox("strategy", (12, top, W - 12, bottom_y - 4), p.strategy, title=f"Vorlage: {p.strategy_name or '-'}")
        pt.button(W - 140, top - 2, "Pr\u00fcfen & verbessern", ("review", "strategy"))
    elif p.tab == "Aufstellung":
        pt.textbox("deploy", (12, top, W - 12, bottom_y - 30), p.deploy, title="Aufstellungsprompt")
        x = pt.button(12, bottom_y - 24, f"Aufstellung \u00fcbernehmen {p.hotkeys.get('deploy', '')}".strip(),
                      ("action", "deploy"), enabled=p.running, accent=True)
        pt.button(x, bottom_y - 24, "Pr\u00fcfen & verbessern", ("review", "deploy"))
    elif p.tab == "Einheiten":
        x = 12
        for label, val in (("Meine Armee", "player"), ("Gegner", "enemy")):
            x = pt.button(x, top, label, ("units_side", val), accent=p.units_side == val)
        x = pt.button(x + 8, top, "Markierte \u2192 Spieler", ("give", "give_player"), enabled=p.running)
        pt.button(x, top, "Markierte \u2192 KI", ("give", "give_ai"), enabled=p.running)
        d.text((12, top + 24), p.units_info, font=font(10), fill=MUTED)
        y0, y1 = top + 42, bottom_y - 4
        d.rectangle((12, y0, W - 12, y1), fill=PANEL, outline=EDGE)
        cols = [(n, int(cx * (W - 24) / 536), i) for n, cx, i in
                (("Name", 14, 0), ("Mann", 160, 2), ("Lage", 230, 3), ("F\u00fchrung", 330, 4), ("Status", 470, 5))]
        for label, cx, _ in cols:
            d.text((cx + 6, y0 + 2), label, font=font(11, True), fill=GOLD)
        visible = max((y1 - y0 - 22) // LINE_H, 1)
        maxfirst = max(len(p.units) - visible, 0)
        first = min(max(p.scroll.get("units") or 0, 0), maxfirst)
        sel = set(p.selected)
        for i, row in enumerate(p.units[first:first + visible]):
            yy = y0 + 20 + i * LINE_H
            bid = row[6] if len(row) > 6 else None
            if bid in sel:
                d.rectangle((13, yy - 1, W - 22, yy + LINE_H - 2), fill=SELECT)
            for _, cx, idx in cols:
                val = str(row[idx]) if idx < len(row) else ""
                col = BLUE if idx == 4 and val == "KI" else (ORANGE if idx == 5 and val else TEXT)
                d.text((cx + 6, yy), val[:max(22, int(22 * W / 560))], font=font(11), fill=col)
            if bid:
                pt.hots.append(Hot(13, yy - 1, W - 22, yy + LINE_H - 2, ("unit", bid)))
        bar = (W - 21, y0 + 20, W - 15, y1 - 2)
        pt.scrollbar(W - 21, y0 + 20, y1 - y0 - 22, len(p.units), visible, first)
        pt.hots.append(Hot(12, y0 + 20, W - 12, y1, ("scroll", "units"), total=len(p.units), visible=visible,
                           first=first, bar=bar))

    # Chat-Zeile (immer sichtbar)
    cy = H - 30
    d.text((12, cy + 3), "Befehl an", font=font(11), fill=MUTED)
    tx = pt.button(68, cy, f"{p.chat_target}    ", ("chat_target",))
    d.polygon([(tx - 20, cy + 8), (tx - 12, cy + 8), (tx - 16, cy + 13)], fill=TEXT)   # Pfeil der Auswahl
    chat_focus = p.focus == "chat"
    d.rectangle((tx, cy, W - 86, cy + 20), fill=PANEL, outline=GOLD if chat_focus else EDGE, width=2 if chat_focus else 1)
    pt.hots.append(Hot(tx, cy, W - 86, cy + 20, ("chat_focus",)))
    f11 = font(11)
    if p.chat_text or chat_focus:
        caret = min(max(p.caret if chat_focus else len(p.chat_text), 0), len(p.chat_text))
        start = 0                                   # langen Text links abschneiden, Cursor sichtbar halten
        while f11.getlength(p.chat_text[start:caret]) > (W - 86 - tx - 16):
            start += 1
        shown = p.chat_text[start:]
        d.text((tx + 6, cy + 3), shown, font=f11, fill=TEXT)
        if chat_focus:
            cx = tx + 6 + int(f11.getlength(p.chat_text[start:caret]))
            d.line((cx, cy + 4, cx, cy + 16), fill=GOLD, width=2)
    else:
        d.text((tx + 6, cy + 3), "Befehl eingeben \u2026 (klicken)", font=f11, fill=MUTED)
    pt.button(W - 80, cy, "Senden", ("send",), enabled=p.running and bool(p.chat_text.strip()))

    # Griff zum Groesse-Aendern (unten rechts)
    for k in (4, 8, 12):
        d.line((W - 2 - k, H - 3, W - 3, H - 2 - k), fill=(150, 130, 80, 255), width=1)
    pt.hots.append(Hot(W - 15, H - 15, W, H, ("grip_size",)))

    if p.menu_open:
        _menu(pt)
    # Menue-Bereiche zuerst pruefen (liegen oben)
    # Menue-Knoepfe vor der Menueflaeche (m_none), diese vor allem darunter
    pt.hots.sort(key=lambda h: 1 if h.action == ("m_none",) else (0 if h.action[0].startswith("m_") else 2))
    return pt.img, pt.hots


def _review_view(pt: Painter, top: int, bottom_y: int) -> None:
    """Pruefen & verbessern im Overlay: Analyse (lesen), Vorschlag (bearbeitbar), Uebernehmen/Neu/Schliessen -
    dieselben Felder wie das Pruefen-Fenster des Steuerpults."""
    p, d = pt.p, pt.d
    r = p.review
    d.text((12, top), f"Pr\u00fcfen & verbessern \u2013 {r.get('title', '')}", font=font(12, True), fill=GOLD)
    st = r.get("status", "")
    d.text((12, top + 17), st[:95], font=font(10), fill=GREEN if r.get("done") else MUTED)
    mid = top + 34 + (bottom_y - top - 34 - 28) // 2
    pt.textbox("rev_ana", (12, top + 34, W - 12, mid - 2), r.get("analysis", ""), f=font(11), fsize=11)
    pt.textbox("rev_sug", (12, mid + 2, W - 12, bottom_y - 28), r.get("proposal", ""),
               title="Vorschlag (bearbeitbar)", f=font(11), fsize=11)
    x = pt.button(12, bottom_y - 24, "\u00dcbernehmen", ("rev_apply",), enabled=bool(r.get("done")), accent=True)
    x = pt.button(x, bottom_y - 24, "Alles neu pr\u00fcfen", ("rev_again",))
    pt.button(x, bottom_y - 24, "Schlie\u00dfen", ("rev_close",))


def _side_icon(d, x: int, y: int, side: str, on: bool, s: int = 16) -> None:
    """Symbol: eigene Armee = Fahne (blau), Gegner = gekreuzte Saebel (rot); aus = grau."""
    if side == "player":
        col = BLUE if on else (110, 112, 120, 255)
        d.line((x + s * 0.2, y, x + s * 0.2, y + s), fill=col, width=2)                     # Fahnenstange
        d.polygon([(x + s * 0.25, y + 1), (x + s * 0.95, y + s * 0.2), (x + s * 0.25, y + s * 0.55)], fill=col)
    else:
        col = (235, 95, 80, 255) if on else (110, 112, 120, 255)
        d.line((x + 1, y + s - 1, x + s - 1, y + 1), fill=col, width=2)                      # Klingen
        d.line((x + 1, y + 1, x + s - 1, y + s - 1), fill=col, width=2)
        d.line((x + s * 0.05, y + s * 0.55, x + s * 0.45, y + s * 0.95), fill=col, width=2)  # Handschutz
        d.line((x + s * 0.55, y + s * 0.95, x + s * 0.95, y + s * 0.55), fill=col, width=2)


def _toggle(pt: Painter, x: int, y: int, side: str, on: bool) -> int:
    """Zweistufiger Schalter mit Symbol: KI fuehrt diese Seite ein/aus."""
    w = 32
    pressed = pt.p.pressed == ("ctl", side)
    fill = (90, 80, 50, 255) if pressed else ((60, 55, 40, 255) if on else BUTTON_OFF)
    pt.d.rounded_rectangle((x, y, x + w, y + 20), 4, fill=fill, outline=GOLD if on else EDGE, width=2 if on else 1)
    _side_icon(pt.d, x + 9, y + 2, side, on, 16)
    if not on:                                               # aus: schraeger Strich
        pt.d.line((x + 6, y + 17, x + w - 6, y + 3), fill=(200, 200, 200, 200), width=1)
    pt.hots.append(Hot(x, y, x + w, y + 20, ("ctl", side)))
    return x + w + 6


def _menu(pt: Painter) -> None:
    """Einstellungen (wie oben im Fenster): KI fuehrt, Modell, Denktiefe, Planabstand, Gruppenmanagement."""
    p, d = pt.p, pt.d
    x0, y0, x1, y1 = W - 330, 32, W - 10, 226
    d.rounded_rectangle((x0, y0, x1, y1), 6, fill=(26, 28, 36, 250), outline=GOLD)
    pt.hots.append(Hot(x0, y0, x1, y1, ("m_none",)))           # Klicks ins Menue nicht durchreichen
    y = y0 + 8
    d.text((x0 + 10, y + 3), "KI f\u00fchrt", font=font(11, True), fill=GOLD)
    x = x0 + 90
    for label, val in CONTROLS:
        x = pt.button(x, y, label, ("m_control", val), accent=p.control_val == val)
    y += 28
    d.text((x0 + 10, y + 3), "Feldherr", font=font(11, True), fill=GOLD)
    pt.button(x0 + 90, y, f"{p.model}  \u203a  n\u00e4chstes", ("m_model",))
    y += 28
    d.text((x0 + 10, y + 3), "Denktiefe", font=font(11, True), fill=GOLD)
    x = x0 + 90
    for e in EFFORTS:
        x = pt.button(x, y, e, ("m_effort", e), accent=p.effort == e)
    y += 28
    pt.button(x0 + 10, y, "Eigenes Gruppenmanagement: " + ("ein" if p.own_groups else "aus"), ("m_groups",),
              accent=p.own_groups)
    y += 28
    x = pt.button(x0 + 10, y, "Abstand Artillerie: " + ("ein" if p.zone_on else "aus"), ("m_zone_toggle",),
                  accent=p.zone_on)
    x = pt.button(x, y, "\u2212", ("m_zone", -5))
    d.text((x + 2, y + 3), f"{p.zone_m} m", font=font(11), fill=TEXT if p.zone_on else MUTED)
    pt.button(x + 40, y, "+", ("m_zone", 5))
    y += 28
    d.text((x0 + 10, y + 2), "Modus gilt ab der n\u00e4chsten Schlacht", font=font(10), fill=MUTED)
    pt.button(x1 - 70, y - 2, "Schlie\u00dfen", ("m_close_menu",))


def scroll_to(hot: Hot, y: int) -> int:
    """Klick/Ziehen am Scrollbalken -> erste sichtbare Zeile."""
    if not hot.bar or hot.total <= hot.visible:
        return 0
    _, by0, _, by1 = hot.bar
    frac = min(max((y - by0) / max(by1 - by0, 1), 0.0), 1.0)
    return int(round(frac * (hot.total - hot.visible)))


def grips(hots: list[Hot]) -> tuple[tuple, tuple]:
    """Greiffeld (Verschieben) und Griff (Groesse) aus den Bereichen des Bilds - fuer den Kopf der Bilddatei."""
    def r(action):
        h = next((h for h in hots if h.action == action), None)
        return (h.x0, h.y0, h.x1, h.y1) if h else (0, 0, 0, 0)
    return r(("grip_move",)), r(("grip_size",))


def write_panel(img: Image.Image | None, path: Path, seq: int, expanded: bool, keyboard: bool = False,
                pos: tuple[int, int] = (-1, -1), hots: list[Hot] | None = None) -> None:
    """Bild fuer die DLL schreiben (atomar). Zugeklappt: nur Kopf mit flags=0, keine Pixel.
    keyboard=True (flags bit 1): ein Feld hat den Fokus - die DLL leitet die Tastatur ins Overlay um.
    Aufgeklappt mit flags bit 2: erweiterter Kopf mit Position (-1 = Standard) und den Griffen."""
    w, h = (img.size if img is not None and expanded else (0, 0))
    flags = (1 if expanded else 0) | (2 if expanded and keyboard else 0) | (4 if expanded else 0)
    head = struct.pack("<4sIIII", b"EBOV", w, h, seq, flags)
    if expanded:
        mv, sz = grips(hots or [])
        head += struct.pack("<10i", int(pos[0]), int(pos[1]), *mv, *sz)
    data = head + (img.tobytes("raw", "BGRA") if img is not None and expanded else b"")
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:          # DLL liest gerade
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


def caret_move(text: str, caret: int, hot: "Hot | None", how: str) -> int:
    """Cursor in umbrochenen Zeilen bewegen: how = up, down, home, end (Breite/Schrift aus dem Feld des Bilds)."""
    caret = min(max(caret, 0), len(text))
    if hot is None:
        return {"up": 0, "home": 0}.get(how, len(text))
    f = font(hot.fsize)
    lines = _wrap_off(text, f, hot.x1 - hot.x0 - 20)
    i = max((k for k, (_, st) in enumerate(lines) if st <= caret), default=0)
    line, st = lines[i]
    if how == "home":
        return st
    if how == "end":
        wrapped = i + 1 < len(lines) and lines[i + 1][1] == st + len(line)
        return st + (len(line.rstrip(" ")) if wrapped else len(line))
    px = f.getlength(line[:caret - st])
    j = i + (-1 if how == "up" else 1)
    if j < 0:
        return 0
    if j >= len(lines):
        return len(text)
    line, st = lines[j]
    col = 0
    while col < len(line) and f.getlength(line[:col + 1]) - f.getlength(line[col]) / 2 < px:
        col += 1
    return st + col


class InputReader:
    """Liest neue Zeilen aus overlay_input.txt (die DLL schreibt fort; wird die Datei kuerzer, neu ab Anfang)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.pos = path.stat().st_size          # alte Eingaben beim Start ignorieren
        except OSError:
            self.pos = 0

    def read(self) -> list[tuple]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.pos:
            self.pos = 0
        if size == self.pos:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.pos)
            data = f.read(size - self.pos)
        end = data.rfind(b"\n")
        if end < 0:
            return []
        self.pos += end + 1
        out = []
        for line in data[:end].decode("ascii", "ignore").splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[1] in ("down", "up", "drag", "wheel", "key", "char", "moved", "resize"):
                try:
                    out.append((parts[1], int(parts[2]), int(parts[3]), int(parts[4]) if len(parts) > 4 else 0))
                except ValueError:
                    pass
        return out
