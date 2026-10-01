# 离线优先同步协议

本文件是 **App 端同步层与后端之间的契约**。客户端同学只需读这一份即可实现同步。

---

## 0. 需求对照表

用户提出的每一条一致性要求，与实现位置的对应关系：

| # | 要求 | 实现位置 | 验证 |
|---|---|---|---|
| 1 | 后端数据库是唯一真实数据源 | 服务端不保存客户端队列；所有读取来自 `var/acemate.db` | — |
| 2 | 客户端本地只做缓存和待同步队列 | 见 §3 客户端模型 | — |
| 3 | 所有写操作先写本地，再进入同步队列 | 见 §3 | — |
| 4 | 同步时使用增量推送和增量拉取 | `POST /api/sync/push` + `GET /api/sync/pull`（游标增量） | `test_sync.py [8]` |
| 5 | 冲突默认「最后写入优先（LWW）」，以 `updated_at` 为准 | `sync.apply_operation()`：`client_updated_at > row.updated_at` **严格大于**才覆盖 | `test_sync.py [3]` |
| 6 | 删除使用软删除，用 `deleted_at` 标记 | `sync.py` 删除分支写 `deleted_at` + 墓碑行；schema 里 4 张表用 CHECK 约束表达 | `test_sync.py [4][5]` |
| 7 | 同步接口必须幂等，支持重试，不能重复插入 | `sync_operations.operation_id` 主键去重（双闸） | `test_sync.py [1][2]` |
| 8 | 每条记录包含 id / user_id / created_at / updated_at / deleted_at / version / sync_status | 见 [DATA_MODEL.md § 同步列](DATA_MODEL.md) | `test_sync.py [7]` |
| 9 | 同步操作包含 operation_id / entity_type / entity_id / action / payload / client_updated_at | 见 §2 操作结构 | — |
| 10 | 拉取使用服务器游标或 server_updated_at，不依赖客户端时间 | `sync_changelog.seq`（全库单调递增 AUTOINCREMENT） | `test_sync.py [8]` |

---

## 1. 核心不变量

1. **服务端权威**：客户端永远不假设自己的数据是对的；写入要等服务端裁决结果。
2. **写先落本地**：用户的任何操作先在本地库完成，立刻可见，再入队。App 可以长时间完全离线。
3. **推送幂等**：每个写操作由客户端生成 `operation_id`。网络超时后**原样重发**即可，不会重复插入。
4. **拉取不依赖客户端时间**：只用服务端游标 `seq` 排序定位。客户端时钟可被用户修改、可回拨，绝不可信。
5. **删除是软删除**：被删的行保留 + 打 `deleted_at`，这样删除事件才能同步给其它设备。
6. **每条变更携带完整快照**：客户端可以乱序、重复应用同一条变更，结果一致。

---

## 2. 数据契约

### 2.1 本地记录（7 个通用同步列）

每张本地表都要有这 7 列，服务端同名字段一一对应：

| 字段 | 类型 | 语义 |
|---|---|---|
| `id` | TEXT | 全局唯一 id，**由客户端生成**（UUIDv4 / ULID）。离线也能创建，联网后不会重号 |
| `user_id` | TEXT | 归属用户 |
| `created_at` | TEXT | ISO8601 UTC 毫秒，首次创建时间 |
| `updated_at` | TEXT | ISO8601 UTC 毫秒，最后修改时间 —— **LWW 的比较依据** |
| `deleted_at` | TEXT / NULL | 非 NULL 即视为已删除（软删除） |
| `version` | INTEGER | 乐观并发版本号，服务端每次成功写入 +1 |
| `sync_status` | TEXT | `synced` / `pending` / `conflict` |

> **时间格式必须统一**：`2026-09-24T10:00:00.000Z`（UTC、毫秒、`Z` 后缀）。
> 这样字符串字典序 == 时间序，服务端可以直接用字符串比较实现 LWW，无需解析。
> 服务端 `db.normalize_ts()` 会兼容 `+08:00`、空格分隔等写法，但客户端应统一输出标准格式。

### 2.2 同步操作（push 的元素）

