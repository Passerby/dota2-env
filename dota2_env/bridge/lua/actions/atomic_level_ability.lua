-------------------------------------------------------------------------------
--- AUTHOR: Nostrademous
-------------------------------------------------------------------------------

local LevelAbility = {}

LevelAbility.Name = "Level Ability"
LevelAbility.NumArgs = 3

-------------------------------------------------

-- nKeep: points to leave unspent, which python keeps back for the talents the agent picks itself. The
-- client silently refuses a talent of a tier the hero has not reached or has already chosen from, although
-- CanAbilityBeUpgraded says yes (docs/VERSION_DIFF.md 3.2), so python checks the tiers.
function LevelAbility:Call( hHero, sAbilityName, nKeep )
    -- "slot:N" levels whatever ability sits in slot N
    local slot = string.match(sAbilityName[1], "^slot:(%d+)$")
    if slot ~= nil then
        local hSlotAbility = hHero:GetAbilityInSlot(tonumber(slot))
        if hSlotAbility == nil then
            print("[ERROR] - no ability in slot", slot)
            do return end
        end
        sAbilityName = {hSlotAbility:GetName()}
    end
    print("Leveling: ", sAbilityName[1])
    -- Sanity Check
    local nAbilityPoints = hHero:GetAbilityPoints()
    if nAbilityPoints > (nKeep[1] or 0) then
        -- Another sanity check
        local hAbility = hHero:GetAbilityByName(sAbilityName[1])
        if hAbility and hAbility:CanAbilityBeUpgraded() then
            -- actually do the leveling
            hHero:ActionImmediate_LevelAbility(sAbilityName[1])
            -- print("EVENT LevelAbility Successful ", hHero:GetPlayerID(), sAbilityName[1])
        else
            print("Trying to level an ability I cannot", sAbilityName[1])
            do return end
        end
    end
end

-------------------------------------------------

return LevelAbility
