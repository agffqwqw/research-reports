#!/usr/bin/env bash
# ============================================================
# 研报站点 · 回滚脚本
#
# 用途：站点上线后出问题，快速退回到「能用的上一版」
# 用法：sudo bash rollback.sh            # 回滚到最近一次备份
#       sudo bash rollback.sh --list     # 列出所有可回滚的备份
#
# 备份由 deploy.sh 在覆盖前自动创建（首次部署前不会存在）
# ============================================================
set -euo pipefail

APP_ROOT=/opt/research-reports
BACKUP_ROOT=/opt/research-reports-backups
SERVICE=research-reports
CADDY_SITE=/etc/caddy/Caddyfile

log(){ echo -e "\n\033[1;32m==> $*\033[0m"; }
die(){ echo -e "\033[1;31mxx $*\033[0m" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请以 root 运行"

if [ "${1:-}" = "--list" ]; then
    log "可用备份"
    ls -1dt "$BACKUP_ROOT"/*/ 2>/dev/null || echo "(无备份)"
    exit 0
fi

LATEST="$(ls -1dt "$BACKUP_ROOT"/*/ 2>/dev/null | head -1 || true)"
[ -n "$LATEST" ] || die "找不到备份目录 $BACKUP_ROOT —— 没有可回滚的版本"

log "准备回滚到：$LATEST"
read -r -p "确认回滚？当前代码将被覆盖 [y/N] " ans
[ "${ans:-N}" = "y" ] || { echo "已取消"; exit 0; }

log "1/4 停服务"
systemctl stop "$SERVICE" || true

log "2/4 恢复代码与配置"
# 只恢复代码目录，不动 data/（数据必须保留）
if [ -d "${LATEST}backend" ]; then
    rsync -a --delete --exclude 'data/' --exclude 'app.env' \
        "${LATEST}backend/" "${APP_ROOT}/backend/"
fi
if [ -d "${LATEST}frontend" ]; then
    rsync -a --delete "${LATEST}frontend/" "${APP_ROOT}/frontend/"
fi
if [ -f "${LATEST}Caddyfile" ]; then
    install -m 644 "${LATEST}Caddyfile" "$CADDY_SITE"
fi

log "3/4 重装依赖（代码可能带新依赖）"
[ -x "${APP_ROOT}/venv/bin/pip" ] && \
    "${APP_ROOT}/venv/bin/pip" install -q -r "${APP_ROOT}/backend/requirements.txt" || true

log "4/4 重启"
systemctl start "$SERVICE"
sleep 3
systemctl is-active "$SERVICE"
systemctl reload caddy 2>/dev/null || systemctl restart caddy
curl -s --max-time 5 http://127.0.0.1:8081/health || echo "健康检查无响应，请查 journalctl -u $SERVICE -n 50"
echo
echo "回滚完成。"
