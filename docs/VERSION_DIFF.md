# 新旧版本差异（Last Order 时代 7.23/7.24 → 2026-09 客户端）

实测环境：macOS，Dota 2 build 25329722（`steam.inf`: ClientVersion 6933，2026-09-15），
`scripts/probe_worldstate.py`，GUI 与 `-dedicated` 两种模式各跑一局 1v1 中路；5v5 全阵营用
`examples/scripted_5v5.py` 在 `-dedicated` 下跑（见第 1.1 节）。
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
| `-fill_with_bots +map start gamemode 1` | — | 全阵营 5v5 同样自动开局，见 1.1 | ✅ |
| `game/dkjson`、`loadfile`、`DebugDraw*`、`Action_MoveDirectly` | 可用 | 可用（动作回执 254/254） | ✅ |
| 新增脚本入口 | — | 引擎会尝试加载 `bots/team_desires.lua`（找不到只打一行日志） | ✅ |
| 默认 bot 买装备 | 未核实 | **默认 AI 会自己买鞋/吃树**；已在 `item_purchase_generic.lua` 里用空 `ItemPurchaseThink` 屏蔽 | ✅ |
| 内置 bot AI 作为对手 | — | bot 脚本不定义 `Think()` 时由 Valve 默认 AI 接管，1v1 中路里会补刀、对线、击杀 | ✅ |
| `host_timescale` 加速 | 可用 | `-dedicated` 下 2×、4× 实测稳定（6 tick/步 → 20 step/s） | ✅ |
| 1v1 结束 | — | 任一方第 2 次死亡（含被塔杀）即原生结束：摧毁败方基地 → POST_GAME → **立刻停止推送 worldstate**，服务器随后退出；胜负只能从 console.log 的 `Building: npc_dota_*_fort destroyed` 读到 | ✅ |
| **复活后的英雄不上报** | 正常 | 英雄复活后只要还站在泉水没动，就**不出现在己方 worldstate 的 `units` 里**（`players[].is_alive` 为 true）；发一条移动指令后恢复。Lua 端一直正常。环境在这种状态下把 `MOVE` 以外的动作（包括 `NOOP`）换成朝地图中心走一步（`actions.to_bridge_action`）：2026-09 的 LLM 5v5 里模型看到 `hero not spawned` 就一直回 NOOP，4 个英雄复活后在泉水里站到了比赛结束。实测 1v1 被塔杀后只发 NOOP：复活后 1 帧就重新上报，之后站着不动也不再消失 | ✅ |
| 每个端口的连接数 | — | 同一 worldstate 端口只接受一个客户端 | ✅ |
| Lua 阻塞与 `-nowatchdog` | dotaservice 原版靠 Lua 死循环等动作文件实现锁步 | Lua `Think()` 里循环 `loadfile` 阻塞时**整个游戏冻结**（游戏时间不走、worldstate 停发），解除后无损恢复。不带 `-nowatchdog`：阻塞满 **60 秒**进程被杀（`FATAL ERROR: Watchdog timeout exceeded in Lua script code`）；带 `-nowatchdog`：阻塞 90 秒仍存活并恢复。该参数字符串在二进制里搜不到但确实有效 | ✅ |
| GUI 模式看画面 | 控制台 `jointeam spec` | 服务器 VM 自动发（1.3） | ✅ |

## 1.1 5v5 全阵营（新增，2026-09 实测）

`gamemode 1` + `-dedicated`，天辉 5 个英雄全部由 Python 驱动（`examples/scripted_5v5.py`），夜魇是内置 AI，
`timescale 4` / `ticks_per_observation 6`。跑完整一局：8467 步 / 26 分 43 秒游戏时间，内置 AI 推掉天辉基地结束。

| 项目 | 结果 | 状态 |
|---|---|---|
| 选英雄 | `hero_selection.lua` 里 `SelectHero(ids[i], heroes[i])` 对每队 5 个位置都生效，实测拿到配置的影魔 / 火枪 / 莉娜 / 莱恩 / 冰女 | ✅ |
| player id | 天辉 0-4、夜魇 5-9；`GetTeamPlayers()` 的数组顺序 == player id 升序，所以「配置里的第 i 个英雄 = 第 i 个 player id」成立（5 条 LUARDY 的 `player_id` 与英雄一一对应） | ✅ |
| 一个文件驱动 5 个英雄 | 5 个 bot 都 `loadfile` 同一个 `actions_t2.lua`，各自执行 `player` 等于自己的那条主动作；一个文件产生 5 条 ACK（Python 只认第一条，其余自然丢弃） | ✅ |
| 动作送达 | 整局 8467 个文件 → 8466 executed / **0 lost** / 1 pending，跳帧 0，延迟中位数 0.100 游戏秒（和 1v1 相同的下限） | ✅ |
| 吞吐 | 20.0 step/s（= `timescale 4` ÷ 0.2 游戏秒每步）全程不掉，**和 1v1 一样**，5 个英雄没有带来额外开销 | ✅ |
| 结束方式 | 和 1v1 一样：基地被破的瞬间客户端就停止推送 worldstate——最后一帧基地还剩 1% 血（`ancient` 分量累计只到 -0.99，没有出现血量为 0 的那一帧），所以胜负是 `match_winner()` 从 console.log 的 `Building: npc_dota_goodguys_fort destroyed` 读出来的 → `terminated=True, winner=夜魇` | ✅ |
| LUARDY | 每个受控英雄各打一条，带自己的 `player_id` 和槽位→技能名；`DotaSession.lua_status()` 因此按 player id 索引 | ✅ |
| 建筑数量 | 每方 11 座塔（`unit_type=6`）+ 1 个基地（`unit_type=9`）；前哨 / 双子门 / 莲花池是 `unit_type=10`，不会混进塔的计数 | ✅ |
| 内置 AI 作为 5 人对手 | 会分路、补刀、抱团推进、破塔破基地；实测整局把脚本队打成 0 杀 42 死、塔 2:11 | ✅ |

