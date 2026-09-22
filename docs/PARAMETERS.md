# 环境参数一览

所有和运行环境有关的"旋钮"：Dota 启动参数、端口与路径、传给 Lua 的配置、Gym 环境参数、以及写死在代码里的时序常量。
"验证"一列：✅ = 在 2026-09 客户端（build 25329722，macOS）上实测过效果；➖ = 沿用自 dotaservice / Last Order，
本项目没有单独验证它的作用；⚠️ = 有已知疑点。实测细节见 [VERSION_DIFF.md](VERSION_DIFF.md)。

## 1. Dota 启动参数（`bridge/game.py: DotaGame.launch_args`）

### 观测与日志

| 参数 | 值 | 作用 | 验证 |
|---|---|---|---|
| `-botworldstatetosocket_radiant` | `12120` | 在该 TCP 端口推送天辉视角的 `CMsgBotWorldState` | ✅ |
| `-botworldstatetosocket_dire` | `12121` | 同上，夜魇视角 | ✅ |
| `-botworldstatetosocket_frames` | `ticks_per_observation` | 每 N 个 tick 推一帧（30 tick = 1 游戏秒）。二进制里搜不到该字符串，但 N=6 → 0.2s、N=3 → 0.1s 实测生效 | ✅ |
| `-botworldstatesocket_threaded` | — | 原意是在独立线程里发送 worldstate。字符串搜不到，是否还有作用未知 | ⚠️ |
| `-con_logfile` | `<session>/bots/console.log` | 控制台日志落盘。Lua 的 `print`（`LUARDY`、`ACK`、`sync key`）、比赛结束（`Building: ..._fort destroyed`）、看门狗报错都从这里读 | ✅ |
| `-con_timestamp` | — | 日志每行带时间戳 | ✅ |
| `-console` | — | 启用控制台 | ➖ |

### 运行方式

| 参数 | 值 | 作用 | 验证 |
|---|---|---|---|
| `-dedicated` | 仅 `HOST_MODE_DEDICATED` | 无 GUI 的专用服务器。macOS 可用，地图加载约 3s（GUI 约 11s） | ✅ |
| `-fill_with_bots` | DEDICATED / GUI | 空位全部用 bot 填满（每队按 `heroes` 顺序发英雄，发完了剩下的位置是挂机小精灵：1v1 每队 1 个真英雄 + 4 个小精灵，5v5 每队 5 个） | ✅ |
| `+map start gamemode N` | DEDICATED / GUI | 直接开图；21 = `DOTA_GAMEMODE_1V1MID`，1 = `DOTA_GAMEMODE_AP`（5v5 全阵营）。两种模式都是 -90s 进入 PRE_GAME | ✅ |
| `+sv_lan` | `1`（DEDICATED / GUI）、`0`（GUI_MENU） | 局域网服务器 | ➖ |
| `-novid` | 仅 `HOST_MODE_GUI_MENU` | 跳过开场视频 | ➖ |
| `+host_timescale` | `timescale` | 游戏速度倍率。`-dedicated` 下 2×、4× 实测稳定；<1 可让游戏慢下来迁就慢策略（未测） | ✅（1/2/4） |
| `-nowatchdog` | — | 关闭"Lua 阻塞看门狗"。**不带**：Lua 连续阻塞满 60 秒进程被杀（`FATAL ERROR: Watchdog timeout exceeded in Lua script code, exiting`）；**带**：阻塞 90 秒仍存活并无损恢复。阻塞期间整个游戏冻结（游戏时间不走、worldstate 停发）。锁步模式依赖它 | ✅ |
| `+sv_cheats` | `1` | 允许作弊指令 / 调试绘制（`DebugDraw*`） | ➖ |
| `+sv_hibernate_when_empty` | `0` | 没有真人连接时服务器不休眠——无 GUI 跑 bot 对局必需 | ➖ |
| `+dota_1v1_skip_strategy` | `1` | 跳过 1v1 的策略阶段 | ➖ |
| `+dota_surrender_on_disconnect` | `0` | 断线不判负 | ➖ |
| `-insecure` | — | 不启用 VAC（加载自定义脚本需要） | ➖ |
| `-noip` | — | 不绑定外部 IP | ➖ |
| `+clientport` | `27006` | 客户端端口，避开 Steam 占用 | ➖ |
| `+hostname` | `dota2_env` | 服务器名。会出现在录像文件名里 | ✅ |
| `+servercfgfile` | `dota2_env_server.cfg` | 服务器激活时 exec 的 cfg，环境写在 `<dota>/game/dota/cfg/` 下，内容是 `script_reload_code bots/server_actions`：把 `bridge/lua/server_actions.lua` 加载进服务器 VM（矢量施法靠它）。启动行上的 `+script_reload_code` 在地图加载前执行，静默丢掉 | ✅ |
| `+lservercfgfile` | `dota2_env_server.cfg` | 同上，listen server（GUI 两种 mode）用的那个 convar | ➖ |

