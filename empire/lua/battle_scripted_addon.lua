
-- [empire_bridge] BEGIN - von install.py eingefuegt, von install.py --uninstall entfernt
-- Laedt empire_bridge.dll aus dem Spielordner. Sie setzt in Schlachten ohne eigenes battle_script
-- data\ai_bridge.battle_script ein.
do
	local function mark(s)
		pcall(function() local f = io.open("empire_bridge_log.txt", "a"); f:write(os.date("%Y-%m-%d %H:%M:%S"), "  [lua] ", s, "\n"); f:close() end)
	end
	local ok, res = pcall(function()
		local init, err = package.loadlib("empire_bridge.dll", "luaopen_empire_bridge")
		if not init then return "loadlib fehlgeschlagen: " .. tostring(err) end
		init()
		return "DLL geladen"
	end)
	mark("battle_scripted.lua: " .. tostring(ok) .. " " .. tostring(res))
end
-- [empire_bridge] END
