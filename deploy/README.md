# 研报站点 · 部署与运维手册

> 目标机器：腾讯云 Lighthouse `lhins-4pxfpuht`（广州）· Ubuntu 24.04 · 2C2G / 50GB SSD
> 公网 IP：`203.0.113.10` ｜ 到期：**2026-10-09**
> **状态：已部署并运行中** ｜ 最后按实况核对：**2026-09-17**
>
> ⚠️ 本手册早期版本（2026-09-13）写着「尚未上传部署」、后端在 8080 —— **均为过期信息**。
> 当前实况以本文为准，端口一律以 **8081** 为准。

---

## 一、部署形态

```
访客浏览器
    ↓ https://203.0.113.10   （443；80 只做跳转）
┌──────────────────────────────────────────┐
│  Caddy v2.11.4（反向代理 + 静态托管）     │
│    /api/*   →  127.0.0.1:8081            │
│    /health  →  127.0.0.1:8081            │
│    /docs* /redoc* /openapi.json → 8081（透传）│
│    其他      →  frontend/dist/ 静态       │
└──────────────────────────────────────────┘
                    ↓
        uvicorn（FastAPI，仅回环 8081）
                    ↓
     SQLite（唯一事实源） + Redis DB 1（限流计数 / 加速层）
```

### 端口分配（2026-09-17 核对）

| 端口 | 占用者 | 监听 | 对外 |
|:---|:---|:---|:---|
| 443 / 80 | Caddy | 全网卡 | ⏸️ **云防火墙未放行** |
| 2019 | Caddy 管理 API | 回环 | ❌ |
| **8081** | 研报站后端 uvicorn | `127.0.0.1` | ❌ |
| 8080 | redis-demo（已下线对外路由） | `127.0.0.1` | ❌ |
| 6379 | Redis | `127.0.0.1` | ❌ |
| 22 | sshd（仅密钥） | 全网卡 | ✅ |

### 关键决策

| 决策 | 选择 | 理由 |
|:---|:---|:---|
| 域名 | **不用**，IP 直访 | 免 ICP 备案 |
| 前端托管 | Caddy 静态文件 | 比再起 Node 服务省内存（2G 机器要精打细算） |
| 后端监听 | 只 `127.0.0.1:8081` | 不直接暴露，所有流量必经 Caddy |
| **为何是 8081** | **8080 被 redis-demo 占用** | 两者并存避免冲突 |
| worker | **1 个** | SQLite 单写者，多 worker 无收益还有锁竞争 |
| Redis DB | `1`（不是 0） | `redis-demo` 占了 0，隔离免冲突 |

> ⚠️ **反直觉点**：前端**不需要**配 API 地址。所有请求都是 `/api/...` 相对路径，同源部署天然无跨域。

---

## 二、安全态势（2026-09-17 加固后）

| 层 | 措施 | 验证方式 |
|:---|:---|:---|
| **SSH** | `PasswordAuthentication no` / `PermitRootLogin prohibit-password` / `MaxAuthTries 3` / 仅密钥 | 本机跑 `ssh -o PubkeyAuthentication=no ubuntu@203.0.113.10` → 应**无密码提示**、直接 `Permission denied (publickey)` |
| **防火墙** | 云防火墙仅放行 **22 + ICMP** | 控制台防火墙页 |
| **fail2ban** | 双 jail：`sshd` + `caddy-research-reports`（读 Caddy 访问日志，封 80/443） | `fail2ban-client status caddy-research-reports` |
| **限流（应用层）** | slowapi：`auth_login` 10/分、`auth_captcha` 30/分、`auth_register` 5/时、`password_request` 5/时、`task_create` 20/时、`public_read` 200/分 | `backend/app/ratelimit.py` |
| **限流（网络层）** | fail2ban：`maxretry=15` / `findtime=600` / `bantime=3600` + **递增封禁**（×2，上限 24h） | 同上 |
| **接口文档** | `/docs` `/redoc` `/openapi.json` **默认关闭**（`ENABLE_DOCS=false`，fail-closed） | Caddy 显式透传，关闭时应返回 **404** 而非 200 |
| **服务特权** | 研报站与 redis-demo 均**非 root 运行** | `systemctl show research-reports -p User` |
| **数据库文件** | `app.db` 权限 `600` | `ls -l backend/data/app.db` 应为 `-rw-------` |

