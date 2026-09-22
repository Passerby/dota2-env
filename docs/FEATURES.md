# 观测特征 / 动作 / 奖励

两个环境共用同一套特征定义，5v5 只是把单英雄的那份按英雄堆叠，再加一条全局向量：

| | `dota2_env/Mid1v1-v0` | `dota2_env/AllPick5v5-v0` |
|---|---|---|
| 控制 | 1 个英雄 | 本方 5 个英雄全部 |
| 观测 | `observation_space` | `team_observation_space` |
| 动作 | `action_space` | `team_action_space` |
| 奖励 | `LaningReward` | `TeamReward` |
| 终止 | `Mid1v1Rules` | `AllPick5v5Rules` |
| 游戏模式 | 21（1v1 中路） | 1（全阵营选择，`-fill_with_bots`） |

特征顺序的**唯一来源**是 `dota2_env/observation.py` 里的 `HERO_FEATURES` / `ABILITY_FEATURES` /
`UNIT_FEATURES` / `TEAM_FEATURES` 四个元组，地图部分（1.5-1.7）则是 `dota2_env/map_features.py` 里的
`MAP_FEATURES` / `RUNE_SPOTS` / `RUNE_FEATURES` / `LANDMARKS` / `LANDMARK_FEATURES`。下面的表按这些元组的顺序列出，
改元组就要改这份文档。
全部是 `float32`，已做粗归一化（除以一个量级常数，不裁剪，所以偶尔会 >1）。

```python
from dota2_env.observation import HERO_FEATURES

H = {name: i for i, name in enumerate(HERO_FEATURES)}
hp = observation['hero'][H['health_frac']]  # 1v1
hp = observation['heroes'][row][H['health_frac']]  # 5v5，row 是英雄序号
```

## 1. 观测

### 1v1：`observation_space`

| key | shape | 内容 |
|---|---|---|
| `hero` | (25,) | 本方英雄，见 1.1 |
| `abilities` | (6, 3) | 技能槽 0-5，见 1.2 |
| `items` | (6, 4) | 背包格 0-5，见 1.2 |
| `units` | (32, 16) | 1600 范围内最近的 32 个单位，见 1.3 |
| `unit_mask` | (32,) | `units` 哪些行有效 |
| `local_map` | (3, 33, 33) | 以英雄为中心的局部地图，见 1.5 |
| `runes` | (4, 4) | 4 个神符点，见 1.6 |
| `landmarks` | (10, 4) | 10 个地标，见 1.7 |

### 5v5：`team_observation_space`

| key | shape | 内容 |
|---|---|---|
| `heroes` | (5, 25) | 每行一个受控英雄，字段同 `hero` |
| `abilities` | (5, 6, 3) | |
| `items` | (5, 6, 4) | |
| `units` | (5, 32, 16) | **每个英雄各有一张自己的邻近单位表** |
| `unit_mask` | (5, 32) | |
| `local_map` | (5, 3, 33, 33) | 每个英雄各以自己为中心 |
| `runes` | (5, 4, 4) | 每个英雄各自的相对位置 |
| `landmarks` | (5, 10, 4) | |
| `team` | (11,) | 全局战况，见 1.4 |

行号 `i` 对应 `info["player_ids"][i]`，也就是本方 player id 升序的第 i 个；`hero_selection.lua` 按同样的顺序
发英雄，所以 `gym.make(heroes=(...))` 的第 i 个英雄名就是第 i 行。行号在一局内固定不变。

### 1.1 `hero`（25 维）

| i | 名称 | 取值 | 来源 |
|---|---|---|---|
| 0 | `x` | `location.x / 8192` | 地图坐标，实测范围 x∈[-8088, 8167] |
| 1 | `y` | `location.y / 8192` | |
| 2 | `facing_sin` | `sin(facing)` | `facing` 是角度，先转弧度 |
| 3 | `facing_cos` | `cos(facing)` | |
| 4 | `is_alive` | 0/1 | |
| 5 | `health_frac` | `health / health_max` | |
| 6 | `health` | `health / 1000` | 绝对值，和 `health_frac` 一起给出体型信息 |
| 7 | `health_regen` | `health_regen / 10` | |
| 8 | `mana_frac` | `mana / mana_max` | |
| 9 | `mana` | `mana / 1000` | |
| 10 | `level` | `level / 30` | |
| 11 | `attack_damage` | `attack_damage / 100` | 已含装备加成 |
| 12 | `attack_range` | `attack_range / 1000` | |
| 13 | `attacks_per_second` | 原值 | 约 0.5-1.5 |
| 14 | `movement_speed` | `current_movement_speed / 500` | |
| 15 | `armor` | `armor / 10` | |
| 16 | `gold` | `(reliable_gold + unreliable_gold) / 1000` | ⚠️ 新版 1v1 里死亡后会异常暴涨，见 VERSION_DIFF |
| 17 | `last_hits` | `last_hits / 100` | |
| 18 | `denies` | `denies / 100` | |
| 19 | `ability_points` | 原值（整数） | >0 说明可以加点 |
| 20 | `is_stunned` | 0/1 | |
| 21 | `is_silenced` | 0/1 | |
| 22 | `is_attacking` | 0/1 | `attack_target_handle` 存在且 ≠ `0xFFFFFFFF` |
| 23 | `dota_time` | `dota_time / 600` | 开局 -90s，0 是第一波兵 |
| 24 | `time_of_day` | 原值 0-1 | |

