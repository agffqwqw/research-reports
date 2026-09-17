#!/usr/bin/env bash
# ============================================================
# SSH 加固脚本 · 研报站点
#
# 【为什么需要它】
#   2026-09-16 的 SSH 加固当时**只改在服务器上**，仓库里只有 README 的文字描述。
#   → 从零重建后会退回 Ubuntu 默认：**允许密码登录 + 允许 root 密码登录**，
#     而且重建者不会收到任何提示。
#   这与同期的 `/api/config-check` 鉴权事故是**完全相同**的模式：
#     加固只落在机器上，没进「可交付资产」。
#   2026-09-17 的重建演练把这个缺口暴露了出来。
#
# 【加固内容】
#   PasswordAuthentication no          仅密钥登录
#   PermitRootLogin prohibit-password  root 仅密钥
#   MaxAuthTries 3                     限制单连接认证尝试
#   ClientAliveInterval 300 / CountMax 2  回收僵死会话
#
# 【⚠️ 两个关键安全设计 —— 别删】
#   1) **必须双位置同改**：/etc/ssh/sshd_config 与
#      /etc/ssh/sshd_config.d/50-cloud-init.conf
#      （后者经主配置的 Include 加载，**会覆盖主配置** —— 只改一个等于没改）
#   2) **落盘前先做自锁保护**：若系统上找不到任何 authorized_keys，
#      直接拒绝执行 —— 否则「关掉密码登录 + 没有任何密钥」= **永久把自己锁在门外**。
#      这是本脚本唯一不能妥协的地方。
#
# 【用法】
#   bash ssh-harden.sh           体检（默认，不改任何东西）
#   bash ssh-harden.sh --apply   实际加固
# ============================================================
set -euo pipefail

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

SSHD_MAIN=/etc/ssh/sshd_config
SSHD_EXTRA=/etc/ssh/sshd_config.d/50-cloud-init.conf
WANT_KEYS=(PasswordAuthentication PermitRootLogin MaxAuthTries ClientAliveInterval ClientAliveCountMax)

say() { printf '%s\n' "$*"; }

# ---------------------------------------------------------------- 1. 自锁保护
say "== 1/4 自锁保护检查（有无可用密钥）"
KEY_FOUND=0
for f in /root/.ssh/authorized_keys $(ls -d /home/*/.ssh/authorized_keys 2>/dev/null || true); do
    if [ -s "$f" ]; then
        n="$(grep -cE '^[[:space:]]*(ssh-|ecdsa-|sk-)' "$f" 2>/dev/null || echo 0)"
        say "   $f  ($n 个公钥)"
        [ "$n" -gt 0 ] && KEY_FOUND=1
    fi
done
if [ "$KEY_FOUND" -eq 0 ]; then
    say ""
    say "xx 拒绝执行：系统上找不到任何可用的 SSH 公钥。"
    say "   此时关闭密码登录 = **永久无法登录**（只能去控制台 VNC 救）。"
    say "   请先把公钥写入 /root/.ssh/authorized_keys（或对应用户名下），再重跑本脚本。"
    exit 1
fi
say "   ✓ 至少存在一个可用公钥，可安全关闭密码登录"

# ---------------------------------------------------------------- 2. 写配置
say ""
say "== 2/4 写入配置（双位置同改）"
if [ "$APPLY" -eq 0 ]; then
    say "   [体检模式] 将要修改："
    say "     $SSHD_MAIN"
    say "     $SSHD_EXTRA   （若存在）"
    say "   加固项：${WANT_KEYS[*]}"
    say ""
    say "   当前实际生效值（sshd -T）："
    sshd -T 2>/dev/null | grep -iE '^(passwordauthentication|permitrootlogin|maxauthtries)' | sed 's/^/     /' || true
    say ""
    say "   加 --apply 才会真正写入。"
    exit 0
fi

# 先删掉这些键的所有现存写法（含被注释掉的），避免「后面的覆盖前面的」产生歧义
strip_keys() {
    sed -i -E "/^[[:space:]]*#?[[:space:]]*($(IFS='|'; echo "${WANT_KEYS[*]}"))[[:space:]]+/d" "$1"
}
harden_file() {
    local f="$1"
    [ -f "$f" ] || { say "   (跳过，不存在: $f)"; return 0; }
    cp -a "$f" "$f.bak.$(date +%Y%m%d-%H%M%S)"
    strip_keys "$f"
    {
        printf '\n# ---- 由 deploy/ssh-harden.sh 于 %s 写入 ----\n' "$(date '+%F %T')"
        printf 'PasswordAuthentication no\n'
        printf 'PermitRootLogin prohibit-password\n'
        printf 'MaxAuthTries 3\n'
        printf 'ClientAliveInterval 300\n'
        printf 'ClientAliveCountMax 2\n'
    } >> "$f"
    say "   ✓ 已加固 $f"
}
harden_file "$SSHD_MAIN"
harden_file "$SSHD_EXTRA"

# ---------------------------------------------------------------- 3. 校验后 reload
say ""
say "== 3/4 语法校验"
if ! sshd -t 2>&1; then
    say "xx sshd -t 校验失败 —— **不 reload**，保持原状。"
    say "   已改动前的备份在 *.bak.<时间戳>，可自行回滚。"
    exit 1
fi
say "   ✓ 语法通过"

say ""
say "== 4/4 重载（用 reload，不用 restart）"
say "   为什么是 reload：restart 会断开**当前这条正在用的连接**，"
say "   一旦新配置有问题就再也进不来了；reload 保留既有连接。"
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || {
    say "   !! reload 失败，请手动检查"; exit 1;
}
say "   ✓ 已 reload"

say ""
say "== 生效值复查（sshd -T）=="
sshd -T 2>/dev/null | grep -iE '^(passwordauthentication|permitrootlogin|maxauthtries|clientaliveinterval|clientalivecountmax)' | sed 's/^/   /' || true
say ""
say "提示：从**另一条**连接验证加固生效（别把当前会话关掉再试）："
say "     ssh -o PubkeyAuthentication=no root@<IP>"
say "     期望：**不出现 password: 提示**，直接 Permission denied (publickey)"
