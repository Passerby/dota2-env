# dota2-env

Dota 2 的 [Gymnasium](https://gymnasium.farama.org/) 环境：用标准的 `reset()` / `step()` 接口控制真实
Dota 2 客户端里的英雄——1v1 中路一个，或者 5v5 全阵营一整队。由 [LastOrder-Dota2](https://github.com/bilibili/LastOrder-Dota2)
的运行环境抽离、重构而来，不含任何模型代码；面向 RL 训练，也面向 LLM agent（自带文本观测 / JSON 动作封装）。

已在 2026-09 的客户端（macOS，无 GUI `-dedicated` 模式）上实测。新旧版本差异见 [docs/VERSION_DIFF.md](docs/VERSION_DIFF.md)。

```python
import gymnasium as gym
import dota2_env

env = gym.make('dota2_env/Mid1v1-v0', timescale=4)  # 无 GUI，4 倍速，对手为 Valve 内置 bot
observation, info = env.reset()
for _ in range(1000):
    action = env.unwrapped.sample_legal_action()  # 或你的策略；info["action_mask"] 给出合法动作
    observation, reward, terminated, truncated, info = env.step(action)
    if terminated or truncated:
        observation, info = env.reset()
env.close()
```

## 安装

```bash
uv venv -p 3.12 && uv pip install -e ".[dev]"
pytest                                        # 不需要 Dota，用假的 session 跑
python examples/scripted_agent.py             # 需要 Steam 在运行；脚本化补刀 agent 打内置 bot，约 2 分钟一局
python examples/scripted_5v5.py               # 同上，5v5 全阵营，一队 5 个英雄都由脚本驱动
python examples/llm_match.py --config configs/match.example.yaml --dry-run   # LLM 对局，先验证配置
```

观测 / 动作 / 奖励的每一个字段见 [docs/FEATURES.md](docs/FEATURES.md)。

- 游戏路径默认取各平台 Steam 默认位置，可用环境变量 `DOTA_GAME_PATH` 覆盖（指到 `.../dota 2 beta/game`）。
- 运行时会把 `<dota>/game/dota/scripts/vscripts/bots` 软链接到临时会话目录，`close()` 时移除；
  如果那里已有真实的 `bots` 目录会拒绝运行。Windows 上创建软链接需要管理员权限。
- 同一台机器同一时间只能跑一个环境实例（端口固定，启动前会 `pkill dota2`）。

## 环境 `dota2_env/Mid1v1-v0`

1v1 中路，默认双方影魔。`gym.make` 的主要参数（全部参数、Dota 启动参数和时序常量见 [docs/PARAMETERS.md](docs/PARAMETERS.md)）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `render_mode` | `None` | `None`/`"ansi"` 无 GUI；`"ansi"` 时 `render()` 返回文本；`"human"` 打开游戏窗口，进图后自动进观战视角 |
| `team_id` | `TEAM_RADIANT` | 控制哪一方，两边都实测过 |
| `hero` / `opponent_hero` | `npc_dota_hero_nevermore` | 任意英雄单位名 |
| `opponent` | `"builtin"` | `"builtin"` Valve 内置 bot AI；`"idle"` 站着不动 |
| `timescale` | `1.0` | `host_timescale`。实测 4 倍速稳定（20 step/s） |
| `ticks_per_observation` | `6` | 每步的游戏 tick 数，30 tick = 1 游戏秒 |
| `reward_fn` / `rules` | `LaningReward()` / `Mid1v1Rules()` | 可替换，见 `rewards.py` |
| `starting_items` / `ability_priority` | 出门装 / `(5,0,3,4,1,2)` | 自动购买 / 自动加点；传 `()` 关闭，改用 `queue_purchase()` / `queue_train_ability()`。天赋不自动加：每个到了等级、还没选的天赋层留一个技能点给 `TALENT` |
| `restock_tp` | `True` | 身上、储藏处、信使都没有回城卷轴时自动买一张；野外买的放在储藏处，用 `COURIER` 送 |
| `replay_dir` | `None` | 给个目录（相对路径相对 CWD）就录像，见下面「录像」（⚠️ 当前客户端上会让这一局提前崩） |

**观测**（`spaces.Dict`，全部 float32，已归一化，字段名见 `observation.py` 的 `*_FEATURES`）

| key | shape | 内容 |
|---|---|---|
| `hero` | (28,) | 位置、朝向、血/蓝、等级、攻击、金钱、补刀、状态、时间、回城卷轴次数与冷却、储藏处件数 |
| `abilities` | (6, 3) | 槽位 0-5：等级、冷却、是否可施放 |
| `items` | (6, 4) | 背包格 0-5：物品 id、次数、冷却、是否可用；名字用 `env.unwrapped.cast_slots()` 读 |
| `talents` | (8,) | 8 个天赋学了没有，每两个一层（10/15/20/25 级），下标就是 `TALENT` 的 `talent` |
| `units` | (32, 16) | 1600 范围内最近的 32 个单位（英雄/小兵/野怪/塔），相对坐标、阵营、血量、几刀能杀、是否在打我… |
| `unit_mask` | (32,) | `units` 哪些行有效 |
| `local_map` | (3, 33, 33) | 以英雄所在格为中心、每格 64 单位的局部地图：不可走、活树、相对高度（比英雄高几级），见 [docs/MAP_DATA.md](docs/MAP_DATA.md) |
| `runes` | (4, 4) | 4 个神符点（强化 / 赏金各 2）：相对位置、距离、可能有符（刷新时就标上，本队看到空了才清掉） |
| `landmarks` | (10, 4) | 肉山坑、魔方、智慧神龛、莲花池、前哨各 2 个：相对位置、距离、前哨是否归我方 |

**动作**（`spaces.Dict`）：`type`（NOOP / MOVE / ATTACK / CAST / CAST_TARGET / STOP / CAST_DIRECTION / MOVE_TO /
PICKUP_RUNE / TP / TALENT / COURIER）、
`move`（16 方向，每步约 300 距离，下的是走寻路的 `MOVE_TO_POSITION`）、`target`（`units` 表的行号）、
`ability`（0-5 是技能槽，6-11 是背包格 0-5）、`point`（`MOVE_TO` 的世界坐标 (x, y)：终点原样交给客户端，整条路由它规划，
去远处用它，一步 300 的 `MOVE` 被墙挡住会原地不动；`TP` 也用它）、`rune`（`runes` 表的行号）、`talent`（`talents` 的下标）。
`CAST` 无目标或对自己用，`CAST_TARGET` 对单位用（只能指地面的技能打在它脚下），`CAST_DIRECTION` 朝 `move`
方向按施法距离放；矢量技能（法球、滚滚）的第二个点顺着施法方向延伸，由服务器 VM 代发，
见 [docs/FEATURES.md](docs/FEATURES.md) 2.1、2.6。`PICKUP_RUNE` 走过去捡神符（由服务器 VM 代发），`TP` 用回城卷轴传送，
`TALENT` 学天赋，`COURIER` 让信使把储藏处的东西送来；后两个不打断英雄手上的事，见 2.7。不相关的字段会被忽略。

**info**：`action_mask`（`type` / `attack_target` / `cast_target` / `ability` / `rune` / `talent` 六个 0/1 数组，`ability` 按动作类型分行，
每个格子能用哪几种施法由 Lua 上报的施法类型决定）、`world_state`（原始
`CMsgBotWorldState`）、`world_states`（上一步以来的每一帧，旧的在前，最后一帧就是 `world_state`；记事件的人一帧都不能漏）、
`dota_time`、`reward`（各奖励分量，未加权）、`winner`、出错时 `error`；
`action_delivery` / `skipped_observations`（动作是否被游戏执行、延迟多少、有多少帧策略没来得及据以行动）。

**奖励**（`LaningReward`，权重可传参覆盖）：正补、反补、升级、自身血量变化、敌方英雄血量变化、击杀、死亡、双方一塔血量、胜负。

**终止**（`Mid1v1Rules`）：任一方第 2 次死亡（任何死因，和客户端原生 1v1 规则一致）或一塔被破 → `terminated`；
`dota_time ≥ 600` → `truncated`；客户端无数据且无胜负记录（崩溃）→ `truncated` 且 `info["error"]`。

## 环境 `dota2_env/AllPick5v5-v0`

全阵营（All Pick）整局，本方 5 个英雄**全部**由 agent 控制，对手是 Valve 内置 bot。参数和 1v1 基本一致，
差别是 `heroes` / `opponent_heroes` 各收 5 个英雄名（默认影魔 / 火枪 / 莉娜 / 莱恩 / 冰女），
`rules` 默认 `AllPick5v5Rules(max_dota_time=2400)`。

**观测**（`spaces.Dict`）：就是把 1v1 那份按英雄堆叠，再加一条全局向量。

| key | shape | 内容 |
|---|---|---|
| `heroes` | (5, 28) | 每行一个受控英雄，字段同 1v1 的 `hero` |
| `abilities` | (5, 6, 3) | |
| `items` | (5, 6, 4) | |
| `talents` | (5, 8) | |
| `units` | (5, 32, 16) | 每个英雄各有一张自己的邻近单位表 |
| `unit_mask` | (5, 32) | |
| `local_map` | (5, 3, 33, 33) | 每个英雄各以自己为中心 |
| `runes` | (5, 4, 4) | |
| `landmarks` | (5, 10, 4) | |
| `team` | (11,) | 时间、双方存活人数、金钱、双方剩余塔数、双方基地血量、击杀 / 死亡 |

**动作**：`type` / `move` / `target` / `ability` / `rune` / `talent` 六个 `MultiDiscrete([...] * 5)` 加 `point`（(5, 2) 的 `Box`），
第 `i` 位（`point` 的第 `i` 行）驱动第 `i` 个英雄。
行号 `i` 对应 `info["player_ids"][i]`（本方 player id 升序），一局内固定。

**奖励**（`TeamReward`）：正补、反补、升级、自身血量、敌方血量（以上都对 5 人求和）、击杀、死亡、
整座塔的得失、双方基地血量、胜负。队伍级标量，不做个体信用分配。

**终止**：任一方基地被破 → `terminated`；`dota_time ≥ 2400` → `truncated`。

```python
env = gym.make('dota2_env/AllPick5v5-v0', timescale=4)
observation, info = env.reset()  # 选英雄 + 策略阶段，比 1v1 久，默认等 600s
# {'type': (5,), 'move': (5,), 'target': (5,), 'ability': (5,), 'point': (5, 2), 'rune': (5,), 'talent': (5,)}
action = env.unwrapped.sample_legal_action()
```

**Wrappers**（`dota2_env.wrappers`）

- `FlatObservationWrapper`：把 Dict 观测拍平成一个 `Box`，两个环境都能用。
- `FlatActionWrapper`：`MultiDiscrete([type, move, target, ability])`，**只适用于 1v1**
  （5v5 的动作空间本来就是 `MultiDiscrete`）。带不了坐标，所以只有前 7 种动作，`MOVE_TO` 及之后的都没有。
- `TextWrapper`：给 LLM 用，**只适用于 1v1**。观测是文本（单位行号与 `target` 一致，技能名、物品名和每个格子能用的施法类型来自运行中的客户端，
  后面跟官方中文名；另有地形、看得见的敌方英雄、各路兵线、神符点、地标几行，见 [docs/LLM_MATCH.md](docs/LLM_MATCH.md) §8），
  动作是 `{"type": "ATTACK", "target": 3}`、`{"type": "MOVE_TO", "point": [1180, -1216]}` 这样的 JSON；
  不合法的动作变成 NOOP 并写进 `info["action_error"]`。
  单英雄用法见 [examples/llm_agent.py](examples/llm_agent.py)（未实测，需要 `anthropic` 和 API 凭据）；
  一整队各用一个模型见下面的 LLM 对局。
  5v5 的文本观测可以直接用 `env.render()`（`render_mode="ansi"`，每个英雄一段）。

## 录像

无 GUI 模式没有画面，想回看只能靠录像。给 `replay_dir` 一个目录就打开 GOTV 录制，`close()` 时把这一局的
`.dem` 从 Dota 安装目录**移动**到那里（不会在游戏目录里堆积），路径同时写进 `env.unwrapped.replay_path`：

```python
env = gym.make('dota2_env/AllPick5v5-v0', timescale=4, replay_dir='replays')
try:
    ...
finally:
    env.close()
    print(env.unwrapped.replay_path)  # replays/auto-20260920-0117-start-dota2_env.dem
```

两个 example 都带 `--replay [DIR]`（不给目录就用 `replays/`）。看的时候把 `.dem` 拖进 Dota 客户端，
或者控制台 `playdemo <路径>`。

⚠️ **在 2026-09 的 macOS 客户端上，开录像会让客户端崩溃，这一局到 `dota_time` 约 +139s 就结束了。**
崩溃报告里是 GOTV 自己的 `HLTVServerAsync` 线程段错误（`EXC_BAD_ACCESS`），整个进程没了，worldstate
断流只是副作用。崩溃时刻 = GOTV 开始广播的时刻 = 广播源起点（实测稳定在 `dota_time +19.2`）+ `tv_delay`，
而 demo 里的画面又恰恰是广播开始之后才写的，所以把 `tv_delay` 调大只会让它什么都录不到。六种 `+tv_*`
组合的实测对照见 [docs/VERSION_DIFF.md](docs/VERSION_DIFF.md) 1.2 节。

所以现在 `replay_dir` 的实际能力是：**录到约 110 秒画面，然后这一局结束**。够看 agent 开局长什么样，
**不要在训练里开**；开的时候会打一条 warning。旧版客户端（Last Order 时代）同样的参数是能录完整局的，
换 Linux / Windows 原生很可能也没这个问题，但都没验证过。

拿到的 `.dem` 一律是**未收尾**的（文件头 `CDemoFileInfo` 偏移为 0，环境会打 `replay ... is unfinalized`）：
客户端只在正常退出时补写它，而它既不从 stdin 收命令，SIGTERM / SIGINT 也是立刻死。不过**未收尾的
文件实测可以正常打开播放**。

## 需要知道的行为

- 每次 `reset()` 都会重启 Dota（无 GUI 约 10-20 秒）。Dota 不能设随机种子，`seed` 只影响 `env.np_random`。
- 游戏是实时的：`step()` 写入动作后阻塞到下一帧观测，不会等你的策略。策略越慢，英雄按上一条指令执行得越久。
- 新版客户端里，**复活后站在泉水里的英雄不会出现在 worldstate 里**，直到它移动。环境此时把英雄当作站在出生点、
  只开放 MOVE 和 MOVE_TO；agent 发别的动作（包括 NOOP）时环境替它朝地图中心走一步，下一帧就恢复正常。
- 决定胜负的那一下（第二次死亡 / 破塔）之后客户端立刻停止推送，环境从 console.log 里读胜负。
- 观测的地图部分读 `dota2_env/data/map.json`，它是按客户端版本从游戏文件和 bot API 导出的（6934 / 7.41f）。
  Dota 打了地图补丁就要重跑 `scripts/extract_map.py`，否则树的编号会错位；环境发现本机客户端版本和
  `map.json` 不一致时会打 warning。见 [docs/MAP_DATA.md](docs/MAP_DATA.md)。
- `close()` 是先 SIGTERM 自己那个客户端进程再等它退出（超时 20 秒才 SIGKILL），并且在 `finally` 里一定会
  摘掉 `bots` 软链接、清掉会话目录——中间哪一步抛异常都不会把软链接留在 Dota 安装目录里。
  启动前那次 `pkill dota2` 保留着，用来清理上次跑崩的残留。

## LLM 对局（`examples/llm_match.py`）

一份 YAML 定义 LLM gateway（key / base_url / model）和两队各 5 个 agent（昵称、英雄、位置、用哪个 gateway、
决策频率），跑起来就是不同模型互相打。只支持 OpenAI 兼容的 `/chat/completions`，所以 DeepSeek、OpenRouter、
通义、Kimi、vLLM、Ollama 都能直接用。

```bash
uv pip install -e ".[dev,llm]"                  # pyyaml + httpx + python-dotenv + jinja2 是可选依赖，环境本身不需要
cp .env.example .env                            # 填 API key；.env 已进 .gitignore
python examples/llm_match.py --config configs/match.example.yaml --dry-run
```

决策是异步的：每个 agent 按自己的 `decision_interval`（游戏秒）发请求，慢的模型只是决策得更少，
不会冻住游戏。`plan_length` 让模型一次给出一串连续动作（一行一个，如 `MOVE, 1180, -1216`、`ATTACK, 3`，
最后一行是理由），一帧下发一个，填掉决策之间的空档；请求是流式的，第一行一到就下发，不等模型写完。带 token / 花费计量与硬上限、all-chat 喊话、队内信息共享、JSONL 日志
（`log_prompts` 连 prompt 一起存，方便调试）。system 里有本英雄每个技能和天赋的官方中文说明，以及本模式神符、智慧神龛、
莲花池什么时候在哪出现（实测的时间表，`data/events.json`）；user 里有身上物品的说明（价格、合成、神秘商店）、地形、
看得见的敌方英雄和各路兵线（带绝对坐标）、神符点、地标、这几样东西下一次什么时候来，最后是最近几条命令和下发时英雄站的位置。
`MOVE, x, y` 下的是 `MOVE_TO`，由游戏自己寻路走到那里。每个键的含义、prompt 的组成、节奏语义、token 估算和已知限制见
[docs/LLM_MATCH.md](docs/LLM_MATCH.md)；配置模板是 [configs/match.example.yaml](configs/match.example.yaml)。

调 prompt 用 `python scripts/prompt_debugger.py --config configs/<配置>.yaml`：浏览器里逐次看模型收到的 prompt、
回复每行怎么被读的、哪一步被拒了，也能把某一次的 prompt 改了用同一个 gateway 重发几份对比（LLM_MATCH.md §7）。

现在一队 LLM 打内置 bot；两队都是 LLM 还没接（原因见 LLM_MATCH.md §10）。

## 结构

```
dota2_env/
  envs/mid1v1.py     DotaMid1v1Env（gymnasium.Env）
  envs/allpick5v5.py DotaAllPick5v5Env（一队 5 个英雄）
  observation.py     worldstate -> numpy 观测、单位表、队伍观测
  map_features.py    map.json 的加载、每局的树表、观测的地图部分（local_map / runes / landmarks），events.json 的时间表
  data/              map.json（地图）、items / abilities / heroes.json（中英文技能物品文本、合成、神秘商店）和
                     events.json（神符 / 智慧神龛 / 莲花的刷新时间表），都由 scripts/ 生成
  game_text.py       读 data/ 的文本：官方中英文名、本英雄技能说明、物品说明（价格 / 合成 / 神秘商店）
  actions.py         动作空间、合法性 mask、到 bridge 动作的翻译
  rewards.py         LaningReward / Mid1v1Rules、TeamReward / AllPick5v5Rules
  text.py            文本观测（render_mode="ansi" / LLM）
  wrappers.py        Flat* / TextWrapper
  llm/               LLM 对局：config（YAML）、gateway（OpenAI 兼容）、agent（prompt/解析）、runner（异步循环）、
                     prompts/（system / user 两条消息的 Jinja 模板，改 prompt 措辞只改这里）
  bridge/            与 Dota 通信的底层，不依赖 gymnasium
    game.py          DotaGame：会话目录、bots 软链接、启动参数、写动作/配置文件（每队 5 个英雄）
    worldstate.py    socket 读取与监听子进程
    session.py       DotaSession：一局比赛的 start / observe / act / close
    console_sync.py  console.log 动作回执（延迟 / 丢步统计，可选）
    protos/          Valve 最新 CMsgBotWorldState proto 及生成码
    lua/             游戏内 bot 脚本
tests/               假 session + pytest（含 gymnasium check_env）
examples/            random_agent / scripted_agent / scripted_5v5 / llm_agent / llm_match / latency_check
configs/             LLM 对局配置模板
scripts/             probe_worldstate.py（新客户端兼容性探测）、probe_http.py（bot VM 的 HTTP 能力探测）、
                     check_match.py（把一份 LLM 对局日志读成一页体检报告）、
                     prompt_debugger.py + prompt_debugger/（浏览对局日志、改 prompt 重发的本地网页）、
                     extract_map.py + map_scan.lua（导出 map.json）、fetch_game_text.py（导出中英文文本）、
                     probe_trees.py（核对 tree_id）、probe_events.py（实测刷新时间，导出 events.json）
docs/                FEATURES.md、PARAMETERS.md、LLM_MATCH.md、VERSION_DIFF.md、BRIDGE_ACTIONS.md、IPC_CHANNELS.md、
                     MAP_DATA.md、SERVER_VM.md、REFERENCES.md
```

通信机制：Dota 以 `-botworldstatetosocket_*` 启动，在 TCP 12120/12121 上每 N tick 推送一帧
`CMsgBotWorldState`；动作写成 `bots/actions_t<team>.lua`（`return '<json>'`），游戏内 Lua 每 tick `loadfile` 执行。
底层 JSON 格式见 [docs/BRIDGE_ACTIONS.md](docs/BRIDGE_ACTIONS.md)。为什么是文件而不是 bot VM 自带的
`CreateHTTPRequest`（实测数据与对比）见 [docs/IPC_CHANNELS.md](docs/IPC_CHANNELS.md)。

重新生成 proto：从 [SteamDatabase/Protobufs](https://github.com/SteamDatabase/Protobufs) 的 `dota2/` 取
`dota_gcmessages_common_bot_script.proto` 和 `valveextensions.proto` 放进 `dota2_env/bridge/protos/`，执行
`protoc -I dota2_env/bridge/protos --python_out=dota2_env/bridge/protos dota2_env/bridge/protos/*.proto`，
再把生成码里的 `import valveextensions_pb2` 改成 `from . import valveextensions_pb2`。

## 致谢与引用

本仓库包含或改写了下列项目的代码：

| 项目 | 许可证 | 用在哪里 |
|---|---|---|
| [bilibili/LastOrder-Dota2](https://github.com/bilibili/LastOrder-Dota2) | MIT | 整个运行环境从它抽离、重构而来；`dota2_env/bridge/lua/` 大部分文件原样取自它的 `dotaservice/lua/`，其余在它基础上改写 |
| [TimZaman/dotaservice](https://github.com/TimZaman/dotaservice) | [Beerware](https://github.com/TimZaman/dotaservice/blob/master/LICENSE)，Copyright (c) 2018 Tim Zaman et al. | LastOrder 内嵌的那份 Lua 动作层（`action_processor.lua`、`actions/`），以及 worldstate 走 TCP 12120/12121 的接法 |
| [Nostrademous/Dota2-WebAI](https://github.com/Nostrademous/Dota2-WebAI) | MIT | 文件头标着 `AUTHOR: Nostrademous` 的动作脚本（经 dotaservice 改写后流传下来） |
| [jagt/pprint.lua](https://github.com/jagt/pprint.lua) | Public Domain | `dota2_env/bridge/lua/pprint.lua`（dotaservice 改过） |
| [Tieske/uuid](https://github.com/Tieske/uuid)（原始代码出自 Rackspace） | Apache-2.0 | `dota2_env/bridge/lua/uuid.lua`，文件头保留了原许可声明 |
| [SteamDatabase/Protobufs](https://github.com/SteamDatabase/Protobufs) | —（Valve 的游戏文件） | `dota2_env/bridge/protos/*.proto`：从 Dota 2 客户端提取的 bot 接口定义 |

数据：`dota2_env/data/` 下的文件由 `scripts/` 从 Valve 的游戏文件、bot 脚本 API 和 dota2.com 的 datafeed
提取，内容属于 Valve，不适用本仓库的许可证。解地图实体表用的是
[ValveResourceFormat](https://github.com/ValveResourceFormat/ValveResourceFormat) 的 Source2Viewer-CLI（MIT），
它只在提取时运行，不随仓库分发。

参考资料：Valve 官方的 [Dota Bot Scripting](https://developer.valvesoftware.com/wiki/Dota_Bot_Scripting)、
[ModDota Lua (Bots) API](https://docs.moddota.com/lua_bots/)、[2aius/d2ai](https://github.com/2aius/d2ai)、
dotaservice 的 [NOTES.md](https://github.com/TimZaman/dotaservice/blob/master/NOTES.md)（后三者引用在哪里见 [docs/IPC_CHANNELS.md](docs/IPC_CHANNELS.md) §5）；
[leamare/dota-interactive-map](https://github.com/leamare/dota-interactive-map)（ISC）的 7.41 数据用来人工对照
`map.json`，没有拷贝（见 [docs/MAP_DATA.md](docs/MAP_DATA.md) §7）。

同类工作（别人怎么让 LLM 实时打游戏、MOBA 上的 LLM 研究）以及和本项目的对比见 [docs/REFERENCES.md](docs/REFERENCES.md)。

## 许可证

[MIT](LICENSE)。LastOrder-Dota2（bilibili）和 Dota2-WebAI（Nostrademous）同为 MIT，它们的版权声明一并列在
LICENSE 里；来自 dotaservice 和 `uuid.lua` 的部分继续适用上表里各自的许可证。

Dota 2 是 Valve Corporation 的商标，本项目与 Valve 无关。