**英雄未被上报时**（新版客户端复活后站泉水不动就不出现在 worldstate 里，见 VERSION_DIFF）：
只填 `x` / `y`（用出生点坐标）、`is_alive=1`、`dota_time`，其余全 0，动作掩码只放开 `MOVE`。
这时发 `MOVE` 就按给的方向走；发别的（包括 `NOOP`）环境会替它朝地图中心走一步，因为英雄不动就永远不会重新上报。
实测下一帧就重新上报，之后站着不动也不会再消失。
**英雄死亡时**：`is_alive=0`，动作掩码只剩 `NOOP`；客户端连这个英雄都不上报时整条向量为 0。

### 1.2 `abilities`（6×3）和 `items`（6×4）

`abilities` 按技能槽 0-5，一个槽一行（槽位末尾那几个 slot=0 的通用技能会被过滤掉）。

| i | 名称 | 取值 |
|---|---|---|
| 0 | `level` | `level / 4` |
| 1 | `cooldown` | `min(cooldown_remaining, 100) / 10` |
| 2 | `castable` | `is_fully_castable` 0/1 |

槽位到技能名的映射是版本相关的（影魔在新版就换过），运行时用
`env.unwrapped.ability_names()` 从客户端读，不要写死。

`items` 按背包格 0-5，一格一行，空格子整行为 0（背包格 6-8、储藏处、TP 格 15、中立物品格 16 不收，那里的东西用不了或要指向地点）。

| i | 名称 | 取值 |
|---|---|---|
| 0 | `item_id` | 原值（整数），0 = 空格子 |
| 1 | `charges` | `charges / 10` |
| 2 | `cooldown` | `min(cooldown_remaining, 100) / 10` |
| 3 | `castable` | `is_fully_castable` 0/1 |

worldstate 里物品只有 id。技能和物品的名字、能怎么施放由 Lua 在变化时打印 `SLOTS` 行上报（见 2.6），
运行时用 `env.unwrapped.cast_slots()` 读：1v1 是 `{编号: (名字, 施法类型)}`，编号 0-5 是技能、6-11 是背包格，
5v5 前面多一层 `player_id`。

### 1.3 `units`（32×16）

收录**活着**且类型属于 {英雄, 小兵英雄, 近战/远程兵, 野怪, 防御塔} 的单位，距离本方英雄 ≤ 1600，
按距离升序取最近 32 个。行号 `i` 就是动作里的 `target = i`，也是 `TextWrapper` 文本里的 `[i]`。
**行号每步都会变**（重排序），所以动作必须基于当步观测。

1v1 里只有两个"真"英雄会进表（其余 8 个位置是 `-fill_with_bots` 塞的挂机小精灵）；5v5 里 10 个都进。

| i | 名称 | 取值 |
|---|---|---|
| 0 | `rel_x` | `(unit.x - hero.x) / 1600` |
| 1 | `rel_y` | `(unit.y - hero.y) / 1600` |
| 2 | `distance` | `d / 1600` |
| 3 | `in_attack_range` | `d <= hero.attack_range + hero.bounding_radius + unit.bounding_radius` |
| 4 | `is_enemy` | `unit.team_id != team_id` |
| 5 | `is_hero` | 0/1 |
| 6 | `is_lane_creep` | 0/1 |
| 7 | `is_tower` | 0/1（野怪和小兵英雄在 `is_hero` / `is_lane_creep` / `is_tower` 三位上全是 0） |
| 8 | `health_frac` | `health / health_max` |
| 9 | `health` | `health / 1000` |
| 10 | `attack_damage` | `attack_damage / 100` |
| 11 | `attack_range` | `attack_range / 1000` |
| 12 | `hits_to_kill` | `min(unit.health / hero.attack_damage, 20) / 20`，**不算护甲减免**，补刀判断用 |
| 13 | `is_attacking_me` | `unit.attack_target_handle == hero.handle` |
| 14 | `facing_sin` | |
| 15 | `facing_cos` | |

