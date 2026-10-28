# 下一步

按难度递增的后续练习方向。

> 返回 [README](../README.md) · 上一篇 [鉴权与安全](05-auth.md)

---

## 下一步可以练什么

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
