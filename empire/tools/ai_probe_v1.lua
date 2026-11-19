--[[
  ai_probe.lua - Machbarkeitstest fuer eine Lua-Bruecke (rome-battle-ai -> Empire)
  Wird von data/battle_scripted.lua geladen. Liest nur, gibt keine Befehle.
  Ergebnis: ai_probe_log.txt im Spielordner (oder in %TEMP%, falls dort nicht schreibbar).
  Entfernen: battle_scripted.lua.orig zurueck nach battle_scripted.lua kopieren, diese Datei loeschen.
--]]

local LOGNAME = "ai_probe_log.txt"
local logpath = nil

local function openlog()
	if type(io) ~= "table" or type(io.open) ~= "function" then return nil end
	local cands = { LOGNAME }
	if type(os) == "table" and type(os.getenv) == "function" then
		local t = os.getenv("TEMP")
		if t then cands[#cands + 1] = t .. "\\" .. LOGNAME end
	end
	for _, p in ipairs(cands) do
		local f = io.open(p, "a")
		if f then logpath = p; return f end
	end
	return nil
end

local function log(s)
	local f = openlog()
	if f then f:write(tostring(s), "\n"); f:close() end
end

local function try(label, fn)
	local ok, res = pcall(fn)
	if ok then log("  " .. label .. " = " .. tostring(res))
	else log("  " .. label .. " FEHLER: " .. tostring(res)) end
	return ok and res or nil
end

-- Methodennamen eines gebundenen Objekts (userdata) auflisten
local function methods(obj)
	local names = {}
	local mt = getmetatable(obj)
	if type(mt) == "table" then
		for k in pairs(mt) do names[#names + 1] = tostring(k) end
		if type(mt.__index) == "table" then
			for k in pairs(mt.__index) do names[#names + 1] = "." .. tostring(k) end
		end
	elseif mt ~= nil then
		names[#names + 1] = "(metatable: " .. type(mt) .. ")"
	end
	table.sort(names)
	return table.concat(names, " ")
end

local function vec(v)
	if v == nil then return "nil" end
	local ok, s = pcall(function()
		return string.format("(%.1f, %.1f, %.1f)", v:get_x(), v:get_y(), v:get_z())
	end)
	return ok and s or tostring(v)
end

------------------------------------------------------------------
-- 1. Umgebung
------------------------------------------------------------------
log("==================================================")
log("ai_probe start  " .. ((type(os) == "table" and os.date) and os.date() or "(os.date fehlt)"))
log("_VERSION=" .. tostring(_VERSION))
log("io=" .. type(io) .. " os=" .. type(os) .. " package=" .. type(package) .. " debug=" .. type(debug))
if type(package) == "table" then
	log("package.loadlib=" .. type(package.loadlib) .. " cpath=" .. tostring(package.cpath))
	log("package.path=" .. tostring(package.path))
end
if type(io) == "table" then log("io.popen=" .. type(io.popen)) end
log("empire_battle=" .. type(empire_battle) .. " battle_vector=" .. type(battle_vector))

local g = {}
for k, v in pairs(_G) do g[#g + 1] = tostring(k) .. ":" .. type(v) end
table.sort(g)
log("globals: " .. table.concat(g, " "))

------------------------------------------------------------------
-- 2. Schlachtzustand auslesen (wird mehrfach aufgerufen)
------------------------------------------------------------------
local probe_battle = nil

function AI_Probe_Dump(tag)
	log("---- dump: " .. tostring(tag))
	if type(empire_battle) == "nil" then log("  empire_battle fehlt"); return end
	if probe_battle == nil then
		probe_battle = try("empire_battle:new()", function() return empire_battle:new() end)
		if probe_battle == nil then return end
		log("  battle methods: " .. methods(probe_battle))
	end
	local b = probe_battle
	try("game_time", function() return b:game_time() end)
	local als = try("alliances", function() return b:alliances() end)
	if als == nil then return end
	log("  alliances methods: " .. methods(als))
	local na = try("alliances:count", function() return als:count() end) or 0
	for a = 1, na do
		local al = als:item(a)
		if a == 1 then log("  alliance methods: " .. methods(al)) end
		local arms = al:armies()
		local nar = try("alliance " .. a .. " armies:count", function() return arms:count() end) or 0
		for r = 1, nar do
			local army = arms:item(r)
			if a == 1 and r == 1 then log("  army methods: " .. methods(army)) end
			local units = army:units()
			local nu = try("  a" .. a .. " r" .. r .. " units:count", function() return units:count() end) or 0
			for u = 1, nu do
				local un = units:item(u)
				if a == 1 and r == 1 and u == 1 then log("  unit methods: " .. methods(un)) end
				local line = string.format("    a%d r%d u%d", a, r, u)
				local function add(lbl, fn)
					local ok, v = pcall(fn)
					line = line .. " " .. lbl .. "=" .. (ok and tostring(v) or "ERR")
				end
				add("name", function() return un:name() end)
				add("pos", function() return vec(un:position()) end)
				add("bearing", function() return un:bearing() end)
				add("men", function() return un:number_of_men_alive() .. "/" .. un:initial_number_of_men() end)
				add("routing", function() return un:is_routing() end)
				add("moving", function() return un:is_moving() end)
				add("range", function() return un:missile_range() end)
				log(line)
			end
		end
	end
	-- Controller nur anlegen, NICHT take_control aufrufen (keine Eingriffe)
	if tag == "t+5s" then
		try("create_unit_controller (alliance 2, army 1)", function()
			local c = als:item(2):armies():item(1):create_unit_controller()
			log("  controller methods: " .. methods(c))
			return c
		end)
	end
end

local function safe_dump(tag)
	local ok, err = pcall(AI_Probe_Dump, tag)
	if not ok then log("  dump '" .. tostring(tag) .. "' FEHLER: " .. tostring(err)) end
end

function AI_Probe_Timer_5s()  safe_dump("t+5s") end
function AI_Probe_Timer_60s() safe_dump("t+60s") end

function AI_Probe_Phase(event)
	local n = "?"
	pcall(function() n = event:get_name() end)
	log("---- battle phase event: " .. tostring(n))
	safe_dump("phase " .. tostring(n))
end

------------------------------------------------------------------
-- 3. Sofort-Dump beim Laden, dann Timer und Phasen-Handler
------------------------------------------------------------------
safe_dump("beim Laden")
if probe_battle ~= nil then
	try("register_singleshot_timer 5s",  function() probe_battle:register_singleshot_timer("AI_Probe_Timer_5s", 5000); return true end)
	try("register_singleshot_timer 60s", function() probe_battle:register_singleshot_timer("AI_Probe_Timer_60s", 60000); return true end)
	try("register_battle_phase_handler", function() probe_battle:register_battle_phase_handler("AI_Probe_Phase"); return true end)
end
log("ai_probe geladen, log=" .. tostring(logpath))

return true
