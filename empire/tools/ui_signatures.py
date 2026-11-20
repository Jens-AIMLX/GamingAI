"""UI-Skripte des Spiels (.luac in den Packs) auslesen und alle Aufrufe eines Moduls (Standard: CampaignUI) mit
Beispiel-Argumenten auflisten - so erfahren wir die Signaturen der Funktionen, die das Kampagnen-Lua anbietet.

    python tools/ui_signatures.py "D:\\SteamLibrary\\steamapps\\common\\Empire Total War" > docs/campaign_ui_signatures.txt

Liest nur; spaetere Packs ueberschreiben fruehere (ui.pack < patch.pack < ... < patch5.pack).
"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import pack  # noqa: E402
from luadis import module_calls  # noqa: E402

PACKS = ("ui.pack", "patch.pack", "patch2.pack", "patch3.pack", "patch4.pack", "patch5.pack")


def ui_scripts(game: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for pk in PACKS:
        path = game / "data" / pk
        if not path.exists():
            continue
        _, _, ents = pack.index(str(path))
        with open(path, "rb") as f:
            for name, pos, size in ents:
                if name.lower().endswith(".luac") and name.lower().startswith("ui\\"):
                    f.seek(pos)
                    files[name.lower()] = f.read(size)
    return files


def main() -> None:
    game = Path(sys.argv[1])
    module = sys.argv[2] if len(sys.argv) > 2 else "CampaignUI"
    ex: dict[str, list] = defaultdict(list)
    files = ui_scripts(game)
    for name, data in sorted(files.items()):
        try:
            for fn, expr, _, _, _ in module_calls(data, module):
                if expr not in [e for e, _ in ex[fn]]:
                    ex[fn].append((expr, name.split("\\")[-1].replace(".luac", "")))
        except Exception as e:  # einzelne Datei unlesbar: weiter
            print(f"# Fehler in {name}: {e}")
    print(f"# {module}: {len(ex)} Funktionen mit Aufrufen in {len(files)} UI-Skripten")
    for fn in sorted(ex):
        print(f"{fn}  [{len(ex[fn])} Varianten]")
        for expr, src in ex[fn][:3]:
            print(f"    {expr[:200]}   <- {src}")


if __name__ == "__main__":
    main()