### 录像（只在 `replay_dir` 非 None 时加）

| 参数 | 值 | 作用 | 验证 |
|---|---|---|---|
| `+tv_enable` | `1` | 打开 GOTV | ✅ |
| `+tv_autorecord` | `1` | 开局自动录像，写到 `<dota>/dota/replays/auto-<日期>-<时分>-<地图>-<hostname>.dem` | ✅ |

⚠️ **刻意不加 `+tv_delay`。** GOTV 开始广播的那一刻客户端就段错误崩掉（`HLTVServerAsync` 线程），而 demo
里的画面又是广播开始之后才写的：`tv_delay 0` 一帧都收不到，`tv_delay 3600` 不崩但 demo 里没有任何对局
内容。不加就是这条取舍上最不坏的点（崩之前能录到约 110 秒画面）。六种组合的实测对照和崩溃栈见
[VERSION_DIFF.md](VERSION_DIFF.md) 1.2 节。`+tv_title` / `+tv_transmitall` 没有加，前者只影响广播标题。

### 三种 host mode

| mode | 对应 | 用途 |
|---|---|---|
| `HOST_MODE_DEDICATED` | `render_mode=None` / `"ansi"` | 无 GUI，自动开局。训练、自动化测试默认用这个 |
| `HOST_MODE_GUI` | `render_mode="human"` | 有画面，自动开局。进图后 `server_actions.lua` 从服务器控制台发 `jointeam spec`，真人自动进观战视角（2026-09 实测 ✅，见 [VERSION_DIFF.md](VERSION_DIFF.md) 1.3） |
| `HOST_MODE_GUI_MENU` | 仅 bridge 层 | 只打开客户端，人类手动建房，用于人机对战（⚠️ 新版大厅流程未验证） |

## 2. 端口、路径、环境变量

| 项 | 值 | 说明 |
|---|---|---|
| worldstate 端口 | 12120（天辉）/ 12121（夜魇） | `DotaGame.PORT_WORLDSTATES`。每个端口**只接受一个连接** ✅；端口固定是"一台机器只能跑一个实例"的原因之一 |
| `DOTA_GAME_PATH` | 环境变量 | 指到 `.../dota 2 beta/game`；不设则用各平台 Steam 默认路径（`DEFAULT_GAME_PATHS`），也可传 `dota_path=` |
| 会话目录 | `$TMPDIR/dota2_env_<game_id>/bots/` | 每次 `reset()` 新建：拷入 Lua、`config_auto.lua`、动作文件、`console.log`。`close()` 时删除，`keep_files=True` 保留 |
| bots 软链接 | `<dota>/game/dota/scripts/vscripts/bots` → 会话目录 | 已存在真实目录则拒绝运行；残留的旧软链接会被替换；`close()` 时移除。Windows 需要管理员权限 |
| 服务器 cfg | `<dota>/game/dota/cfg/dota2_env_server.cfg` | 一行 `script_reload_code bots/server_actions`，构造 `DotaGame` 时写入、`close()` 时删除（`remove_dota_files`） |
| 录像目录 | `<dota>/dota/replays/` | 客户端自己写 `.dem` 的地方（属于 Dota 安装目录）。`close()` 把这一局的那个文件 **move** 到 `replay_dir`，不会留在游戏目录里 |
| `replay_dir` | 由调用方给，相对路径相对当前工作目录 | 例如 `"replays"`；`close()` 之后路径在 `env.unwrapped.replay_path` |
| 进程 | 启动前 `pkill -x dota2` 并等它退干净 | 会杀掉机器上所有 Dota 进程，然后最多等 15 秒直到没有 `dota2` 进程（否则 SIGKILL）：上一局还在退出时启动新实例，新实例会在启动阶段自己退出。`close()` 只停自己那个进程组（`dota.sh` 壳 + 游戏本体：SIGTERM → 等待 → 超时才 SIGKILL） |

## 3. 传给 Lua 的配置（`bots/config_auto.lua`，由 `DotaGame._write_config` 生成）

| 键 | 来源 | 作用 |
|---|---|---|
| `game_id` | 自动（uuid） | 会话标识 |
| `ticks_per_observation` | 同启动参数 | Lua 端目前只做存在性校验 |
| `heroes` | `{"2": [英雄名, ...], "3": [...]}` | `hero_selection.lua` 按 `GetTeamPlayers()` 的顺序给每队的 bot 发英雄（第 i 个 player 拿第 i 个名字，发完了补 `npc_dota_hero_wisp`）；每个英雄名对应生成一份 `bot_<hero>.lua`。这个顺序就是 5v5 观测里的行号顺序 ✅ |
| `control` | `{"2": mode, "3": mode}` | `agent`：执行 Python 写的动作文件；`builtin`：不定义 `Think()`，交给 Valve 默认 AI ✅；`idle`：空 `Think()`，站着不动。非 `builtin` 的队伍同时屏蔽默认买装备（`ItemPurchaseThink` 置空）✅ |

