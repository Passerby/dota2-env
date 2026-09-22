# LLM 对局：一份 YAML 让不同的模型互相打

每个英雄一个 LLM agent，各自按自己的节奏决策，游戏不停下来等任何人。配置见
[configs/match.example.yaml](../configs/match.example.yaml)，入口是
[examples/llm_match.py](../examples/llm_match.py)。

```bash
uv pip install -e ".[dev,llm]"                                   # pyyaml + httpx + python-dotenv 是可选依赖
cp configs/match.example.yaml configs/my_match.yaml              # 填 model id
cp .env.example .env                                             # 填 key；直接 export 也行，且优先于 .env
python examples/llm_match.py --config configs/my_match.yaml --dry-run   # 不开游戏、不花钱
python examples/llm_match.py --config configs/my_match.yaml             # 真跑，需要 Steam
```

只支持 **OpenAI 兼容**的 `POST {base_url}/chat/completions`——DeepSeek、OpenRouter、通义、Kimi、智谱、
vLLM、Ollama 都是这个协议，所以没有按厂商分的适配层。`dota2_env/llm/` 不从 `dota2_env/__init__.py` 导出，
环境本身仍然只依赖 protobuf / gymnasium / numpy。

## 1. `gateways`

一个条目 = 一组 (key, base_url, model)。多个 agent 可以共用一个条目，共用同一个连接池。

| 键 | 必填 | 说明 |
|---|---|---|
| `base_url` | ✅ | 到 `/v1` 为止，代码会拼 `/chat/completions`。结尾的 `/` 会被去掉 |
| `api_key` | ✅ | `${VAR}` 从环境变量取，`examples/llm_match.py` 启动时会先读仓库根目录的 `.env`（模板 `.env.example`，已导出的变量优先）。也可以写明文，`.env` 和 `configs/*.yaml` 都已进 `.gitignore`（只有示例例外） |
| `model` | ✅ | 照各家文档填，本仓库不猜型号名 |
| `price_per_mtok` | | `{input: , output: }`，美元/百万 token。**只影响成本报告**，不影响请求 |
| `timeout_seconds` | | 默认 8.0。超时不抛异常，记一条 error，英雄保持上一条命令 |
| `params` | | 原样合并进请求 body：`temperature`、`max_tokens`，以及各家关思考的开关 |

关思考没有统一写法（有的换模型，有的传 `enable_thinking: false`，有的传 `reasoning_effort`），
所以它就是 `params` 里的一项，写在配置里而不是代码里。agent 自己的 `params` 覆盖 gateway 的。

## 2. `match`

| 键 | 默认 | 说明 |
|---|---|---|
| `mode` | `allpick5v5` | `allpick5v5` 或 `mid1v1`。1v1 是把整条链路跑通的便宜办法（1/5 的 token） |
| `timescale` | 4.0 | 游戏倍速。越快，模型在每个游戏秒里能用的墙钟越少 |
| `ticks_per_observation` | 6 | 30 tick = 1 游戏秒，所以 6 = 每秒 5 帧。这是**帧率**，不是决策频率 |
| `max_dota_time` | 600.0 | 传给 `AllPick5v5Rules` / `Mid1v1Rules`。满局 5v5 是 2400 |
| `spend_limit_usd` | 2.0 | 累计花费达到就**立即结束对局** |
| `log_dir` | `logs` | 每局一个 `<UTC 时间戳>.jsonl` |
| `plan_length` | 1 | 每次回复给几个连续动作，一帧一个。1 = 单动作（老行为），6 ≈ 覆盖 1.2 游戏秒 |
| `log_prompts` | false | 把发给模型的 observation 也写进日志。调试必开，日志会大一倍多 |
| `all_chat` | true | agent 可以在回复里加 `say` 喊话 |
| `share_team_state` | true | prompt 里附队友摘要，每轮约 70 token |
| `render` | false | 开游戏窗口 |

## 3. 两队

`radiant` / `dire` 各有 `control`（`agent` / `builtin` / `idle`）。`control: agent` 的队伍要列 5 个
`agents`；其他队伍只要 `heroes`。

**`agents` 的顺序就是英雄映射**：第 `i` 个 agent 驱动观测第 `i` 行，也就是 `info["player_ids"][i]`，
也就是本队 player id 升序的第 `i` 个。改顺序等于换英雄。

All Pick 不会把同一个英雄发给两边，所以**十个英雄名必须各不相同**，配置会校验这一点。

