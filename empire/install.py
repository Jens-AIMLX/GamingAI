"""
install.py - installiert die Empire Battle AI in den Spielordner (oder entfernt sie wieder).

    python install.py                      # Spielordner automatisch ueber Steam finden
    python install.py --game-dir "D:\\SteamLibrary\\steamapps\\common\\Empire Total War"
    python install.py --uninstall

Was passiert:
  1. empire_bridge.dll bauen, falls noch nicht vorhanden (dll/build.bat, braucht MSVC)
  2. empire_bridge.dll           -> <Spiel>/empire_bridge.dll
  3. lua/ai_bridge.battle_script -> <Spiel>/data/ai_bridge.battle_script
  4. <Spiel>/data/battle_scripted.lua: Sicherung .orig anlegen, Lade-Block anhaengen (zwischen Markern)
  5. <Spiel>/data/campaigns/*/scripting.lua: Sicherung .orig, Kampagnen-Probe anhaengen (lua/campaign_addon.lua)
  6. <Spiel>/ai_bridge/config.lua anlegen (falls nicht vorhanden)
  7. python/local_settings.json mit dem Spielordner schreiben (fuer commander.py / ui.py)
--uninstall stellt battle_scripted.lua und die Kampagnenskripte wieder her und entfernt DLL und Battle-Skript.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXPECTED_SHA256 = "9b130e708d01a60010df286d2e39867b8fc4f0814d544fcbfe3c1e352df04120"  # Steam-Build 11084729
APPID = "10500"
BEGIN, END = "-- [empire_bridge] BEGIN", "-- [empire_bridge] END"


def steam_libraries() -> list[Path]:
    roots: list[Path] = []
    try:
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for name in ("SteamPath", "InstallPath"):
                        try:
                            roots.append(Path(winreg.QueryValueEx(k, name)[0]))
                        except OSError:
                            pass
            except OSError:
                pass
    except ImportError:
        pass
    libs: list[Path] = []
    for r in roots:
        vdf = r / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            libs += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="ignore"))]
        libs.append(r)
    seen, out = set(), []
    for l in libs:
        if str(l).lower() not in seen:
            seen.add(str(l).lower())
            out.append(l)
    return out


def find_game() -> Path | None:
    for lib in steam_libraries():
        p = lib / "steamapps" / "common" / "Empire Total War"
        if (p / "Empire.exe").exists():
            return p
    return None


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def strip_addon(text: str) -> str:
    return re.sub(r"\n?" + re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "\n", text, flags=re.S).rstrip() + "\n"


def install(game: Path, force: bool) -> None:
    exe = game / "Empire.exe"
    digest = sha256(exe)
    if digest != EXPECTED_SHA256:
        print(f"WARNUNG: Empire.exe hat einen anderen Stand ({digest[:12]}...). Die DLL prueft die Bytes selbst und "
              "schaltet sich bei einer unbekannten Version ab - das Spiel laeuft dann normal ohne KI.")
        if not force:
            sys.exit("Abbruch. Mit --force trotzdem installieren.")
    dll = HERE / "dll" / "empire_bridge.dll"
    if not dll.exists():
        print("empire_bridge.dll fehlt - baue sie mit dll/build.bat ...")
        subprocess.run(["cmd", "/c", str(HERE / "dll" / "build.bat")], check=True)
    try:
        shutil.copy2(dll, game / "empire_bridge.dll")
        print(f"  DLL           -> {game / 'empire_bridge.dll'}")
    except PermissionError:
        print(f"  HINWEIS: {game / 'empire_bridge.dll'} ist gesperrt (Empire laeuft) und wurde NICHT ersetzt. "
              "Nach dem Beenden von Empire `python install.py` erneut ausfuehren; alles andere ist eingerichtet.")
    shutil.copy2(HERE / "lua" / "ai_bridge.battle_script", game / "data" / "ai_bridge.battle_script")
    print(f"  Battle-Skript -> {game / 'data' / 'ai_bridge.battle_script'}")

    bs = game / "data" / "battle_scripted.lua"
    orig = bs.with_name("battle_scripted.lua.orig")
    if not orig.exists():
        shutil.copy2(bs, orig)
        print(f"  Sicherung     -> {orig}")
    # Immer vom Original ausgehen: entfernt auch aeltere, von Hand eingebaute Lade-Bloecke ohne Marker
    text = strip_addon(orig.read_text(encoding="latin-1"))
    addon = (HERE / "lua" / "battle_scripted_addon.lua").read_text(encoding="utf-8")
    bs.write_text(text + addon, encoding="latin-1")
    print(f"  Lade-Block    -> {bs}")
    install_campaign(game)

    cfg_dir = game / "ai_bridge"
    cfg_dir.mkdir(exist_ok=True)
    cfg = cfg_dir / "config.lua"
    if not cfg.exists():
        cfg.write_text('return {\n\tcontrol = "enemy",\n\tplayer_alliance = "auto",\n\ttick_ms = 1000,\n}\n', encoding="utf-8")
        print(f"  Konfiguration -> {cfg}")
    (HERE / "python" / "local_settings.json").write_text(json.dumps({"game_dir": str(game)}, indent=2), encoding="utf-8")
    print("Fertig. Steuerpult: start_ui.bat  |  Feldherr: python python/commander.py")


def campaign_scripts(game: Path) -> list[Path]:
    return sorted((game / "data" / "campaigns").glob("*/scripting.lua"))


def install_campaign(game: Path) -> None:
    """Kampagnen-Probe (lua/campaign_addon.lua, nur lesen) an jedes data/campaigns/*/scripting.lua anhaengen -
    jeweils vom Original (.orig) aus, damit der Block nie doppelt drin steht."""
    addon = (HERE / "lua" / "campaign_addon.lua").read_text(encoding="utf-8")
    capture = (HERE / "lua" / "campaign_capture.lua").read_text(encoding="utf-8")   # Datenerfassung je Zug
    addon = addon.replace(END, capture.rstrip() + "\n" + END)
    for sc in campaign_scripts(game):
        orig = sc.with_name("scripting.lua.orig")
        if not orig.exists():
            shutil.copy2(sc, orig)
        sc.write_text(strip_addon(orig.read_text(encoding="latin-1")) + addon, encoding="latin-1")
        print(f"  Kampagne      -> {sc.relative_to(game)}")


def uninstall(game: Path) -> None:
    for sc in campaign_scripts(game):
        orig = sc.with_name("scripting.lua.orig")
        if orig.exists():
            shutil.copy2(orig, sc)
            print(f"  wiederhergestellt: {sc}")
        else:
            sc.write_text(strip_addon(sc.read_text(encoding="latin-1")), encoding="latin-1")
    bs = game / "data" / "battle_scripted.lua"
    orig = bs.with_name("battle_scripted.lua.orig")
    if orig.exists():
        shutil.copy2(orig, bs)
        print(f"  wiederhergestellt: {bs}")
    elif bs.exists():
        bs.write_text(strip_addon(bs.read_text(encoding="latin-1")), encoding="latin-1")
        print(f"  Lade-Block entfernt: {bs}")
    for p in (game / "empire_bridge.dll", game / "data" / "ai_bridge.battle_script"):
        try:
            p.unlink()
            print(f"  entfernt: {p}")
        except FileNotFoundError:
            pass
        except PermissionError:
            print(f"  {p} ist in Benutzung - bitte Empire beenden und erneut ausfuehren")
    print("Deinstalliert. Der Ordner ai_bridge/ (Konfiguration, Logs) bleibt erhalten.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Empire Battle AI installieren/entfernen")
    ap.add_argument("--game-dir", help="Ordner mit Empire.exe (Standard: ueber Steam suchen)")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--force", action="store_true", help="auch bei unbekannter Empire.exe-Version installieren")
    args = ap.parse_args()
    game = Path(args.game_dir) if args.game_dir else find_game()
    if not game or not (game / "Empire.exe").exists():
        sys.exit("Empire.exe nicht gefunden. Bitte --game-dir angeben.")
    print(f"Spielordner: {game}")
    uninstall(game) if args.uninstall else install(game, args.force)


if __name__ == "__main__":
    main()
