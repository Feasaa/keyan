# 前端接口调用示例

给前端用的完整调用说明：请求/响应实例都取自**真实运行结果**，不是手写示意。

> 返回 [README](../README.md) · 上一篇 [下一步](06-roadmap.md)

---

## 0. 三个先记住的约定

**① Base URL 带版本前缀**

```
http://127.0.0.1:8000/api/v1
```

本地开发时 CORS 只放行了 `http://localhost:5173` / `http://127.0.0.1:5173`（Vite 默认端口）。换端口要改后端 `.env` 的 `CORS_ORIGINS`。

**② 所有 JSON 接口同一个外壳，HTTP 状态码是真实的**

```json
{ "code": 0, "message": "ok", "data": { }, "trace_id": "136d0109..." }
```

`code = 0` 表示业务成功。**失败时 HTTP 状态码也是真的**（401 就是 401、422 就是 422），不是"一律 200 把错误塞进 body"。所以：

- 不要只看 `res.ok`，也不要只看 `code` —— 两者一起看；
- 报障时把 `trace_id` 一起给出，服务端日志按它串联。

**③ 雪花 ID 是字符串**

用户 ID 是 19 位整数，超过 JS 的 `Number.MAX_SAFE_INTEGER`（16 位）。后端已经序列化成字符串，前端**不要 `Number(id)`**，否则末几位被静默改写，表现为"用户 ID 对不上"。

---

## 1. 能力清单

| 分组 | 接口                             | 说明                            |
| -- | ------------------------------ | ----------------------------- |
| 鉴权 | `POST /auth/verification-code` | 给邮箱发 6 位验证码                   |
|    | `POST /auth/register`          | 邮箱 + 密码 + 码，**成功直接返回令牌**（201） |
|    | `POST /auth/login`             | 邮箱 + 密码                       |
|    | `POST /auth/refresh`           | refresh 换新的一对，旧的立刻作废          |
|    | `POST /auth/logout`            | 撤销 refresh token（幂等）          |
|    | `GET /auth/me`                 | 校验登录态 / 取当前用户                 |
| 对话 | `GET /chat/status`             | LLM 是否配置、当前提示词版本，不打真网络        |
|    | `POST /chat`                   | 一次性返回完整回答                     |
|    | `POST /chat/stream`            | SSE 逐块返回                      |
| 健康 | `GET /health`                  | 存活探针                          |
|    | `GET /health/ready`            | 就绪探针                          |

> **对话接口目前不需要登录**（`/chat` 不校验令牌）。要接鉴权见 [06 下一步](06-roadmap.md) 第 3 条。

---

## 2. 鉴权

### 2.1 发验证码

`POST /auth/verification-code`

```json
{ "email": "you@qq.com", "scene": "register" }
```

`scene` 可省略，默认 `register`。

成功（200）：

```json
{
  "code": 0,
  "message": "验证码已发送，请查收邮件",
  "data": { "email": "you@qq.com", "expires_in": 300, "resend_after": 60 },
  "trace_id": "db10a747..."
}
```

前端要点：

- **不返回验证码本身**，也不告诉你"这个邮箱是否已注册"（防账号枚举）。所以别做"邮箱已占用"的即时校验，只能等提交注册时才知道。
- `resend_after = 60`：60 秒冷却，拿它做倒计时按钮，别自己写死 60。
- 60 秒内重发返回 **429 / code 11429**，`message` 会带剩余秒数。
- 同一邮箱每天 10 次、同一 IP 每小时 20 次（可配）。

### 2.2 注册

`POST /auth/register` → **HTTP 201**

```json
{
  "email": "you@qq.com",
  "password": "至少8位",
  "code": "123456",
  "nickname": ""
}
```

| 字段         | 规则                                       |
| ---------- | ---------------------------------------- |
| `email`    | 合法邮箱，服务端统一转小写                            |
| `password` | 8–128 位，**不限字符类型**（NIST 建议：不强制大小写数字符号混排） |
| `code`     | 必须是 6 位数字                                |
| `nickname` | 可省略，最长 64；留空则取邮箱 `@` 前缀（`you@qq.com` → `you`） |

成功（201）：

```json
{
  "code": 0,
  "message": "注册成功",
  "data": {
    "user": {
      "id": "1857349201835921408",
      "email": "you@qq.com",
      "nickname": "you",
      "created_at": "2026-10-28T07:58:49Z"
    },
    "token": {
      "access_token": "eyJhbGciOiJIUzI1NiIs...",
      "refresh_token": "9tK3xQ...",
      "token_type": "Bearer",
      "expires_in": 900
    }
  },
  "trace_id": "..."
}
```