> ℹ️ **SSH 加固已自动化**：由 `deploy/ssh-harden.sh` 完成，`deploy.sh` 的**步骤 6.5** 会调用它。
> 它内置**自锁保护** —— 系统上找不到任何 `authorized_keys` 时**直接拒绝执行**，
> 避免「关掉密码登录 + 没有任何密钥」把人永久锁在门外。
> 只想体检不动手：`bash deploy/ssh-harden.sh`（不带 `--apply`）。

> ⚠️ **三个必知的坑**：
> 1. **改 sshd 必须双位置同改** —— `/etc/ssh/sshd_config` **和** `/etc/ssh/sshd_config.d/50-cloud-init.conf`（后者经 `Include` 加载会覆盖主配置）。`ssh-harden.sh` 已处理。
> 2. **关闭密码登录会让 OrcaTerm 的「终端连接(SSH)+密码」失效** —— 改用「免密连接(TAT)」或 VNC，或直接本机 `ssh root@...`。
> 3. **重载用 `reload` 不用 `restart`** —— `restart` 会断开当前正在用的连接，一旦新配置有问题就再也进不来。
>
> 📌 **这条曾经掉过链子**：SSH 加固在 2026-09-16 只改在服务器上、**没进仓库**，
> 导致 09-17 重建演练时发现「从零重建会退回 Ubuntu 默认」。**加固必须进版本库，否则等于没有。**

---

## 三、日常运维

### 定时任务（root crontab，共 4 条）

```
*/5 * * * * flock -xn /tmp/stargate.lock -c '/usr/local/qcloud/stargate/admin/start.sh ...'   # 腾讯云自带
*/5 * * * * /opt/research-reports/venv/bin/python /opt/research-reports/deploy/healthcheck.py >> /var/log/research-reports/healthcheck.log 2>&1
15 3 * * *  /bin/bash /opt/research-reports/deploy/backup.sh >> /var/log/research-reports/backup.log 2>&1
20 3 * * * /opt/research-reports/deploy/offsite-backup.py >> /var/log/research-reports/offsite.log 2>&1
```

| 任务 | 频率 | 作用 |
|:---|:---|:---|
| 服务监测 | 每 5 分钟 | 三层探活，异常通知（同一故障只发一次，恢复再发一次） |
| 本地备份 | 每天 03:15 | SQLite + Redis → `/opt/research-reports-backups/data/`，保留 14 天 |
| **异地备份** | 每天 03:20 | 打包加密后**邮件外发**（见下） |

> ⚠️ `crontab -l` 检查时注意：**root 与 ubuntu 名下不要各有一份** —— 会造成所有任务双跑。正确状态是**只有 root 有**、ubuntu 显示 `no crontab for ubuntu`。

### 备份策略

| 项 | 做法 | 去向 | 保留 |
|:---|:---|:---|:---|
| SQLite | `sqlite3 .backup`（在线一致快照，**不是裸 `cp`**） | 本机 `/opt/research-reports-backups/data/` | 14 天 |
| Redis | `BGSAVE` 后拷 `dump.rdb` | 同上 | 14 天 |
| **异地副本** | tar.gz → **AES-256-CBC / PBKDF2-200000 加密** → 邮件外发 | **163 邮箱** | 由邮箱侧决定 |

> ⚠️ **必须知道的三件事：**
> 1. **本机备份 ≠ 容灾**。备份落在同一块盘上，误删能救、机器挂了救不了 —— 所以才有 03:20 的异地外发。
> 2. **Redis 备份是"锦上添花"，SQLite 才是命脉**：`content_json` 是唯一事实源，Redis 丢了可从 SQLite 重建。
> 3. **系统必须装 `sqlite3`**（`apt-get install -y sqlite3`）。缺它时 `backup.sh` 会因 `set -euo pipefail` 直接中止，**连 Redis 那步也跑不到**，而日志里只有一行 `sqlite3: command not found` —— 极易被忽略。

**异地备份的加密口令**：`deploy/.backup-pass`（600 root）。按既定取舍，口令**随邮件正文一并发出** —— 代价是这层加密**防不住「邮箱账号被攻破」**，只防"邮件被误转发／备份文件单独泄露"。

