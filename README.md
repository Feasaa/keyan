# learn-fastapi（AI / LLM 服务骨架）

FastAPI + LangChain 的分层服务骨架。分层方式刻意对齐 Spring Boot 的习惯，如果你有 Spring 基础，可以按"同一职责、不同写法"来读。

当前只保留 **LLM 对话** 与 **健康检查** 两条链路：没有数据库、没有业务 CRUD，专注 AI 能力的接入方式。

---

## 一、先看总对照表

| Spring Boot | 本项目（FastAPI） | 职责 |
| --- | --- | --- |
| `DemoApplication.java`（`@SpringBootApplication`） | `app/main.py` 的 `create_app()` | 装配应用：中间件、路由、生命周期 |
| `SpringApplication.run(...)` | `app = create_app()` | 启动入口 |
| `@RestController` | `app/controllers/*.py` 里的 `router` | 暴露 HTTP 接口 |
| `@RequestMapping("/api/v1")` | `settings.API_PREFIX` + `app/router.py` | 统一前缀与版本分组 |
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
│   │   └── sse.py              # SSE 编码 + 心跳（与具体业务解耦，可复用）
│   ├── prompts/                # 提示词（与代码分离，便于评审调优）
│   │   ├── system.py           # 系统提示词：角色与硬约束
│   │   ├── assistant.py        # 助理提示词：人设与语气
│   │   └── registry.py         # 按名字取提示词 + 拼接工具
│   ├── controllers/            # 控制器（≈ controller 包）
│   │   ├── health.py           # 存活探针 + 就绪探针
│   │   └── chat.py             # 一次性对话 + SSE 流式对话 + 状态
│   ├── router.py               # 路由聚合（≈ 组件扫描的"手写替代品"）
│   ├── schemas/                # 接口契约 DTO（≈ dto/vo 包）
│   │   ├── health.py
│   │   └── chat.py
│   ├── services/               # 业务逻辑（≈ @Service）
│   │   ├── exceptions.py       # 业务异常语义
│   │   └── llm_service.py      # LangChain LCEL 链封装（invoke / astream）
│   └── utils/                  # 通用工具（≈ common/util 包，当前为空）
├── tests/                      # 测试（≈ src/test/java）
│   ├── conftest.py             # TestClient + LLM 桩夹具
│   ├── test_health.py
│   ├── test_chat.py
│   └── test_prompts.py
├── environment.yml             # conda 环境定义
├── requirements.txt            # pip 依赖
├── pyproject.toml              # 项目元信息 + pytest / ruff 配置
├── .env copy.example           # 环境变量模板（复制成 .env 使用）
└── .gitignore
```

### 各层存在的理由（按依赖顺序）

**`app/`** — 所有代码收在一个包内。Spring 的组件扫描只扫启动类所在包及子包，代码放到根包外就注入不了；Python 里则是 `uvicorn app.main:app` 找不到模块。两者本质相同：**必须在约定的根包之下**。

**`app/core/`** — 配置、日志、安全、异常处理这些东西每个项目都要，但不属于任何业务。混在业务包里会导致：换环境时要全局搜索改代码；想复用到下一个项目得连业务一起搬。所以单独成层，业务只"使用"它。

**`app/controllers/`** — 只做 HTTP 协议转换：收参数 → 调 service → 返回。**禁止写业务规则**，因为同一段业务还要被定时任务、消息消费者调用，写在接口层就被绑死在 HTTP 上。对应 Spring 里"Controller 不写业务"的铁律。

**版本号只留在 URL 里** — `/api/v1` 是**对外契约**，不兼容变更只能 v1、v2 并存；但它不该用目录嵌套表达。目录结构属于内部实现，哪天把 `controllers/` 拆成 `controllers/admin/`、`controllers/open/`，URL 不该跟着变。所以这里是 `controllers/chat.py` + `API_PREFIX=/api/v1`，而不是 `api/v1/endpoints/chat.py`。

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
HTTP → Starlette 路由匹配 → APIRouter（controllers）
     → Depends 解析依赖 → Service → 上游 LLM 网关
```

