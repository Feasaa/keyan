# 鉴权与安全

JWT 双令牌、邮箱验证码、VAPTCHA 人机验证（服务端二次验签）、防 SQL 注入的四层防线、数据库账号权限。

> 返回 [README](../README.md) · 上一篇 [接口契约](04-api-contract.md) · 下一篇 [下一步](06-roadmap.md)

---

## 鉴权（/api/v1/auth）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/auth/verification-code` | 发 6 位邮箱验证码（Redis 存哈希，5 分钟有效）；配置了 VAPTCHA 时需带人机验证票据 |
| POST | `/auth/register` | 邮箱 + 密码 + 码，验过即建号并直接返回令牌 |
| POST | `/auth/login` | 邮箱 + 密码 |
| POST | `/auth/refresh` | refresh_token 换一对新令牌，旧的立刻作废 |
| POST | `/auth/logout` | 撤销 refresh_token（幂等） |
| GET | `/auth/me` | 拿 access_token 换当前用户 |

**哪些接口要令牌**：除 `/auth/*`（发码、注册、登录、刷新）和 `/health*`、`/chat/status` 外，其余全部要求 `Authorization: Bearer <access_token>` —— 包括 `/chat`、`/chat/stream`、`/conversations/*`、`/messages/*`。对话接口**没有**匿名模式：没令牌直接 `401 / 11401`，不落库也不调模型。

```bash
# 1. 发码（未配置 VAPTCHA 时 token/knock 等字段可整体省略）
curl -X POST http://127.0.0.1:8000/api/v1/auth/verification-code \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@qq.com","token":"<vaptcha token>","knock":"<vaptcha knock>","ip":"<SDK 签名 IP>"}'
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

## 验证码：邮箱验证码自建 + VAPTCHA 人机验证

**邮箱验证码走 Redis 自建**，理由是和注册动作在同一个事务语义里 —— 校验通过才能建号。第三方验证码服务通常只给"校验通过"的票据，跨系统对账会引入"码验过了但建号失败了"这类中间态。既然已经有 Redis，自建成本更低。

四个设计点：

1. **存哈希不存明文。** Redis 常被配成无密码、可远程访问，`KEYS vc:*` 就能 dump。存明文等于一次配置失误把所有在途验证码公开。这里用 sha256 而不是 argon2 —— 6 位数字只有 100 万种可能，慢哈希也挡不住离线穷举，真正的作用只是"不直接可读"。防穷举靠的是**在线**的试错次数限制。
2. **三层限流，缺一层都能被打穿**：60 秒冷却（防连点/轰炸）、每邮箱每日上限（换 IP 也没用）、每 IP 每小时上限（换邮箱也没用）。
3. **试错 5 次即作废。** 6 位数字在 5 分钟有效期内不限次是能被脚本穷举完的。`INCR` 是原子的，所以并发爆破也会被这个计数挡住。
4. **发信失败要把冷却一起清掉**，否则用户看到"发送失败"，想重试却被自己的 60 秒冷却挡在门外。

## 人机验证：VAPTCHA V4（服务端二次验签）

选型 VAPTCHA 而不是极验 / Turnstile：国内访问稳定、免费额度够学习项目用、手势验证对脚本成本高。VID / VKey 在 VAPTCHA 控制台的"验证单元"里。

**整条链路分工**（对应 `app/services/vaptcha_service.py`）：

```
浏览器                          业务服务器                        VAPTCHA
  │ 加载 https://c4.vaptcha.com/src/v4.js
  │ 用 VID 初始化验证单元（SDK 自动调 v4c/v4i 的 config、knock、validate）
  │ await validate() ── 通过后拿到四件套
  │   token / knock / dfu / ip（ip 是 token 实际签名用的 IP 快照）
  │ ── 原样随业务请求提交 ──→ ① 立即验签（票据有有效期，拖着不验就是给重放留窗口）
  │                          ② 验过才开始：限流检查 → 消费发码配额 → 发信
  │                          remote：JSON POST vid/vkey/token/knock/dfu/ip ──→ v41
  │                          ←── {code:0, data:{result:true, code:0}} ───────