失败：

| HTTP | code  | 场景                         |
| ---- | ----- | -------------------------- |
| 400  | 11400 | 验证码错误 / 已过期 / 试错超 5 次      |
| 409  | 11409 | 该邮箱已被注册                    |
| 422  | 10422 | 字段校验失败（见 `data.errors`）    |
| 429  | 11429 | 操作过于频繁                     |
| 502  | 21502 | 验证码邮件发送失败                  |
| 503  | 21503 | 鉴权功能未配置（后端没配 `JWT_SECRET`） |

> 注意顺序：服务端**先校验验证码，再检查邮箱是否重复**。所以"邮箱已注册"这个信息只有在验证码正确时才会返回 —— 这是刻意的，否则发码接口就成了账号枚举工具。

### 2.3 登录

`POST /auth/login`

```json
{ "email": "you@qq.com", "password": "你的密码" }
```

成功（200）与注册同结构（`AuthResult`）。

**密码错误和邮箱不存在返回完全相同的响应**（401 / code 11401 / `"邮箱或密码不正确"`），前端别指望区分。

### 2.4 刷新令牌

`POST /auth/refresh`

```json
{ "refresh_token": "9tK3xQ..." }
```

成功返回**新的 `AuthResult`**，里面是**全新的一对**令牌。

⚠️ **旧 refresh_token 立刻失效（轮换）**。所以并发刷新会互相作废 —— 前端必须做单飞（single-flight），见第 6 节的客户端实现。

### 2.5 登出

`POST /auth/logout`

```json
{ "refresh_token": "9tK3xQ..." }
```

成功：`{"code":0,"message":"已登出","data":null,...}`。幂等，传一个不存在的 token 也返回成功。

⚠️ `access_token` 在剩余有效期（默认 15 分钟）内**仍然可用** —— JWT 自包含的固有代价。前端登出后要主动清掉本地令牌，别指望它立刻失效。

### 2.6 当前用户

`GET /auth/me`，需要 `Authorization: Bearer <access_token>`

成功（200）：`data` 就是 `UserProfile`。

未登录 / 令牌无效：

```json
{ "code": 11401, "message": "缺少访问令牌", "data": null, "trace_id": "..." }
```

`message` 有两种：`缺少访问令牌`（没带 header）和 `登录已失效，请重新登录`（令牌无效或过期）。**按 `code` 判断，不要匹配文案。**

---

## 3. 对话

### 3.1 状态

`GET /chat/status`

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "configured": true,
    "model": "deepseek-flash",
    "base_url": "https://api.deepseek.com/v1",
    "prompt_id": "chat_system",
    "prompt_version": 1
  },
  "trace_id": "..."
}
```

`configured: false` 说明后端没配 `LLM_API_KEY`，此时两个对话接口都会返回 503。前端可以用它决定是否禁用输入框。

### 3.2 一次性对话

`POST /chat`

```json
{
  "message": "用一句话介绍 LangChain",
  "history": [
    { "role": "user", "content": "你好" },
    { "role": "assistant", "content": "你好，有什么可以帮你？" }
  ]
}
```

成功（200）：

```json
{
  "code": 0,
  "message": "ok",
  "data": { "answer": "你好", "model": "deepseek-flash", "usage": null },
  "trace_id": "daf37e29..."
}
```

参数约束（不满足直接 422）：

| 字段                  | 约束                           |
| ------------------- | ---------------------------- |
| `message`           | 1–8000 字                     |
| `history`           | 最多 20 条                      |
| `history[].content` | 1–8000 字                     |
| `history[].role`    | **只允许 `user` / `assistant`** |

`role: "system"` 会被拒：

```json
{
  "code": 10422,
  "message": "请求参数校验失败",
  "data": { "errors": [ { "field": "history.0.role", "message": "Value error, role 只允许 ['assistant', 'user']，收到：'system'" } ] },
  "trace_id": "..."
}
```

这是**防提示注入**：系统提示词只能由服务端注入，客户端伪造 system 角色一律拦掉。

### 3.3 流式对话（SSE）

`POST /chat/stream`，请求体与 `/chat` 完全相同。

⚠️ **不能用 `EventSource`**。`EventSource` 只支持 GET、不能带自定义 header、不能带 body。这里是 POST + JSON body，必须用 `fetch` + `ReadableStream` 手动解析。

响应头：`content-type: text/event-stream; charset=utf-8`

**真实帧**（`\r\n` 分隔，这里为了可读按行展示）：

```
event: delta
data: {"delta":"你好"}