## 4. Gym 环境参数（`gym.make("dota2_env/Mid1v1-v0", ...)`，1v1）

| 参数 | 默认 | 说明 |
|---|---|---|
| `render_mode` | `None` | 见上面的 host mode；`"ansi"` 时 `render()` 返回文本观测 |
| `team_id` | `TEAM_RADIANT`(2) | 控制方；`TEAM_DIRE`(3) 也实测过 ✅ |
| `hero` / `opponent_hero` | `npc_dota_hero_nevermore` | 英雄单位名（只测过影魔） |
| `opponent` | `"builtin"` | `"builtin"` / `"idle"` |
| `timescale` | `1.0` | → `+host_timescale` |
| `ticks_per_observation` | `6` | → `-botworldstatetosocket_frames`；一步 = 这么多 tick |
| `reward_fn` | `LaningReward()` | 权重见 `rewards.py: DEFAULT_WEIGHTS`，可传 `LaningReward(weights={...})` 局部覆盖 |
| `rules` | `Mid1v1Rules(deaths_to_lose=2, max_dota_time=600)` | 终止 / 截断条件 |
| `starting_items` | 吃树、仙灵火、2 树枝、圆环 | 第一步自动购买；`()` 关闭 |
| `ability_priority` | `(5, 0, 3, 4, 1, 2)` | 有技能点时按此槽位顺序尝试加点（Lua 跳过当前不能升的）；`()` 关闭 |
| `step_timeout` | `20.0` 秒（墙钟） | 这么久没有新 worldstate 就结束本局：日志里有胜负 → `terminated`，否则 `truncated` + `info["error"]` |
| `replay_dir` | `None` | 给一个目录就录像，`close()` 时把 `.dem` 移过去并把路径写进 `env.unwrapped.replay_path`；`None` 不录 |
| `keep_files` | `False` | 保留会话目录用于排查 |
| `dota_path` | `None` | 覆盖游戏路径 |
| `session_factory` | `DotaSession` | 测试用，替换掉整个 bridge |

`reset(options={"timeout": 300})`：等待英雄出生的最长墙钟秒数。`seed` 只作用于 `env.np_random`，Dota 本身无法设种子。

## 5. Gym 环境参数（`gym.make("dota2_env/AllPick5v5-v0", ...)`）

和 1v1 同名的参数含义相同，下面只列不一样的。

| 参数 | 默认 | 说明 |
|---|---|---|
| `heroes` / `opponent_heroes` | 影魔 / 火枪 / 莉娜 / 莱恩 / 冰女 | 各 5 个英雄单位名，按本方 player id 升序发下去；第 i 个就是观测第 i 行 |
| `rules` | `AllPick5v5Rules(max_dota_time=2400)` | 基地被破 → `terminated`；2400 游戏秒 → `truncated` |
| `reward_fn` | `TeamReward()` | 权重见 `rewards.py: TeamReward.DEFAULT_WEIGHTS` |
| `starting_items` | 吃树、2 树枝、圆环 | **每个**英雄都买一份 |
| `ability_priority` | `(5, 0, 3, 4, 1, 2)` | 每个英雄各自加点 |
| 游戏模式 | `DOTA_GAMEMODE_AP`(1) | 写死在 env 里，和 1v1 的 21 一样不作为参数暴露 |

`reset(options={"timeout": 600})`：5v5 要多等选英雄 + 策略阶段，默认超时比 1v1 长。
`queue_purchase(row, item)` / `queue_train_ability(row, ability)` / `queue_chat(row, message)` 的第一个参数是英雄行号。

LLM 对局的那份 YAML（gateway、每个英雄一个 agent、决策频率、花费上限）不是环境参数，见
[LLM_MATCH.md](LLM_MATCH.md)。

## 6. 写死的时序 / 尺寸常量

