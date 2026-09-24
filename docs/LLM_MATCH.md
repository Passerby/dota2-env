# LLM 对局：一份 YAML 让不同的模型互相打

每个英雄一个 LLM agent，各自按自己的节奏决策，游戏不停下来等任何人。配置见
[configs/match.example.yaml](../configs/match.example.yaml)，入口是
[examples/llm_match.py](../examples/llm_match.py)。

```bash
uv pip install -e ".[dev,llm]"                                   # pyyaml + httpx + python-dotenv + jinja2 是可选依赖
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
| `timeout_seconds` | | 默认 8.0，管的是**整个回复**：流式请求里 httpx 的超时只管两个数据块之间的间隔，所以另外按总时长掐断。超时不抛异常，已经收到的行照常执行，记一条 error，之后英雄保持上一条命令 |
| `params` | | 原样合并进请求 body：`temperature`、`max_tokens`，以及各家关思考的开关。`stream` 永远是 true，改不掉 |

关思考没有统一写法（有的换模型，有的传 `enable_thinking: false`，有的传 `reasoning_effort`），
所以它就是 `params` 里的一项，写在配置里而不是代码里。agent 自己的 `params` 覆盖 gateway 的。

请求是流式的（`stream: true`，外加 `stream_options: {include_usage: true}`），回复一行一行地到（见 §4）。
token 数从流的最后一个数据块里读：DeepSeek 要 `include_usage` 才给，OpenRouter 总是给（带 `cost`），
Kimi 放在最后那个 choice 里面，这三种都认。一家网关在流里不报 usage，它的 token 和花费就记成 0，
`spend_limit_usd` 也就管不住它（§10）。

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
| `history_length` | 6 | user 最后列出最近几条命令（NOOP 不算）和下发时英雄站的位置，见 §8 |
| `log_prompts` | false | 把发给模型的 observation 也写进日志。调试必开，日志会大一倍多 |
| `all_chat` | true | agent 可以在回复里加一行 `SAY, ...` 喊话 |
| `show_reason` | true | 每份回复的 REASON 放在那个英雄的血条上方（服务器 VM 的 `SetCustomHealthLabel`，中文最多 85 个字），只有开窗口（`render`）才看得见，见 [SERVER_VM.md](SERVER_VM.md) §5 |
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

## 4. 一行一个动作，边写边执行（`plan_length`）

回复是纯文本，一行一个动作：开头是动作类型，后面的数字用英文逗号隔开，没有引号也没有键名，最后一行是 `REASON`。
`plan_length: 6` 时是这样：

```text
MOVE, 1180, -1216
NOOP
NOOP
ATTACK, 3
NOOP
NOOP
REASON, 兵线在推，往前走去补那个远程兵
```

`MOVE` 和 `CAST_DIRECTION` 靠后面数字的个数区分：`MOVE, x, y` 走到坐标，`MOVE, D` 朝方向 D 走，
`CAST_DIRECTION, S, x, y` 和 `CAST_DIRECTION, S, D` 同理。每种动作的写法在 `agent.FORMS` 里，system prompt
的动作表和它一一对应（有测试钉住）。另外四种：`PICKUP_RUNE, R`（R 是 "rune spots" 行方括号里的编号）、
`TP, x, y`（坐标原样交给环境的 `TP`，不换算成方向）、`TALENT, T`（T 是 system 天赋表里的编号）、`COURIER`。
`TALENT` 和 `COURIER` 不打断英雄手上的事，和 NOOP 一样；它们的环境语义见 [FEATURES.md](FEATURES.md) 2.7。

`MOVE, x, y` 下的是环境的 `MOVE_TO`（[FEATURES.md](FEATURES.md) 2.1）：终点原样交给游戏，由它规划整条路一直走到那里，
直到下一条 NOOP 以外的命令，所以发一次、后面接 NOOP 就行。以前它被换成 16 个方向之一、每条只走 300，寻路也只在这
300 里绕，方向上横着一堵墙英雄就原地不动：上一局冰女在自家基地边这样卡了两分多钟。`MOVE, D` 仍然只走约 300，
system prompt 只让它用来短距离挪一下（躲技能、拉开距离）：那一局模型用方向走的 MOVE 占到 47%（JSON 时只有 11%），
方向又常算错，火枪要回西北方向的一塔却连发 `MOVE, 12`（正南），顶着地图边缘站了约 100 秒。

`plan_length: 1` 时就是一行动作加一行 REASON：动作一帧下发，之后 NOOP 到下次决策，英雄在那几帧里接着做这条命令，
做完了就站着。

**请求是流式的，第一行一到就下发。** gateway 每收到一个换行就把那一行交出去，主线程在下一帧把它放进计划：
一份回复的第一个动作取代旧计划还没跑完的部分，马上下发，不等模型写完后面的行。REASON 放在最后就是为了这个，
放在前面的话英雄得先等模型写完一整句话。之后的动作一帧最多下发一个；行来得比帧慢（加速跑的时候）就到一行发一行。
拿上一局 deepseek-flash 的 2,025 份回复换算：同样的计划，JSON 的中位数是 195 个字符，一行一个是 82 个；
第一个动作写到第 17 个字符就完整了，而 JSON 要整份收完才能解析。

读行的规则（`agent.read_line`，大小写不敏感）：

- 空行和 markdown 代码块的 ```` ``` ```` 行跳过。
- `REASON, ...`：逗号后面整段都是理由（理由里可以再有逗号），最多 200 字符。只进日志和英雄头顶（`show_reason`），
  不影响执行，但它是看懂模型在想什么最直接的东西。
