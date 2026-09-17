# 重建演练报告 · 2026-09-17

> 演练级别：**② 半清空 + 卸包**（停服务 → 删站点目录 → 卸载系统包 → 按文档从零重建）
> 演练场地：**生产实例 `lhins-4pxfpuht` 本身**（对外端口已关闭，外部暴露面为 0）
> 执行时间：2026-09-17 22:55:53 → 23:04:41
> 结论：**成功**。抓到 **2 个致命缺陷**（已修复），恢复链路与数据完整性均验证通过。

---

## 一、结论先行

| 项 | 结果 |
|:---|:---|
| 重建是否成功 | ✅ **是**（从裸环境重建出完整可用系统） |
| 数据是否完整回来 | ✅ `integrity_check=ok`、`report=8`、`user=1` |
| **RTO（实况，含 2 次排障）** | **8 分 48 秒** |
| **RTO（纯执行，包已可用）** | **约 2–3 分钟**（`deploy.sh` 单独 54 秒） |
| 是否达到目标 | ✅ 远优于 RTO-数据 ≤ 1h / RTO-整机 ≤ 2h |
| 新发现缺陷 | **2 个致命 + 1 个小笔误**，全部已修复 |

---

## 二、时间线

| 时刻 | 事件 | 备注 |
|:---|:---|:---|
| 22:40:40 | 演练前补跑 `backup.sh` | 新备份 `app-20260917-224040.db.gz` |
| 22:47 | 数据与配置拉到本机并逐一校验 sha256 | 本机副本 + 邮箱副本双保险 |
| 22:51 | 建快照 `before-rebuild-drill-20260917` | 整机回滚点 |
| **22:55:53** | **DRILL_START** — 记录基线 | 4 服务 active / 5 条 cron / 13G 磁盘 / 71M 目录 |
| 22:56:11 | 停服务 + `rm -rf /opt/research-reports` | 含 venv / data / app.env / 全部脚本 |
| 22:56:11→29 | `apt-get purge caddy sqlite3 fail2ban redis-server` | **四个命令全部消失** |
| 22:56:51 | 解包 → 第 1 次 `deploy.sh` | 🔴 **失败：CRLF** |
| 22:57:33 | 就地修行尾 → 第 2 次 | 🔴 **失败：gpg 无 tty** |
| 22:59:45 → 23:00:39 | 第 3 次 `deploy.sh` | ✅ **exit=0（54 秒）** |
| 23:04:41 | 配置 + 数据还原完成 | `RESTORE_DONE` |
| 23:10:32 | 终态自检（异地备份 / 告警脚本） | ✅ 全绿 |

---

## 三、抓到的缺陷（本次演练最大价值）

### 🔴 缺陷 1：CRLF 行尾 —— `deploy.sh` 第一行就挂

```
deploy.sh: line 13: set: pipefail: invalid option name
file:  ... with CRLF line terminators      ← 415/415 行全是 CRLF
```

**根因**：本机是 Windows，`core.autocrlf` 把工作区的 `.sh` 转成 CRLF；而 `pack.sh` 是
**「从工作区打包」** → CRLF 原样进 tar → Linux 上 `set -euo pipefail` 变成 `set -euo pipefail\r`。

> ⚠️ **最迷惑的一点**：**git 仓库里的 blob 本来就是 LF**，问题只出在「工作区被转 CRLF → 打包带走」这一段。
> 所以从 `git clone` 出来的版本是好的，从**本机工作区打包**的版本是坏的 —— 而部署恰恰走后一条路。

**修复**：
1. 新增 **`.gitattributes`**（`* text=auto eol=lf` + 各脚本类型再钉一遍 + 二进制声明）
2. `pack.sh` 增加「**归一 + 校验**」步骤 —— 只归一不校验等于没做
3. 把工作区残留的 CRLF 文件重新检出

### 🔴 缺陷 2：`gpg` 在无 tty 环境失败 —— 脚本自称幂等但实际不幂等

```
gpg: cannot open '/dev/tty': No such device or address
```

**根因**：`/usr/share/keyrings/caddy-stable-archive-keyring.gpg` **已存在** → gpg 询问「是否覆盖」
→ TAT / CI / cron 无控制终端 → 失败。

✅ 已排除网络因素（Cloudsmith 可达，HTTP 200 / 6884 字节）。

**修复**：`gpg --batch --yes --dearmor`

### 🟡 小笔误
收尾提示写「还需要你手工做**两**件事」，但实际列了**三**条 → 已改为「三件事」。

---

## 四、成功侧的关键验证

| 验证项 | 结果 |
|:---|:---|
| **`sqlite3` 是否被自动装上** | ✅ **是** —— 说明「把 sqlite3 加进安装列表」的修复生效（此前必然重演静默失败） |
| crontab 条数 | ✅ **5 条，与基线完全一致** |
| 含 `offsite-backup.py`（03:20）与 `alert-run.sh`（每 6h） | ✅ 都在 |
| **cron 引用的 4 个脚本是否都存在** | ✅ 全部存在（修复前 `offsite-backup.py` 根本不在仓库里） |
| 4 个服务 | ✅ `research-reports` / `caddy` / `redis-server` / `fail2ban` 全 active |
| 监听端口 | ✅ 与基线一致（80 / 443 / 8081 / 8080 / 6379 / 2019） |
| 数据还原 | ✅ `integrity_check = ok`、`report=8`、`user=1` |
| 还原后配置可用 | ✅ `offsite-backup.py --dry-run` 打包成功（43.8 KB）；`alert.py` 4 项全绿 |

---

## 五、重建时的必知副作用

1. **`deploy.sh` 会重新生成 `app.env`**（随机 `JWT_SECRET` / `WORKER_TOKEN`）
   → **必须还原原值，否则 Worker 断线**。本次已从本机备份还原。
   > 建议后续给 `deploy.sh` 加「已存在则跳过」逻辑，避免这一步成为隐藏陷阱。
2. **`/opt/research-reports-backups/` 幸存**（它不在 `/opt/research-reports` 之下）
   → 事实上构成一层额外安全网，设计上值得保留。
3. **演练期间外部零影响**：端口本就关闭，重建期间对外暴露面不变。

---

## 六、本次演练的复盘

**做对了什么**：
- 演练前把**数据 + 配置都拉到本机并校验 sha256** —— 这是敢在生产机上做破坏性演练的前提
- 先建快照再动手
- 分三级（① 半清空 / ② 半清空+卸包 / ③ 全清空）评估，选了保留远程通道的 ②

**如果重来一次会改什么**：
- 「**从工作区打包 → 部署**」这条路径，应该像 `git clone` 一样可信。本次两个缺陷都出在这条路径上。

**结论**：**两个缺陷都只能在真实重建时暴露** —— 静态检查、代码审查、甚至 `bash -n` 都发现不了
（CRLF 的 `bash -n` 在 Linux 上是对 LF 版本做的，看不出问题）。**这就是重建演练不可替代的原因。**
