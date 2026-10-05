# 部署入门

> 这份是**通用部署指南**：讲清原理与步骤，不含任何真实服务器地址、实例 ID 或凭证。
> 把它当成「怎么把本项目部署到你自己的一台 Linux 服务器上」的路线图。

---

## 零、先理解目标形态

```
访客浏览器
    ↓  https://<你的站点>      ← 80 端口只做 301 跳转
┌────────────────────────────────────────┐
│  Caddy      反向代理 + 静态文件托管      │
│    /api/*  →  127.0.0.1:8081           │
│    其他     →  frontend/dist/ 静态      │
└────────────────────────────────────────┘
                    ↓
      FastAPI（uvicorn，只监听回环 127.0.0.1）
                    ↓
      SQLite（唯一事实源） + Redis（限流计数）
```

**三条设计原则**（理解了这三条，后面的步骤都是自然推论）：

| 原则 | 含义 | 直接后果 |
|:---|:---|:---|
| **后端不直接暴露** | uvicorn 只绑 `127.0.0.1` | 所有人都必须经 Caddy，安全策略只需配一处 |
| **后端只跑 1 个 worker** | SQLite 是单写者 | 多进程无收益，反而有锁竞争 |
| **前端是纯静态** | Vite 构建产物 | 不必再起 Node 服务，省内存（小机器很关键） |

---

## 一、前置准备

| 项 | 要求 | 说明 |
|:---|:---|:---|
| 服务器 | Ubuntu 22.04 / 24.04，≥2C2G | 1G 内存够跑，但要开 swap |
| 系统包 | `python3-venv` `sqlite3` `redis-server` `caddy` `fail2ban` | ⚠️ **`sqlite3` 极易漏装**，见下文「常见坑」 |
| SSH | **只用密钥登录** | 关掉密码登录，见第四节 |
| 代码 | 从你的仓库克隆或打包上传 | `deploy/pack.sh` 可生成干净的上传包 |

> 📌 **本项目的 `deploy.sh` 会幂等安装上述系统包**，所以不必手动 `apt install`。
> 但请确认你的机器能访问 apt 源。

---

## 二、部署步骤

### 1. 把代码放到服务器

推荐放 `/opt/<你的项目名>/`。两种方式：

**方式 A：打包上传**（推荐首次部署）

```bash
# 在你自己的机器上
bash deploy/pack.sh
# 产物在 deploy/dist-upload/，它会自动：
#   · 构建前端
#   · 把所有脚本行尾归一为 LF
#   · 排除 *.env / *.pass 等凭证文件（并校验）
scp deploy/dist-upload/<包名>.tar.gz <用户>@<服务器>:/tmp/
```

**方式 B：从 git 仓库克隆**

```bash
git clone <你的仓库地址> /opt/research-reports
```

> ⚠️ 无论哪种方式，**仓库里不含 `app.env`** —— 密钥必须单独配置（下一步）。

### 2. 执行部署脚本

```bash
cd /opt/research-reports
sudo bash deploy/deploy.sh
```

`deploy.sh` 是**幂等**的（可重复执行），它按顺序做 9 件事：

```
1-3  环境检查 / 安装系统依赖 / 创建 venv 与 Python 依赖
4    初始化数据目录与数据库（建表 + 灌入 reports/ 里的研报）
5    安装 systemd 服务（含 UMask=0077 收紧文件权限）
6    安装 Caddy 配置
6.5  安装并启用 fail2ban（含 SSH 加固）
7-9  启服务 / 装定时任务 / 打印验收指引
```

### 3. 配置密钥（首次必须）

```bash
# 生成配置
sudo cp /opt/research-reports/backend/app.env.example \
        /opt/research-reports/backend/app.env
sudo chmod 600 /opt/research-reports/backend/app.env
sudo nano /opt/research-reports/backend/app.env
```

**必须填的两项**：

```bash
# 令牌签名密钥（不填服务起不来）
JWT_SECRET=<openssl rand -hex 32 的输出>

# Worker 拉取任务用的凭据（服务器信任本机）
WORKER_TOKEN=<openssl rand -base64 32 的输出>
```

> ⚠️ `WORKER_TOKEN` 要**同时写进本机 `worker/worker.env`**，两边必须一致。
> 它是「服务器信任本机」的凭据，不是本机信任服务器的 —— 填错就等于谁都能拉走任务队列。

### 4. 验证

