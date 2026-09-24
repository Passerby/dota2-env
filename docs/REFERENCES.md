# 相关工作：还有谁在让 LLM 实时打游戏

调研于 2026-09-23：网页搜索，加上读项目页、论文摘要和作者本人的发言。这个方向变化很快，「没人做」这类结论只对调研日期有效。
✅ = 读过一手来源（项目页、论文、作者原话），⚠️ = 只看了二手报道或搜索摘要。

## 结论

- 没找到让 LLM 在 Dota 2 里实时打完整对局的公开项目，LLM 对 LLM、5v5 一个模型管一个英雄的就更没有。
  Dota 上现有的工作都是强化学习或手写脚本 bot（§5）。
- 最接近的两个：
  - Brood War Bench（§1，星际 1，2026-09-19）：实时、LLM 对 LLM，但每方只有一个 agent。
  - 腾讯的 Think in Games（§2.1，王者荣耀）：是 MOBA，但模型只离线预测宏观动作，不实时操作。
- 各家撞上的是同一个问题：游戏不等模型想完。应对办法的对照见 §4。

## 1. Brood War Bench（星际争霸：母巢之战）

[报告](https://bw.swerdlow.dev/report)，作者 Ben Swerdlow（[@benswerd](https://twitter.com/benswerd)），2026-09-19 发布，
[HN 上](https://news.ycombinator.com/item?id=49766966)有 150 多条讨论。✅

**做法**

- 19 个「模型 × 推理强度」的组合打单循环，共 171 局，三个种族都有。打到时限的局记为 T，不算胜负。
- 每个模型跑在自家的 agent 工具里：Claude Code、Codex、Grok Build。作者在 HN 上说这主要是为了省钱：他手上有大量免费额度，
  不想按 API 价格付费。
- 游戏这一侧只给两个 BWAPI 工具：下命令、取观测（落地页演示里叫 `issue_commands` 和 `observation`）。作者的理由是，
  在他这个规模下这样最公平。
- 游戏实时进行，不等模型。对局在 Freestyle 的 VM 上并行跑，每局保存引擎数据和双方 agent 的日志。
- 报告里没有代码链接。[落地页](https://bw.swerdlow.dev/)可以带自己的 agent 上去打。

**结果**（节选，全表 19 行见报告）

| 名次 | 配置 | 胜-负 | APM | 每局花费 |
|---|---|---|---|---|
| 1 | Codex Astra / xhigh | 18-0 | 12.6 | $10.54 |
| 3 | Claude Fable | 15-3 | 12.6 | $12.24 |
| 4 | Codex Astra / low | 14-4 | 25.7 | $21.07 |
| 5 | Codex 5.6 Sol / medium | 13-5 | 10.1 | $5.12 |
| 7 | Claude Opus 5 | 12-6 | 10.5 | $20.78 |
| 8 | Codex 5.6 Sol / xhigh | 11-7 | 8.0 | $3.23 |
| 12 | Codex 5.6 Terra / low | 8-10 | 48.3 | $4.65 |
| 14 | Claude Sonnet | 7-11 | 6.2 | $8.98 |
| 16 | Grok 4.6 / xhigh | 2-15 | 2.8 | $0.66 |
| 18 | Claude Haiku | 0-16 | 0.3 | $0.34 |

- 作者的判断：没有一个超过新手水平，一个会光子炮 rush 的新手能把这些局全赢下来。
- 推理强度和胜率的关系因模型而异。Astra 强度越高赢得越多（xhigh 18-0，low 14-4）；Sol 和 Luna 反而是低强度赢得多
  （Sol medium 13-5、xhigh 11-7；Luna low 9-9、xhigh 7-11）。强度越低，APM 越高。
- 低强度不一定省钱：Astra low 每局 $21.07，是 xhigh 的两倍。

**几条发现，和本项目的对应**

| 他们看到的 | 本项目里对应的地方 |
|---|---|
| 老模型把即时战略当回合制玩，想着想着就被打死了 | 回复边写边执行（[LLM_MATCH.md](LLM_MATCH.md) §4），决策节奏是上限（§5），超时后英雄保持上一条命令（§1 `timeout_seconds`） |
| Grok 4.6 xhigh 有一局 43 分钟用了 11,138 个推理 token，只发了 6 批命令，一个战斗单位都没出 | [`scripts/check_match.py`](../scripts/check_match.py) 的 `held`（没有新决策的帧）和 `latency` 两列量的就是这个 |
| 推理强度是单独的一个维度，而且不是越高越好 | 关思考、调强度都写在 `params` 里（§1），agent 自己的 `params` 覆盖 gateway 的，同一个模型可以配几档一起打 |
| Codex 自己拆出经济、生产、部队三个子 agent，彼此不怎么交流，兵造一个就送一个 | 5v5 每队 5 个独立 agent，是同一个问题的放大版；`share_team_state`（§2）开和关可以直接对比 |
| Codex 派一个农民去骚扰，对面花几十秒琢磨怎么处理它 | 实时环境里，逼对手多想本身就是一种打法 |

**和本项目的区别**

| | Brood War Bench | dota2-env |
|---|---|---|
| 测的是什么 | 模型加上它自家的 agent 工具。工具还能自己拆子 agent，所以分不出哪部分是模型的功劳 | 同一套 harness 下的裸模型（OpenAI 兼容接口），只有模型是变量 |
| 每方几个 LLM | 1 个 | 1v1 中路 1 个，5v5 每方 5 个 |
| 固定参照 | 只有模型互打，外加「新手能赢」的定性判断 | 可以拿 Valve 内置 bot（`control: builtin`，§3）当固定对手 |
| 统计 | 胜负、APM、每局花费、胜率对花费图、19 × 19 对阵表，以及科技、工人、兵力、建筑、存钱、人口随时间的曲线 | `check_match.py` 只报 harness 这一侧（延迟、token、花费、被拒的动作），还没有游戏这一侧的曲线，比如补刀、GPM/XPM、经济、没花掉的钱、推塔 |

**作者说的下一步**（HN）：加 code mode（他举的例子是 marine staggering 这种微操）；打多局，比如 BO5，让 agent
从前几局里学、自己攒自动化脚本；人够多的话开放成锦标赛。

## 2. MOBA

### 2.1 Think in Games（TiG），腾讯

[arXiv 2508.21365](https://arxiv.org/abs/2508.21365)，2025-08。摘要 ✅，下面的细节来自
[The Decoder 的报道](https://the-decoder.com/tencent-trains-ai-that-can-explain-and-execute-game-strategies-in-honor-of-kings/) ⚠️。

- 游戏是王者荣耀。定义了 40 个宏观动作（报道里举的例子是 Push top lane、Secure dragon、Defend base），
  真实对局录像里的每个时刻都标成其中一个。
- 先做 SFT，再用 GRPO。Qwen3-14B 训完，宏观动作的准确率是 90.91%，DeepSeek-R1 是 86.67%。
- 和本项目的区别：它离线预测「这时候该干什么」，模型不上手操作。本项目的模型在真实客户端里实时下指令。

### 2.2 其它

- ⚠️ Honor of Kings Arena（[OpenReview](https://openreview.net/pdf?id=7e6W6LEOBg3)）：王者荣耀上的强化学习环境，
  相当于本项目 RL 那一半在王者荣耀上的对应物。只看了标题。
- ⚠️ Grok 5 对 T1（英雄联盟，[Dexerto 的报道](https://www.dexerto.com/league-of-legends/t1-accepts-elon-musks-challenge-for-top-lol-team-to-compete-against-grok-ai-3286754/)）：
  2025-11 马斯克提议让 Grok 5 只通过摄像头看屏幕，反应和点击速度限制在人类水平，去打 T1；T1 接受了。
  截至调研日没搜到比赛结果。

## 3. 其它实时游戏上的 LLM 基准

| 项目 | 游戏 | 游戏等不等模型 | 说明 |
|---|---|---|---|
| [TextStarCraft II](https://arxiv.org/abs/2312.11865)（NeurIPS 2024）✅ | 星际 2 | 摘要没说 | 文本观测、宏观命令，用 Chain of Summarization 做单帧和多帧总结；对手是内置 AI，打到 Harder（Lv5） |
| [LLM-PySC2](https://arxiv.org/abs/2411.05348) ✅ | 星际 2 | 异步查询，延迟不随 agent 数量增长 | 完整的 pysc2 动作空间，多 agent 协作；结论是模型没有明确指令时会幻觉、协作低效 |
| [SC2Arena / StarEvolve](https://arxiv.org/abs/2508.10428) ✅ | 星际 2 | 摘要没说 | 文本观测、低层动作空间、全种族；StarEvolve 是「规划—执行—校验」的分层框架，还用对局数据微调 |
| [Orak](https://arxiv.org/abs/2506.03610) ✅ | 12 款游戏，其中街霸 3、超级马里奥、星际 2 是实时的 | 这三款评测时暂停 | 走 MCP 工具接入。街霸 3 有 8 个模型、星际 2 有 7 个模型两两对战，算 Elo；论文把星际 2 改成不暂停之后，模型在困难难度下全是 0 分 |
| [LLM Colosseum](https://github.com/OpenGenerativeAI/llm-colosseum)（2024）✅ | 街霸 3 | 不等，多线程 | 模型看屏幕的文字描述或截图，输出一串招式；模型之间对打，按 Elo 排名 |
| [LM Fight Arena](https://arxiv.org/abs/2510.08928)（2025-10）✅ | 真人快打 2 | 不等 | 6 个多模态模型打锦标赛，双方用同一个角色 |
| [WrathBench](https://wrathbench.shard.page) ✅ | 魔兽世界巫妖王（AzerothCore 私服） | 不等，一局就是真实的 90 分钟 | 单人练级、做任务，不对战。模型写 TypeScript 片段调用 SDK，有自己改写的草稿本和只追加的日志，在旅店或城里休息时可以进反思模式 |
| [Kaggle Game Arena](https://www.kaggle.com/game-arena) ⚠️ | 国际象棋、德州扑克、狼人杀、四子棋、Dark Hex | 回合制 | 列在这里作对照：LLM 对 LLM，但没有时间压力 |

## 4. 怎么对付「游戏不等模型」

| 做法 | 谁在用 |
|---|---|
| 模型直接出动作；回复流式到达，写完一行执行一行；决策节奏按游戏秒计；超时就保持上一条命令 | 本项目（[LLM_MATCH.md](LLM_MATCH.md) §4、§5） |
| 节奏交给 agent 工具自己安排，要不要拆子 agent 也由它决定 | Brood War Bench。结果：想太久的被打死，子 agent 之间不协作 |
| 模型写代码，代码按游戏的速度跑，模型隔一段时间回来看、改代码 | WrathBench；Brood War Bench 的作者也打算加 code mode |
| 开两个线程：一个在冻结的状态上慢慢规划，一个按时出动作，并且能读到规划线程写了一半的推理 | AgileThinker（见下） |
| 评测时把游戏暂停 | Orak 对它的三款实时游戏 |
| 反过来，把 AI 的反应速度压到人类水平 | Grok 5 对 T1 的提议 |

[Real-Time Reasoning Agents in Evolving Environments](https://arxiv.org/abs/2511.04898)（Wen、Ye、Zhang、Yang、Zhu，ICLR 2026）✅

- 把「模型还在想，环境已经在变」形式化，做了一个 Real-Time Reasoning Gym，里面是 Freeway、Snake、Overcooked 三个游戏。
- AgileThinker 就是上表里的双线程做法。实验主要用 DeepSeek V3 和 R1，因为这个做法要读得到推理过程。
- 用生成的 token 数当时钟：每生成 N 个 token，环境走一步。论文实测它和墙钟时间的 R² 是 0.9986。
- 本项目用的是真实延迟，结果里混着网关和机房的速度。要做和硬件无关、可复现的对比，可以借鉴这种 token 时钟。

## 5. Dota 上已有的（都不是 LLM）

- [OpenAI Five](https://arxiv.org/abs/1912.06680)：大规模强化学习，5v5，英雄池有限制。
- LastOrder-Dota2、dotaservice、Dota2-WebAI：本项目代码的来源，见 [README](../README.md) 的「致谢与引用」。其中 Dota2-WebAI
  让网页服务器做宏观决策、Lua 负责执行，和本项目「模型决策、Lua 执行」的分工最像，但没做完就停了。
- ⚠️ [OpenHyperAI](https://github.com/forest0xia/dota2bot-OpenHyperAI)：社区维护的手写脚本 bot，支持 127 个英雄（7.41 版本）。

下次更新这份文档，可以从 ⚠️ [BAAI-Agents/GPA-LM](https://github.com/BAAI-Agents/GPA-LM)（大模型玩游戏的论文清单）和
Brood War Bench 的 HN 讨论开始。
