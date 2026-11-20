
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
		for _, name in ipairs({ "FactionTurnStart", "CharacterSelected", "SettlementSelected", "CharacterTurnStart",
		                        "PanelOpenedCampaign", "MovementPointsExhausted" }) do
			pcall(es.AddEventCallBack, name, once(name))
		end
	end
end
-- [empire_bridge] END