```bash
systemctl status research-reports        # 应为 active
curl -s http://127.0.0.1:8081/health     # 应为 {"status":"ok",...}
```

---

## 三、对外开放（HTTPS + 防火墙）

⚠️ **这一步最容易踩坑，请按顺序做。**

### 3.1 先选证书方案

| 方案 | 适用 | 代价 |
|:---|:---|:---|
| **有域名 + Let's Encrypt** | 面向公众 | 需要域名；国内还需 **ICP 备案**（行政流程，1–3 周） |
| **纯 IP + 自签证书** | 自己/小范围用 | 浏览器会显示"不安全"警告；需把根证书发给使用者导入 |
| **纯 IP + LE IP 证书** | 想免警告又不想备案 | ⚠️ 有效期仅 **6 天**，且**只支持 HTTP-01**（全靠 80 口可达续期）——**80 一关，6 天后证书失效** |

### 3.2 放行防火墙

在云服务商控制台放行 **80 + 443**。

> ⚠️ **必须同时放行 443** —— 80 只做 301 跳转、不提供内容，只开 80 等于站点打不开。
>
> ⚠️ **顺序不能颠倒**：证书申请会立刻发起验证。若 80 未放行，验证必然失败并进入退避重试。
> **正确顺序：先放行端口 → 再 reload Caddy。**

### 3.3 自签证书的两个非直觉必配项

用 `deploy/Caddyfile`（自签版）时，有两项**少一个就失败**：

| 配置 | 不加会怎样 |
|:---|:---|
| `pki { ca local { intermediate_lifetime 8760h } }` | 叶子证书被 CA 的 **7 天**中间证书"静默压缩"到 7 天 |
| `default_sni <你的IP>` | **用 IP 访问时客户端不发 SNI** → TLS 握手**直接失败**（浏览器连不上，不是证书警告） |

```caddyfile
{
    default_sni <你的服务器IP>
    pki { ca local { intermediate_lifetime 8760h } }
}
https://<你的服务器IP> {
    tls { issuer internal { lifetime 8760h } }
    # ... 反代与静态托管规则 ...
}
http://<你的服务器IP> {
    redir https://{host}{uri} permanent
}
```

> ⚠️ **改 CA 配置后必须删掉旧 CA 目录重建**：
> `rm -rf /var/lib/caddy/.local/share/caddy/pki/authorities/local`
> 否则新配置不生效。

---

## 四、安全加固清单

| 层 | 措施 | 验证方式 |
|:---|:---|:---|
| **SSH** | 关闭密码登录、限制 root、`MaxAuthTries 3`、仅密钥 | `ssh -o PubkeyAuthentication=no <用户>@<IP>` 应直接 `Permission denied (publickey)` |
| **云防火墙** | 仅放行 22 + 80 + 443（+ ICMP） | 控制台防火墙页 |
| **fail2ban** | 双 jail：`sshd` + 自定义的 Caddy 日志 jail | `fail2ban-client status <jail名>` |
| **应用限流** | 登录 10/分、注册 5/时、提交任务 20/时… | `backend/app/ratelimit.py` 的 `LIMITS` 字典 |
| **接口文档** | `/docs` `/redoc` `/openapi.json` **默认关闭** | 应返回 **404**（而非 200） |
| **文件权限** | `app.env` 600、`app.db` 600、`data/` 700 | `ls -l`；systemd 里 `UMask=0077` 防再生 |
| **数据库密码** | bcrypt 哈希（非明文、非可逆） | 查库看 `$2b$` 前缀 |

> ⚠️ **两个必须知道的坑**：
>
> **1. 改 sshd 必须双位置同改** —— `/etc/ssh/sshd_config` **和** `/etc/ssh/sshd_config.d/*.conf`
> （后者经 `Include` 加载，会覆盖主配置）。
>
> **2. 重载用 `reload` 不用 `restart`** —— `restart` 会断开你当前正在用的 SSH 连接，
> 一旦新配置有问题就**再也进不来**。
>
> 🔴 **加固后务必确认「至少有一把可用密钥」再关密码登录** —— 否则会把自己永久锁在门外。

---

## 五、备份（三层）

```
本机备份（每日）→ 异地外发（每日，加密）→ 整机快照（手动）
```

