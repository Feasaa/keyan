-- =============================================================================
-- agent_chat 初始表结构
--
-- 执行时机：MySQL 容器首次启动、数据目录为空时由 docker-entrypoint-initdb.d 执行。
--            数据卷已存在时不会重复执行 —— 改表结构请走迁移（Alembic），
--            不要再往这个目录加文件。
--
-- 主键策略：雪花 ID（BIGINT UNSIGNED），由应用侧生成后显式写入。
--            不用 AUTO_INCREMENT，因为：
--              1) 多实例部署时自增会冲突，雪花天然全局唯一且大致有序；
--              2) 主键即时间序，InnoDB 聚簇索引插入局部性好，页分裂少；
--              3) 不需要再额外维护一套对外 ID。
--            对外暴露时必须以【字符串】形式返回：雪花 ID 超过 2^53，
--            JavaScript 的 Number 会静默丢精度（末几位变 0），
--            前端拿到的就不是同一条记录了。
--
-- 时间类型：DATETIME(3) 存 UTC，毫秒精度。
--            不用 TIMESTAMP：它有 2038 上限，且会跟随会话时区隐式转换。
-- =============================================================================

SET NAMES utf8mb4;
SET time_zone = '+00:00';

CREATE DATABASE IF NOT EXISTS `agent_chat`
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_0900_ai_ci;

USE `agent_chat`;


