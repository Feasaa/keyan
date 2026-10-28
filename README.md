# learn-fastapi（AI / LLM 服务骨架）

FastAPI + LangChain 的分层服务骨架。分层方式刻意对齐 Spring Boot 的习惯，如果你有 Spring 基础，可以按"同一职责、不同写法"来读。

对外链路有三条：**邮箱注册登录**（`/api/v1/auth`）、**LLM 对话**（`/api/v1/chat`）、**健康检查**（`/api/v1/health`）。底层还铺好了雪花 ID、MySQL + Alembic 迁移、Redis、统一响应外壳、业务错误码、全局异常处理。

鉴权部分是完整闭环：邮箱验证码 → 注册 → JWT access + 落库 refresh → 刷新轮换 → 登出撤销，含三层发码限流与账号枚举防护。

---

## 一、先看总对照表

| Spring Boot | 本项目（FastAPI） | 职责 |
| --- | --- | --- |
| `DemoApplication.java`（`@SpringBootApplication`） | `app/main.py` 的 `create_app()` | 装配应用：中间件、路由、生命周期 |
| `SpringApplication.run(...)` | `app = create_app()` | 启动入口 |
| `@RestController` | `app/api/routes/*.py` 里的 `router` | 暴露 HTTP 接口 |
| `@RequestMapping("/api/v1")` | `settings.API_PREFIX` + `api/router.py` | 统一前缀与版本分组 |
| 组件扫描（自动注册 Controller） | `include_router(...)`（**必须手写**） | 把路由接进应用 |
| `@Service` | `app/services/*.py` | 业务逻辑 |
| `@Autowired` | `Depends(get_x)` | 依赖注入 |
| `@Bean` / `@Configuration` | `app/core/`、各 service 的 `get_xxx_service()` | 基础设施装配 |
| `application.yml` + `@ConfigurationProperties` | `app/core/config.py` + `.env` | 配置管理 |
| `Filter` / `HandlerInterceptor` | `app/main.py` 里的 `add_middleware(...)` | 横切关注点 |
| `@ControllerAdvice` + `@ExceptionHandler` | `app/core/exception_handlers.py` 的 `register_exception_handlers` | 统一异常映射 |
| 统一返回体 `Result<T>` | `app/core/response.py` 的 `ApiResponse[T]` | `{code, message, data, trace_id}` 外壳 |
| 错误码枚举（`ErrorCode.NOT_FOUND`） | `app/core/errors.py` 的 `ErrorCode(ErrorSpec)` | 业务码 + HTTP 状态 + 默认文案 |
| `MDC.put("traceId", ...)` + `Filter` | `app/core/context.py` 的 `ContextVar` + 纯 ASGI 中间件 | 请求级链路 ID |
| JPA `@Entity` / `@Table` | `app/db/models.py` 的 `Mapped[...]` | 表模型 |
| Flyway / Liquibase（不用 `ddl-auto`） | `alembic/versions/` | 表结构变更（本项目禁用 `create_all`） |
| `@Repository` | `app/repositories/*.py` | 数据访问，SQL 的唯一落点 |
| `BCryptPasswordEncoder` | `app/core/security.py` 的 `hash_password` / `verify_password` | 密码哈希（argon2id） |
| `JwtUtil` + `JwtAuthenticationFilter` | `app/core/security.py` + `app/api/deps.py` 的 `get_current_user` | 令牌签发与解析 |
| `@AuthenticationPrincipal` | `CurrentUser` 类型别名 | 取当前登录用户 |
| Spring Security 的 `SessionRegistry` | `auth_sessions` 表 | 可撤销的登录态（refresh token） |
| springdoc（Swagger UI） | 内置 `/docs`（Swagger）、`/redoc` | 接口文档 |
| `pom.xml` / `build.gradle` | `pyproject.toml` + `requirements.txt` / `environment.yml` | 依赖与构建 |
| `src/test/java` + `@SpringBootTest` + `MockMvc` | `tests/` + `TestClient` | 测试 |
| `@MockBean` | `app.dependency_overrides[...]` | 测试替换实现 |
| profile（`--spring.profiles.active`） | `.env` / 环境变量 + `DEBUG` | 多环境 |

---

## 二、目录结构（每个文件夹为什么存在）

