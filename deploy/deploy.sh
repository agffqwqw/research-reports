#!/usr/bin/env bash
# ============================================================
# 研报站点 · 一键部署 / 重建脚本
#
# 用途：在一台干净的 Ubuntu 22.04 / 24.04 上从零重建整个站点
# 用法：
#   sudo bash deploy.sh              # 完整部署（幂等，可重复执行）
#   sudo bash deploy.sh --check-only # 只体检，不改任何东西
#
# ⚠️ 执行前必须先把代码同步到服务器（见 deploy/README.md 第 3 节）
#    本脚本假定当前目录就是项目里的 deploy/，代码已位于 /opt/research-reports
# ============================================================
set -euo pipefail

APP_ROOT=/opt/research-reports
BACKEND=${APP_ROOT}/backend
VENV=${APP_ROOT}/venv
ENV_FILE=${BACKEND}/app.env
SERVICE=research-reports
FRONTEND_DIST=${APP_ROOT}/frontend/dist
CADDY_SITE=/etc/caddy/Caddyfile

CHECK_ONLY=0
[ "${1:-}" = "--check-only" ] && CHECK_ONLY=1

BACKUP_ROOT=/opt/research-reports-backups
STAMP="$(date +%Y%m%d-%H%M%S)"

log()  { echo -e "\n\033[1;32m==> $*\033[0m"; }
warn() { echo -e "\033[1;33m!! $*\033[0m"; }
die()  { echo -e "\033[1;31mxx $*\033[0m" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请以 root 运行：sudo bash deploy.sh"

# ============================================================
# 0. 前置检查
# ============================================================
log "0/9 前置检查"

[ -d "$BACKEND" ]      || die "找不到后端目录 $BACKEND —— 请先同步代码（README 第 3 节）"
[ -f "$BACKEND/requirements.txt" ] || die "找不到 requirements.txt"
[ -f "$BACKEND/scripts/init_db.py" ] || die "找不到 scripts/init_db.py"

if [ ! -d "$FRONTEND_DIST" ]; then
    warn "前端产物缺失：$FRONTEND_DIST"
    warn "请在本机执行 `npm run build` 后把 dist/ 一起同步上来（README 第 3 节）"
    warn "本次将跳过前端托管配置，站点会没有页面。"
    HAVE_FRONTEND=0
else
    HAVE_FRONTEND=1
    echo "前端产物：$(du -sh "$FRONTEND_DIST" | cut -f1)"
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
    log "体检模式：只报告，不做任何变更"
    echo "--- 端口监听 ---"
    ss -tlnp 2>/dev/null | grep -E ':(22|80|443|6379|8080|8081)\b' || echo "(无)"
    echo "--- 服务状态 ---"
    for s in redis-server caddy "$SERVICE"; do
        printf '%-18s %s\n' "$s" "$(systemctl is-active "$s" 2>/dev/null || echo not-installed)"
    done
    echo "--- 磁盘 ---"
    df -h / | tail -1
    exit 0
fi

export DEBIAN_FRONTEND=noninteractive

# ============================================================
# 1. 系统依赖
# ============================================================
log "1/9 安装系统依赖（python3-venv / caddy / fail2ban / rsync）"

apt-get update -qq
apt-get install -y -qq python3-venv python3-pip curl rsync

# ---------- Redis ----------
if ! command -v redis-server >/dev/null 2>&1; then
    apt-get install -y -qq redis-server
fi
systemctl enable --now redis-server
redis-cli ping >/dev/null && echo "Redis: OK"

# ---------- Caddy ----------
if ! command -v caddy >/dev/null 2>&1; then
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy
fi

# ---------- fail2ban（SSH 防爆破，替代手工拉黑）----------
if ! command -v fail2ban-client >/dev/null 2>&1; then
    apt-get install -y -qq fail2ban
fi
systemctl enable --now fail2ban

# ============================================================
# 2. Python 虚拟环境与依赖
# ============================================================
log "2/9 构建 Python 虚拟环境"
if [ ! -x "${VENV}/bin/python" ]; then
    python3 -m venv "$VENV"
fi
"${VENV}/bin/pip" install -q --upgrade pip
"${VENV}/bin/pip" install -q -r "$BACKEND/requirements.txt"
echo "已安装："
"${VENV}/bin/pip" list --format=freeze 2>/dev/null | grep -Ei '^(fastapi|uvicorn|redis|PyJWT|bcrypt|itsdangerous|jinja2|python-multipart)' || true

# ============================================================
# 3. 凭证文件
# ============================================================
log "3/9 准备 app.env"
if [ ! -f "$ENV_FILE" ]; then
    if [ -f "${BACKEND}/app.env.example" ]; then
        install -m 600 "${BACKEND}/app.env.example" "$ENV_FILE"
    else
        die "缺少 app.env 且没有 app.env.example，无法生成配置"
    fi
    # 自动填充两个必须随机的值
    SECRET=$(openssl rand -hex 32)
    WOKEN=$(openssl rand -base64 32 | tr -d '/+=' | cut -c1-40)
    sed -i "s|^JWT_SECRET=.*|JWT_SECRET=${SECRET}|" "$ENV_FILE"
    sed -i "s|^WORKER_TOKEN=.*|WORKER_TOKEN=${WOKEN}|" "$ENV_FILE"
    echo "已生成随机 JWT_SECRET 与 WORKER_TOKEN"
    warn "邮件相关（MAIL_USER / MAIL_PASSWORD / MAIL_ENABLED）需你手工填写！"
else
    echo "app.env 已存在，保留原凭证（不覆盖）"
fi
chmod 600 "$ENV_FILE"
chown root:root "$ENV_FILE"
ls -l "$ENV_FILE"

# 配置自查（把问题列出来，不阻断部署）
log "   配置自查"
"${VENV}/bin/python" - <<'PY' || true
import sys
sys.path.insert(0, '/opt/research-reports/backend')
try:
    from app.config import config
    probs = config.check()
    if probs:
        print("  ⚠ 发现问题：")
        for p in probs:
            print("   -", p)
    else:
        print("  ✓ 配置无问题")
except Exception as e:
    print("  自检脚本异常：", type(e).__name__, e)
PY

# ============================================================
# 4. 目录与数据
# ============================================================
log "4/9 初始化数据目录与数据库"
mkdir -p "${BACKEND}/data"
mkdir -p /var/log/research-reports

# 建库（幂等）
(cd "$BACKEND" && "${VENV}/bin/python" scripts/init_db.py)
(cd "$BACKEND" && "${VENV}/bin/python" scripts/init_db.py --check)

# 灌入三篇种子研报（幂等：按 code+period 覆盖）
if [ -d "${APP_ROOT}/reports" ]; then
    (cd "$BACKEND" && "${VENV}/bin/python" scripts/seed_data.py) || \
        warn "种子灌入失败（不影响服务启动，可稍后重跑）"
else
    warn "未找到 ${APP_ROOT}/reports，跳过种子数据"
fi

# ============================================================
# 5. systemd 服务
# ============================================================
log "5/9 安装 systemd 服务"

# ---------- 数据文件权限收紧（幂等，每次部署都执行）----------
# SQLite 默认按 umask 022 创建 app.db → **644，所有用户可读**。
# 库里存着用户邮箱与密码哈希，不该对全员开放。
# systemd 单元的 UMask=0077 只约束**新建**文件，已存在的旧文件必须显式改。
if [ -d "${APP_ROOT}/backend/data" ]; then
    chmod 700 "${APP_ROOT}/backend/data" 2>/dev/null || true
    find "${APP_ROOT}/backend/data" -type f -exec chmod 600 {} + 2>/dev/null || true
    echo "  已收紧 data/ → 目录 700 / 文件 600"
fi

# ---------- 覆盖前先备份当前版本，供 rollback.sh 回退 ----------
# 只在服务已存在（即非首次部署）时备份，避免存下空壳
if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE}.service"; then
    SNAP="${BACKUP_ROOT}/${STAMP}"
    mkdir -p "$SNAP"
    for d in backend frontend; do
        [ -d "${APP_ROOT}/$d" ] && rsync -a --exclude 'data/' --exclude 'app.env' \
            "${APP_ROOT}/$d/" "${SNAP}/$d/"
    done
    [ -f "$CADDY_SITE" ] && cp "$CADDY_SITE" "${SNAP}/Caddyfile"
    echo "已备份当前版本 → $SNAP"
    # 只保留最近 5 份
    ls -1dt "${BACKUP_ROOT}"/*/ 2>/dev/null | tail -n +6 | xargs -r rm -rf
else
    echo "首次部署，无需备份"
fi

install -m 644 "$(dirname "$0")/research-reports.service" \
    "/etc/systemd/system/${SERVICE}.service"
systemctl daemon-reload
systemctl enable --now "$SERVICE"
sleep 3
systemctl is-active "$SERVICE" || { journalctl -u "$SERVICE" -n 30 --no-pager; die "服务启动失败"; }

# ============================================================
# 6. Caddy 反代
# ============================================================
log "6/9 配置 Caddy 反代"
mkdir -p /var/log/caddy
chown caddy:caddy /var/log/caddy

if [ -f "$CADDY_SITE" ] && [ ! -f "${CADDY_SITE}.bak" ]; then
    cp "$CADDY_SITE" "${CADDY_SITE}.bak"
    echo "已备份原配置 → ${CADDY_SITE}.bak"
fi
install -m 644 "$(dirname "$0")/Caddyfile" "$CADDY_SITE"
caddy validate --config "$CADDY_SITE" || die "Caddyfile 校验失败"

# ⚠️ 关键：caddy validate 是**以 root 身份**执行的，而 Caddy 加载配置时会
#    打开日志文件 —— 于是日志文件被创建成 root:root 600。
#    但 caddy.service 是以 caddy 用户运行的，它打不开 root 独占的文件，
#    结果是「validate 通过、服务启动却失败」(permission denied)。
#    必须在这里把属主改回 caddy，否则每次部署都会把 Caddy 弄挂。
mkdir -p /var/log/caddy
chown -R caddy:caddy /var/log/caddy
chmod 755 /var/log/caddy

systemctl enable --now caddy
systemctl reload caddy 2>/dev/null || systemctl restart caddy
sleep 2
systemctl is-active caddy || {
    echo "✗ Caddy 启动失败，最近日志："
    journalctl -u caddy -n 20 --no-pager
    echo
    echo "  提示：若报 permission denied 指向 /var/log/caddy，执行"
    echo "        chown -R caddy:caddy /var/log/caddy && systemctl restart caddy"
    die "Caddy 未能启动（redis-demo 等既有站点也会受影响）"
}

# ============================================================
# 6.5 fail2ban：网络层封禁（读 Caddy 访问日志）
# ============================================================
# 与应用层 slowapi 互补：
#   slowapi  管瞬时爆发（如登录 10 次/分钟），立刻 429
#   fail2ban 管持续试探（如 10 分钟累计 15 次失败），封 1 小时且递增
# 慢速爆破每分钟只试几次、永不触发应用层阈值，只有 fail2ban 能治。
#
# ⚠️ action 限定 multiport dports 80,443 —— **绝不动 SSH 22**，
#    否则一次误封就把管理通道一起切断，只能去控制台救。
install -m 644 "$(dirname "$0")/fail2ban-caddy-filter.conf" \
    /etc/fail2ban/filter.d/caddy-research-reports.conf
install -m 644 "$(dirname "$0")/fail2ban-caddy-jail.local" \
    /etc/fail2ban/jail.d/caddy-research-reports.local

if fail2ban-client -t >/dev/null 2>&1; then
    systemctl restart fail2ban
    sleep 3
    if systemctl is-active --quiet fail2ban; then
        echo "  fail2ban 已加载 caddy-research-reports jail"
        fail2ban-client status 2>/dev/null | grep "Jail list" || true
    else
        echo "  ⚠ fail2ban 未能启动，最近日志："
        journalctl -u fail2ban -n 10 --no-pager || true
    fi
else
    # 校验失败就不重启 —— 宁可沿用旧规则，也不要让防护整个失效
    echo "  ⚠ fail2ban 配置校验失败，保持原状不重启："
    fail2ban-client -t 2>&1 | tail -5
fi

# ============================================================
# 7. 服务监测 + 备份（crontab）
# ============================================================
log "7/9 安装定时任务（服务监测 + 数据备份）"

mkdir -p /var/lib/research-reports /var/log/research-reports
chmod 700 /var/lib/research-reports

# 错误页上写着「已自动邮件通知」—— 靠这个监测脚本让它成立。
# 同一故障只通知一次，恢复时再发一封，避免变成邮件轰炸。
CRON_HEALTH="*/5 * * * * ${VENV}/bin/python ${APP_ROOT}/deploy/healthcheck.py >> /var/log/research-reports/healthcheck.log 2>&1"
CRON_BACKUP="15 3 * * * /bin/bash ${APP_ROOT}/deploy/backup.sh >> /var/log/research-reports/backup.log 2>&1"

