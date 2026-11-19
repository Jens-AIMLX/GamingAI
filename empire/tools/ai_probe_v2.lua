--[[
  ai_probe.lua v2 - Machbarkeitstest Lua-Bruecke (rome-battle-ai -> Empire)
  Lauf 1 ergab: battle_scripted.lua laeuft, io/os/package.loadlib vorhanden,
  aber empire_battle fehlt beim Laden (vermutlich UI-Lua-Zustand).
  v2 klaert: Wer laedt das Skript? Was steckt in system/vfs/lookup?
  Taucht empire_battle spaeter in diesem Zustand auf? (Polling ueber debug.sethook)
  Liest nur, gibt keine Befehle, ruft kein CliExecute/io.popen auf.
  Ergebnis: ai_probe_log.txt im Spielordner.
--]]

local LOGNAME = "ai_probe_log.txt"
local T0 = os.clock()

local function log(s)
	local f = io.open(LOGNAME, "a")
	if f then
		f:write(string.format("[%7.1fs] ", os.clock() - T0), tostring(s), "\n")
		f:close()
	end
end

local function try(label, fn)
	local ok, res = pcall(fn)
	if ok then log("  " .. label .. " = " .. tostring(res))
	else log("  " .. label .. " FEHLER: " .. tostring(res)) end
	return ok and res or nil
end

local function keys(t, limit)
	if type(t) ~= "table" then return "(" .. type(t) .. ")" end
	local names, n = {}, 0
	for k, v in pairs(t) do
		n = n + 1
		if not limit or n <= limit then names[#names + 1] = tostring(k) .. ":" .. type(v) end
	end
	table.sort(names)
	local s = table.concat(names, " ")
	if limit and n > limit then s = s .. " ... (" .. n .. " Eintraege)" end
	return s
end

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
-- 1. Herkunft und Umgebung
------------------------------------------------------------------
log("==================================================")
log("ai_probe v2 start " .. os.date())
log("traceback beim Laden:\n" .. debug.traceback())
log("coroutine.running=" .. tostring(coroutine.running()))
log("system: " .. keys(system))
log("vfs: " .. keys(vfs))
log("defined: " .. keys(defined, 40))
log("lookup: " .. keys(lookup, 40))
log("udata_lookup: " .. keys(udata_lookup, 40))
log("package.loaded: " .. keys(package.loaded, 60))
log("empire_battle=" .. type(empire_battle) .. " battle=" .. type(battle))

------------------------------------------------------------------
-- 2. Schlacht auslesen, sobald empire_battle verfuegbar ist
------------------------------------------------------------------
local function dump(b, tag)
	log("---- dump: " .. tag)
	log("  battle methods: " .. methods(b))
	try("game_time", function() return b:game_time() end)
	local als = try("alliances", function() return b:alliances() end)
	if als == nil then return end
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
end

------------------------------------------------------------------
-- 3. Polling ueber debug.sethook: alle ~2 s pruefen, ob neue
--    Globals (insb. empire_battle) auftauchen. Stoppt nach 15 min.
------------------------------------------------------------------
local baseline = {}
for k in pairs(_G) do baseline[k] = true end

local last_check = os.clock()
local checks, dumped = 0, false

local function poll()
	local now = os.clock()
	if now - last_check < 2 then return end
	last_check = now
	checks = checks + 1

	local new = {}
	for k, v in pairs(_G) do
		if not baseline[k] then
			new[#new + 1] = tostring(k) .. ":" .. type(v)
			baseline[k] = true
		end
	end
	if #new > 0 then log("neue Globals: " .. table.concat(new, " ")) end
	if checks % 30 == 0 then log("poll #" .. checks .. " empire_battle=" .. type(empire_battle)) end

	if not dumped and empire_battle ~= nil then
		dumped = true
		log("empire_battle ist jetzt da (poll #" .. checks .. ")")
		local b = (type(battle) == "userdata" and battle) or
			try("empire_battle:new()", function() return empire_battle:new() end)
		if b then
			local ok, err = pcall(dump, b, "nach Auftauchen")
			if not ok then log("  dump FEHLER: " .. tostring(err)) end
		end
	end

	if now - T0 > 900 then
		log("Polling beendet nach 15 min (" .. checks .. " Pruefungen)")
		debug.sethook()
	end
end

local ok, err = pcall(debug.sethook, function()
	local ok2, err2 = pcall(poll)
	if not ok2 then log("poll FEHLER: " .. tostring(err2)); debug.sethook() end
end, "", 200000)
log("debug.sethook gesetzt: " .. tostring(ok) .. " " .. tostring(err))
log("ai_probe v2 geladen")

return true
