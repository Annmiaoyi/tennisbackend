-- ============================================================================
--  AceMate 后端数据库结构（SQLite）
--  数据一致性总原则：离线优先 + 后端权威 + 最终一致
--    · 后端数据库是**唯一真实数据源**；客户端只做缓存与待同步队列
--    · 写操作先写本地 → 进同步队列 → 推送到后端（幂等）
--    · 拉取使用**服务器游标**（单调递增 seq），不依赖客户端时间
--    · 冲突解决：最后写入优先（LWW），比较依据 updated_at
--    · 删除一律**软删除**（deleted_at 非空即视为已删除）
-- ============================================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 通用同步列（所有业务表都带这 7 列 + server_seq）
--   id            TEXT     全局唯一 id，由**客户端生成**（UUIDv4/ULID），
--                          这样离线也能创建记录、联网后不会重号
--   user_id       TEXT     记录归属用户（多租户隔离与增量拉取的过滤依据）
--   created_at    TEXT     ISO8601 UTC，首次创建时间
--   updated_at    TEXT     ISO8601 UTC，最后修改时间 —— **LWW 的比较依据**
--   deleted_at    TEXT     软删除标记；非 NULL 即视为已删除（保留行以便同步）
--   version       INTEGER  乐观并发版本号；每次成功写入 +1
--   sync_status   TEXT     synced | pending | conflict
--   server_seq    INTEGER  服务器为该行分配的最近一次变更游标（=changelog.seq）
-- ---------------------------------------------------------------------------