CUR_CRON="$(crontab -l 2>/dev/null || true)"
NEW_CRON="$CUR_CRON"
for line in "$CRON_HEALTH" "$CRON_BACKUP"; do
    key="$(echo "$line" | awk '{print $2, $3, $7}')"
    if echo "$CUR_CRON" | grep -qF "$key"; then
        echo "  已存在，跳过: $key"
    else
        NEW_CRON="${NEW_CRON}${NEW_CRON:+
}${line}"
        echo "  新增: $key"
    fi
done
if [ "$NEW_CRON" != "$CUR_CRON" ]; then
    printf '%s\n' "$NEW_CRON" | crontab -
    echo "  crontab 已更新"
fi
crontab -l | sed 's/^/    /'

# 立刻跑一次监测，确认脚本本身能工作
echo "  --- 监测脚本试跑 ---"
"${VENV}/bin/python" "${APP_ROOT}/deploy/healthcheck.py" || \
    echo "  （返回非 0 表示当前就有异常，详情见上方输出）"

# ============================================================
# 8. 本机自检
# ============================================================
log "8/9 自检"
echo "--- 健康检查（后端直连）---"
echo "  说明：/health 返回 503 不一定代表故障 —— 它是「降级」信号，"
echo "        通常表示 Redis 未就绪。浏览功能不受影响（SQLite 是唯一事实源）。"
curl -s --max-time 5 http://127.0.0.1:8081/health || echo "  ✗ /health 无响应"
echo
echo "--- 配置自检（后端直连）---"
echo "  说明：problems 非空代表有配置待补，见下方提示。"
curl -s --max-time 5 http://127.0.0.1:8081/api/config-check || echo "  ✗ /api/config-check 无响应"
echo
echo "--- 经 Caddy 访问 ---"
curl -s -o /dev/null -w "  /api/reports  HTTP %{http_code}\n" --max-time 5 http://127.0.0.1/api/reports
curl -s -o /dev/null -w "  /（首页）     HTTP %{http_code}\n" --max-time 5 http://127.0.0.1/
echo
echo "--- 错误页是否就位 ---"
if [ -f "${APP_ROOT}/frontend/dist/error.html" ]; then
    echo "  ✓ dist/error.html 存在（Caddy 出错时会渲染它）"