- `SAY, ...`：嘲讽，开了 `all_chat` 才发，一份回复只发第一条，最多 80 字符。REASON 和 SAY 后面跟的是中文，
  模型常把紧跟着的逗号写成全角，所以这两行的逗号写成 `，` 或冒号也认；动作行只认英文逗号。
- 其余每一行都是一个动作。多于 `plan_length` 的动作行丢掉，后面的 REASON / SAY 照读；少了就是计划短一点、
  早几帧进入 NOOP。

没有换行结尾的最后一行，只有回复正常结束才算数（`finish_reason` 是 `stop`，或者网关没给）。`max_tokens` 用完或者中途出错时，
它可能只写了一半——`MOVE, 11` 也许是 `MOVE, 1180, -1216` 的前半截——所以这时它只留在日志的 `reply` 里。
回复中途断掉（超时、网关报错），断之前收到的动作照样执行。

六个动作**一帧一个**依次下发，`ticks_per_observation: 6` 下正好覆盖 1.2 游戏秒，空档就填满了。
攻击后面接的是 NOOP，不是 STOP 也不是再一个动作：NOOP 以外的动作一下发就替换掉手上的命令（`Action_*` 替换整个队列，
[VERSION_DIFF.md](VERSION_DIFF.md) 3.1），还没打出去的这一下就没了，持续施法也会被打断；`ATTACK` 本身只打一下
（`once: true`），走进射程、转身、抬手都算在里面。system prompt 在动作列表后面把这几条讲给模型，并让它在攻击和
持续施法后面留够 NOOP；STOP 只在真想打断的时候用。

关键是**每个动作都在它真正下发的那一帧才被校验**（`agent.resolve`）：行号按 handle 重新定位（见 §6），
`CAST_DIRECTION` 的坐标换成英雄当时所在位置看过去的方向，合法性用当帧的 mask 查。所以计划可以写得乐观——
到时候不合法的那个动作会变成 NOOP 被跳过，不会连累后面的。动作类型不认识、数字个数不对、写的不是数字，也是这样变成 NOOP，原因写进日志并在下一轮
告诉模型；坐标外面的括号会被去掉，因为模型常从坐标表里连括号一起抄。
有一种情况掩码看不出来：同一技能还在转身 / 抬手时（还没进冷却，掩码照样是可用），客户端会忽略新下的同技能施法，
所以计划里连续两帧对同一技能换目标，第二次不生效（[VERSION_DIFF.md](VERSION_DIFF.md) 3.1）。