| 常量 | 位置 | 值 | 含义 |
|---|---|---|---|
| 动作最小延迟 | `bot_controlled.lua.tpl` | `0.07` 游戏秒 | Lua 只执行"比它所依据的观测晚至少 0.07s"的动作（约 2 tick），沿用自 Last Order |
| 动作无上限 | 同上 | — | 迟到的动作照样执行；被更新的文件覆盖的动作直接丢失。游戏不等 agent——见下节 |
| Lua 就绪上报 | 同上 | 第 10 次 `Think` | 打印 `LUARDY {team, player_id, abilities}`，`env.unwrapped.ability_names()` 的数据来源 |
| 施法格上报 | 同上 + `behavior.lua` | 技能 0-5 / 背包格 0-5 一变就打 | 打印 `SLOTS {team, player_id, slots}`（名字 + 施法类型），`env.unwrapped.cast_slots()` 和 `ability` 掩码的数据来源（worldstate 只有物品 id，也没有施法方式） |
| 看门狗阈值 | 引擎 | 60 秒 | 仅在不带 `-nowatchdog` 时生效 ✅ |
| 监听队列上限 | `worldstate.py` | 2 帧 | 超出的帧在监听进程里丢弃；`observe()` 再跳到最新一帧。两处被丢掉的帧里的 `tree_events` 都会接到下一帧前面 |
| 关闭等待 | `game.py: stop_dota` | 20 秒 | SIGTERM 之后等客户端自己退出，超时才 SIGKILL |
| 单位表 | `observation.py` | `MAX_UNITS=32`，`UNIT_RADIUS=1600` | 观测里最近单位的数量与范围 |
| 队伍规模 | `observation.py` | `TEAM_SIZE=5` | 5v5 观测 / 动作的行数 |
| 塔数 | `observation.py` | `N_TOWERS=11` | 每方的塔总数，`team` 向量里做归一化用（实测确认：3 路各 3 座 + 基地 2 座）|
| 技能槽 | `observation.py` | `N_ABILITIES=6` | 只看槽位 0-5 |
| 背包格 | `observation.py` | `N_ITEM_SLOTS=6` | 只看背包格 0-5；动作的 `ability` 因此是 `N_CAST_SLOTS=12` 选一（`actions.py`） |
| 移动 | `actions.py` | 16 方向 × 300 距离 | `MOVE` 的离散化 |
| 方向施法 | `actions.py` | 16 方向 × 技能的 `cast_range` | `CAST_DIRECTION` 的落点；`cast_range` 为 0 时退回 300 |
| 反补阈值 | `actions.py` | 血量 < 50% | 友方小兵何时可作为攻击目标 |
| 复活出生点 | `observation.py` | 天辉 (-6700,-6700)，夜魇 (6900,6650) | 英雄复活后未被上报时 MOVE 的起点；agent 不发 MOVE 时环境从这里朝地图中心走一步 |
| 地图坐标刻度 | `map_features.py` | `MAP_SCALE=8192` | `hero` 的 x/y 以及 `runes` / `landmarks` 的相对坐标都除以它（`observation.py` 从这里导入） |
| 局部地图 | `map_features.py` | `MAP_RADIUS=16`（33×33 格，每格 64） | `local_map` 以英雄所在格为中心向四周各取 16 格，约 ±1050 |
| 点位匹配半径 | `map_features.py` | `SPOT_RADIUS=200` | `rune_infos` / 本方建筑离 `map.json` 的点位这么近才算“就在这个点上” |
| 神符可用状态 | `map_features.py` | `RUNE_STATUS_AVAILABLE=1` | bot API 的值，`extract_map.py` 会和客户端核对 |
| 地图数据版本 | `data/map.json` | ClientVersion 6934 / 7.41f | 环境构造时和本机 `steam.inf` 比对，不一致打 warning；刷新见 `docs/MAP_DATA.md` |

## 7. 实时性：游戏不等 agent

Dota 是实时推进的。策略慢了不会报错，只是那几个 tick 英雄继续执行上一条指令。环境通过 `info` 暴露这件事：

| 字段 | 含义 |
|---|---|
| `info["skipped_observations"]` | 累计有多少帧观测因为策略没跟上而被跳过（`observe()` 总是取最新一帧） |
| `info["action_delivery"]["sent" / "executed" / "lost" / "pending"]` | 写了多少动作文件；Lua 确认执行了多少；多少被后一个文件覆盖、Lua 从未见过；多少还没回执 |
| `info["action_delivery"]["last_delay" / "delay_median" / "delay_max"]` | 动作真正执行时，比它所依据的观测晚了多少**游戏秒**（下限 0.07） |

丢失动作里附带的购买 / 加点会在下一步自动重发。⚠️ 这组字段目前只有单测覆盖，还没在真实客户端上量过实际延迟分布
（`examples/latency_check.py` 就是干这个的）。

缓解手段（按代价从小到大）：调大 `ticks_per_observation`；把 `timescale` 调到 1 以下；锁步模式——让被控方的 Lua 阻塞等待
对应帧的动作文件，游戏整体冻结直到 agent 给出动作。锁步的前提（Lua 可长时间阻塞、`-nowatchdog` 有效、解除后无损恢复）
已验证，但**尚未实现**。