### 1.4 `team`（11，5v5 专用）

单英雄视角看不到的全局量。数据都取自本方视角的 worldstate：建筑双方都可见，敌方英雄只在可见时才计入。

| i | 名称 | 取值 |
|---|---|---|
| 0 | `dota_time` | `dota_time / 600`，和 `hero` 里那一位同一个刻度；5v5 一局能打到 40 分钟，所以这一位常态 >1 |
| 1 | `time_of_day` | 0-1 |
| 2 | `heroes_alive` | 本方存活玩家数 / 5 |
| 3 | `enemy_heroes_visible` | worldstate 里能看到的存活敌方英雄数 / 5（**看不到 ≠ 死了**） |
| 4 | `gold` | 本方 5 人手上金钱之和 / 10000 |
| 5 | `towers` | 本方还站着的塔 / 11 |
| 6 | `enemy_towers` | 敌方 / 11（每方固定 11 座：3 路各 3 座 + 基地 2 座，实测确认） |
| 7 | `ancient_health_frac` | 本方基地血量比例，没了就是 0 |
| 8 | `enemy_ancient_health_frac` | |
| 9 | `kills` | 本方总击杀 / 50 |
| 10 | `deaths` | 本方总死亡 / 50 |

### 1.5 `local_map`（3×33×33）

以英雄脚下那个 64 单位的格子为中心、向四周各 16 格（约 ±1050）的局部地图，数据来自
`dota2_env/data/map.json`（按客户端版本导出，来源和实测结论见 [MAP_DATA.md](MAP_DATA.md)）。
`local_map[c, row, col]`：`row` 越大越靠北（+y），`col` 越大越靠东（+x），`[:, 16, 16]` 是英雄脚下那一格，
和 MOVE 的方向约定一致（0 = 东，4 = 北）。

| c | 名称 | 取值 |
|---|---|---|
| 0 | `blocked` | 1 = gridnav 标记为不可走（悬崖、地图边缘、莲花池这类建筑脚下），地图外补 1；**不含树** |
| 1 | `tree` | 1 = 这一格上有还站着的树（一棵树占 2×2 格），随 `tree_events` 更新 |
| 2 | `height` | 这一格比英雄脚下高几级（一级 = 128 单位的地形高度），正数是更高的地面；地图外取边缘值 |

树：worldstate 的 `tree_events` 只在树被砍 / 重生时出现，所以环境每局维护一张树表（`env.unwrapped.trees`，
`standing[tree_id]`）。`reset()` 的等待循环和每次 `step()` 拿到的帧都会喂给它，bridge 跳帧时也会把被跳过那几帧的
事件接到下一帧，不会漏。事件只报本队看得到的变化，迷雾里倒下或长回来的树要等再次看到那里才更新，所以这一层
是**本队知道的**树（实测见 [MAP_DATA.md](MAP_DATA.md) 4.5）。临时树（发芽、种下的树枝）不进树表。

英雄没被上报但还活着时（刚复活），地图部分以出生点 `RESPAWN_LOCATION` 为中心算；死了且没上报时全 0。

### 1.6 `runes`（4×4）

行固定为 `power_top`、`power_bottom`、`bounty_top`、`bounty_bottom`。成对的点按中路对角线命名：`y > x`（西北侧）
是 `_top`（见 [MAP_DATA.md](MAP_DATA.md) §3）。2:00、4:00 的水神符和 6:00 起的强化神符都刷在两个 `power_*` 点。

| i | 名称 | 取值 |
|---|---|---|
| 0 | `rel_x` | `(spot.x - hero.x) / 8192` |
| 1 | `rel_y` | `(spot.y - hero.y) / 8192` |
| 2 | `distance` | 上两位的欧氏长度 |
| 3 | `available` | worldstate `rune_infos` 里这个点的状态是 AVAILABLE；**受视野限制**，看不到时状态是 UNKNOWN，这一位为 0 |

### 1.7 `landmarks`（10×4）