| agent 键 | 默认 | 说明 |
|---|---|---|
| `nickname` | ✅ | 进 prompt、进日志、进队友摘要 |
| `hero` | ✅ | 英雄单位名，如 `npc_dota_hero_lina` |
| `position` | ✅ | 1-5 号位：`safe` / `mid` / `offlane` / `support` / `hard_support`，即客户端的优势路 / 中路 / 劣势路 / 辅助 / 纯辅助。**只影响 prompt**（位置说明和队友摘要），环境不分路 |
| `gateway` | ✅ | 引用上面的某个 gateway |
| `decision_interval` | 1.0 | **游戏秒**。0.2 = 每帧（5 Hz），3.0 = 每 3 秒 |
| `params` | `{}` | 覆盖 gateway 的 `params` |

`position` 写 `offlane` 而不是 `off`：YAML 里裸的 `off` 会被解析成布尔假。

## 4. 先给理由，再给一串动作（`plan_length`）

`plan_length: 1` 时模型每次回一个动作，一帧下发、之后 NOOP 到下次决策——中间那几帧英雄是站着的。

`plan_length: 6` 时它回的是一个计划：

```jsonc
{"reason": "兵线在推，往前走去补那个远程兵",
 "actions": [{"type": "MOVE", "move": 4}, {"type": "MOVE", "move": 4}, {"type": "MOVE", "move": 4},
             {"type": "ATTACK", "target": 3}, {"type": "ATTACK", "target": 3}, {"type": "STOP"}]}
```

六个动作**一帧一个**依次下发，`ticks_per_observation: 6` 下正好覆盖 1.2 游戏秒，空档就填满了。

关键是**每个动作都在它真正下发的那一帧才被校验**：行号按 handle 重新定位（见 §5），合法性用当帧的 mask 查。
所以计划可以写得乐观——到时候不合法的那个动作会变成 NOOP 被跳过，不会连累后面的。
有一种情况掩码看不出来：同一技能还在转身 / 抬手时（还没进冷却，掩码照样是可用），客户端会忽略新下的同技能施法，
所以计划里连续两帧对同一技能换目标，第二次不生效（[VERSION_DIFF.md](VERSION_DIFF.md) 3.1）。
`reason` 只进日志，不影响执行，但它是看懂模型在想什么最直接的东西。

`plan_length × 帧长 ≈ decision_interval` 是自然的配法。计划比 interval 长也没关系，下一次回复到达时
没跑完的尾巴直接被替换掉（拿到的是更新的观测，这是好事）。模型回的动作多于 `plan_length` 会被截断，
少了就是计划短一点、早几帧进入 NOOP。回成单个动作对象（便宜模型经常这样）也照样接受。

代价是输出 token 从约 30 涨到约 180，system prompt 从约 420 涨到约 610。

## 5. 决策节奏是上限，不是速率

`decision_interval` 控制**发起**频率：到点了而且上一个请求已经回来，才发新的。一个比自己 interval 还慢的
模型，只会决策得更少——实际达成频率 = `min(1/interval, 1/延迟)`。把 5 Hz 配给一个 1.2 秒延迟的模型，
真实就是 0.8 Hz 左右。这就是"游戏不等人"的含义。

日志里的 `held_frames`（这一帧没有新决策）和 `mean_latency` 直接告诉你差多远；
`info["action_delivery"]` 和 `info["skipped_observations"]` 是环境自己那一侧的同类指标。

按游戏秒而不是墙钟计，是为了让 `timescale` 改了之后每个游戏分钟的决策数不变，两队也才可比。

## 6. 行号只能靠 handle 活过等待

计划里的动作一帧下发一个，跑完之后发 NOOP 直到下一份计划到达。`bridge/lua/actions/none.lua` 不下任何
order，所以英雄会继续执行上一条命令（`clear.lua` 才是清空的那个）——NOOP 不会让它停下。同一条命令也
绝不重发：单位表每帧重排（见 [FEATURES.md](FEATURES.md) §1.3），重发等于打错目标。

正因为重排，模型基于第 t 帧说的 `target 3`，在真正下发的那一帧要**按 handle 重新定位行号**——
发 prompt 时记下那一帧的 `unit_handles`，动作下发时把行号换成 handle 再查它现在的行号。这对计划尤其重要：
第 6 个动作要等 1.2 秒才下发，那时行号早就变了。目标已经消失（死了、出视野）就退回 NOOP，日志里记一条
`kind: "rejected"`。

## 7. 日志

`<log_dir>/<时间戳>.jsonl`，只由主线程写，三种行：

**`kind: "decision"`** —— 每次模型回复一行：

```jsonc
{"kind": "decision", "team": 2, "nickname": "影魔", "dota_time": 12.4,
 "decided_at": 11.2,          // 模型看到的是哪一帧，和 dota_time 的差就是它慢了多少游戏秒
 "latency": 0.9, "input_tokens": 1043, "output_tokens": 178, "usd": 0.000527,
 "reply": "...",              // 原始回复文本，一个字没改
 "reason": "兵线在推，往前走去补那个远程兵",
 "plan": [{"type": "MOVE", "move": 4}, ...],   // 读出来的动作序列，还没校验
 "prompt": "time 0:12\nnevermore lvl 1 ...",   // 只有 log_prompts: true 时才有
 "say": null, "error": null}
```

