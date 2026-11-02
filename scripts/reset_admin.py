"""Run only from the local Docker terminal. Does not print passwords."""
import getpass
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db
from app.security import password_hash

db.init()
if not db.one("SELECT id FROM users WHERE id=1"):
    raise SystemExit("尚未创建管理员，请在 Web 页面完成首次设置")
password = getpass.getpass("输入新的管理员密码（至少 12 位）：")
confirm = getpass.getpass("再次输入：")
if len(password) < 12 or password != confirm:
    raise SystemExit("长度不足或两次输入不一致，未修改")
with db.connection() as c:
    c.execute("UPDATE users SET password=? WHERE id=1", (password_hash(password),))
    c.execute("DELETE FROM sessions")
db.audit("本地终端重设管理员密码", {})
print("密码已更新，所有旧会话已退出。")