`plan_length × 帧长 ≈ decision_interval` 是自然的配法。计划比 interval 长也没关系，下一份回复的第一行一到，
没跑完的尾巴直接被替换掉（拿到的是更新的观测，这是好事）。

## 5. 决策节奏是上限，不是速率

`decision_interval` 控制**发起**频率：到点了而且上一份回复已经传完，才发新的。一个比自己 interval 还慢的
模型，只会决策得更少——实际达成频率 = `min(1/interval, 1/延迟)`。把 5 Hz 配给一个 1.2 秒延迟的模型，
真实就是 0.8 Hz 左右。这就是"游戏不等人"的含义。

日志里的 `held_frames`（这一帧没有新决策）和 `mean_latency` 直接告诉你差多远；
`info["action_delivery"]` 和 `info["skipped_observations"]` 是环境自己那一侧的同类指标。

按游戏秒而不是墙钟计，是为了让 `timescale` 改了之后每个游戏分钟的决策数不变，两队也才可比。

## 6. 行号只能靠 handle 活过等待

计划里的动作一帧下发一个，跑完之后发 NOOP 直到下一份回复的第一行到达。`bridge/lua/actions/none.lua` 不下任何
order，所以英雄会继续执行上一条命令（`clear.lua` 才是清空的那个）——NOOP 不会让它停下。同一条命令也
绝不重发：单位表每帧重排（见 [FEATURES.md](FEATURES.md) §1.3），重发等于打错目标。

正因为重排，模型基于第 t 帧说的 `ATTACK, 3`，在真正下发的那一帧要**按 handle 重新定位行号**——
发 prompt 时记下那一帧的 `unit_handles`，动作下发时把行号换成 handle 再查它现在的行号。这对计划尤其重要：
第 6 个动作要等 1.2 秒才下发，那时行号早就变了。目标已经消失（死了、出视野）就退回 NOOP，日志里记一条
`kind: "rejected"`。

## 7. 日志

`<log_dir>/<时间戳>.jsonl`，只由主线程写，三种行：

**`kind: "decision"`** —— 每份回复传完时一行（它的动作在那之前就已经开始下发了）：

```jsonc
{"kind": "decision", "team": 2, "nickname": "影魔", "dota_time": 12.4,
 "decided_at": 11.2,          // 模型看到的是哪一帧，和 dota_time 的差就是整份回复慢了多少游戏秒
 "latency": 0.9,              // 墙钟秒，到整份回复传完
 "first_line_latency": 0.35,  // 墙钟秒，到第一行传到，也就是英雄最早能照它行动的时候；一行都没有时是 null
 "input_tokens": 1043, "output_tokens": 45, "usd": 0.000527,
 "reply": "MOVE, 4\nMOVE, 4\n...",   // 原始回复文本，一个字没改，包括被截断没执行的半行
 "reason": "兵线在推，往前走去补那个远程兵",
 "plan": ["MOVE, 4", "MOVE, 4", "ATTACK, 3", "NOOP"],   // 读出来的动作行，原样，还没校验
 "prompt": "time 0:12\nnevermore lvl 1 ...",   // 只有 log_prompts: true 时才有
 "say": null, "error": null}
```

回复中途断掉时 `error` 是网关的错误，`reply` / `plan` 是断之前收到的部分；传完了却一个动作都没有时，
`error` 是 `reply carried no actions`。

**`kind: "rejected"`** —— 某个计划里的动作在下发那一帧被判非法（其余动作照跑）：

```jsonc
{"kind": "rejected", "team": 2, "nickname": "影魔", "dota_time": 13.0,
 "step": "ATTACK, 3", "error": "target 3 is gone from the unit table"}
```

**`kind: "summary"`** —— 最后一行，每个 agent 的请求数 / 回复数 / 网络错 / 被拒动作数 /
已执行的计划步数 / 持有帧数 / token / 花费 / 平均延迟。

怎么看——先跑这个，它把下面这些都算好了：

```bash
python scripts/check_match.py logs/<时间戳>.jsonl
```

