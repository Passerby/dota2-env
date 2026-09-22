# 地图与游戏文本数据：来源、刷新与实测结论

实测环境：macOS（arm64），Dota 2 `steam.inf` ClientVersion **6934 / VersionDate 2026-09-18**，
补丁 **7.41f**（2026-09-15）。脚本：[`scripts/extract_map.py`](../scripts/extract_map.py) +
[`scripts/map_scan.lua`](../scripts/map_scan.lua)、[`scripts/fetch_game_text.py`](../scripts/fetch_game_text.py)、
[`scripts/probe_trees.py`](../scripts/probe_trees.py)。✅ = 本机实测，⚠️ = 未验证或他人报告。

仓库里的四个数据文件都是脚本生成的，**不要手改**，换客户端版本就重跑：

| 文件 | 内容 | 生成脚本 |
|---|---|---|
| `dota2_env/data/map.json` | gridnav、高度级、树（带 tree_id）、神符点、地标、建筑、商店、野怪营地、Watcher、兵线、世界边界 | `extract_map.py` |
| `dota2_env/data/items.json` | 物品：价格、配方、冷却、蓝耗、数值，中英文名称 / 说明 / 备注 | `fetch_game_text.py` |
| `dota2_env/data/abilities.json` | 技能与天赋：冷却、蓝耗、施法距离、数值，中英文名称 / 说明 / 备注 / 碎片 / 神杖 | `fetch_game_text.py` |
| `dota2_env/data/heroes.json` | 英雄：技能列表、天赋列表，中英文名 | `fetch_game_text.py` |

`map.json` 由观测的地图部分（`dota2_env/map_features.py`，见 [FEATURES.md](FEATURES.md)）读取；
文本三件套目前只是快照，包里还没有代码读它们。

## 1. 每个字段从哪来

旧仓库的 `data/*7.23*`、`*7.24b*`（devilesk 交互地图格式）都早于 7.33 扩图（±8288 → ±10240），
7.41 又挪了魔方、莲花池、双生门、一塔和野怪点，全部作废。现在三路数据互相核对：

| `map.json` 字段 | 来源 | 交叉校验（6934 结果） |
|---|---|---|
| `gridnav` | 本机 `game/dota/maps/dota.vpk` 里的 `maps/dota.gnv`，标准库直接解 | 与 `IsLocationPassable` 在远离树的格子上 94.7% 一致 ✅ |
| `height_level` | bot API `GetHeightLevel`，逐格中心扫描 | 建筑脚下 `ground_height // 128` 与级数一一对应 ✅ |
| `trees` | bot API `GetTreeLocation(0..)` | 与实体表 2475 个 `ent_dota_tree` 坐标集合完全相同；`GetAllTrees` 也是 2475 ✅ |
| `runes` | 实体表 `dota_item_rune_spawner_{powerup,bounty}` | `GetRuneSpawnLocation` 与 world state `rune_infos` 误差 < 2 ✅ |
| `landmarks` | 实体表：`roshan_location_1/2`、`miniboss_location_1/2`（`info_player_start_dota`）、`npc_dota_xp_fountain`、`npc_dota_lotus_pool`、`npc_dota_watch_tower` | 神龛 / 莲花池 / 前哨与 world state 建筑误差 < 2 ✅；肉山坑、魔方只有实体表 |
| `buildings` | 扫描那局的第一帧 world state（塔 / 兵营 / 基地 / 其余建筑，含前哨、双生门、神龛、莲花池） | 塔与实体表、`GetTower` 误差 < 2 ✅ |
| `shops` | `GetShopLocation` | 边路商店返回 (0,0,0)，已剔除 |
| `neutral_camps` | 实体表 `npc_dota_neutral_spawner` + 同名 `trigger_multiple` 的物理包围盒（VRF）；`type` / `speed` / `team` 来自 `GetNeutralSpawners` | 包围盒与 `GetNeutralSpawners` 的 `min`/`max` 误差 < 1 ✅ |
| `watchers` | 实体表 `npc_dota_lantern` | — |
| `lanes` | `GetLocationAlongLane(lane, k/20)`，每路 21 个点 | 0.1–0.9 段都落在可走格上 ✅ |
| `world_bounds` | `GetWorldBounds` | 等于 gridnav 首末格的中心 ✅ |