**`kind: "rejected"`** —— 某个计划里的动作在下发那一帧被判非法（其余动作照跑）：

```jsonc
{"kind": "rejected", "team": 2, "nickname": "影魔", "dota_time": 13.0,
 "step": {"type": "ATTACK", "target": 3}, "error": "target 3 is gone from the unit table"}
```

**`kind: "summary"`** —— 最后一行，每个 agent 的请求数 / 回复数 / 网络错 / 被拒动作数 /
已执行的计划步数 / 持有帧数 / token / 花费 / 平均延迟。

怎么看——先跑这个，它把下面这些都算好了：

```bash
python scripts/check_match.py logs/<时间戳>.jsonl
```

它会报：对局是否跨过了开赛号角（`dota_time 0`，之前地图上没小兵）、延迟分位、命令下发时已经旧了多少、
计划长度分布、被拒动作的原因排行、模型 `reason` 的样本、嘴炮条数、以及每个 agent 的汇总。

手动挖的话：

```bash
tail -1 logs/<时间戳>.jsonl | python -m json.tool                  # 收尾汇总
jq -r 'select(.kind=="decision") | "\(.dota_time) \(.nickname) \(.reason)"' logs/<时间戳>.jsonl
jq -r 'select(.kind=="rejected") | "\(.nickname) \(.error)"' logs/<时间戳>.jsonl | sort | uniq -c
jq -r 'select(.kind=="decision") | .prompt' logs/<时间戳>.jsonl | head -40   # 需要 log_prompts
```

**每个模型的 `rejected_actions` 比例是判断它值不值得跑的最便宜的信号。**

## 8. token 与花费估算

实测的单次请求（字符数 ÷ 3，5v5、对线期的观测）：

| | `plan_length: 1` | `plan_length: 6` |
|---|---|---|
| system prompt | ~446 | ~609 |
| 观测 + 队友摘要 | ~467 | ~467 |
| 输入合计 | **~913** | **~1076** |
| 输出 | ~30 | ~180 |

团战打满 32 行单位表时观测再加约 430。上表是加入物品栏和施法类型之前量的：物品栏每格一行，6 格全满约多 60；
每个技能 / 物品行后面的 `ready: CAST_TARGET CAST_DIRECTION` 之类每行约多 5；system prompt 现在是约 1000 字符
（`plan_length: 6` 约 1180）。

| 场景 | 请求数 | token | deepseek-flash 高峰价 |
|---|---|---|---|
| 5 人，10 分钟短局，1 Hz，单动作 | 2,875 | ~2.7 M | ~$1.2 |
| 5 人，10 分钟短局，1 Hz，6 动作计划 | 2,875 | ~3.6 M | ~$1.6 |
| 5 人，10 分钟短局，每 3 秒 | 1,000 | ~1.3 M | ~$0.6 |
| 10 人，40 分钟满局，1 Hz，6 动作计划 | 10,375 | ~13 M | ~$5.7 |

请求数按「延迟 0.9 秒 × timescale」封顶算的，不是按 `decision_interval` 直接除——见 §5。
成本是**输入主导**的，单位表占 prompt 的大头，所以 `MAX_UNITS` 是最大的成本旋钮；
`plan_length` 主要涨的是输出。开思考的话花费由**延迟**而不是 interval 决定。
`price_per_mtok` 填了才有美元数字；各家对缓存命中的输入另有计价，所以报告里的金额是上限估算。

## 9. 已知限制

- **chat 只能写，不能读**。`CMsgBotWorldState` 里没有聊天字段，所以 agent 喊的话谁也读不到，
  内置 bot 更不会回。跨队互喷要等阶段二（同进程驱动两队，在进程内回传）。
- **双队 LLM 还没实现**。两队都写 `control: agent` 会报错。Lua 那边已经支持（per-team 的 `heroes` /
  `control`、`actions_t<team>.lua`、ACK 带 team、两个 worldstate 端口一直都开），堵点是
  `DotaSession` 只管一队，而两个 env 实例不能共存（`pkill`、bots 软链接、固定端口都是全局的）。
  最小的缝是给 `DotaSession` 加一个 `game=` 参数复用同一个 `DotaGame`。
- **`ACTION_CHAT` 没在真实客户端上验证过**。Lua 侧的 dispatch 是现成的
  （`bot_controlled.lua.tpl:49-50`），但喊话是否真的显示，第一次真机跑要自己确认。
- 本文的 token 数基于 3 字符/token 的估计，没有用真实分词器量过。
