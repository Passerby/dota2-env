# 服务器 VM：普通对局里能用的自定义游戏脚本

实测环境：macOS（arm64），Dota 2 `steam.inf` ClientVersion **6937 / VersionDate 2026-09-22**，`-dedicated`，
1v1 中路，`host_timescale 2`。脚本：[`scripts/probe_server_vm.py`](../scripts/probe_server_vm.py)。
✅ = 本机实测，⚠️ = 没实测、推断，或者要开窗口才能确认。

## 结论

Dota 进程里有两个互不相通的 Lua VM。环境一直用 bot VM 下指令，2026-09 起也把 `server_actions.lua` 放进了服务器 VM
（[IPC_CHANNELS.md](IPC_CHANNELS.md) 2.1）。服务器 VM 就是自定义游戏（游廊）跑的那个 VScript VM，**在普通对局里大部分也能用**：

| | bot VM | 服务器 VM |
|---|---|---|
| 环境里谁在用 | `bot_<hero>.lua`（`bot_controlled.lua.tpl`）、`hero_selection.lua` | `bridge/lua/server_actions.lua` |
| 本来是给谁的 | 机器人脚本，每队一份 | 自定义游戏 |
| 怎么加载 | 引擎按 `vscripts/bots` 自动加载 | `+servercfgfile` 指向的 cfg 里一行 `script_reload_code bots/server_actions` |
| 本机的规模 | 153 个全局函数，英雄句柄上 217 个方法 ✅ | 236 个带说明的全局函数，80 个类共 1719 个方法，51 个枚举 ✅ |
| 看得到什么 | 本队视野 | 整个服务器，没有迷雾 |
| 能做什么 | 给自己的英雄下指令 | 给任意单位下指令、改状态、刷单位、聊天、监听游戏事件、服务器控制台、暂停 |

- 两边都没有 `io` / `os` ✅，给 Python 回话只能 `print` 进 console.log。按 API 说明，`InitLogFile` / `AppendToLogFile` 已经废弃，
  说明里写的是 "Print to the console for logging instead"。
- 普通对局里 `GameRules:GetGameModeEntity()` 是 nil ✅。所以 `CDOTABaseGameMode` 的 159 个方法（所有 `Set*Filter`、
  `SetFogOfWarDisabled` 之类）都用不了。这是查文档最容易踩的坑：这些文档本来是给自定义游戏写的。
- 服务器 VM 的 `GameRules:State_Get()` 用新的状态编号（`PRE_GAME` = 8、`GAME_IN_PROGRESS` = 10）✅，和 worldstate 的枚举
  （`bridge/constants.py`，`PRE_GAME` = 4）不同。脚本里用 VM 自带的 `DOTA_GAMERULES_STATE_*` 常量就不会错。

## 1. 函数从哪来、文档在哪

- 函数是 `libserver.dylib` 用 C++ 注册进 VM 的，每个都带一句 Valve 写的说明，所以 `strings libserver.dylib` 能直接搜到
  "Have Entity say string, and teamOnly or not" 这样的句子。`_G` 里一共有 343 个函数，比 236 多出来的是 Lua 标准库，以及
  `game/core/scripts/vscripts/init.lua` 等 core 脚本里用 Lua 写的辅助函数（`DumpScriptBindings`、`ScriptFunctionHelp`……）。
- **本机这个版本的完整参考**：`python scripts/probe_server_vm.py --dump-api vscript_api.lua`（无头，约 30 秒）。签名和说明存在
  `FDesc` / `CDesc` / `EDesc` 三张表里，**只有 developer mode 才会填**：脚本在启动行加 `-dev +developer 1`，再调
  `DumpScriptBindings()` ✅。不加的话这三张表是 nil，从脚本 `SendToServerConsole('script_help2')` 也什么都不输出 ✅。导出格式是
  LuaDoc 风格，每条是 `---[[ 名字  说明 ]]`、`-- @param`、`function 名字(...) end`。参数大多只有类型、没有名字，说明里常写着参数顺序，例如
  `DebugDrawText`：`Draw text in 3d (origin, text, bViewCheck, duration)`。
- **游戏事件**：VPK 里的 `resource/core.gameevents`（94 个）和 `resource/game.gameevents`（276 个），每个事件的字段和类型都有
  （`player_chat`：`teamonly` / `userid` / `playerid` / `text`；`dota_match_done`：`winningteam`；`last_hit`：`PlayerID` /
  `EntKilled` / `FirstBlood` / `HeroKill` / `TowerKill`）。用 `dota2_env.bridge.game.read_vpk` 读 `game/core/pak01_dir.vpk`
  和 `game/dota/pak01_dir.vpk` 就能拿到。