```json
{
  "operation_id":    "8f14e45f-ea0b-4c1a-9c1e-2f6b7d0a1234",
  "entity_type":     "training_session",
  "entity_id":       "ts-01J8Z9K2M4",
  "action":          "update",
  "payload":         { "notes": "改了备注", "stroke_count": 386 },
  "client_updated_at": "2026-09-24T10:00:00.000Z"
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `operation_id` | ✅ | **幂等键**。每个写操作生成一次，重试时必须复用同一个值 |
| `entity_type` | ✅ | 见下表 |
| `entity_id` | ✅ | 记录 id（`create` 时就是客户端生成的 id） |
| `action` | ✅ | `create` / `update` / `delete` |
| `payload` | | 字段字典。服务端按白名单过滤，未在白名单内的字段直接忽略 |
| `client_updated_at` | | 客户端声明的时间，LWW 的输入。缺失/不可解析时回退为服务端当前时间 |

**可同步实体**（`entity_type` → 表 / 必填字段）：

| entity_type | 表 | 创建必填 |
|---|---|---|
| `student_profile` | `students` | `name` |
| `training_session` | `training_sessions` | `student_id`、`started_at` |
| `stroke_record` | `stroke_records` | `session_id`、`stroke_type` |
| `feedback_ticket` | `feedback_tickets` | `title` |

> 字段白名单见 `sync.py` 的 `SYNCABLE[...]['fields']`。
> **有意排除** `version` / `sync_status` / `server_seq` —— 这些是服务端专属列，客户端写不进来。

---

## 3. 客户端模型

```
┌────────────────────────────┐
│ 本地库（缓存，可读可写）      │  ← UI 只读这里，永远秒开
└──────────┬─────────────────┘
           │ 用户操作
           ▼
┌────────────────────────────┐
│ 待同步队列 sync_queue       │  ← 持久化，进程被杀也不丢
│  operation_id (PK)          │
│  entity_type / entity_id    │
│  action / payload           │
│  client_updated_at          │
│  retry_count                │
└──────────┬─────────────────┘
           │ 联网时批量 flush
           ▼
     POST /api/sync/push
           │
           ▼ 按裁决结果更新本地
```

### 写入流程（必须按此顺序）

1. **生成 id**（`create` 时）：本地生成 UUID，写入本地库，`sync_status = 'pending'`。
2. **写本地库**：UI 立刻可见。
3. **入队**：生成 `operation_id`，把操作追加到 `sync_queue`。
4. **（可选）尝试 flush**：有网就立即推送，失败不报错、留在队列里。

> ⚠️ 顺序不能反。先推后写会导致「推送成功但本地没落库」的窗口期，UI 会跳变。

---

## 4. 推送：`POST /api/sync/push`

### 请求

```
POST /api/sync/push
X-User-Id: u_demo
X-Device-Id: iphone-15-pro