## 1.2 GOTV 录像会让客户端崩溃（2026-09 实测）

**根因：GOTV 自己的 `HLTVServerAsync` 线程段错误，把整个客户端带崩。** 不是「录像抢了 worldstate socket」——
socket 断只是进程没了的副作用。macOS 崩溃报告（`~/Library/Logs/DiagnosticReports/dota2-*.ips`）里两次
自发崩溃都是：

```
exception: EXC_BAD_ACCESS (SIGSEGV), KERN_INVALID_ADDRESS
faulting thread 24: HLTVServerAsync
   libengine2.dylib +0x282cb3 / +0x286d6d / ...
   libtier0.dylib   CThread::ThreadProc(void*)
```

（本机是 Apple Silicon `Mac16,1`，Dota 只有 x86-64 版，崩溃报告里 `"translated": true`，即跑在 Rosetta 下。
这个崩溃是不是 Rosetta 或 macOS 独有，**没有在别的平台验证过**。）

崩溃时间点 = GOTV **开始广播**的时刻，也就是「广播源起点 + `tv_delay`」。实测广播源起点稳定在
`dota_time ≈ +19.2`：

| `+tv_*` 参数 | 客户端活到 | demo 里的对局内容 |
|---|---|---|
| 不加（对照组） | **不崩**，180s 看完，dota_time 到 599 | 不录 |
| `tv_enable 1 tv_autorecord 1`（引擎默认 delay，约 120） | 崩在 `dota_time` **139.2**（= 19.2 + 120） | 有，约 110 秒画面 |
| `+ tv_delay 90` | 崩在 `dota_time` **109.2**（= 19.2 + 90） | 有：829 个包 / 812 个 `DEM_Packet`，tick 36–3303 |
| `+ tv_delay 0` | 广播立刻开始，**一帧 actionable worldstate 都收不到** | 没有文件 |
| `+ tv_delay 3600` | **不崩**，180s 看完 | **没有画面**：15 个包，0 个 `DEM_Packet` |
| `+ tv_advertise_watchable 0 + tv_maxclients 0` | 立刻崩 | 没有文件 |

结论：**在这台机器上没有能同时要到「完整录像」和「跑完一局」的配置**。`tv_delay` 只是在挪崩溃时刻——
挪早了录不到画面，挪到比赛结束之后就等于没录。demo 的进度永远落后实时 `tv_delay`。

其它顺带测到的：

- 重连没用：socket 断掉后 `connect()` 能瞬间连上一次，随即再次 reset，之后端口就再也不接了——因为进程已经没了。
- **客户端没有任何优雅退出的途径**：stdin 收不到命令（`BrokenPipeError`，`dota.sh` 起的进程 stdin 是关的），
  SIGINT 让它段错误（exit 139，0.0s），SIGTERM 也是立刻死。所以 demo 文件头字节 8 的 `CDemoFileInfo`
  偏移永远是 0（未收尾）。**但未收尾的 demo 实测可以在客户端里正常打开播放。**
- 旧版（Last Order 时代 7.23/7.24）用 `+tv_delay 0 +tv_enable 1 +tv_title <id> +tv_autorecord 1
  +tv_transmitall 1` 是能录到完整对局的；在当前这个 build 上同一组参数直接崩。

环境的取舍：`replay_dir` 用的是上表第二行，也就是「崩之前能录到约 110 秒画面」。构造时会打 warning。
换个平台（Linux / Windows 原生）很可能直接就好了，但没验证过。**这个开关先留着不修，排查步骤见
第 5 节待办第 1 条。**

## 1.3 无界面自动执行控制台命令，以及 GUI 模式自动进观战（2026-09 实测）

**能自动执行的命令走两条路**：`<dota>/game/dota/cfg/dota2_env_server.cfg`（启动参数 `+servercfgfile` /
`+lservercfgfile`，`-dedicated` 走前者、GUI 监听服务器走后者，日志里是 `SV: Executing listen server config file`）
在服务器激活时 exec，里面的 `script_reload_code bots/server_actions` 把服务器 VM 脚本装进去；之后脚本随时可以
`SendToServerConsole('<命令>')`。启动行上的 `+script_reload_code` 在地图加载前执行，静默丢掉 ✅。

**GUI 模式的 `jointeam spec`**（监听服务器的真人在进队前没有 player id，`PlayerResource` 里只有 10 个 bot）：