event: meta
data: {"model":"deepseek-flash","prompt_tokens":159,"completion_tokens":2}

event: done
data: [DONE]
```

| event   | data                                            | 说明                            |
| ------- | ----------------------------------------------- | ----------------------------- |
| `delta` | `{"delta": "增量文本"}`                             | 可能出现很多次，**按顺序拼接**             |
| `meta`  | `{"model","prompt_tokens","completion_tokens"}` | 结束前一次，供统计用量；token 数可能为 `null` |
| `done`  | `[DONE]`（纯文本，不是 JSON）                           | 正常结束                          |
| `error` | `{"code": 22502, "message": "..."}`             | 流中途出错，之后不再有帧                  |

两个容易踩的点：

1. **流开始前**的错误（参数不合法 422、LLM 未配置 503）仍然是普通 JSON，HTTP 状态码是真的 —— 要先判 `res.ok`。
2. **流开始后**的错误只能靠 `event: error` 帧传（响应头已发出，状态码改不了）。所以两种错误通道都要处理。

服务端会周期性发 `: ping` 注释行防代理空闲超时，**注释行没有 event/data，解析时直接忽略**。

---

## 4. 健康检查

| 接口                  | 成功响应 `data`                                                     |
| ------------------- | --------------------------------------------------------------- |
| `GET /health`       | `{"status":"ok","name":"learn-fastapi","version":"0.1.0"}`  |
| `GET /health/ready` | `{"status":"ready","checks":{"db":"ok","redis":"ok","llm":"ok"}}` |

`status` 三档含义：
- `ready`（HTTP 200）：全部正常
- `degraded`（HTTP 200）：有依赖未配置（如本地开发没起 Redis），服务仍可用
- `not_ready`（HTTP 503）：有依赖配了却连不上（如 MySQL 挂了），负载均衡应摘掉本实例

---

## 5. 错误码表

编码规则是 5 位：`X M SSS` = 归属 / 模块 / HTTP 状态码。

- **首位 `1`** = 调用方问题 → 提示用户改输入
- **首位 `2`** → 服务端或依赖问题 → 提示"稍后重试"，别让用户改

| code  | HTTP | 含义               | 前端建议                    |
| ----- | ---- | ---------------- | ----------------------- |
| 0     | 200  | 成功               | —                       |
| 10400 | 400  | 请求参数不合法          | 提示用户                    |
| 10404 | 404  | 资源不存在            | 检查路径                    |
| 10405 | 405  | 方法不支持            | 检查 method               |
| 10409 | 409  | 资源冲突             | 提示用户                    |
| 10422 | 422  | 参数校验失败           | 读 `data.errors[]` 定位到字段 |
| 10429 | 429  | 请求过于频繁           | 倒计时重试                   |
| 10500 | 500  | 服务内部异常           | 报障（带 trace_id）          |
| 10502 | 502  | 上游服务异常           | 稍后重试                    |
| 10503 | 503  | 服务暂时不可用          | 稍后重试                    |
| 11400 | 400  | 验证码不正确或已过期       | 提示用户重新获取                |
| 11401 | 401  | 未登录 / 密码错 / 令牌失效 | 跳登录页或触发刷新               |
| 11403 | 403  | 没有权限             | 提示无权限                   |
| 11409 | 409  | 该邮箱已被注册          | 提示去登录                   |
| 11429 | 429  | 操作过于频繁           | 用 `resend_after` 倒计时    |
| 21502 | 502  | 验证码邮件发送失败        | 提示稍后重试                  |
| 21503 | 503  | 鉴权功能未配置          | 联系后端                    |
| 22502 | 502  | LLM 上游服务异常       | 提示稍后重试                  |
| 22503 | 503  | LLM 未配置          | 禁用对话入口                  |
| 22504 | 504  | LLM 上游超时         | 提示稍后重试                  |

422 的 `data` 结构固定：

```json
{ "errors": [ { "field": "email", "message": "value is not a valid email address..." } ] }
```

`field` 是点分路径，嵌套字段形如 `history.0.role`，可以直接映射到表单字段。

---

## 6. TypeScript 类型定义

```ts
// api-types.ts