> 🔴 **`deploy/.backup-pass` 的重要性和真实风险边界（2026-09-30 修正）**
>
> - 它**不在版本库里**（正确，密钥不该进 git），`deploy.sh` 也**不会**重新生成它
> - ⚠️ **原文此处曾写「口令一丢 → 邮箱里所有加密备份永远解不开」，与实际实现不符。**
>   因为口令**随邮件正文一并发出**（见上一段），服务器上这份丢了**并不致命** ——
>   从邮箱里就能把口令取回来。2026-09-30 演练已实测：**邮件正文口令与服务器口令逐字一致**。
> - **因此真正的单点风险是「邮箱账号本身」**：账号被攻破 = 备份与口令**同时**失守。
> - 最坏情况应修正为：**服务器损毁 + 本机硬盘坏 + 邮箱账号不可达 = 数据全丢**
>
> **要求（不变）**：仍建议做至少一份「仓库外 + 机器外」的离线口令备份
> （密码管理器 / 另一台设备 / 纸质均可），以便**完全不依赖邮箱**也能解密历史备份。
> ⚠️ 只把它放在本机的另一个目录里，**等于没备份** —— 那和服务器在同一个故障域里。

**日常巡检**：

```bash
ls -lh /opt/research-reports-backups/data/        # 本地备份产物
tail -20 /var/log/research-reports/backup.log     # 本地备份日志（第一排查入口）
tail -20 /var/log/research-reports/offsite.log    # 异地外发日志
```

### 回滚

```bash
sudo bash /opt/research-reports/deploy/rollback.sh --list   # 看有哪些备份
sudo bash /opt/research-reports/deploy/rollback.sh          # 回滚到最近一次
```

**回滚只动代码，不动数据**（`data/` 和 `app.env` 被显式排除）—— 这是刻意的：代码退回旧版不该丢用户数据。要"连数据一起退"，那叫**恢复备份**，是另一件事。

### 快照

| 快照 ID | 名称 | 用途 |
|:---|:---|:---|
| `lhsnap-iqi7kv5z` | `before-full-rebuild-drill-20260930` | **③ 级重建演练前的完整状态**（2026-09-30 创建） |
| `lhsnap-0quciukp` | `before-rebuild-drill-20260917` | ② 级重建演练前（2026-09-17 创建） |

> ⚠️ **快照配额上限 = 2 个/实例**（`describe_general_resource_quotas` → `SNAPSHOT total=2`）。
> 打新快照前必须先删旧的。
>
> 🔴 **踩坑（2026-09-30 实测）**：控制台重装系统时的「**备份后重装**」选项会在重装前先建系统盘快照 ——
> **配额已满时，该选项会导致整笔重装请求失败**（表现为"点了没反应"）。已有快照时应**取消勾选**。
>
> ⚠️ **快照 ≠ 备份**：快照治"整机被搞坏了"，**防不住误删单个文件**。

---

## 四、对外开放计划（尚未执行）

**当前状态**：Caddy 已在跑 HTTPS，但用的是 **Caddy 内置 CA 自签证书**（浏览器会警告），且**云防火墙未放行 80/443**，所以实际外网不可达。

**目标**：换成 **Let's Encrypt 的 IP 地址证书** —— LE 自 **2026-01** 起 GA 支持 IP 证书，**无需域名、无需 ICP 备案**，浏览器不再警告。

配置已备好：**`deploy/Caddyfile.le-ip`**（当前生效版是 `deploy/Caddyfile`）。

> ⚠️ **顺序不可颠倒**：`reload caddy` 会**立刻发起 ACME 申请**。若 80 尚未放行，HTTP-01 验证必然失败并进入退避重试。
> **正确顺序：先放行 80 + 443 → 再部署 Caddyfile.le-ip → reload → 盯日志。**

> 🔴 **最大单点风险**：IP 证书**只支持 HTTP-01**（不支持 TLS-ALPN-01、不支持 DNS-01），有效期仅 **6 天**，自动续期全靠 **80 长期可达**。
> **80 一关，6 天后证书过期，全站 HTTPS 失效。**

放行端口这一步**可以走 Lighthouse MCP**（`create_firewall_rules` / `delete_firewall_rules` /
`describe_firewall_rules`），**不必手动点控制台** —— 2026-09-17 实测（当晚放行 80/443 即由 MCP 完成）。
> 📌 本文档早期版本写的「放行端口只能在控制台点」**有误**，已更正。

---