| 发法 | 结果 |
|---|---|
| `SendToConsole('jointeam spec')`（客户端控制台） | 每次都被拒：`[InputService] Cannot execute concommand 'jointeam', missing required FCVAR flag`（脚本源的命令要带特定标记，手敲不受限） ✅ |
| 写进 cfg，随服务器激活 exec | 没测出结果：那两轮客户端在启动阶段就退出了（连 `con_logfile` 都没创建），原因是上一局的游戏进程还没退干净（见下），不是这条命令 ⚠️ |
| `SendToServerConsole('jointeam spec')`（服务器控制台，`State_Get()` ≥ PRE_GAME 后） | 下一秒真人出现在观战队（player 10，team 1，conn 2），画面正常；两轮复现 ✅ |
| `dota_spectator_auto_spectate_bot_games 1` | 没轮到测（上一条已成功）；按帮助文本它指的是 Valve 常驻的展示局 |

`server_actions.lua` 现在进入 PRE_GAME 后每秒从服务器控制台发一次，直到 `PlayerResource` 里出现 team 1 的真人为止，
然后打一行 `server_actions: the host watches from the spectator team`。`-dedicated` 下（`IsDedicatedServer()`）不发。
服务器 VM 没有 `DOTA_TEAM_SPECTATOR` 常量（有 GOODGUYS / BADGUYS / NEUTRALS / NOTEAM），脚本里写的是字面量 1。

**`stop_dota` 以前只杀壳** ✅：`dota.sh` 是 bash 包装，`process.terminate()` 只 SIGTERM 了 bash，`dota2` 本体成为孤儿继续跑
（GUI 窗口一直开着），下一次 `run_dota()` 开头的 `pkill dota2` 才杀它，新实例紧接着启动就撞上它的退出过程，在启动阶段
自己退出。现在 `run_dota` 用 `start_new_session` 起进程、`stop_dota` 对整个进程组发信号并在 `pkill` 后等到没有
`dota2` 进程为止。

## 1.4 Linux 客户端（Docker，2026-09 实测）

宿主 Ubuntu 26.04（内核 7.0），容器 Ubuntu 24.04，ClientVersion 6941 / buildid 25539253（和 macOS 同一个 build），无界面。
部署和并行见 [DOCKER.md](DOCKER.md)。

| 项目 | 结果 | 状态 |
|---|---|---|
| `dota.sh` | Linux 分支先 `. /etc/os-release`，`VERSION_CODENAME` 不是 `sniper` 就打印 `FATAL: It appears dota.sh was not launched within the Steam for Linux sniper runtime environment` 退出。sniper（Steam Linux Runtime 3.0）只有经 Steam 启动才有，所以环境在 Linux 上直接起 `bin/linuxsteamrt64/dota2`，补上 `dota.sh` 会设的 `LD_LIBRARY_PATH=<game>/bin/linuxsteamrt64`、`ENABLE_PATHMATCH=1`（散装文件按大小写不敏感查找）和工作目录 `<game>`（`DotaGame.run_dota`）。`dota.sh` 其余的设置（`__GL_THREADED_OPTIMIZATIONS`、`SDL_VIDEO_DRIVER=x11`、退出码 42 就重启）只和窗口、客户端自更新有关 | ✅ |
| 系统库 | 普通 Ubuntu 24.04 缺 libX11、libdrm、libva、libvdpau（自带的 ffmpeg 要）和 libharfbuzz、libfribidi、libglib-2.0、libthai（自带的 pango 要），装上就能跑；其余都在 `bin/linuxsteamrt64` 里 | ✅ |
| 打开文件数 | `dota.sh` 里有 `ulimit -n 2048`，容器默认软上限 1024；客户端自己把软上限提到 4096，一局 1v1 开着 277 个 | ✅ |
| 通信层 | `bots` 软链接、worldstate socket、动作文件、console.log 回执、服务器 VM 的 cfg 原样可用：1500 步 20.0 steps/s，回执里的动作延迟 0.1 游戏秒，和 macOS 一样 | ✅ |
| Steam 客户端 | 无界面不需要 Steam 在跑（macOS 上要）；镜像里只有 SteamCMD | ✅ |
| 匿名 SteamCMD | 只拿到 718M 的可执行文件 depot，内容 depot 报 missing license，要一个账号（DOCKER.md 第 1 节） | ✅ |
| 并行 | 每个容器一层 overlay，4 个同时跑都是 20.0 steps/s | ✅ |

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
- **地图** ✅：中路一塔坐标没变（天辉 -1544,-1408 / 夜魇 524,652），但 7.33 起地图整体变大，
  gridnav 现在是 x∈[-10240, 10240]、y∈[-10752, 10240]（旧图 ±8288）；x∈[-8088, 8167] 是两个智慧神龛，
  不是地图边界。新增前哨、双子门、莲花池、魔方、Watcher 等。旧的 `data/*7.24b*` 全部作废，已按
  ClientVersion 6934 / 7.41f 重新导出为 `dota2_env/data/map.json`（来源与实测结论见
  [MAP_DATA.md](MAP_DATA.md)）。
- **信使** ✅：每个玩家一个信使（`unit_type=11`，每队 5 个），但顶层 `couriers` 列表实测为空，
  信使信息要从 `units` 里取。
- **符文** ✅：`rune_infos` 的 4 个点位就是 2 个强化神符点和 2 个赏金神符点（开局 type=-1、status=2 即 MISSING）；
  `RUNE_BOUNTY_3/4` 已返回 (0,0,0)。经验神符 7.38 起换成了智慧神龛（`npc_dota_xp_fountain`，中立建筑）；
  bot API 新增 `RUNE_WATER`=7、`RUNE_XP`=8、`RUNE_SHIELD`=9 三种类型常量。刷新那一刻 status 不需要视野就变成
  AVAILABLE，本队看到那里空了才变回来（[MAP_DATA.md](MAP_DATA.md) 4.6）。