```
sr/
├── app/                        # 应用主包（≈ 根包 com.example.demo）
│   ├── __init__.py
│   ├── main.py                 # 入口：应用工厂 + 生命周期 + 中间件 + 挂路由
│   ├── core/                   # 基础设施：配置、ID、错误码、统一响应（与业务无关）
│   │   ├── config.py           # 全局配置（≈ application.yml + @ConfigurationProperties）
│   │   ├── errors.py           # 错误码表 + AppError（≈ 错误码枚举 + 业务异常基类）
│   │   ├── response.py         # 统一响应外壳 ApiResponse[T]
│   │   ├── context.py          # 请求级 ContextVar（trace_id）
│   │   ├── middleware.py       # trace_id 中间件（纯 ASGI，不缓冲 SSE）
│   │   ├── exception_handlers.py  # 四类异常的集中翻译（≈ @ControllerAdvice）
│   │   ├── security.py         # 密码哈希（argon2id）+ 令牌签发/校验（≈ BCryptPasswordEncoder + JwtUtil）
│   │   ├── snowflake.py        # 雪花 ID 生成器（时间有序、多实例不冲突）
│   │   └── sse.py              # SSE 事件名与结束哨兵（帧编码交给 sse-starlette）
│   ├── prompts/                # 提示词（正文是文件，不是代码常量）
│   │   ├── files/chat_system.md  # 提示词正文 + 元数据（id / version / description）
│   │   ├── loader.py           # 加载与校验（不合法启动即失败）
│   │   └── registry.py         # 按 id 取用 + 按 mtime 热更新
│   ├── db/                     # 数据访问层（≈ repository + entity）
│   │   ├── base.py             # ORM 基类 + 约束命名约定 + utcnow
│   │   ├── enums.py            # 状态枚举，与 CHECK 约束一一对应
│   │   ├── models.py           # 4 张表的模型，与 alembic/versions 保持一致
│   │   ├── session.py          # 引擎 / 会话工厂 / get_session 依赖
│   │   ├── redis.py            # Redis 客户端（验证码 / 限流计数）
│   │   └── types.py            # MySQL 物理类型（UNSIGNED / MEDIUMTEXT / DATETIME(3)）
│   ├── repositories/           # SQL 的唯一落点（≈ @Repository）
│   │   ├── user_repository.py          # 用户读写 + 软删除过滤
│   │   └── auth_session_repository.py  # refresh token 会话（按哈希查）
│   ├── api/                    # 接口层（FastAPI 惯例）
│   │   ├── __init__.py         # 只导出 api_router
│   │   ├── router.py           # 路由聚合（≈ 组件扫描的"手写替代品"）
│   │   ├── deps.py             # 依赖装配 + get_current_user（≈ @AuthenticationPrincipal）
│   │   └── routes/             # 一个资源一个文件
│   │       ├── auth.py         # 发码 / 注册 / 登录 / 刷新 / 登出 / 当前用户
│   │       ├── health.py       # 存活探针 + 就绪探针
│   │       └── chat.py         # 一次性对话 + SSE 流式对话 + 状态
│   ├── schemas/                # 接口契约 DTO（≈ dto/vo 包）
│   │   ├── auth.py             # 邮箱归一化 + 密码长度策略
│   │   ├── health.py
│   │   └── chat.py
│   ├── services/               # 业务逻辑（≈ @Service）
│   │   ├── exceptions.py       # 业务异常语义
│   │   ├── auth_service.py     # 注册 / 登录 / 刷新 / 登出（事务边界在这里）
│   │   ├── verification_service.py  # 验证码签发与校验 + 三层限流
│   │   ├── mail_service.py     # 异步 SMTP 发信
│   │   └── llm_service.py      # LangChain LCEL 链封装（invoke / astream_events）
│   └── utils/                  # 通用工具（≈ common/util 包）
│       ├── net.py              # IP ↔ VARBINARY(16)
│       └── masking.py          # 日志脱敏（邮箱 / 令牌）
├── tests/                      # 测试（≈ src/test/java）
│   ├── conftest.py             # TestClient + LLM 桩 + 邮件桩 + 测试库隔离
│   ├── test_health.py          # 探针 + 统一响应外壳
│   ├── test_chat.py            # 一次性 / 流式对话
│   ├── test_prompts.py         # 提示词加载与热更新
│   ├── test_errors.py          # 错误码编码规则 + 四类异常路径
│   ├── test_auth.py            # 注册 / 登录 / 刷新 / 登出 / 账号枚举防护
│   ├── test_security.py        # 密码哈希 + JWT（含 alg=none 伪造）
│   ├── test_verification.py    # 验证码一次性 / 过期 / 冷却 / 双层配额
│   ├── test_sql_injection.py   # 源码 AST 扫描 + 运行时注入载荷
│   ├── test_snowflake.py       # 并发唯一性 + 位布局 + 时钟回拨
│   ├── test_models.py          # 纯 metadata 断言（物理类型 / 约束名）
│   └── test_db_contract.py     # 连真库比对 information_schema + 权限收权校验
├── alembic/                    # 数据库迁移（表结构的唯一真相源）
│   ├── env.py                  # 复用 app 的配置与模型，DSN 不写进 ini
│   ├── script.py.mako          # 迁移模板（已改成项目风格，生成即过 ruff）
│   └── versions/               # 迁移脚本，只依赖 sqlalchemy，不 import 应用代码
├── deploy/                     # 本地依赖编排（Docker，不参与应用打包）
│   ├── docker-compose.yml      # MySQL 8.4 LTS + Redis 7.4
│   ├── .env.example            # 复制成 deploy/.env 后填密码
│   ├── mysql/conf.d/01-server.cnf  # 字符集 / 时区 / 慢查询 / sql_mode
│   ├── mysql/initdb/10-grants.sh   # 账号收权：运行时只给 DML，DDL 归迁移账号
│   └── redis/redis.conf        # AOF / volatile-lru / ACL 收权
├── environment.yml             # conda 环境定义
├── requirements.txt            # pip 依赖
├── pyproject.toml              # 项目元信息 + pytest / ruff 配置
├── .env copy.example           # 环境变量模板（复制成 .env 使用）
└── .gitignore
```

### 各层存在的理由（按依赖顺序）

**`app/`** — 所有代码收在一个包内。Spring 的组件扫描只扫启动类所在包及子包，代码放到根包外就注入不了；Python 里则是 `uvicorn app.main:app` 找不到模块。两者本质相同：**必须在约定的根包之下**。

**`app/core/`** — 配置、日志、安全、异常处理这些东西每个项目都要，但不属于任何业务。混在业务包里会导致：换环境时要全局搜索改代码；想复用到下一个项目得连业务一起搬。所以单独成层，业务只"使用"它。

**`app/api/`** — 只做 HTTP 协议转换：收参数 → 调 service → 返回。**禁止写业务规则**，因为同一段业务还要被定时任务、消息消费者调用，写在接口层就被绑死在 HTTP 上。对应 Spring 里"Controller 不写业务"的铁律。