## 五、Worker（本机生成端）

本机 worker 通过 `SERVER_URL` 调用站点的 `/api/worker/*`，并带 `X-Worker-Token` 头。

```ini
# worker/worker.env（含令牌，已被 .gitignore 排除）
SERVER_URL=http://127.0.0.1:8080
WORKER_TOKEN=<与服务器 app.env 中的值一致>
```

> ⚠️ **`SERVER_URL` 目前是「本机联调」值**（模板注释原文：「本机开发用 127.0.0.1；部署后换成公网地址」）。
> **若要驱动线上站点，应改为 `http://203.0.113.10`**（经 Caddy 走 80/443，无需额外开 8080）。
> 保持 `127.0.0.1:8080` 的前提是：**本机自己跑着一个后端**（本地 `backend/app.env` 里 `APP_PORT=8080`）。

> ⚠️ **反直觉点**：`WORKER_TOKEN` 是「服务器信任本机」的凭据，不是"本机信任服务器"的。
> 它必须和**服务器上**的值一致，而不是本机随便设一个 —— 否则任何人都能拉走任务队列。

```bash
# 从服务器取值
ssh root@203.0.113.10 "grep WORKER_TOKEN /opt/research-reports/backend/app.env"
```

---

## 六、排障速查

| 症状 | 最可能的原因 | 怎么办 |
|:---|:---|:---|
| 外网打不开、服务器内 `curl` 正常 | **80/443 没在云防火墙放行** | 控制台加 TCP 规则 |
| 浏览器报证书不受信任 | 当前是**自签证书**（预期状态） | 见第四节，换 LE IP 证书 |
| `/api/*` 返回 502 | 后端没起来 / 端口不对（应为 8081） | `systemctl status research-reports` |
| 服务起不来 | `app.env` 缺字段 / 依赖没装全 | `journalctl -u research-reports -n 50` |
| `/health` 返回 503 | Redis 或 SQLite 不可用 | 看返回体里哪个是 `fail`；Redis 挂了不影响浏览 |
| 页面白屏、资源 404 | 前端 `dist/` 没传上来 | 检查 `/opt/research-reports/frontend/dist/index.html` |
| 刷新详情页 404 | Caddyfile 少了 SPA 回退 | 确认有 `try_files {path} /index.html` |
| **`/docs` 返回 200 而非 404** | Caddy 少了 `/docs*` 透传 → 被 SPA 回退吞掉 | 确认有 `handle /docs*` 等三条 |
| `/api/config-check` 能匿名访问 | 鉴权改动未生效 | 应返回 **401** |
| Worker 拉不到任务 | `WORKER_TOKEN` 不一致 / `SERVER_URL` 仍指本机 | 见第五节 |
| **备份目录是空的** | **`sqlite3` 没装** | `apt-get install -y sqlite3`，再手动跑一次 `backup.sh` |
| 邮箱收不到备份 | SMTP 授权码错 / 配置文件字段缺 | 看 `offsite.log`，它会明确报「配置缺失：×××」 |
| 定时任务跑了两遍 | root 与 ubuntu 名下各有一份 crontab | `su ubuntu -c 'crontab -l'`，有就 `crontab -r` |

**通用起手式：**

```bash
systemctl status research-reports
journalctl -u research-reports -n 50 --no-pager
curl -s http://127.0.0.1:8081/api/config-check | python3 -m json.tool
```

---

## 七、文件清单

