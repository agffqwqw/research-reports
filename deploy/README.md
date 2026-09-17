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

> ⚠️ **两个必知的坑**：
> 1. **改 sshd 必须双位置同改** —— `/etc/ssh/sshd_config` **和** `/etc/ssh/sshd_config.d/50-cloud-init.conf`（后者经 `Include` 加载会覆盖主配置）。
> 2. **关闭密码登录会让 OrcaTerm 的「终端连接(SSH)+密码」失效** —— 改用「免密连接(TAT)」或 VNC，或直接本机 `ssh root@...`。

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
| `lhsnap-g3jo4j4z` | `pre-public-baseline-20260916` | **对外开放前基线** |
| `lhsnap-6rb1k91j` | `before-caddy-fastapi-20260910` | 部署 Caddy+FastAPI 之前 |

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

放行端口这一步**只能在腾讯云控制台点**，脚本改不了云平台。

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
| `healthcheck.py` | 服务存活监测 | crontab，每 5 分钟 |
| `research-reports.service` | systemd 单元 | 由 deploy.sh 安装 |
| `Caddyfile` | 反代 + 静态托管（**当前生效版 = 自签**） | 由 deploy.sh 安装 |
| `Caddyfile.le-ip` | **对外开放日待部署**的 LE 版 | 见第四节 |
| `fail2ban-caddy-filter.conf` | fail2ban 过滤器（读 Caddy 日志） | 装到 `/etc/fail2ban/filter.d/` |
| `fail2ban-caddy-jail.local` | fail2ban jail 配置 | 装到 `/etc/fail2ban/jail.d/` |

---

## 八、变更记录

### 2026-09-17
- **redis-demo 下线**：删除 Caddy 的 `/demo/*` 反代（前后端零引用，已核实）；服务侧同步加固 —— 绑 `127.0.0.1`、改为 `redisapp` 用户运行、`/login` 加限流（5 次/300 秒，超限封禁 900 秒）
- **修复备份从未成功**：根因是服务器**未装 `sqlite3`**，`backup.sh` 连续两天静默失败。装上后补跑成功（`db.gz` 18K + `rdb` 36K，`integrity_check` = ok）
- **新增异地备份**：`offsite-backup.py` — AES-256-CBC 加密后邮件外发，每日 03:20；加密可逆性已验证
- **`/docs` 类接口默认关闭**：`ENABLE_DOCS` 默认 `false`（fail-closed）
- **`app.py` 版本归一**：合并仓库新版（`/hash`、前缀白名单）与本次加固，三远程 + 服务器四处一致

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
