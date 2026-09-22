-------------------------------------------------------------------------------
--- AUTHOR: Nostrademous
-------------------------------------------------------------------------------
local behavior = require( "bots/behavior" )

local UseAbility = {}

UseAbility.Name = "Use Ability"
UseAbility.NumArgs = 3

-------------------------------------------------
function UseAbility:Call( hUnit, intAbilitySlot, iType )
    local hAbility
    if intAbilitySlot[1] >= 0 then
        hAbility = hUnit:GetAbilityInSlot(intAbilitySlot[1])
    else
        hAbility = hUnit:GetItemInSlot(-intAbilitySlot[1] - 1)
    end

    if not hAbility then
        print('[ERROR]: ', hUnit:GetUnitName(), " failed to find ability in slot ", intAbilitySlot[1])
        do return end
    end

    -- Without a target, whatever needs one goes on the hero itself: a tango on the nearest tree, a salve or
    -- a friendly spell on the hero, an item aimed at the ground (a ward, a branch) at its feet.
    if not behavior.Has(hAbility, ABILITY_BEHAVIOR_NO_TARGET) then
        if hAbility:GetName() == 'item_tango' then
            local tree = hUnit:GetNearbyTrees(700)[1]
            if tree ~= nil then
                hUnit:Action_UseAbilityOnTree(hAbility, tree)
            else
                print("UseAbility can not find tree")
            end
        elseif behavior.Has(hAbility, ABILITY_BEHAVIOR_UNIT_TARGET) then
            hUnit:Action_UseAbilityOnEntity(hAbility, hUnit)
        elseif intAbilitySlot[1] < 0 and behavior.Has(hAbility, ABILITY_BEHAVIOR_POINT) then
            hUnit:Action_UseAbilityOnLocation(hAbility, hUnit:GetLocation())
        else
            print("UseAbility: nothing to use without a target", hAbility:GetName())
        end
        do return end
    end

    iType = iType[1]

    -- Note: we do not test for range, mana/cooldowns or any debuffs on the hUnit (e.g., silenced).

    if iType == nil or iType == ABILITY_STANDARD then
        hUnit:Action_UseAbility(hAbility)
    elseif iType == ABILITY_PUSH then
        hUnit:ActionPush_UseAbility(hAbility)
    elseif iType == ABILITY_QUEUE then
        hUnit:ActionQueue_UseAbility(hAbility)
    end
end
-------------------------------------------------

return UseAbility
