# Docker 磁盘占用排查与回收（Windows + WSL2）

> 2026-11-11 实战记录。C 盘从 86GB 可用恢复到 150GB，vhdx 从 89.5GB 降到 27.1GB。
> 本文只记录**可复现的判断依据和命令**，避免下次再从零试错。

## 一、结论先行

`docker_data.vhdx` 的大小不等于 Docker 的实际占用，也不等于任何单个工具报的数字。
真正要盯的是三个数：

| 数 | 怎么取 | 意义 |
|---|---|---|
| vhdx 文件大小 | `Get-Item $env:LOCALAPPDATA\Docker\wsl\disk\docker_data.vhdx` | 它在 C 盘上实占多少 |
| 数据盘已用 | 盘内 `df -Pk /mnt/docker-desktop-disk` | 里面真正装了多少 |
| 差值（slack） | 上两者相减 | **能压缩回收多少** |

只有 slack 是压缩收益的判据。其他数字（`docker system df`、`fstrim` 报的 trimmed 字节数）都不靠谱，理由见第三节。

## 二、诊断顺序

### 1. 先看 Docker 自己认账多少

```bash
docker system df          # 四项汇总
docker system df -v       # 逐镜像/容器/卷
```

注意：这个数字**偏低**，它不计两样东西——容器日志，和 `desktop-containerd` 里的 containerd 层存储。

### 2. 再看盘内真实占用（必须扫全树）

```powershell
# 新版 Docker Desktop 里 wsl -d docker-desktop 看不到数据盘，要用 nsenter
docker run --rm --privileged --pid=host --entrypoint sh redis:7.2-alpine `
  -c "nsenter -t 1 -m -- df -h /mnt/docker-desktop-disk"
```

```powershell
# 逐个目录量，确认大头在哪
docker run --rm --privileged --pid=host --entrypoint sh redis:7.2-alpine `
  -c "nsenter -t 1 -m -- du -sm /mnt/docker-desktop-disk/data/*"
```

**教训**：只扫 `data/docker/*` 会漏掉 `data/desktop-containerd`——本次那 48GB 就藏在那里，
只扫 docker 目录会得出"51GB 无文件可达"的错误结论。

### 3. 定位完再决定动作

| 占用形态 | 处置 |
|---|---|
| `overlay2` 里有大量悬空层 | `docker image prune`（不带 `-a`） |
| 容器日志 | 加 `logging.options.max-size` 限流，或 truncate |
| 构建缓存 | `docker builder prune` |
| 悬空卷 | `docker volume prune -af`（先 `docker volume ls -qf dangling=true` 核对） |
| slack 很大 | 压缩 vhdx（见第四节） |

## 三、四条关键判据（都是踩过的坑）

### 1. 镜像的 "SIZE" 是共享层摊算，删镜像往往回收极少

5 个 `2.09GB` 的后端镜像 tag 共享 2.075GB 基础层，每个独有只 19MB。
删掉 3 个悬空镜像，Docker 报的回收量是 **0B**。

### 2. WSL2 的 discard 不穿透到 vhdx，`fstrim` 报多少都没意义

实测：`fstrim -av` 报 `934.59GB trimmed`，随后的 `Optimize-VHD` 只回收 **0.09GB**。
因为 ext4 的空闲块从来没被分配进 vhdx，trim 它们对文件大小毫无影响。

→ **判断压缩收益只看 slack，不看 trimmed。**

### 3. 新版 Docker Desktop 里 `wsl -d docker-desktop` 进不到数据盘

现象：`df` 只有 128MB 的根盘（`/dev/sde`），`/mnt/docker-desktop-disk` 不存在，
导致 `fstrim` 只 trim 到 `/`。

原因：引擎跑在 distro 内部的系统容器里，数据盘只挂在**那一层的 mount namespace**。

解法：特权容器 + `nsenter` 进 VM init（pid 1）的 namespace：

```bash
docker run --rm --privileged --pid=host --entrypoint sh IMAGE -c "nsenter -t 1 -m -- <命令>"
```