它会报：对局是否跨过了开赛号角（`dota_time 0`，之前地图上没小兵）、整份回复和第一行的延迟分位、回复传完时观测已经旧了多少、
计划长度分布、被拒动作的原因排行、模型 `reason` 的样本、嘴炮条数、以及每个 agent 的汇总。

手动挖的话：

```bash
tail -1 logs/<时间戳>.jsonl | python -m json.tool                  # 收尾汇总
jq -r 'select(.kind=="decision") | "\(.dota_time) \(.nickname) \(.reason)"' logs/<时间戳>.jsonl
jq -r 'select(.kind=="rejected") | "\(.nickname) \(.error)"' logs/<时间戳>.jsonl | sort | uniq -c
jq -r 'select(.kind=="decision") | .prompt' logs/<时间戳>.jsonl | head -40   # 需要 log_prompts
```

**每个模型的 `rejected_actions` 比例是判断它值不值得跑的最便宜的信号。**

## 8. prompt 里有什么

每次决策发两条消息，不带对话历史（最近几条命令在 user 的最后，见下）。名字一律用客户端自己的官方中文（英雄、技能、物品来自 datafeed 的简体中文，
神符点、地标、商店、标签来自客户端的 `dota_schinese.txt`），数据来源见 [MAP_DATA.md](MAP_DATA.md) §6。

两条消息的措辞和版式都在 Jinja 模板里：[system.jinja](../dota2_env/llm/prompts/system.jinja) 和
[user.jinja](../dota2_env/llm/prompts/user.jinja)。`agent.system_prompt()` / `agent.user_prompt()` 只把数据交给模板
（`agent` / `match` 两份配置、客户端的队伍和分路名、坐标表、技能说明、物品说明、状态块、队友、最近的命令），
自己不拼句子，所以改措辞、调顺序、加减段落都只改模板。状态块、技能说明和物品说明是 `text.py` / `game_text.py`
渲染好的整块文字：它们也是环境 `render_mode="ansi"` 的输出，环境不能依赖 jinja2，所以留在 Python 里。
改模板的时候知道这几点：

- 环境是 `StrictUndefined`：模板里拼错或没传的变量会直接抛 `UndefinedError`（带模板文件名和行号），不会默默少一句。
- 只有标签的行（`{% if %}`、`{% for %}`、`{# 注释 #}`）不会留在 prompt 里，模板里的空行就是 prompt 里的空行；
  模板最后一行结尾的换行会被去掉。
- 模板里还能用过滤器 `display_name`（`{{ agent.hero | display_name('en') }}` 是英文名）、`clock`（游戏时间写成 `1:05`，
  和状态块的 `time` 一样）和全局表 `positions`（1–5 号位的名字）。
- 改完跑 `pytest tests/test_llm.py`（两条消息的版式有测试钉住），再用 `--dry-run` 看 system 全文。

**system**：整局不变，网关的前缀缓存一直能命中。依次是：

1. 身份：昵称、天辉 / 夜魇、英雄（"影魔（Shadow Fiend）"，括号里是英文名，模型读过的攻略大多是英文）、位置和位置职责、胜负条件。
   然后是地图方位：两边基地在哪、上中下路和河道怎么走、**你们的优势路是下路（天辉）还是上路（夜魇）**。第一次实机试跑时
   没有这一段，1 号位说着"开局走上路优势路"一路走进了夜魇那半边（天辉的优势路是下路），改动前的日志里也是一样。
   接着是一张坐标表（`text.map_reference()`，数据来自 map.json），坐标和状态里的 `pos`、队友位置是同一套：
   ```
   天辉: 遗迹 (-5920, -5352); 上路一塔 (-6336, 1856); 上路二塔 (-6501, -872); …; 四塔 (-5712, -4864); 四塔 (-5392, -5192)
   夜魇: 遗迹 (5528, 5000); 上路一塔 (-5275, 6036); …
   神符点: 上路强化神符 (-1640, 1112); 下路强化神符 (1180, -1216); 上路赏金神符 (-996, 4431); 下路赏金神符 (595, -4660)
   地标: 上路肉山巢穴 (-3194, 2395); 下路肉山巢穴 (2860, -2765); …; 上路前哨 (-4096, -448); 下路前哨 (3392, -448)
   ```