[ { "operation_id": "...", "entity_type": "...", ... }, ... ]     ← 裸数组，最多 500 条
```

### 响应

```json
{
  "received_at": "2026-09-24T11:02:33.412Z",
  "total": 3, "applied": 2, "duplicate": 1, "conflict_lost": 0, "rejected": 0,
  "cursor": 128,
  "results": [
    {
      "operation_id": "…", "entity_type": "training_session", "entity_id": "ts-1",
      "action": "update",
      "result": "applied",
      "applied": true,
      "server_version": 3,
      "server_updated_at": "2026-09-24T11:02:33.412Z",
      "server_seq": 128,
      "reason": "LWW 客户端版本更新，已应用"
    }
  ]
}
```

`cursor` = **本次写入后服务端的最新游标**，客户端可以直接把它当作下次拉取的起点，省一次往返。

### 四种裁决结果

| result | applied | 含义 | 客户端应该做什么 |
|---|---|---|---|
| `applied` | ✅ | 已写入 | 本地 `sync_status = 'synced'`，记下 `server_version`；出队 |
| `duplicate` | ❌ | `operation_id` 之前处理过 | **出队**。这是重试成功的正常结果，不是错误 |
| `conflict_lost` | ❌ | LWW 落败，服务端版本更新 | 本地改用响应里的 `server_version` / `server_updated_at`，**等下次 pull 拿服务端内容**；出队 |
| `rejected` | ❌ | 请求不合法（缺必填、未知实体、越权…） | 出队并**记录日志**。重试无用，需要修数据；`reason` 里有原因 |

> **关键**：`duplicate` 和 `conflict_lost` 都不是错误。客户端只要**无条件出队**，
> 这两类结果出现多少次都不会改库（幂等）。只有 `rejected` 值得上报。

### 幂等的两道闸

`sync.apply_operation()` 里：

1. **闸一**：查 `sync_operations.operation_id`。命中 → 直接回放上次的裁决结果，不改库。
2. **闸二**：未命中 → **先把 `operation_id` 占位插入**（`result='pending'`），再执行业务逻辑。
   这样同一批次里出现两个相同 `operation_id` 时，第二个会撞主键 → 按 `duplicate` 处理。

> 为什么要闸二：只靠闸一的话，同一批次内的重复 `operation_id` 在批次开始时都还没写入，
> 会双双通过查重 → 重复插入。闸二把「查重」变成「插入即占位」，依赖数据库主键唯一性，
> 在 `BEGIN IMMEDIATE` 的写锁内绝对可靠。

### 批次语义

整个批次跑在**一个**事务里：

- 逐条独立裁决 —— 一条 `rejected` 不影响其它条；
- 但所有变更**一起提交或一起回滚**。客户端重试时不会看到「一半已写」的中间态。

---

## 5. 拉取：`GET /api/sync/pull`

### 请求

```
GET /api/sync/pull?cursor=128&limit=200&entity_types=training_session,stroke_record
X-User-Id: u_demo
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `cursor` | 0 | 上次拿到的 `next_cursor`。首次传 0 |
| `limit` | 200 | 1~1000 |
| `entity_types` | 全部 | 逗号分隔 |

### 响应

```json
{
  "cursor": 128,
  "next_cursor": 340,
  "has_more": true,
  "count": 200,
  "server_time": "2026-09-24T11:05:00.000Z",
  "changes": [
    {
      "seq": 129,
      "entity_type": "training_session",
      "entity_id": "ts-1",
      "action": "update",
      "version": 3,
      "updated_at": "2026-09-24T11:02:33.412Z",
      "deleted_at": null,
      "payload": { "id": "ts-1", "user_id": "u_demo", "...": "完整快照" }
    }
  ]
}
```

### 游标语义

- `seq` 是**全库单调递增**的 `AUTOINCREMENT`，跨实体统一。
  客户端**只需保存一个数字**，就能增量拉取所有实体 —— 比按表各存一个时间戳可靠得多
  （时间戳会因时钟回拨、同毫秒并列而出错）。
- 服务端内部用 `WHERE seq > ? ORDER BY seq ASC LIMIT ?+1`，
  多取一条只为判断 `has_more`，返回时裁掉。
- **`has_more = true` 时必须继续拉**，不能停。

### 应用变更（客户端）

```
for change in changes:
    local = 本地库.find(change.entity_type, change.entity_id)

    # 1. 跳过陈旧变更（乱序到达时）
    if local and local.version >= change.version: continue

    # 2. 落库：payload 是完整快照，直接整体覆盖
    local.upsert(change.payload)
    local.version    = change.version
    local.deleted_at = change.deleted_at      # 非 NULL → 本地也标记删除（或从列表隐藏）
    local.sync_status = 'synced'

# 3. 原子保存「数据 + 游标」——必须在同一事务里
save_atomically(next_cursor)
```

> **最后一步必须原子**。如果先存数据后存游标、中间崩溃，下次会重复拉同一批（可接受，因为幂等）；
> 但如果先存游标后存数据、中间崩溃，就会**永久丢数据**。
> 所以顺序是：写数据 → 写游标 → 一起提交。

---

## 6. 新设备基线：`GET /api/sync/snapshot`

新装的 App 若从 `cursor=0` 开始 pull，会把全部历史变更回放一遍，很浪费。

```
GET /api/sync/snapshot
```

返回**当前全量状态**（**含墓碑行** —— 这样新设备也知道哪些 id 已被删除）以及当前游标：

```json
{
  "cursor": 340,
  "server_time": "2026-09-24T11:05:00.000Z",
  "entity_types": ["student_profile", "..."],
  "counts": { "student_profile": 1, "training_session": 40, "...": 0 },
  "entities": {
    "student_profile": [ { "id": "...", "deleted_at": null, "...": "..." } ],
    "training_session": [ "...", { "id": "ts-x", "deleted_at": "2026-09-24T13:00:00.000Z" } ]
  }
}
```