行固定为 `roshan_top`、`roshan_bottom`、`tormentor_top`、`tormentor_bottom`、`wisdom_top`、`wisdom_bottom`、
`lotus_top`、`lotus_bottom`、`outpost_top`、`outpost_bottom`（肉山坑、魔方刷新点、智慧神龛、莲花池、前哨），
命名规则同 1.6。

| i | 名称 | 取值 |
|---|---|---|
| 0-2 | `rel_x` / `rel_y` / `distance` | 同 `runes` |
| 3 | `is_ours` | 这个点上站着本方的建筑。只有前哨会是 1（开局 `outpost_top` 归天辉、`outpost_bottom` 归夜魇），神龛、莲花池是中立建筑 |

肉山现在在哪个坑、魔方现在在哪一侧会随时间变（7.41：肉山开局在上坑，15:00 起昼夜交替时换坑），观测里只有两个位置。

## 2. 动作

### 2.1 动作类型

| 值 | `ActionType` | 用到的字段 |
|---|---|---|
| 0 | `NOOP` | — |
| 1 | `MOVE` | `move`：16 方向（0=东，逆时针），每次朝该方向 300 距离下一个 `MOVE_DIRECTLY` |
| 2 | `ATTACK` | `target`：`units` 表行号 |
| 3 | `CAST` | `ability`：无目标，或对自己用（友方单位技能、药水；吃树找最近的树；物品指向地点的放脚下） |
| 4 | `CAST_TARGET` | `ability` + `target`：对 `units` 表里的单位用；只能指地面的技能打在这个单位脚下，矢量技能从我方英雄穿过它继续往前 |
| 5 | `STOP` | — |
| 6 | `CAST_DIRECTION` | `ability` + `move`：朝 16 方向之一、按这个技能的 `cast_range` 放到地面上；矢量施法的技能（法球、滚滚）第二个点沿同一方向继续（见 2.6） |

`ability` 0-5 是技能槽，6-11 是背包格 0-5。每个格子能用哪几种施法由掩码给出（见 2.3、2.6）。
无关字段会被忽略。`MOVE_DIRECTLY` 是直线移动，不绕障碍，长距离寻路要自己打点。
英雄活着但没被上报时，`MOVE` 以外的动作都会被换成朝地图中心走一步（见 1.1）。

### 2.2 空间

```python
action_space = Dict(type=Discrete(7), move=Discrete(16), target=Discrete(32), ability=Discrete(12))
team_action_space = Dict(type=MultiDiscrete([7] * 5), move=..., target=..., ability=...)  # 每个 key 一个 (5,) 数组
```

5v5 每个 key 是长度 5 的数组，第 `i` 位驱动第 `i` 个英雄。`actions.hero_action(action, i)` 把第 i 列
取出来变成单英雄动作字典。

### 2.3 合法性掩码 `info["action_mask"]`

1v1 是 `type` (7,) / `attack_target` (32,) / `cast_target` (32,) / `ability` (7, 12)，5v5 全部前面多一维 (5, …)。
`ability` 按动作类型分行：`ability[t]` 是动作类型 `t` 能用的格子，只有 `CAST` / `CAST_TARGET` / `CAST_DIRECTION`
三行会有 1，其余行全 0。RL 里先采样 `type`、再用 `ability[type]` 遮 `ability` 那一头就行。

| 掩码位 | 条件 |
|---|---|
| `NOOP` | 永远合法 |
| `MOVE` | 能行动（没被晕 / 妖术 / 噩梦）且没被缠绕；英雄"活着但没被上报"时只有它合法 |
| `STOP` | 能行动 |
| `ATTACK` | 能行动、没被缴械，且至少有一个 `attack_target` |
| `CAST` / `CAST_DIRECTION` | 能行动，且 `ability[该类型]` 里至少有一位是 1 |
| `CAST_TARGET` | 同上 + 至少有一个 `cast_target` |
| `attack_target[i]` | 敌方单位：非物理免疫且非无敌；**己方**单位：非英雄非塔且血量 < 50%（反补） |
| `cast_target[i]` | 敌方单位：非魔法免疫且非无敌 |
| `ability[t][s]` | 格子 s 现在可用，且它的施法类型（2.6）允许动作类型 t |

"现在可用"：技能是等级 > 0、`is_fully_castable`、英雄没被沉默；物品是格子里有东西、`is_fully_castable`、
英雄没被缄默（沉默不影响物品）。施法类型到动作类型：

