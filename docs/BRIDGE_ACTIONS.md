# Bridge 层动作 JSON（`DotaSession.act` / `DotaGame.write_action`）

Gym 环境会替你生成这些；只有直接用 `dota2_env.bridge` 或想扩展动作空间时才需要看。

```jsonc
{
  "dotaTime": -79.8,            // 必须每步不同，Lua 会跳过已执行过的 dotaTime；且要早于当前 DotaTime 至少 0.07s
  "extraData": "###42###",      // 回执用的唯一 key，格式必须是 ###<id>###
  "actions": [ {...} ],         // 每个 player 每步只执行第一条
  "extra_actions": {"actions": [ {...} ]},   // 加点、买物品、聊天等，全部执行
  "draw": [ {"type": "circle", "x":0,"y":0,"z":0,"radius":100,"r":255,"g":0,"b":0} ]
}
```

`actionType` 与参数字段（见 `bridge/lua/bot_controlled.lua.tpl` 的 `act``）：

| actionType | 字段 |
|---|---|
| `DOTA_UNIT_ORDER_NONE` / `STOP` / `GLYPH` / `BUYBACK` | — |
| `DOTA_UNIT_ORDER_MOVE_TO_POSITION` | `moveToLocation.location.{x,y}` |
| `DOTA_UNIT_ORDER_MOVE_DIRECTLY` | `moveDirectly.location.{x,y}` |
| `DOTA_UNIT_ORDER_ATTACK_TARGET` | `attackTarget.{target,once}` |
| `DOTA_UNIT_ORDER_CAST_POSITION` | `castLocation.{abilitySlot,location}` |
| `DOTA_UNIT_ORDER_CAST_TARGET` | `castTarget.{abilitySlot,target}` |
| `DOTA_UNIT_ORDER_CAST_TARGET_TREE` | `castTree.{abilitySlot,tree}` |
| `DOTA_UNIT_ORDER_CAST_NO_TARGET` | `cast.abilitySlot` |
| `DOTA_UNIT_ORDER_CAST_TOGGLE` | `castToggle.abilitySlot` |
| `DOTA_UNIT_ORDER_TRAIN_ABILITY` | `trainAbility.ability`（技能名） |
| `DOTA_UNIT_ORDER_PURCHASE_ITEM` | `purchaseItem.itemName` |
| `DOTA_UNIT_ORDER_PICKUP_RUNE` / `PICKUP_ITEM` / `DROP_ITEM` | `pickUpRune.rune` / `pickUpItem.itemId` / `dropItem.{slot,location}` |
| `ACTION_CHAT` / `ACTION_COURIER` / `ACTION_SWAP_ITEMS` | `chat.{message,toAllchat}` / `courier.action` / `swapItems.{slotA,slotB}` |

`trainAbility.ability` 除技能名外也接受 `"slot:N"`（按槽位加点，不依赖版本相关的技能名）。
每个动作文件必须包含被控玩家的一条主动作（什么都不做就发 `DOTA_UNIT_ORDER_NONE`），否则 Lua 不会把该文件标记为已执行，`extra_actions` 会每 tick 重复执行。