**为什么叫 `api/routes/` 而不是 `controllers/`** — 这是 FastAPI 生态的命名惯例（官方教程用 `routers/`，官方模板用 `api/routes/`），`controllers` 是 Java/Spring 的词。跟着生态走，别人接手时不用先猜你的目录约定。

**版本号只留在 URL 里** — `/api/v1` 是**对外契约**，不兼容变更只能 v1、v2 并存；但它不该用目录嵌套表达。目录结构属于内部实现，哪天把 `routes/` 拆成 `routes/admin/`、`routes/open/`，URL 不该跟着变。所以这里是 `api/routes/chat.py` + `API_PREFIX=/api/v1`，而不是 `api/v1/endpoints/chat.py`。

**`app/schemas/`** — 出入参契约。独立于任何存储结构：接口返回常常是拼装结果，且内部字段（密钥、审计字段）绝不能返回给前端。对应 Spring 的 DTO/VO 分层。

**`app/repositories/`** — SQL 的唯一落点，一表一仓库。存在的意义有两个：一是"防注入"这条规则只需要在一个地方守住（见[防 SQL 注入](#防-sql-注入四层参数绑定只是第一层)）；二是软删除过滤、字段白名单这类**漏一次就是漏洞**的细节，写进仓储就不会被某个调用方忘掉。对应 `@Repository`。

**`app/services/`** — 业务规则的唯一归宿：可复用、可单测（不依赖 HTTP）、隔离框架（换 Web 框架不用重写业务）。LLM 的链构建、降级、超时重试，以及注册/登录的事务边界，全部收敛在这里，接口层只做协议适配。对应 `@Service`。

**`tests/`** — 测试锁住的是**接口契约**而不是实现。LLM 用桩链、邮件用桩服务替换，不花钱、不依赖网络、结果可断言。

---

## 三、请求流转对比

Spring：

```
HTTP → DispatcherServlet → HandlerMapping → Controller
     → Service（@Transactional） → Repository → DB
```

FastAPI：

```
HTTP → Starlette 路由匹配 → APIRouter（routes）
     → Depends 解析依赖 → Service → 上游 LLM 网关
```

路径：`/api/v1/chat/stream` 由三段拼出来 —— `settings.API_PREFIX` + `router.py` 的 `prefix="/chat"` + 路由模块里的 `"/stream"`。资源根路径只在一处定义，改名不会漏。

---

## 四、几个必须知道的写法差异

### 1. 路由不会自动注册

Spring 加了 `@RestController` 就被扫描到；FastAPI 必须在 `app/api/router.py` 里显式 `include_router`，再在 `main.py` 里挂一次。**漏写就是 404**，而且不报错。这是从 Spring 转过来最容易踩的坑。

### 2. 依赖注入的时机不同

| | Spring | FastAPI |
| --- | --- | --- |
| 声明位置 | 字段 / 构造器 | **函数参数** |
| 装配时机 | 容器启动时装配整个对象图 | 每个请求按需解析 |
| 默认作用域 | 单例（singleton） | **每次请求调用一次工厂函数** |

`service: LLMService = Depends(get_llm_service)` 读作："处理请求前先调 `get_llm_service()`，把返回值传进来。"

⚠️ **后果**：如果工厂函数里每次 `return XxxService()`，每个请求都会新建对象，对象级缓存立刻丢。所以 `get_llm_service()` 用 `lru_cache(maxsize=1)` 做成进程级单例 —— LLM 客户端内部持有 httpx 连接池，每请求新建等于每请求新建连接池。

### 3. 参数校验靠类型，不靠注解

```python
message: str = Field(min_length=1)  # ≈ @NotBlank
history: list[ChatMessage] = Field(default_factory=list)  # ≈ @Size(max=20)
```

Spring 要写 `@GetMapping @PathVariable @Valid @RequestBody @Operation @ApiResponse` 一堆注解；FastAPI 用 Python 原生类型标注一次表达完，文档自动生成。

### 4. 异常处理

service 层**不抛 HTTP 异常**，只抛 `AppError` 子类（带业务错误码，如 `LLMNotConfiguredError` / `LLMUpstreamError`）；由 `exception_handlers.py` 统一翻译成状态码 + 统一响应体。原因：service 不该知道 HTTP 语义，否则换框架就要重写业务。这对应 Spring 里"Service 抛业务异常，由 `@ControllerAdvice` 统一映射成状态码"的分工，区别是 Python 侧**不需要在路由上写 `try/except`**，全局兜住。

细节见[统一响应结构](#统一响应结构)和[异常怎么走](#异常怎么走)。

### 5. Java 习惯 → Python 规范（迁移对照）

Java 背景最容易带过来的几个习惯，在这里都是反例：

| Java 习惯 | Python / FastAPI 规范 | 为什么 |
| --- | --- | --- |
| `controller/UserController.java` | `api/routes/users.py` | 一个资源一个模块，不加 `Controller` 后缀 |
| `IUserService` + `UserServiceImpl` | `services/user_service.py` 单个模块 | 不做接口/实现分离，鸭子类型 + 需要时才用 `Protocol` |
| `UserDTO` / `UserVO` / `UserRequest` | `schemas/user.py` 里的 `UserCreate` / `UserRead` | 按**用途**命名，不按分层命名 |
| 包名全小写、无分隔 | 模块 `snake_case`，类 `CamelCase` | PEP 8；文件名即 import 路径 |
| `static final int MAX = 20` | 模块级 `MAX_RETRIES = 20` | 没有真正的常量，靠命名约定 |
| 构造器注入 `@Autowired` | 函数参数 `Depends(...)` | 依赖声明在签名上，随请求解析 |
| `application.yml` + profile | `.env` + pydantic-settings | 环境变量优先 |
| `getUserId()` / `setUserId()` | 直接 `user.id` | 不用写 getter/setter，需要时用 `@property` |

判断标准只有一条：**跟着生态的写法走，别把 Java 的命名习惯翻译成 Python**。别人接手时不该先猜你的目录约定。

---

## 五、环境准备（conda）

```powershell
# 方式一：用 environment.yml 一键创建
conda env create -f environment.yml
conda activate fastapi-learn

# 方式二：用 requirements.txt
conda create -y -n fastapi-learn python=3.11
conda activate fastapi-learn
pip install -r requirements.txt
```

> **为什么要单独建环境**：FastAPI 新版要求 Python ≥3.10，本机 `sy1` 是 Python 3.8.20，装不了新版本。已建好的 `fastapi-learn` 用 Python 3.11。
>
> 若 pip 报 SSL 错误，换镜像：
> `pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com`

## 六、本地依赖（MySQL + Redis）

数据库与缓存用 Docker 起，配置在 `deploy/`：

```
deploy/
├── docker-compose.yml        # MySQL 8.4 LTS + Redis 7.4
├── .env.example              # 复制成 .env，改密码
├── mysql/conf.d/01-server.cnf  # 字符集 / 时区 / 慢查询 / sql_mode
└── redis/redis.conf          # AOF + volatile-lru + ACL 收权

alembic/
├── env.py                    # 复用 app 的配置与模型
└── versions/                 # 迁移脚本
```

```powershell
cd D:\workspace\python\sr\deploy
cp .env.example .env           # 改掉里面的三个密码
docker compose up -d           # 只起容器，此时库里是空的
docker compose ps              # 等到两个服务都 healthy

cd D:\workspace\python\sr
alembic upgrade head           # 建表（表结构由 Alembic 管理，不在容器启动时建）
alembic check                  # 应输出 No new upgrade operations detected
```

四个容易踩的点：

- **Windows 上 bind mount 的配置文件会被 MySQL 忽略**。挂进去的文件权限恒为 `0777`，MySQL 见到 world-writable 直接跳过，只在日志里留一行 `World-writable config file ... is ignored`，进程照常启动。后果是 `time_zone` 还是 `SYSTEM`、`slow_query_log` 还是 0、`max_connections` 还是 151——**看起来一切正常，其实配置全没生效**。所以 compose 里把配置挂到 `conf.d-src`，由 entrypoint 用 `install -m 0644` 复制到 `conf.d` 再启动。验证方法：`SELECT @@time_zone, @@slow_query_log, @@max_connections;`

- **Redis 的 ACL 规则不能走命令行参数**。Redis 会把命令行值整体 `sdscatrepr` 加引号后当成一个 token，ACL 解析时用户名里就带了空格，直接报 `Spaces not allowed in ACL usernames` 并拒绝启动。所以 compose 里由 entrypoint 渲染出临时 aclfile 再用 `--aclfile` 加载。
- **表结构只由 Alembic 管，容器启动时不再建表**。之前 `docker-entrypoint-initdb.d` 里放了一份 DDL，结果和模型在注释、索引上大面积漂移，`schema_migrations` 还和 `alembic_version` 打架。删掉重复定义后，`alembic check` 才能作为"模型和库是否一致"的守门员。想从零重来：`docker compose down -v` 后重新 `up -d` + `alembic upgrade head`（`-v` 会连数据一起删）。
- **MySQL 8.4 默认认证插件是 `caching_sha2_password`**，PyMySQL / aiomysql 需要装 `cryptography` 才能握手，否则报 `RuntimeError: 'cryptography' package is required`。8.4 已经移除了 `--default-authentication-plugin`，不要指望退回 `mysql_native_password`。
- **雪花 ID 出接口必须转字符串**。`BIGINT` 超过 2^53，前端 `JSON.parse` 会把末几位变成 0，拿到的 ID 直接查不到记录。Pydantic 里用 `field_serializer` 或直接声明 `str`。

### 雪花 ID（app/core/snowflake.py）

```python
from app.core.snowflake import get_snowflake, decompose

user_id = get_snowflake().next_id()  # 进程级单例，不要每请求新建
decompose(user_id).created_at  # 不查库就知道这条记录什么时候建的
```

位布局 `1 | 41 时间戳 | 10 机器号 | 12 序列号`，单机每毫秒 4096 个。两个约束：

- **`SNOWFLAKE_WORKER_ID` 多实例必须各不相同**，否则两台机器会生成完全相同的 ID。
- **时钟回拨超过 5ms 直接抛 `ClockMovedBackwardsError`**。宁可报错也不生成可能重复的 ID——小幅回拨（NTP 校时）会等待追上，不打断业务。

纪元 `2024-01-01` 写死在代码里，不做成环境变量：能配置的东西就会被配错，而改纪元会让新旧 ID 撞车。

### 数据访问层（app/db/）

```python
from app.db import Message, MessageRole, get_session


async def create_message(session: AsyncSession, **kwargs) -> Message:
    message = Message(role=MessageRole.USER, **kwargs)
    session.add(message)
    await session.commit()  # 提交点由业务决定，依赖不会替你提交
    return message
```

四个设计决定，每个都对应一个具体的坑：

| 决定 | 不这么做会怎样 |
| --- | --- |
| 表结构由 Alembic 管，不用 `create_all()` | 应用启动时偷偷建表，改结构没人知道，迁移无从谈起 |
| 关系全部 `lazy="raise"` | 异步里惰性加载抛 `MissingGreenlet`，报错栈完全看不出哪行触发 |
| `get_session` 不自动提交 | 流式对话要分多次提交，统一提交语义对不上；且"忘写 commit"会变成"看起来成功了" |
| `expire_on_commit=False` | commit 后属性过期，再访问触发同步 IO，在 async 里直接炸 |

`tests/test_db_contract.py` 会连真实库比对 `information_schema`：表、列、物理类型、索引、约束名逐项核对。**它当场抓到过 6 处漂移**——三个 `deleted_at` 漏了 `DATETIME(3)`、两个累计 token 写成了 `INT` 而库里是 `BIGINT`、`refresh_token_hash` 模型写 `VARCHAR` 库里是 `CHAR`。这类不一致单测看不出来，只会在上线后炸。

数据库没起时这些测试自动 skip，不会阻塞本地跑单测。

### 迁移（alembic/）

表结构**只由 Alembic 定义**。`deploy` 的容器启动时不再建表 —— 两处都写 DDL 必然漂移，实测就在列注释和表达式索引上对不上。

```powershell
alembic revision --autogenerate -m "add xxx"   # 改完模型后生成迁移，务必人工过一遍
alembic upgrade head                            # 应用
alembic downgrade -1                            # 回滚一步
alembic current                                 # 当前版本
alembic check                                   # 模型与库有无差异，CI 里跑这个
```

三个容易踩的地方：

1. **`alembic.ini` 必须保持纯 ASCII**。configparser 用系统 locale 编码读它，Windows 上是 GBK，写中文注释会直接 `UnicodeDecodeError`，报错还指向 `alembic/config.py`，跟注释毫无关系。
2. **迁移文件不能 import 应用代码**。自定义 `TypeDecorator` 默认会被渲染成 `app.db.types.IntEnumValue()`，模型一改名历史迁移就跑不起来。`env.py` 里的 `render_item` 把它退化成物理类型（`TINYINT UNSIGNED` / `VARCHAR`）。
3. **表达式索引比不了**。`idx_conv_user_recent` 建在 `(user_id, last_message_at DESC)` 上，DESC 是表达式不是列，Alembic 只能生成"近似签名"，每次 check 都误报。已在 `include_object` 里排除，代价是这个索引的变更要手写迁移。

另外两处工程细节：

- `script.py.mako` 已改成本项目的风格（`str | Sequence[str] | None`、import 排序），这样 `alembic revision` 生成出来的文件直接过 `ruff`，不用每次手动修。
- `alembic/versions/*.py` 在 `pyproject.toml` 里只放行了 `F401`（空迁移必然带入未使用的 `sa` / `op`）和 `I001`（模板占位符为空时多一个空行）两条规则，其余检查照常生效。


## 七、运行

```powershell
conda activate fastapi-learn
cd D:\workspace\python\sr

# 配置密钥（不配也能启动，LLM 接口返回 503）
copy ".env copy.example" .env

# 启动服务
uvicorn app.main:app --reload --port 8000
```

| 地址 | 说明 |
| --- | --- |
| http://127.0.0.1:8000/docs | Swagger UI，可直接调试接口（右上角 Authorize 可填令牌） |
| http://127.0.0.1:8000/redoc | ReDoc 文档 |
| http://127.0.0.1:8000/api/v1/health | 存活探针（liveness） |
| http://127.0.0.1:8000/api/v1/health/ready | 就绪探针（readiness） |
| POST http://127.0.0.1:8000/api/v1/auth/verification-code | 发邮箱验证码 |
| POST http://127.0.0.1:8000/api/v1/auth/register | 邮箱注册 |
| POST http://127.0.0.1:8000/api/v1/auth/login | 邮箱密码登录 |
| POST http://127.0.0.1:8000/api/v1/auth/refresh | 刷新令牌 |
| POST http://127.0.0.1:8000/api/v1/auth/logout | 登出 |
| GET http://127.0.0.1:8000/api/v1/auth/me | 当前登录用户 |
| http://127.0.0.1:8000/api/v1/chat/status | 对话功能状态（是否已配置密钥） |
| POST http://127.0.0.1:8000/api/v1/chat | 对话，一次性返回 |
| POST http://127.0.0.1:8000/api/v1/chat/stream | 对话，SSE 流式返回 |

> 端口注意：本机 **8010 被系统保留**，绑定会报 WinError 10013，用 8000 即可。
> 若 8000 被旧进程占用（报 `[Errno 10048]`），先找到并停掉：
> `Get-NetTCPConnection -LocalPort 8000 -State Listen` → `Stop-Process -Id <PID>`。

## 八、测试与静态检查

```powershell
conda activate fastapi-learn
cd D:\workspace\python\sr

pytest                              # 全部用例
ruff check .                        # 静态检查
ruff format --check .               # 格式检查（CI 用）
ruff format .                       # 真正格式化
```

测试用桩链替换 LLM 服务、用桩邮件服务截获验证码（`dependency_overrides[...]`，对应 Spring 的 `@MockBean`），不依赖外部网络，随时可跑。

`tests/test_db_contract.py` 是**契约测试**：连真实库，把 `information_schema` 里的列类型、索引、约束名跟 `Base.metadata` 逐项比对，另外校验运行时账号确实没有 DDL 权限。库没起时自动 skip，起了就一定跑 —— 这是唯一能拦住"模型改了、迁移没生成""权限被悄悄放开"这类漂移的东西。

鉴权相关用例需要 MySQL + Redis：注册要真写库（唯一约束、事务边界、级联删除用假的测不出来），验证码要真 TTL 和真 `INCR`。跑在**独立的 Redis db 15** 上，用 `SCAN` + `DEL` 清理（`FLUSHDB` 属于 `@dangerous`，被 ACL 拒），MySQL 侧只删邮箱前缀为 `sr-test-` 的行，不碰开发数据。任一后端不可达时整组 skip。

> 注意：如果清理 Redis 失败，测试会**直接退出**而不是继续跑。否则配额计数会跨轮次累积，表现为"单跑能过、连着跑就 429"，极难定位。

## 九、接口契约

### 统一响应结构

所有 JSON 接口（含健康检查）都是同一个外壳：

```json
{"code": 0, "message": "ok", "data": {"status": "ok", "version": "0.1.0"}, "trace_id": "9f2c..."}
{"code": 22502, "message": "LLM 上游服务异常：RuntimeError", "data": null, "trace_id": "9f2c..."}
```

**HTTP 状态码仍然是真实的**（502 就是 502），不是"一律 200、错误码放 body"。三个理由：

1. 网关、监控、告警、日志采集都按状态码统计错误率，全 200 会让现成的可观测性工具全部失效；
2. 浏览器、CDN、重试中间件依赖状态码语义；
3. SSE 是事件流，套不了 envelope —— 两套语义并存只会更乱。

所以分工是：**状态码表达协议层结果，`code` 表达业务原因**。

`trace_id` 每个请求一个，回写到响应头 `X-Request-ID`。客户端带了就沿用，便于前端/网关串起同一条链路。

### 错误码

5 位十进制：`X M SSS`

| 位 | 含义 |
| --- | --- |
| `X` | 错误归属：`1` 调用方问题，`2` 服务端/依赖问题 |
| `M` | 模块：`0` 通用，`1` 鉴权，`2` 对话，`3` 数据 |
| `SSS` | HTTP 状态码 |

把状态码编进错误码，是为了排障时一眼看懂：`22502` = 对话模块、服务端问题、上游 502，不用翻对照表。前端也能只看首位决定"该提示用户还是该重试"。

| code | HTTP | 含义 |
| --- | --- | --- |
| `0` | 200 | 成功 |
| `10400` | 400 | 请求参数不合法 |
| `10404` | 404 | 资源不存在 |
| `10405` | 405 | 请求方法不支持 |
| `10422` | 422 | 参数校验失败（`data.errors` 里逐字段说明） |
| `10429` | 429 | 请求过于频繁 |
| `10500` | 500 | 服务内部异常 |
| `11400` | 400 | 验证码不正确或已过期 |
| `11401` | 401 | 未登录 / 登录已失效 / 邮箱或密码不正确 |
| `11403` | 403 | 账号被停用 |
| `11409` | 409 | 该邮箱已被注册 |
| `11429` | 429 | 发码或试错过于频繁 |
| `21502` | 502 | 验证码邮件发送失败 |
| `21503` | 503 | 鉴权功能未配置（缺 JWT_SECRET / SMTP / Redis） |
| `22502` | 502 | LLM 上游异常 |
| `22503` | 503 | LLM 未配置 |
| `22504` | 504 | LLM 上游超时 |

`11401` 同时覆盖"令牌失效""密码错"和"邮箱不存在"，是刻意的：这三种情况客户端动作完全一样（回登录页），区分只会泄露实现细节。同理 `11400` 不区分"码错"和"码过期"。

### 异常怎么走

| 异常 | 谁接 | 结果 |
| --- | --- | --- |
| `AppError` 子类 | `handle_app_error` | 状态码与文案由错误码决定，不记堆栈 |
| `RequestValidationError` | `handle_validation_error` | 422 + 逐字段错误 |
| `HTTPException`（框架抛的 404/405） | `handle_http_exception` | 按状态码映射业务码 |
| 其他 `Exception` | `handle_unexpected_error` | 500 + 堆栈进日志，**响应里绝不带原文** |

所以业务代码里不写 `HTTPException`，直接抛 `LLMUpstreamError(...)` 之类即可 —— 路由层看不到一个状态码。

**兜底 handler 必须存在**：没有它，未预期异常会走 Starlette 默认的纯文本 500，既不是统一结构，DEBUG 模式下还会把堆栈直接返回给客户端。

### 鉴权（/api/v1/auth）

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

**登录接口的三个防枚举细节**（`tests/test_auth.py` 逐条锁住）

1. 邮箱不存在和密码错误返回**完全相同**的响应 —— 状态码、错误码、文案都一致。
2. 邮箱不存在时也跑一次 argon2 校验（`dummy_hash()`），把耗时拉平。不做这一步，登录接口就是个**时序预言机**：邮箱存在 50ms、不存在 1ms，不看响应内容、只看耗时就能批量探测哪些邮箱注册过。
3. 账号状态检查放在密码校验**之后**。反过来，"账号已被停用"这句话本身就泄露了账号存在。

**注册为什么"先消费验证码，再查邮箱是否已注册"**

反过来的话，不掌握该邮箱的人也能通过"是否报邮箱已占用"批量探测注册情况。先验证码后查重，攻击者必须先能收到邮件才问得出答案。

**密码策略：8–128 位，不限字符类型。**

这不是偷懒，是 NIST SP 800-63B 的建议：强制大小写数字符号混排会把人逼向 `Passw0rd!` 这类可预测变形，实际熵反而下降；长度才直接决定搜索空间。真正的防护是限制尝试次数，本项目在登录（验证码试错上限）和发码（三层配额）两侧都有。

上限 128 是防 DoS：密码长度直接进哈希函数的计算量，不设上限时一个 1MB 的"密码"能占满一个核。

**密码哈希用 argon2id，bcrypt 只保留校验能力。** 登录时按密文前缀分流，旧算法验过之后顺手把新密文写回库 —— 用户无感，也不需要批量迁移脚本。不用 passlib 是因为它依赖 Python 3.13 已移除的 `crypt` 模块，import 阶段就炸。

### 验证码：邮箱验证码自建，人机验证先不接

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

### 防 SQL 注入：四层，参数绑定只是第一层

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

### 数据库账号权限（防注入的第二道墙）

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

### 对话（/api/v1/chat，LangChain LCEL 链）

| 方法 | 路径 | 语义 | 失败状态码 |
| --- | --- | --- | --- |
| POST | `/chat` | 一次性返回，适合短回答与服务端调用 | 503 未配置、502 上游异常、422 校验失败 |
| POST | `/chat/stream` | SSE 流式返回（`text/event-stream`） | 流开始前：503 / 422；流开始后：error 事件 |
| GET | `/chat/status` | 配置状态（运维自检，不打真网络） | — |

### SSE 帧格式

| event | data | 说明 |
| --- | --- | --- |
| `delta` | `{"delta":"增量文本"}` | 模型增量输出，客户端顺序拼接即得完整回答 |
| `meta` | `{"model":"...","prompt_tokens":157,"completion_tokens":4}` | 结束前给出实际模型与用量 |
| `done` | `[DONE]` | 结束哨兵，收到后停止读取 |
| `error` | `{"code":22502,"message":"..."}` | 流中途失败。此时响应头已发出，只能靠事件传错误 |
| 注释行 | `: ping - <时间>` | 心跳，空闲超过 `SSE_PING_SECONDS` 时发出，客户端忽略 |

实测报文：

```
event: delta
data: {"delta":"。"}

event: meta
data: {"model":"deepseek-flash","prompt_tokens":157,"completion_tokens":4}

event: done
data: [DONE]

```

### 为什么用 astream_events 而不是 astream

`astream()` 只给**最终输出的文本块**——链路中间发生了什么、这次用了多少 token，一概拿不到。`astream_events(version="v2")` 把链上每一跳都暴露成事件：

| LangChain 事件 | 我们怎么用 |
| --- | --- |
| `on_chat_model_stream` | 取 `chunk.content` → `delta` 帧（空块丢弃） |
| `on_chat_model_end` | 取 `usage_metadata` / `model_name` → `meta` 帧 |
| `on_chain_*` / `on_parser_*` | 忽略（纯文本场景用不上） |

换来的是：用量可统计、后续接工具调用/检索步骤时不用换协议。代价是**事件量更大**（每个 token 会有模型、解析器、子链三条事件），所以必须按 `event` 名过滤，只透传业务需要的部分——不能把 `astream_events` 的原始字典直接扔给客户端。

实测发现的两个细节：

- reasoning 类模型的 `on_chat_model_stream` 会大量出现 `content == ""`（推理增量不走 content），必须过滤空块，否则客户端收到一堆空帧。
- 真正的文本在 `on_parser_stream` 里也有；我们取 `on_chat_model_stream`，是因为换掉解析器（结构化输出等）后它仍然有效。

服务层不把 LangChain 的事件字典往上抛：`astream_chat()` 产出的是自己的 `TokenDelta` / `StreamFinished`，由路由映射成 SSE 帧。否则 LangChain 一升级，路由层跟着全改。

### SSE 为什么用 sse-starlette

FastAPI / Starlette 内置只有通用的 `StreamingResponse`，**没有 SSE 语义**（`starlette.responses` 里没有任何 EventSource 类）。SSE 的帧编码、ping 心跳、断线取消、停机收尾都是协议层通用问题，交给 `sse-starlette` 的 `EventSourceResponse`：

| 能力 | 谁负责 |
| --- | --- |
| 帧编码、多行 `data:` 拆分 | `sse-starlette` |
| ping 心跳（`ping=` 参数） | `sse-starlette` |
| 客户端断线检测与取消 | `sse-starlette` |
| 停机时通知在途流收尾（`shutdown_event`） | `sse-starlette` + `main.py` 的 lifespan |
| 响应头（`Cache-Control: no-store`、`X-Accel-Buffering: no`） | `sse-starlette` |
| 事件命名（`delta`/`done`/`error`）、错误分通道 | 本项目（`core/sse.py` + 路由） |

三个容易踩的细节：

1. **dict 必须用 `JSONServerSentEvent`**：普通的 `ServerSentEvent` 对 dict 走 `str()`，会产出 `data: {'code': 502}` 这种单引号 Python repr，客户端 `JSON.parse` 直接失败。这个坑只有把报文打出来看才会发现。
2. **错误有两个通道**：配置缺失这类前置条件在响应头下发前抛 `AppError`，客户端拿到真实的 503；而流开始后的上游故障只能发 `event=error`，此时 HTTP 状态码已经是 200，改不了。
3. **停机要能收尾**：`main.py` 的 lifespan 在关闭时 `set()` 一个 `anyio.Event` 并传给 `EventSourceResponse`，在途的流会收到通知后主动结束，而不是被进程退出硬切断——否则客户端只看到连接莫名断开。

### 健康检查（/api/v1/health）

| 路径 | 语义 | 失败后果 |
| --- | --- | --- |
| `/health` | liveness：进程活着、装配正常，不查外部依赖 | 编排系统重启容器 |
| `/health/ready` | readiness：`ready` / `degraded`，检查 LLM 是否配置 | 只摘流量，不重启 |

两者不能合并成一个：合并会让"依赖抖动"触发实例重启，把小故障放大成雪崩。LLM 未配置时 readiness 返回 `degraded` 而非失败 —— 服务仍能提供健康检查等能力，对话接口明确返回 503。

### 配置（.env）

`LLM_API_KEY`（不填 = 功能关闭，主服务不受影响）、`LLM_BASE_URL`（OpenAI 兼容网关）、`LLM_MODEL`、`LLM_TIMEOUT_SECONDS`、`LLM_MAX_RETRIES`、`LLM_TEMPERATURE`、`SSE_PING_SECONDS`、`PROMPT_CHAT_SYSTEM_ID`（用哪份提示词）。提示词正文不在配置里，在 `app/prompts/files/*.md`。

安全要点：历史消息 role 白名单只允许 `user`/`assistant` —— 客户端伪造 `system` 角色会被 422 拦截（防提示注入）；系统提示词仅由服务端配置注入。

```powershell
# 未配置密钥时
curl.exe http://127.0.0.1:8000/api/v1/chat/status
# {"code":0,"message":"ok","data":{"configured":false,...,"prompt_id":"chat_system"},"trace_id":"..."}

# 流式对话（-N 关闭 curl 自身缓冲，才能看到逐块输出）
curl.exe -N -X POST http://127.0.0.1:8000/api/v1/chat/stream -H "Content-Type: application/json" -d "{\"message\":\"你好\"}"
```

### 链的结构

```
prompt | model | parser
```

- `prompt`：`ChatPromptTemplate`，system 用 `{system_prompt}` **运行时占位**，另加 `{history}`、`{message}`
- `model`：`ChatOpenAI`（OpenAI 兼容协议，可指向 DeepSeek / one-api 等网关）
- `parser`：`StrOutputParser`，把 `AIMessage` 转成纯文本

同一套链同时支持 `invoke()`（一次性）与 `astream_events()`（流式），换模型只改工厂一行。

> system 用占位符而不是建链时写死，是为了让提示词可热更新：改文件不影响已建好的链和 httpx 连接池，下一次请求自动用新内容。

### 提示词管理（app/prompts/）

提示词是**数据**，不是代码常量。正文放 Markdown 文件，元数据写在 front matter：

```markdown
---
id: chat_system
version: 1
description: 对话场景的系统提示词（角色 / 约束 / 风格）
---
# 角色
...
```

| 文件 | 作用 |
| --- | --- |
| `prompts/files/chat_system.md` | 提示词正文 + `id` / `version` / `description` |
| `prompts/loader.py` | 解析 front matter 并校验，不合法直接抛错 |
| `prompts/registry.py` | 按 id 取用，按文件 mtime 判断是否重载 |

三条硬规则（都在 `loader.py` 里强制执行）：

1. **`id` 必须等于文件名** —— 否则改名之后引用就错位了。
2. **`version` 必须是正整数** —— 版本是排障的锚点，不能写成 `v2`、`latest`。
3. **正文不能为空** —— 空提示词会让模型行为完全失控。

启动时加载失败会直接让进程起不来（快速失败）；运行期重载失败则**保留旧版本**并记 error 日志——宁可继续用旧提示词，也不能因为一次手误让线上进入"无提示词"状态。

改提示词不用重启：

```powershell
# 1. 改 app/prompts/files/chat_system.md（顺手把 version +1）
# 2. 下一次请求自动生效，可查证
curl.exe http://127.0.0.1:8000/api/v1/chat/status
# {"prompt_id":"chat_system","prompt_version":2,...}
```

**为什么带 version**：线上答歪了，你要能回答"这条请求用的是哪版提示词、哪个模型"。`/chat/status` 暴露当前版本，服务日志里每条对话都记 `prompt id / version / checksum`——这三个字段是事后回溯的唯一线索。

**为什么合并成一条 system prompt**：之前拆成 `system` + `assistant` 两段再拼接，是生造的分类。业界按用途分（聊天/抽取/分类），一个聊天场景就是一条 system prompt，角色、约束、风格写在里面用标题分段。两段拼接的隐患是**指令冲突时谁赢不确定**——system 说"简洁"、assistant 说"展开讲"，模型行为会飘。

新增提示词：放一个新 `.md` 到 `files/`，在 `registry.py` 的 `PromptKey` 加一行，然后 `PROMPT_CHAT_SYSTEM_ID` 指过去即可。

---

## 十、下一步可以练什么

按由易到难（统一返回、异常处理、错误码、表结构、迁移、仓库层、鉴权已经做完了，下面接在后面）：

1. **SSE 断线续传**：给每帧带 `id:`，客户端重连时用 `Last-Event-ID` 续传，对应 SSE 规范里的重连语义。
2. **LLM 结构化输出**：`.with_structured_output(PydanticModel)` 替换 `StrOutputParser`，做实体抽取。
3. **对话接口挂鉴权**：`/api/v1/chat` 现在不要求登录，接上 `CurrentUser` 后把 `user_id` 从令牌里取，`conversations` / `messages` 才有真正的归属。
4. **流式落库**：对话时先写一条 `status=STREAMING` 的 `Message`，流结束改 `COMPLETE`；Redis 做增量缓冲，断线重连能从 DB 续上。
5. **就绪探针加 DB / Redis**：现在 `/health/ready` 只看 LLM 配置，应该真跑一次 `SELECT 1` 和 `PING`，失败返回 503。
6. **登录失败次数限制**：验证码侧有三层限流，密码登录侧还没有 —— 现在只靠 argon2 的计算成本，没有按账号/IP 的失败计数与锁定。
7. **忘记密码**：`VerificationScene` 加一个 `RESET_PASSWORD`（Redis key 自动隔离），复用同一套发码与校验，再加一个 `POST /auth/reset-password`。表里再加 `email_verified_at` 就能支撑"改邮箱需重新验证"。
8. **人机验证**：真要接就挂 `VerificationService.issue()` 最前面（在消耗配额之前校验票据），见上一节的选择表。
9. **加 RAG**：文档切分 → 向量库（如 pgvector / Chroma）→ retriever 接进链，`prompt | retriever` 组合。

