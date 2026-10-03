# 对接契约与参数 · 单一真源

> **三端对接只认这一份。** 字段名、接口路径、请求头、参数取值一律以此为准。
> 改动契约 = 改这一份 + 三端同步改代码 + 跑 [TESTING.md](TESTING.md) 的回归。
>
> 术语、枚举、时间口径、身份模型见 [OVERVIEW.md](OVERVIEW.md)。
> 通道设计见 [WATCH_CHANNEL.md](WATCH_CHANNEL.md)。

---

## 0. 参数速查表（**所有魔法值集中在这里**）

### 0.1 服务端地址与端口

| 参数 | 值 | 备注 |
|---|---|---|
| 默认端口 | `8787` | `run.py --port` 可改 |
| 默认监听 | `127.0.0.1` | 对外需 `--host 0.0.0.0` |
| 模拟器 baseURL | `http://127.0.0.1:8787` | 仅 iOS 模拟器可用 |
| **真机 baseURL** | `http://<Mac局域网IP>:8787` | ⚠️ `127.0.0.1` 在手机上指手机自己 |
| 生产 | `https://<域名>` | 必须 HTTPS + 配 `NETPULSE_INGEST_KEY` |

### 0.2 三端工程标识

| 端 | Bundle / 产物 | 部署目标 | 备注 |
|---|---|---|---|
| Watch | `com.example.WatchTennis` | watchOS 27.0，`TARGETED_DEVICE_FAMILY=4` | **独立 watchOS App**，当前不是 Phone 的伴生 target |
| Phone | `snoworlds.ATennis` | iOS 27.0，`TARGETED_DEVICE_FAMILY=1,2` | |
| Server | — | Python ≥ 3.9，FastAPI | 产物不随 App 打包 |

> 📌 接 [WATCH_CHANNEL.md](WATCH_CHANNEL.md) 后，Watch 必须改为 Phone 的**伴生 App**
> （Phone 工程内嵌 watchOS target，或两个 target 同属一个 App Group 与同一 App ID 前缀），
> 否则 `WCSession` 无法配对。**这是通道落地的前置条件。**

### 0.3 请求头

| 头 | 用于 | 必填 | 说明 |
|---|---|---|---|
| `Content-Type: application/json` | 所有 POST | ✅ | |
| `X-Ingest-Key` | 写口（上传） | 生产必填 | 与后端环境变量 `NETPULSE_INGEST_KEY` 同值；未配置时后端放行（启动日志会告警） |
| `X-Device-Id` | 写口 | 建议 | 设备标识，仅审计用（排查"哪台表上报的"） |
| `Authorization: Bearer <token>` | 读口（拉数据） | ✅ | 由 `POST /api/wechat/login` 签发 |
| `X-User-Id` | `/api/ingest/*` 专用 | 否 | 缺省 `u_demo`。**`/api/prod/*` 不用这个头** |

### 0.4 客户端静态配置（Phone 侧一次性设定）

```swift
SessionUploader.baseURL        = URL(string: "http://192.168.1.10:8787")!  // 真机用局域网 IP
SessionUploader.openid         = nil      // 生产由微信 OAuth 取得；nil → dev_openid
SessionUploader.studentId      = nil      // nil → 服务端按 openid 派生并自动开通
SessionUploader.studentName    = nil
SessionUploader.deviceId       = "iPhone-<设备名>"
SessionUploader.ingestKey      = nil      // 后端配了 NETPULSE_INGEST_KEY 时填同值
SessionUploader.includeStrokes = true     // 关掉只传场次聚合，流量极小

RawUploader.shared.compress            = true   // DEFLATE 压缩
RawUploader.shared.keepLocalAfterUpload = false // 上传成功即删本地留档（L0 才是耐久存储）
```

### 0.5 建议的运行参数

| 项 | 建议值 | 理由 |
|---|---|---|
| 会话上传超时 | 30 s | 一场几百拍的建议体量 |
| 原始包上传超时 | 120 s | 单包可达数十 MB |
| 失败重试 | 指数退避 1→2→4…上限 60 s | 上行幂等，重试零成本 |
| 触发时机 | App 进前台 / 有网 / 训练结束后即时 | — |
| 逐拍批大小 | 200（上限 500） | 服务端 `MAX_OPS_PER_PUSH = 500` |

---

## 0.6 契约是代码，不是文档（**2026-10-01 起**）

本文是**人读的说明**；机器读的真源是：