else
    echo "  ✗ dist/error.html 缺失 —— 错误页会 404，请确认前端构建产物包含 public/error.html"
fi
echo
echo "--- 公开研报数量 ---"
curl -s --max-time 5 'http://127.0.0.1/api/reports?page_size=50' \
    | "${VENV}/bin/python" -c "import sys,json;d=json.load(sys.stdin);print('  共',d.get('total','?'),'篇')" 2>/dev/null || echo "  (解析失败)"

# ============================================================
# 9. 收尾提示
# ============================================================
log "9/9 完成"
cat <<'EOF'
============================================================
部署完成。

  应用日志   journalctl -u research-reports -f
  Caddy 日志 tail -f /var/log/caddy/research-reports.access.log
  监测日志   tail -f /var/log/research-reports/healthcheck.log
  健康检查   curl http://127.0.0.1:8081/health

  已自动装好的定时任务：
    - 每 5 分钟  服务监测（异常即发邮件给 ADMIN_EMAIL）
    - 每天 03:15 数据备份（SQLite + Redis，保留 14 天）

  ⚠ 还需要你手工做两件事：
    1) 云平台防火墙放行 80 端口（默认只有 22 和 ICMP）
       这是「外网能不能打开」的唯一门槛，脚本改不了云平台。

    2) 填写邮件凭证（否则注册验证邮件、故障告警都发不出去）：
      编辑 /opt/research-reports/backend/app.env
        MAIL_ENABLED=true
        MAIL_USER=你的邮箱@163.com
        MAIL_PASSWORD=SMTP授权码
        MAIL_FROM=你的邮箱@163.com
      改完执行：systemctl restart research-reports
      ⚠ 不填的话，错误页上「已自动邮件通知」这句就是空头承诺。

  回滚：bash rollback.sh        （回退到上一版代码，不动数据）
  备份：bash backup.sh          （已加进 crontab，也可手动跑）
============================================================
EOF