| 施法类型 | `CAST` | `CAST_TARGET` | `CAST_DIRECTION` |
|---|---|---|---|
| `no_target` 无目标 | ✅ | | |
| `self` 能对友方单位（包括自己）用，吃树也算 | ✅ 对自己 | | |
| `enemy` 能对敌方单位用 | | ✅ | |
| `point` 指向地面（含矢量施法） | 只限物品：放脚下 | ✅ 打在目标脚下 | ✅ |
| `vector` 矢量施法，和上面几种同时出现 | 不影响掩码；这两种施法都改走服务器 VM 并带上第二个点（2.6） | | |
| 空（被动） | | | |

Lua 还没上报过的格子（开局头几帧、刚买的物品）算不可用。

英雄死亡时只剩 `NOOP`。`env.unwrapped.sample_legal_action()` 按掩码均匀采样，可当 baseline。

### 2.4 自动购买 / 加点

两个环境都会在第一步给每个受控英雄买 `starting_items`，并在 `ability_points > 0` 时按
`ability_priority`（默认 `(5, 0, 3, 4, 1, 2)`，先大招）逐个槽位尝试加点，Lua 跳过当前不能升的。
传 `()` 关闭，改用 `queue_purchase()` / `queue_train_ability()`（5v5 的两个方法第一个参数是英雄行号）。
同一条通道上还有 `queue_chat(message)`（5v5 是 `queue_chat(row, message)`），下一步在 all-chat 里说一句话；
Lua 会执行 `extra_actions` 里的每一条，所以喊话不占英雄的主动作。
⚠️ 受控方的内置买装 AI 是关掉的，5v5 全程只有出门装，装备这条线要自己接。

### 2.5 物品怎么用

`ability = 6 + k` 对应背包格 k，下发给 Lua 的是负数槽位 `-(k + 1)`（bridge 层的约定）。
`CAST_TARGET` 只能对敌方单位用（`cast_target` 只放开敌人）；`CAST` 对自己用，2026-09 在 1v1 里实测：

| 物品类型 | Lua 怎么做 | 实测 |
|---|---|---|
| 无目标（如仙灵火、魔棒） | 直接使用 | 仙灵火 ✅ |
| 指向单位（如治疗药膏、净化药水） | 对自己用 | 两个都 ✅，身上出现 `modifier_flask_healing` / `modifier_clarity_potion` |
| 吃树 | 吃 700 范围内最近的树，找不到就打一行日志；树不在施法距离内时英雄会先走过去（约 2-4 秒），之后的 `MOVE` 会打断它 | 1v1 一塔旁 ✅；5v5 泉水边走过去吃到 ✅ |
| 指向地点（如眼、树枝） | 放在自己脚下 | 假眼 ✅；树枝在一塔旁 ✅，在泉水里种不出来 |
| 被动（如圆环） | 掩码里就是 0 | ✅ |

### 2.6 施法类型从哪来

worldstate 里没有技能 / 物品的施法方式。Lua 每帧看一遍 0-5 号技能和背包格，名字或施法类型一变就打一行
`SLOTS {team, player_id, slots}`，由 `behavior.lua` 从 `GetBehavior()` 和 `GetTargetTeam()` 算出
`no_target` / `self` / `enemy` / `point`，矢量施法的技能再多报一个 `vector`。所以技能被魔晶、神杖改了施法方式，
或者换了技能，掩码也会跟着变。

两处 Lua 端的换算：`CAST_TARGET` 遇到只能指地面的技能，改成 `Action_UseAbilityOnLocation(目标脚下)`，因为
客户端会忽略对这种技能下的单位指令；`CAST` 遇到友方单位技能 / 药水，对自己施放。
`CAST_DIRECTION` 的落点在 Python 里算：英雄位置 + `cast_range` × 方向，`cast_range` 为 0 时退回 300。

2026-09 在 5v5 里实测（对中路敌方小兵，走的都是 gym 动作）：

| 技能 | 施法类型 | 动作 | 结果 |
|---|---|---|---|
| 莉娜 龙破斩 | `enemy` `point` | `CAST_DIRECTION` | ✅ |
| 帕克 幻象法球（矢量施法） | `point` | `CAST_TARGET` 点小兵 | ✅（改之前直接对单位下指令放不出来） |
| 滚滚 虚张声势（矢量施法） | `point` | `CAST_DIRECTION` | ✅ 冲出去约 570 |
| 戴泽 薄葬 | `self` | `CAST` | ✅ 自己身上出现 `modifier_dazzle_shallow_grave` |
| 莱恩 裂地尖刺 | `enemy` `point` | `CAST_TARGET` | ✅ |

