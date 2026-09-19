# 新旧版本差异（Last Order 时代 7.23/7.24 → 2026-09 客户端）

实测环境：macOS，Dota 2 build 25329722（`steam.inf`: ClientVersion 6933，2026-09-15），
`scripts/probe_worldstate.py`，GUI 与 `-dedicated` 两种模式各跑一局 1v1 中路。
标 ✅ 的是实测确认，标 ⚠️ 的是从静态分析推断、还需要进一步验证。

## 结论

**通信层完全可用。** 软链接 `bots` 目录、`-botworldstatetosocket_*` socket 观测、Lua `loadfile`
动作文件、console.log 回执，这一整套机制在当前客户端上原样工作。需要改的都在**游戏内容层**
（技能、地图、物品）和 **Python 依赖层**。

## 1. 通信 / 引擎层

| 项目 | 旧版 | 新版 | 状态 |
|---|---|---|---|
| `vscripts/bots` 软链接加载自定义脚本 | 可用 | 可用（日志 `Radiant bot scripting using script path .../bots`） | ✅ |
| `-botworldstatetosocket_radiant/dire` | 12120/12121 | 不变 | ✅ |
| `-botworldstatetosocket_frames N` | 每 N tick 一帧 | 二进制里已搜不到该字符串，但**仍然生效**（N=6 → 0.2s，N=3 → 0.1s） | ✅ |
| `-botworldstatesocket_threaded` | 有 | 字符串搜不到，是否还有作用未知；保留无害 | ⚠️ |
| 包格式 | 4 字节小端长度 + protobuf | 不变；每帧仍是**全量**状态（约 45KB / 100+ 单位），不是增量 | ✅ |
| `-dedicated` 无 GUI | 可用 | macOS 上可用，地图加载约 3s（GUI 约 11s） | ✅ |
| `-fill_with_bots +map start gamemode 21` | 自动开 1v1 中路 | 不变，-90s 开始 PRE_GAME | ✅ |
| `game/dkjson`、`loadfile`、`DebugDraw*`、`Action_MoveDirectly` | 可用 | 可用（动作回执 254/254） | ✅ |
| 新增脚本入口 | — | 引擎会尝试加载 `bots/team_desires.lua`（找不到只打一行日志） | ✅ |
| 默认 bot 买装备 | 未核实 | **默认 AI 会自己买鞋/吃树**；已在 `item_purchase_generic.lua` 里用空 `ItemPurchaseThink` 屏蔽 | ✅ |
| 内置 bot AI 作为对手 | — | bot 脚本不定义 `Think()` 时由 Valve 默认 AI 接管，1v1 中路里会补刀、对线、击杀 | ✅ |
| `host_timescale` 加速 | 可用 | `-dedicated` 下 2×、4× 实测稳定（6 tick/步 → 20 step/s） | ✅ |
| 1v1 结束 | — | 任一方第 2 次死亡（含被塔杀）即原生结束：摧毁败方基地 → POST_GAME → **立刻停止推送 worldstate**，服务器随后退出；胜负只能从 console.log 的 `Building: npc_dota_*_fort destroyed` 读到 | ✅ |
| **复活后的英雄不上报** | 正常 | 英雄复活后只要还站在泉水没动，就**不出现在己方 worldstate 的 `units` 里**（`players[].is_alive` 为 true）；发一条移动指令后恢复。Lua 端一直正常 | ✅ |
| 每个端口的连接数 | — | 同一 worldstate 端口只接受一个客户端 | ✅ |
| Lua 阻塞与 `-nowatchdog` | dotaservice 原版靠 Lua 死循环等动作文件实现锁步 | Lua `Think()` 里循环 `loadfile` 阻塞时**整个游戏冻结**（游戏时间不走、worldstate 停发），解除后无损恢复。不带 `-nowatchdog`：阻塞满 **60 秒**进程被杀（`FATAL ERROR: Watchdog timeout exceeded in Lua script code`）；带 `-nowatchdog`：阻塞 90 秒仍存活并恢复。该参数字符串在二进制里搜不到但确实有效 | ✅ |
| GUI 模式看画面 | 控制台 `jointeam spec` | 同 | ⚠️ 未重新验证 |

## 2. Protobuf（`CMsgBotWorldState`）

来源：SteamDatabase/Protobufs `dota2/dota_gcmessages_common_bot_script.proto`。

- **`Actions` / `Action` 消息被 Valve 整个删除了。** 旧代码用它们构造动作再 `MessageToDict` 成 JSON。
  对我们没有功能影响（动作本来就是走 Lua 文件的 JSON），新环境直接用 Python dict，
  这对 LLM 输出 JSON 动作反而更自然。
- 新增 `valveextensions.proto` 依赖（`valve_map_field` / `valve_map_key` / `diff_encode_field`）。
  这些只是字段注解；实测 socket 上发的还是全量消息，`dropped_items_deltas` / `rune_infos_deltas` 为空。
- 字段变化：
  - `Vector.x/y/z`：required → optional
  - `hero_id`、`ability_id`、`item_id`：uint32 → **int32，默认 -1**
  - 各种 `*_handle`：默认值变为 `4294967295`（无目标时不再是 0）——**判空逻辑要改**
  - `glyph_cooldown_enemy`：uint32 → float
  - 新增：`Player.primary_unit_handle / mmr / location`，`Unit.is_specially_undeniable`，
    `Modifier.handle`，`TrackingProjectile.handle`，`CourierState` 新增神秘商店两个状态