2. **本英雄的技能**（`game_text.hero_text()`），按技能栏顺序，每条开头是状态里 "abilities" 下用的内部名：
   ```
   - nevermore_shadowraze1 毁灭阴影（Shadowraze）：冷却时间 9 秒，魔法消耗 75
     影魔对其正前方区域释放毁灭能量，……
     基础伤害：85 / 150 / 215 / 280；距离：200；连中额外伤害：35 / 50 / 65 / 80；……
     提示：连中效果的持续时间在每次连中时刷新。
     阿哈利姆魔晶：每击中一名敌方英雄后冷却时间减少2秒。……
   - nevermore_shadowraze2 毁灭阴影（Shadowraze）：同 nevermore_shadowraze1，距离：450
   ```
   先天技能、被动、要神杖或魔晶才有的技能会标出来；和上一条同名同说明的技能只写差别。
   接着是**天赋表**，四层各两个，编号就是 `TALENT, T` 的 T（名字和数值来自 `abilities.json`）：
   ```
   10 级：[0] +30 毁灭阴影连中伤害，或 [1] +30 灵魂盛宴攻击速度
   15 级：[2] +1.5 魔王降临降低护甲，或 [3] +2 灵魂盛宴每名英雄收集灵魂
   ```
3. 状态里没写明的约定：行号、相对位置 `(dx, dy)`、地形行、看得见的敌方英雄和兵线两行、塔 / 神符 / 地标行、技能编号、
   矢量技能、天赋怎么学（技能点自动加，但每个到了等级、还没选的天赋层都留一个点，等模型用 `TALENT` 选）、
   回城卷轴（持续施法秒数和 800 的落点规则从 `items.json` 读）、用掉后自动补买、野外补买的放在储藏处要用 `COURIER` 送、
   每轮最后那几条命令怎么看（几条下来位置都没变就是被挡住了，换一种做法），然后是动作格式，
   一行一个动作，数字个数区分坐标和方向（§4）：
   ```
   MOVE, x, y                  走到坐标 (x, y)，游戏自己找路，绕开树和悬崖
   MOVE, D                     朝方向 D 走约 300，只用来短距离挪一下，比如躲技能、拉开距离
   CAST_DIRECTION, S, x, y     朝坐标 (x, y) 那个方向放到最远施法距离
   ```
   `x, y` 是绝对坐标，和上面那张表、状态里的 `pos`、敌方英雄和兵线的 `at` 同一套，所以模型可以直接抄。
   `MOVE, x, y` 原样交给游戏（`MOVE_TO`）；`CAST_DIRECTION` 的坐标由 `agent.resolve()` 在动作**真正下发的那一帧**
   换成方向 D（`atan2` 到 16 个方向之一），用的是英雄当时的位置，所以计划里后面几行的坐标不会因为人走开了而跑偏。
   移动下的都是 `MOVE_TO_POSITION`，走客户端自己的寻路，会绕开树和悬崖（以前下的是直线的 `MOVE_DIRECTLY`）。
   动作列表后面讲命令怎么互相取代（见 §4）：`NOOP` 让英雄接着做手上的事，`STOP` 打断它；`ATTACK` 只打一下，
   打出去之前来了别的动作就没了；说明里写着"持续施法"的技能和物品要等它放完。以前这里写的是
   `{"type": "STOP"} 或 {"type": "NOOP"}`，像是两个一样的东西，模型拿 STOP 当"等一下"用会把攻击和持续施法打断。
4. 回复的样子（`plan_length` 行动作，最后一行 `REASON, ...`）和"边写边执行"：第一行一写完就下发，所以最要紧的
   动作写在第一行；开了 `all_chat` 再加喊话规则（REASON 前面加一行 `SAY, ...`）。计划里每个动作下发时取代前一个，
   所以要在攻击和持续施法后面留够 NOOP。

**user**：每次决策重新渲染，段落之间空一行：

