# 数据模型

DDL 事实源：`server/schema.sql`（带完整注释）。本文件说明**为什么这样设计**与字段语义。

---

## 1. 表清单

| # | 表 | 类别 | 可同步 | 说明 |
|---|---|---|---|---|
| 1 | `students` | 业务 | ✅ `student_profile` | 学员档案 |
| 2 | `training_sessions` | 业务 | ✅ `training_session` | 训练会话（同步主体） |
| 3 | `stroke_records` | 业务 | ✅ `stroke_record` | 会话内单次击球样本（同步主体，高频写） |
| 4 | `feedback_tickets` | 业务 | ✅ `feedback_ticket` | 反馈工单 |
| 5 | `persona_segments` | 业务 | ❌ | 用户画像分群（服务端聚合，客户端只读） |
| 6 | `nt_benchmarks` | 聚合 | ❌ | NTRP 常模基准 |
| 7 | `platform_metrics` | 聚合 | ❌ | 平台级展示型指标（key → JSON） |
| 8 | `sync_operations` | 同步 | — | **幂等去重表**（核心） |
| 9 | `sync_changelog` | 同步 | — | **服务器游标**（核心） |
| + | `sync_pulls` | 同步 | — | 拉取审计 |

---

## 2. 通用同步列（所有业务表都有）

| 列 | 类型 | 约束 | 语义 |
|---|---|---|---|
| `id` | TEXT | PK | 全局唯一，**客户端生成**（UUIDv4/ULID）→ 离线可创建、联网不重号 |
| `user_id` | TEXT | NOT NULL | 归属用户；多租户隔离 + 增量拉取的过滤依据 |
| `created_at` | TEXT | NOT NULL | ISO8601 UTC 毫秒，首次创建 |
| `updated_at` | TEXT | NOT NULL | ISO8601 UTC 毫秒，最后修改 —— **LWW 比较依据** |
| `deleted_at` | TEXT | | 软删除标记，非 NULL 即已删除（行保留以便同步） |
| `version` | INTEGER | NOT NULL DEFAULT 1 | 乐观并发版本号，每次成功写入 +1 |
| `sync_status` | TEXT | NOT NULL DEFAULT 'synced' | `synced` / `pending` / `conflict` |
| `server_seq` | INTEGER | | 该行最近一次变更的游标（= `sync_changelog.seq`） |

### 索引约定

每张同步表都建了 3 个索引：

```sql
CREATE INDEX idx_<table>_user ON <table>(user_id, deleted_at);  -- 拉取 / 列表
CREATE INDEX idx_<table>_seq  ON <table>(server_seq);           -- 游标定位
-- 业务查询索引，例如
CREATE INDEX idx_strokes_session ON stroke_records(session_id, seq_in_session);
```

> `(user_id, deleted_at)` 是复合索引，因为**所有**列表查询都带 `deleted_at IS NULL` 过滤。

### 业务列的 NOT NULL 与 CHECK

4 张可同步表的业务列**不能**写 `NOT NULL`，因为要被客户端删除的、服务端从未见过的记录
需要插入**墓碑行**（无业务数据）。真实规则用表级 CHECK 表达：

```sql
students            CHECK (deleted_at IS NOT NULL OR name IS NOT NULL)
training_sessions   CHECK (deleted_at IS NOT NULL OR (student_id IS NOT NULL AND started_at IS NOT NULL))
stroke_records      CHECK (deleted_at IS NOT NULL OR (session_id IS NOT NULL AND stroke_type IS NOT NULL))
feedback_tickets    CHECK (deleted_at IS NOT NULL OR title IS NOT NULL)
```

即 **活行必须有业务必填字段，墓碑不要求**，与 `SYNCABLE[...]['required']` 一一对应。
详见 [SYNC_PROTOCOL.md § 8](SYNC_PROTOCOL.md)。

> 注意：这些业务列是**可空**的，所以查询展示时必须容错。
> `data_api.num()` 已把 `None` 渲染成 `—`。

---

## 3. `sync_operations` —— 幂等去重表

需求：「同步接口必须幂等，支持重试，不能重复插入」。

```sql
CREATE TABLE sync_operations (
  operation_id      TEXT PRIMARY KEY,   -- ← 幂等键
  user_id           TEXT NOT NULL,
  device_id         TEXT,
  entity_type       TEXT NOT NULL,
  entity_id         TEXT NOT NULL,
  action            TEXT NOT NULL,
  payload           TEXT,
  client_updated_at TEXT,

  -- 服务端裁决结果
  result            TEXT NOT NULL,      -- applied|duplicate|conflict_lost|rejected
  reason            TEXT,
  server_version    INTEGER,
  server_updated_at TEXT,
  server_seq        INTEGER,
  applied           INTEGER NOT NULL DEFAULT 0,

  received_at       TEXT NOT NULL       -- 服务器接收时间（审计，不参与 LWW）
);
```

### 两道幂等闸