/** 统一响应外壳 */
export interface ApiEnvelope<T> {
  code: number;
  message: string;
  data: T | null;
  trace_id: string;
}

/** 422 时的 data */
export interface ValidationDetail {
  errors: Array<{ field: string; message: string }>;
}

export interface UserProfile {
  /** 雪花 ID，19 位字符串，禁止 Number() */
  id: string;
  email: string;
  nickname: string;
  /** ISO 8601，UTC 带 Z 后缀，可直接 new Date() */
  created_at: string;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: "Bearer";
  /** access_token 剩余有效秒数 */
  expires_in: number;
}

export interface AuthResult {
  user: UserProfile;
  token: TokenPair;
}

export interface SendCodeResult {
  email: string;
  expires_in: number;
  /** 多少秒后可以重发 */
  resend_after: number;
}

export interface ChatStatus {
  configured: boolean;
  model: string;
  base_url: string;
  prompt_id: string;
  prompt_version: number;
}

export interface ChatMessage {
  /** 只允许 user / assistant，system 会被 422 拒绝 */
  role: "user" | "assistant";
  content: string;
}

export interface ChatRequest {
  message: string;
  /** 最多 20 条 */
  history?: ChatMessage[];
}

export interface ChatResponse {
  answer: string;
  model: string;
  usage: Record<string, number> | null;
}

/** SSE 事件（客户端解析后的形状） */
export type ChatStreamEvent =
  | { type: "delta"; delta: string }
  | {
      type: "meta";
      model: string;
      prompt_tokens: number | null;
      completion_tokens: number | null;
    }
  | { type: "done" }
  | { type: "error"; code: number; message: string };
```

---


## 7. 可直接用的客户端

`fetch` 封装 + 401 单飞刷新 + SSE 解析。复制即用，无第三方依赖。

结构按依赖顺序：`ApiError`（错误类型）→ `auth`（令牌存储）→ `request`（含 401 重放）→ `refreshOnce`（单飞）→ `parseSSE`（流解析）→ `api`（对外方法）。

```ts
// api-client.ts
import type {
  ApiEnvelope,
  AuthResult,
  ChatRequest,
  ChatResponse,
  ChatStreamEvent,
  ChatStatus,
  SendCodeResult,
  TokenPair,
  UserProfile,
  ValidationDetail,
} from "./api-types";

const BASE = "http://127.0.0.1:8000/api/v1";

/** 业务错误：HTTP 状态码与业务码都保留，方便上层分流处理 */
export class ApiError extends Error {
  constructor(
    readonly code: number,
    message: string,
    readonly status: number,
    readonly traceId = "",
    readonly data: unknown = null,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** 422：把 errors 转成 { 字段: 提示 }，直接喂给表单 */
  get fieldErrors(): Record<string, string> {
    const errors = (this.data as ValidationDetail | null)?.errors ?? [];
    // as const 不能省：不加的话 map 推断出 string[][]，
    // 而 Object.fromEntries 要求 Iterable<[PropertyKey, T]>
    return Object.fromEntries(errors.map((e) => [e.field, e.message] as const));
  }
}

// ---------------------------------------------------------------- 令牌存储

const STORAGE_KEY = "auth";

export interface StoredAuth {
  access: string;
  refresh: string;
  /** 毫秒时间戳，用于提前判断是否需要刷新 */
  expiresAt: number;
}

export const auth = {
  get(): StoredAuth | null {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as StoredAuth) : null;
  },
  set(pair: TokenPair) {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        access: pair.access_token,
        refresh: pair.refresh_token,
        expiresAt: Date.now() + pair.expires_in * 1000,
      } satisfies StoredAuth),
    );
  },
  clear() {
    localStorage.removeItem(STORAGE_KEY);
  },
  /** 提前 30 秒视为过期，避免请求途中刚好失效 */
  isExpiringSoon(): boolean {
    const current = auth.get();
    return !current || current.expiresAt - Date.now() < 30_000;
  },
};

// ---------------------------------------------------------------- 底层请求