1. **身上物品的说明**（物品栏 6–11 去重，数据里没有的物品不写）。物品栏不变它就不变，所以放在最前面，前缀缓存还能盖住它：
   ```
   - item_magic_wand 魔杖（Magic Wand）：价格 460，冷却时间 15 秒
     合成：魔棒 + 铁树枝干 + 铁树枝干 + 图纸（150）
     主动：充能 ……
   - item_hyperstone 振奋宝石（Hyperstone）：价格 2000，神秘商店有售
   - item_rapier 圣剑（Divine Rapier）：价格 5600，冷却时间 6 秒
     合成：圣者遗物（神秘商店） + 恶魔刀锋（神秘商店）
   ```
   有几种配方就用"，或"连起来（动力鞋三种）；图纸写价格，零价图纸不算一件；不朽之守护这类商店不卖的不写价格。
2. `text.describe()` 的状态块（标签是英文，名字是中文）。和地图有关的几行（`TeamRunner.decide()` 要传 `trees`，
   也就是 `env.unwrapped.trees`，不传就没有）：
   ```
   terrain: height 1 (0 river .. 3 fountain), nearest tree dist 101 (-27,-97), within 300: wall (+32,+0) (+30,+12); tree (-96,+0) (-89,-37)
   ...
     [0] enemy tower hp 2500/2500 dist 1211 (+832,+880) attackable higher
   enemy heroes in sight: 斧王 lvl 3 hp 540/760 at (1024, -380) dist 2210; 巫医 lvl 2 hp 520/520 at (-5810, 3260) dist 6100
   creep waves: 上路 ours 4 hp 88% at (-6021, 3500) dist 1650; 上路 enemy 3 hp 100% at (-5800, 3900) dist 2100; 中路 ours 4 hp 70% at (-900, -700) dist 600; …
   our towers, outermost standing per lane: 上路一塔 dist 9553 (-9056,-3040); 中路一塔 dist 7611 (-4264,-6304); 下路一塔 dist 11477 (+2140,-11275)
   enemy towers, outermost standing per lane: 上路一塔 dist 8075 (-7995,+1140); 中路一塔 dist 4778 (-2196,-4244); 下路一塔 dist 7970 (+3549,-7136)
   rune spots: [0] 上路强化神符 dist 5773 (-4360,-3784) available; [1] 下路强化神符 dist 6303 (-1540,-6112); [2] 上路赏金神符 dist 3745 (-3716,-465) available; …
   landmarks: 上路肉山巢穴 dist 6422 (-5914,-2501); 下路肉山巢穴 dist 7662 (+140,-7661); …; 上路前哨 dist 8661 (-6816,-5344) ours; 下路前哨 dist 5386 (+672,-5344) enemy
   ```
   - 地点一律写成和单位表一样的相对位置：`dist` 是直线距离，`(dx, dy)` 是它的坐标减去英雄的 `pos`。
     绝对坐标只在 system 的那张表里出现一次，省得每轮重复。第一版这里写的是 16 格罗盘方向编号（`dir 0`），
     两个方位差 13° 的地方会落进同一格，看起来像错的，已经去掉。
   - `height` 是 map.json 的高度级（0 河道 … 3 泉水台）；单位行末的 `higher` / `lower` 是那个单位比你高 / 低。
   - `nearest tree` 是 700 内离你最近、本队知道还立着的树（树表跟着 tree_events 更新），对树之祭祀用 CAST 吃的就是它。
   - `within 300` 沿 16 个 move 方向每 32 单位看一次，把 300 内先碰到的 `wall` / `tree` / `up` / `down` 按种类列出位置，空地不列。
   - `enemy heroes in sight` / `creep waves` 是单位表 1600 以外的补充：本队看得见的每个敌方英雄（等级、血量），和每一波小兵
     （同一方彼此 800 以内的算一波；属于哪条路看 map.json 里离它最近的那条路径，同一条路按从天辉到夜魇的顺序排；个数、总血量）。
     这两行写的是**绝对坐标**（`at`）和距离：它们就是给 `MOVE, x, y` 抄的，不用模型自己拿 `pos` 加减。上一局上路的两个英雄
     整场站在自己一塔下"等兵线过来"，1600 内一个小兵都看不到，2:42 还是 1 级；中路在号角前走进了敌方中路一塔。
     1v1 两边泉水里停着的填位英雄不算敌方英雄（和单位表同一个过滤）。
   - `our towers` / `enemy towers`：每条路上两方最外面那座还立着的塔（一塔倒了换二塔），是找线、认危险区的锚点；
     敌方的塔开局就在本方的 world state 里（map.json 的建筑就是从天辉视角的第一帧取的），不需要视野。
   - `available` 的意思见 [FEATURES.md](FEATURES.md) §1.6（刷新就标上，不需要视野）；方括号里是 `PICKUP_RUNE` 的 R。
   - 技能行下面、有学了或现在能学的天赋时，多一行 `talents: [1] +30 灵魂盛宴攻击速度 learned; [2] … / [3] … ready: TALENT`。
   - 物品行最后是 TP 格：`TP slot: item_tpscroll 回城卷轴 charges 1 ready: TP`（冷却中写 `cd 43.1s`）。储藏处有东西、
     或者信使正带着东西时，再多一行 `stash: item_tpscroll 回城卷轴 charges 1; courier dist 5212 (-4012,-3325)`
     （信使死了写 `courier dead`，带着东西时后面跟 `carrying …`）。
   - 英雄行、技能行、物品行、单位表里的英雄都带中文名；"上路 / 下路"就是 map.json 的 top / bottom。
