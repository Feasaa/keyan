# 架构与分层约定

为什么这样分目录、请求怎么流转、Java 习惯与 Python 规范的对应关系。

> 返回 [README](../README.md) · 下一篇 [环境准备与运行](02-getting-started.md)

---

## 先看总对照表

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
| `pom.xml` / `build.gradle` | `requirements.txt` + `requirements-dev.txt`（`pyproject.toml` 只放工具配置） | 依赖与构建 |
| `src/test/java` + `@SpringBootTest` + `MockMvc` | `tests/` + `TestClient` | 测试 |
| `@MockBean` | `app.dependency_overrides[...]` | 测试替换实现 |
| profile（`--spring.profiles.active`） | `.env` / 环境变量 + `DEBUG` | 多环境 |

---

## 目录结构（每个文件夹为什么存在）

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
├── deploy/                     # 容器侧配置（只有配置文件，没有应用代码）
│   ├── mysql/conf.d/01-server.cnf  # 字符集 / 时区 / 慢查询 / sql_mode
│   ├── mysql/initdb/10-grants.sh   # 账号收权：运行时只给 DML，DDL 归迁移账号
│   └── redis/redis.conf        # AOF / volatile-lru / ACL 收权
├── docs/                       # 文档（README 只做索引，细节按主题分篇）
│   ├── 01-architecture.md      # 目录分层、请求流转、Java↔Python 对照
│   ├── 02-getting-started.md   # 环境准备、容器、配置、运行、测试
│   ├── 03-data-layer.md        # 雪花 ID / 仓储 / Alembic
│   ├── 04-api-contract.md      # 统一响应 / 错误码 / SSE / 探针 / 提示词
│   ├── 05-auth.md              # 双令牌 / 验证码 / 防注入 / 账号权限
│   └── 06-roadmap.md           # 后续练习方向
├── docker-compose.yml          # MySQL 8.4 LTS + Redis 7.4（必须在根目录，见 02）
├── environment.yml             # conda 环境：只声明解释器，用 -r 引用 requirements
├── requirements.txt            # 运行时依赖（唯一版本来源）
├── requirements-dev.txt        # 开发/测试依赖（-r requirements.txt）
├── pyproject.toml              # 项目元信息 + pytest / ruff 配置
├── .env.example                # 唯一配置模板（应用与 compose 共用）
├── .gitignore
└── .gitattributes              # 统一 LF（容器挂载的 .cnf/.conf 对 CRLF 敏感）
```

### 各层存在的理由（按依赖顺序）

**`app/`** — 所有代码收在一个包内。Spring 的组件扫描只扫启动类所在包及子包，代码放到根包外就注入不了；Python 里则是 `uvicorn app.main:app` 找不到模块。两者本质相同：**必须在约定的根包之下**。

**`app/core/`** — 配置、日志、安全、异常处理这些东西每个项目都要，但不属于任何业务。混在业务包里会导致：换环境时要全局搜索改代码；想复用到下一个项目得连业务一起搬。所以单独成层，业务只"使用"它。

**`app/api/`** — 只做 HTTP 协议转换：收参数 → 调 service → 返回。**禁止写业务规则**，因为同一段业务还要被定时任务、消息消费者调用，写在接口层就被绑死在 HTTP 上。对应 Spring 里"Controller 不写业务"的铁律。

**为什么叫 `api/routes/` 而不是 `controllers/`** — 这是 FastAPI 生态的命名惯例（官方教程用 `routers/`，官方模板用 `api/routes/`），`controllers` 是 Java/Spring 的词。跟着生态走，别人接手时不用先猜你的目录约定。

**版本号只留在 URL 里** — `/api/v1` 是**对外契约**，不兼容变更只能 v1、v2 并存；但它不该用目录嵌套表达。目录结构属于内部实现，哪天把 `routes/` 拆成 `routes/admin/`、`routes/open/`，URL 不该跟着变。所以这里是 `api/routes/chat.py` + `API_PREFIX=/api/v1`，而不是 `api/v1/endpoints/chat.py`。

**`app/schemas/`** — 出入参契约。独立于任何存储结构：接口返回常常是拼装结果，且内部字段（密钥、审计字段）绝不能返回给前端。对应 Spring 的 DTO/VO 分层。

**`app/repositories/`** — SQL 的唯一落点，一表一仓库。存在的意义有两个：一是"防注入"这条规则只需要在一个地方守住（见[防 SQL 注入](05-auth.md#防-sql-注入四层参数绑定只是第一层)）；二是软删除过滤、字段白名单这类**漏一次就是漏洞**的细节，写进仓储就不会被某个调用方忘掉。对应 `@Repository`。

**`app/services/`** — 业务规则的唯一归宿：可复用、可单测（不依赖 HTTP）、隔离框架（换 Web 框架不用重写业务）。LLM 的链构建、降级、超时重试，以及注册/登录的事务边界，全部收敛在这里，接口层只做协议适配。对应 `@Service`。

**`tests/`** — 测试锁住的是**接口契约**而不是实现。LLM 用桩链、邮件用桩服务替换，不花钱、不依赖网络、结果可断言。

---

## 请求流转对比

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

## 几个必须知道的写法差异

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

细节见[统一响应结构](04-api-contract.md#统一响应结构)和[异常怎么走](04-api-contract.md#异常怎么走)。

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