- **物品**：TP 固定在物品槽位 15 ✅，储藏处是物品槽 9-14 ✅（见 3.2）。⚠️ 旧的固定出装路线（`nevermore.py`）和禁用物品规则需要对照新版物品表重新核对。
- **物品使用** ✅（2026-09，1v1 headless）：Lua 的 `GetItemInSlot():GetName()`、`GetBehavior()` 和
  `ABILITY_BEHAVIOR_*` 常量都能用。按施法方式对自己用：仙灵火、治疗药膏、净化药水、吃树（一塔旁）、假眼都生效；
  树枝放脚下在一塔旁能种、在泉水里种不出来，往前 150 放也种不出来；圆环（被动）什么都不发生。
  被动物品和被动技能一样，worldstate 里 `is_fully_castable` 是 true，只能靠 Lua 的 `GetBehavior()` 区分。
- **施法方式** ✅（2026-09，5v5 headless）：`GetBehavior()` / `GetTargetTeam()` 可用，常量值是 `NO_TARGET=4`、
  `UNIT_TARGET=8`、`POINT=16`、`PASSIVE=2`、`VECTOR_TARGETING=2^30`，目标阵营 `FRIENDLY=1`、`ENEMY=2`。
  矢量施法的技能（帕克幻象法球、滚滚虚张声势）同时带 `POINT`。对只能指地面的技能下单位指令
  （`Action_UseAbilityOnEntity`），客户端直接忽略：法球、墨客的笔、滚滚、榴霰弹都不放，蓝和冷却都不动；
  龙破斩、裂地尖刺带 `UNIT_TARGET` 所以可以。同样这些技能用 `Action_UseAbilityOnLocation` 全部能放，
  滚滚会冲向那个点，第二个点传不进去（见 3.1）。worldstate 的 `cast_range` 和 Lua 的 `GetCastRange()` 一致，没学的技能是 0。
- 填充位英雄仍可选 `npc_dota_hero_wisp`（hero_id 91）✅。

## 3.1 矢量施法的第二个点（2026-09 实测，ClientVersion 6934）

**结论：bot API 给不了第二个点；服务器 VM 的 `ExecuteOrderFromTable` 可以，条件是 `TargetIndex = 0`** ✅。
环境现在就这么做：`bridge/lua/server_actions.lua` 由 `cfg/dota2_env_server.cfg`（`+servercfgfile`，服务器激活时
exec）加载进服务器 VM，每 tick `loadfile` 同一份 `bots/actions_t<team>.lua`，把 Python 发的
`DOTA_UNIT_ORDER_CAST_VECTOR` 变成两条指令：先 `DOTA_UNIT_ORDER_VECTOR_TARGET_POSITION`（=30）带第二个点，
再 `DOTA_UNIT_ORDER_CAST_POSITION` 带起点，两条都带 `TargetIndex = 0`、`AbilityIndex = 技能 entindex`。bot 那边
对这条动作只做 `Action_ClearActions(false)`。不给第二个点时，它落在世界坐标原点附近（地图中心）。

**为什么之前 13 组服务器端实验全失败**（反汇编 `libserver.dylib` 的 `DOTA_ExecuteOrders`，2026-09-22）：
`ExecuteOrderFromTable` 解析表时 `TargetIndex` 缺省是 **-1**，而指令执行器对 30 号指令先看 TargetIndex：为 0
走无目标路径，非 0 就按实体索引解析，-1 不在合法范围直接丢弃（错误码 7）。真人客户端发来的 30 号指令
`target_index` 缺省是 0，所以没这个问题。显式写 `TargetIndex = 0`（或施法者自己的 entindex）之后：

| 放法（服务器 VM，无界面 `-dedicated`） | 结果 |
|---|---|
| 只发 5 号（对照） | 滚滚冲到 A，挥砍朝地图中心（43-45°） |
| 30 号带 B + 5 号带 A，同一 think，`TargetIndex = 0` | **挥砍朝 B（270°）**，重复 4 次都一样 |
| 同上，`TargetIndex = 施法者 entindex` | 270° |
| 30 号带 B，下一 think 再发 5 号 | 270° |
| 30 号带 A、5 号带 B（对调） | 冲到 B，挥砍朝 A（88°）：施法指令的点是起点，30 号的点是终点 |
| 30 号的点放到 2000 远 | 270°：只取方向 |
| bot 正在 `Action_MoveDirectly` 时服务器发两条 | 照样冲、照样 270°，bot 的移动不会盖掉它；同一 tick 再 `Action_ClearActions(false)` 也不影响 |
| 帕克幻象法球 30 号带 B + 5 号带 A | 球拐向 B（277°），对照组拐向地图中心（73°） |