**客户端流程**：

1. `GET /api/sync/snapshot` → 清空本地库，整体写入；
2. 本地保存 `localCursor = response.cursor`；
3. 之后正常走 `pull(cursor=localCursor)` 增量。

---

## 7. 冲突处理：LWW 的细节

判定式（服务端）：

```
client_updated_at  >  row.updated_at     →  覆盖（applied）
client_updated_at  <= row.updated_at     →  保留服务端（conflict_lost）
```

### 为什么是「严格大于」

这不只是随机选的边界，它**同时解决了幂等**：

重放一个已经应用过的操作时，`client_updated_at` 恰好**等于**服务端 `updated_at`
（因为服务端上次就是用它写的 `updated_at`），于是判定为 `conflict_lost`、不改库。

也就是说，**即使 `sync_operations` 去重表被清空，重放也不会造成数据损坏** —— 这是第二层保险。

### 时间相同时怎么定胜负？

**服务端赢**。因为客户端声明的是一份可能存在、也可能不存在的记录，
而服务端持有的是已经落库的事实。平局保留事实。

### 客户端时间不可信怎么办？

协议层面接受客户端的 `client_updated_at`（这是 LWW 的定义），但要清楚其风险：
用户改系统时间可以"抢占"写入。

现有缓解：
- `sync_operations.received_at` 记录**服务端**接收时间，与 `client_updated_at` 分开存，便于审计；
- 若客户端时间缺失/不可解析，回退为服务端当前时间。

> 如果将来这个风险不可接受，把 LWW 的输入从 `client_updated_at` 换成
> `server_received_at`（服务端接收顺序）即可，判定式与幂等性都不变。
> 代价是离线很久的设备联网后，其改动会被后到的在线设备覆盖。

---

## 8. 软删除

### 服务端的完整行为（`sync.apply_operation()` 的 delete 分支）

| 情况 | 处理 |
|---|---|
| 行存在、未删除、`client_ts > updated_at` | `UPDATE SET deleted_at = client_ts, version += 1`；写 changelog（快照）；**行保留** |
| 行存在、已是删除态 | `conflict_lost`（重复删除是幂等的） |
| 行存在、`client_ts <= updated_at` | `conflict_lost`（LWW 落败） |
| **行不存在** | 插入**墓碑行**：只填同步列（`id/user_id/created_at/updated_at/deleted_at/version/sync_status`），`deleted_at = created_at = version 1` |

> 最后一行很重要：客户端删掉一条服务端从未见过的记录（例如它的 create 曾被 `rejected`），
> 服务端也必须留一个墓碑，否则这个删除事件无法传播到其它设备 ——
> 别的设备下次 snapshot 时看不到这个 id，就会以为它"从未存在"，而不是"已删除"。

### 墓碑行与 schema 约束

墓碑没有业务字段，因此这 4 张表的业务列**不能写 `NOT NULL`**。真实规则用表级 CHECK 表达：

```sql
-- students
CHECK (deleted_at IS NOT NULL OR name IS NOT NULL)

-- training_sessions
CHECK (deleted_at IS NOT NULL OR (student_id IS NOT NULL AND started_at IS NOT NULL))

-- stroke_records
CHECK (deleted_at IS NOT NULL OR (session_id IS NOT NULL AND stroke_type IS NOT NULL))

-- feedback_tickets
CHECK (deleted_at IS NOT NULL OR title IS NOT NULL)
```

即：**活行必须有业务必填字段，墓碑不要求**。与 `SYNCABLE[...]['required']` 一一对应。

> 📌 这是实测踩出来的 bug：最初业务列写的是 `NOT NULL`，
> 删除一条服务端没见过的记录会抛 `sqlite3.IntegrityError: NOT NULL constraint failed`，
> 异常穿透整个 push 事务 → **整批回滚 + HTTP 500**。
> 回归用例见 `scripts/test_sync.py` 的 `[5]`。

### 复活墓碑

对墓碑做 `update` 会清空 `deleted_at`（即"恢复删除"）。因为墓碑可能没有业务字段，
服务端会先校验 `payload + 现有行` 能否凑齐必填字段：