| 文件 | 作用 | 何时用 |
|:---|:---|:---|
| `README.md` | 就是这份手册 | 改动前读一遍 |
| `deploy.sh` | 一键部署 / 重建（幂等） | 首次部署、代码更新后 |
| `pack.sh` | 本机打包前端 + 代码 | 每次上传前 |
| `rollback.sh` | 回滚到上一版代码 | 上线出问题时 |
| `backup.sh` | 备份 SQLite + Redis（本机） | crontab，每天 03:15 |
| `offsite-backup.py` | 打包加密 + 邮件外发（异地） | crontab，每天 03:20 |
| `backup-mail.env` | 异地备份的 SMTP 与收件人配置 | 600 root，**勿提交** |
| `.backup-pass` | 异地备份的加密口令 | 600 root，**勿提交** |
| `healthcheck.py` | 服务存活监测（站点是否活着） | crontab，每 5 分钟 |
| `alert.py` | 主动告警（备份新鲜度 / 磁盘 / 内存 / 负载） | crontab，每 6 小时；可 `--dry-run` 自检 |
| `alert-run.sh` | `alert.py` 的包装脚本（承载日志重定向） | 由 crontab 调用 |
| `ssh-harden.sh` | **SSH 加固**（双位置同改 + 自锁保护） | 由 deploy.sh 步骤 6.5 调用；不带 `--apply` 即体检 |
| `research-reports.service` | systemd 单元 | 由 deploy.sh 安装 |
| `Caddyfile` | 反代 + 静态托管（**当前生效版 = 自签**） | 由 deploy.sh 安装 |
| `Caddyfile.le-ip` | **对外开放日待部署**的 LE 版 | 见第四节 |
| `fail2ban-caddy-filter.conf` | fail2ban 过滤器（读 Caddy 日志） | 装到 `/etc/fail2ban/filter.d/` |
| `fail2ban-caddy-jail.local` | fail2ban jail 配置 | 装到 `/etc/fail2ban/jail.d/` |

---

## 八、变更记录

### 2026-09-30 —— ③ 级全清空重建演练（控制台重装系统盘）
- **演练升级到 ③ 级**：控制台「重装系统」清空系统盘并换镜像（`lhbp-c6e7uu0g` → 实际变为 `lhbp-7wrz1nyu`）。
  实测 **RTO：端到端 18 分 5 秒 / 纯执行 8 分 7 秒 / 脚本部分 73 秒**；数据逐表一致、4 服务全 `active + enabled`、SSH 加固自动恢复
- **开工前先验证复原链**（本次最大改进）：163 IMAP 取回 → 邮件口令与本机 `.backup-pass` 比对 → openssl 解密 → 解包 → `integrity_check=ok` → 与线上逐表比对，确认"邮箱里那份就是最新状态"后才动手
- 🔴 **修复 `pack.sh` 行尾校验的全量误报**：`grep -l $'\r'` 把 CR 字节**当命令行参数**传递，Windows/Git Bash 会吞掉 0x0D → grep 收到**空模式** → 匹配一切 → 42 个**实测纯 LF** 的文件被判"含 CR"，打包被自己的防御拦死。
  同类失效方案：`awk '/\r$/'`、`grep -P '\r$'`（Windows 文本模式会吃掉 CR，**连真 CRLF 都测不出**，属静默漏报）。
  → 改用**字节级**比对 `cmp -s "$f" <(tr -d '\r' < "$f")`，不受参数传递与文本模式影响
  > 📌 教训：**防御逻辑本身也需要被验证**。一个永远说"不合格"的检查，和一个永远说"合格"的检查，危害是等价的。
- **`deploy.sh` 收尾由「三件事」扩为「四件事」**：新增「立刻补跑 `backup.sh`」—— 全新系统备份目录为空时，首次 `alert.py` 必报「备份目录里找不到任何 `app-*.db.gz`」（属正确告警，但易被误判为故障）
- ✅ **TAT 在重装后依然可用** —— 关闭了 9/17 遗留的"③ 级最大未知数（agent 是否随 OS 消失）"
- ⚠️ **MCP 硬拦 `.ssh` 路径**：含 `/root/.ssh`、`/home/ubuntu/.ssh` 的 TAT 命令稳定返回 `AccessDeny` → 重装后「写回 SSH 公钥」**必须人工执行**（VNC 粘贴；命令见 `docs/rebuild-drill-20260930.md` 与工作区 `drill-20260928/restore-pubkey.txt`）
- **`known_hosts` 处理**：重装会更换主机密钥 → 本机 `ssh` 报 `REMOTE HOST IDENTIFICATION HAS CHANGED` → 执行 `ssh-keygen -R 203.0.113.10` 后重连
- **重启自启已实测**：`reboot` 后 4 个服务无人工干预自动全部拉起，`/health`、Caddy HTTPS、`/api/reports` 均 200 —— 补上 9/17 漏测项，同时关闭 9/28 暴露的"服务未设自启"缺陷
- **镜像差异对照**（旧 → 新）：caddy 手动装 → **预装且已在跑**；redis-server、sqlite3、fail2ban 已装 → **未装**；cron 5 条 → 1 条。`deploy.sh` 幂等覆盖后结果仍正确 ✅
- 完整报告见 `docs/rebuild-drill-20260930.md`