3. 队友摘要（队友英雄也是中文名）。
4. 最近的命令（`agent.Note`，最多 `history_length` 条，旧的在前）：每条是下发那一帧的游戏时间、英雄当时站的位置、
   原样的那一行，后面跟"已经下达"或"被拒绝了：<原因>"。NOOP 不算，因为它不改变英雄在做的事。请求失败、一行动作都没收到
   记一条"你的回复没能送达：<原因>"，回复传完了却没有一行动作记"你的回复无法使用：<原因>"；回复中途断掉但已经有动作下发的，
   只记下发了的那几行。还没下过命令时是"你还没有下过命令"。
   ```
   你最近的命令，旧的在前，括号里是命令下发时你站的位置：
     1:31 (5856, -8416) MOVE, 12 已经下达
     1:32 (5856, -8416) MOVE, 12 已经下达
     1:33 (5856, -8416) ATTACK, 3 被拒绝了：target 3 is gone from the unit table
   ```
   以前这里只有一句"你上一条命令是 X，已经下达"：命令下发了，英雄却没动，模型看不出来，火枪就是这样对着地图边缘
   发了约 100 秒 `MOVE, 12`。现在同一个位置连着出现，就是被挡住了。

实机核对（无头 5v5，示例配置的五个英雄，假网关只记录不花钱）：五个英雄技能栏 0–5 里客户端报的技能名，
system 里全都有说明（莱恩槽位 4 的 `generic_hidden` 是占位，本来就没有文字）；600 条 user 里 392 条有
`available`、147 条有 `tree@`、62 条有坡；英雄实际 `z // 128` 与 map.json 高度级 4,000 帧里 3,856 帧一致。

## 9. token 与花费估算

实测的单次请求（字符数 ÷ 3，5v5、对线期的观测）：

| | `plan_length: 1` | `plan_length: 6` |
|---|---|---|
| system prompt | ~446 | ~609 |
| 观测 + 队友摘要 | ~467 | ~467 |
| 输入合计 | **~913** | **~1076** |
| 输出 | ~30 | ~180 |

输出一栏是回复还是 JSON 的时候量的。换成一行一个动作之后，把上一局 deepseek-flash 的 2,025 份回复原样换写，
字符数是原来的 43%（中位数 195 → 82）；DeepSeek 报的输出中位数从那一局的 92 token 降到了行格式第一局的 55
（deepseek-flash 官方直连，`plan_length: 6`，1,008 份回复）。

团战打满 32 行单位表时观测再加约 430。上表是加入物品栏和施法类型之前量的：物品栏每格一行，6 格全满约多 60；
每个技能 / 物品行后面的 `ready: CAST_TARGET CAST_DIRECTION` 之类每行约多 5。