```

四条纪律：

1. **VKey 只能放服务端。** 前端拿到 VKey 就能自己签给自己，验签等于作废。本项目 VKey 只出现在 `.env`（`VAPTCHA_VKEY`），绝不进任何前端资源。
2. **验签在最前面**（`AuthService.send_register_code` 第一行）。排在三层限流与发信配额之前 —— "人机没过却花掉一次发信配额"的中间态就不存在。
3. **fail-closed。** 验签上游超时 / 不可达 / 非 200 一律 504 拒绝（`21504`）。故障时放行等于给攻击者一个"打挂验签链路就免验证"的按钮。
4. **IP 优先用 SDK 的签名 IP。** token 是拿那个 IP 签的，换成服务端来源 IP 必然对不上（local 模式尤其敏感）；只有前端没带时才退回来源地址。

**踩坑点：页面必须走 HTTPS，否则 `Error: 当前网站未授权`**

`validate()` 走不通时，报错只有一句 `Error: 当前网站未授权`。它既不说原因，也不说明 VID 其实有效，很容易被误判成"VID 填错了""SDK 没加载"或"域名白名单没配"。

按现象判断（本项目实测）：

| 现象 | 结论 |
| --- | --- |
| `window.vaptcha` 不存在 / script 加载报错 | SDK 没加载，先查 `c4.vaptcha.com` 可达性 |
| 初始化 reject，`当前网站未授权`，而页面是 **http://** | **页面必须 HTTPS** —— 同一 VID、同一 hostname，从 `http://127.0.0.1:5173` 换到 `https://127.0.0.1:5443` 后立刻初始化成功 |
| 换成 HTTPS 后仍报同一句 | 才是域名白名单没放行该 hostname |

官方原文："确认业务页面可通过 **HTTPS** 加载 `c4.vaptcha.com/src/v4.js` 并访问 `v4c.vaptcha.com/api/config`"。这句话是硬要求，不是建议。

不用猜，直接问 SDK 背后的配置接口（GET，参数与 SDK 一致）：

```bash
curl -s "https://v4c.vaptcha.com/api/config?vid=<VID>&sdkv=300&tag=0123456789abcdef&lang=zh-CN&_t=$(date +%s000)" \
  -H "Referer: https://your.domain/login"
# {"code":0,...}                        可用
# {"code":8,"msg":"验证单元不存在"}      VID 写错
# {"code":9,"msg":"验证单元不匹配"}      站点未授权（http 页面、域名不在白名单，都落到这个码）
```

>>> 注意：这个接口返回的 `code` 与官方 verify 接口的 `data.code` **不是同一套**。前者是 SDK 的配置获取结果（0/8/9），后者是 token 验签结果（0/1/2/3），别混。

本地联调需要同时满足两件事：

1. **用 HTTPS 提供页面**。自签证书即可，浏览器点"继续前往"。
2. **白名单里放 hostname**：`127.0.0.1` 和 `localhost` 是两个不同来源，只加一个另一个仍会被拒；不要带端口、不要带 `http://`。

> 加了 HTTPS 之后马上会遇到下一个坑：HTTPS 页面无法 fetch `http://127.0.0.1:8000`（混合内容，浏览器直接拦）。两种解法：给后端也配 TLS（`uvicorn --ssl-keyfile ...`），或本地起一个同源反代（本项目 `web/` 调试用的 `.tmp-tls/serve_https.py` 就是后者，页面走 `/api/v1` 同源转发，后端配置一行都不用改）。

**浏览器差异：Edge 的弹窗分支会卡住**

SDK 在 PC 上默认 `window.open` 一个验证窗口，靠 `postMessage` 把 boot 数据递进去。Chromium 里正常，**Edge 里那个窗口收不到 boot**：标题渲染出来了、内容一直停在「验证准备中…」，不报错也不超时（单独打开 `verify.html` 是同一现象 —— 它的 `postMessage` 找不到 opener）。

判断依据：窗口里的 `#loading` 一直没挂上 `is-hidden`，`#challengeTitle` 停在默认文案。直接问 `v4c.vaptcha.com/api/config` 也还是 `code:0`，所以不是白名单也不是 VID。

解决办法是**不走弹窗分支**：让 SDK 用内嵌形态（面片挂在 `#vaptcha_iframe_overlay` 全屏遮罩里，不占页面布局）。联调页里用 `VaptchaCore.prototype.shouldUseIframe` 兜底（见 `docs/07-api-examples.md` 8.1），生产环境建议直接在控制台把验证单元设为内嵌形态，而不是在业务代码里 patch SDK 内部方法。

