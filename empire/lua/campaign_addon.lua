
-- [empire_bridge] BEGIN - von install.py eingefuegt, von install.py --uninstall entfernt
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
				if i > 25 then out[#out + 1] = indent .. "  ... (" .. (#ks - 25) .. " weitere)"; break end
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
		local function state_probe(context)
			if state_done or CampaignUI == nil then return end
			local ok_local, is_local = pcall(function() return conditions.FactionIsLocal(context) end)
			if not (ok_local and is_local) then return end
			state_done = true
			local out = { "== Lese-Probe " .. stamp() .. " ==" }
			local function try(label, fn, ...)
				local res = { pcall(fn, ...) }
				out[#out + 1] = "-- " .. label .. ": " .. (res[1] and ("ok, " .. (#res - 1) .. " Rueckgabe(n)") or ("FEHLER " .. tostring(res[2])))
				if res[1] then
					for i = 2, #res do dump(res[i], 3, {}, "  ", out, { 400 }) end
				end
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
			try("RetrieveFactionRegionList(pid)", C.RetrieveFactionRegionList, pid)
			try("RetrieveFactionAgentsList(pid)", C.RetrieveFactionAgentsList, pid)
			try("FactionDetails(pid)", C.FactionDetails, pid)
			try("FactionDetails(key)", C.FactionDetails, key)
			try("RetrieveDiplomacyDetails(pid)", C.RetrieveDiplomacyDetails, pid)
			try("RetrieveFactionListForDiplomacy()", C.RetrieveFactionListForDiplomacy)
			try("TradeInfo()", C.TradeInfo)
			try("PrestigeDetails()", C.PrestigeDetails)
			try("MonarchyInfo()", C.MonarchyInfo)
			try("GovernorshipList()", C.GovernorshipList)
			try("TheatreList()", C.TheatreList)
			try("RetrieveVisibleEnitityDetails()", C.RetrieveVisibleEnitityDetails)
			pcall(function()
				local f = io.open("ai_bridge\\campaign_state_probe.txt", "w")
				f:write(table.concat(out, "\n"), "\n")
				f:close()
			end)
		end
		pcall(es.AddEventCallBack, "FactionTurnStart", function(context) pcall(state_probe, context) end)
		for _, name in ipairs({ "FactionTurnStart", "CharacterSelected", "SettlementSelected", "CharacterTurnStart",
		                        "PanelOpenedCampaign", "MovementPointsExhausted" }) do
			pcall(es.AddEventCallBack, name, once(name))
		end
	end
end
-- [empire_bridge] END
