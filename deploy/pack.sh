#!/usr/bin/env bash
# ============================================================
# 研报站点 · 打包脚本
#
# 用途：把要上传的文件打成一个 tar.gz，排除依赖与凭证
# 用法：bash pack.sh
# 产出：dist-upload/research-reports-YYYYMMDD-HHMM.tar.gz
#
# ⚠️ 本脚本只打包，不上传。上传命令见 README 第 3 节。
# ============================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

STAMP="$(date +%Y%m%d-%H%M)"
OUT_DIR="$HERE/deploy/dist-upload"
OUT="$OUT_DIR/research-reports-$STAMP.tar.gz"
mkdir -p "$OUT_DIR"

echo "==> 项目根目录: $HERE"

# ---------- 1. 前端构建 ----------
if [ -d frontend ]; then
    echo "==> 构建前端"
    ( cd frontend && npm run build )
    [ -d frontend/dist ] || { echo "xx 前端构建未产出 dist/" >&2; exit 1; }
    echo "    dist 体积: $(du -sh frontend/dist | cut -f1)"
fi

# ---------- 2. 清掉 pycache（避免打包垃圾）----------
find ./backend -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true

# ---------- 3. 打包 ----------
# 只收「要上传的东西」，用白名单，避免 exclude 规则写漏
# 注意两个坑：
#   a) dist-upload 必须排除，否则包会把自己打进去（包中包，越传越大）
#   b) worker.env 必须排除，里面有 WORKER_TOKEN
echo "==> 打包"
tar -czf "$OUT" \
    --exclude="$OUT" \
    --exclude='deploy/dist-upload' \
    --exclude='frontend/node_modules' \
    --exclude='backend/data' \
    --exclude='backend/app.env' \
    --exclude='worker/worker.env' \
    --exclude='__pycache__' \
    backend frontend worker deploy reports 2>/dev/null || {
        echo "xx 打包失败（tar 退出码 $?）" >&2
        exit 1
    }

echo
echo "==> 已生成: $OUT"
echo "    体积: $(du -sh "$OUT" | cut -f1)"
echo
echo "==> 检查包内是否误含凭证（应当为空）"
if tar -tzf "$OUT" | grep -E '(^|/)\.env$|(^|/)worker\.env$|\.db$|\.db-wal$|\.rdb$' | grep -v '\.example$'; then
    echo "    !! 发现疑似凭证文件，请检查后重新打包"
    rm -f "$OUT"
    exit 1
else
    echo "    ✓ 未包含任何凭证文件"
fi
echo
echo "==> 包内依据目录（应含 reports 与 frontend/dist）"
TOC="$(tar -tzf "$OUT")"
for d in backend frontend/dist worker deploy reports; do
    if printf '%s\n' "$TOC" | grep -qE "(^|/)${d%/*}/|^${d}\$"; then
        printf '    ✓ %s\n' "$d"
    else
        printf '    ✗ %s 缺失\n' "$d"
    fi
done
echo
echo "==> 下一步（手工执行，本脚本不会代劳）："
echo "    scp $OUT root@203.0.113.10:/tmp/"
echo "    然后按 deploy/README.md 第 3 节解包部署"