`IMAGE` 只要本地有、带 busybox sh 和 nsenter 即可（`redis:7.2-alpine` 就够）。

### 4. `docker system df` 不计容器日志

日志在 `/mnt/docker-desktop-disk/data/docker/containers/<id>/*-json.log`，
默认无上限。本次最大一个 493MB。根治办法是在 compose 里限流：

```yaml
logging:
  driver: json-file
  options:
    max-size: "50m"
    max-file: "3"
```

## 四、回收流程

### 压缩 vhdx

```powershell
# 必须管理员（Optimize-VHD / diskpart 需要）
pwsh -File .\scripts\docker-compact.ps1 -Restart -StartStack
```

脚本六步：环境检查 → 确保 Docker 运行且数据盘挂载 → fstrim → 优雅停 Docker →
`Optimize-VHD -Mode Full`（无 Hyper-V 时回退 `diskpart compact vdisk`）→ 可选重启 + 拉起生产栈。

- 余量不足 1GB 会自动跳过（不白停一次生产栈），`-Force` 可强制
- 日志在 `backups/compact/docker-compact_YYYYMMDD.log`
- 用 `-VhdxPath` 指定非默认磁盘位置

### 停机的正确姿势

优先 `docker desktop stop`（会通知容器、刷盘、正常卸载 ext4）；
只有等 45 秒句柄仍未释放，才用 `wsl --shutdown`。
**硬停正在写盘的 ext4 是孤儿 inode 的典型成因**，会造出 df 算已用、du 看不见的僵尸块。

### containerd 遗留快照

`data/desktop-containerd/daemon/io.containerd.snapshotter.v1.overlayfs`
在关闭 containerd 镜像存储后可能原地遗留几十 GB。处置脚本 `scripts/containerd-cleanup.ps1`
（改名 `.stale` → 重启验证 → 确认后删除，失败自动回滚）。

> 2026-11-11 那次最终**没有执行**：Docker Desktop 自己重建了数据盘，48GB 随之消失。
> 脚本保留作预防，不要盲目跑。

## 五、不要做的事

| 操作 | 后果 |
|---|---|
| `wsl --manage <distro> --set-sparse true --allow-unsafe` | 微软因数据损坏风险默认禁用稀疏 VHD，生产数据盘上不要赌 |
| `rm -rf` containerd 数据目录 | 破坏 bolt 元数据，镜像层可能整片失效，只能重来 |
| `docker image prune -a` | 会删掉非运行所需的 tag，包括手工打的 `pre-*` 回滚点 |
| `docker system prune --volumes` | 直接删数据卷，postgres / milvus 数据无法恢复 |
| 在容器内 `-v /var/lib/docker:/v` 去 du | Docker Desktop 只共享允许路径，挂上去是空的 |

## 六、本次数据存档

| 时点 | vhdx | C 盘可用 | 备注 |
|---|---|---|---|
| 起始 | 89.54 GB | 86.0 GB | 悬空卷 11.13GB、构建缓存 9.2GB |
| 清理后 | 89.54 GB | 86.0 GB | 清卷 11.13GB + 缓存 4.73GB（vhd 未缩） |
| 首次压缩 | 74.41 GB | 103.7 GB | 回收 17.8GB |
| 二次压缩 | 74.41 GB | 103.7 GB | 零收益（trim 只打到 90MB 根盘） |
| 数据盘重建后 | 74.65 GB | 102.8 GB | 盘内实占降到 22.2GB，containerd 48GB 自愈 |
| **最终压缩** | **27.13 GB** | **150.3 GB** | 回收 47.58GB |

Docker 内部占用（`docker system df` 合计）：16.5GB → **9.7GB**
（镜像 4.93 + 卷 2.66 + 构建缓存 2.03 + 容器 0.12）

## 七、相关文件

- `scripts/docker-compact.ps1` — vhdx 压缩（fstrim → 停 Docker → Optimize-VHD）
- `scripts/containerd-cleanup.ps1` — containerd 遗留快照回收（可逆，带备份闸口）
- `backups/compact/*.log` — 两个脚本的执行日志
