# dota2-env

Dota 2 的 [Gymnasium](https://gymnasium.farama.org/) 环境：用标准的 `reset()` / `step()` 接口控制一个真实
Dota 2 客户端里的英雄。由 [LastOrder-Dota2](../LastOrder-Dota2) 的运行环境抽离、重构而来，不含任何模型代码；
面向 RL 训练，也面向 LLM agent（自带文本观测 / JSON 动作封装）。

已在 2026-09 的客户端（macOS，无 GUI `-dedicated` 模式）上实测。新旧版本差异见 [docs/VERSION_DIFF.md](docs/VERSION_DIFF.md)。

```python
import gymnasium as gym
import dota2_env

env = gym.make("dota2_env/Mid1v1-v0", timescale=4)          # 无 GUI，4 倍速，对手为 Valve 内置 bot
observation, info = env.reset()
for _ in range(1000):
    action = env.unwrapped.sample_legal_action()             # 或你的策略；info["action_mask"] 给出合法动作
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
```

- 游戏路径默认取各平台 Steam 默认位置，可用环境变量 `DOTA_GAME_PATH` 覆盖（指到 `.../dota 2 beta/game`）。
- 运行时会把 `<dota>/game/dota/scripts/vscripts/bots` 软链接到临时会话目录，`close()` 时移除；
  如果那里已有真实的 `bots` 目录会拒绝运行。Windows 上创建软链接需要管理员权限。
- 同一台机器同一时间只能跑一个环境实例（端口固定，启动前会 `pkill dota2`）。

## 环境 `dota2_env/Mid1v1-v0`

1v1 中路，默认双方影魔。`gym.make` 的主要参数（全部参数、Dota 启动参数和时序常量见 [docs/PARAMETERS.md](docs/PARAMETERS.md)）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `render_mode` | `None` | `None`/`"ansi"` 无 GUI；`"ansi"` 时 `render()` 返回文本；`"human"` 打开游戏窗口（控制台输入 `jointeam spec` 才有视角） |
| `team_id` | `TEAM_RADIANT` | 控制哪一方，两边都实测过 |
| `hero` / `opponent_hero` | `npc_dota_hero_nevermore` | 任意英雄单位名 |
| `opponent` | `"builtin"` | `"builtin"` Valve 内置 bot AI；`"idle"` 站着不动 |
| `timescale` | `1.0` | `host_timescale`。实测 4 倍速稳定（20 step/s） |
| `ticks_per_observation` | `6` | 每步的游戏 tick 数，30 tick = 1 游戏秒 |
| `reward_fn` / `rules` | `LaningReward()` / `Mid1v1Rules()` | 可替换，见 `rewards.py` |
| `starting_items` / `ability_priority` | 出门装 / `(5,0,3,4,1,2)` | 自动购买 / 自动加点；传 `()` 关闭，改用 `queue_purchase()` / `queue_train_ability()` |

**观测**（`spaces.Dict`，全部 float32，已归一化，字段名见 `observation.py` 的 `*_FEATURES`）

| key | shape | 内容 |
|---|---|---|
| `hero` | (25,) | 位置、朝向、血/蓝、等级、攻击、金钱、补刀、状态、时间 |
| `abilities` | (6, 3) | 槽位 0-5：等级、冷却、是否可施放 |
| `units` | (32, 16) | 1600 范围内最近的 32 个单位（英雄/小兵/野怪/塔），相对坐标、阵营、血量、几刀能杀、是否在打我… |
| `unit_mask` | (32,) | `units` 哪些行有效 |

**动作**（`spaces.Dict`）：`type`（NOOP / MOVE / ATTACK / CAST / CAST_TARGET / STOP）、`move`（16 方向，每步约 300 距离）、
`target`（`units` 表的行号）、`ability`（槽位）。不相关的字段会被忽略。

**info**：`action_mask`（`type` / `attack_target` / `cast_target` / `ability` 四个 0/1 数组）、`world_state`（原始
`CMsgBotWorldState`）、`dota_time`、`reward`（各奖励分量，未加权）、`winner`、出错时 `error`；
`action_delivery` / `skipped_observations`（动作是否被游戏执行、延迟多少、策略跳过了多少帧）。

**奖励**（`LaningReward`，权重可传参覆盖）：正补、反补、升级、自身血量变化、敌方英雄血量变化、击杀、死亡、双方一塔血量、胜负。

**终止**（`Mid1v1Rules`）：任一方第 2 次死亡（任何死因，和客户端原生 1v1 规则一致）或一塔被破 → `terminated`；
`dota_time ≥ 600` → `truncated`；客户端无数据且无胜负记录（崩溃）→ `truncated` 且 `info["error"]`。

**Wrappers**（`dota2_env.wrappers`）

- `FlatObservationWrapper` / `FlatActionWrapper`：拍平成 `Box` / `MultiDiscrete`，给不支持 Dict 的 RL 库用。
- `TextWrapper`：给 LLM 用。观测是文本（单位行号与 `target` 一致，技能名来自运行中的客户端），动作是
  `{"type": "ATTACK", "target": 3}` 这样的 JSON；不合法的动作变成 NOOP 并写进 `info["action_error"]`。
  用法见 [examples/llm_agent.py](examples/llm_agent.py)（未实测，需要 `anthropic` 和 API 凭据）。

## 需要知道的行为

- 每次 `reset()` 都会重启 Dota（无 GUI 约 10-20 秒）。Dota 不能设随机种子，`seed` 只影响 `env.np_random`。
- 游戏是实时的：`step()` 写入动作后阻塞到下一帧观测，不会等你的策略。策略越慢，英雄按上一条指令执行得越久。
- 新版客户端里，**复活后站在泉水里的英雄不会出现在 worldstate 里**，直到它移动。环境此时把英雄当作站在出生点、
  只开放 MOVE，走一步后恢复正常。
- 决定胜负的那一下（第二次死亡 / 破塔）之后客户端立刻停止推送，环境从 console.log 里读胜负。

## 结构

```
dota2_env/
  envs/mid1v1.py     DotaMid1v1Env（gymnasium.Env）
  observation.py     worldstate -> numpy 观测、单位表
  actions.py         动作空间、合法性 mask、到 bridge 动作的翻译
  rewards.py         LaningReward、Mid1v1Rules
  text.py            文本观测（render_mode="ansi" / LLM）
  wrappers.py        Flat* / TextWrapper
  bridge/            与 Dota 通信的底层，不依赖 gymnasium
    game.py          DotaGame：会话目录、bots 软链接、启动参数、写动作/配置文件
    worldstate.py    socket 读取与监听子进程
    session.py       DotaSession：一局比赛的 start / observe / act / close
    console_sync.py  console.log 动作回执（延迟 / 丢步统计，可选）
    protos/          Valve 最新 CMsgBotWorldState proto 及生成码
    lua/             游戏内 bot 脚本
tests/               假 session + pytest（含 gymnasium check_env）
examples/            random_agent / scripted_agent / llm_agent / latency_check
scripts/             probe_worldstate.py（新客户端兼容性探测）、probe_http.py（bot VM 的 HTTP 能力探测）
docs/                PARAMETERS.md、VERSION_DIFF.md、BRIDGE_ACTIONS.md、IPC_CHANNELS.md
```

通信机制：Dota 以 `-botworldstatetosocket_*` 启动，在 TCP 12120/12121 上每 N tick 推送一帧
`CMsgBotWorldState`；动作写成 `bots/actions_t<team>.lua`（`return '<json>'`），游戏内 Lua 每 tick `loadfile` 执行。
底层 JSON 格式见 [docs/BRIDGE_ACTIONS.md](docs/BRIDGE_ACTIONS.md)。为什么是文件而不是 bot VM 自带的
`CreateHTTPRequest`（实测数据与对比）见 [docs/IPC_CHANNELS.md](docs/IPC_CHANNELS.md)。

重新生成 proto：从 SteamDatabase/Protobufs 的 `dota2/` 取 `dota_gcmessages_common_bot_script.proto` 和
`valveextensions.proto` 放进 `dota2_env/bridge/protos/`，执行
`protoc -I dota2_env/bridge/protos --python_out=dota2_env/bridge/protos dota2_env/bridge/protos/*.proto`，
再把生成码里的 `import valveextensions_pb2` 改成 `from . import valveextensions_pb2`。
