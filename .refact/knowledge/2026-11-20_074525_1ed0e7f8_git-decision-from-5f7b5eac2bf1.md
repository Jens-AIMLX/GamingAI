---
id: 8afe1e82-12a5-44fb-8f1a-95e162dc2719
title: Git decision from 5f7b5eac2bf1
tags:
- decision
- git
created: 2026-11-20
updated: 2026-11-20
filenames:
- empire/python/commander.py
- empire/python/empire_general/describe.py
- empire/python/empire_general/general.py
- empire/python/empire_general/plan.py
links: []
kind: decision
status: proposed
superseded_by: null
deprecated_at: null
review_after: 2026-11-20
source_chat_id: null
created_at: 2026-11-20T06:45:25.007835300+00:00
summary: null
description: null
entities: []
related_files: []
related_entities: []
content_hash: 0cde37e0847f78af58aab3b84a4ff28f4ac3b06a1abe8f64cc7ee1a826f1c162
source_tool: buddy_memory_lifecycle:git
source_confidence: 0.8199999928474426
source_trajectory_id: null
source_message_range: null
source_commit: 5f7b5eac2bf1ed3ef48d115ac4267ec9421e196c
topic: null
last_used_at: null
use_count: 0
last_injected_at: null
dismissed_count: 0
source_content_hash: 0cde37e0847f78af58aab3b84a4ff28f4ac3b06a1abe8f64cc7ee1a826f1c162
review_needed: true
occurrences: 0
---

Git decision from 5f7b5eac2bf1

Source commit: 5f7b5eac2bf1
Paths: empire/python/commander.py, empire/python/empire_general/describe.py, empire/python/empire_general/general.py, empire/python/empire_general/plan.py
Summary: Einheiten per Chat-Befehl uebergeben und zuruecknehmen - Plan-Schema: give_to_player / take_from_player; der Feldherr fuellt sie nur bei ausdruecklichem Befehl ("gib mir Kavallerie 1", "die Moerser fuehre ich selbst", "nimm die Generaele wieder", "du uebernimmst wieder alles") und loest Beschreibungen in Einheiten auf