路径：`/api/v1/chat/stream` 由三段拼出来 —— `settings.API_PREFIX` + `router.py` 的 `prefix="/chat"` + 控制器里的 `"/stream"`。资源根路径只在一处定义，改名不会漏。

---

## 四、几个必须知道的写法差异

### 1. 路由不会自动注册

Spring 加了 `@RestController` 就被扫描到；FastAPI 必须在 `app/router.py` 里显式 `include_router`，再在 `main.py` 里挂一次。**漏写就是 404**，而且不报错。这是从 Spring 转过来最容易踩的坑。

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
message: str = Field(min_length=1)     # ≈ @NotBlank
history: list[ChatMessage] = Field(default_factory=list)  # ≈ @Size(max=20)
```

Spring 要写 `@GetMapping @PathVariable @Valid @RequestBody @Operation @ApiResponse` 一堆注解；FastAPI 用 Python 原生类型标注一次表达完，文档自动生成。

### 4. 异常处理

service 层**不抛 HTTP 异常**，只抛业务异常（`LLMNotConfiguredError` / `LLMUpstreamError`）；由接口层翻译成 `503` / `502`。原因：service 不该知道 HTTP 语义，否则换框架就要重写业务。这对应 Spring 里"Service 抛业务异常，由 `@ControllerAdvice` 统一映射成状态码"的分工。

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

## 六、运行

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

## 七、测试

```powershell
conda activate fastapi-learn
cd D:\workspace\python\sr
pytest
```

测试用桩链替换 LLM 服务（`dependency_overrides[get_llm_service]`，对应 Spring 的 `@MockBean`），不依赖外部网络，随时可跑。

## 八、接口契约

### 对话（/api/v1/chat，LangChain LCEL 链）

| 方法 | 路径 | 语义 | 失败状态码 |
| --- | --- | --- | --- |
| POST | `/chat` | 一次性返回，适合短回答与服务端调用 | 503 未配置、502 上游异常、422 校验失败 |
| POST | `/chat/stream` | SSE 流式返回（`text/event-stream`） | 流开始前：503 / 422；流开始后：error 事件 |
| GET | `/chat/status` | 配置状态（运维自检，不打真网络） | — |

### SSE 帧格式

| event | data | 说明 |
| --- | --- | --- |
| `delta` | `{"delta": "增量文本"}` | 模型增量输出，客户端顺序拼接即得完整回答 |
| `done` | `[DONE]` | 结束哨兵，收到后停止读取 |
| `error` | `{"code": 502, "message": "..."}` | 流中途失败。此时响应头已发出，只能靠事件传错误 |
| 注释行 | `: keep-alive` | 心跳，空闲超过 `SSE_HEARTBEAT_SECONDS` 时发出，客户端忽略 |

实测报文：

```
event: delta
data: {"delta": "你好"}

event: delta
data: {"delta": "，"}

event: done
data: [DONE]