-- ---------------------------------- 1. 学员档案 ------------------------------
CREATE TABLE IF NOT EXISTS students (
  id                TEXT PRIMARY KEY,
  user_id           TEXT NOT NULL,

  name              TEXT,
  avatar_url        TEXT,
  avatar_initial    TEXT,                    -- 无头像时展示的首字
  tier              TEXT,                    -- VIP | Elite | Pro | Club | Standard
  device_id         TEXT,                    -- 例：AC-88204
  watch_model       TEXT,                    -- 例：Watch Ultra 2
  batch             TEXT,                    -- 所属班期，例：2024 春季班

  years_playing     TEXT,                    -- 球龄，例：4年
  hand              TEXT,                    -- 右手 | 左手
  backhand          TEXT,                    -- 双反 | 单反
  racket            TEXT,                    -- 战拍型号
  racket_tension    TEXT,                    -- 拍线磅数，例：52 lbs

  nt_level          TEXT,                    -- NTRP 水平，例：3.5
  nt_score          REAL,                    -- 综合评分

  sessions_count    INTEGER DEFAULT 0,        -- 累计场次
  hours_total       REAL    DEFAULT 0,        -- 累计小时
  strokes_total     INTEGER DEFAULT 0,        -- 累计击球
  forehand_avg      INTEGER,                  -- 正手均速 km/h
  serve_peak        INTEGER,                  -- 发球峰值 km/h
  sweet_spot        REAL,                     -- 甜区命中率 %
  spin_rate         INTEGER,                  -- 平均转速 RPM
  hit_rate          REAL,                     -- 有效击球占比 %
  training_load     TEXT,                     -- 训练负荷：Optimal | High ...
  acwr              REAL,                     -- 急慢性负荷比

  last_training_at  TEXT,                    -- 最近训练时间（展示用文案或 ISO8601）
  last_training_note TEXT,                   -- 最近训练备注，例：刚结束 · 截击专项
  location          TEXT,
  is_online         INTEGER DEFAULT 0,

  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  deleted_at        TEXT,
  version           INTEGER NOT NULL DEFAULT 1,
  sync_status       TEXT    NOT NULL DEFAULT 'synced',
  server_seq        INTEGER,

  -- 业务列不能写 NOT NULL，原因：**墓碑行**（服务端从未见过的 id 收到 delete）
  -- 只填同步列、没有业务数据，写 NOT NULL 会让墓碑插入抛 IntegrityError，
  -- 进而把整个推送批次回滚掉（客户端删一条服务端没见过的记录就 500）。
  -- 用下面这条表级约束把真实规则说清楚：活行必须有业务必填字段，墓碑不要求。
  -- 与同步层 SYNCABLE[...]['required'] 一一对应。
  CHECK (deleted_at IS NOT NULL OR name IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_students_user   ON students(user_id, deleted_at);
CREATE INDEX IF NOT EXISTS idx_students_seq    ON students(server_seq);

-- ------------------------------- 2. 训练会话 --------------------------------
-- 同步主体之一：一次训练/对抗 = 一条会话
CREATE TABLE IF NOT EXISTS training_sessions (
  id                TEXT PRIMARY KEY,
  user_id           TEXT NOT NULL,
  student_id        TEXT,                     -- 关联 students.id

  title             TEXT,                     -- 例：截击专项 / 底线对抗训练
  session_type      TEXT,                     -- drill | match | rally | serve
  location          TEXT,
  court_type        TEXT,                     -- hard | clay | grass | indoor
  started_at        TEXT,                     -- ISO8601
  ended_at          TEXT,
  duration_sec      INTEGER,

  -- -------- 采集侧元数据（Apple Watch / NetPulse 接入新增）--------
  worn_wrist        TEXT,                     -- left | right，决定正/反手的旋转符号
  source            TEXT,                     -- acemate | netpulse_watch，标记数据来源端
  external_id       TEXT,                     -- 采集端原始会话 ID，跨端幂等去重键

  stroke_count      INTEGER DEFAULT 0,
  -- 击球分项计数：由逐拍分类结果按场聚合，供 G2「配平门禁」校验
  -- 校验式：forehand+backhand+serve+slice+volley+smash ≈ stroke_count
  forehand_count    INTEGER,
  backhand_count    INTEGER,
  serve_count       INTEGER,
  slice_count       INTEGER,
  volley_count      INTEGER,
  smash_count       INTEGER,
  rally_max         INTEGER,
  distance_km       REAL,
  calories_kcal     REAL,
  avg_hr            INTEGER,
  max_hr            INTEGER,
  avg_speed_kmh     REAL,
  peak_speed_kmh    REAL,
  forehand_avg_kmh  REAL,
  backhand_avg_kmh  REAL,
  serve_avg_kmh     REAL,
  serve_peak_kmh    REAL,
  spin_rpm          INTEGER,
  sweet_spot_rate   REAL,
  unforced_errors   INTEGER,
  winners           INTEGER,
  hr_zone           TEXT,                     -- 心率区间分布 JSON
  notes             TEXT,

  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  deleted_at        TEXT,
  version           INTEGER NOT NULL DEFAULT 1,
  sync_status       TEXT    NOT NULL DEFAULT 'synced',
  server_seq        INTEGER,

  -- 活行必须有 student_id / started_at；墓碑行不受限（见 students 表注释）
  CHECK (deleted_at IS NOT NULL OR (student_id IS NOT NULL AND started_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_sessions_user    ON training_sessions(user_id, deleted_at);
CREATE INDEX IF NOT EXISTS idx_sessions_student ON training_sessions(student_id, started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_seq     ON training_sessions(server_seq);
-- 跨端幂等：同一采集来源的同一会话 ID 只能存在一行（部分索引，不影响手工录入的历史数据）
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_external
  ON training_sessions(source, external_id) WHERE external_id IS NOT NULL;

-- ------------------------------- 3. 击球记录 --------------------------------
-- 同步主体之二：会话内的单次击球样本（App 侧高频写入，必须幂等 + 增量）
CREATE TABLE IF NOT EXISTS stroke_records (
  id                TEXT PRIMARY KEY,
  user_id           TEXT NOT NULL,
  session_id        TEXT,                     -- 关联 training_sessions.id

  seq_in_session    INTEGER,                  -- 会话内序号
  stroke_type       TEXT,                     -- forehand | backhand | serve | slice | volley | smash
  is_slice          INTEGER DEFAULT 0,
  speed_kmh         REAL,
  spin_rpm          INTEGER,
  spin_type         TEXT,                     -- top | back | flat
  sweet_spot        INTEGER DEFAULT 0,        -- 是否命中甜区
  depth_m           REAL,                     -- 落点深度
  landing_zone      TEXT,                     -- deep | mid | short
  lateral_offset_m  REAL,                     -- 左右偏差
  net_clearance_m   REAL,                     -- 过网高度
  impact_ms         INTEGER,                  -- 击球瞬间距会话开始的毫秒偏移
  confidence        REAL,                     -- 算法置信度
  anomaly           INTEGER DEFAULT 0,        -- 异常抖动标记

  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  deleted_at        TEXT,
  version           INTEGER NOT NULL DEFAULT 1,
  sync_status       TEXT    NOT NULL DEFAULT 'synced',
  server_seq        INTEGER,

  -- 活行必须有 session_id / stroke_type；墓碑行不受限（见 students 表注释）
  CHECK (deleted_at IS NOT NULL OR (session_id IS NOT NULL AND stroke_type IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_strokes_user    ON stroke_records(user_id, deleted_at);
CREATE INDEX IF NOT EXISTS idx_strokes_session ON stroke_records(session_id, seq_in_session);
CREATE INDEX IF NOT EXISTS idx_strokes_seq     ON stroke_records(server_seq);

-- ------------------------------- 4. 反馈工单 --------------------------------
-- 同步主体之三：App 内提交的建议 / 缺陷工单
CREATE TABLE IF NOT EXISTS feedback_tickets (
  id                TEXT PRIMARY KEY,
  user_id           TEXT NOT NULL,
  student_id        TEXT,

  code              TEXT,                     -- 工单号，例：FB-20241028-09
  title             TEXT,
  body              TEXT,
  category          TEXT,                     -- 功能新增 | 硬件交互 | 算法优化 | UI 与交互 ...
  status            TEXT,                     -- triage | new | sprint | rejected | done
  status_label      TEXT,                     -- 展示文案，例：待评审 (Triage)
  priority          TEXT,                     -- 紧急 | 高 | 中 | 低
  votes             INTEGER DEFAULT 0,        -- 附议数
  reporter_name     TEXT,
  reporter_meta     TEXT,                     -- 例：NTRP 4.5 · Yonex EZONE
  reporter_device   TEXT,                     -- 例：3 小时前来自 iPhone 15 Pro
  source            TEXT,                     -- iOS App | Apple Watch | Web
  roadmap           TEXT,                     -- 例：v2.5 灰度中
  assignee          TEXT,
  occurred_at       TEXT,

  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  deleted_at        TEXT,
  version           INTEGER NOT NULL DEFAULT 1,
  sync_status       TEXT    NOT NULL DEFAULT 'synced',
  server_seq        INTEGER,

  -- 活行必须有 title；墓碑行不受限（见 students 表注释）
  CHECK (deleted_at IS NOT NULL OR title IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_feedback_user   ON feedback_tickets(user_id, deleted_at);
CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback_tickets(status, occurred_at);
CREATE INDEX IF NOT EXISTS idx_feedback_seq    ON feedback_tickets(server_seq);

-- ---------------------------- 5. 用户画像分群 --------------------------------
-- 由后端聚合计算出的分群（服务端生成，客户端只读）
CREATE TABLE IF NOT EXISTS persona_segments (
  id                TEXT PRIMARY KEY,
  user_id           TEXT NOT NULL,
  code              TEXT,                     -- ARCHETYPE 01 ...
  name              TEXT NOT NULL,            -- 底线进攻型重炮手
  name_en           TEXT,
  subtitle          TEXT,
  headcount         INTEGER,
  share_pct         REAL,
  description       TEXT,
  metrics_json      TEXT,                     -- 各维度指标 JSON
  nt_range          TEXT,
  insight           TEXT,
  color             TEXT,                     -- 该分群的主题色
  sort_order        INTEGER DEFAULT 0,

  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  deleted_at        TEXT,
  version           INTEGER NOT NULL DEFAULT 1,
  sync_status       TEXT    NOT NULL DEFAULT 'synced',
  server_seq        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_persona_seq ON persona_segments(server_seq);

-- ------------------------------- 6. 其他聚合 --------------------------------
-- NTRP 基准常模（云端常模，供横向对比）
CREATE TABLE IF NOT EXISTS nt_benchmarks (
  id                TEXT PRIMARY KEY,
  user_id           TEXT NOT NULL DEFAULT 'system',
  level             TEXT NOT NULL,            -- 3.5
  sample_size       INTEGER,
  avg_speed_kmh     REAL,
  sweet_spot_rate   REAL,
  forehand_kmh      REAL,
  serve_kmh         REAL,
  spin_rpm          INTEGER,
  algorithm_version TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  deleted_at        TEXT,
  version           INTEGER NOT NULL DEFAULT 1,
  sync_status       TEXT NOT NULL DEFAULT 'synced',
  server_seq        INTEGER
);

-- 平台级指标（概览看板的 KPI / 热力图 / 构成分布等）
-- 以 key-value 存 JSON，避免为展示型聚合建大量窄表
CREATE TABLE IF NOT EXISTS platform_metrics (
  id                TEXT PRIMARY KEY,
  user_id           TEXT NOT NULL DEFAULT 'system',
  metric_key        TEXT NOT NULL UNIQUE,
  value_json        TEXT NOT NULL,
  label             TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  deleted_at        TEXT,
  version           INTEGER NOT NULL DEFAULT 1,
  sync_status       TEXT NOT NULL DEFAULT 'synced',
  server_seq        INTEGER
);

-- ---------------------- 7. 同步：操作幂等表（核心） --------------------------
-- 需求：「同步接口必须幂等，支持重试，不能重复插入」
-- 做法：客户端为每个写操作生成唯一 operation_id；服务端以此为**主键**，
--       同一 operation_id 重复到达时直接回放上次结果，不再改库。
CREATE TABLE IF NOT EXISTS sync_operations (
  operation_id      TEXT PRIMARY KEY,         -- 幂等键（客户端生成 UUID）
  user_id           TEXT NOT NULL,
  device_id         TEXT,                     -- 来源设备，便于排障

  entity_type       TEXT NOT NULL,            -- training_session | stroke_record | ...
  entity_id         TEXT NOT NULL,
  action            TEXT NOT NULL,            -- create | update | delete
  payload           TEXT,                     -- 客户端提交的字段 JSON
  client_updated_at TEXT,                     -- 客户端声明的时间（LWW 输入）

  -- 服务端裁决结果
  result            TEXT NOT NULL,            -- applied | duplicate | conflict_lost | rejected
  reason            TEXT,
  server_version    INTEGER,
  server_updated_at TEXT,
  server_seq        INTEGER,                  -- 本次写入分配的游标（未写入则 NULL）
  applied           INTEGER NOT NULL DEFAULT 0, -- 1=本次真正改了库，0=未改（重复/落败）

  received_at       TEXT NOT NULL             -- 服务器接收时间（审计用，不参与 LWW）
);
CREATE INDEX IF NOT EXISTS idx_sync_ops_entity ON sync_operations(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_sync_ops_user   ON sync_operations(user_id, received_at);

-- --------------------- 8. 同步：变更日志（服务器游标） ----------------------
-- 需求：「拉取使用服务器游标或 server_updated_at，不要依赖客户端时间」
-- 做法：seq 为**全库单调递增**游标（AUTOINCREMENT），跨实体统一，
--       客户端只需保存一个 next_cursor 即可增量拉取全部实体。
--       每条记录带该实体的**完整快照**，因此重复拉取/乱序应用都安全。
CREATE TABLE IF NOT EXISTS sync_changelog (
  seq               INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id           TEXT NOT NULL,
  entity_type       TEXT NOT NULL,
  entity_id         TEXT NOT NULL,
  action            TEXT NOT NULL,            -- create | update | delete
  version           INTEGER NOT NULL,
  updated_at        TEXT NOT NULL,            -- 该次写入后的实体 updated_at
  deleted_at        TEXT,
  payload           TEXT NOT NULL,            -- 实体完整快照 JSON
  operation_id      TEXT,                     -- 溯源到具体同步操作
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changelog_user_seq ON sync_changelog(user_id, seq);

-- --------------------- 9. 同步：拉取审计（便于排障） ------------------------
CREATE TABLE IF NOT EXISTS sync_pulls (
  id                TEXT PRIMARY KEY,
  user_id           TEXT NOT NULL,
  device_id         TEXT,
  from_cursor       INTEGER NOT NULL,
  to_cursor         INTEGER NOT NULL,
  change_count      INTEGER NOT NULL,
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pulls_user ON sync_pulls(user_id, created_at);
