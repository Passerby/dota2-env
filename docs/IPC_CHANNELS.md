# 通信通道：现在这四条，以及 Lua HTTP 到底行不行

实测环境：macOS（arm64），Dota 2 `steam.inf` ClientVersion **6934 / VersionDate 2026-09-18**，
`-dedicated`，`host_timescale 2`。脚本：[`scripts/probe_http.py`](../scripts/probe_http.py)、
[`examples/latency_check.py`](../examples/latency_check.py)。
✅ = 本机实测，⚠️ = 静态分析或他人报告、本机未复现。

## 结论

**保持现状。** 观测走 worldstate socket，动作走 `bots/actions_t<team>.lua`，回执走 console.log；
2026-09 起服务器 VM 也读同一份动作文件（见第 2.1 节），矢量施法就是这么发出去的。
Lua 里的 HTTP 是真实存在、当前版本也确实能用的（见下），但拿它当动作通道，每一项指标都更差：

| | 现在（文件 + socket + 日志） | 换成 Lua HTTP | |
|---|---|---|---|
| 动作落地延迟（timescale 2） | **0.100 游戏秒**，稳定（120/120，0 丢） ✅ | 0.14–0.27 游戏秒（4–8 个 Think tick），抖动 2× ✅ | 文件胜 |
| 锁步（让游戏等策略） | 可以：Lua 阻塞 → 全局冻结，解除后无损恢复 ✅（见 VERSION_DIFF） | **不可能**：回调只在 Lua 让出控制权后才投递 ✅ | 文件胜 |
| 运行依赖 | 文件系统 | Steam 的 HTTP 栈（UA `Valve/Steam HTTP Client 1.0 (570)`）✅；Steam 关掉就返回 nil ⚠️ | 文件胜 |
| 观测 | worldstate protobuf 全量帧，Lua 零开销 | 要在 Lua 里手搓序列化（d2ai 的做法：每帧 ~130 ms，CPU 很高）⚠️ | socket 胜 |
| 方向 | Python 主动写，Lua 每 tick 取 | 只能 Lua 发起、Python 应答 | 文件胜 |
| 出错模式 | 文件写坏 / 日志行截断（都已处理） | StatusCode=0 静默失败，没有日志 | 平 |

HTTP 唯一比现状干净的地方是 **Lua → Python 的结构化上报**（现在的 `LUARDY` / `ACK` 靠 `print` + 解析
console.log）。省掉的是日志解析，换来的是多一个 Steam 依赖和一个常驻 HTTP server —— 现在日志够用，不换。

## 1. HTTP 在 bot VM 里的实测结果（2026-09 客户端）

| 项目 | 结果 |
|---|---|
| `CreateHTTPRequest` | ✅ 存在（`type` 为 `function`），**只有 `":<port>/<path>"` 这一种写法能用** |
| `CreateRemoteHTTPRequest` | ✅ 存在，但**对任何 URL 都返回 nil**（试过 `http://127.0.0.1:P/`、`http://localhost:P/`、`http://example.com/`、`https://example.com/`，四个全 nil，不抛异常）→ **不能从 bot 脚本直接调外网 API** |
| URL 形式 | `:8121/path` → 200 ✅；`localhost:8121/path`、`http://localhost:8121/path` → 回调里 `StatusCode = 0` ✅。引擎内部拼的是 `http://localhost%s`，参数直接接在 `http://localhost` 后面 |
| 请求对象方法 | `Send(cb)`、`SetHTTPRequestRawPostBody(mime, body)`、`SetHTTPRequestHeaderValue`、`SetHTTPRequestGetOrPostParameter`、`SetHTTPRequestAbsoluteTimeoutMS`、`SetHTTPRequestNetworkActivityTimeout` ⚠️（binary 里的注册表）；本次实测了 `Send` 和 `SetHTTPRequestRawPostBody`（服务端确实收到 body 和 content-type）✅ |
| 响应对象 | `{StatusCode = number, Body = string, Request = table}` ✅ |
| 走哪条栈 | Steam：服务端看到 UA `Valve/Steam HTTP Client 1.0 (570)`，binary 里对应 `STEAMHTTP_INTERFACE_VERSION003` ✅ |
| 载荷 | 上下行各测到 1 MB 都完整通过（0 / 1K / 64K / 256K / 1M，收发字节一致，没试更大），**大小几乎不影响 RTT**（1 MB 也是 ~70 ms）✅ |
| 空载往返（30 次） | wall **67 / 71 / 137 ms**（min/median/max），**4 / 5 / 8 个 Think tick** ✅（两次独立运行：67/71/137 与 67/72/128，稳定） |
| 阻塞时能否收到回调 | **不能** ✅：Lua 自旋 1745 ms 期间 `callback_ran_during_spin = false`，自旋一结束立刻投递（RTT 1746 ms）|
| bot VM 沙箱 | `_VERSION = "Lua 5.1"`；`io`、`os`、`jit` 都是 **nil**；`require` / `loadfile` / `dofile` / `package` 可用；`DebugPause` 存在 ✅ |

注册位置（静态）：`libserver.dylib` 的 bot VM 函数表里是 `Script_CreateLocalHostHTTPRequestBotVM` /
`Script_CreateRemoteHTTPRequestBotVM` 两个专用入口，和 `GetAllTrees` / `DebugDrawLine` / `InstallDamageCallback`
这些 bot 专有函数排在同一张注册表里 —— 也就是说 HTTP 确实是 bot 脚本 API 的一部分，不是 addon VM 的溢出
（addon VM 那张表里对应的是另一个入口 `CreateHTTPRequestScriptVM`）。