1. **闸一**：`SELECT ... WHERE operation_id = ?`，命中 → 回放上次结果，`result='duplicate'`，不改库。
2. **闸二**：未命中 → **先占位插入**（`result='pending'`）再执行业务逻辑。
   同批次内的重复 `operation_id` 会撞主键 → 按 `duplicate`。

**为什么必须闸二**：只靠闸一的话，同一批次开始查重时两条重复操作都还没写入，
会双双通过 → 重复插入。闸二把"查重"变成"插入即占位"，依赖数据库主键唯一性；
在 `BEGIN IMMEDIATE` 写锁内这是可靠的。

> `applied` 列独立于 `result`：`applied=1` 表示**本次真正改了库**。
> `duplicate` / `conflict_lost` / `rejected` 都是 `0`。用它一眼看出「服务端实际写了多少」。

---

## 4. `sync_changelog` —— 服务器游标

需求：「拉取使用服务器游标或 server_updated_at，不要依赖客户端时间」。

```sql
CREATE TABLE sync_changelog (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,  -- ← 全库单调递增游标
  user_id      TEXT NOT NULL,
  entity_type  TEXT NOT NULL,
  entity_id    TEXT NOT NULL,
  action       TEXT NOT NULL,     -- create | update | delete
  version      INTEGER NOT NULL,
  updated_at   TEXT NOT NULL,
  deleted_at   TEXT,              -- 客户端据此识别墓碑
  payload      TEXT NOT NULL,     -- 实体**完整快照** JSON
  operation_id TEXT,              -- 溯源到具体同步操作
  created_at   TEXT NOT NULL
);
```

### 为什么用单一自增整数而不是时间戳

| | 时间戳 | `seq`（本项目） |
|---|---|---|
| 时钟回拨 | 💥 漏数据 | ✅ 不受影响 |
| 同毫秒并列 | 💥 排序不稳定、可能漏 | ✅ 严格全序 |
| 客户端保存 | 每张表一个水位 | ✅ **只需一个数字** |
| 跨实体统一 | 需要额外协调 | ✅ 天然统一 |

服务端内部用 `WHERE seq > ? ORDER BY seq ASC LIMIT ?+1` 判 `has_more`。

### 为什么每条都存完整快照

让客户端可以**无序、重复**应用变更 —— 落库就是整体覆盖，不需要 diff 合并逻辑。
代价是存储冗余；对本项目的写入量完全可接受。

---

## 5. `platform_metrics` —— 展示型聚合

```sql
CREATE TABLE platform_metrics (
  id         TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL DEFAULT 'system',
  metric_key TEXT NOT NULL UNIQUE,   -- 例：'overview_kpis'
  value_json TEXT NOT NULL,          -- 任意 JSON
  label      TEXT,
  ...
);
```

**设计取舍**：管理页有大量「服务端算出来的展示型聚合」——热力图矩阵、构成分布、
雷达图坐标、NTRP 直方图、AI 诊断文案……它们：
- 无语义实体（不是"一条业务记录"）；
- 结构随图表变化；
- 不需要增量同步（服务端生成、客户端只读）。

给每个图表建窄表会产生几十张一次性表。用一个 key→JSON 表代替，
代价是**这些值没有 schema 约束** —— 改图表时必须同步改 `server/seed.py`。

统一读入口（失败返回默认值，页面不会因缺 key 而 500）：

```python
def metric(key, default=None):
    row = db.query_one('SELECT value_json FROM platform_metrics WHERE metric_key = ?', (key,))
    ...
```

### 当前 metric_key 清单

| 分组 | keys |
|---|---|
| 概览 | `overview_kpis` `online_now` `heatmap_24h_7d` `stroke_mix` `hardware_telemetry` `live_sessions` |
| NTRP | `ntrp_distribution` `ntrp_insight` `ntrp_radar` `ntrp_benchmark` |
| 反馈 | `feedback_digest` `feedback_hot_tags` `feedback_positive_rate` `feedback_kpis` `feedback_status_filters` `feedback_hardware_split` `feedback_categories` `feedback_timeline` `feedback_nextgen` |
| 训练对比 | `ai_diagnosis` `compare_radar` `compare_speed_bars` `compare_multi_rally` `training_history` `training_history_total` |
| 画像 | `persona_total_users` `persona_skill_histogram` `persona_donuts` `persona_insight` `persona_spotlight` `persona_recommendations` `persona_comparison` |

---

## 6. 业务表字段要点

### `students`（学员档案）

身份与设备：`name` `avatar_url` `avatar_initial` `tier`(VIP/Elite/Pro/Club/Standard)
`device_id` `watch_model` `batch`

技术档案：`years_playing` `hand`(右手/左手) `backhand`(双反/单反) `racket` `racket_tension`
`nt_level` `nt_score`

累计统计：`sessions_count` `hours_total` `strokes_total` `forehand_avg` `serve_peak`
`hit_rate`

