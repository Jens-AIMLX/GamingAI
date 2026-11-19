"""Empire General - ein Sprachmodell als Feldherr in Total War: EMPIRE.

Architektur nach rome-battle-ai (https://github.com/Cegli865/rome-battle-ai, MIT, (c) 2026 Cegli865):
Lagebericht -> Plan (festes Schema) vom Sprachmodell -> Executor mit Reflexen -> Spielbefehle.
Hier an Empire angepasst und ueber die Datei-Bruecke (empire_bridge.py) statt eines Speicher-Hooks.
"""