```
Tennisbackend/contract/TennisContract.swift        ← 唯一真源
ATennis/ATennis/Shared/TennisContract.swift        ← 同步副本（勿手改）
WatchTennis/WatchTennis/Shared/TennisContract.swift ← 同步副本（勿手改）
```

里面包含：上行 DTO（`MatchSession` / `MatchSwing`）、下行 DTO（`SessionListItem` /
`StrokeCounts` / `SessionListResponse`）、WCSession 消息类型、枚举
（`SwingType` 6+`unknown` / `SessionType` / `Wrist`）、接口路径与请求头常量、
以及生物力学口径常量。

### 改契约的四步

```bash
# 1. 改真源（并递增文件头的 version，同步记入本文 §8）
vim contract/TennisContract.swift

# 2. 同步到两个 App 工程
bash scripts/sync_contract.sh

# 3. 对账（38 项：枚举 / 上行字段 / 白名单 / 幂等 / 时间 / 下行字段）
.venv/bin/python scripts/verify_contract.py

# 4. 两端编译验证，然后三个仓库各自提交
```

`bash scripts/sync_contract.sh --check` 只比对 sha256，CI 或提交前用。

### 为什么这样设计

| 隐患 | 现在的挡法 |
|---|---|
| 字段名不一致 → 接口 200 但指标全 NULL | 类型编译不过；`verify_contract.py` 还会从 `ingest.py` 源码抠出后端实际读取的键做**双向**对账 |
| 契约加了字段、`SYNCABLE` 白名单没加 → 被静默丢弃 | 对账脚本把契约字段真跑一遍 `build_operations`，断言 payload 键全在白名单里 |
| 日期发成数字 → 后端 400 | `MatchSession` **手写 Codable**，强制 ISO8601，与调用方配的 encoder 无关 |
| 两端各定义一个同名类型 | 共享文件进两个 target，重名立刻编译冲突 |
| 同步副本悄悄漂移 | `--check` 比 sha256 |

> ⚠️ 契约文件**同时被 Watch 与 Phone 两个 target 编译**。新增共享类型前先确认两端都没有同名类型。

---

## 1. 上行 A：会话结论（**主通道**）

```
POST /api/prod/sessions
Content-Type: application/json
X-Ingest-Key: <可选>
X-Device-Id: iPhone-xxx
```

### 1.1 请求体

