#!/usr/bin/env bash
# ============================================================
# 研报站点 · 数据备份脚本（SQLite + Redis）
#
# 放置路径：/opt/research-reports/deploy/backup.sh
# 定时执行：加入 root 的 crontab
#   15 3 * * *  /bin/bash /opt/research-reports/deploy/backup.sh >> /var/log/research-reports/backup.log 2>&1
#
# 备份内容：
#   1) SQLite：用官方 .backup 命令（在线安全备份，WAL 模式下也能用）
#   2) Redis ：BGSAVE 后拷走 dump.rdb（redis-demo 与本站在同一个实例）
#
# ⚠️ 备份文件默认留在本机 /opt/research-reports-backups/data/
#    它只防「误删」，不防「整机损毁」。要防后者需异地/对象存储，见 README。
# ============================================================
set -euo pipefail

APP_ROOT=/opt/research-reports
BACKUP_DIR=/opt/research-reports-backups/data
DB="${APP_ROOT}/backend/data/app.db"
KEEP_DAYS=14
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP_DIR"
# 备份里含数据库完整副本（用户邮箱 + 密码哈希），不该对全员可读。
# umask 默认 022 → 目录 755、文件 644，这里显式收紧。
chmod 700 "$BACKUP_DIR"

echo "[$(date '+%F %T')] 开始备份"

# ---------- 1. SQLite ----------
if [ -f "$DB" ]; then
    # .backup 是 SQLite 原生命令：在线一致快照，不锁库、不受 WAL 影响
    sqlite3 "$DB" ".backup '${BACKUP_DIR}/app-${STAMP}.db'"
    gzip -f "${BACKUP_DIR}/app-${STAMP}.db"
    echo "  SQLite → app-${STAMP}.db.gz ($(du -h "${BACKUP_DIR}/app-${STAMP}.db.gz" | cut -f1))"
else
    echo "  !! SQLite 不存在：$DB"
fi

# ---------- 2. Redis ----------
if command -v redis-cli >/dev/null 2>&1 && redis-cli ping >/dev/null 2>&1; then
    redis-cli BGSAVE >/dev/null
    # 等 BGSAVE 落盘（最多 10 秒）
    for _ in $(seq 1 10); do
        IN_PROGRESS="$(redis-cli info persistence | grep -o 'rdb_bgsave_in_progress:[01]' | cut -d: -f2)"
        [ "$IN_PROGRESS" = "0" ] && break
        sleep 1
    done
    RDB="$(redis-cli config get dir | tail -1)/dump.rdb"
    if [ -f "$RDB" ]; then
        cp "$RDB" "${BACKUP_DIR}/dump-${STAMP}.rdb"
        echo "  Redis  → dump-${STAMP}.rdb ($(du -h "${BACKUP_DIR}/dump-${STAMP}.rdb" | cut -f1))"
    else
        echo "  !! 找不到 $RDB"
    fi
else
    echo "  -- Redis 不可用，跳过"
fi

# ---------- 3. 收紧备份文件权限 ----------
# 备份是数据库完整副本，与源库同等敏感（用户邮箱 + 密码哈希），
# 统一收成 600/700，避免被同机其他账户读取。
chmod 700 "$BACKUP_DIR" 2>/dev/null || true
find "$BACKUP_DIR" -type f -exec chmod 600 {} + 2>/dev/null || true
echo "  权限已收紧：目录 700 / 文件 600"

# ---------- 4. 清理过期 ----------
DELETED="$(find "$BACKUP_DIR" -type f \( -name 'app-*.db.gz' -o -name 'dump-*.rdb' \) \
    -mtime +${KEEP_DAYS} -print -delete | wc -l)"
[ "$DELETED" -gt 0 ] && echo "  清理 ${KEEP_DAYS} 天前的备份：${DELETED} 个"

echo "  当前备份共 $(ls -1 "$BACKUP_DIR" | wc -l) 份，占用 $(du -sh "$BACKUP_DIR" | cut -f1)"
echo "[$(date '+%F %T')] 完成"
