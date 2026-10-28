# 接口契约

统一响应外壳、错误码、对话与 SSE 流、健康探针、提示词热更新。

> 返回 [README](../README.md) · 上一篇 [数据层：雪花 ID / 仓储 / 迁移](03-data-layer.md) · 下一篇 [鉴权与安全](05-auth.md)

---

## 统一响应结构

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

## 错误码

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

## 异常怎么走

| 异常 | 谁接 | 结果 |
| --- | --- | --- |
| `AppError` 子类 | `handle_app_error` | 状态码与文案由错误码决定，不记堆栈 |
| `RequestValidationError` | `handle_validation_error` | 422 + 逐字段错误 |
| `HTTPException`（框架抛的 404/405） | `handle_http_exception` | 按状态码映射业务码 |
| 其他 `Exception` | `handle_unexpected_error` | 500 + 堆栈进日志，**响应里绝不带原文** |

所以业务代码里不写 `HTTPException`，直接抛 `LLMUpstreamError(...)` 之类即可 —— 路由层看不到一个状态码。

**兜底 handler 必须存在**：没有它，未预期异常会走 Starlette 默认的纯文本 500，既不是统一结构，DEBUG 模式下还会把堆栈直接返回给客户端。

## 对话（/api/v1/chat，LangChain LCEL 链）

| 方法 | 路径 | 语义 | 失败状态码 |
| --- | --- | --- | --- |
| POST | `/chat` | 一次性返回，适合短回答与服务端调用 | 503 未配置、502 上游异常、422 校验失败 |
| POST | `/chat/stream` | SSE 流式返回（`text/event-stream`） | 流开始前：503 / 422；流开始后：error 事件 |
| GET | `/chat/status` | 配置状态（运维自检，不打真网络） | — |

## SSE 帧格式

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

## 为什么用 astream_events 而不是 astream

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

## SSE 为什么用 sse-starlette

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

## 健康检查（/api/v1/health）

| 路径 | 语义 | 失败后果 |
| --- | --- | --- |
| `/health` | liveness：进程活着、装配正常，不查外部依赖 | 编排系统重启容器 |
| `/health/ready` | readiness：`ready` / `degraded`，检查 LLM 是否配置 | 只摘流量，不重启 |

两者不能合并成一个：合并会让"依赖抖动"触发实例重启，把小故障放大成雪崩。LLM 未配置时 readiness 返回 `degraded` 而非失败 —— 服务仍能提供健康检查等能力，对话接口明确返回 503。

## 链的结构

```
prompt | model | parser
```

- `prompt`：`ChatPromptTemplate`，system 用 `{system_prompt}` **运行时占位**，另加 `{history}`、`{message}`
- `model`：`ChatOpenAI`（OpenAI 兼容协议，可指向 DeepSeek / one-api 等网关）
- `parser`：`StrOutputParser`，把 `AIMessage` 转成纯文本

同一套链同时支持 `invoke()`（一次性）与 `astream_events()`（流式），换模型只改工厂一行。

> system 用占位符而不是建链时写死，是为了让提示词可热更新：改文件不影响已建好的链和 httpx 连接池，下一次请求自动用新内容。

## 提示词管理

提示词是**数据**，不是代码常量。正文放仓库根的 `prompts/` 目录，元数据写在 front matter：

```markdown
---
id: chat_system
version: 1
description: 对话场景的系统提示词（角色 / 约束 / 风格）
---
# 角色
...
```

| 路径 | 作用 |
| --- | --- |
| `prompts/chat_system.md` | 提示词正文 + `id` / `version` / `description`（**内容，不在包里**） |
| `app/prompts/loader.py` | 解析 front matter 并校验，不合法直接抛错 |
| `app/prompts/registry.py` | 按 id 取用，按文件 mtime 判断是否重载 |

**为什么正文不在 `app/` 包里**：提示词是内容资产，和代码的变更节奏、变更人都不一样。放进包里会有三个具体后果：

1. **改一句文案要重新打包发版** —— 提示词迭代频率远高于代码，绑在一起等于把文案改动升级成一次发布。
2. **`.md` 混进代码目录** —— 搜代码、跑静态检查、看目录树时都是噪声。
3. **非开发人员改不了** —— 产品和运营调提示词，不该先进源码树。

目录位置由 `PROMPTS_DIR` 决定：相对路径按仓库根解析，容器里挂载到别处就填绝对路径（如 `PROMPTS_DIR=/app/prompts`，`-v ./prompts:/app/prompts:ro`）。改提示词不必重启，也不必重建镜像。

三条硬规则（都在 `loader.py` 里强制执行）：

1. **`id` 必须等于文件名** —— 否则改名之后引用就错位了。
2. **`version` 必须是正整数** —— 版本是排障的锚点，不能写成 `v2`、`latest`。
3. **正文不能为空** —— 空提示词会让模型行为完全失控。

启动时加载失败会直接让进程起不来（`lifespan` 里显式加载一次，快速失败）；运行期重载失败则**保留旧版本**并记 error 日志——宁可继续用旧提示词，也不能因为一次手误让线上进入"无提示词"状态。

改提示词不用重启：

```powershell
# 1. 改 prompts/chat_system.md（顺手把 version +1）
# 2. 下一次请求自动生效，可查证
curl.exe http://127.0.0.1:8000/api/v1/chat/status
# {"prompt_id":"chat_system","prompt_version":2,...}
```

**为什么带 version**：线上答歪了，你要能回答"这条请求用的是哪版提示词、哪个模型"。`/chat/status` 暴露当前版本，服务日志里每条对话都记 `prompt id / version / checksum`——这三个字段是事后回溯的唯一线索。

**为什么合并成一条 system prompt**：之前拆成 `system` + `assistant` 两段再拼接，是生造的分类。业界按用途分（聊天/抽取/分类），一个聊天场景就是一条 system prompt，角色、约束、风格写在里面用标题分段。两段拼接的隐患是**指令冲突时谁赢不确定**——system 说"简洁"、assistant 说"展开讲"，模型行为会飘。

新增提示词：放一个新 `.md` 到 `prompts/`（`id` 与文件名一致），在 `app/prompts/registry.py` 的 `PromptKey` 加一行，然后 `PROMPT_CHAT_SYSTEM_ID` 指过去即可。

---