-- -----------------------------------------------------------------------------
-- 账号
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `users` (
  `id`            BIGINT UNSIGNED  NOT NULL                COMMENT '雪花 ID',
  `username`      VARCHAR(64)      NOT NULL                COMMENT '登录账号',
  `email`         VARCHAR(255)         NULL                COMMENT '可选登录方式，可为空',
  `password_hash` VARCHAR(255)     NOT NULL                COMMENT 'bcrypt/argon2id 密文，含算法前缀',
  `nickname`      VARCHAR(64)      NOT NULL DEFAULT ''     COMMENT '展示名',
  `status`        TINYINT UNSIGNED NOT NULL DEFAULT 1      COMMENT '0 禁用 / 1 正常 / 2 锁定',
  `last_login_at` DATETIME(3)          NULL                COMMENT '最近登录时间',
  `last_login_ip` VARBINARY(16)        NULL                COMMENT 'INET6_ATON 写入，同时兼容 IPv4/IPv6',
  `created_at`    DATETIME(3)      NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at`    DATETIME(3)      NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `deleted_at`    DATETIME(3)          NULL                COMMENT '软删除标记，NULL 表示有效',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_users_username` (`username`),
  -- 可空唯一索引：MySQL 允许多个 NULL，所以 email 留空不冲突
  UNIQUE KEY `uk_users_email` (`email`),
  KEY `idx_users_status` (`status`, `deleted_at`),
  CONSTRAINT `ck_users_status` CHECK (`status` IN (0, 1, 2))
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = '账号';

-- 注意：软删除与唯一索引天生冲突 —— 用户注销后 username 仍被占用，
-- 新用户无法注册同名账号。MySQL 没有部分索引（PostgreSQL 的 WHERE 子句），
-- 惯例做法是软删除时把 username 改写为 `原名#<时间戳>`，
-- 或引入 `deleted_at` 参与的唯一键。注册逻辑里必须处理这一点。


-- -----------------------------------------------------------------------------
-- 对话
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `conversations` (
  `id`                      BIGINT UNSIGNED  NOT NULL              COMMENT '雪花 ID，即对外的对话 ID',
  `user_id`                 BIGINT UNSIGNED  NOT NULL              COMMENT '归属用户',
  `title`                   VARCHAR(255)     NOT NULL DEFAULT ''   COMMENT '首轮内容摘要，异步回填',
  `model`                   VARCHAR(64)      NOT NULL DEFAULT ''   COMMENT '本会话使用的模型',
  `prompt_id`               VARCHAR(64)      NOT NULL DEFAULT 'chat_system' COMMENT '提示词 id，对齐 app/prompts',
  `prompt_version`          INT UNSIGNED     NOT NULL DEFAULT 1    COMMENT '提示词版本，答歪时用于回溯',
  `message_count`           INT UNSIGNED     NOT NULL DEFAULT 0    COMMENT '消息条数，冗余字段，列表页免 COUNT',
  `total_prompt_tokens`     BIGINT UNSIGNED  NOT NULL DEFAULT 0    COMMENT '累计输入令牌',
  `total_completion_tokens` BIGINT UNSIGNED  NOT NULL DEFAULT 0    COMMENT '累计输出令牌',
  `status`                  TINYINT UNSIGNED NOT NULL DEFAULT 0    COMMENT '0 活跃 / 1 归档',
  `last_message_at`         DATETIME(3)      NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '列表排序键',
  `created_at`              DATETIME(3)      NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at`              DATETIME(3)      NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  `deleted_at`              DATETIME(3)          NULL              COMMENT '软删除标记',
  PRIMARY KEY (`id`),
  -- 会话列表的唯一主查询：WHERE user_id = ? AND deleted_at IS NULL
  -- ORDER BY last_message_at DESC。DESC 索引让排序免掉 filesort。
  KEY `idx_conv_user_recent` (`user_id`, `last_message_at` DESC),
  KEY `idx_conv_status` (`status`, `last_message_at`),
  CONSTRAINT `fk_conv_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`)
    ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `ck_conv_status` CHECK (`status` IN (0, 1))
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = '对话';


-- -----------------------------------------------------------------------------
-- 消息（核心表，增长最快）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `messages` (
  `id`                BIGINT UNSIGNED  NOT NULL              COMMENT '雪花 ID，兼作 SSE Last-Event-ID',
  `conversation_id`   BIGINT UNSIGNED  NOT NULL              COMMENT '所属对话',
  `user_id`           BIGINT UNSIGNED  NOT NULL              COMMENT '冗余用户 ID，按用户审计时免 JOIN',
  `seq`               INT UNSIGNED     NOT NULL              COMMENT '会话内递增序号，从 1 开始',
  `role`              VARCHAR(16)      NOT NULL              COMMENT 'user / assistant / system / tool',
  `content`           MEDIUMTEXT           NULL              COMMENT '正文；NULL 表示尚未产生内容',
  `status`            TINYINT UNSIGNED NOT NULL DEFAULT 0    COMMENT '0 streaming / 1 complete / 2 interrupted / 3 failed',
  `model`             VARCHAR(64)          NULL              COMMENT 'assistant 消息才有',
  `prompt_tokens`     INT UNSIGNED         NULL              COMMENT '单条输入令牌',
  `completion_tokens` INT UNSIGNED         NULL              COMMENT '单条输出令牌',
  `first_token_ms`    INT UNSIGNED         NULL              COMMENT '首字延迟，体验指标',
  `total_ms`          INT UNSIGNED         NULL              COMMENT '总耗时',
  `finish_reason`     VARCHAR(32)          NULL              COMMENT 'stop / length / content_filter',
  `error_message`     VARCHAR(512)         NULL              COMMENT 'status=failed 时填，需脱敏',
  `client_message_id` VARCHAR(64)          NULL              COMMENT '客户端幂等键，重试去重用',
  `parent_message_id` BIGINT UNSIGNED      NULL              COMMENT '重新生成/分支时的来源消息',
  `created_at`        DATETIME(3)      NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at`        DATETIME(3)      NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  -- 会话内唯一序号：既是排序依据，也防止并发写入产生重复位次。
  -- 不能靠 created_at 排序：同一毫秒可能插入两条，顺序不确定，翻页会跳行。
  UNIQUE KEY `uk_msg_conv_seq` (`conversation_id`, `seq`),
  -- 幂等键：客户端重试带同一个 client_message_id，INSERT 时直接撞唯一键。
  -- client_message_id 为 NULL 时不参与去重（MySQL 唯一索引允许多个 NULL），
  -- 所以 assistant 消息不受影响。
  UNIQUE KEY `uk_msg_client_idem` (`user_id`, `client_message_id`),
  KEY `idx_msg_user_created` (`user_id`, `created_at`),
  -- 对账用：捞取所有长时间停留在 streaming 状态的消息
  KEY `idx_msg_status_updated` (`status`, `updated_at`),
  CONSTRAINT `fk_msg_conv` FOREIGN KEY (`conversation_id`) REFERENCES `conversations` (`id`)
    ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `ck_msg_role` CHECK (`role` IN ('user', 'assistant', 'system', 'tool')),
  CONSTRAINT `ck_msg_status` CHECK (`status` IN (0, 1, 2, 3))
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = '消息';

-- status 是 Redis 与 MySQL 的接缝，四个状态缺一不可：
--   0 streaming    流式进行中，Redis 里还有缓冲；进程崩溃后靠它捞出来对账
--   1 complete     正常收尾，content 已完整
--   2 interrupted  客户端断线，content 是截断的半句，前端要显示"已中断"
--   3 failed       上游报错，content 可能为空，error_message 有值
-- 没有这个字段就无法区分"模型真的答了空"和"还没答完"。


-- -----------------------------------------------------------------------------
-- 登录态（可选）
--
-- 如果登录只发无状态 JWT，可以删掉这张表 —— 代价是无法主动踢人下线、
-- 登出后旧 token 仍然有效到过期。需要"登出即失效"就必须有它。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `auth_sessions` (
  `id`                 BIGINT UNSIGNED NOT NULL             COMMENT '雪花 ID',
  `user_id`            BIGINT UNSIGNED NOT NULL             COMMENT '归属用户',
  `refresh_token_hash` CHAR(64)        NOT NULL             COMMENT 'sha256 十六进制，绝不存原文',
  `user_agent`         VARCHAR(255)    NOT NULL DEFAULT ''  COMMENT '设备标识，供用户自行管理登录设备',
  `ip`                 VARBINARY(16)       NULL             COMMENT 'INET6_ATON 写入',
  `expires_at`         DATETIME(3)     NOT NULL             COMMENT '过期时间',
  `revoked_at`         DATETIME(3)         NULL             COMMENT '非空即已失效（登出/踢人）',
  `created_at`         DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_sess_token` (`refresh_token_hash`),
  KEY `idx_sess_user` (`user_id`, `expires_at`),
  CONSTRAINT `fk_sess_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`)
    ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = '登录态';


-- -----------------------------------------------------------------------------
-- 结构版本
--
-- init 脚本只在空库执行一次，之后的结构变更必须走迁移。
-- 这张表用来记录"当前库是哪个版本"，避免部署时靠肉眼比对。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `schema_migrations` (
  `version`    VARCHAR(64) NOT NULL             COMMENT '迁移版本号',
  `applied_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`version`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = '结构版本记录';

INSERT INTO `schema_migrations` (`version`)
VALUES ('0001_init')
ON DUPLICATE KEY UPDATE `version` = `version`;
