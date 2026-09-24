-- The server VM picks the rune up (bots/server_actions.lua): on 6937 Action_PickUpRune(RUNE_POWERUP_2) walks to
-- the other power rune or nowhere (docs/VERSION_DIFF.md 3.2). The bot only gives up whatever it was doing, the
-- way any other order replaces its queue.
local PickUpRune = {}

PickUpRune.Name = "Pick Up Rune"
PickUpRune.NumArgs = 1

function PickUpRune:Call( hUnit )
    hUnit:Action_ClearActions(false)
end

return PickUpRune