async function toApiError(res: Response): Promise<ApiError> {
  // 网关/代理层的错误可能不是 JSON，兜底成通用错误
  try {
    const body = (await res.json()) as ApiEnvelope<unknown>;
    return new ApiError(body.code, body.message, res.status, body.trace_id, body.data);
  } catch {
    return new ApiError(-1, `请求失败（HTTP ${res.status}）`, res.status);
  }
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  /** 内部标记：已经重试过一次，避免刷新死循环 */
  _retried?: boolean;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, _retried = false } = options;
  const current = auth.get();

  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(current ? { Authorization: `Bearer ${current.access}` } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  // 401 且不是刷新接口本身：刷新一次再重放
  if (res.status === 401 && !_retried && current && !path.startsWith("/auth/refresh")) {
    try {
      await refreshOnce();
    } catch {
      auth.clear();
      throw await toApiError(res);
    }
    return request<T>(path, { ...options, _retried: true });
  }

  if (!res.ok) throw await toApiError(res);

  const envelope = (await res.json()) as ApiEnvelope<T>;
  if (envelope.code !== 0) {
    throw new ApiError(envelope.code, envelope.message, res.status, envelope.trace_id, envelope.data);
  }
  return envelope.data as T;
}

// ---------------------------------------------------------------- 单飞刷新

let refreshing: Promise<AuthResult> | null = null;

/**
 * 并发 401 时只发一次刷新请求。
 * refresh_token 是轮换的（旧的用完即废），并发刷新会互相作废，
 * 表现为"偶发被踢下线"——所以必须单飞。
 */
function refreshOnce(): Promise<AuthResult> {
  if (refreshing) return refreshing;

  const current = auth.get();
  const task = (async () => {
    if (!current) throw new ApiError(11401, "未登录", 401);
    const result = await request<AuthResult>("/auth/refresh", {
      method: "POST",
      body: { refresh_token: current.refresh },
      _retried: true,
    });
    auth.set(result.token);
    return result;
  })().finally(() => {
    refreshing = null;
  });

  refreshing = task;
  return task;
}

// ---------------------------------------------------------------- SSE 解析

interface SSEFrame {
  event: string;
  data: string;
}

/**
 * 把 SSE 字节流解析成事件。
 * 兼容 \r\n / \n，忽略 `:` 开头的注释行（服务端的 ping 心跳）。
 */
async function* parseSSE(stream: ReadableStream<Uint8Array>): AsyncGenerator<SSEFrame> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let event = "";
  let dataLines: string[] = [];

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      buffer = buffer.replace(/\r\n/g, "\n");

      const lines = buffer.split("\n");
      // 最后一段可能是半行，留到下一轮
      buffer = lines.pop() ?? "";

      for (const line of lines) {
        if (line === "") {
          // 空行 = 一个事件结束
          if (dataLines.length > 0) {
            yield { event: event || "message", data: dataLines.join("\n") };
          }
          event = "";
          dataLines = [];
          continue;
        }
        if (line.startsWith(":")) continue; // 注释行 / ping

        const colon = line.indexOf(":");
        const field = colon === -1 ? line : line.slice(0, colon);
        let fieldValue = colon === -1 ? "" : line.slice(colon + 1);
        if (fieldValue.startsWith(" ")) fieldValue = fieldValue.slice(1);

        if (field === "event") event = fieldValue;
        else if (field === "data") dataLines.push(fieldValue);
      }
    }
  } finally {
    reader.releaseLock();
  }
}

// ---------------------------------------------------------------- 对外接口

