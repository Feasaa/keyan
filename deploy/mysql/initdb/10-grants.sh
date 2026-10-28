#!/bin/bash
# 数据库账号权限收权：运行时账号只给 DML，DDL 交给独立的迁移账号。
#
# 为什么值得拆成两个账号：
#   应用只需要 SELECT/INSERT/UPDATE/DELETE。一旦它同时拥有 DROP/ALTER，
#   那么"防注入"就只剩下参数绑定这一道墙；墙被绕过一次，表就没了。
#   拆开之后，即使注入成功，攻击者能做的也只有"以应用身份读写本库"，
#   删表、改结构、跨库读都做不到。
#
# 只在**首次**初始化数据目录时由官方 entrypoint 执行（volume 为空时）。
# 已有数据的环境需要手动跑一次，见 README 的说明。

set -euo pipefail

MIGRATOR_USER="${MYSQL_MIGRATOR_USER:-migrator}"
MIGRATOR_PASSWORD="${MYSQL_MIGRATOR_PASSWORD:-}"

if [ -z "$MIGRATOR_PASSWORD" ]; then
  echo "[grants] MYSQL_MIGRATOR_PASSWORD 未设置，跳过收权（运行时账号保留 DDL 权限）" >&2
  exit 0
fi

if [ "$MIGRATOR_USER" = "$MYSQL_USER" ]; then
  echo "[grants] 迁移账号不能与运行时账号同名，否则收权会把自己锁死" >&2
  exit 1
fi

# 库名里的 _ 和 % 在 GRANT 语句中是通配符，必须转义。
# 官方镜像建库时也是这么做的（所以 SHOW GRANTS 里看到的是 `agent\_chat`），
# 不转义的话 `agent_chat` 这个模式还会匹配到 agentXchat。
DB_PATTERN=$(printf '%s' "$MYSQL_DATABASE" | sed -e 's/_/\\_/g' -e 's/%/\\%/g')

# 用 `REVOKE ALL PRIVILEGES, GRANT OPTION FROM user` 这种全局形式，而不是
# `REVOKE ... ON db.*`：后者要求库名模式与已有授权**逐字符一致**，
# 上面那个转义差异会让它报 1141 "There is no such grant defined"。
#
# 注意 heredoc 不加引号，$ 会被展开；反引号必须转义，否则会被当成命令替换。
mysql --protocol=socket -uroot -p"$MYSQL_ROOT_PASSWORD" <<-SQL
CREATE USER IF NOT EXISTS '${MIGRATOR_USER}'@'%' IDENTIFIED BY '${MIGRATOR_PASSWORD}';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM '${MIGRATOR_USER}'@'%';
GRANT ALL PRIVILEGES ON \`${DB_PATTERN}\`.* TO '${MIGRATOR_USER}'@'%';

REVOKE ALL PRIVILEGES, GRANT OPTION FROM '${MYSQL_USER}'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE ON \`${DB_PATTERN}\`.* TO '${MYSQL_USER}'@'%';

FLUSH PRIVILEGES;
SQL

echo "[grants] 完成：${MIGRATOR_USER} 拥有 DDL；${MYSQL_USER} 只有 DML"