`+script_reload_code bots/<file>` 写在启动行**没有用**（地图还没加载，静默丢掉）；`servercfgfile` 指向的 cfg
在服务器激活时（game_time 1.0）exec，VM 已就绪 ✅。服务器 VM 里有 `loadfile` / `dofile` / `require` /
`DoIncludeScript` / `LoadKeyValues` / `SendToServerConsole`，没有 `io` / `os`；`loadfile` 每次都读到新内容 ✅。
`PlayerResource:GetSelectedHeroEntity(pid)` 对 bot 有效，`GetAbilityByIndex(i)` 和 bot API 的 `GetAbilityInSlot(i)`
顺序一致 ✅。

哪些技能是矢量以运行时的 `GetBehavior()` 为准：实测帕克幻象法球、滚滚虚张声势带这一位；按 npc 数据还有残阴、
复制之墙、烈焰之军、狂风之力、回身踢、弹无虚发，以及魔晶 / 神杖改出来的动能栅栏、海象飞踢等（没有逐个实测）。
下面是 bot API 那 46 组的记录，结论仍然有效：**bot API 自己给不了第二个点**。

**缺省方向**（只给一个点）：

- 滚滚冲到给的点，冲刺和挥砍时身体一直对着同一个固定点。东、西、北共 5 次冲刺拟合出来是 (90, 80)，误差 2.3°
  （直接取原点是 3.7°）；往西冲时挥砍朝东北偏东 29°。
- 帕克的法球朝西放，先往西南飞再掉头往东北；朝东放，332° 出手、拐到 73°。都是往地图中心那一侧拐。
- 自定义游戏库 Nibuja05/dota_vector_targeting 里方向 = 第二个点 − 起点。引擎如果把没设过的第二个点当 (0, 0, 0)，
  就正好是"从起点指向原点"，和实测对得上 ⚠️（推断，原版技能的引擎代码看不到）。

**试过的放法**：滚滚站在泉水边 (-7000, -6600)（回蓝快），起点 A 在东边 400，终点 B 在 A 南边 400，想要的是朝南
（270°）挥砍；帕克同样站位，起点放到 800，终点距离 200 / 400 / 1000 都试过，想要的是球往南拐。

| 途径 | 试了什么 | 组数 | 结果 |
|---|---|---|---|
| bot API | 只给一个点（对照） | 滚滚、帕克各 1 | 缺省方向 |
| bot API | 再补一次终点施法：`Action_` / `ActionPush_` / `ActionQueue_` × 同一 think / 下一 think / 抬手时 / 出手后 | 各 12 | 缺省方向 |
| bot API | 先给终点再给起点；先 ping 终点；施法前后插 `MoveToLocation` / `MoveDirectly` | 各 8 | 缺省方向 |
| bot API | 只改终点距离（帕克，200 / 1000） | 4 | 缺省方向 |
| bot API | `Action_UseAbilityOnLocation(h, v, true)`（网上流传的"第三个参数表示排队"） | 1 | 报错 `called with 4 arguments - expected 3`：客户端里的签名是 `(HSCRIPT, const VectorWS &)` |
| 服务器端脚本，**没设 `TargetIndex`** | `ExecuteOrderFromTable` 发 `DOTA_UNIT_ORDER_VECTOR_TARGET_POSITION`（=30）：和施法指令同帧前 / 后、隔一帧、连发 0.3 秒、配排队施法、配 `CastAbilityOnPosition`、两点对调、服务器发完 bot 再施法 | 滚滚 4、帕克 9 | 缺省方向：30 号指令被引擎丢掉了（TargetIndex 缺省 -1，见上） |

**顺带测清的 bot 指令规则**（所有技能都一样，环境也会碰到）：

- 同一次 think 里连下两条施法：后下的 `Action_` / `ActionPush_` 生效（冲刺去了 B），`ActionQueue_` 排在后面。
- 从下一次 think 开始，同一技能还在转身 / 抬手时，新下的同技能施法被忽略，技能照第一个点放；排队或压栈的那条
  在技能进入冷却后被丢掉。所以连续两帧对同一技能换目标，第二次不生效。
- `Action_*` 调用时替换整个队列；`DOTA_UNIT_ORDER_NONE`（NOOP）只画圈，不碰队列。

**服务器端脚本**：本地房间开作弊（启动参数自带 `+sv_cheats 1`）后，控制台 `script_reload_code bots/<文件名>` 能把
`bots/` 下的 Lua 加载进服务器 VM（ryndrb/dota2bot 的 Buff 模式、Fretbots 都这么做），里面有 `ExecuteOrderFromTable`、
`HeroList`、`SpawnEntityFromTableSynchronous`。无界面时靠 `servercfgfile`（见上）自动加载。标准对局里
`GameRules:GetGameModeEntity()` 是 nil，挂不上 `SetExecuteOrderFilter`。`ExecuteOrderFromTable` 只认 `UnitIndex /
OrderType / TargetIndex / AbilityIndex / Position / Queue`（二进制里的键名就这六个），没有下令玩家这一项，
脚本下的指令在日志里叫 "Game code"。