矢量施法的技能（Lua 多报一个 `vector`）由 Python 改发 `DOTA_UNIT_ORDER_CAST_VECTOR`：`CAST_DIRECTION` 的第二个点
沿同一方向继续，`CAST_TARGET` 的第二个点在"我方英雄 → 目标"的延长线上（目标就站在脚下时用英雄朝向）。bot API 发不出
第二个点，这条动作由服务器 VM 里的 `bridge/lua/server_actions.lua` 用 `ExecuteOrderFromTable` 发两条指令完成，
实测滚滚挥砍、帕克法球都跟着第二个点走（[VERSION_DIFF.md](VERSION_DIFF.md) 3.1）。文本里这类技能的 `ready:` 后面
会带 `(vector: runs on past the target / along the direction)`，LLM 的 system prompt 也解释了这一点。
打在目标脚下没有提前量，移动中的英雄可能躲开。同一技能还在转身 / 抬手时再下一次施法会被客户端忽略，
所以连续两帧对同一技能换目标，第二次不生效。

## 3. 奖励

都是"每步各分量的差值 × 权重求和"，分量（未加权）在 `info["reward"]` 里，权重可以在构造时覆盖：
`TeamReward(weights={'kill': 2.0})`。

### 3.1 `LaningReward`（1v1）

| 分量 | 权重 | 含义 |
|---|---|---|
| `last_hit` | 0.16 | 正补数变化 |
| `deny` | 0.12 | 反补数变化 |
| `xp_level` | 0.3 | 等级变化 |
| `health` | 1.0 | 自身血量比例变化（双方都活着时才计） |
| `enemy_health` | 0.8 | 敌方英雄血量比例下降（只在敌人可见时计） |
| `kill` | 2.0 | 击杀数变化 |
| `death` | -2.0 | 死亡数变化 |
| `tower_health` | 1.5 | 敌方一塔掉血比例 − 己方一塔掉血比例 |
| `win` | 5.0 | 胜 +1 / 负 -1，只在最后一步 |

### 3.2 `TeamReward`（5v5）

同样的骨架，把个人项对本方 5 个英雄求和，并把"一塔血量"换成全局推进项。

| 分量 | 权重 | 含义 |
|---|---|---|
| `last_hit` | 0.02 | 5 人正补数变化之和 |
| `deny` | 0.02 | 5 人反补数变化之和 |
| `xp_level` | 0.2 | 5 人等级变化之和 |
| `health` | 0.5 | 5 人血量比例变化之和（每人只在自己前后两帧都活着时计） |
| `enemy_health` | 0.4 | 5 个敌方英雄血量比例下降之和（只在可见时计） |
| `kill` | 1.0 | 本方总击杀变化 |
| `death` | -1.0 | 本方总死亡变化 |
| `tower` | 2.0 | （敌方倒塌的塔数）−（己方倒塌的塔数），整座算 |
| `ancient` | 3.0 | （敌方基地掉血比例）−（己方基地掉血比例） |
| `win` | 10.0 | 胜 +1 / 负 -1 |

奖励是**队伍级的一个标量**，不做个体信用分配；要按英雄拆分的话，个人项（`last_hit` / `deny` /
`xp_level` / `health`）在 `TeamReward.__call__` 里本来就是逐英雄算完再求和的，继承改写即可。

## 4. 终止

| | 条件 |
|---|---|
| `Mid1v1Rules` | 任一方第 2 次死亡（任何死因）或一塔被破 → `terminated`；`dota_time ≥ 600` → `truncated` |
| `AllPick5v5Rules` | 任一方基地（`npc_dota_*_fort`）被破 → `terminated`，赢家是对面；`dota_time ≥ 2400` → `truncated` |

两者都会在 `game_state == POST_GAME` 时 `terminated`。决定胜负的那一下之后客户端立刻停止推送
worldstate，所以真正的胜负是从 console.log 的 `Building: npc_dota_*_fort destroyed` 读出来的
（`DotaSession.match_winner()`）；客户端无数据又没有胜负记录（崩了）则 `truncated` + `info["error"]`。

## 5. 已知缺口

观测里还没有：modifier、投射物、符文的种类（只有点位上有没有符）、肉山 / 魔方当前在哪、临时树、信使、
经验值（只有等级）、敌方金钱、背包格 6-8 和储藏处。
动作里还没有：对队友施法（对自己可以用 `CAST`）、信使、买活、TP；矢量施法的第二个点实测给不了（2.6）。5v5 另外还缺：小地图级的全局单位表（现在每个英雄只看自己周围 1600）、按英雄的奖励拆分。