```json
{
  "openid": "openid_xxx",
  "student_id": null,
  "student_name": null,
  "include_strokes": true,
  "session": { /* 见 §3 MatchSession 契约 */ }
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `openid` | string | ✅ | 身份根。`student_id` 为空时据此自动开通学员档案 |
| `session` | object | ✅ | 见 §3 |
| `student_id` | string | 否 | 显式指定归属学员；缺省用 `stu-wx-<sha1(openid)[:12]>` |
| `student_name` | string | 否 | 首次开通时写入 |
| `include_strokes` | bool | 否 | 默认 `true` |

### 1.2 服务端做的三件事

1. **归档 L0** —— 原样封存进 `var/raw/`，`shape=match_session`，sha256 幂等
2. **结构化 L2** —— 翻译成 `training_sessions` + `stroke_records`（走同一个 `sync.push`，幂等 / changelog / LWW 全继承）
3. **开通学员** —— `openid` 首次出现时自动建档案（幂等）

### 1.3 响应（200）

```json
{
  "ok": true,
  "session_id": "nps-A1B2C3D4",
  "external_id": "A1B2C3D4",
  "strokes_ingested": 5,
  "analysis_db": { "applied": 6, "duplicate": 0 },
  "raw_layer": { "status": "created", "raw_id": "0a2a1910e41ac91c",
                 "revision": 1, "sha256": "…", "byte_size": 8036 },
  "student_id": "stu-wx-1d1c9aa0576f",
  "student_provisioned": true
}
```

| 字段 | 客户端怎么用 |
|---|---|
| `raw_layer.status` | `created` / `duplicate` —— **两者都算成功** |
| `analysis_db.duplicate > 0` | 幂等命中，**不要重试** |
| `student_provisioned` | 首次上传自动开通，可用来提示用户 |

### 1.4 错误码

| 码 | 原因 | 处理 |
|---|---|---|
| 400 | `openid` / `session` / `session.id` 缺失，或 `startedAt` 无法解析 | 修数据，重试无用 |
| 401 | `X-Ingest-Key` 缺失或不正确 | 检查配置 |
| 500 | 服务端异常 | 可重试，查服务端日志 |

---

## 2. 上行 B：原始波形（可选，见 [RAW_LAYER_DECISION.md](RAW_LAYER_DECISION.md)）

### 2.1 文件上传（推荐，包体大时用）

```
POST /api/raw/sessions        multipart/form-data
```

| 表单项 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 采集端会话 ID，**与 §1 的 `session.id` 相同** |
| `raw` | ✅ | `raw_*.json` 文件（可 gzip / DEFLATE 压缩） |
| `openid` | 否 | |
| `source` | 否 | 默认 `netpulse_watch` |
| `device_id` | 否 | |
| `shape` | 否 | `raw_package`（默认，含 `samples[]`）｜`match_session` |

### 2.2 JSON 内联上传（包体小、已在内存里时用）

```
POST /api/raw/sessions/json
{ "id": "<sessionId>", "session": { ...整包... }, "shape": "raw_package" }
```

### 2.3 幂等规则

- 同一 `(session_id, shape, sha256)` 重复上传 → `status=duplicate`，不产生新行
- 同一 `(session_id, shape)` **不同字节** → `revision + 1`，**历史版本永远保留**
- L0 **只追加，永不 UPDATE / DELETE**（由 SQLite 触发器在库层面强制）
- `raw_package` 与 `match_session` 是**两条独立版本线**，互不覆盖

### 2.4 压缩说明

Apple 的 `NSData.compressed(using: .zlib)` 产出的是 **RFC1951 裸 DEFLATE**，
既不是 gzip 也不带 `0x78` 头的 zlib 容器。后端按「解压结果是不是 JSON」判定，
三种格式都认（`.json` / `.json.zz` / `.json.gz`）。

---

## 3. `MatchSession` 传输契约（**最关键的一页**）

> 这是 Watch → Phone → Server 全链路的**唯一数据形态**。
> 字段名必须**逐字**对齐下表；名字不一致不会报错，只会**静默写出空值**。

```json
{
  "id": "A1B2C3D4-E5F6-7890-ABCD-1234567890EF",
  "startedAt": "2026-10-01T02:26:47Z",
  "endedAt": "2026-10-01T03:24:47Z",
  "duration": 3480,
  "wrist": "left",
  "avgHeartRate": 138,
  "maxHeartRate": 176,
  "activeCalories": 486,
  "distanceKm": 3.21,
  "title": "Apple Watch 训练",
  "sessionType": "rally",
  "location": null,
  "courtType": null,
  "swings": [
    {
      "type": "forehand",
      "impactTime": 12.5,
      "racketHeadSpeedKmh": 108.4,
      "confidence": 0.82
    }
  ]
}
```

### 3.1 会话级字段

| 字段 | 类型 | 必填 | 说明 | 缺失后果 |
|---|---|---|---|---|
| `id` | string (UUID) | ✅ | **跨端幂等键**，客户端生成 | 400 拒绝 |
| `startedAt` | ISO8601 string | ✅ | 见 [OVERVIEW.md](OVERVIEW.md) §5 | 400 拒绝 |
| `endedAt` | ISO8601 string | 否 | 缺失时回退为 `startedAt` | 时长只能靠 `duration` |
| `duration` | number（秒） | 否 | 缺失时用 `endedAt − startedAt` 复算 | — |
| `wrist` | `"left"` / `"right"` | 否 | 决定正反手符号 | 正反手可能整体互换 |
| `avgHeartRate` | number | 否 | **标量**，不是数组 | `avg_hr` 为空 |
| `maxHeartRate` | number | 否 | **标量**，不是数组 | `max_hr` 为空 |
| `activeCalories` | number (kcal) | 否 | 兼容别名 `calories` | `calories_kcal` 为空 |
| `distanceKm` | number (km) | 否 | | `distance_km` 为空 |
| `title` | string | 否 | | 默认「Apple Watch 训练」 |
| `sessionType` | enum | 否 | 缺失时后端按击球构成推断 | — |
| `location` / `courtType` | string | 否 | | — |
| `swings` | array | ✅（可空数组） | 见 §3.2 | `stroke_count` 为空 |

### 3.2 逐拍字段 `swings[]`

| 字段 | 类型 | 必填 | 说明 | 缺失后果 |
|---|---|---|---|---|
| `type` | enum（6 类） | ✅ | 见 [OVERVIEW.md](OVERVIEW.md) §4.1。`unknown` 不入明细但仍计入 `stroke_count` | 该拍被跳过 |
| `impactTime` | number（秒，**相对会话开始**） | 否 | ⚠️ **不是**绝对时间戳，**不是**字符串 | `impact_ms` 为空，`rally_max` 算不出 |
| `racketHeadSpeedKmh` | number (km/h) | 否 | 兼容别名 `speed_kmh` | 该拍 `speed_kmh` 为空，**全场球速指标全空** |
| `confidence` | number 0~1 | 否 | 分类置信度 | 质量门禁 G3 失效 |

> ⚠️ **`peakRotation` / `peakAccel` / `estimatedSpeed` 这类采集端内部字段不要上行。**
> 契约里没有它们的位置，传了也会被白名单过滤掉。
> 若确实需要保留，走 L0 原始层，不要塞进 `MatchSession`。

### 3.3 ✅ 与 Watch 代码的差异（**2026-10-01 已全部修复**）

上一版这一节列的是 6 处"接口 200 但指标全 NULL"的口径差异。现在 Watch 侧
**不再直接把采集模型编码上行**，而是经 `WatchSession.toMatchSession()`
产出契约类型 `MatchSession`：

| 原差异 | 现在怎么处理 |
|---|---|
| `estimatedSpeed` | → `MatchSwing.racketHeadSpeedKmh` |
| `timestamp`（绝对 `Date`） | → `MatchSwing.impactTime` = `timestamp.timeIntervalSince(startedAt)`，**相对秒数** |
| `heartRates[]` 采样数组 | → `avgHeartRate` / `maxHeartRate` 两个标量（`scalarAvgHeartRate` / `scalarMaxHeartRate`，**无采样时是 nil 不是 0**） |
| 无 `confidence` | → `SwingDetector` 输出启发式置信度并上行（接入 CoreML 后换成模型概率） |
| 无 `distanceKm` | 仍缺（未采集 HealthKit 距离），保持留空 |
| `SwingType` 仅 4 类 | → 枚举已扩到 **6 + `unknown`**，与后端 `STROKE_TYPES` 逐字一致 |

**两处仍存在的真实边界（不是缺陷）**：

1. **启发式分类器只产出 4 类**（发球/切削/正手/反手）。`volley` / `smash` / `unknown`
   需要 CoreML 模型才能产出 —— 枚举已就位，缺的是模型能力（M4-5）。
   因此「六类之和 < `stroke_count`」的差额目前恒为 0。
2. **`distanceKm` 未采集**（HealthKit 距离订阅未加）。留空符合「缺失 ≠ 0」，可接受。

> ⚠️ **不要再把 `SwingEvent` / `WatchSession` 直接编码上行。**
> 它们是采集端内部模型，`peakRotation` / `peakAccel` / `estimatedSpeed` / `timestamp`
> 在契约里没有位置，传了会被白名单静默过滤。上行只有 `MatchSession` 一个入口。

---

## 4. 下行：Phone 读取（`/api/prod/*`）

读接口统一要求 `Authorization: Bearer <token>`。
`?openid=` 只作一致性自检，与 token 不符返回 **403**。

| 接口 | 用途 |
|---|---|
| `POST /api/wechat/login` | `{ code }` → `{ openid, token, expires_at }`（token 有效期 30 天） |
| `POST /api/wechat/logout` | 主动失效当前 token |
| `GET /api/prod/me` | 当前身份 + 可选数据范围 + `session_count` |
| `POST /api/prod/profile` | 终端用户自助改昵称 / 持拍手 / 球拍等 |
| `GET /api/prod/sessions` | 训练历史（含会话级指标）。`range=all|7|14|30`、`limit ≤ 500` |
| `GET /api/prod/sessions/{sid}` | 单场详情：逐拍明细 + 击球构成。`sid` 可用**采集端 id** 或 L2 主键 |
| `GET /api/prod/analysis` | 纵向技术分析（KPI + 构成 + 趋势 + 洞察） |
| `GET /api/prod/leaderboard` | 排行榜（**已匿名化**） |

### 4.1 `GET /api/prod/sessions` 响应字段

```json
{
  "count": 1,
  "range": "all",
  "studentId": "stu-wx-1d1c9aa0576f",
  "sessions": [{
    "id": "nps-A1B2C3D4",
    "externalId": "A1B2C3D4",
    "title": "Apple Watch 训练",
    "sessionType": "rally",
    "location": null,
    "startedAt": "2026-09-28T09:00:00.000Z",
    "endedAt": "2026-09-28T09:30:00.000Z",
    "duration": 1800,
    "durationLabel": "30 分 0 秒",
    "wrist": "right",
    "counts": { "forehand": 2, "backhand": 1, "serve": 1,
                "slice": 1, "volley": 0, "smash": 0 },
    "total": 6,
    "avgHeartRate": 139, "maxHeartRate": 173,
    "calories": 514, "distanceKm": 2.31,
    "avgSpeedKmh": 96.4, "peakSpeedKmh": 121.5, "servePeakKmh": 118.0,
    "rallyMax": 4
  }]
}
```

> ⚠️ Phone 的 UI 模型与这个响应**不是一一对应**：UI 需要的是
> `ShotSegment` / `metrics[]` / `timeline.pulses` 这类**成品展示结构**。
> 中间必须有一层 **`Response → UI 模型` 的装配层**，不要让 View 直接读接口 JSON。
> 见 [ROADMAP.md](ROADMAP.md) M2-2。
>
> 📌 这个响应的类型已在契约里定义好：`SessionListResponse` / `SessionListItem` /
> `StrokeCounts`。Phone 侧**直接解码这几个类型**即可，不要自己再写一份 Codable 结构 ——
> 字段名不一致会静默变 null，这与上行是同一类坑。
> 注意 `startedAt` / `endedAt` 在响应里是**带毫秒**的字符串，用
> `startedAtDate` / `endedAtDate` 取 `Date`（`.iso8601` 解码策略不认毫秒）。

### 4.2 后端**没有**数据源的维度（已全部下线，不要再期待）

`spin_rpm`、`spin_type`、`sweet_spot`、`depth_m`、`landing_zone`、
`lateral_offset_m`、`net_clearance_m`、`anomaly`，
以及会话级的 `unforced_errors`（非受迫性失误）、`winners`（制胜分）。

原因：手腕单点 IMU 测不到。落点需要看到球的飞行轨迹，甜区需要拍面振动传感器；
`unforced_errors` / `winners` 是**对抗结果**，需要知道球有没有落在界内、这一分谁赢。

**2026-10-02 首轮收口，2026-10-03 补漏**：这些字段不再出现在任何上行白名单
（`sync.py` 的 `SYNCABLE`）、接口出口（`user_api.py` / `data_api.py`）、
契约类型（`TennisContract.swift`）以及 Phone / Watch 的任何展示项里。
DB 列**暂留但恒为 NULL**。
权威清单与逐项原因见 `server/datasources.py` 的 `REMOVED`（管理台 `/settings#removed`）。
不要再新增对这些量的 Mock 展示 —— 用随机数撑场面会被当成真实数据。

### 4.3 ⚠️ 单场详情接口的字段风格不一致（**已知，待清理**）

`GET /api/prod/sessions` 返回 camelCase 业务字段（如上表）；
但 `GET /api/prod/sessions/{sid}` 的 `session` 段是**直接 `dict(row)` dump 数据库行**：

- 字段名是 **snake_case**（`started_at` / `avg_hr` / `stroke_count` …）
- 还会带出 `user_id` / `deleted_at` / `version` / `sync_status` 等**内部列**

影响：同一套 API 两种风格；且把内部列暴露给了终端用户。

**处置**：这是需要后端清理的项，已登记为 [ROADMAP.md](ROADMAP.md) §7.1 D2。
在清理成 camelCase 显式字段之前，**不给它写契约类型** —— 不把有问题的形状固化下来。
`verify_contract.py` 的 G 段把现状固定成一条断言，清理时会提醒补契约。

---

## 5. 备用上行口：`/api/ingest/*`（App 不用）

供其它接入方与 curl 手工核对：

```
POST /api/ingest/watch-session
X-User-Id: u_demo        ← 注意：租户命名空间与 /api/prod 不同
X-Device-Id: xxx
{ "session": {...}, "student_id": "stu-...", "include_strokes": true, "force": false }
```

体与 `/api/prod/sessions` 的 `session` 段**完全一致**（同一个 `server/ingest.py`）。

- `verdict` 取值：`created` / `duplicate` / `overwritten` / `partial` / `rejected`
- `GET /api/ingest/sessions` —— 按会话粒度回看落库完整性，含 `balanced`（G2 配平门禁）结果

> ⚠️ **不要**同时往 `/api/prod/sessions` 与 `/api/ingest/watch-session` 发同一场球。
> 两者租户命名空间不同（`wx_<openid>` vs `X-User-Id`），会在两个租户里各落一份。

---

## 6. 幂等与冲突（客户端行为约定）

| 服务端返回 | 客户端该做什么 |
|---|---|
| `created` / `overwritten` / `partial` | 成功，出队 |
| `duplicate` | **成功**（幂等命中），出队。**不要当失败重试** |
| `conflict_lost` | 成功，出队；等下次 `pull` 拿服务端内容 |
| `rejected` | 出队 + **记录日志**。重试无用，要修数据 |
| HTTP 401 | 检查 `X-Ingest-Key` / token |
| HTTP 400 | 修数据 |

**断网恢复后可直接重发整包，无需先查询是否已上传。**

三道幂等闸（服务端）：

1. `operation_id` 去重（`sync_operations` 主键）
2. 部分唯一索引 `UNIQUE(source, external_id) WHERE external_id IS NOT NULL`
3. LWW：`client_updated_at > server.updated_at` 才覆盖（严格大于）

---

## 7. 契约变更流程（**必读**）

自 2026-10-01 起，**先改代码真源，再改文档**（顺序反了容易漏）：

```bash
# 1. 改真源，并递增文件头的 version
vim contract/TennisContract.swift

# 2. 若涉及枚举 / 术语 / 口径 → 同步改 OVERVIEW.md
#    若涉及服务端可写字段 → 同步改 server/sync.py 的 SYNCABLE
#    （白名单是静默的：漏了不报错，只丢字段）

# 3. 同步到两个 App
bash scripts/sync_contract.sh

# 4. 对账 + 三套回归
.venv/bin/python scripts/verify_contract.py     # 38 项：契约 ↔ 后端
.venv/bin/python scripts/test_sync.py           # 54 项：同步协议
.venv/bin/python scripts/verify_layers.py       # 73 项：L0–L3 + 鉴权

# 5. 两端编译验证后，三个仓库各自提交
# 6. 本文 §8 追加一行，并把「最后的契约版本」写在上表
```

> ⚠️ **服务端字段白名单是静默的**：客户端多传的字段会被直接忽略，不报错。
> 新增可写字段必须同时改 `SYNCABLE[...]['fields']`。
> 第 4 步的 `verify_contract.py` D 段会自动抓这条 —— 契约字段没进白名单就会 FAIL。

---

## 8. 契约变更记录

| 日期 | 契约版本 | 变更 | 影响端 |
|---|---|---|---|
| 2026-10-01 | — | 建立三端统一契约文档；确认通道为 Watch→WCSession→Phone→Server；记录 Watch 字段差异 6 项 | 三端 |
| 2026-10-01 | **v1.0.0** | **契约代码化**：`contract/TennisContract.swift` 成为唯一真源（上行 `MatchSession` / `MatchSwing`、下行 `SessionListItem` / `StrokeCounts` / `SessionListResponse`、`WatchToPhoneMessage` / `PhoneToWatchMessage`、`SwingType` 6+`unknown`、`SessionType`、`Wrist`、路径与请求头常量、生物力学常量）；`MatchSession` 手写 Codable 强制 ISO8601 | 三端 |
| 2026-10-01 | **v1.0.0** | **命名统一**：Watch `TrainingSession`→`WatchSession`（15 处）；Phone `TrainingSession`→`TrainingRecord`（12 处）；`SessionRow` 保持为 View 名 | Watch + Phone |
| 2026-10-01 | **v1.0.0** | **Watch 产出 DTO**：`WatchSession.toMatchSession()`，修掉 6 处字段口径；`SwingDetector` 补启发式 `confidence`；`SessionStore` 双写本地全量 + 上行契约包 | Watch |
| 2026-10-01 | **v1.0.0** | 新增 `scripts/sync_contract.sh`（同步 / `--check` 查漂移）与 `scripts/verify_contract.py`（38 项对账） | 三端 |
| 2026-10-01 | **v1.0.0** | 球速换算半径定为 `TennisContract.Biomechanics.racketRadiusMeters = 1.05`（拍臂等效半径：肩→拍头）；**已拍板**，否决 `0.685`（球拍长度 —— 会系统性偏低约 35%）。同步修正 `OVERVIEW.md` §3.2 / `INTEGRATION.md` §4.1 / `ingest.py` 注释。见 ROADMAP §7.1 D1 | 三端 |
