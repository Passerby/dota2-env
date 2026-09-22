-- What a skill or item can be aimed at, read from its behaviour flags. Shared by the cast actions and
-- by the SLOTS report that python builds its action mask from.
local Behavior = {}

-- The flags are single bits of a plain number, so arithmetic does what a bitwise and would.
local function HasFlag( nBits, nFlag )
    return math.floor(nBits / nFlag) % 2 == 1
end

function Behavior.Has( hAbility, nBehavior )
    return HasFlag(hAbility:GetBehavior(), nBehavior)
end

-- In the words python's mask uses: no_target, self (our own hero), enemy (an enemy unit), point (the
-- ground). Empty for a passive. vector rides along on a two-point skill: no bot order carries the second
-- point, so python sends such a cast as DOTA_UNIT_ORDER_CAST_VECTOR and bots/server_actions.lua issues it
-- from the server VM (docs/VERSION_DIFF.md 3.1); the mask does not change.
function Behavior.Kinds( hAbility )
    local kinds = {}
    if Behavior.Has(hAbility, ABILITY_BEHAVIOR_NO_TARGET) then
        table.insert(kinds, 'no_target')
    end
    if Behavior.Has(hAbility, ABILITY_BEHAVIOR_UNIT_TARGET) then
        local nTeam = hAbility:GetTargetTeam()
        -- a tango targets trees (a custom team); used without a target it finds the nearest one itself
        if HasFlag(nTeam, ABILITY_TARGET_TEAM_FRIENDLY) or hAbility:GetName() == 'item_tango' then
            table.insert(kinds, 'self')
        end
        if HasFlag(nTeam, ABILITY_TARGET_TEAM_ENEMY) then
            table.insert(kinds, 'enemy')
        end
    end
    if Behavior.Has(hAbility, ABILITY_BEHAVIOR_POINT) then
        table.insert(kinds, 'point')
    end
    if Behavior.Has(hAbility, ABILITY_BEHAVIOR_VECTOR_TARGETING) then
        table.insert(kinds, 'vector')
    end
    return kinds
end

return Behavior
