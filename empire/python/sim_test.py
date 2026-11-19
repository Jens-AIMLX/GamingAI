"""
sim_test.py - Offline-Test der Bruecke ohne Spiel.

Fuehrt data/ai_bridge.battle_script in echtem Lua 5.1 (lupa) gegen eine nachgebaute Battle-API aus
(Einheiten laufen 1,5 m/s zu goto-Zielen, halt stoppt, Timer/Phasen werden simuliert) und laesst
selftest.py als eigenen Prozess dagegen laufen. Simulation ~20x Echtzeit.
    python sim_test.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from lupa import lua51

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "lua" / "ai_bridge.battle_script"

MOCK = r"""
-- Unterschiede des Spiel-Lua nachbilden (in Lauf 6 gefunden): math.huge fehlt
math.huge = nil
-- Bridge-Skript soll ganz ohne math-Bibliothek auskommen (Umfang im Spiel unbekannt)
local _mathsave = math
math = { sqrt = _mathsave.sqrt }
SIM = { t = 0, timers = {}, handlers = {}, units = {} }
local function vec(x, y, z)
  local v = { x = x or 0, y = y or 0, z = z or 0 }
  function v:get_x() return self.x end
  function v:get_y() return self.y end
  function v:get_z() return self.z end
  function v:set(x, y, z) self.x, self.y, self.z = x, y, z end
  return v
end
battle_vector = { new = function() return vec() end }
local function list(items)
  return { count = function() return #items end, item = function(_, i) return items[i] end }
end
local function mkunit(t, x, z, bearing)
  local u = { pos = vec(x, 25, z), men = 100, start = 100, t = t, b = bearing, target = nil, ctrl = nil }
  function u:position() return vec(self.pos.x, self.pos.y, self.pos.z) end
  function u:bearing() return self.b end
  function u:number_of_men_alive() return self.men end
  function u:initial_number_of_men() return self.start end
  function u:is_moving() return self.target ~= nil end
  function u:is_routing() return false end
  function u:is_leaving_battle() return false end
  function u:missile_range() return 70 end
  function u:type() return self.t end
  SIM.units[#SIM.units + 1] = u
  return u
end
local function mkarmy(us)
  local a = { us = us }
  function a:units() return list(self.us) end
  function a:create_unit_controller()
    local c = { units = {}, owned = false }
    function c:add_units(u) self.units[#self.units + 1] = u end
    function c:take_control() self.owned = true; for _, u in ipairs(self.units) do u.ctrl = self; u.target = nil end end
    function c:release_control() self.owned = false; for _, u in ipairs(self.units) do u.ctrl = nil end end
    function c:goto_location(v) assert(v and v.get_x, "goto_location braucht vector"); for _, u in ipairs(self.units) do u.target = vec(v.x, v.y, v.z) end end
    function c:halt() for _, u in ipairs(self.units) do u.target = nil end end
    function c:attack_unit(t, b1, b2) assert(t and t.position, "attack_unit braucht Einheit"); for _, u in ipairs(self.units) do u.target = t:position() end end
    return c
  end
  return a
end
local a1 = mkarmy({ mkunit("euro_line_infantry", -50, 130, 180), mkunit("euro_line_infantry", 0, 130, 180), mkunit("euro_cuirassiers", 50, 130, 180) })
local a2 = mkarmy({ mkunit("euro_line_infantry", -40, -130, 0), mkunit("euro_line_infantry", 10, -130, 0), mkunit("12_lber_land_cannon", 60, -140, 0) })
local alls = { { armies = function() return list({ a1 }) end }, { armies = function() return list({ a2 }) end } }
local B = {}
function B:alliances() return list(alls) end
function B:game_time() return SIM.t end
function B:register_repeating_timer(name, ms) SIM.timers[#SIM.timers + 1] = { name = name, ms = ms / 1000, next = SIM.t + ms / 1000, rep = true } end
function B:register_singleshot_timer(name, ms) SIM.timers[#SIM.timers + 1] = { name = name, ms = ms / 1000, next = SIM.t + ms / 1000, rep = false } end
function B:unregister_timer(name) for _, t in ipairs(SIM.timers) do if t.name == name then t.dead = true end end end
function B:register_battle_phase_handler(n) SIM.handlers.phase = n end
function B:register_unit_selection_handler(n) SIM.handlers.sel = n end
function B:register_command_handler(n) SIM.handlers.cmd = n end
empire_battle = { new = function() return B end }

function SIM_phase(name)
  _G[SIM.handlers.phase]({ get_name = function() return name end })
end
function SIM_select(alliance)
  local u = (alliance == 1 and a1 or a2).us[1]
  _G[SIM.handlers.sel](u, true)
end
function SIM_step(dt)
  SIM.t = SIM.t + dt
  for _, u in ipairs(SIM.units) do
    if u.target then
      local dx, dz = u.target.x - u.pos.x, u.target.z - u.pos.z
      local d = math.sqrt(dx * dx + dz * dz)
      local s = 1.5 * dt
      if d <= s then u.pos.x, u.pos.z = u.target.x, u.target.z; u.target = nil
      else u.pos.x, u.pos.z = u.pos.x + dx / d * s, u.pos.z + dz / d * s end
    end
  end
  for _, t in ipairs(SIM.timers) do
    if not t.dead and SIM.t >= t.next then
      _G[t.name]()
      if t.rep then t.next = t.next + t.ms else t.dead = true end
    end
  end
end
"""


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="empire_bridge_sim_"))
    bridge_dir = work / "ai_bridge"
    bridge_dir.mkdir()
    (bridge_dir / "config.lua").write_text(
        'return { control = "enemy", player_alliance = "auto", tick_ms = 1000 }\n', encoding="utf-8")
    os.chdir(work)  # das Battle-Skript schreibt relativ nach ai_bridge/
    print(f"Simulation in {work}")

    lua = lua51.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(MOCK)
    lua.execute(SCRIPT.read_text(encoding="latin-1"))

    env = dict(os.environ, EMPIRE_BRIDGE_DIR=str(bridge_dir), PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen([sys.executable, str(HERE / "selftest.py")], env=env)

    stop = threading.Event()

    def run_sim():
        lua.globals().SIM_phase("Startup")
        lua.globals().SIM_phase("Deployment")
        for _ in range(40):
            lua.globals().SIM_step(0.25); time.sleep(0.0125)
        lua.globals().SIM_select(1)
        lua.globals().SIM_phase("Deployed")
        while not stop.is_set():
            lua.globals().SIM_step(0.25)
            time.sleep(0.0125)

    th = threading.Thread(target=run_sim, daemon=True)
    th.start()
    try:
        rc = proc.wait(timeout=180)
    finally:
        stop.set()
        th.join(timeout=5)
    print("\n--- bridge_log.txt ---")
    print((bridge_dir / "bridge_log.txt").read_text(encoding="utf-8", errors="replace"))
    print(f"selftest Rueckgabecode {rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