### 2026-09-17
- **redis-demo 下线**：删除 Caddy 的 `/demo/*` 反代（前后端零引用，已核实）；服务侧同步加固 —— 绑 `127.0.0.1`、改为 `redisapp` 用户运行、`/login` 加限流（5 次/300 秒，超限封禁 900 秒）
- **修复备份从未成功**：根因是服务器**未装 `sqlite3`**，`backup.sh` 连续两天静默失败。装上后补跑成功（`db.gz` 18K + `rdb` 36K，`integrity_check` = ok）
- **新增异地备份**：`offsite-backup.py` — AES-256-CBC 加密后邮件外发，每日 03:20；加密可逆性已验证
- **`/docs` 类接口默认关闭**：`ENABLE_DOCS` 默认 `false`（fail-closed）
- **`app.py` 版本归一**：合并仓库新版（`/hash`、前缀白名单）与本次加固，三远程 + 服务器四处一致
- **修复 `/api/config-check` 鉴权回归**：9/16 的修复只改在服务器上、**未进版本库** → 一次重新部署即被覆盖回匿名可访问（实测泄露完整配置）。已落库复测 401
  > 📌 这条与下面的 SSH 加固是**同一个模式**：**加固只落在机器上，等于没有。**
- **`APP_PASSWORD` 轮换**：原为弱口令，已换 24 位随机值
- **对外开放并收回**：放行 80/443 → 部署 `Caddyfile.le-ip` → LE 签发真实 IP 证书、全部验收通过 → 业务验证完成后**按需收回端口**（回到 22 + ICMP）
- **修复 `pack.sh` 密钥泄漏**：原先未排除 `deploy/.backup-pass` 与 `deploy/backup-mail.env`，且凭证检查正则也抓不到它们（只匹配名叫 `.env` 的）→ **每次打包都会把加密口令与邮箱授权码装进产物**
- **② 级重建演练（在生产机上做，成功）**：停服务 + 删站点目录 + 卸载系统包 → 从零重建
  - **实测 RTO：8 分 48 秒**（含 2 次排障）／**纯执行约 2–3 分钟** —— 远优于目标（≤1h / ≤2h）
  - 抓到两个**只在真实重建时才暴露**的致命缺陷并修复：
    - ① **CRLF 行尾** —— Windows 工作区被 `core.autocrlf` 转 CRLF，而 `pack.sh` 是从工作区打包 → Linux 上 `set -euo pipefail` 变成 `set -euo pipefail\r`，**脚本第一行就挂**
      （治愈：新增 `.gitattributes`；`pack.sh` 加「归一 **+ 校验**」步骤）
    - ② **`gpg --dearmor` 在无 tty 环境失败** —— keyring 已存在时询问是否覆盖，TAT/cron 无控制终端
      （治愈：`--batch --yes`；原脚本**自称幂等，但第二次运行必挂**）
  - 完整报告见 `docs/rebuild-drill-20260917.md`
- **SSH 加固纳入可重建资产**：新增 `ssh-harden.sh` 并接入 `deploy.sh` **步骤 6.5**
  > 此前 SSH 加固只存在于机器上、仓库里只有文字描述 → **从零重建会退回 Ubuntu 默认（允许密码登录）**，且重建者不会收到提示。本项修复后 L5「可交付」才真正闭环。

### 2026-09-16
- **SSH 加固**：关闭密码登录、限制 root 为密钥登录、`MaxAuthTries 3`
- **P0 收尾**：`/api/config-check` 加鉴权（→401）、删除含明文密码的 `test_auth.py`、`app.db` 权限收紧为 600
- 建立对外开放前的基线快照

### 2026-09-15
- 首次部署上线：Caddy + uvicorn（8081）+ SQLite + Redis；Caddy 改为 IP 直访 HTTPS（自签）

---

## 九、一句话

> **形态是「Caddy 托管静态前端 + 反代 `/api` 到本机 uvicorn:8081」，脚本幂等可重跑，覆盖前自动备份，回滚只动代码不动数据。**
>
> **备份有三层：本机（每天 03:15）→ 异地邮箱（每天 03:20，加密）→ 整机快照（手动）。**
>
> **对外开放只差一步：在控制台放行 80 + 443，然后部署 `Caddyfile.le-ip`。**
