# 数据层：雪花 ID / 仓储 / 迁移

ID 生成策略、ORM 与仓储的边界、Alembic 作为表结构的唯一真相源。

> 返回 [README](../README.md) · 上一篇 [环境准备与运行](02-getting-started.md) · 下一篇 [接口契约](04-api-contract.md)

---

## 雪花 ID（app/core/snowflake.py）

```python
from app.core.snowflake import get_snowflake, decompose

user_id = get_snowflake().next_id()  # 进程级单例，不要每请求新建
decompose(user_id).created_at  # 不查库就知道这条记录什么时候建的
```

位布局 `1 | 41 时间戳 | 10 机器号 | 12 序列号`，单机每毫秒 4096 个。两个约束：

- **`SNOWFLAKE_WORKER_ID` 多实例必须各不相同**，否则两台机器会生成完全相同的 ID。
- **时钟回拨超过 5ms 直接抛 `ClockMovedBackwardsError`**。宁可报错也不生成可能重复的 ID——小幅回拨（NTP 校时）会等待追上，不打断业务。

纪元 `2024-01-01` 写死在代码里，不做成环境变量：能配置的东西就会被配错，而改纪元会让新旧 ID 撞车。

## 数据访问层（app/db/）

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

## 迁移（alembic/）

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