复现：

```bash
.venv/bin/python scripts/probe_http.py --port 8121 --timescale 2
```

## 2. 对照组：现在的文件通道

```bash
.venv/bin/python examples/latency_check.py --think 0.0 --timescale 2 --steps 120
# action_delivery: sent 120, executed 120, lost 0, delay_median = delay_max = 0.100 游戏秒
```

0.100 游戏秒 = 3 个 tick，正好是「Lua 端 0.07 秒下限 + 下一次 Think」。没有抖动，没有丢包。
HTTP 的 4–8 tick 是**在这之上**的额外往返，而且 Python 那边还什么都没算。

还有一点：文件通道的延迟是按 **tick** 算的，HTTP 的往返是按**墙钟**算的（~70 ms 花在 Steam 的 HTTP 栈上，
和载荷大小无关）。所以 timescale 开得越高，HTTP 折算成游戏时间越贵 —— 4 倍速下 71 ms 就是 0.28 游戏秒，
而文件通道仍然是那几个 tick。（两边都只在 timescale 2 下实测过。）

## 2.1 服务器 VM：同一份动作文件的第二个读者

`bridge/lua/server_actions.lua` 由 `<dota>/game/dota/cfg/dota2_env_server.cfg`（启动参数 `+servercfgfile`，服务器激活时
exec）加载进服务器 VM，用 `loadfile('bots/actions_t<team>')` 每 tick 读动作文件，按 `dotaTime` 去重，和 bot 一样等
文件满 0.07 秒再执行。服务器 VM 有 `ExecuteOrderFromTable`，能发 bot API 没有的指令（矢量施法的
`VECTOR_TARGET_POSITION`）；实测和 bot 的 `Action_*` 互不干扰（[VERSION_DIFF.md](VERSION_DIFF.md) 3.1）。
它没有 `io` / `os`，回执仍然只能靠 `print` 进 console.log。启动行上的 `+script_reload_code` 在地图加载前执行、
静默丢掉，cfg 是唯一能无界面自动加载服务器 Lua 的办法。有了它，控制台命令也能自动跑：cfg 里的行在服务器激活时
执行，脚本里 `SendToServerConsole(...)` 随时执行（GUI 模式的 `jointeam spec` 就是这么发的）；`SendToConsole`
（客户端控制台）对脚本源有 FCVAR 限制，`jointeam` 这类命令过不了（[VERSION_DIFF.md](VERSION_DIFF.md) 1.3）。

## 3. 其它候选通道

| 通道 | 结论 |
|---|---|
| `-netconport`（TCP 控制台） | ✅ 不存在：当前 `libserver.dylib` 里搜不到 `netcon` 字符串，那是 Source 1 的东西 |
| GSI（Game State Integration） | 只有记分板级别的粗粒度状态，由客户端推给 HTTP endpoint，做不了单位级观测 |
| `-dedicated` 下用 stdin 发控制台命令 | ✅ 不行：进程不读 stdin（见 `DotaGame.stop_dota` 的说明）。要往服务器 VM 送东西走 2.1 节的 cfg + 文件 |
| `dota_bot_practice_script` | "Bot script ID to use for local games"，走创意工坊 UGC ID，帮不了本地多实例 |
| worldstate socket | 就是现在用的，Valve 为机器学习开的官方口子，无可替代 |

## 4. 真正值得做的改进（都和 HTTP 无关）

1. **锁步模式**（对 LLM agent 收益最大）：现在 `step()` 不等策略，策略越慢英雄按旧指令走得越久。
   Lua 端改成「没有新动作就阻塞轮询 `loadfile`」即可让整局游戏等 Python —— VERSION_DIFF 已实测
   阻塞时游戏冻结、`-nowatchdog` 下 90 秒仍能无损恢复。代价是自旋烧一个核（bot VM 没有 `os`，睡不了）。
   **这条路 HTTP 走不通**（回调进不来），是保留文件通道最硬的理由。

   顺手试过的替代品：bot VM 里的 `DebugPause()`（**不带参数**，传参数会报
   `DebugPause called with 1 arguments - expected 0`）确实能暂停
   （日志 `CDOTAGameRules:Pause = true PlayerId=-1 fUnpauseDelay=3.00`），但**暂停后 Think() 就不再被调用**，
   Lua 自己解不了暂停 ✅ —— 单向陷阱，不能用来做锁步。能不能从 `-dedicated` 的 stdin 发控制台命令解暂停，未验证 ⚠️。
2. 多实例并行：瓶颈是全局的 `vscripts/bots` 软链接、固定端口和 `pkill dota2`，和用哪种通道无关。

## 5. 参考

- [ModDota: Lua (Bots) API](https://docs.moddota.com/lua_bots/) —— 函数表里列着 `CreateHTTPRequest( cstring ) : handle`「Create a localhost HTTP request.」
- [2aius/d2ai](https://github.com/2aius/d2ai) —— 完全基于 `CreateHTTPRequest` 的 C++ agent 框架，Lua 侧
  `req:SetHTTPRequestRawPostBody("d2ai", msg)` + `req:Send(function(res) ... end)`，配置里 server 就写成 `":4101"`；
  自述每个英雄约 130 ms 发一次观测，大头就是 `CreateHTTPRequest` 调用（他们测到 50–400 ms）。
- [TimZaman/dotaservice NOTES.md](https://github.com/TimZaman/dotaservice/blob/master/NOTES.md) ——
  "The `CreateRemoteHTTPRequest` from the LUA api does not work when steam is off"，结论是建议别用。
