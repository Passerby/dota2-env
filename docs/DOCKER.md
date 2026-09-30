# Docker：无界面 Dota 2 + dota2-env

实测环境：RTX8000（宿主 Ubuntu 26.04，内核 7.0，88 核，snap 版 Docker 29.8，服务器在国内），镜像 Ubuntu 24.04，
Dota 2 buildid **25539253**（和 macOS 客户端同一个 build）。✅ = 实测，⚠️ = 没实测或推断。

## 结论

- **镜像里没有 Dota。** 镜像 `dota2-env`（约 680M）只有 Linux 客户端要的系统库、SteamCMD 和 Python 环境；
  Dota（约 76G）装在宿主机的大盘上，运行时挂到容器的 `/opt/dota2`。不打进镜像的原因：76G 会落在 Docker 的数据目录
  （这台机器在系统盘），containerd 还要再存一份压缩层；每次 Dota 更新都得整体重建；构建时还得把 Steam 登录带进去。
  挂载的话，更新是增量的（SteamCMD 只下改了的文件），镜像也不用跟着 Dota 版本走。
- **匿名账号下不到游戏内容，要一个 Steam 账号登录一次**（免费账号就行，见第 1 节）。登录缓存放在宿主机的一个目录里，
  之后下载、更新都不用再输密码。
- **一个容器 = 一个环境实例。** 端口（12120/12121）、`pkill dota2`、`bots` 软链接在容器里都是容器自己的，多个实例并行
  就是多个容器，每个容器给 Dota 目录套一层自己的 overlay（第 5 节）。
- 只支持无界面（`render_mode=None` / `"ansi"`，也就是 `-dedicated`）。

## 1. 为什么要 Steam 账号

| 做法 | 结果 |
|---|---|
| `+login anonymous +app_update 570` | 显示 `Success! App '570' fully installed.`，实际只装了 **718M**：Linux 可执行文件的 depot 373306（`game/bin`、`game/dota/bin`、shader）。没有 `gameinfo.gi`、`pak01_*.vpk`、地图，客户端一启动就 `FATAL ERROR: Application unable to load gameinfo.gi` ✅ |
| `+login anonymous +download_depot 570 373301`（内容 depot） | `Depot download failed : missing license for depot (No subscription)` ✅ |
| `+login anonymous anonymous` | 同上 ✅ |
| `+login anonymous +app_license_request 570` | `AppID 570 already owned.`，接着下内容 depot 仍然是 missing license ✅ |
| `ServerAppID` 373310 / `CustomGameServerAppID` 471280（`steam.inf` 里写的） | 匿名看不到任何 depot（私有 app） ✅ |
| 真实账号 `+login <账号> +app_update 570` | 75,939,309,076 字节，完整安装 ✅ |

