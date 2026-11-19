"""
sim_commander.py - Offline-Test der ganzen Kette ohne Spiel und ohne Sprachmodell:
Battle-Skript (echtes Lua 5.1 via lupa) <-> Datei-Bruecke <-> commander.py --backend rule.

Die nachgebaute Schlacht hat einfache Regeln: Einheiten laufen 1,5 m/s (Kavallerie 4 m/s), eine angreifende
Einheit rueckt bis auf ihre Reichweite (Nahkampf: Kontakt) heran und toetet dann Maenner beim Ziel; unter 35 %
Mannstaerke flieht eine Einheit. Allianz 2 wird gesteuert, Allianz 1 steht still (Zielscheibe).
    python sim_commander.py
"""
from __future__ import annotations

import json
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
math.huge = nil
local _m = math
math = { sqrt = _m.sqrt }
local sqrt = _m.sqrt
SIM = { t = 0, timers = {}, handlers = {}, units = {}, fired = {} }
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
local function mkunit(t, x, z, bearing, men, rng, spd)
  local u = { pos = vec(x, 25, z), men = men, start = men, t = t, b = bearing, rng = rng, spd = spd,
              target = nil, attack = nil, routing = false }
  function u:position() return vec(self.pos.x, self.pos.y, self.pos.z) end
  function u:bearing() return self.b end
  function u:number_of_men_alive() return self.men end
  function u:initial_number_of_men() return self.start end
  function u:is_moving() return self.moving == true end
  function u:is_routing() return self.routing end
  function u:is_leaving_battle() return false end
  function u:missile_range() return self.rng end
  function u:type() return self.t end
  SIM.units[#SIM.units + 1] = u
  return u
end
local function mkarmy(us)
  local a = { us = us }
  function a:units() return list(self.us) end
  function a:create_unit_controller()
    local c = { units = {} }
    function c:add_units(u)
      if u.grouped then error("calling 'add_units' on bad self (unit is already in another group)") end
      self.units[#self.units + 1] = u
    end
    function c:add_group(u) self.units[#self.units + 1] = u end
    function c:take_control() end
    function c:release_control() end
    function c:goto_location(v) for _, u in ipairs(self.units) do u.target = vec(v.x, v.y, v.z); u.attack = nil end end
    function c:halt() for _, u in ipairs(self.units) do u.target = nil; u.attack = nil end end
    function c:attack_unit(t, run, q) assert(t and t.position, "attack_unit braucht Einheit"); for _, u in ipairs(self.units) do u.attack = t; u.target = nil end end
    function c:fire_at_will(b) assert(type(b) == "boolean") end
    return c
  end
  return a
end
local a1 = mkarmy({
  mkunit("euro_line_infantry", -60, 150, 180, 120, 80, 1.5), mkunit("euro_line_infantry", 0, 150, 180, 120, 80, 1.5),
  mkunit("euro_line_infantry", 60, 150, 180, 120, 80, 1.5), mkunit("euro_cuirassiers", 140, 160, 180, 45, 0, 4) })
local a2 = mkarmy({
  mkunit("euro_line_infantry", -60, -150, 0, 120, 80, 1.5), mkunit("euro_line_infantry", 0, -150, 0, 120, 80, 1.5),
  mkunit("euro_line_infantry", 60, -150, 0, 120, 80, 1.5), mkunit("12_lber_land_cannon", 0, -200, 0, 18, 600, 0.6),
  mkunit("euro_hussars", -140, -160, 0, 50, 0, 4) })
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

function SIM_phase(name) _G[SIM.handlers.phase]({ get_name = function() return name end }) end
function SIM_select() _G[SIM.handlers.sel](a1.us[1], true) end
local function step_to(u, tx, tz, dt)
  local dx, dz = tx - u.pos.x, tz - u.pos.z
  local d = sqrt(dx * dx + dz * dz)
  local s = u.spd * dt
  if d <= s then u.pos.x, u.pos.z = tx, tz; return true end
  u.pos.x, u.pos.z = u.pos.x + dx / d * s, u.pos.z + dz / d * s
  u.moving = true
  return false
end
function SIM_step(dt)
  SIM.t = SIM.t + dt
  for _, u in ipairs(SIM.units) do
    u.moving = false
    if u.men > 0 and not u.routing then
      if u.attack then
        local t = u.attack
        if t.men <= 0 or t.routing then u.attack = nil
        else
          local dx, dz = t.pos.x - u.pos.x, t.pos.z - u.pos.z
          local d = sqrt(dx * dx + dz * dz)
          local reach = (u.rng > 0) and u.rng or 5
          if d > reach then step_to(u, t.pos.x, t.pos.z, dt)
          else
            local kill = (u.rng > 0) and (u.men * 0.004 * dt * (1.5 - d / reach)) or (u.men * 0.02 * dt)
            t.men = t.men - kill
            if t.men < 0 then t.men = 0 end
            SIM.fired[#SIM.fired + 1] = 1
          end
        end
      elseif u.target then
        if step_to(u, u.target.x, u.target.z, dt) then u.target = nil end
      end
      if u.men < u.start * 0.35 then u.routing = true end
    end
  end
  for _, t in ipairs(SIM.timers) do
    if not t.dead and SIM.t >= t.next then
      _G[t.name]()
      if t.rep then t.next = t.next + t.ms else t.dead = true end
    end
  end
end
function SIM_summary()
  local s = ""
  for i, u in ipairs(SIM.units) do
    s = s .. string.format("%s men %d/%d at (%.0f,%.0f)%s\n", u.t, u.men, u.start, u.pos.x, u.pos.z, u.routing and " ROUTING" or "")
  end
  return s
end
"""


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="empire_cmd_sim_"))
    bridge_dir = work / "ai_bridge"
    bridge_dir.mkdir()
    (bridge_dir / "config.lua").write_text('return { control = "enemy", player_alliance = "auto", tick_ms = 1000 }\n')
    os.chdir(work)
    lua = lua51.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(MOCK)
    lua.execute(SCRIPT.read_text(encoding="latin-1"))
    g = lua.globals()

    env = dict(os.environ, EMPIRE_BRIDGE_DIR=str(bridge_dir), PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen([sys.executable, str(HERE / "commander.py"), "--backend", "rule", "--once",
                             "--interval", "8", "--close-interval", "4"], env=env, cwd=str(HERE))
    stop = threading.Event()
    sim_lock = threading.Lock()

    def run_sim():
        g.SIM_phase("Startup"); g.SIM_phase("Deployment")
        for _ in range(20):
            g.SIM_step(0.25); time.sleep(0.01)
        g.SIM_select(); g.SIM_phase("Deployed")
        t_end = 240.0   # Spielsekunden
        while not stop.is_set() and g.SIM.t < t_end:
            g.SIM_step(0.25)
            time.sleep(0.0125)   # ~20x Echtzeit
        g.SIM_phase("Complete")

    th = threading.Thread(target=run_sim, daemon=True)
    th.start()
    th.join(timeout=200)
    try:
        rc = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill(); rc = -1
    stop.set()

    print("\n--- Endstand der Simulation ---")
    print(g.SIM_summary())
    logs = sorted((HERE / "logs").glob("battle_*.jsonl"), key=lambda p: p.stat().st_mtime)
    entries = [json.loads(l) for l in logs[-1].read_text(encoding="utf-8").splitlines()] if logs else []
    plans = [e for e in entries if e["type"] == "plan"]
    errors = [e for e in entries if e["type"] == "error"]
    reports = [e for e in entries if e["type"] == "report"]
    st = json.loads((bridge_dir / "state.json").read_text(encoding="utf-8"))
    bad = [r for r in st["order_results"] if not r["ok"]]
    print(f"Plaene {len(plans)}, Planungsfehler {len(errors)}, Befehlsfehler (letzte 100) {len(bad)}, Schuesse/Treffer-Ticks {len(g.SIM.fired)}")
    if reports:
        print("\n--- erster Lagebericht ---\n" + reports[0]["text"])
    for b in bad[:5]:
        print("Befehlsfehler:", b)
    a1_loss = sum(1 for _ in [])
    ok = len(plans) >= 3 and not errors and not bad and len(g.SIM.fired) > 0
    print("\nERGEBNIS:", "PASS" if ok else "FAIL", f"(commander rc={rc})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