- 能 → `applied`
- 不能 → `rejected`，reason 为「复活已删除记录时缺少必填字段：…」

这样保证任何时候都不会产生一条违反 CHECK 的活行（否则同样会整批 500）。

### 客户端处理删除

```swift
// 收到 change
if let deletedAt = change.deletedAt, !deletedAt.isEmpty {
    // 方案 A（推荐）：保留墓碑行，查询时加 WHERE deleted_at IS NULL
    local.markDeleted(id: change.entityId, at: deletedAt, version: change.version)
    // 方案 B：直接从本地表删掉。风险：本地不再知道这个 id 已删除，
    //        若之后从别处又收到该 id 的旧变更，会把它"复活"
}
```

---

## 9. 完整时序（含离线）

```
设备 A（在线）                       服务端                        设备 B（离线 3 天后上线）
─────────────                        ──────                        ─────────────────────
用户改备注
  ├ 写本地库 (status=pending)
  └ 入队 op-1
POST /push [op-1] ────────────────►  闸一未命中
                                     闸二占位 op-1
                                     LWW: 10:00 > 09:30 ✓
                                     UPDATE + changelog(seq=101)
                            ◄──────  {applied, version=2, cursor=101}
出队 op-1，status=synced
                                     ...期间设备 B 离线，A 又改了 3 次 → seq 102,103,104
                                                                  设备 B 上线
                                                                  ├ 用户离线时改了备注
                                                                  ├ 写本地 + 入队 op-9
                                                                  POST /push [op-9] ─►  LWW 比较
                                                                         若 B 的 client_ts 更晚 → applied (seq=105)
                                                                         否则 conflict_lost
                                                                  ◄───────────────────
GET /pull?cursor=100 ────────────►  seq>100 的变更
                            ◄──────  101..105（含 A 的 4 次 + B 自己的 1 次）
应用：按 version 跳过陈旧的，整体覆盖，deleted_at 同步
原子保存 next_cursor=105
```

> 注意 `pull` 会把设备 B **自己推送的变更**也带回来（seq=105 就是 B 的 op-9）。
> 这是**刻意的**：它让客户端只需一套"应用远端变更"的代码，不必区分来源。
> 因为变更带完整快照 + `version`，重复应用自己的变更完全安全。

---

## 10. 建议的客户端参数

| 项 | 建议值 | 理由 |
|---|---|---|
| push 批大小 | 200 | 上限 500；留余量，避免单个请求体过大 |
| pull `limit` | 200~500 | 上限 1000；`has_more=true` 就继续拉 |
| push 重试 | 指数退避 1s → 2s → 4s → … → 上限 60s | 幂等，重试零成本 |
| 触发时机 | App 进前台、操作后 3s 防抖、网络从断到通 | — |
| 首次安装 | 先 `snapshot`，再按返回游标 `pull` | 避免回放全部历史 |
| 时钟 | 统一输出 UTC 毫秒 `...Z` | 保证字符串比较 == 时间比较 |

---

## 11. 排障

| 现象 | 排查 |
|---|---|
| 某次 push 一直返回 `duplicate` | 正常。说明之前那次其实成功了，客户端没收到响应 |
| 数据"改不动" | 看是不是 `conflict_lost`：客户端 `client_updated_at` 没有严格大于服务端 `updated_at` |
| 一条记录反复出现 | 客户端没正确处理 `deleted_at`，把墓碑行当活行显示了 |
| pull 卡在同一个 cursor 不动 | `has_more` 是否为 true 却没继续拉 |
| 服务端 push 返回 500 | 查日志。历史上出现过「墓碑插入违反 NOT NULL」，见 §8 |
| 想审计某设备卡在哪 | `GET /api/sync/status` 的 `recent_pulls`；或直接查 `sync_pulls` 表 |

```sql
-- 某个 operation_id 到底发生了什么
SELECT * FROM sync_operations WHERE operation_id = '…';

-- 某条实体的完整变更史
SELECT seq, action, version, updated_at, deleted_at FROM sync_changelog
 WHERE entity_type='training_session' AND entity_id='ts-1' ORDER BY seq;

-- 当前水位
SELECT COALESCE(MAX(seq),0) FROM sync_changelog;
```
