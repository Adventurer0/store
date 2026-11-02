import hashlib
import hmac
import json
import os
import re
import secrets
from cryptography.fernet import Fernet
from fastapi import HTTPException, Request
from . import db


def cipher():
    path = db.DATA / "master.key"
    if not path.exists():
        # Never silently replace a lost key when encrypted stores already exist.
        if db.one("SELECT count(*) n FROM stores")["n"]:
            raise RuntimeError("master.key 丢失，请从备份恢复密钥；不能重新生成")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        except FileExistsError:
            pass
    return Fernet(path.read_bytes().strip())


def encrypt(data):
    return cipher().encrypt(db.dumps(data).encode()).decode()


def decrypt(value):
    return json.loads(cipher().decrypt(value.encode()))


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()
    return salt + "$" + digest


def check_password(password, stored):
    return hmac.compare_digest(password_hash(password, stored.split("$")[0]), stored)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def session(token):
    if not token:
        return None
    return db.one("SELECT * FROM sessions WHERE token=? AND expires>?", (token_hash(token), db.now()))


def require_user(request: Request):
    s = session(request.cookies.get("sd_session"))
    if not s:
        raise HTTPException(401, "请先登录")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), s["csrf"]):
            raise HTTPException(403, "登录校验已过期，请刷新页面")
    return s


def redact(text, credentials=None):
    value = str(text)
    for secret in (credentials or {}).values():
        if isinstance(secret, str) and len(secret) >= 4:
            value = value.replace(secret, "[已隐藏]")
    value = re.sub(r'(?i)((?:access_token|refresh_token|app_secret|signSecret|password|authorization|cookie|sign)\s*[=:]\s*)[^\s&,]+', r'\1[已隐藏]', value)
    return value[:12000]