```

三个容易踩的工业级细节：

1. **响应头必须关掉缓冲**：`X-Accel-Buffering: no` + `Cache-Control: no-cache, no-transform`，否则 Nginx 会把整条流攒完再下发，流式效果消失。
2. **错误有两个通道**：配置缺失这类前置条件在响应头下发前抛 `HTTPException`，客户端拿到真实的 503；而流开始后的上游故障只能发 `event=error`，此时 HTTP 状态码已经是 200，改不了。
3. **心跳不能靠取消上游**：直接对 `__anext__` 做超时取消会让异步生成器进入不可恢复状态。`core/sse.py` 用独立生产者任务 + 有界队列（背压）实现，消费者只对 `queue.get()` 超时。

### 健康检查（/api/v1/health）

| 路径 | 语义 | 失败后果 |
| --- | --- | --- |
| `/health` | liveness：进程活着、装配正常，不查外部依赖 | 编排系统重启容器 |
| `/health/ready` | readiness：`ready` / `degraded`，检查 LLM 是否配置 | 只摘流量，不重启 |

两者不能合并成一个：合并会让"依赖抖动"触发实例重启，把小故障放大成雪崩。LLM 未配置时 readiness 返回 `degraded` 而非失败 —— 服务仍能提供健康检查等能力，对话接口明确返回 503。

### 配置（.env）

`LLM_API_KEY`（不填 = 功能关闭，主服务不受影响）、`LLM_BASE_URL`（OpenAI 兼容网关）、`LLM_MODEL`、`LLM_TIMEOUT_SECONDS`、`LLM_MAX_RETRIES`、`LLM_TEMPERATURE`、`SSE_HEARTBEAT_SECONDS`。提示词默认取自 `app/prompts/`，可用 `LLM_SYSTEM_PROMPT` / `LLM_ASSISTANT_PROMPT` 覆盖。

安全要点：历史消息 role 白名单只允许 `user`/`assistant` —— 客户端伪造 `system` 角色会被 422 拦截（防提示注入）；系统提示词仅由服务端配置注入。

```powershell
# 未配置密钥时
curl.exe http://127.0.0.1:8000/api/v1/chat/status
# {"configured":false,"model":"deepseek-chat","base_url":""}

# 流式对话（-N 关闭 curl 自身缓冲，才能看到逐块输出）
curl.exe -N -X POST http://127.0.0.1:8000/api/v1/chat/stream -H "Content-Type: application/json" -d "{\"message\":\"你好\"}"
```

### 链的结构

```
prompt | model | parser
```

- `prompt`：`ChatPromptTemplate`，system 提示词 + `{history}` 占位 + `{message}`
- `model`：`ChatOpenAI`（OpenAI 兼容协议，可指向 DeepSeek / one-api 等网关）
- `parser`：`StrOutputParser`，把 `AIMessage` 转成纯文本

同一套链同时支持 `invoke()`（一次性）与 `astream()`（流式），换模型只改工厂一行。

### 提示词管理（app/prompts/）

提示词不写在业务代码里，而是独立成包，`config.py` 导入其默认值：

| 文件 | 内容 | 作用 |
| --- | --- | --- |
| `prompts/system.py` | `SYSTEM_PROMPT` | 角色定位与硬约束（"不能做什么"） |
| `prompts/assistant.py` | `ASSISTANT_PROMPT` | 人设、语气、输出风格（"以什么口吻回答"） |
| `prompts/registry.py` | `PROMPTS` / `get_prompt()` / `compose_system_prompt()` | 按名字取提示词、多段拼接 |

```python
# 新增提示词：在 PromptKey 与 PROMPTS 各加一行即可
from app.prompts import PromptKey, get_prompt

extract_prompt = get_prompt(PromptKey.SYSTEM)
```

链路里两段提示词由 `compose_system_prompt()` 合成一条 system 消息，客户端无法覆盖；`.env` 中同名变量可临时覆盖，留空则用代码内定义。

---

## 九、下一步可以练什么

按由易到难：

1. **SSE 断线续传**：给每帧带 `id:`，客户端重连时用 `Last-Event-ID` 续传，对应 SSE 规范里的重连语义。
2. **LLM 结构化输出**：`.with_structured_output(PydanticModel)` 替换 `StrOutputParser`，做实体抽取。
3. **加会话记忆**：用 `RunnableWithMessageHistory` 或外部存储，把 history 从请求体搬到服务端。
4. **加统一异常处理**：写 `@app.exception_handler`，对应 `@ControllerAdvice`，把业务异常集中映射成状态码。
5. **加鉴权**：`OAuth2PasswordBearer` + JWT，用 `Depends` 做"当前用户"依赖，对应 Spring Security 的 Filter + `@AuthenticationPrincipal`。
6. **加 RAG**：文档切分 → 向量库（如 pgvector / Chroma）→ retriever 接进链，`prompt | retriever` 组合。
