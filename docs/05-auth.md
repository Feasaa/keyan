# 鉴权与安全

JWT 双令牌、邮箱验证码、防 SQL 注入的四层防线、数据库账号权限。

> 返回 [README](../README.md) · 上一篇 [接口契约](04-api-contract.md) · 下一篇 [下一步](06-roadmap.md)

---

## 鉴权（/api/v1/auth）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/auth/verification-code` | 发 6 位邮箱验证码（Redis 存哈希，5 分钟有效） |
| POST | `/auth/register` | 邮箱 + 密码 + 码，验过即建号并直接返回令牌 |
| POST | `/auth/login` | 邮箱 + 密码 |
| POST | `/auth/refresh` | refresh_token 换一对新令牌，旧的立刻作废 |
| POST | `/auth/logout` | 撤销 refresh_token（幂等） |
| GET | `/auth/me` | 拿 access_token 换当前用户 |

```bash
# 1. 发码
curl -X POST http://127.0.0.1:8000/api/v1/auth/verification-code \
  -H 'Content-Type: application/json' -d '{"email":"you@qq.com"}'
# {"code":0,"message":"验证码已发送，请查收邮件",
#  "data":{"email":"you@qq.com","expires_in":300,"resend_after":60},"trace_id":"..."}

# 2. 注册（code 填邮件里那 6 位）
curl -X POST http://127.0.0.1:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@qq.com","password":"correct horse battery staple","code":"123456"}'

# 3. 之后每个请求带令牌
curl http://127.0.0.1:8000/api/v1/auth/me -H "Authorization: Bearer <access_token>"
```

**令牌为什么是 access(JWT) + refresh(落库) 两件套**

| | access_token | refresh_token |
| --- | --- | --- |
| 形态 | JWT，自包含 | 32 字节随机串 |
| 存哪 | 不落库 | 库里只存 sha256 |
| 有效期 | 15 分钟 | 30 天 |
| 能否撤销 | 不能，到期自然失效 | 能，`revoked_at` 一写就废 |
| 用途 | 每个请求的 Authorization 头 | 只在换票时用一次 |

纯 JWT 的软肋是"登出后旧 token 还能用"，纯 session 的软肋是"每请求查一次库"。分开用各取所长：access 短到 15 分钟，即使泄露也就这么久；refresh 落库，所以"立刻踢人"是可行的。

**refresh 每次换票都轮换**，旧的立刻作废。不轮换的话，refresh token 一旦被复制走就永久有效，而且服务端没有任何信号能发现"同一份凭证在两个地方用"。

并发刷新同一 token 时，用 `SELECT ... FOR UPDATE` 加行锁：第二个请求阻塞到第一个提交，再读时这行已经 `revoked_at` 非空，不会双发新令牌。

**refresh token 复用检测**

`family_id` 把同一串登录链路（首次登录 → 第一次 refresh → 第二次 refresh …）连在一起。旧 token 被重放时，分两种情况：

- **宽限期内（默认 30 秒）**：按"并发刷新 / 网络重试"处理，只报 401，不告警。因为正常用户的重试动作或并发刷新会触发这种情况，误伤成本太高。
- **超过宽限期**：判定为 token 泄露 —— 撤销**整条链路**（`revoke_family`），并发 `WARNING` 日志（应接入告警）。只撤被重放的那一代没用，攻击者手上可能是链路中任意一代的 token。

1. 邮箱不存在和密码错误返回**完全相同**的响应 —— 状态码、错误码、文案都一致。
2. 邮箱不存在时也跑一次 argon2 校验（`dummy_hash()`），把耗时拉平。不做这一步，登录接口就是个**时序预言机**：邮箱存在 50ms、不存在 1ms，不看响应内容、只看耗时就能批量探测哪些邮箱注册过。
3. 账号状态检查放在密码校验**之后**。反过来，"账号已被停用"这句话本身就泄露了账号存在。

**登录失败限流**（`tests/test_auth.py::test_login_locks_account_after_repeated_failures` 逐条锁住）

两个独立计数器：

| 维度 | Redis key | 阈值（默认） | 窗口 |
| --- | --- | --- | --- |
| 按邮箱 | `login:fail:email:<email>` | 5 次 | 15 分钟 |
| 按 IP | `login:fail:ip:<ip>` | 20 次 | 15 分钟 |

关键顺序：**先检查计数，再跑 argon2**。反过来就等于"被刷一次浪费一次 CPU"，限流失去意义。同样，账号不存在也要计数，否则"第 6 次变 429"会变成账号存在性预言机。

成功登录只清本账号的计数，不清 IP。理由是：否则任何有效账号都能把 IP 计数归零，换邮箱绕过限流。

**注册为什么"先消费验证码，再查邮箱是否已注册"**

反过来的话，不掌握该邮箱的人也能通过"是否报邮箱已占用"批量探测注册情况。先验证码后查重，攻击者必须先能收到邮件才问得出答案。

**密码策略：8–128 位，不限字符类型。**

这不是偷懒，是 NIST SP 800-63B 的建议：强制大小写数字符号混排会把人逼向 `Passw0rd!` 这类可预测变形，实际熵反而下降；长度才直接决定搜索空间。真正的防护是限制尝试次数，本项目在登录（验证码试错上限）和发码（三层配额）两侧都有。

上限 128 是防 DoS：密码长度直接进哈希函数的计算量，不设上限时一个 1MB 的"密码"能占满一个核。

**密码哈希用 argon2id，bcrypt 只保留校验能力。** 登录时按密文前缀分流，旧算法验过之后顺手把新密文写回库 —— 用户无感，也不需要批量迁移脚本。不用 passlib 是因为它依赖 Python 3.13 已移除的 `crypt` 模块，import 阶段就炸。