**官方对 token 窗口的要求（本地模式尤其别设大）**

- token 自身的校验窗口是 **30 秒**；
- 本地验签官方**建议 3 秒**有效期，并对已通过的 token 做防复用。

本项目 `VAPTCHA_LOCAL_TTL_SECONDS` 默认 3，配合 Redis `SET NX` 保证一张票只放行一次。窗口就是重放空间 —— 设成 300 秒等于把可重放期放大 100 倍。

> 前端要抄的接入代码（SDK 初始化 → `validate()` → 四件套随发码提交 → 票过期/复用怎么重试）在 [07-api-examples.md](07-api-examples.md) 的 2.1.1，不重复写在这里。

**两种验签方式**（`VAPTCHA_VERIFY_MODE`，都按官方文档实现）

| 模式 | 做法 | 取舍 |
| --- | --- | --- |
| `remote`（默认） | POST `https://v41.vaptcha.com/api/verify`，读 `data.result`+`data.code` | 复用检测、IP 漂移判定由官方兜底，集成最省心；代价是每次一次外网往返 |
| `local` | 本地 HMAC-SHA256：`hex(HMAC(VKey, "{ts}.{ip}.{dfu}.{knock}")) == token 第三段`，校验时间窗口 | 少一次外网往返；但票据在有效期内可被重放（官方文档明确提示），所以额外用 Redis `SET NX` 标记票据一次性 |

官方返回码语义（`remote`）：`data.code` 0 通过、1 鉴权不通过但 token 存在、2 鉴权通过但 token 已过期、3 其它失败。**严格通过只有 `result=true && code=0`**。`code=1` 多见于政企老宽带 IP 随机漂移，`VAPTCHA_ALLOW_CODE_1=true` 才放行 —— 这是安全与可用性的取舍，默认不放。

> V2 时代的 `api.vaptcha.com` / `0.vaptcha.com` 已经 NXDOMAIN，`curl: (6) Could not resolve host` 就是这个原因，不是本机 DNS 坏了。V4 用 `v41.vaptcha.com`。

**配置项**（`.env`，VID/VKey 任一为空 = 功能关闭，票据不校验、行为与从前完全一致）：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `VAPTCHA_VID` / `VAPTCHA_VKEY` | 空 | 验证单元的 VID / VKey，都填才启用 |
| `VAPTCHA_VERIFY_MODE` | `remote` | `remote` 调官方接口 / `local` 本地验签 |
| `VAPTCHA_VERIFY_URL` | `https://v41.vaptcha.com/api/verify` | 官方 V4 验签端点（JSON 提交） |
| `VAPTCHA_TIMEOUT_SECONDS` | `5` | 验签请求超时（发码本身要发 SMTP，多一次 100ms 级调用不是瓶颈） |
| `VAPTCHA_LOCAL_TTL_SECONDS` | `300` | 仅 local：票据有效期窗口 |
| `VAPTCHA_ALLOW_CODE_1` | `false` | 仅 remote：`data.code=1` 是否宽松放行 |

**接口契约**：`POST /auth/verification-code` 请求体在原有 `email`/`scene` 之上新增四个可选字段（与 SDK `validate()` 返回同名，前端零转换）：`token`、`knock`、`dfu`、`ip`。服务端启用 VAPTCHA 后 `token`/`knock` 缺一即 400。local 模式必须回传 `ip`，否则签名必然对不上。

**错误码**：

| code | HTTP | 场景 |
| --- | --- | --- |
| `11400` | 400 | 票据缺失 / 验签不通过 / 已过期 / 已被使用 —— 与邮箱验证码同码（客户端动作一样：重新验证），文案区分"人机验证未通过，请重新验证" |
| `21504` | 504 | 验签上游超时 / 不可达 / 外层 code≠0（VID-VKey 配错），fail-closed 拒绝发码 |

测试契约在 `tests/test_vaptcha.py`（20 条）：关闭时无票据也放行、开启时缺票据 400、远程通过/不通过/过期/code=1 严格与宽松、上游故障 504 且验证码绝不发出、请求体是官方 JSON 契约、本地验签的正确签名/过期/篡改/IP 不符/重复使用。

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
