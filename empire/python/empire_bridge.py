"""
empire_bridge.py - Python-Seite der Empire AI Bridge (Datei-Bruecke).

Liest das Lagebild (state.json), das das Battle-Skript im Spiel jeden Takt schreibt,
und schreibt Befehle (orders.lua) als Lua-Literal. Protokoll: ../PROTOCOL.md

    from empire_bridge import EmpireBridge, Order
    br = EmpireBridge()                      # Standard: <Spielordner>/ai_bridge (siehe default_dir)
    st = br.wait_for_state(lambda s: s["phase"] == "Deployed")
    br.send([Order("2.1.3", "goto_location", [{"x": 100, "z": -50}])])
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

LOCAL_SETTINGS = Path(__file__).resolve().parent / "local_settings.json"


def default_dir() -> Path:
    """Bruecken-Ordner <Spielordner>/ai_bridge. Reihenfolge: EMPIRE_BRIDGE_DIR, EMPIRE_GAME_DIR,
    local_settings.json (von install.py geschrieben)."""
    if os.environ.get("EMPIRE_BRIDGE_DIR"):
        return Path(os.environ["EMPIRE_BRIDGE_DIR"])
    if os.environ.get("EMPIRE_GAME_DIR"):
        return Path(os.environ["EMPIRE_GAME_DIR"]) / "ai_bridge"
    try:
        game = json.loads(LOCAL_SETTINGS.read_text(encoding="utf-8"))["game_dir"]
        return Path(game) / "ai_bridge"
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        raise SystemExit("Spielordner unbekannt: bitte zuerst `python install.py` ausfuehren "
                         "oder EMPIRE_GAME_DIR setzen.")


@dataclass
class Order:
    unit: str
    cmd: str
    args: list[Any] = field(default_factory=list)


def to_lua(v: Any) -> str:
    """Serialisiert reine Daten (dict/list/str/Zahl/bool/None) als Lua-Literal."""
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            raise ValueError("NaN/Inf nicht erlaubt")
        return repr(v)
    if isinstance(v, str):
        return '"' + "".join(_lua_char(c) for c in v) + '"'
    if isinstance(v, Order):
        return to_lua({"unit": v.unit, "cmd": v.cmd, "args": list(v.args)})
    if isinstance(v, (list, tuple)):
        return "{" + ", ".join(to_lua(x) for x in v) + "}"
    if isinstance(v, dict):
        parts = []
        for k, x in v.items():
            if not isinstance(k, str) or not k.isidentifier():
                raise ValueError(f"Schluessel muss ein Lua-Bezeichner sein: {k!r}")
            parts.append(f"{k} = {to_lua(x)}")
        return "{" + ", ".join(parts) + "}"
    raise TypeError(f"nicht serialisierbar: {type(v).__name__}")


def _lua_char(c: str) -> str:
    if c == "\\":
        return "\\\\"
    if c == '"':
        return '\\"'
    if c == "\n":
        return "\\n"
    if ord(c) < 32 or ord(c) == 127:
        return f"\\{ord(c)}"
    if ord(c) > 127:  # Lua 5.1: Bytes als Dezimal-Escapes
        return "".join(f"\\{b}" for b in c.encode("utf-8"))
    return c


class EmpireBridge:
    def __init__(self, directory: str | os.PathLike | None = None):
        self.dir = Path(directory) if directory else default_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self._seq = self._initial_seq()

    # ---------- Lagebild ----------
    def read_state(self) -> dict | None:
        """Liest state.json; None, wenn (noch) nicht vorhanden oder gerade halb geschrieben."""
        path = self.dir / "state.json"
        for _ in range(5):
            try:
                text = path.read_text(encoding="utf-8")
                return json.loads(text)
            except FileNotFoundError:
                return None
            except (json.JSONDecodeError, PermissionError, UnicodeDecodeError):
                time.sleep(0.05)
        return None

    def wait_for_state(self, pred: Callable[[dict], bool] = lambda s: True,
                       timeout: float | None = None, newer_than: int | None = None,
                       poll: float = 0.2) -> dict | None:
        """Wartet (Echtzeit) auf ein Lagebild, das pred erfuellt (und optional seq > newer_than)."""
        t0 = time.monotonic()
        while timeout is None or time.monotonic() - t0 < timeout:
            st = self.read_state()
            if st and (newer_than is None or st.get("seq", 0) > newer_than) and pred(st):
                return st
            time.sleep(poll)
        return None

    def wait_game_time(self, seconds: float, timeout: float | None = None) -> dict | None:
        """Wartet, bis seconds Spielzeit vergangen sind (beruecksichtigt Pause und Zeitraffer)."""
        st = self.wait_for_state(timeout=timeout)
        if st is None:
            return None
        target = st["game_time"] + seconds
        return self.wait_for_state(lambda s: s["game_time"] >= target, timeout=timeout)

    # ---------- Befehle ----------
    def _initial_seq(self) -> int:
        seq = 0
        st = self.read_state()
        if st:
            seq = max(seq, int(st.get("last_order_seq") or 0))
        try:
            text = (self.dir / "orders.lua").read_text(encoding="utf-8")
            import re
            m = re.search(r"seq\s*=\s*(\d+)", text)
            if m:
                seq = max(seq, int(m.group(1)))
        except FileNotFoundError:
            pass
        return seq

    def send(self, orders: Iterable[Order | dict]) -> int:
        """Schreibt einen Befehlssatz; das Spiel fuehrt ihn beim naechsten Takt einmal aus. Gibt seq zurueck."""
        self._seq += 1
        lst = [o if isinstance(o, Order) else Order(**o) for o in orders]
        text = "return " + to_lua({"seq": self._seq, "orders": lst}) + "\n"
        tmp = self.dir / "orders.lua.tmp"
        tmp.write_text(text, encoding="utf-8")
        for _ in range(50):
            try:
                os.replace(tmp, self.dir / "orders.lua")
                break
            except PermissionError:  # Spiel liest gerade
                time.sleep(0.02)
        else:
            raise RuntimeError("orders.lua konnte nicht ersetzt werden")
        return self._seq

    def wait_ack(self, seq: int, timeout: float = 30) -> list[dict] | None:
        """Wartet, bis das Spiel seq ausgefuehrt hat; liefert die Ergebnisse dieses Befehlssatzes."""
        st = self.wait_for_state(lambda s: (s.get("last_order_seq") or 0) >= seq, timeout=timeout)
        if st is None:
            return None
        return [r for r in st.get("order_results") or [] if r.get("seq") == seq]

    def write_config(self, control: str = "enemy", player_alliance: str | int = "auto", tick_ms: int = 1000,
                     own_groups: bool = False) -> None:
        """Schreibt config.lua; wirkt ab der naechsten Schlacht. own_groups: KI uebernimmt auch Einheiten, die in
        Gruppen des Spielers stecken (Versuch ueber add_group)."""
        assert control in ("enemy", "player", "both", "none")
        text = "return " + to_lua({"control": control, "player_alliance": player_alliance, "tick_ms": tick_ms,
                                   "own_groups": bool(own_groups)}) + "\n"
        (self.dir / "config.lua").write_text(text, encoding="utf-8")


# ---------- kleine Geometrie-Helfer fuer Lagebilder ----------
def units(state: dict, *, alliance: int | None = None, controlled: bool | None = None,
          alive: bool = True) -> list[dict]:
    out = []
    for al in state.get("alliances", []):
        if alliance is not None and al["index"] != alliance:
            continue
        for u in al.get("units", []):
            if controlled is not None and bool(u.get("controlled")) != controlled:
                continue
            if alive and (not u.get("men") or u.get("routing") or u.get("leaving")):
                continue
            out.append(u)
    return out


def centroid(us: list[dict]) -> tuple[float, float] | None:
    if not us:
        return None
    return (sum(u["x"] for u in us) / len(us), sum(u["z"] for u in us) / len(us))


def unit_by_id(state: dict, uid: str) -> dict | None:
    for al in state.get("alliances", []):
        for u in al.get("units", []):
            if u["id"] == uid:
                return u
    return None