| 层 | 脚本 | 作用 | 防什么 |
|:---|:---|:---|:---|
| 本机 | `deploy/backup.sh` | SQLite `.backup` + Redis `BGSAVE` | **误删**能救 |
| 异地 | `deploy/offsite-backup.py` | 打包加密后邮件外发 | 机器挂了能救 |
| 快照 | 云控制台 | 整机磁盘快照 | 系统被搞坏能回滚 |

> ⚠️ **三个反直觉点**：
>
> 1. **本机备份 ≠ 容灾**。备份和源数据在同一块盘上，误删能救、机器挂了救不了 —— 所以才需要异地外发。
> 2. **Redis 备份是锦上添花，SQLite 才是命脉**。`content_json` 是唯一事实源，Redis 丢了可从 SQLite 重建。
> 3. **快照 ≠ 备份**。快照治"整机被搞坏"，**防不住误删单个文件**。

---

## 六、常见坑（都是实际踩过的）

| 症状 | 根因 | 解决 |
|:---|:---|:---|
| **备份目录始终是空的** | **`sqlite3` 没装** | `apt-get install -y sqlite3`，再手动跑一次 `backup.sh` |
| **`deploy.sh` 第一行就报 `set: pipefail: invalid option name`** | 脚本是 **CRLF 行尾**（Windows 上编辑/打包导致） | 服务器上 `sed -i 's/\r$//'` 全部脚本；源头上用 `.gitattributes` 钉住 `eol=lf` |
| **Caddy 显示 `Valid configuration` 但服务起不来** | `caddy validate` 以 root 跑，会以 root 创建日志文件（600），而服务以 caddy 用户运行 → 打不开日志 | `chown -R caddy:caddy /var/log/caddy` |
| **`pack.sh` 报「打包失败（tar 退出码 2）」** | `tar -f` 支持 `host:path` 远程语法，Windows 的 `C:/...` 里 `C` 被当成主机名 | 用相对路径 + `--force-local` |
| **`/docs` 返回 200 而不是 404** | Caddy 少了 `/docs*` 透传 → 请求落到 SPA 回退规则上 | 确认 Caddyfile 有 `handle /docs*` 等三条透传 |
| **刷新详情页 404** | Caddyfile 少了 SPA 回退 | 确认有 `try_files {path} /index.html` |
| **上传"成功"但服务跑的是旧代码** | `scp` 多文件会**平铺**到目标目录（错位） | 逐个指定完整目标路径；`grep -c <新标识> app/xxx.py` 验证 |
| **接口文档开着但访问 404** | 本机 `app.env` 没设 `ENABLE_DOCS=true` | 开发机设 true，生产保持 false |
| **定时任务跑了两遍** | root 与普通用户名下各有一份 crontab | 只保留 root 的那份 |

---

## 七、排障起手式

```bash
# 服务状态
systemctl status research-reports
journalctl -u research-reports -n 50 --no-pager

# 后端健康（绕过 Caddy 直连）
curl -s http://127.0.0.1:8081/health | python3 -m json.tool

# 经 Caddy 的入口
curl -sk https://127.0.0.1/health

# 谁在监听
ss -tlnp | grep -E ':(80|443|8081|6379)'

# 防火墙（云侧需去控制台看）
iptables -S

# fail2ban
fail2ban-client status
```

---

## 八、重建与回滚

| 场景 | 做法 |
|:---|:---|
| **代码回滚** | `sudo bash deploy/rollback.sh --list` 看备份 → `sudo bash deploy/rollback.sh` |
| **数据恢复** | 用备份的 `app-*.db.gz` 覆盖 `backend/data/app.db`（先备份原文件） |
| **整机重建** | 从新系统跑 `deploy.sh` + 恢复数据备份 |

> ℹ️ **回滚只动代码，不动数据** —— 代码退回旧版不该丢用户数据。
> "连数据一起退"叫**恢复备份**，是另一件事。

> ⚠️ **重建前先验证备份可用**：解压 → `PRAGMA integrity_check` → 比对行数。
> 把"备份存在"升级为"**备份可用且是最新的**"，破坏性操作才从赌博变成验证。

---

## 九、一句话

> **形态是「Caddy 托管静态前端 + 反代 `/api` 到回环 uvicorn」，脚本幂等可重跑，覆盖前自动备份，回滚只动代码不动数据。**
>
> **最容易被忽略的三件事：`sqlite3` 没装会让备份静默失败、CRLF 行尾会让脚本第一行就挂、加固后没留可用密钥会把自己锁在门外。**
