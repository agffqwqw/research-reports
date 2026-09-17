# -*- coding: utf-8 -*-
"""认证安全：密码哈希 + JWT + 图形验证码（B6）

图形验证码采用「签名方案」而非「存储方案」：
    答案被 itsdangerous 签名后放进 token 返回，验证时解密比对。
    这样**无需 Redis / 内存存储**，天然无状态、可横向扩展。
    图形本身用 SVG 输出，无需 Pillow 等图像库。
"""
import random
import string
import time

import bcrypt
import jwt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import config

# ---------- 密码 ----------
# 直接用 bcrypt（不经过 passlib）：passlib 1.7.4 与 bcrypt 4.x 不兼容，
# 后者移除了 passlib 依赖的 bcrypt.__about__ 属性，会抛 ValueError。
_BCRYPT_MAX = 72  # bcrypt 的密码长度上限（字节）


def _to_bcrypt_bytes(plain: str) -> bytes:
    """截断到 72 字节，避免超长密码直接抛异常。"""
    return plain.encode("utf-8")[:_BCRYPT_MAX]


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_to_bcrypt_bytes(plain), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_to_bcrypt_bytes(plain), hashed.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return False


# ---------- JWT ----------
def create_access_token(user_id: int, role: str, email: str) -> str:
    payload = {
        "sub": str(user_id),
        "role": role,
        "email": email,
        "iat": int(time.time()),
        "exp": int(time.time()) + config.JWT_EXPIRE,
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def decode_access_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


# ---------- 图形验证码 ----------
_CAPTCHA_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # 去掉易混淆的 0/O/1/I
_captcha_signer = URLSafeTimedSerializer(config.JWT_SECRET, salt="captcha")


def gen_captcha() -> tuple[str, str]:
    """生成验证码，返回 (token, svg)。token 提交回服务端校验，答案不落库。"""
    answer = "".join(random.choices(_CAPTCHA_CHARS, k=4))
    token = _captcha_signer.dumps(answer.lower())
    return token, _render_svg(answer)


def verify_captcha(token: str, user_input: str) -> bool:
    if not token or not user_input:
        return False
    try:
        answer = _captcha_signer.loads(token, max_age=300)  # 5 分钟有效
    except (BadSignature, SignatureExpired):
        return False
    return str(answer).lower() == str(user_input).strip().lower()


def _render_svg(answer: str) -> str:
    """把 4 位字符画成带干扰线的 SVG。"""
    colors = ("#185fa5", "#3b6d11", "#854f0b", "#a32d2d", "#6b21a8")
    parts = []
    x = 12
    for i, ch in enumerate(answer):
        y = random.randint(24, 38)
        rot = random.randint(-18, 18)
        color = colors[i % len(colors)]
        parts.append(
            f'<text x="{x}" y="{y}" font-size="30" font-family="Consolas,monospace" '
            f'fill="{color}" transform="rotate({rot} {x + 12} {y})">{ch}</text>'
        )
        x += 32
    # 三条干扰线
    lines = []
    for _ in range(3):
        x1, y1 = random.randint(0, 40), random.randint(0, 48)
        x2, y2 = random.randint(90, 136), random.randint(0, 48)
        lines.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="#cbd5e1" stroke-width="1"/>'
        )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="136" height="48" '
        'viewBox="0 0 136 48" style="background:#f7f8fa">'
        + "".join(lines) + "".join(parts) + "</svg>"
    )