- 用当前 proto 解析实测数据，**没有 unknown fields**，说明这份 proto 与客户端一致。
- 旧仓库的 `*_pb2.py` 是 protobuf 3.x 生成的，新版 protobuf 运行时无法加载；已用 protoc 29 重新生成。
  不再编译 `dota_shared_enums.proto`（它现在依赖一长串其它 proto），用到的几个常量放在 `constants.py`。

## 3. 游戏内容层（影响 agent，不影响环境）

- **影魔技能布局变了** ✅：
  槽位 0-2 毁灭阴影（5059-5061），槽位 3 是新技能 `nevermore_frenzy`（id 1216，Feast of Souls，
  技能名已由 Lua `GetAbilityInSlot():GetName()` 实测确认），槽位 4 Dark Lord(5063)，槽位 5 Requiem(5064)；
  **Necromastery(5062) 变成先天技能，在槽位 6，开局自动 1 级**。
  旧的 `nevermore.py` 加点顺序（先点 necromastery）和按槽位取技能的特征/动作掩码全部失效。
  技能列表末尾还多了 4 个 slot=0 的通用技能（id 5669/842/8873/2610），按 slot 索引时要过滤。
- **天赋名** ✅：全部换过。实测槽位 7-14：`special_bonus_unique_nevermore_7/4/3`、`..._frenzy_max_collection_count`、
  `..._nevermore_1/6`、`..._frenzy_castspeed`、`..._raze_procsattacks`；槽位 15 起是 `special_bonus_attributes`、
  `ability_capture`、`twin_gate_portal_warp`、`ability_lamp_use` 等通用技能。环境通过 Lua 在开局上报
  槽位→技能名（`env.unwrapped.ability_names()`），并支持 `"slot:N"` 加点，不再硬编码技能名。
- **金钱异常** ⚠️：实测中英雄第一次死亡后不久金钱从几百跳到 5000+，原因未查（可能是新版 1v1 模式或 bot 的补偿机制）。
- **地图** ✅/⚠️：中路一塔坐标没变（天辉 -1544,-1408 / 夜魇 524,652），但 7.33 起地图整体变大
  （实测单位范围 x∈[-8088, 8167]，泉水在 ±7400），新增前哨、双子门、莲花池等。
  旧的 `data/*7.24b*` gridnav/高度图不能再用，需要重新导出。
- **信使** ✅：每个玩家一个信使（`unit_type=11`，每队 5 个），但顶层 `couriers` 列表实测为空，
  信使信息要从 `units` 里取。
- **符文** ✅/⚠️：`rune_infos` 实测有 4 个点位（开局 type=-1）；新版符文种类和刷新规则与 7.24 不同，需重新核对。
- **物品**：TP 固定在物品槽位 15 ✅。⚠️ 旧的固定出装路线（`nevermore.py`）和禁用物品规则需要对照新版物品表重新核对。
- 填充位英雄仍可选 `npc_dota_hero_wisp`（hero_id 91）✅。

## 4. Python 层

| | 旧仓库 | dota2-env |
|---|---|---|
| Python | 3.8 | 3.10+（实测 3.12） |
| protobuf | 3.x 生成码 | protoc 29 生成码 + 运行时 7.x |
| 依赖 | tensorflow 2.3、psutil、PyYAML | 仅 protobuf |
| 路径 | 全部相对 CWD，游戏路径写在 `play_with_human_local.py` 顶部，`dota_game.py` 反向 import 它 | 包内定位 Lua；`DOTA_GAME_PATH` 环境变量或默认 Steam 路径 |
| 接口 | 手写 `Dota2Env.reset/step`，返回原始 protobuf | `gymnasium.Env`：Dict 观测/动作空间、action mask、reward、terminated/truncated，通过 `check_env` |
| 动作文件写入 | 直接写，靠 Lua 端容错；JSON 里有引号/反斜杠会破坏 Lua 字符串 | 先写临时文件再 `os.replace` 原子替换；对 `\` 和 `'` 转义 |
| socket 读取 | 分片接收时长度计算有 bug（`remain - len(data)` 用的是累计长度） | `_recv_exact` |
| 进程间传观测 | 直接传 protobuf 对象 | 传序列化 bytes |
| 日志 | import 时就在 CWD 创建 `dotapy.log` | 标准 `logging`，不落盘 |

## 5. 后续待办

1. 从新客户端导出 `items`（物品名 / 价格 / 禁用规则）；技能名已可在运行时从 Lua 拿到。
2. 重新导出地图 gridnav / 高度（如果 LLM agent 还需要局部地图的话）。
3. 验证 `HOST_MODE_GUI_MENU`（人类自建房间对战）流程在新版大厅 UI 下是否还能把 bot 脚本指到本地 `bots`。
4. `host_timescale` 4× 以上的上限；多实例并行（端口、`pkill`、`bots` 软链接目前都是全局的）。
5. 查清死亡后金钱暴涨的原因。
6. 观测里还没有：物品栏、modifier、投射物、树/地形；动作里还没有：物品使用、指向地点的技能、信使。