匿名账号「拥有」570 这个 app，但它的许可证只覆盖可执行文件那个 depot，所以 `app_update` 会报完整安装。内容 depot
（373301、381451–381455）要账号。[dotaservice](https://github.com/TimZaman/dotaservice/blob/master/docker/README.md) 的
Dockerfile 也是用账号 + Steam Guard 码下载的。从已经装好的 macOS 客户端拷内容也行（内容 depot 不分平台，build 一致），
但这台服务器从 Mac 拷只有 0.59 MB/s。

## 2. 构建镜像

```bash
docker build -t dota2-env \
  --build-arg UID=$(id -u) --build-arg GID=$(id -g) \
  --build-arg APT_MIRROR=mirrors.tuna.tsinghua.edu.cn \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  .
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `UID` / `GID` | 1000 / 1000 | 容器里跑一切的用户 `dota`。设成宿主机上 Dota 目录主人的 id，SteamCMD 下下来的文件才归你，不归 root |
| `APT_MIRROR` | 空（官方源） | 只写主机名，替换 `archive.ubuntu.com` 和 `security.ubuntu.com`；走 http，因为装证书之前 https 源验不了 |
| `PIP_INDEX_URL` | `https://pypi.org/simple` | PyPI 镜像 |
| `BASE_IMAGE` | `ubuntu:24.04` | Docker Hub 拉不动时换镜像站，比如 `docker.m.daocloud.io/library/ubuntu:24.04` |
| `STEAMCMD_URL` | Valve 的 `steamcmd_linux.tar.gz` | 在国内可以直接下载 ✅ |

在 RTX8000 上用上面几个镜像，1 分 23 秒构建完 ✅。**snap 版 Docker 的命令行读不到 `/mnt` 下的目录**（没接
`removable-media`），构建上下文要放在 `$HOME` 里；daemon 那边挂载 `/mnt` 没问题 ✅。

## 3. 登录一次，下载 / 更新 Dota

```bash
DOTA=/mnt/data-0/kevin/dota2     # Dota 安装，约 76G
STEAM=/mnt/data-0/kevin/steam    # SteamCMD 的登录缓存
mkdir -p $DOTA && mkdir -m 700 -p $STEAM
```

第一次登录要交互，自己输密码和 Steam Guard 码（邮件或手机）：

```bash
docker run -it --rm --hostname dota2-steam -v /etc/machine-id:/etc/machine-id:ro \
  -v $STEAM:/home/dota/Steam dota2-env steamcmd.sh +login <账号> +quit
```

之后下载和更新都用缓存的凭据（`Logging in using cached credentials.` ✅）。`--hostname` 和 machine-id 固定下来，是为了让
每个 SteamCMD 容器在 Steam 那边都像同一台机器（去掉它们缓存会不会失效没测 ⚠️）。下载在后台跑，SSH 断了也不影响；
SteamCMD 出错退出时 Docker 会重启它，接着上次的进度下：

```bash
docker run -d --name dota2-download --restart on-failure:20 --hostname dota2-steam \
  -v /etc/machine-id:/etc/machine-id:ro -v $STEAM:/home/dota/Steam -v $DOTA:/opt/dota2 \
  dota2-env steamcmd.sh +force_install_dir /opt/dota2 +login <账号> +app_update 570 +quit
docker logs -f dota2-download     # 看到 Success! App '570' fully installed. 就完了
docker rm dota2-download
```

实测 ✅：75.9G 用 5 分 38 秒下完（国内的 Steam CDN，约 225 MB/s），没有中断过；文件属于构建时给的 `UID`。
更新是同一条命令，SteamCMD 只下有变化的文件。**更新前先停掉所有在跑的环境容器**：overlay 的底层在挂着的时候被改，
行为是未定义的。

## 4. 跑一个环境

```bash
docker run --rm --init -v $DOTA:/opt/dota2 dota2-env python -u examples/scripted_agent.py --timescale 4
```

实测 ✅：1500 步全程 20.0 steps/s（4 倍速的上限，和 macOS 一样），Lua 回执里的动作延迟 0.1 游戏秒；结束后 `bots` 软链接
和 `dota2_env_server.cfg` 都从安装目录里拿掉了。客户端自己把打开文件数的软上限提到 4096，一局开着 277 个，不用
`--ulimit`。

- `--init` 让 tini 当 1 号进程，替 Python 收掉孤儿进程。
- LLM 对局：API key 用 `--env-file .env` 传（`.env` 不进镜像）；自己的 `configs/*.yaml` 也不进镜像（只有示例），挂进去：
  `-v $PWD/configs:/opt/dota2-env/configs:ro`；transcript 默认写 `logs/`，要留下来就 `-v $PWD/logs:/opt/dota2-env/logs`。
  实测 ✅（2026-09-29，`deepseek_5v5` 改成无界面，原速）：1671 次决策，延时中位数 0.71 s、到第一行 0.53 s；同一份配置
  在海外的 Mac 上是 1.12 s / 0.93 s。DeepSeek 在国内，回得快，每个英雄能真正每秒决策一次（5 个英雄合计每游戏秒 4.9 次，
  Mac 上 4.0 次），按单价估的花费也涨得更快：3 美元的 `spend_limit_usd` 在 4:12 就到了。
- 模型也在这台机器上（比如 vLLM）时，别给环境容器用 `--network host`：那样 12120/12121 又变成全机共用，只能跑一个实例。
  把模型容器和环境容器放进同一个自建网络（`docker network create dota`，两边都 `--network dota`），gateway 的
  `base_url` 写模型容器的名字，比如 `http://qwen35-4b:8000/v1`（这种接法没在对局里实测 ⚠️）。
- 只支持无界面。开窗口的模式（`render_mode='human'`、`--render`）要 X 和 GPU，容器里没有。

## 5. 多个环境并行

一个容器一个实例。环境要往 Dota 目录里放 `bots` 软链接和 `dota2_env_server.cfg`，Lua 每一步都经这个软链接读动作文件，
几个容器共用一个可写的安装目录会互相覆盖，所以每个容器给 Dota 目录套一层自己的 overlay：看到的是同一份文件，
写的东西进自己的 upper。

```bash
RW=/mnt/data-0/kevin/dota2-rw    # 每个实例的可写层，只放软链接、cfg 和 Dota 自己写的小文件
for i in 0 1 2 3; do
  mkdir -p $RW/$i/upper $RW/$i/work
  docker volume create --driver local --opt type=overlay --opt device=overlay \
    --opt o=lowerdir=$DOTA,upperdir=$RW/$i/upper,workdir=$RW/$i/work dota2-$i
  docker run -d --init --name dota2-env-$i -v dota2-$i:/opt/dota2 dota2-env \
    python -u examples/scripted_agent.py --timescale 4
done
```

实测 ✅：4 个容器同时跑，每个都是 20.0 steps/s，每个约占 1 核、850M 内存；共享的安装目录一个字节没被写；一局下来
upper 里只剩 Dota 自己写的 `boot.vcfg`，4 个实例加起来 308K。88 核的机器按每实例 1 核粗算能跑几十个，上限没测 ⚠️。

volume 可以一直留着反复用（upper 里的软链接和 cfg 下一局会被替换）。清理时 `work/work` 是内核以 root 建的，普通用户
删不掉，借容器用 root 删：

```bash
docker volume rm dota2-0 dota2-1 dota2-2 dota2-3
docker run --rm --user root -v $(dirname $RW):/x dota2-env rm -rf /x/$(basename $RW)
```

## 6. 排障

| 现象 | 原因 / 办法 |
|---|---|
| `FATAL: It appears dota.sh was not launched within the Steam for Linux sniper runtime environment` | Linux 的 `dota.sh` 只肯在 Valve 的 sniper runtime 里跑；环境在 Linux 上直接起 `bin/linuxsteamrt64/dota2`（[VERSION_DIFF.md](VERSION_DIFF.md) 1.4）。自己手动起客户端时也别用 `dota.sh` |
| `FATAL ERROR: Application unable to load gameinfo.gi file from directory "dota"` | 只装了匿名账号那 718M，内容没下（第 1 节） |
| 启动时报缺某个 `.so` | 对照 Dockerfile 里 apt 装的那几个库；`ldd` 查：`LD_LIBRARY_PATH=/opt/dota2/game/bin/linuxsteamrt64 ldd <.so>` |
| SteamCMD 又要密码：`Cached credentials not found` | 登录缓存失效或 `$STEAM` 没挂对，按第 3 节重新交互登录一次 |
| `docker build` 报 `open Dockerfile: no such file or directory` | snap 版 Docker 的命令行读不到 `/mnt`，把仓库放到 `$HOME` 下再构建 |
| SteamCMD 打 `PosixFileOpen: RESOLVE_BENEATH unsupported` | SteamCMD 自己退回普通 `open`，下载不受影响 ✅；原因推测是容器的 seccomp 规则没放行 `openat2` ⚠️ |
