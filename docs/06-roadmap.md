# 下一步

按难度递增的后续练习方向。

> 返回 [README](../README.md) · 上一篇 [鉴权与安全](05-auth.md) · 下一篇 [前端接口调用示例](07-api-examples.md)

---

## 下一步可以练什么

按由易到难（统一返回、异常处理、错误码、表结构、迁移、仓库层、鉴权已经做完了，下面接在后面）：

1. ~~**SSE 断线续传**~~ ✅：生成与连接解耦 —— 生产者任务独立跑、每块 `XADD` 进 Redis Stream；每帧带 `id:`（Stream entry id），客户端重连时用 `Last-Event-ID` 调 `GET /chat/stream/{message_id}` 续读，不重新生成、不重复计费。配套两个界：无人接收超过 `SSE_RESUME_GRACE_SECONDS` 就停生成（落 `interrupted`），缓冲只留 `SSE_RESUME_TTL_SECONDS`（过期 410）。
2. **LLM 结构化输出**：`.with_structured_output(PydanticModel)` 替换 `StrOutputParser`，做实体抽取。
3. ~~**对话接口挂鉴权**~~ ✅：`/chat`、`/chat/stream` 用 `CurrentUser` 强制登录，没令牌直接 `401 / 11401`；`user_id` 全从令牌取，会话与消息的归属条件写进 WHERE。**没有**保留匿名模式 —— 上下文只认服务端，请求体里的 `history` 字段已删除（让浏览器决定喂给模型什么上下文，等于把提示注入的入口开在客户端）。因此历史消息列表 / 续聊 / 评价全部只对本人可见。
4. ~~**流式落库**~~ ✅：流开始前先写用户消息 + 一条 `status=STREAMING` 的助手占位，流结束回填 `COMPLETE`；**没有客户端接收**（grace 到期）或进程停机标 `INTERRUPTED`（已生成的部分保留），上游报错标 `FAILED`。增量分片不进 MySQL —— 落库只在开头和结尾各一次，中间的分片放 Redis Stream（第 1 条），避免每块一次写库。
5. **会话搜索**：`title` 和消息正文的全文检索（`LIKE` 或 MySQL 全文索引），配合会话列表的游标分页做"搜索后跳转"。
6. ~~**就绪探针加 DB / Redis**~~ ✅：现在 `/health/ready` 真跑一次 `SELECT 1` 和 `PING`，失败返回 503。
7. ~~**登录失败次数限制**~~ ✅：验证码侧有三层限流，密码登录侧也有了 —— 按账号 / IP 双计数，验证密码前先检查计数。
8. **忘记密码**：`VerificationScene` 加一个 `RESET_PASSWORD`（Redis key 自动隔离），复用同一套发码与校验，再加一个 `POST /auth/reset-password`。表里再加 `email_verified_at` 就能支撑"改邮箱需重新验证"。
9. ~~**人机验证**~~ ✅：VAPTCHA V4 接在 `AuthService.send_register_code()` 最前面（消耗配额之前验签），支持 `remote` 官方接口与 `local` 本地 HMAC 两种模式，前端联调页在 `web/`。
10. **评价数据回灌**：把 `feedback=down` 的问答捞出来做 badcase 集，用于回归测试与提示词调优（`idx_msg_feedback` 索引就是为这个建好的）。
11. **加 RAG**：文档切分 → 向量库（如 pgvector / Chroma）→ retriever 接进链，`prompt | retriever` 组合。
