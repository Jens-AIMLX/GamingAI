
-- [empire_bridge] BEGIN - von install.py eingefuegt, von install.py --uninstall entfernt
-- Laedt empire_bridge.dll schon auf der Kampagnenkarte (wie battle_scripted.lua in Schlachten): Overlay im Spielbild
-- mit dem Steuerpult (F8). Die DLL richtet sich je Spielprozess nur einmal ein (g_installed), mehrfaches Laden ist
-- harmlos.
do
	local ok, res = pcall(function()
		local init, err = package.loadlib("empire_bridge.dll", "luaopen_empire_bridge")
		if not init then return "loadlib fehlgeschlagen: " .. tostring(err) end
		init()
		return "DLL geladen"
	end)
	pcall(function()
		local f = io.open("empire_bridge_log.txt", "a")
		f:write(os.date("%Y-%m-%d %H:%M:%S"), "  [lua] campaign scripting.lua: ", tostring(ok), " ", tostring(res), "\n")
		f:close()
	end)
end
-- Kampagnen-Probe (nur lesen, ruft keine Spielfunktion auf): schreibt nach ai_bridge/campaign_probe.txt, was das
-- Kampagnen-Lua kann - Bibliotheken (io/os/package.loadlib/debug), globale Namen, Module, Ereignisse, Abfragen
-- (conditions), die Funktionen von game_interface und CampaignUI sowie die Kontext-Objekte einiger Ereignisse.
do
	local lines, first = {}, true
	local function add(s) lines[#lines + 1] = s end
	local function flush()
		pcall(function()
			local f = io.open("ai_bridge\\campaign_probe.txt", first and "w" or "a")
			f:write(table.concat(lines, "\n"), "\n")
			f:close()
			first = false
		end)
		lines = {}
	end
	local function keys(t, label)
		local ks = {}
		if type(t) == "table" then
			for k, v in pairs(t) do ks[#ks + 1] = tostring(k) .. " (" .. type(v) .. ")" end
		end
		table.sort(ks)
		add(label .. ": " .. #ks .. " Eintraege")
		for _, k in ipairs(ks) do add("  " .. k) end
	end
	local function meta(obj, label)
		local ok, mt = pcall(getmetatable, obj)
		if not ok or mt == nil then
			add(label .. ": " .. type(obj) .. ", keine Metatabelle")
			if type(obj) == "table" then keys(obj, label) end
			return
		end
		if type(mt) ~= "table" then add(label .. ": Metatabelle geschuetzt (" .. tostring(mt) .. ")"); return end
		keys(mt, label .. " Metatabelle")
		if type(mt.__index) == "table" then keys(mt.__index, label .. " __index") end
		if type(obj) == "table" then keys(obj, label .. " Felder") end
	end
	local function stamp()
		local ok, s = pcall(function() return os.date("%Y-%m-%d %H:%M:%S") end)
		return ok and s or "?"
	end

	pcall(function()
		add("== Kampagnen-Probe " .. stamp() .. " ==")
		add("Lua " .. tostring(_VERSION) .. " | io " .. type(io) .. " | os " .. type(os) .. " | package " .. type(package)
			.. " | package.loadlib " .. type(package and package.loadlib) .. " | debug " .. type(debug)
			.. " | require " .. type(require))
		keys(_G, "Globale Namen (_G)")
		if package and package.loaded then keys(package.loaded, "Geladene Module (package.loaded)") end
		local ok_es, es = pcall(require, "EpisodicScripting")
		if ok_es then keys(es, "EpisodicScripting") end
		if type(conditions) == "table" then keys(conditions, "Abfragen (conditions)") end
		if CampaignUI ~= nil then meta(CampaignUI, "CampaignUI") end
		local ok_ev, ev = pcall(require, "data.events")
		if ok_ev then keys(ev, "Ereignisse (data.events)") end
	end)
	flush()

	local ok_es, es = pcall(require, "EpisodicScripting")
	if ok_es and es and es.AddEventCallBack then
		local function on_world(context)
			pcall(function()
				add("== WorldCreated " .. stamp() .. " ==")
				meta(es.game_interface, "game_interface")
				meta(context, "Kontext WorldCreated")
				if CampaignUI ~= nil then meta(CampaignUI, "CampaignUI (nach WorldCreated)") end
			end)
			flush()
		end
		local seen = {}
		local function once(name)
			return function(context)
				if seen[name] then return end
				seen[name] = true
				pcall(function()
					add("== " .. name .. " " .. stamp() .. " ==")
					meta(context, "Kontext " .. name)
				end)
				flush()
			end
		end
		pcall(es.AddEventCallBack, "WorldCreated", on_world)

		-- Probe 2 (nur Lesefunktionen): zu Beginn des ersten eigenen Zugs die Rueckgaben der Lesefunktionen von
		-- CampaignUI nach ai_bridge/campaign_state_probe.txt (Signaturen aus den UI-Skripten des Spiels)
		local state_done = false
		local function dump(v, depth, seen, indent, out, budget)
			if budget[1] <= 0 then return end
			local t = type(v)
			if t ~= "table" then
				local s = tostring(v)
				if t == "userdata" then
					local ok, mt = pcall(getmetatable, v)
					if ok and type(mt) == "table" then
						local ks = {}
						for k in pairs(mt) do ks[#ks + 1] = tostring(k) end
						table.sort(ks)
						s = s .. " [Metatabelle: " .. table.concat(ks, ", ") .. "]"
					end
				end
				out[#out + 1] = indent .. s .. " (" .. t .. ")"
				budget[1] = budget[1] - 1
				return
			end
			if seen[v] then out[#out + 1] = indent .. "<schon ausgegeben>"; return end
			seen[v] = true
			local ks = {}
			for k in pairs(v) do ks[#ks + 1] = k end
			table.sort(ks, function(a, b) return tostring(a) < tostring(b) end)
			out[#out + 1] = indent .. "{ " .. #ks .. " Eintraege"
			for i, k in ipairs(ks) do
				if i > 60 then out[#out + 1] = indent .. "  ... (" .. (#ks - 60) .. " weitere)"; break end
				local val = v[k]
				if type(val) == "table" and depth > 0 then
					out[#out + 1] = indent .. "  [" .. tostring(k) .. "] ="
					dump(val, depth - 1, seen, indent .. "    ", out, budget)
				elseif type(val) == "table" then
					out[#out + 1] = indent .. "  [" .. tostring(k) .. "] = {...}"
				else
					local tmp = {}
					dump(val, 0, seen, "", tmp, budget)
					out[#out + 1] = indent .. "  [" .. tostring(k) .. "] = " .. (tmp[1] or "?")
				end
				budget[1] = budget[1] - 1
				if budget[1] <= 0 then out[#out + 1] = indent .. "  ... (Ausgabe gekuerzt)"; break end
			end
			out[#out + 1] = indent .. "}"
		end
		-- Probe 2 laeuft NUR, wenn ai_bridge/campaign_probe2.on existiert: ein Aufruf in die Engine kann das Spiel
		-- abstuerzen lassen (pcall faengt nur Lua-Fehler; Absturz 2026-11-20 21:10 beim ersten Lauf). Vor jedem Aufruf
		-- steht "BEGINN <Aufruf>" in campaign_state_probe.progress, danach "ENDE"; fehlt das ENDE, hat dieser Aufruf
		-- das Spiel abstuerzen lassen - er kommt nach campaign_probe2.skip und wird in spaeteren Laeufen ausgelassen.
		local P = "ai_bridge\\"
		local function exists(name)
			local f = io.open(P .. name, "r")
			if f then f:close(); return true end
			return false
		end
		local function state_probe(context)
			if state_done or CampaignUI == nil or not exists("campaign_probe2.on") then return end
			local ok_local, is_local = pcall(function() return conditions.FactionIsLocal(context) end)
			if not (ok_local and is_local) then return end
			state_done = true
			local skip, open = {}, nil
			pcall(function() for line in io.lines(P .. "campaign_probe2.skip") do skip[line] = true end end)
			pcall(function()
				for line in io.lines(P .. "campaign_state_probe.progress") do
					local b, e = line:match("^BEGINN (.*)$"), line:match("^ENDE (.*)$")
					if b then open = b elseif e and e == open then open = nil end
				end
			end)
			if open and not skip[open] then           -- letzter Lauf ist bei diesem Aufruf abgestuerzt
				skip[open] = true
				pcall(function() local f = io.open(P .. "campaign_probe2.skip", "a"); f:write(open, "\n"); f:close() end)
			end
			local prog = io.open(P .. "campaign_state_probe.progress", "w")
			local outf = io.open(P .. "campaign_state_probe.txt", "w")
			if not (prog and outf) then return end
			outf:write("== Lese-Probe " .. stamp() .. " ==\n")
			outf:flush()
			local function try(label, fn, ...)
				if skip[label] then
					outf:write("-- " .. label .. ": AUSGELASSEN (Spiel stuerzte hier in einem frueheren Lauf ab)\n")
					outf:flush()
					return nil
				end
				prog:write("BEGINN ", label, "\n")
				prog:flush()
				local res = { pcall(fn, ...) }
				local out = { "-- " .. label .. ": " .. (res[1] and ("ok, " .. (#res - 1) .. " Rueckgabe(n)")
				                                          or ("FEHLER " .. tostring(res[2]))) }
				if res[1] then
					for i = 2, #res do
						local v = res[i]
						if type(v) == "table" then             -- alle Feldnamen vollstaendig, unabhaengig vom Limit
							local ks = {}
							for kk in pairs(v) do ks[#ks + 1] = tostring(kk) end
							table.sort(ks)
							out[#out + 1] = "  Felder (" .. #ks .. "): " .. table.concat(ks, ", ")
							local first = v[1]
							if type(first) == "table" then
								local fk = {}
								for kk in pairs(first) do fk[#fk + 1] = tostring(kk) end
								table.sort(fk)
								out[#out + 1] = "  Felder je Eintrag (" .. #fk .. "): " .. table.concat(fk, ", ")
							end
						end
						pcall(dump, v, 3, {}, "  ", out, { 1500 })
					end
				end
				outf:write(table.concat(out, "\n"), "\n")
				outf:flush()
				prog:write("ENDE ", label, "\n")
				prog:flush()
				return res[1] and res[2] or nil
			end
			local C = CampaignUI
			local pid = try("PlayerFactionId()", C.PlayerFactionId)
			local key = try("PlayersFactionKey()", C.PlayersFactionKey)
			try("PlayersFaction()", C.PlayersFaction)
			try("CurrentTurn()", C.CurrentTurn)
			try("CurrentYear()", C.CurrentYear)
			try("CurrentSeasonString()", C.CurrentSeasonString)
			try("IsPlayersTurn()", C.IsPlayersTurn)
			try("RetrieveFactionMilitaryForceLists(pid, true)", C.RetrieveFactionMilitaryForceLists, pid, true)
			try("RetrieveFactionMilitaryForceLists(pid, false)", C.RetrieveFactionMilitaryForceLists, pid, false)
			local regions = try("RetrieveFactionRegionList(pid)", C.RetrieveFactionRegionList, pid)
			try("RetrieveFactionAgentsList(pid)", C.RetrieveFactionAgentsList, pid)
			try("FactionDetails(pid)", C.FactionDetails, pid)
			try("FactionDetails(key)", C.FactionDetails, key)
			local dip = try("RetrieveDiplomacyDetails(pid)", C.RetrieveDiplomacyDetails, pid)
			try("RetrieveFactionListForDiplomacy()", C.RetrieveFactionListForDiplomacy)
			try("TradeInfo()", C.TradeInfo)
			try("PrestigeDetails()", C.PrestigeDetails)
			try("MonarchyInfo()", C.MonarchyInfo)
			-- Aufrufe NUR in der Form, in der die UI-Skripte des Spiels sie machen (docs/campaign_ui_signatures.txt):
			-- ein fehlendes oder falsches Argument laesst die Engine abstuerzen (TheatreList() ohne Argument, 21:18)
			try("RetrieveVisibleEnitityDetails()", C.RetrieveVisibleEnitityDetails)
			local gships = try("GovernorshipList(pid)", C.GovernorshipList, pid)
			if type(gships) == "table" and type(gships[1]) == "table" and gships[1].Key ~= nil then
				try("RetrieveGovernorshipDetails(GovernorshipList(pid)[1].Key)", C.RetrieveGovernorshipDetails, gships[1].Key)
			end
			try("HomeTheatre(pid)", C.HomeTheatre, pid)
			try("TheatreList(true)", C.TheatreList, true)
			local region
			if type(regions) == "table" and type(regions[1]) == "table" and regions[1].SettlementAddress ~= nil then
				region = try("SettlementsRegion(regions[1].SettlementAddress)", C.SettlementsRegion, regions[1].SettlementAddress)
			end
			-- Probe 3: Regionsdetails (wie region_details.luac), Staatskasse (government_screens.luac), Feindarmeen
			if region ~= nil then
				local details = try("InitialiseRegionInfoDetails(region)", C.InitialiseRegionInfoDetails, region)
				if type(details) == "table" and type(details.Region) == "table" and details.Region.Region ~= nil then
					try("RegionsPublicOrders(details.Region.Region)", C.RegionsPublicOrders, details.Region.Region)
				end
			end
			try("InitialiseGovernmentDetails()", C.InitialiseGovernmentDetails)
			local enemy
			if type(dip) == "table" and type(dip.AtWar) == "table" then
				for k, v in pairs(dip.AtWar) do
					enemy = type(k) == "string" and k or (type(v) == "string" and v or nil)
					if enemy then break end
				end
			end
			if enemy then
				outf:write("-- Feind fuer die naechsten Aufrufe: " .. enemy .. "\n")
				try("FactionDetails(enemy)", C.FactionDetails, enemy)
				try("RetrieveFactionMilitaryForceLists(enemy, true)", C.RetrieveFactionMilitaryForceLists, enemy, true)
			end
			-- Probe 4: Rekrutierung (recruitment.luac), Forschung (technology_researching.luac), Bauten/Bau/Schaeden
			-- (building_browser.luac: erst mit nil, dann ueber das Feld region und NextAutoSettlementSelection)
			try("ReviewPanelInfo()", C.ReviewPanelInfo)
			local unis = try("FactionsUniversities(key)", C.FactionsUniversities, key)
			if type(unis) == "table" and type(unis[1]) == "table" and unis[1].Address ~= nil then
				try("UniversityResearching(unis[1].Address)", C.UniversityResearching, unis[1].Address)
			end
			local bb = try("BuildingBrowserDetails(nil)", C.BuildingBrowserDetails, nil)
			if type(bb) == "table" and bb.region ~= nil then
				local nxt = try("NextAutoSettlementSelection(bb.region)", C.NextAutoSettlementSelection, bb.region)
				if nxt ~= nil then
					try("BuildingBrowserDetails(next)", C.BuildingBrowserDetails, nxt)
				end
				-- Probe 5: Gebaeudedetails eines belegten Slots (Schaden/Bau?) in beiden Reihenfolgen der UI-Skripte,
				-- danach ein voller Umlauf ueber alle Siedlungen (nur Zaehler und Zeit, keine Ausgabe der Inhalte)
				local s
				for _, slot in pairs(bb.slots or {}) do
					if type(slot) == "table" and slot.building_key and slot.building_key ~= "empty" then s = slot; break end
				end
				if s then
					outf:write("-- Slot fuer Gebaeudedetails: " .. tostring(s.building_key) .. " / " .. tostring(s.slot_key) .. "\n")
					try("BuildingDetails(building, region, faction, slot)", C.BuildingDetails,
					    s.building_key, s.region_key, s.faction_key, s.slot_key)
					try("BuildingDetails(building, faction, region, slot)", C.BuildingDetails,
					    s.building_key, s.faction_key, s.region_key, s.slot_key)
				end
				try("Umlauf aller Siedlungen", function()
					local t0 = os.clock()
					local first, cur, n, slots, empty, names = bb.region, bb.region, 0, 0, 0, {}
					repeat
						local d = C.BuildingBrowserDetails(cur)
						n = n + 1
						if type(d) == "table" then
							names[#names + 1] = tostring(d.region_name)
							for _, sl in pairs(d.slots or {}) do
								slots = slots + 1
								if type(sl) == "table" and sl.building_key == "empty" then empty = empty + 1 end
							end
						end
						cur = C.NextAutoSettlementSelection(cur)
					until cur == nil or cur == first or n >= 300
					return { Regionen = n, Slots = slots, LeereSlots = empty, Sekunden = os.clock() - t0,
					         ZurueckAmAnfang = (cur == first), Namen = table.concat(names, ", ") }
				end)
			end
			outf:write("== Ende der Lese-Probe ==\n")
			outf:close()
			prog:close()
		end
		pcall(es.AddEventCallBack, "FactionTurnStart", function(context) pcall(state_probe, context) end)
		for _, name in ipairs({ "FactionTurnStart", "CharacterSelected", "SettlementSelected", "CharacterTurnStart",
		                        "PanelOpenedCampaign", "MovementPointsExhausted" }) do
			pcall(es.AddEventCallBack, name, once(name))
		end
	end
end
-- [empire_bridge] END
