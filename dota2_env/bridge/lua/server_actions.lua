-- The server-VM half of the action channel. cfg/dota2_env_server.cfg (the servercfgfile, exec'd once the
-- server activates) loads it with script_reload_code; it then reads the same bots/actions_t<team> files as the
-- bots and runs the actions a bot cannot issue itself.
--
-- Today that is the vector cast: a vector skill's second point travels as a VECTOR_TARGET_POSITION order ahead
-- of the cast order, and the bot API has no such order. ExecuteOrderFromTable delivers it as long as
-- TargetIndex is 0: the table parser defaults it to -1, and the order executor drops a vector order whose
-- target is neither 0 nor a live entity (docs/VERSION_DIFF.md 3.1).
local dkjson = require('game/dkjson')

-- a file younger than this is left for a later tick, as the bots do, so both VMs pick it up together
local MIN_AGE = 0.07
local executed = {}  -- team -> dotaTime of the file last run
local JOIN_INTERVAL = 1.0
local SPECTATOR_TEAM = 1  -- DOTA_TEAM_SPECTATOR, which the server VM does not define
local last_join = -math.huge

local function cast_vector(action)
    local hero = PlayerResource:GetSelectedHeroEntity(action.player)
    if hero == nil or not hero:IsAlive() then
        return
    end
    local slot = action.castVector.abilitySlot
    local ability = slot >= 0 and hero:GetAbilityByIndex(slot) or hero:GetItemInSlot(-slot - 1)
    if ability == nil then
        return
    end
    local start, direction = action.castVector.location, action.castVector.direction
    local z = hero:GetAbsOrigin().z
    local order = {
        UnitIndex = hero:entindex(),
        OrderType = DOTA_UNIT_ORDER_VECTOR_TARGET_POSITION,
        TargetIndex = 0,
        AbilityIndex = ability:entindex(),
        Position = Vector(start.x + direction.x * 100, start.y + direction.y * 100, z),
        Queue = false,
    }
    ExecuteOrderFromTable(order)
    order.OrderType = DOTA_UNIT_ORDER_CAST_POSITION
    order.Position = Vector(start.x, start.y, z)
    ExecuteOrderFromTable(order)
end

local function run_team(team)
    local chunk = loadfile('bots/actions_t' .. team)
    if chunk == nil then
        return
    end
    local data = dkjson.decode(chunk())
    if data == nil or executed[team] == data.dotaTime or GameRules:GetDOTATime(false, true) - data.dotaTime < MIN_AGE then
        return
    end
    executed[team] = data.dotaTime
    for _, action in ipairs(data.actions) do
        if action.actionType == 'DOTA_UNIT_ORDER_CAST_VECTOR' then
            cast_vector(action)
        end
    end
end

-- The host of a listen server (render_mode "human") sits on no team and sees nothing until it joins the
-- spectators, which is the "jointeam spec" a person used to type into the console. Only the server console
-- takes it from a script: the client console refuses script-issued jointeam (an FCVAR check). Nothing goes
-- out before PRE_GAME, which the server enters once every client has loaded, so the order always finds a
-- loaded host (docs/VERSION_DIFF.md 1.3). The host has no player id until it joins, so the order is repeated
-- until a human shows up on the spectator team.
local function host_watches()
    for player_id = 0, 23 do
        if PlayerResource:IsValidPlayerID(player_id) and not PlayerResource:IsFakeClient(player_id) then
            return PlayerResource:GetTeam(player_id) == SPECTATOR_TEAM
        end
    end
    return false
end

ServerActions = {}

function ServerActions:Think()
    run_team(DOTA_TEAM_GOODGUYS)
    run_team(DOTA_TEAM_BADGUYS)
    local now = GameRules:GetGameTime()
    if IsDedicatedServer() or last_join == nil or now - last_join < JOIN_INTERVAL then
        return 0.03
    end
    if host_watches() then
        last_join = nil
        print('server_actions: the host watches from the spectator team')
    elseif GameRules:State_Get() >= DOTA_GAMERULES_STATE_PRE_GAME then
        last_join = now
        SendToServerConsole('jointeam spec')
    end
    return 0.03
end

local thinker = SpawnEntityFromTableSynchronous('info_target', {targetname = 'dota2_env_server_actions'})
thinker:SetThink('Think', ServerActions, 'server_actions', 0)
