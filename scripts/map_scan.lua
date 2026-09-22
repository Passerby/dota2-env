-- Bot-script half of scripts/extract_map.py: prints the static map as the bot API sees it.
-- The __X0__ style placeholders are filled in from the gridnav header before the file is copied over
-- every bot_<hero>.lua. It runs on the Radiant hero only, one phase per Think, and every line is
-- "MAPSCAN <seq> <tag> <payload>", under 700 characters, so the console neither splits nor merges them.
local dkjson = require('game/dkjson')

local X0, Y0, CELL, WIDTH, HEIGHT = __X0__, __Y0__, __CELL__, __WIDTH__, __HEIGHT__
local ROWS_PER_THINK = 2
local TREES_PER_LINE = 20
local MAX_TREE_MISSES = 64

local seq, step, phase = 0, 0, 'globals'
local names, next_name = nil, 1
local tree_id, tree_misses = 0, 0
local row = 0

local function emit(tag, text)
    seq = seq + 1
    print('MAPSCAN ' .. seq .. ' ' .. tag .. ' ' .. text)
end

local function round1(value)
    return math.floor(value * 10 + 0.5) / 10
end

local function vec(v)
    return {round1(v.x), round1(v.y), round1(v.z)}
end

-- A value JSON can hold: numbers, booleans and strings as they are, vectors as [x, y, z], the rest as a type name.
local function plain(value)
    local kind = type(value)
    if kind == 'number' or kind == 'boolean' or kind == 'string' then
        return value
    end
    local ok, v = pcall(vec, value)
    if ok then
        return v
    end
    return kind
end