## 验证码：邮箱验证码自建，人机验证先不接

**邮箱验证码走 Redis 自建**，理由是和注册动作在同一个事务语义里 —— 校验通过才能建号。第三方验证码服务通常只给"校验通过"的票据，跨系统对账会引入"码验过了但建号失败了"这类中间态。既然已经有 Redis，自建成本更低。

四个设计点：

1. **存哈希不存明文。** Redis 常被配成无密码、可远程访问，`KEYS vc:*` 就能 dump。存明文等于一次配置失误把所有在途验证码公开。这里用 sha256 而不是 argon2 —— 6 位数字只有 100 万种可能，慢哈希也挡不住离线穷举，真正的作用只是"不直接可读"。防穷举靠的是**在线**的试错次数限制。
2. **三层限流，缺一层都能被打穿**：60 秒冷却（防连点/轰炸）、每邮箱每日上限（换 IP 也没用）、每 IP 每小时上限（换邮箱也没用）。
3. **试错 5 次即作废。** 6 位数字在 5 分钟有效期内不限次是能被脚本穷举完的。`INCR` 是原子的，所以并发爆破也会被这个计数挡住。
4. **发信失败要把冷却一起清掉**，否则用户看到"发送失败"，想重试却被自己的 60 秒冷却挡在门外。

**人机验证这层建议先不接**，理由：注册场景的滥用成本主要由发信配额决定，上面三层限流已经挡住绝大部分；而极验 / Turnstile 都需要先开通账号拿 key，属于"外部依赖 + 按量计费"的决策，不该在功能主链路还没跑通时引入。

真要接的话，按场景选：

| 方案 | 适合 | 代价 |
| --- | --- | --- |
| **极验 GeeTest v4** | 国内用户为主 | 企业账号 + 按量计费，国内体验最好 |
| **Cloudflare Turnstile** | 海外用户为主 | 免费、多数场景无感通过，国内到 challenges.cloudflare.com 偶有波动 |
| 自建图片验证码 | 零第三方依赖 | OCR 很容易破、无障碍差，只挡最粗糙的脚本 |
| reCAPTCHA | 已有 Google 生态 | 国内基本不可用 |

接入点在 `VerificationService.issue()` 的最前面 —— 在消耗任何配额之前校验票据，避免"人机没过却已经花掉一次发信配额"。

## 防 SQL 注入：四层，参数绑定只是第一层

| 层 | 手段 | 在本项目哪里 |
| --- | --- | --- |
| 1 | **参数绑定**，值永远不进 SQL 文本 | 所有查询走 ORM 表达式；原生 SQL 用 `text("... = :name")` + 参数字典 |
| 2 | **入口校验**，把输入形状收敛 | Pydantic `EmailStr`（拒 CRLF，顺带堵住 SMTP 头注入）、`code` 的 `^\d{6}$`、长度上限 |
| 3 | **账号最小权限**，DDL 从运行时账号上摘掉 | `deploy/mysql/initdb/10-grants.sh` |
| 4 | **错误不泄露 SQL** | 全局兜底 500 只回中性文案，堆栈只进日志 |

第 1 层能生效是因为 SQLAlchemy 把 `WHERE email = %s` 和值**分开**传给驱动 —— 注入串只会被当成"一个长得奇怪的字符串"。`tests/test_sql_injection.py` 里有一条测试直接把编译后的语句打出来验证这一点。

第 2 层是"顺手"的收益：`EmailStr` 会拒绝含换行的地址，这恰好也堵住了"往 Subject 里塞 `Bcc:`"这类 SMTP 头注入。

第 3 层最容易被忽略，但收益最大：`app` 账号只有 `SELECT/INSERT/UPDATE/DELETE`，即使前两层都被绕过，攻击者也删不掉表、改不了结构、读不到其他库。

```sql
-- 收权后的样子（SHOW GRANTS FOR CURRENT_USER()）
GRANT USAGE ON *.* TO `app`@`%`
GRANT SELECT, INSERT, UPDATE, DELETE ON `agent\_chat`.* TO `app`@`%`
```

第 1 层还有一个容易被破坏的地方：**`text(f"...")`**。它长得和正常代码几乎一样，写的人往往觉得"这是内部变量，安全"。所以 `tests/test_sql_injection.py` 用 **AST** 扫源码，凡是把 f-string / `+` / `%` / `.format()` 交给 `text` / `execute` / `literal_column` 的一律失败。用 AST 而不是正则，是因为正则会把注释和 docstring 里的示例也算进去 —— 那种测试最后一定会被人加 skip。

## 数据库账号权限（防注入的第二道墙）

`deploy/mysql/initdb/10-grants.sh` 把账号拆成两个：

| 账号 | 权限 | 谁用 |
| --- | --- | --- |
| `app`（运行时） | `SELECT / INSERT / UPDATE / DELETE` | 应用进程 |
| `migrator`（迁移） | `ALL`（含 DDL） | 只给 `alembic` 用 |

`alembic/env.py` 读 `DB_MIGRATION_USER`，不配就退回运行时账号 —— 本地图方便，但那种情况下 `app` 必然带着 DDL 权限，生产别这么干。

这个脚本只在**首次**初始化数据目录时自动执行（volume 为空）。已有数据的环境要手动跑一次：

```bash
docker exec agent-chat-mysql bash /docker-entrypoint-initdb.d/10-grants.sh
```

`tests/test_db_contract.py::test_runtime_account_has_no_ddl_privileges` 会校验这条不变量。没有测试锁住的话，一句 `GRANT ALL` 就能把它悄悄撤销 —— 权限是"多给一点没关系"的典型场景，直到出事。
