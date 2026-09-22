-- The server VM issues the two orders of a vector cast (bots/server_actions.lua). The bot only gives up
-- whatever it was doing, the way any other cast replaces its queue.
local CastVector = {}

CastVector.Name = "Cast Vector"
CastVector.NumArgs = 4

function CastVector:Call( hUnit, intAbilitySlot, vLoc, vDir )
    hUnit:Action_ClearActions(false)
    local vStart = Vector(vLoc[1], vLoc[2], vLoc[3])
    DebugDrawLine(vStart, Vector(vStart.x + vDir[1] * 300, vStart.y + vDir[2] * 300, vStart.z), 255, 0, 0)
end

return CastVector
