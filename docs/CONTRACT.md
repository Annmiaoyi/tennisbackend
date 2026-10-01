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

### 3.3 ⚠️ 当前 Watch 代码与本契约的差异（**必须修**）

| Watch 现有字段 | 契约要求 | 后果 |
|---|---|---|
| `estimatedSpeed` | `racketHeadSpeedKmh` | 球速全维度为空 |
| `timestamp`（Date → ISO 字符串） | `impactTime`（相对秒，number） | `impact_ms` 与 `rally_max` 丢失 |
| `heartRates[]`（采样数组） | `avgHeartRate` / `maxHeartRate`（标量） | `avg_hr`、`max_hr` 为空 |
| 无 | `confidence` | G3 门禁失效 |
| 无 | `distanceKm` | 记为缺失（可接受） |
| `SwingType` 仅 4 类 | 6 类 | 缺 `volley` / `smash` |

**这两处不会报错**：接口返回 200、库里也有行，但关键指标全是 NULL。
必须由 Watch 侧产出一个**独立的 `MatchSession` DTO**，而不是把采集模型直接编码上行。
见 [ROADMAP.md](ROADMAP.md) P0-1。

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
    "rallyMax": 4, "sweetSpotRate": null
  }]
}
```

> ⚠️ Phone 的 UI 模型与这个响应**不是一一对应**：UI 需要的是
> `ShotSegment` / `metrics[]` / `timeline.pulses` 这类**成品展示结构**。
> 中间必须有一层 **`Response → UI 模型` 的装配层**，不要让 View 直接读接口 JSON。
> 见 [ROADMAP.md](ROADMAP.md) P1-2。

### 4.2 后端**没有**数据源的维度（不要再期待）

`spin_rpm`、`spin_type`、`sweet_spot`、`depth_m`、`landing_zone`、
`lateral_offset_m`、`net_clearance_m`、`anomaly`。

原因：手腕单点 IMU 测不到。落点需要看到球的飞行轨迹，甜区需要拍面振动传感器。
Phone UI 里对应的展示项（甜区占比、旋转、落点分布）**要么标注为暂不实现，要么删掉**，
不要用随机数/Mock 撑场面后当成真实数据展示。

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

1. 改本文（`CONTRACT.md`），说明字段、类型、必填、兼容策略
2. 若涉及枚举或术语 → 同步改 [OVERVIEW.md](OVERVIEW.md)
3. **三端同时改**：Watch 侧 DTO + Phone 侧装配层 + Server 侧白名单
   （Server 白名单在 `server/sync.py` 的 `SYNCABLE`，新增字段不加白名单会被静默丢弃）
4. 跑回归：`python scripts/test_sync.py`（54 项）+ `python scripts/verify_layers.py`（73 项）
5. 在 [CHANGELOG 段落](#8-契约变更记录) 追加一行

> ⚠️ **服务端字段白名单是静默的**：客户端多传的字段会被直接忽略，不报错。
> 新增可写字段必须同时改 `SYNCABLE[...]['fields']`。

---

## 8. 契约变更记录

| 日期 | 变更 | 影响端 |
|---|---|---|
| 2026-10-01 | 建立三端统一契约文档；确认通道为 Watch→WCSession→Phone→Server；记录 Watch 字段差异 6 项 | 三端 |