> `hit_rate` 口径（**2026-10-03 修订**）：**识别置信度达标（`confidence` ≥ 0.60）的击球占比**。
> 原注释写「有效击球占比」，字面会被读成「球是否落在界内」—— 那是落点，腕表测不到。
> 新口径只依赖分类器置信度，是腕部侧完全可得的量，因此**保留**（登记编号 MD-032）。

> 硬件准入（2026-10-02 / 2026-10-03）：`sweet_spot` `spin_rate` `unforced_errors`
> `winners` 列**暂留但恒为 NULL**（腕部单点 IMU 测不到），已停止写入、
> 不再出现在任何接口与页面。权威清单见 `server/datasources.py` 的 `REMOVED`。

负荷：`training_load`(Optimal/High) `acwr`

展示：`last_training_at` `last_training_note` `location` `is_online`

> `last_training_at` / `last_training_note` 是**展示型文案**（如「刚结束 · 截击专项」），
> 允许非 ISO8601。它与 `training_sessions.started_at` 不同 —— 后者才参与业务计算。

### `training_sessions`（训练会话）

关联：`student_id`
上下文：`title` `session_type`(drill/match/rally/serve) `location` `court_type`
时间：`started_at` `ended_at` `duration_sec`
量：`stroke_count` `rally_max` `distance_km` `calories_kcal`
心率：`avg_hr` `max_hr` `hr_zone`（区间分布 JSON）
球速：`avg_speed_kmh` `peak_speed_kmh` `forehand_avg_kmh` `backhand_avg_kmh`
　　　`serve_avg_kmh` `serve_peak_kmh`
备注：`notes`
（`unforced_errors` / `winners` / `spin_rpm` / `sweet_spot_rate` 列暂留恒 NULL，
见 `server/datasources.py` 的 `REMOVED`）

### `stroke_records`（击球样本）

关联：`session_id` `seq_in_session`
分类：`stroke_type`(forehand/backhand/serve/slice/volley/smash) `is_slice`
物理：`speed_kmh`
质量：`impact_ms` `confidence` `anomaly`

> 硬件准入（2026-10-02）：`spin_rpm` / `spin_type` / `sweet_spot` / `depth_m` /
> `landing_zone` / `lateral_offset_m` / `net_clearance_m` 列暂留恒 NULL，
> 已停止写入 —— 均需拍面传感器或球的飞行轨迹，腕部单点 IMU 测不到。

> 这张表是**高频写入**主体（一次训练几百条），也是同步协议压力的主要来源。
> 单次 push 上限 500 条，客户端建议批 200。

### `feedback_tickets`（工单）

`code`(FB-YYYYMMDD-NN) `title` `body`
分类：`category` `status`(triage/new/sprint/rejected/done) `status_label` `priority`
社区信号：`votes`（附议数）
提交人：`reporter_name` `reporter_meta` `reporter_device` `source`
处理：`roadmap` `assignee` `occurred_at`

### `persona_segments`（画像分群）

`code`(ARCHETYPE 01) `name` `name_en` `subtitle` `headcount` `share_pct`
`description` `metrics_json`（各维度指标）`nt_range` `insight` `color` `sort_order`

> `metrics_json` 存的是该分群在雷达图 / 柱状图上的各维度值。

---

## 7. 加一张新的可同步表

1. **`schema.sql`**：建表，含 7 个同步列 + `server_seq`，
   业务列的必填约束写成 `CHECK (deleted_at IS NOT NULL OR <必填列> IS NOT NULL)`，
   加 3 个索引。
2. **`sync.py` 的 `SYNCABLE`**：加一项
   `{'table': ..., 'fields': [白名单], 'required': [必填]}`。
   > 白名单是**安全边界**，只列客户端可写字段；不要图省事写成「全部字段」。
3. **`server/seed.py`**：如需演示数据，加灌入逻辑。
4. **`server/routers/data_api.py`**：加只读接口（记得 `WHERE deleted_at IS NULL`）。
5. **`scripts/test_sync.py`**：在 `[4]` 的循环里加上这个 `entity_type`，
   自动覆盖「软删除不物理删」的断言。

> 别忘了 `[7]` 的断言会自动检查新表是否含全部 7 个同步列。

## 8. 重建数据库

```bash
python -m server.app --port 8787 --seed     # --seed → seed.run(force=True) → init_db(force=True)
```

`init_db(force=True)` 会删除 `var/acemate.db`（含 `-wal` / `-shm`）后重新建表。
**只在开发/演示环境使用**，会清空全部数据。

> ⚠️ **已知缺陷**：`init_db(force=True)` 删除文件前没有关闭本进程已缓存的连接
> （`db._local.conn`）。若同一进程里之前执行过查询，Windows 上可能删不掉文件，
> 或删掉后旧连接仍指向已删除的 inode 导致后续写入"丢失"。
> 规避：重建时用独立进程（`--seed` 在 `uvicorn.run` 之前执行，是独立的一次性动作）。
> 彻底修复：在 `force` 分支先 `conn.close()` 并 `_local.conn = None`。