加上 §8 的技能说明、物品说明和地图行之后（实测字符数，中文按各家分词器约 0.6–1 个 token 一个字）：

| | 字符 | 其中中文 | 约合 token |
|---|---|---|---|
| system（示例配置五个英雄） | 2,600–3,200 | 1,300–1,700 | ~1,500–2,100 |
| user：物品说明（开局的树之祭祀 + 两个铁树枝干 + 圆环） | ~280 | ~150 | ~150 |
| user：状态 + 队友 + 上条命令（对线期） | ~1,600 | ~330 | ~650 |

这之后 user 又加了三块：看得见的敌方英雄（每个约 55 字符）、兵线（每波约 50 字符，对线期 6–12 波）、最近
`history_length` 条命令（每条约 45 字符，默认 6 条）。合起来每轮约多 900–1,200 字符，大多是 ASCII，约合 300–400 token
（估算，还没有实测）。

DeepSeek 自己数的（第一局实机，deepseek-flash、`plan_length: 6`，-1:30 到 2:00 共 821 次请求）：输入平均 **2,568**
token（改动前一局 744），输出 105（改动前 81）。所以输入大约是原来的三倍半，但多出来的大头在 system 里，整局不变；
物品说明只在物品栏变化时才变。那 821 次按未命中缓存的价格算是 $0.74，照这个速度跑满 10 分钟约 $2.6（上限估算）；
之后 system 又加了地图方位和坐标表（约多 450 token），跑满 10 分钟的上限估算变成约 $2.9。再之后加了天赋表和
`PICKUP_RUNE` / `TP` / `TALENT` / `COURIER` 的说明与动作行，影魔的 system 多 804 字符（364 个汉字），约合 500 token。
网关有前缀缓存时这两块按缓存命中计价（各家通常是正常输入价的一到两成），下表的金额是加这些之前算的。

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

## 10. 已知限制

- **chat 只能写，不能读**。`CMsgBotWorldState` 里没有聊天字段，所以 agent 喊的话谁也读不到，
  内置 bot 更不会回。跨队互喷要等阶段二（同进程驱动两队，在进程内回传）。服务器 VM 的 `player_chat` 事件
  能读到每条走聊天通道的消息（无头实测，[SERVER_VM.md](SERVER_VM.md) §2），还没接进环境。
- **双队 LLM 还没实现**。两队都写 `control: agent` 会报错。Lua 那边已经支持（per-team 的 `heroes` /
  `control`、`actions_t<team>.lua`、ACK 带 team、两个 worldstate 端口一直都开），堵点是
  `DotaSession` 只管一队，而两个 env 实例不能共存（`pkill`、bots 软链接、固定端口都是全局的）。
  最小的缝是给 `DotaSession` 加一个 `game=` 参数复用同一个 `DotaGame`。
- **`SAY` 走的 `ActionImmediate_Chat` 在服务器端不留痕迹**。无头实测既没有聊天日志行，也不触发 `player_chat`，
  屏幕上显不显示要开窗口看。服务器 VM 的 `Say` 实测能让 bot 以自己的名义进入全体 / 队伍聊天
  （[SERVER_VM.md](SERVER_VM.md) §2），`SAY` 可以改走它。
- **REASON 头顶文字换了画法，还没开窗口看过**。第一版每 tick 用 `DebugDrawText` 重画，窗口里中文能显示但一直在抖；
  现在改成英雄的血条标签，由客户端自己画。字号、长句是不是一行要 `render: true` 再看一次（[SERVER_VM.md](SERVER_VM.md) §5）。
- **花费计量靠流里的 usage**。请求是流式的，token 数只能从最后一个数据块里读（§1）；在流里不报 usage 的网关，
  token 和花费记成 0，`spend_limit_usd` 对它不起作用。DeepSeek、OpenRouter、Kimi 都报，通义、vLLM、Ollama
  认 `stream_options.include_usage`。换一家新网关先跑一小段，看 `check_match.py` 的汇总里 token 是不是 0。
- 本文的 token 数基于 3 字符/token 的估计，没有用真实分词器量过。