**参考**：Valve 的 [Dota2-Gameplay#7675](https://github.com/ValveSoftware/Dota2-Gameplay/issues/7675)（2023 年请求
`UseAbilityOnEntityWithVector`，没有回复，2026-04 被机器人关掉）；[Nibuja05/dota_vector_targeting](https://github.com/Nibuja05/dota_vector_targeting)
（自定义游戏库，只读客户端发来的两条指令：先 `VECTOR_TARGET_POSITION` 带第二个点，再 `CAST_POSITION` 带起点，自己
不发指令）；ryndrb/dota2bot 等开源 bot 都只给一个点（玛西回身踢那里注释着 `-- vector targeted; not reliable`）。

**怎么复测**：bot 侧探针 Lua 写在动作文件的 `return '<json>'` 前面（两个 VM 每次 think 都 `loadfile` 这个文件，
所以要用 `if GetBot ~= nil then ... end` 包起来）；服务器侧探针放进会话目录、写一个 cfg 让 `+servercfgfile` 指过去，
都不用改仓库里的 Lua。用 `GetBot():GetUnitName()` 选中英雄，按阶段调 bot API，逐 think 打印 `GetLocation()` / `GetFacing()`（滚滚挥砍时的
朝向就是矢量方向）和 `GetLinearProjectiles()`（法球的位置和速度）。有界面时控制台开 `dota_ability_debug 1` 免冷却。

## 3.2 回城卷轴、神符、天赋、信使（2026-09 实测，ClientVersion 6937）

先用探针（bot VM 里换上探针 bot 脚本，服务器 VM 负责摆场景：刷新冷却、`FindClearSpaceForUnit` 挪英雄、`HeroLevelUp` 升级）
在 1v1（gamemode 21）和 5v5（gamemode 1）无头、4 倍速下测 bot API，再用 5v5 环境的 gym 动作把 `TP`、`PICKUP_RUNE`、
`TALENT`、`COURIER` 和回城卷轴补买从头到尾跑了一遍 ✅。

**回城卷轴** ✅
- 每个英雄出生时 TP 格（物品槽 15）有 1 张 `item_tpscroll`。1v1 在 -90 秒就能用；5v5 在 -88 秒还有 98 秒冷却。
- `Action_UseAbilityOnLocation(GetItemInSlot(15), 点)`：持续施法 3 秒（`IsChanneling()` 为 true），然后传送；
  用掉最后一张后 TP 格就空了，冷却 80 秒。
- 落点：目标点离友方建筑不超过 800 就正好落在目标点（中路一塔外 600 的点，落点分毫不差）；更远就落在离目标点最近的
  友方建筑朝目标方向 800 处（对 (0, 0) 施放，落在 (-953, -869)，正好是中路一塔 (-1544, -1408) 外 800）；
  直接指一塔的坐标，落在塔边约 210 处。短时间里第二次传到同一座塔，持续施法明显变长（和卷轴说明一致）。

**购买、储藏处、信使** ✅
- 在泉水 `ActionImmediate_PurchaseItem('item_tpscroll')`：叠进 TP 格（1 张变 2 张）。在野外买：进储藏处，worldstate 里是
  物品槽 9；英雄回到泉水时自动挪进 TP 格。
- `ActionImmediate_Courier(信使, COURIER_ACTION_TAKE_AND_TRANSFER_ITEMS)`：信使从储藏处取回物品，约 24 秒飞了 7,700，
  把卷轴放进 TP 格，然后自己飞回泉水（`GetCourierState` 1 在基地 → 3 运送中 → 4 返回 → 1）。常量值：`RETURN`=0、
  `RETURN_STASH_ITEMS`=2、`TAKE_STASH_ITEMS`=3、`TRANSFER_ITEMS`=4、`BURST`=5、`TAKE_AND_TRANSFER_ITEMS`=6。
  每队 `GetNumCouriers()` = 5，`GetCourier(i)` 是第 i 个玩家的。
- 运送途中卷轴既不在英雄身上也不在储藏处，而在 worldstate 里信使单位（`unit_type=11`，`player_id` 是主人）的 `items` 里。

**神符** ✅
- `RUNE_POWERUP_1/2`、`RUNE_BOUNTY_1/2` 的值是 0-3，顺序正好是 `RUNE_SPOTS`（上路强化、下路强化、上路赏金、下路赏金）。
- 1v1 中路模式 0:00 不刷神符（四个点都是 MISSING）。5v5：0:00 四个点**都**刷赏金神符（type 5，两个强化神符点也是）；
  2:00 两个强化神符点都刷圣水神符（type 7 = `RUNE_WATER`）。0:00 没人捡的赏金神符会一直留在强化神符点上，和 2:00 的
  圣水神符同时躺着（两个 `dota_item_rune` 实体）；上路那个圣水神符离点位 90。
- bot API 的 `Action_PickUpRune(n)`：n = 0（上路强化）和 3（下路赏金）正常，捡到后 +80 蓝 / +40 金；**n = 1（下路强化）
  不可用**：0:00 英雄站在神符旁原地不动，2:00 英雄走过神符、一路跑向上路强化神符点，两次都一样。n = 2 没测。
- 服务器 VM 的 `ExecuteOrderFromTable({OrderType = DOTA_UNIT_ORDER_PICKUP_RUNE, TargetIndex = 神符实体})` 能捡到下路强化神符点的
  圣水神符。环境的 `PICKUP_RUNE` 现在全部这样下发（`bridge/lua/server_actions.lua`）。用 gym 动作实测：0:00 四个点依次各捡一次
  赏金神符，每次 +44 金（40 加上自然增长），捡完那个点变成 MISSING；下路强化神符点 2:00 的圣水神符 +87 蓝。

**天赋** ✅
- 影魔的槽 7-14 是 8 个天赋，顺序和 `heroes.json` 的 `talents` 一致；槽 15 是 `special_bonus_attributes`（最多 7 级），
  16-19 是 `ability_capture`、`abyssal_underlord_portal_warp`、`twin_gate_portal_warp`、`ability_lamp_use`。
  worldstate 的 `abilities` 里带着这些槽和等级。
- 10 级时 8 个天赋的 `CanAbilityBeUpgraded()` 都是 true，`GetHeroLevelRequiredToUpgrade()` 都是 10，但
  `ActionImmediate_LevelAbility` 实际只收一层里的一个：10 级学槽 9 被拒；学了 8 之后 7 被拒；15、20、25 级分别学 10、11、14 后，
  9、12、13 被拒。所以分层是 (7, 8)、(9, 10)、(11, 12)、(13, 14) 对应 10、15、20、25 级。被拒不扣技能点，也不报错。
- 用 gym 动作实测：升到 10 级后自动加点留下 1 点，`TALENT` 学了第 [1] 个天赋，技能点归零，这一层随之关闭。
- `special_bonus_attributes` 在 10 级时 `ActionImmediate_LevelAbility` 被拒；服务器的 `HeroLevelUp` 一路升到 25 级时它自己涨到了
  7 级（扣技能点）。正常升级会不会这样没测 ⚠️。

## 3.3 神符、智慧神龛、莲花池什么时候来（2026-09 实测，ClientVersion 6938）

[`scripts/probe_events.py`](../scripts/probe_events.py)：无头、英雄都不动，5v5 打到 22:00、1v1 打到 15:00，4 倍速。服务器 VM
看得到整张地图：每个 `dota_item_rune` 实体一出现就记下时间、位置和模型，15 秒后用 `UTIL_Remove` 拿走，免得没人捡的符挡住
下一个；英雄从号角起就站在智慧神龛旁、小精灵站在莲花池里，神龛一启动、莲花一长出来就被拿走，看经验和物品栏什么时候变就知道
时间；5v5 最后 50 秒再让另一个小精灵走进一直没人碰的莲花池，数它存了几朵。推出来的时间表写进 `dota2_env/data/events.json`
（[MAP_DATA.md](MAP_DATA.md) §3.1），写之前每一次神符刷新都要落在服务器 VM 的 `GameRules:GetNextBountyRuneSpawnTime()` /
`GetNextRuneSpawnTime()` 报过的倒计时上 ✅。

| | 5v5（gamemode 1） | 1v1 中路（gamemode 21） | 在哪 |
|---|---|---|---|
| 赏金神符 | 0:00；4:00 起每 4 分钟 | 4:00 起每 4 分钟 | 0:00 四个神符点各一个；之后只在上路 / 下路赏金神符点 |
| 圣水神符 | 2:00、4:00 | 4:00 | 两个强化神符点各一个 |
| 强化神符 | 6:00 起每 2 分钟 | 同左 | 两个强化神符点里随机一处；5v5 那局 8 个里 7 种都出现了 |
| 智慧神龛 | 7:00 起每 7 分钟 | 同左 | 两个神龛各自启动；站进 300 以内 3 秒后给经验，7:00 / 14:00 / 21:00 依次 200 / 500 / 800 |
| 疗伤莲花 | 3:00 起每 3 分钟 | 同左 | 两个莲花池各长一朵；一个池子最多存 6 朵（5v5 的池子到 21:00 该长 7 朵，只采到 6） |

- 1v1 的倒计时照样报 0:00 和 2:00（号角前 `GetNextBountyRuneSpawnTime` 是 0、`GetNextRuneSpawnTime` 是 120），但这两次什么都
  不刷，和 3.2 里"1v1 中路模式 0:00 不刷神符"一致。所以探针只要求刷出来的都在倒计时上，反过来不要求。
- 以前以为的"赏金神符每 3 分钟、6:00 前后才有强化神符"都不对：客户端术语表 `DOTA_Glossary_Gold_BountyRunes` 自己也写着每 4 分钟。
- 神龛 7:00 之前站过去不给经验（1:00 站 8 秒，0 → 0）；一次启动被拿走以后，同一轮再站过去什么都没有（7:30）。客户端的
  console.log 会自己记一行 `XP Fountain: Unit npc_dota_hero_nevermore gains 500 XP`。
- 莲花不是地上的物品，池子和神龛单位身上也看不出来：整局 `npc_dota_lotus_pool` 的 `modifier_passive_lotus_pool_building`
  层数一直是 0、`ability_lotus_pool` 充能一直是 0，`npc_dota_xp_fountain` 的 `ability_xp_fountain` 充能一直是 1。所以 world state
  里没有池子里有几朵、神龛这一轮拿没拿，环境只能按时间表算（`map_features.upcoming`）。
- 采莲花不用持续施法，英雄技能栏里也没有 `ability_pluck_lotus`：站进 350 以内就自动把池子里的都采走，第一朵 1.5 秒，之后每朵快
  70%，最少 0.3 秒（`ability_lotus_pool` 的数值）。三朵疗伤莲花自动合成大疗伤莲花，两朵大的合成巨大疗伤莲花；两朵疗伤莲花是一件
  `item_famango`，充能 2。
- 顺带看到、还没进时间表的：昼夜 5 分钟一换（0:00 天亮）；肉山开局在上坑，15:00 入夜时走到下坑，20:00 天亮时走回上坑；
  痛苦魔方（`npc_dota_miniboss`）20:00 在两边同时刷出。

**world state 里的神符**（开发探针时的三局 5v5，天辉给了神符点的视野，夜魇没有）：
- 有视野：刷出来那一帧就是 AVAILABLE，type 也对（0:00 是 5 赏金，4:00 强化神符点上是 7 圣水）；被拿走后变回 type -1、MISSING。
- 没视野：0:00 四个点里三个直接报 type 5；有一局上路强化神符点从 0:00 一直报 type 5 到 6:00，其间 2:00、4:00 那里刷的其实是
  圣水神符；6:00 的强化神符只报 AVAILABLE、type -1。所以文本里 `available` 后面的神符名只是"最后一次知道的种类"。
- 探针删过神符之后，天辉那一路 world state 三局都在 4:15 那一批删完后读不下去了（`DecodeError: Wire format was corrupt`，
  第三局用的是环境自己的 `worldstate_listener` 进程，一样），有一局夜魇那一路也断了。原因没查清 ⚠️，所以探针只用服务器 VM 的
  记录，不读 world state。正常对局里神符是被捡走的，不是被删掉的。

## 4. Python 层

| | 旧仓库 | dota2-env |
|---|---|---|
| Python | 3.8 | 3.10+（实测 3.12） |
| protobuf | 3.x 生成码 | protoc 29 生成码 + 运行时 7.x |
| 依赖 | tensorflow 2.3、psutil、PyYAML | 仅 protobuf（PyYAML 和 httpx 回到了可选的 `[llm]` extra，只有 LLM 对局配置用；同一个 extra 里还有渲染 LLM prompt 的 Jinja2） |
| 路径 | 全部相对 CWD，游戏路径写在 `play_with_human_local.py` 顶部，`dota_game.py` 反向 import 它 | 包内定位 Lua；`DOTA_GAME_PATH` 环境变量或默认 Steam 路径 |
| 接口 | 手写 `Dota2Env.reset/step`，返回原始 protobuf | `gymnasium.Env`：Dict 观测/动作空间、action mask、reward、terminated/truncated，通过 `check_env` |
| 动作文件写入 | 直接写，靠 Lua 端容错；JSON 里有引号/反斜杠会破坏 Lua 字符串 | 先写临时文件再 `os.replace` 原子替换；对 `\` 和 `'` 转义 |
| socket 读取 | 分片接收时长度计算有 bug（`remain - len(data)` 用的是累计长度） | `_recv_exact` |
| 进程间传观测 | 直接传 protobuf 对象 | 传序列化 bytes |
| 日志 | import 时就在 CWD 创建 `dotapy.log` | 标准 `logging`，不落盘 |

## 5. 后续待办

1. **录像只能录到开局约 110 秒**（`replay_dir`，现状见 1.2 节）。根因是客户端 GOTV 的 `HLTVServerAsync`
   线程段错误，不在我们这边，所以先留着这个开关不修，按这个顺序查：
   (a) 换 Linux / Windows 原生跑同一组 `+tv_*`，确认是不是 macOS + Rosetta 独有——**这一步能不能修全看它**；
   (b) 如果是平台独有，就按平台决定 `replay_dir` 的默认行为（比如 macOS 上直接拒绝并说明原因）；
   (c) 客户端更新后重测一次，崩溃栈用 `~/Library/Logs/DiagnosticReports/dota2-*.ips` 对；
   (d) 如果 Valve 一直不修，又确实需要回看，改成录 worldstate 流（每帧 `CMsgBotWorldState` 落盘，
       用 `text.py` 的 `describe()` 当文本回放）——这条一定可用，但看不到画面。
2. ~~导出物品价格~~ 已完成：`dota2_env/data/items.json` / `abilities.json` / `heroes.json`（中英文，
   `scripts/fetch_game_text.py`，见 [MAP_DATA.md](MAP_DATA.md)），含合成配方和神秘商店标记，并接进了
   LLM 的 prompt（本英雄技能进 system，身上物品进 user，见 [LLM_MATCH.md](LLM_MATCH.md) §8）。还剩禁用物品规则。
3. ~~重新导出地图 gridnav / 高度~~ 已完成：`dota2_env/data/map.json`（`scripts/extract_map.py`），并接进了观测
   （`local_map` / `runes` / `landmarks`）。`tree_events` 跟着本队视野走（迷雾里的变化再次看到时带 `delayed` 补报），
   种树枝不产生事件（见 MAP_DATA.md 4.5）。后续：发芽的事件、前哨被占领后 `team_id` 是否跟着变还没验证。
4. 验证 `HOST_MODE_GUI_MENU`（人类自建房间对战）流程在新版大厅 UI 下是否还能把 bot 脚本指到本地 `bots`。
5. `host_timescale` 4× 以上的上限。多实例并行在 Docker 里解决了：一个容器一个实例，4 个并行实测过
   （[DOCKER.md](DOCKER.md) 第 5 节）；不用容器时同一台机器还是只能跑一个（端口、`pkill`、`bots` 软链接都是全局的）。
6. 查清死亡后金钱暴涨的原因。
7. 观测里还没有：modifier、投射物、肉山 / 魔方当前在哪；动作里还没有：对队友施法、信使。矢量施法的第二个点
   实测给不了（3.1），要再试就换新思路，别重复表里已经试过的。
