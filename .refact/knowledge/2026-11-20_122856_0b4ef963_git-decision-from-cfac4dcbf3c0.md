---
id: 9e7ec39e-1897-4333-beec-cb9db98e8bae
title: Git decision from cfac4dcbf3c0
tags:
- decision
- git
created: 2026-11-20
updated: 2026-11-20
filenames:
- empire/dll/build.bat
- empire/dll/empire_bridge.c
- empire/dll/overlay.c
- empire/python/empire_bridge.py
- empire/python/ui.py
links: []
kind: decision
status: proposed
superseded_by: null
deprecated_at: null
review_after: 2026-11-20
source_chat_id: null
created_at: 2026-11-20T11:28:56.869427100+00:00
summary: null
description: null
entities: []
related_files: []
related_entities: []
content_hash: b7eec1fbf3255366fd1ba1d21c6b5fc801142dd9d745cfd5b60a2c108cc11c9d
source_tool: buddy_memory_lifecycle:git
source_confidence: 0.8199999928474426
source_trajectory_id: null
source_message_range: null
source_commit: cfac4dcbf3c0bb7b8497a48362c3bdf7c291311c
topic: null
last_used_at: null
use_count: 0
last_injected_at: null
dismissed_count: 0
source_content_hash: b7eec1fbf3255366fd1ba1d21c6b5fc801142dd9d745cfd5b60a2c108cc11c9d
review_needed: true
occurrences: 0
---

Git decision from cfac4dcbf3c0

Source commit: cfac4dcbf3c0
Paths: empire/dll/build.bat, empire/dll/empire_bridge.c, empire/dll/overlay.c, empire/python/empire_bridge.py, empire/python/ui.py
Summary: Overlay Stufe 1: Feld "Overlay aktiv" im Spielbild (auch Vollbild) - dll/overlay.c: Hilfsgeraet liefert die Adressen von Present/Reset in d3d9.dll; beide FUNKTIONEN werden per Hot-Patch umgeleitet (5 Fuellbytes + "mov edi,edi"). Methodentabellen umzubiegen reicht nicht: sie liegen unter Windows 10/11 pro Geraet im Spei