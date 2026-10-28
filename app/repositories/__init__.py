"""数据访问层（≈ Spring 的 `@Repository`）。

这一层是**防 SQL 注入的唯一入口**，规则只有一条：
    永远不要用 f-string / % / format 把变量拼进 SQL 文本。

SQLAlchemy 的 `select(User).where(User.email == email)` 会把 email 编译成
绑定参数（发出去的语句是 `WHERE email = %(email_1)s`，值走协议单独传给驱动），
SQL 文本和值在物理上分离 —— 注入串只会被当成"一个长得奇怪的字符串"。

需要写原生 SQL 时，用 `text("... WHERE email = :email")` + 参数字典，
`:name` 同样是绑定参数。真正的红线只有一条：`text(f"...")`。
tests/test_sql_injection.py 会扫源码拦住这种写法，光靠 code review 靠不住。

另外三个"非 SQL"层面的加固，也在这一层落实：
    1. 软删除过滤 —— `deleted_at IS NULL` 写进每个查询，而不是指望调用方记得
    2. 唯一约束兜底 —— 并发注册靠数据库的 uk_users_email，不靠"先查后插"
    3. 最小字段 —— 只 SELECT 需要的列，密码哈希不出现在列表查询里
"""

from app.repositories.auth_session_repository import AuthSessionRepository
from app.repositories.user_repository import UserRepository

__all__ = ["AuthSessionRepository", "UserRepository"]
