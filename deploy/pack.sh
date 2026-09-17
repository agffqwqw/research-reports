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

# ---------- 2.5 统一脚本行尾为 LF（防御性，必须做）----------
# 为什么有 .gitattributes 还要做这一步：
#   .gitattributes 只在 git **检出**时生效。若文件被编辑器 / AI 写入 / 跨平台拷贝
#   改写过，工作区仍可能是 CRLF；而本脚本是「从工作区打包」→ CRLF 会原样进 tar。
# 实测后果（2026-09-17 重建演练）：deploy.sh 带 CRLF 到 Linux 后，
#   `set -euo pipefail` 变成 `set -euo pipefail\r`
#   → bash 报 "set: pipefail: invalid option name"，脚本第一行就挂。
# 这里就地归一，并且**校验**归一结果 —— 只做不验等于没做。
echo "==> 统一脚本行尾为 LF"
SHELL_LIKE=( -name '*.sh' -o -name '*.py' -o -name '*.service' \
             -o -name '*.conf' -o -name '*.local' -o -name 'Caddyfile*' )
find backend frontend worker deploy reports -type f \( "${SHELL_LIKE[@]}" \) \
    -exec sed -i 's/\r$//' {} + 2>/dev/null || true
CR_FILES="$(find backend frontend worker deploy reports -type f \( "${SHELL_LIKE[@]}" \) \
    -exec grep -l $'\r' {} + 2>/dev/null || true)"
if [ -n "$CR_FILES" ]; then
    echo "xx 以下文件仍含 CR，拒绝打包：" >&2
    printf '     %s\n' $CR_FILES >&2
    exit 1
fi
echo "    ✓ 无 CR"

# ---------- 3. 打包 ----------
# 只收「要上传的东西」，用白名单，避免 exclude 规则写漏
# 注意三个坑：
#   a) dist-upload 必须排除，否则包会把自己打进去（包中包，越传越大）
#   b) worker.env / app.env 含令牌与密钥，必须排除
#   c) deploy/ 下还有两个**非 .env 结尾**的密钥文件也必须排除：
#        deploy/.backup-pass     异地备份的加密口令
#        deploy/backup-mail.env  SMTP 授权码与收件人
#      2026-09-17 发现：早期版本漏了 c)，凭证检查正则也抓不到（只匹配名叫 .env 的），
#      等于每次打包都会把加密口令和邮箱授权码一起装进产物。
echo "==> 打包"
tar -czf "$OUT" \
    --exclude="$OUT" \
    --exclude='deploy/dist-upload' \
    --exclude='frontend/node_modules' \
    --exclude='backend/data' \
    --exclude='backend/app.env' \
    --exclude='worker/worker.env' \
    --exclude='deploy/.backup-pass' \
    --exclude='deploy/backup-mail.env' \
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
# 宽匹配：任何 *.env / *.pass / *.db / *.rdb 都拦下（example 模板除外）。
# 不要用「精确文件名」列表 —— 那种写法漏一个就等于没查。
if tar -tzf "$OUT" | grep -E '\.(env|pass|db|db-wal|rdb)$' | grep -v example; then
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