export const api = {
  // ---- 鉴权 ----
  sendCode(email: string) {
    return request<SendCodeResult>("/auth/verification-code", {
      method: "POST",
      body: { email, scene: "register" },
    });
  },

  register(input: { email: string; password: string; code: string; nickname?: string }) {
    return request<AuthResult>("/auth/register", { method: "POST", body: input }).then((r) => {
      auth.set(r.token);
      return r;
    });
  },

  login(email: string, password: string) {
    return request<AuthResult>("/auth/login", {
      method: "POST",
      body: { email, password },
    }).then((r) => {
      auth.set(r.token);
      return r;
    });
  },

  async logout() {
    const current = auth.get();
    try {
      if (current) {
        await request<null>("/auth/logout", {
          method: "POST",
          body: { refresh_token: current.refresh },
        });
      }
    } finally {
      auth.clear(); // 无论服务端是否成功，本地令牌都要清
    }
  },

  me() {
    return request<UserProfile>("/auth/me");
  },

  // ---- 对话 ----
  chatStatus() {
    return request<ChatStatus>("/chat/status");
  },

  chat(payload: ChatRequest) {
    return request<ChatResponse>("/chat", { method: "POST", body: payload });
  },

  /**
   * 流式对话。用 async generator 暴露事件，调用方 for await 即可。
   * 注意：不能用 EventSource —— 这里是 POST + JSON body。
   */
  async *chatStream(payload: ChatRequest, signal?: AbortSignal): AsyncGenerator<ChatStreamEvent> {
    const current = auth.get();
    const res = await fetch(`${BASE}/chat/stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(current ? { Authorization: `Bearer ${current.access}` } : {}),
      },
      body: JSON.stringify(payload),
      signal,
    });

    // 流开始前的错误（422 / 503）仍是普通 JSON
    if (!res.ok) throw await toApiError(res);
    const body = res.body;
    if (!body) throw new ApiError(-1, "响应体为空", res.status);

    for await (const frame of parseSSE(body)) {
      if (frame.event === "delta") {
        const chunk = JSON.parse(frame.data) as { delta: string };
        yield { type: "delta", delta: chunk.delta };
      } else if (frame.event === "meta") {
        const meta = JSON.parse(frame.data) as {
          model: string;
          prompt_tokens: number | null;
          completion_tokens: number | null;
        };
        yield {
          type: "meta",
          model: meta.model,
          prompt_tokens: meta.prompt_tokens,
          completion_tokens: meta.completion_tokens,
        };
      } else if (frame.event === "done") {
        yield { type: "done" };
        return;
      } else if (frame.event === "error") {
        const err = JSON.parse(frame.data) as { code: number; message: string };
        yield { type: "error", code: err.code, message: err.message };
        return;
      }
    }
  },
};
```

### 用法示例

> 下面的 `render` / `toast` 是占位函数，换成你项目里的渲染与提示。

```ts
// 登录
await api.login("you@qq.com", "你的密码");

// 一次性对话
const { answer } = await api.chat({ message: "你好" });

// 流式对话
let text = "";
for await (const ev of api.chatStream({ message: "讲个笑话" })) {
  if (ev.type === "delta") {
    text += ev.delta;
    render(text); // 逐字渲染
  } else if (ev.type === "meta") {
    console.log("用量", ev.prompt_tokens, ev.completion_tokens);
  } else if (ev.type === "error") {
    toast(ev.message); // 流中途出错
  }
}

// 用户点"停止"
const controller = new AbortController();
const task = api.chatStream({ message: "写一篇长文" }, controller.signal);
// controller.abort() 会中断 fetch，服务端检测到断开后停止生成
```

### 表单错误映射

```ts
try {
  await api.register({ email, password, code });
} catch (e) {
  if (e instanceof ApiError && e.code === 10422) {
    setFieldErrors(e.fieldErrors); // { email: "...", password: "..." }
  } else if (e instanceof ApiError && e.code === 11409) {
    showTip("该邮箱已注册，请直接登录");
  } else if (e instanceof ApiError && e.code === 11429) {
    startCountdown(60);
  } else {
    showTip((e as ApiError).message);
  }
}
```

---

## 8. 联调自测

```bash
# 存活
curl.exe http://127.0.0.1:8000/api/v1/health

# 对话是否可用（不打真网络）
curl.exe http://127.0.0.1:8000/api/v1/chat/status

# 流式（-N 关掉 curl 自己的缓冲，否则看不到逐块输出）
curl.exe -N -X POST http://127.0.0.1:8000/api/v1/chat/stream -H "Content-Type: application/json" -d "{\"message\":\"你好\"}"
```

带令牌：

```bash
curl.exe http://127.0.0.1:8000/api/v1/auth/me -H "Authorization: Bearer <access_token>"
```

浏览器里还能直接用 Swagger：<http://127.0.0.1:8000/docs>（右上角 Authorize 填 `Bearer <token>`）。

---

## 9. 还没做的（前端先别按有来做）

| 项        | 现 状                                        |
| -------- | ------------------------------------------ |
| 对话接口鉴权   | `/chat`、`/chat/stream` 不校验令牌，也没有"对话归属用户"   |
| 对话历史持久化  | 没有 `GET /conversations`，`history` 要前端自己攒着传 |
| SSE 断线续传 | 没有 `id:` 帧和 `Last-Event-ID`，断了只能整轮重来       |
| 忘记密码     | 没有重置密码接口                                   |
| 登录失败锁定   | 只有验证码侧限流，密码登录侧没有计数                         |
| 人机验证     | 未接入                                        |
