# learn-fastapi（AI / LLM 服务骨架）

FastAPI + LangChain 的分层服务骨架。分层方式刻意对齐 Spring Boot 的习惯，如果你有 Spring 基础，可以按"同一职责、不同写法"来读。

当前只保留 **LLM 对话** 与 **健康检查** 两条链路：没有数据库、没有业务 CRUD，专注 AI 能力的接入方式。

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
| `@ControllerAdvice` + `@ExceptionHandler` | endpoint 内的 `try/except` + `HTTPException` | 统一异常映射 |
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
│   ├── core/                   # 基础设施：配置、SSE 工具（与业务无关）
│   │   ├── config.py           # 全局配置（≈ application.yml + @ConfigurationProperties）
│   │   └── sse.py              # SSE 事件名与结束哨兵（帧编码交给 sse-starlette）
│   ├── prompts/                # 提示词（正文是文件，不是代码常量）
│   │   ├── files/chat_system.md  # 提示词正文 + 元数据（id / version / description）
│   │   ├── loader.py           # 加载与校验（不合法启动即失败）
│   │   └── registry.py         # 按 id 取用 + 按 mtime 热更新
│   ├── api/                    # 接口层（FastAPI 惯例）
│   │   ├── __init__.py         # 只导出 api_router
│   │   ├── router.py           # 路由聚合（≈ 组件扫描的"手写替代品"）
│   │   └── routes/             # 一个资源一个文件
│   │       ├── health.py       # 存活探针 + 就绪探针
│   │       └── chat.py         # 一次性对话 + SSE 流式对话 + 状态
│   ├── schemas/                # 接口契约 DTO（≈ dto/vo 包）
│   │   ├── health.py
│   │   └── chat.py
│   ├── services/               # 业务逻辑（≈ @Service）
│   │   ├── exceptions.py       # 业务异常语义
│   │   └── llm_service.py      # LangChain LCEL 链封装（invoke / astream_events）
│   └── utils/                  # 通用工具（≈ common/util 包，当前为空）
├── tests/                      # 测试（≈ src/test/java）
│   ├── conftest.py             # TestClient + LLM 桩夹具
│   ├── test_health.py
│   ├── test_chat.py
│   └── test_prompts.py
├── deploy/                     # 本地依赖编排（Docker，不参与应用打包）
│   ├── docker-compose.yml      # MySQL 8.4 LTS + Redis 7.4
│   ├── .env.example            # 复制成 deploy/.env 后填密码
│   ├── mysql/
│   │   ├── conf.d/01-server.cnf  # 字符集 / 时区 / 慢查询 / sql_mode
│   │   └── init/01-schema.sql    # 首次启动自动建表
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

**`app/services/`** — 业务规则的唯一归宿：可复用、可单测（不依赖 HTTP）、隔离框架（换 Web 框架不用重写业务）。LLM 的链构建、降级、超时重试全部收敛在这里，接口层只做状态码翻译。对应 `@Service`。

**`tests/`** — 测试锁住的是**接口契约**而不是实现。LLM 用桩链替换，不花钱、不依赖网络、结果可断言。

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

service 层**不抛 HTTP 异常**，只抛业务异常（`LLMNotConfiguredError` / `LLMUpstreamError`）；由接口层翻译成 `503` / `502`。原因：service 不该知道 HTTP 语义，否则换框架就要重写业务。这对应 Spring 里"Service 抛业务异常，由 `@ControllerAdvice` 统一映射成状态码"的分工。

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
├── mysql/
│   ├── conf.d/01-server.cnf  # 字符集 / 时区 / 慢查询 / sql_mode
│   └── init/01-schema.sql    # 首次启动自动建表
└── redis/
    └── redis.conf            # AOF + volatile-lru + ACL 收权
```

```powershell
cd D:\workspace\python\sr\deploy
cp .env.example .env           # 改掉里面的三个密码
docker compose up -d           # 首次启动会自动执行 init/01-schema.sql
docker compose ps              # 等到两个服务都 healthy
```

三个容易踩的点：

- **init 脚本只在数据卷为空时执行一次**。改了 `01-schema.sql` 想重来，必须 `docker compose down -v` 把卷删掉——`-v` 会连数据一起删，生产别这么干，改结构走迁移。
- **MySQL 8.4 默认认证插件是 `caching_sha2_password`**，PyMySQL / aiomysql 需要装 `cryptography` 才能握手，否则报 `RuntimeError: 'cryptography' package is required`。8.4 已经移除了 `--default-authentication-plugin`，不要指望退回 `mysql_native_password`。
- **雪花 ID 出接口必须转字符串**。`BIGINT` 超过 2^53，前端 `JSON.parse` 会把末几位变成 0，拿到的 ID 直接查不到记录。Pydantic 里用 `field_serializer` 或直接声明 `str`。

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
| http://127.0.0.1:8000/docs | Swagger UI，可直接调试接口 |
| http://127.0.0.1:8000/redoc | ReDoc 文档 |
| http://127.0.0.1:8000/api/v1/health | 存活探针（liveness） |
| http://127.0.0.1:8000/api/v1/health/ready | 就绪探针（readiness） |
| http://127.0.0.1:8000/api/v1/chat/status | 对话功能状态（是否已配置密钥） |
| POST http://127.0.0.1:8000/api/v1/chat | 对话，一次性返回 |
| POST http://127.0.0.1:8000/api/v1/chat/stream | 对话，SSE 流式返回 |

> 端口注意：本机 **8010 被系统保留**，绑定会报 WinError 10013，用 8000 即可。
> 若 8000 被旧进程占用（报 `[Errno 10048]`），先找到并停掉：
> `Get-NetTCPConnection -LocalPort 8000 -State Listen` → `Stop-Process -Id <PID>`。

## 八、测试

```powershell
conda activate fastapi-learn
cd D:\workspace\python\sr
pytest
```

测试用桩链替换 LLM 服务（`dependency_overrides[get_llm_service]`，对应 Spring 的 `@MockBean`），不依赖外部网络，随时可跑。

## 九、接口契约

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
| `error` | `{"code":502,"message":"..."}` | 流中途失败。此时响应头已发出，只能靠事件传错误 |
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
2. **错误有两个通道**：配置缺失这类前置条件在响应头下发前抛 `HTTPException`，客户端拿到真实的 503；而流开始后的上游故障只能发 `event=error`，此时 HTTP 状态码已经是 200，改不了。
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
# {"configured":false,...,"prompt_id":"chat_system","prompt_version":1}

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

按由易到难：

1. **SSE 断线续传**：给每帧带 `id:`，客户端重连时用 `Last-Event-ID` 续传，对应 SSE 规范里的重连语义。
2. **LLM 结构化输出**：`.with_structured_output(PydanticModel)` 替换 `StrOutputParser`，做实体抽取。
3. **加会话记忆**：用 `RunnableWithMessageHistory` 或外部存储，把 history 从请求体搬到服务端。
4. **加统一异常处理**：写 `@app.exception_handler`，对应 `@ControllerAdvice`，把业务异常集中映射成状态码。
5. **加鉴权**：`OAuth2PasswordBearer` + JWT，用 `Depends` 做"当前用户"依赖，对应 Spring Security 的 Filter + `@AuthenticationPrincipal`。
6. **加 RAG**：文档切分 → 向量库（如 pgvector / Chroma）→ retriever 接进链，`prompt | retriever` 组合。
