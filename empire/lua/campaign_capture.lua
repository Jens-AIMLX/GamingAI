-- Kampagnen-Datenerfassung (Stufe 1): zu Beginn jedes eigenen Zugs (FactionTurnStart, FactionIsLocal) und nach jeder
-- Aktion im eigenen Zug (Klicks, Fenster, Bau/Rekrutierung, Auswahl, Bewegung, Schlacht - 2 s Ruhe abwarten, Takt ueber
-- den Spiel-Timer add_time_trigger) den Lagebericht nach ai_bridge/campaign_state.json - Staat, Statthalterschaften, Regionen mit Ordnung, alle Gebaeudeslots mit
-- Zustand, Forschung, eigene Armeen/Flotten/Agenten, Diplomatie, Armeen der Kriegsgegner (auch im Nebel des Krieges;
-- der Bericht entscheidet, was er zeigt) und die Ereignisse seit dem letzten eigenen Zug.
-- Nur Lesefunktionen, nur in der Form der UI-Skripte des Spiels (docs/campaign_notes.md). Abschaltbar ueber
-- ai_bridge/campaign_capture.off. Jeder Schritt steht vorher als BEGINN in campaign_capture.progress; ein Schritt, bei
-- dem das Spiel abstuerzte (BEGINN ohne ENDE), kommt nach campaign_capture.skip und wird kuenftig ausgelassen.
do
	local P = "ai_bridge\\"
	local ok_es, es = pcall(require, "EpisodicScripting")

	local function exists(name)
		local f = io.open(P .. name, "r")
		if f then f:close(); return true end
		return false
	end
	-- Neue Sitzung (Kampagne geladen/Spiel neu gestartet): Bericht der vorigen Sitzung verwerfen - Steuerpult und Overlay
	-- zeigen nie alte Daten, sondern bleiben leer bis zur ersten Erfassung (Zugbeginn oder erster Klick)
	pcall(os.remove, P .. "campaign_state.json")

	-- Uhr fuer Abstaende: os.clock() (unter Windows Sekunden seit Prozessstart, fein aufgeloest). NICHT os.time():
	-- Empire rechnet Lua-Zahlen mit einfacher Genauigkeit, os.time() (~1,8 Mrd.) springt dort in 128-s-Schritten -
	-- jede Pruefung "seit 2 s Ruhe" o. ae. war damit kaputt (Diagnose 2026-11-20 23:57: 17 s Abstand = "+0s").
	local function now() return os.clock() end

	-- Diagnose Ladeablauf (voruebergehend): welche Spielereignisse kommen nach dem Laden ohne Eingabe? Jedes Ereignis
	-- aus data.events mit Uhrzeit nach ai_bridge/campaign_events.log - nur die ersten 300 s nach Skriptstart, hoechstens
	-- 3000 Zeilen. Reines Lua (Datei schreiben), keine Engine-Aufrufe.
	local diag_t0, diag_n = now(), 0
	local function diag(msg)
		if diag_n >= 3000 or now() - diag_t0 > 300 then return end
		diag_n = diag_n + 1
		pcall(function()
			local f = io.open(P .. "campaign_events.log", diag_n == 1 and "w" or "a")
			f:write(os.date("%H:%M:%S"), string.format("  +%3ds  ", now() - diag_t0), msg, "\n")
			f:close()
		end)
	end
	diag("Skript geladen")
	pcall(function()
		local ev = require("data.events")
		local es0 = require("EpisodicScripting")
		for name in pairs(ev) do
			if type(name) == "string" then
				pcall(es0.AddEventCallBack, name, function() diag(name) end)
			end
		end
	end)

	-- Fehler der Erfassung sichtbar machen: ai_bridge/campaign_capture.error (neueste zuerst ueberschrieben) und
	-- empire_bridge_log.txt
	local function log_error(msg)
		pcall(function()
			local stamp = os.date("%Y-%m-%d %H:%M:%S")
			local f = io.open(P .. "campaign_capture.error", "a")
			f:write(stamp, "  ", tostring(msg), "\n")
			f:close()
			local g = io.open("empire_bridge_log.txt", "a")
			g:write(stamp, "  [lua] Kampagnen-Erfassung: ", tostring(msg), "\n")
			g:close()
		end)
	end

	-- ---------- JSON ----------
	local ESC = { ['"'] = '\\"', ['\\'] = '\\\\', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t' }
	local function jstr(s)
		return '"' .. s:gsub('[%c"\\]', function(c) return ESC[c] or string.format("\\u%04x", c:byte()) end) .. '"'
	end
	local function enc(v, out)
		local t = type(v)
		if t == "string" then out[#out + 1] = jstr(v)
		elseif t == "number" then
			-- im Spiel-Lua fehlt math.huge (wie im Battle-Skript): NaN/unendlich ueber Grenzwerte erkennen
			if v ~= v or v > 1e300 or v < -1e300 then out[#out + 1] = "null"
			else out[#out + 1] = string.format("%.14g", v) end
		elseif t == "boolean" then out[#out + 1] = v and "true" or "false"
		elseif t == "table" then
			local n, count = #v, 0
			for _ in pairs(v) do count = count + 1 end
			if count == n then                      -- Liste (leere Tabellen als [] - der Bericht behandelt [] wie {})
				out[#out + 1] = "["
				for i = 1, n do
					if i > 1 then out[#out + 1] = "," end
					enc(v[i], out)
				end
				out[#out + 1] = "]"
			else
				out[#out + 1] = "{"
				local first = true
				for k, val in pairs(v) do
					local tv = type(val)
					if tv ~= "function" and tv ~= "userdata" then
						if not first then out[#out + 1] = "," end
						first = false
						out[#out + 1] = jstr(tostring(k))
						out[#out + 1] = ":"
						enc(val, out)
					end
				end
				out[#out + 1] = "}"
			end
		else out[#out + 1] = "null" end
	end

	-- ---------- Auswahl einfacher Felder ----------
	local function scalar(v)
		local t = type(v)
		return t == "string" or t == "number" or t == "boolean"
	end
	local function pick(t, keys)
		local r = {}
		if type(t) ~= "table" then return r end
		for _, k in ipairs(keys) do
			if scalar(t[k]) then r[k] = t[k] end
		end
		return r
	end
	local function factors(list)            -- Ordnung/Wohlstand: Liste von Faktoren mit Tooltip "Name: Wert", dazu Total
		local r = { factors = {} }
		if type(list) ~= "table" then return r end
		if scalar(list.Total) then r.total = list.Total end
		for i = 1, #list do
			local e = list[i]
			if type(e) == "table" then
				local name = tostring(e.Tooltip or ""):match("^([^:|]+)") or "?"
				name = (name:gsub("%s+$", ""))
				r.factors[#r.factors + 1] = { name = name, value = e.Total, positive = e.Positive, predicted = e.Predicted }
			end
		end
		return r
	end
	local CHAR_KEYS = { "Name", "AgentType", "Title", "Location", "PosX", "PosY", "Soldiers", "Units", "ActionPoints",
	                    "ActionPointsPerTurn", "IsNaval", "IsMilitary", "Age", "Transitioning" }
	local function character(c)
		local r = pick(c, CHAR_KEYS)
		if type(c) == "table" and type(c.Attributes) == "table" then
			r.PrimaryAttribute = c.Attributes.PrimaryAttributeName
			r.PrimaryLevel = c.Attributes.PrimaryLevel
			local cl = c.Attributes.command_land
			if type(cl) == "table" and scalar(cl.Value) then r.CommandLand = cl.Value end
			local cs = c.Attributes.command_sea
			if type(cs) == "table" and scalar(cs.Value) then r.CommandSea = cs.Value end
		end
		return r
	end
	local function characters(list)
		local r = {}
		if type(list) ~= "table" then return r end
		for i = 1, #list do r[#r + 1] = character(list[i]) end
		return r
	end

	-- ---------- Ereignisse seit dem letzten eigenen Zug ----------
	local EVENTS = { "CampaignBuildingDamaged", "CampaignSettlementAttacked", "CampaignSlotAttacked", "SiegeLifted",
	                 "LandTradeRouteRaided", "SeaTradeRouteRaided", "RegionRiots", "RegionRebels", "RegionIssuesDemands",
	                 "SettlementOccupied", "GarrisonResidenceCaptured", "BuildingCompleted", "ResearchCompleted",
	                 "UnitTrained", "CharacterCompletedBattle", "CharacterCreated", "CharacterPromoted", "MissionIssued",
	                 "MissionSucceeded", "MissionFailed", "MissionNearingExpiry", "FactionGovernmentTypeChanged",
	                 "DuelFought", "AssassinationAttemptSuccess", "SufferAssassinationAttempt", "SabotageAttemptSuccess",
	                 "ArmySabotageAttemptSuccess", "SpyingAttemptSuccess", "SufferSpyingAttempt",
	                 "EspionageAgentApprehended", "TradeRouteEstablished", "TradeLinkEstablished", "SlotOpens",
	                 "VictoryConditionMet", "VictoryConditionFailed", "CampaignArmiesMerge" }
	local OWN_TESTS = { "FactionIsLocal", "CharacterIsLocalCampaign", "SettlementIsLocal", "RegionIsLocal" }
	local events = {}
	local function is_own(context)
		for _, name in ipairs(OWN_TESTS) do
			local fn = conditions and conditions[name]
			if fn then
				local ok, v = pcall(fn, context)
				if ok and v == true then return true end
			end
		end
		return false
	end
	local function on_event(name)
		return function(context)
			pcall(function()
				local e = events[name] or { all = 0, own = 0 }
				events[name] = e
				e.all = e.all + 1
				if is_own(context) then
					e.own = e.own + 1
					if name == "CharacterCompletedBattle" and conditions.CharacterWonBattle then
						local ok, won = pcall(conditions.CharacterWonBattle, context)
						if ok and won == true then e.own_won = (e.own_won or 0) + 1
						elseif ok and won == false then e.own_lost = (e.own_lost or 0) + 1 end
					end
				end
			end)
		end
	end

	-- ---------- Erfassung ----------
	-- kind: "turn" (Beginn des eigenen Zugs), "action" (nach einer Aktion), "request" (Knopf im Steuerpult/Overlay).
	-- Regionen und Gebaeude (teuer: ~950 Slots) nach einer Aktion nur, wenn ihr letzter Stand aelter als HEAVY_EVERY
	-- Sekunden ist; zu Zugbeginn und auf Knopfdruck immer. Der letzte Stand bleibt erhalten (S).
	local HEAVY_EVERY = 20
	local S = nil
	local last_heavy = -1e9
	local capturing = false
	local function capture(kind)
		if capturing or CampaignUI == nil or exists("campaign_capture.off") then return end
		local C = CampaignUI
		local okp, mine = pcall(C.IsPlayersTurn)
		if not (okp and mine) then return end
		capturing = true
		local heavy = kind ~= "action" or now() - last_heavy >= HEAVY_EVERY

		-- Absturzschutz: letzter Lauf bei einem Schritt abgestuerzt -> Schritt kuenftig auslassen
		local skip, open = {}, nil
		pcall(function() for line in io.lines(P .. "campaign_capture.skip") do skip[line] = true end end)
		pcall(function()
			for line in io.lines(P .. "campaign_capture.progress") do
				local b, e = line:match("^BEGINN (.*)$"), line:match("^ENDE (.*)$")
				if b then open = b elseif e and e == open then open = nil end
			end
		end)
		if open and not skip[open] then
			skip[open] = true
			pcall(function() local f = io.open(P .. "campaign_capture.skip", "a"); f:write(open, "\n"); f:close() end)
		end
		local prog = io.open(P .. "campaign_capture.progress", "w")
		if not prog then capturing = false; return end
		local old = S or {}
		S = { version = 2, steps = {}, errors = {}, skipped = {} }
		for _, k in ipairs({ "regions", "buildings", "events", "regions_written" }) do S[k] = old[k] end
		local function step(name, fn)
			if skip[name] then S.skipped[#S.skipped + 1] = name; return end
			prog:write("BEGINN ", name, "\n"); prog:flush()
			local t0 = os.clock()
			local ok, err = pcall(fn)
			if not ok then S.errors[#S.errors + 1] = name .. ": " .. tostring(err) end
			S.steps[#S.steps + 1] = { name = name, ok = ok, seconds = os.clock() - t0 }
			prog:write("ENDE ", name, "\n"); prog:flush()
		end

		local pid, key
		step("meta", function()
			pid, key = C.PlayerFactionId(), C.PlayersFactionKey()
			local year = C.CurrentYear()
			S.meta = { faction = key, turn = C.CurrentTurn(), year = year, season = C.CurrentSeasonString(),
			           players_turn = C.IsPlayersTurn(), written = os.date("%Y-%m-%d %H:%M:%S"), refresh = kind,
			           heavy = heavy }
		end)
		if pid == nil then prog:close(); capturing = false; return end
		step("staat", function()
			S.state = pick(C.InitialiseGovernmentDetails(), { "Treasury", "AnnualIncome", "ArmyUpkeep", "NavyUpkeep",
				"TaxIncomeTotal", "Trade", "Policing", "OtherIncome", "Popularity", "PopularityStatus", "Prestige",
				"Prosperity", "Government", "DatabaseKey", "Home", "NextElections", "Population", "Religion" })
		end)
		step("statthalter", function()
			S.governorships = {}
			local list = C.GovernorshipList(pid)
			for i = 1, (type(list) == "table" and #list or 0) do
				local g = list[i]
				local d = C.RetrieveGovernorshipDetails(g.Key)
				local r = pick(d, { "AnnualIncome", "ArmyUpkeep", "NavyUpkeep", "TaxIncomeTotal", "Trade", "Policing",
					"OtherIncome", "UpperTaxRate", "LowerTaxRate", "AutomanageTaxes", "AutomanageConstruction" })
				r.Key, r.Name = g.Key, g.Name
				if type(d) == "table" and type(d.Governor) == "table" then r.Governor = d.Governor.Name end
				S.governorships[#S.governorships + 1] = r
			end
		end)
		step("fraktion", function()
			local f = C.FactionDetails(pid)
			S.faction = pick(f, { "Key", "Name", "PowerRanking", "PrestigeRanking", "WealthRanking" })
			if type(f) == "table" and type(f.VictoryConditions) == "table" then
				local vc = f.VictoryConditions
				S.faction.victory = pick(vc, { "TotalRegionsNeeded", "ConquestDescription", "PrestigeDescription" })
				if type(vc.Regions) == "table" then
					local need, have = {}, 0
					for _, r in pairs(vc.Regions) do
						if type(r) == "table" then
							need[#need + 1] = pick(r, { "Name", "Key", "Owned", "Captured", "IsOwned" })
						end
					end
					S.faction.victory.regions = need
				end
			end
		end)
		if heavy then step("regionen", function()
			S.regions = {}
			local list = C.RetrieveFactionRegionList(pid)
			for i = 1, (type(list) == "table" and #list or 0) do
				local e = list[i]
				if type(e) == "table" and e.SettlementAddress ~= nil then
					local reg = C.SettlementsRegion(e.SettlementAddress)
					local d = reg ~= nil and C.InitialiseRegionInfoDetails(reg) or nil
					if type(d) == "table" then
						local r = pick(d, { "Name", "Settlement", "OwningFactionKey", "Theatre", "Population",
							"PopulationNumber", "PopulationChange", "Taxed", "UpperTax", "LowerTax", "UpperTaxPercentage",
							"LowerTaxPercentage", "Wealth", "WealthChange", "ReligionKey", "ActiveUpperClass",
							"ActiveLowerClass", "AdministrationCostPercentage" })
						r.upper_order = factors(d.UpperOrder)
						r.lower_order = factors(d.LowerOrder)
						r.town_wealth = factors(d.TownWealth)
						if type(d.Governor) == "table" then r.Governor = d.Governor.Name end
						S.regions[#S.regions + 1] = r
					end
				end
			end
		end) end
		local slot_refs = {}
		if heavy then step("gebaeude", function()
			S.buildings = {}
			local KNOWN = { building_key = 1, chains = 1, entry_tooltip1 = 1, entry_tooltip2 = 1, faction_key = 1,
			                image = 1, level = 1, location = 1, max_level = 1, name = 1, region_key = 1, slot = 1,
			                slot_key = 1, slot_type = 1, type = 1 }
			local bb = C.BuildingBrowserDetails(nil)
			if type(bb) ~= "table" or bb.region == nil then return end
			local first, cur, n = bb.region, bb.region, 0
			repeat
				local d = C.BuildingBrowserDetails(cur)
				n = n + 1
				if type(d) == "table" then
					for _, sl in pairs(d.slots or {}) do
						if type(sl) == "table" then
							local r = pick(sl, { "building_key", "name", "level", "max_level", "location", "region_key",
							                     "slot_key", "slot_type", "type" })
							r.region = d.region_name
							local extra = {}
							for k in pairs(sl) do if not KNOWN[k] then extra[#extra + 1] = tostring(k) end end
							if #extra > 0 then r.unknown_fields = extra end      -- z. B. Bau im Gang?
							S.buildings[#S.buildings + 1] = r
							if sl.building_key and sl.building_key ~= "empty" then
								slot_refs[#slot_refs + 1] = { r, sl.building_key, sl.region_key, sl.faction_key, sl.slot_key }
							end
						end
					end
				end
				cur = C.NextAutoSettlementSelection(cur)
			until cur == nil or cur == first or n >= 400
		end) end
		if heavy then step("gebaeudezustand", function()
			for _, ref in ipairs(slot_refs) do
				local d = C.BuildingDetails(ref[2], ref[3], ref[4], ref[5])
				if type(d) == "table" and type(d.Health) == "number" then ref[1].health = d.Health end
			end
		end)
			last_heavy = now()
			S.regions_written = os.date("%Y-%m-%d %H:%M:%S")
		end
		step("forschung", function()
			S.research = {}
			local unis = C.FactionsUniversities(key)
			for i = 1, (type(unis) == "table" and #unis or 0) do
				local u = unis[i]
				local r = pick(u, { "Name", "RegionKey", "SlotKey", "Health", "Level", "BuildingKey" })
				if type(u) == "table" and u.Address ~= nil then
					local cur = C.UniversityResearching(u.Address)
					if type(cur) == "table" and type(cur.Technology) == "table" then
						r.technology = cur.Technology.Name
						r.technology_key = cur.Technology.Key
						r.turns_left = cur.TurnsToComplete
					end
				end
				S.research[#S.research + 1] = r
			end
		end)
		step("armeen", function()
			S.armies = characters(C.RetrieveFactionMilitaryForceLists(pid, true))
			S.navies = characters(C.RetrieveFactionMilitaryForceLists(pid, false))
		end)
		step("agenten", function()
			S.agents = characters(C.RetrieveFactionAgentsList(pid))
		end)
		local at_war = {}
		step("diplomatie", function()
			local d = C.RetrieveDiplomacyDetails(pid)
			S.diplomacy = {}
			for _, cat in ipairs({ "Allies", "AtWar", "Protectorates", "ProtectorOf", "TradeRights" }) do
				local lst = {}
				if type(d) == "table" and type(d[cat]) == "table" then
					for k, v in pairs(d[cat]) do
						lst[#lst + 1] = { key = tostring(k), name = type(v) == "table" and v.Name or tostring(k) }
						if cat == "AtWar" then at_war[#at_war + 1] = tostring(k) end
					end
				end
				S.diplomacy[cat] = lst
			end
			S.factions = {}
			local all = C.RetrieveFactionListForDiplomacy()
			if type(all) == "table" then
				for k, v in pairs(all) do
					if type(v) == "table" then
						local r = pick(v, { "Key", "Name", "Attitude", "AttitudeValue", "Government", "IsMajor", "IsHuman",
						                    "ReligionName", "Trading" })
						r.Key = r.Key or tostring(k)
						if scalar(v.Relationship) then r.Relationship = v.Relationship end
						S.factions[#S.factions + 1] = r
					end
				end
			end
		end)
		step("feinde", function()
			S.enemies = {}
			for _, f in ipairs(at_war) do
				S.enemies[#S.enemies + 1] = { faction = f, armies = characters(C.RetrieveFactionMilitaryForceLists(f, true)),
				                              navies = characters(C.RetrieveFactionMilitaryForceLists(f, false)) }
			end
		end)
		if kind == "turn" then            -- Ereignisse der Gegnerzuege: "seit dem letzten Zug", bleiben im Zug stehen
			S.events = events
			events = {}
		end

		-- je Hauptteil einzeln kodieren: ein Teil, der scheitert, fehlt dann nur (mit Fehler), statt der ganzen Datei
		-- (2026-11-20 22:39: alle Schritte liefen, aber keine Datei entstand - Fehler blieb im pcall unsichtbar)
		local parts, enc_errors = {}, {}
		for k, v in pairs(S) do
			local part = {}
			local ok, err = pcall(enc, v, part)
			if ok then
				parts[#parts + 1] = jstr(tostring(k)) .. ":" .. table.concat(part)
			else
				enc_errors[#enc_errors + 1] = tostring(k) .. ": " .. tostring(err)
			end
		end
		if #enc_errors > 0 then
			local part = {}
			enc(enc_errors, part)
			parts[#parts + 1] = '"encode_errors":' .. table.concat(part)
			log_error("Kodieren: " .. table.concat(enc_errors, " | "))
		end
		local f, ferr = io.open(P .. "campaign_state.json.tmp", "w")
		if not f then
			log_error("campaign_state.json.tmp nicht schreibbar: " .. tostring(ferr))
		else
			f:write("{", table.concat(parts, ","), "}")
			f:close()
			os.remove(P .. "campaign_state.json")
			local okr, rerr = os.rename(P .. "campaign_state.json.tmp", P .. "campaign_state.json")
			if not okr then log_error("Umbenennen fehlgeschlagen: " .. tostring(rerr)) end
		end
		prog:close()
		capturing = false
	end

	if ok_es and es and es.AddEventCallBack then
		for _, name in ipairs(EVENTS) do pcall(es.AddEventCallBack, name, on_event(name)) end
		local function run(kind)
			local ok, err = pcall(capture, kind)
			if not ok then
				capturing = false
				log_error("Abbruch (" .. kind .. "): " .. tostring(err))
			end
		end
		pcall(es.AddEventCallBack, "FactionTurnStart", function(context)
			local ok_local, is_local = pcall(function() return conditions.FactionIsLocal(context) end)
			if ok_local and is_local then run("turn") end
		end)
		-- Takt: Spiel-Timer (einmalig, im Handler neu gestellt); arbeitet Aktionen und den Knopf im Steuerpult/Overlay
		-- (ai_bridge/campaign_request.txt) ab. Gestellt wird er NUR beim Zugbeginn und bei Eingaben des Spielers
		-- (Klick, Auswahl, Fenster) - wie in den Originalskripten. Beim Laden gestellt (WorldCreated/NewSession/
		-- UICreated) stuerzte Empire 2 s nach Ladebeginn ab (2026-11-20 23:41, c0000005 bei Empire.exe+0x1D54A9).
		-- Nach dem Laden mitten im Zug startet ihn der erste Timer des Spiels nach UICreated (kommt ohne Eingabe) oder
		-- der erste Klick; sein erster Tick erfasst dann voll ("load").
		-- Jedes Stellen bekommt einen eigenen Namen; nur der zuletzt gestellte zaehlt - eine aeltere Kette laeuft
		-- aus, es laufen nie zwei parallel; bei einer Eingabe ohne Tick seit 3 s wird neu gestellt.
		local ui_ready = false                                -- UICreated gesehen: Laden fast fertig
		pcall(es.AddEventCallBack, "UICreated", function() ui_ready = true end)
		local TICK = "empire_bridge_tick_"
		local tick_seq, tick_name, tick_seen, armed_at = 0, nil, -1e9, -1e9
		local function idle()                                 -- seit 3 s weder getickt noch gestellt: Takt tot
			return now() - math.max(tick_seen, armed_at) > 3
		end
		local function arm()
			tick_seq = tick_seq + 1
			local name = TICK .. tick_seq
			local ok = pcall(function() es.game_interface:add_time_trigger(name, 1) end)
			diag("Takt gestellt: " .. name .. (ok and "" or " (FEHLER)"))
			if ok and es.game_interface then tick_name = name; armed_at = now() end
		end
		-- Aktionen im eigenen Zug merken; der Takt erfasst, sobald 2 s Ruhe ist (eine Klickfolge = eine Erfassung)
		local dirty_at = nil
		local function mark()
			dirty_at = now()
		end
		local function input()                                -- Eingabe des Spielers: merken und Takt sicherstellen
			dirty_at = now()
			if idle() then arm() end                      -- Takt tot oder noch nie gelaufen? neu stellen
		end
		for _, name in ipairs({ "ComponentLClickUp", "PanelClosedCampaign", "CharacterSelected", "SettlementSelected",
		                        "FortSelected", "UnitSelectedCampaign" }) do
			pcall(es.AddEventCallBack, name, input)
		end
		for _, name in ipairs({ "HudRefresh", "MovementPointsExhausted", "MultiTurnMove",
		                        "BuildingConstructionIssuedByPlayer", "RecruitmentItemIssuedByPlayer",
		                        "CampaignArmiesMerge", "CharacterCompletedBattle", "SettlementOccupied",
		                        "GarrisonResidenceCaptured", "UnitTrained", "BuildingCompleted", "ResearchCompleted" }) do
			pcall(es.AddEventCallBack, name, mark)
		end
		-- Nach dem Laden (mitten im eigenen Zug) sofort voll erfassen - nicht erst beim naechsten Zugbeginn
		local need_initial = true
		pcall(es.AddEventCallBack, "TimeTrigger", function(context)
			local okn, name = pcall(function() return context.string end)
			if not okn then return end
			if name ~= tick_name then                            -- fremder Timer oder aeltere Kette: auslaufen lassen
				-- Das Spiel verarbeitet Timer, die Karte ist also fertig geladen (nach dem Laden kommt so ein Timer
				-- ohne jede Eingabe, 5 s nach UICreated - Diagnose 23:57): eigenen Takt starten, falls er nicht laeuft
				if ui_ready and idle() then arm() end
				return
			end
			tick_seen = now()
			diag("Takt: " .. name)
			pcall(function()
				if need_initial then
					local okp, mine = pcall(CampaignUI.IsPlayersTurn)
					if okp and mine then
						need_initial = false
						dirty_at = nil
						run("load")
					end
				elseif exists("campaign_request.txt") then
					os.remove(P .. "campaign_request.txt")
					dirty_at = nil
					run("request")
				elseif dirty_at and now() - dirty_at >= 2 then
					dirty_at = nil
					run("action")
				end
			end)
			arm()
		end)
		pcall(es.AddEventCallBack, "FactionTurnStart", function(context)    -- Zugbeginn erfasst selbst
			local ok_local, is_local = pcall(function() return conditions.FactionIsLocal(context) end)
			if ok_local and is_local then need_initial = false; arm() end
		end)
	end
end