- **社区文档**：[ModDota API](https://moddota.com/api/) 的数据来自 [ModDota/dota-data](https://github.com/ModDota/dota-data)，
  做法相同（`-tools` 加一个 dumper addon 读这三张表），参数名补得更全，还有人工修正，但只更新到 2026-04-26（0.47.2）。
  bot VM 的 API 见 [VDC: Dota Bot Scripting](https://developer.valvesoftware.com/wiki/Dota_Bot_Scripting) 和
  [ModDota lua_bots](https://docs.moddota.com/lua_bots/)，两份都比较旧。

## 2. 聊天

| 发法 | 引擎自己的聊天日志 | `player_chat` 事件 |
|---|---|---|
| bot VM `ActionImmediate_Chat(text, all)`（LLM 的 `SAY` 行现在走这个） | 无 ✅ | 无 ✅ |
| 服务器 `Say(PlayerResource:GetPlayer(pid) 或英雄实体, text, false)` | `[All Chat][Agent_R0 (Bot) (0)]: text`，中文完整 ✅ | 有，`playerid` 0 ✅ |
| 服务器 `Say(英雄, text, true)` | `[Allies Chat][Agent_R0 (Bot) (0)]: text` ✅ | 有，`teamonly` 1 ✅ |
| 服务器 `Say(nil, text, false)` / `SendToServerConsole('say text')` | `[All Chat][Console (0)]: text` ✅ | 有，`playerid` -1 ✅ |
| `GameRules:SendCustomMessage` / `SendCustomMessageToTeam` | 无 ✅ | 无 ✅ |

- **发**：服务器的 `Say` 让 bot 玩家以自己的名义发出正式的全体或队伍聊天，走的是真人发言的同一条路。bot 的 `ActionImmediate_Chat`
  调用成功，但服务器端不留任何痕迹。binary 里有 `CDOTAUserMsg_BotChat`，推测它只发给客户端 ⚠️。所以无头时没法验证，
  屏幕上显不显示要开窗口看 ⚠️。
- **收**：`ListenToGameEvent('player_chat', ...)` 能拿到每一条走聊天通道的消息（`playerid`、`text`、`teamonly`）。按事件定义，
  真人玩家发言也是这个事件（GUI 模式里的观战者、`HOST_MODE_GUI_MENU` 里的玩家），但没有实测 ⚠️。这个事件还没接进环境；
  接进来之后 LLM 就能读到聊天（[LLM_MATCH.md](LLM_MATCH.md) §10）。

## 3. 其他实测能用的

| 能力 | 做法 | 结果 |
|---|---|---|
| 游戏事件 | `ListenToGameEvent(name, fn, nil)` | `game_rules_state_change`、`npc_spawned`、`dota_on_hero_finish_spawn`、`dota_player_gained_level`、`dota_hero_inventory_item_change`、`entity_hurt`、`entity_killed`、`last_hit` 都带着字段触发了 ✅；`dota_match_done` 要等对局结束，没测 ⚠️ |
| 瞬移 | `FindClearSpaceForUnit(hero, Vector(x, y, z), true)` | ✅ |
| 升级 | `hero:HeroLevelUp(false)` 调 5 次 | 1 → 6 级，每级触发一次 `dota_player_gained_level` ✅ |
| 金钱 | `PlayerResource:ModifyGold(pid, 1234, true, 0)` | 600 → 1834 ✅ |
| 物品 | `hero:AddItemByName('item_blink')` | ✅ |
| 刷单位 | `CreateUnitByName('npc_dota_creep_badguys_melee', pos, true, nil, nil, DOTA_TEAM_BADGUYS)` | 刷出来马上开打，随后被一塔打死 ✅ |
| 血量 | `hero:SetHealth(123)` | ✅ |
| bot 能不能看到这些变化 | 上面每一项 | bot VM 在下一个 `Think` 就看到新的等级、金钱、血量、物品和位置 ✅ |
| 跳过开局 | `SendToServerConsole('dota_dev forcegamestart')` | 游戏时间从 -62 直接跳到 0，进入 GAME_IN_PROGRESS ✅。`dota_dev` 还有 `hero_refresh`、`hero_level`、`player_givegold`、`good_guys_win` 等子命令（binary 里的字符串，没有逐个试 ⚠️） |
| 捡神符 | `ExecuteOrderFromTable({UnitIndex = 英雄, OrderType = DOTA_UNIT_ORDER_PICKUP_RUNE, TargetIndex = 神符实体})`，实体用 `Entities:FindByClassnameNearest('dota_item_rune', 点位, 400)` 找 | 英雄走过去捡起来 ✅。bot API 的 `Action_PickUpRune(RUNE_POWERUP_2)` 坏了，环境的 `PICKUP_RUNE` 因此改由 `server_actions.lua` 这样发（[VERSION_DIFF.md](VERSION_DIFF.md) 3.2） |
| 运行中改倍速 | `Convars:SetFloat('host_timescale', 0.25)` / `SendToServerConsole('host_timescale 1')` | 实测 0.26× / 0.98×，改回 2 后是 1.95–2.0× ✅ |

有了这些，真机测试可以直接摆出要测的局面，不用等它自然出现。比如 [VERSION_DIFF.md](VERSION_DIFF.md) 3.1 的矢量施法实验，
就不必站在泉水边等回蓝。目前还没做成环境功能。

## 4. 暂停：不用空转的锁步

`PauseGame(true)` 之后的情况 ✅：

- 游戏时间停住，bot VM 的 `Think` 不再被调用，worldstate 也停发。暂停 4 秒，就有 4.1 秒收不到帧，恢复后立刻接上。
- 服务器 VM 里用 `SetThink` 注册、返回 0.03 的 thinker 也停了（暂停期间 0 次）；**用 `SetContextThink` 注册、返回 0 的 thinker 仍然每帧都跑**
  （4 秒里 241–242 次）。
- 由那个 thinker 调 `PauseGame(false)`，26–31 ms 后游戏时间就开始走，没有 3 秒倒计时。

所以"Python 慢了就让游戏等它"的锁步可以不靠 Lua 自旋。[IPC_CHANNELS.md](IPC_CHANNELS.md) §4.1 原来的设想是自旋，代价是占满一个核。
现在的做法可以是：服务器 VM 发现动作文件的 `dotaTime` 比游戏时间落后太多，就 `PauseGame(true)`；新文件一到，就 `PauseGame(false)`。
这还没做成环境功能。LLM 对局"游戏不等人"是有意的设计（[LLM_MATCH.md](LLM_MATCH.md) §5），所以即使做了也只该是个开关。

## 5. 头顶文字：`ACTION_LABEL`，LLM 对局的 REASON

- Python 用 `env.unwrapped.queue_label(row, text)`（1v1 是 `queue_label(text)`）把一条 `ACTION_LABEL` 放进下一步的 `extra_actions`。
- bot 跳过这条动作；`server_actions.lua` 收到时调一次 `hero:SetCustomHealthLabel(text, 255, 255, 255)`，之后不再管它。
- 这个标签是英雄实体上的联网字符串 `m_CustomHealthLabel`（服务器和客户端的库里都是 `CNetworkStringBase<256>`，6938），
  客户端自己把它画在血条上方，所以文字跟着英雄模型走，不用服务器重画；下一条 label 覆盖上一条。
- 字符串最多 255 字节（再加结尾的 NUL），中文 3 字节一个字，也就是 85 个字。`actions.label` 按这个长度在字符之间截断，
  免得引擎从一个字的中间切开；完整的 REASON 仍在 transcript 里。
- LLM 对局里 `show_reason: true`（默认开）会把每份回复的 REASON 这样送过去（[LLM_MATCH.md](LLM_MATCH.md) §2）。

**为什么不用 `DebugDrawText`**：第一版每个 think 用 `DebugDrawText(origin, text, false, 0.05)` 画在英雄血条上方 50 处。
无头实测一切正常（每游戏秒调用 30 次，位置跟着英雄）；2026-09-24 开窗口看：中文能显示，但文字一直在抖，
走动时最厉害，像是被反复清掉再出现。原因是它画在固定的世界坐标上，只能每个服务器 tick 按英雄当时的位置重画一份、
每份只活 0.05 游戏秒；客户端画英雄是逐帧插值的，文字却一格一格地跳，新旧两份还会叠在一起，加速跑时一份的寿命
（4 倍速下 12.5 毫秒墙钟）还不到一帧。

**实测**：`SetCustomHealthLabel` 在普通对局的服务器 VM 里能调，无头 1v1 连发三条中文 label（最后一条 85 字）没有脚本报错 ✅；
窗口里的样子（字号、长句是不是一行、颜色）还没看过 ⚠️。备选：
- `DebugScreenTextPretty`：能指定字体和字号，画在屏幕上的固定位置，不跟英雄，也就不会抖。
- 服务器 `Say(英雄, reason, true)`：发进队伍聊天（第 2 节）。

## 6. 复现

```bash
.venv/bin/python scripts/probe_server_vm.py --timescale 2               # 第 2–4 节，约 1 分钟
.venv/bin/python scripts/probe_server_vm.py --dump-api vscript_api.lua   # 第 1 节的 API 参考，约 30 秒
```

两个模式都是无头运行，不改仓库里的 Lua：探针脚本复制进会话目录，`+servercfgfile` 指向它，只在这一次运行里生效。聊天用的文字以 `zq-` 开头，
这样引擎自己写的聊天日志行和探针的 `print` 就能分开。