local function globals_phase()
    if names == nil then
        names = {}
        for name in pairs(_G) do
            names[#names + 1] = tostring(name)
        end
        table.sort(names)
    end
    for _ = 1, 10 do
        if next_name > #names then
            return 'probe'
        end
        local chunk, length = {}, 0
        while next_name <= #names and length < 400 do
            local name = names[next_name]
            local value = plain(_G[name])
            if type(value) == 'string' then
                value = value:sub(1, 40)
            end
            chunk[name] = value
            length = length + #name + 24
            next_name = next_name + 1
        end
        emit('globals', dkjson.encode(chunk))
    end
    return 'globals'
end

local function probe_phase()
    local report = {dota_time = DotaTime(), game_state = GetGameState()}
    local ok, level = pcall(GetHeightLevel, Vector(0, 0, 0))
    report.height_at_origin = ok and plain(level) or ('error ' .. tostring(level))
    local ok_passable, passable = pcall(IsLocationPassable, Vector(0, 0, 0))
    report.passable_at_origin = ok_passable and plain(passable) or ('error ' .. tostring(passable))
    local ok_all, all = pcall(GetAllTrees)
    if ok_all and type(all) == 'table' then
        report.all_trees = {count = #all, first = {plain(all[1]), plain(all[2]), plain(all[3])}}
    else
        report.all_trees = tostring(all)
    end
    report.tree_0 = plain(select(2, pcall(GetTreeLocation, 0)))
    emit('probe', dkjson.encode(report))
    return 'trees'
end

local function trees_phase()
    for _ = 1, 10 do
        local first, parts = tree_id, {}
        for _ = 1, TREES_PER_LINE do
            local ok, location = pcall(GetTreeLocation, tree_id)
            if ok and location ~= nil and (location.x ~= 0 or location.y ~= 0) then
                tree_misses = 0
                parts[#parts + 1] = string.format('%.1f,%.1f,%.1f', location.x, location.y, location.z)
            else
                tree_misses = tree_misses + 1
                parts[#parts + 1] = '0,0,0'
            end
            tree_id = tree_id + 1
        end
        emit('trees', first .. ' ' .. table.concat(parts, ';'))
        if tree_misses >= MAX_TREE_MISSES then
            return 'grid'
        end
    end
    return 'trees'
end

local function grid_phase()
    -- The height grid may not be ready before the horn; wait for it rather than record garbage.
    local ok, level = pcall(GetHeightLevel, Vector(0, 0, 0))
    if (not ok or level == nil) and DotaTime() < 1 then
        return 'grid'
    end
    for _ = 1, ROWS_PER_THINK do
        if row >= HEIGHT then
            return 'runes'
        end
        local heights, passable = {}, {}
        local y = Y0 + (row + 0.5) * CELL
        -- 'e' marks a call that raised, 'n' a nil answer and 'x' anything but a level from 0 to 9.
        for col = 0, WIDTH - 1 do
            local location = Vector(X0 + (col + 0.5) * CELL, y, 0)
            local ok_height, height = pcall(GetHeightLevel, location)
            if not ok_height then
                heights[col + 1] = 'e'
            elseif height == nil then
                heights[col + 1] = 'n'
            elseif height >= 0 and height <= 9 and height == math.floor(height) then
                heights[col + 1] = tostring(height)
            else
                heights[col + 1] = 'x'
            end
            local ok_passable, is_passable = pcall(IsLocationPassable, location)
            if not ok_passable then
                passable[col + 1] = 'e'
            else
                passable[col + 1] = is_passable and '1' or '0'
            end
        end
        emit('height', row .. ' ' .. table.concat(heights))
        emit('passable', row .. ' ' .. table.concat(passable))
        row = row + 1
    end
    return 'grid'
end

local function runes_phase()
    local runes = {}
    for _, name in ipairs({'RUNE_POWERUP_1', 'RUNE_POWERUP_2', 'RUNE_BOUNTY_1', 'RUNE_BOUNTY_2', 'RUNE_BOUNTY_3', 'RUNE_BOUNTY_4'}) do
        if _G[name] ~= nil then
            local ok, location = pcall(GetRuneSpawnLocation, _G[name])
            runes[name] = ok and plain(location) or ('error ' .. tostring(location))
        end
    end
    emit('runes', dkjson.encode(runes))
    return 'buildings'
end

local function building(kind, team, index, unit)
    emit('building', dkjson.encode({
        kind = kind, team = team, index = index, name = unit:GetUnitName(), location = vec(unit:GetLocation()),
    }))
end

local function buildings_phase()
    for _, team in ipairs({TEAM_RADIANT, TEAM_DIRE}) do
        for index = 0, 10 do
            local unit = GetTower(team, index)
            if unit ~= nil then
                building('tower', team, index, unit)
            end
        end
        for index = 0, 5 do
            local unit = GetBarracks(team, index)
            if unit ~= nil then
                building('barracks', team, index, unit)
            end
        end
        local ancient = GetAncient(team)
        if ancient ~= nil then
            building('ancient', team, 0, ancient)
        end
    end
    return 'shops'
end

local function shops_phase()
    for _, name in ipairs({'SHOP_HOME', 'SHOP_SIDE', 'SHOP_SECRET', 'SHOP_SIDE2', 'SHOP_SECRET2'}) do
        if _G[name] ~= nil then
            for _, team in ipairs({TEAM_RADIANT, TEAM_DIRE}) do
                local ok, location = pcall(GetShopLocation, team, _G[name])
                emit('shop', dkjson.encode({
                    shop = name, team = team, location = ok and plain(location) or ('error ' .. tostring(location)),
                }))
            end
        end
    end
    return 'spawners'
end

local function spawners_phase()
    local ok, spawners = pcall(GetNeutralSpawners)
    if not ok or type(spawners) ~= 'table' then
        emit('spawner', dkjson.encode({error = tostring(spawners)}))
        return 'lanes'
    end
    for index, spawner in pairs(spawners) do
        local record = {index = index}
        if type(spawner) == 'table' then
            for key, value in pairs(spawner) do
                record[tostring(key)] = plain(value)
            end
        else
            record.value = plain(spawner)
        end
        emit('spawner', dkjson.encode(record))
    end
    return 'lanes'
end

local function lanes_phase()
    for _, name in ipairs({'LANE_TOP', 'LANE_MID', 'LANE_BOT'}) do
        local points = {}
        for k = 0, 20 do
            local location = GetLocationAlongLane(_G[name], k / 20)
            points[#points + 1] = {math.floor(location.x + 0.5), math.floor(location.y + 0.5)}
        end
        emit('lane', dkjson.encode({lane = name, points = points}))
    end
    return 'bounds'
end

local function bounds_phase()
    local results = {pcall(GetWorldBounds)}
    local bounds = {}
    for index = 2, #results do
        bounds[#bounds + 1] = results[index]
    end
    if #bounds == 1 and type(bounds[1]) == 'table' then
        bounds = bounds[1]
    end
    emit('bounds', dkjson.encode({ok = results[1], bounds = bounds}))
    emit('done', dkjson.encode({dota_time = DotaTime()}))
    return 'finished'
end

local phases = {
    globals = globals_phase, probe = probe_phase, trees = trees_phase, grid = grid_phase, runes = runes_phase,
    buildings = buildings_phase, shops = shops_phase, spawners = spawners_phase, lanes = lanes_phase,
    bounds = bounds_phase,
}

function Think()
    step = step + 1
    if GetTeam() ~= TEAM_RADIANT or step < 10 or phase == 'finished' then
        return
    end
    local ok, result = pcall(phases[phase])
    if ok then
        phase = result
    else
        emit('error', dkjson.encode({phase = phase, error = tostring(result)}))
        phase = 'finished'
    end
end