实体表是 `maps/dota/entities/default_ents.vents_c`（外加两个 `world_layer_*_base`，里面各有 103 / 66 棵
基地树），二进制 KV3 v5，Python 没有现成解析器，所以用
[ValveResourceFormat](https://github.com/ValveResourceFormat/ValveResourceFormat) 的命令行
`Source2Viewer-CLI`（MIT）解出文本再解析。它只在提取时用，不随仓库分发。

中英文文本来自 dota2.com 官网自己用的 datafeed（`/datafeed/{itemlist,itemdata,abilitylist,abilitydata,herolist,herodata,patchnoteslist}?language=english|schinese`）。
这是**非公开接口**，没有文档、可能会变；如果哪天不能用了，本机 `pak01_dir.vpk` 里的
`scripts/npc/*.txt` 和 `resource/localization/*_{english,schinese}.txt`（dotabuff/d2vpkr 按客户端版本同步）
是离线替代，但要自己解析 KV 文本和占位符。

## 2. 打补丁后怎么刷新

```bash
# 一次性：下载 VRF（macOS arm64 版 54 MB；其他平台见它的 release 页）
mkdir -p ~/.cache/dota2_env/vrf-20.0 && cd ~/.cache/dota2_env/vrf-20.0
curl -sSL -o cli.zip https://github.com/ValveResourceFormat/ValveResourceFormat/releases/download/20.0/cli-macos-arm64.zip && unzip -o cli.zip
```

```bash
# 地图：需要 Steam 在运行，会开一局无头 1v1，约 2 分钟
.venv/bin/python -u scripts/extract_map.py --vrf ~/.cache/dota2_env/vrf-20.0/Source2Viewer-CLI --patch 7.41f
```

```bash
# 中英文文本：约 3,400 次请求，冷启动约 30 分钟，缓存在 $TMPDIR/dota2_env_datafeed，中断后续跑
.venv/bin/python -u scripts/fetch_game_text.py
```

```bash
# 可选：确认 tree_id 仍然和 world state 的 tree_events 对得上，并看一次树重生（约 3 分钟）
.venv/bin/python -u scripts/probe_trees.py
```

- `extract_map.py` 任何一项交叉校验失败都会以非零状态退出，不覆盖 `map.json`；候选文件和原始扫描
  （`scan.log`、`worldstate.bin`、`globals.json`，即 bot VM 的全部全局名）留在 `--work`。
  改了合并逻辑只想重算，用 `--reuse-scan`，不用再开 Dota。
- 环境构造时 `warn_if_stale` 会比较本机 `steam.inf` 的 ClientVersion 和 `map.json` 的
  `client_version`，不一致就打 warning。**地图补丁会改树的编号**，这时树通道会悄悄错位，必须重跑。

## 3. `map.json` 的格式

每个顶层字段一行、列表每个元素一行，方便看 diff。坐标都是世界坐标（x 向东、y 向北）。

- `grid`：`cell_size` 64，`x0` -10240，`y0` -10752，`width` 320，`height` 328。第 `row` 行覆盖
  y ∈ [y0 + 64·row, y0 + 64·(row+1))，第 0 行在最南；列同理，第 0 列在最西。
- `gridnav`：328 行，每格 2 个十六进制字符，是 `dota.gnv` 的原始 flag：`0x01` 可走、`0x02` 阻挡、
  `0x04` 挡英雄、`0x08` 挡单位、`0x10` 禁眼（`gridnav_flags` 里有同样的表）。6934 上
  可走格 59,578 个，其中 869 个同时禁眼（例如肉山坑）。
- `height_level`：328 行，每格一位数字，就是 `GetHeightLevel` 的返回值（**越大越低**，见 4.4）。
- `trees`：`[tree_id, x, y, z]`，`tree_id` 从 0 连续编号，就是 bot API 的 id，也是 world state
  `tree_events.tree_id`。
- `runes` / `landmarks`：`{名字: [x, y, z]}`。成对的东西按**中路对角线**命名：`y > x`（西北侧）叫
  `_top`，另一个叫 `_bottom`。Valve 自己的命名不按这个来（前哨 (-4096,-448) 的 targetname 是
  `npc_dota_watch_tower_bottom`、world state 名字是 `#DOTA_OutpostName_South`），不采用。
- `buildings`：`{name, type, team, x, y, z}`，`type` 是 world state 的 UnitType（6 塔、7 兵营、9 基地、
  10 其他建筑）；`team` 4 是中立（神龛、莲花池、双生门）。
- `shops`：`{shop, team, location}`，`SHOP_HOME` 每队一个，两个秘密商店只记一次。
- `neutral_camps`：`{name, team, type, speed, spawner, box}`；`type` 为 small / medium / large / ancient，
  `speed` 为 fast / normal / slow，有 7 个营地 bot API 不给 speed（记为 null）；`box` 是拉野判定框
  `[x_min, y_min, x_max, y_max]`。
- `lanes`：`top` / `mid` / `bot` 各 21 个 `[x, y]`，从天辉泉水到夜魇泉水。
- `source`：客户端版本、各部分来源、VRF 版本号、扫描时的游戏时间。

7.41f 的地标（6934 实测）：

| 名字 | 位置 | 说明 |
|---|---|---|
| `power_top` / `power_bottom` | (-1640, 1112) / (1180, -1216) | 强化神符；2:00、4:00 的水神符也刷在这两处 |
| `bounty_top` / `bounty_bottom` | (-996, 4431) / (595, -4660) | 赏金神符（7.29 起只剩 2 个点） |
| `roshan_top` / `roshan_bottom` | (-3194, 2395) / (2860, -2765) | 两个肉山坑；实体表只有一个 `npc_dota_roshan_spawner`（在下坑） |
| `tormentor_top` / `tormentor_bottom` | (-7680, 6336) / (7744, -6208) | 魔方的两个刷新点 |
| `wisdom_top` / `wisdom_bottom` | (-8088, 768) / (8167, -1142) | 智慧神龛 `npc_dota_xp_fountain`，7.38 起取代经验神符 |
| `lotus_top` / `lotus_bottom` | (-7548, 4209) / (7504, -4405) | 莲花池 |
| `outpost_top` / `outpost_bottom` | (-4096, -448) / (3392, -448) | 前哨；开局分别归天辉 / 夜魇 |

治疗圣坛（`npc_dota_healer`）7.24 起就没有了，实体表里一个都没有；bot API 的 `GetShrine`、`SHRINE_*`
常量还在，但已无对象可取。

## 4. 实测结论（6934）

### 4.1 gridnav 与树（D1 / D2）

- **树不在 gridnav 里** ✅：9,886 个树格中 9,112 个是普通可走（0x01）。所以 `blocked` 只看 gridnav 的
  可走位，树单独走树通道，砍掉的树格自然变回可走。
- **树占 2×2 格** ✅：2,475 棵树的原点全部落在 64 的整数倍上，也就是格角，一棵树盖住交于此角的四格
  （128×128）。只有 14 棵树和别的树共用格子。
- **格子对齐** ✅：`GetWorldBounds` 返回 [-10208, -10720, 10208, 10208]，正好是从 -10240 / -10752 起算的
  首末格中心；把 gridnav 整体平移一格，与 `IsLocationPassable` 的一致率变化不到 0.01。
- `IsLocationPassable` 像是把 gridnav 的障碍向外扩了 1–2 格（大概考虑了单位碰撞体积），树附近尤其明显，
  所以观测不用它，只用它做一致性检查（远离树和地图边缘时 94.7%）。

### 4.2 野怪营地（D3）

营地判定框有两个来源，结果一致 ✅：VRF 解出的 `neutralcamp_*` trigger 模型物理包围盒
（`m_vMinBounds` / `m_vMaxBounds` 加 trigger 原点，trigger 全部没有旋转），以及 `GetNeutralSpawners`
每项的 `min` / `max`。后者还给 `team`、`type`、`speed`、`location`。

### 4.3 bot API 的现状

- bot VM 里有 529 个全局名，其中 153 个函数（完整列表在 `--work/globals.json`）。
- 过时的：`RUNE_BOUNTY_3/4` 和 `SHOP_SIDE` / `SHOP_SIDE2` 返回 (0,0,0)。
- 神符类型常量有新的 `RUNE_WATER` = 7、`RUNE_XP` = 8、`RUNE_SHIELD` = 9；`RUNE_STATUS_AVAILABLE` = 1，
  与 `map_features.RUNE_STATUS_AVAILABLE` 一致（脚本会校验）。
- `TORMENTOR_LOCATION_*`、`WISDOM_SHRINE`、`LOTUS_POOL` 在 `libserver.dylib` 里有字符串，但 bot VM 的
  `_G` 里**没有**；也没有 `GetGroundHeight(vLoc)`。所以肉山坑、魔方只能靠实体表。
- 这些函数在开局前（dota_time ≈ -90）就能用。

### 4.4 高度级（D4）

`GetHeightLevel` **越大越低** ✅：建筑脚下的地形台阶（`ground_height // 128`）0 → 级 4（河道），
1 → 3（兵线），2 → 2（高地），3 → 1（泉水台）；更高的树坡也是 1；级 5 只出现在河道以下的不可走格。
分辨率其实是 128（相邻两行 / 两列的值成对相同）。可走格上只有 1–4，没有取不到值的格子。
`load_map` 把级数取负，观测里的 `height` 通道因此是“比英雄高几级”，正数 = 更高的地面。

### 4.5 tree_id 与 tree_events（`probe_trees.py`）

- 英雄用树之祭祀吃掉 `map.json` 里的 46 号树 (-6144, -6656)：world state 的 destroyed 事件带的就是
  id 46、位置 (-6144, -6656)，局部地图的树通道随之清零 ✅。**扫描得到的 tree_id 就是 tree_events 的 id。**
- **事件跟着视野走** ✅：英雄吃完树走回泉水（树的位置被周围的树挡住视线），900 游戏秒内都没有重生事件；
  等英雄 245 秒后再走过去，先收到 `respawned=True, delayed=True`，紧接着第二口树之祭祀又收到 destroyed。
  也就是说树在迷雾里长回来（Liquipedia：3 分钟，150 范围内站着单位就不长），本队要到再次看到那个位置
  才收到事件，`delayed` 标的就是这种“补报”。所以树表是**本队知道的**树，不是真实的树，这对观测正合适。
- 一棵树旁边一直站着单位（包括自己的英雄）时它不会重生，树表也就一直显示没有树，和游戏一致。
- 临时树：种下的树枝确实用掉了（背包里没了），但**没有任何树事件** ✅；发芽（Sprout）没测。

## 5. 树表与丢帧

world state 的 `tree_events` 只在树变化时出现，是增量，而且只报本队看到的变化（迷雾里的变化等再次看到时
带 `delayed` 补报，见 4.5），所以每局维护一张树表（`map_features.TreeTable`）：开局全部站着，按 destroyed /
respawned 事件改；同一事件重复出现不会重复计数；id 超出 `map.json` 的（临时树）忽略。

增量最怕丢帧，而 bridge 有两处会丢：`worldstate_listener` 在队列满或帧不可行动时跳过，
`DotaSession.observe()` 落后时直接跳到最新一帧。现在两处都会把被跳过那几帧的 `tree_events` 按顺序
接到下一帧前面（protobuf 解析拼接的消息时，repeated 字段会按顺序合并），`observe()` 的接口不变。
两个环境在 `reset()` 的等待循环里、`step()` 的每次 `observe()` 之后都会把帧喂给树表。

## 6. 文本数据的格式

- `items.json`：`{id, name, cost, quality, neutral_tier, recipe, recipes, initial_charges, stock_max,
  cooldowns, mana_costs, cast_ranges, values, en, zh}`。`recipes` 是配方组件的 item id 列表，
  `neutral_tier` 非中立物品为 null。
- `abilities.json`：非天赋技能 `{id, name, talent: false, innate, max_level, cooldowns, mana_costs,
  cast_ranges, values, en, zh}`；天赋 `{id, name, talent: true, values, en, zh}`，通用天赋（如 +20 攻击力）
  多个英雄共用，只记一次。
- `heroes.json`：`{id, name, primary_attr, attack_capability, abilities, talents, en, zh}`。
- `en` / `zh`：`name`、`desc`、`notes`、`shard`、`scepter`、`stats`（带标题的数值行，如
  “DAMAGE: 90 / 160 / 230 / 300”），空字段省略。
- 说明文字里的占位符都已换成数值（多级写成 `60 / 80 / 100`，`%%` 还原成 `%`），HTML 标签去掉、`<br>`
  变换行。替换规则（6934 / 7.41f 实测）：
  - `%name%`、`{s:name}` 对应同名 special value，名字不分大小写（记录里是 `AbilityCooldown`，文字里写
    `%abilitycooldown%`）；记录里没有的 `%abilitycastrange%` / `%abilityduration%` 这类取记录自己的施法距离、
    持续时间等字段。
  - 碎片 / 神杖文字里的 `%bonus_X%` 是 X 的碎片 / 神杖值；只有碎片 / 神杖才有的数值平时是 0，文字里指的是升级后的值。
  - 天赋的数值不在天赋自己的记录里，而在它强化的那个技能的 special value 的 `bonuses` 里，写作
    `{s:bonus_<special value 名>}`，脚本按英雄收集后再替换。
  - 残留：物品 8 处、技能 57 处，都是 datafeed 记录里根本没有那个数值（如上古巨神的 `tick_rate`、知识之书的
    实时计数）。脚本每次都会打印残留数量。
- datafeed 自己的缺陷：潮汐猎人的**英文** herodata 固定返回 `null`（中文正常），所以 herodata 每个英雄只取一种语言
  （英文不行就用中文，结构和数值与语言无关），英雄名和天赋名改从 `herolist` / `abilitylist` 按语言取。

## 7. 许可与引用

- `map.json` 和文本三件套的内容是 Valve 的游戏数据，不适用本仓库的 MIT 许可。
- VRF（MIT）只在提取时运行，仓库里没有它的代码或二进制。
- [leamare/dota-interactive-map](https://github.com/leamare/dota-interactive-map)（ISC，7.41 数据）只用来人工
  对照（树 2,475、各类实体数量和坐标都一致），没有拷贝任何数据。
- dota2.com datafeed 是非公开接口，请求间隔默认 0.25 秒并缓存，不要调得更快。

## 8. 已知缺口

- 临时树（发芽、种树枝）没有进树表：种树枝不产生任何事件，发芽没测。⚠️
- 前哨被占领后，world state 里它的 `team_id` 会不会变，还没在长局里看到。⚠️
- 肉山当前在哪个坑（7.41 起开局在上坑，15:00 后昼夜交替时换坑）、魔方当前在哪一侧，观测里都没有。
- 高度只有级数，没有连续高度（bot VM 没有 `GetGroundHeight(vLoc)`）。
