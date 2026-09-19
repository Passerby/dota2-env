local config = require("bots/config")

-- Agent-controlled and idle heroes must not shop on their own: on current clients the built-in
-- bot AI otherwise buys items (boots, tango, ...) next to whatever the python agent purchases.
if config.control[tostring(GetTeam())] ~= "builtin" then
    function ItemPurchaseThink()
    end
end
