# 采集端接入与端到端闭环

> 本文描述 **NetPulse 采集端（Apple Watch + iOS）** 与 **AceMate 管理后台 + 终端接口** 如何接成一条完整链路：
> 从手表传感器采集，到原始数据封存，到结构化入库，再到跨学员排行与纵向技术分析。
>
> 涉及代码 —— 📌 **2026-10-01 起三端已拆为独立仓库**，统一约定见
> [OVERVIEW.md](OVERVIEW.md)，对接契约见 [CONTRACT.md](CONTRACT.md)：
> - 采集端 Watch：`WatchTennis`（SwiftUI / watchOS，独立 Xcode 工程）
> - 展示端 Phone：`ATennis`（SwiftUI / iOS，独立 Xcode 工程）
> - 通信通道：Watch `WCSession` → Phone → HTTP → 后端，见 [WATCH_CHANNEL.md](WATCH_CHANNEL.md)
> - 服务端：`Tennisbackend`（本仓库，FastAPI + SQLite）
> - 标注工作台 UI：`server/templates/pages/annotation.html`
>
> ⚠️ **2026-09-29 变更**：数据侧引入 **L0 原始数据层**，
> 上行通道由「双通道」收敛为「两条路径、同一入口」（见 §1.2）。

---

## 1. 端到端数据流

### 1.1 四层数据分层（2026-09-29 起）

```
L0 原始数据层   var/raw/acemate_raw.db + var/raw/files/*.json[.gz|.zz]
                只追加、sha256 内容寻址、可重放。识别算法的每一版都拿它重跑。
L1 标注层       var/annotation/annotations.db
                逐拍真值标签（人工标注 / 视频同步），sessions.raw_id → L0.raw_id
L2 识别·分析层  var/acemate.db
                识别出来的结构化结果：training_sessions + stroke_records，可重算
L3 终端展示层   /api/prod/*
                只读 L2 + 复用 analytics.py，给微信端 / iOS 看历史·分析·排行榜
```

**为什么必须把原始层单独拆出来**：采集端上传的是高频整包
（加速度 800Hz + 设备运动 200Hz，一场 1 小时 ≈ 700 MB），
它有两个性质不能当普通业务表对待 ——
① 它是所有派生结果的唯一来源，解析逻辑改了必须能重跑；
② 它可能比当前算法更"聪明"，今天读不出的字段不代表以后读不出，
一旦允许覆盖，旧包就永久丢了。

所以 L0 的规则是：**只 INSERT，永不 UPDATE / DELETE**（由 SQLite 触发器在库层面强制）。

### 1.2 两条上行路径（原「双通道」已收敛）

采集端送两种**性质不同**的东西，走两个接口，都指向同一台后端（`:8787`）：

| 路径 | 载荷 | 接口 | 落哪层 | 代码 |
|---|---|---|---|---|
| 会话结论 | `MatchSession`（swings / 心率 / 卡路里 / 距离） | `POST /api/prod/sessions` | L0(`shape=match_session`) + L2 + 开通学员 | `ios/…/SessionUploader.swift` |
| 原始波形 | `{ session, samples[] }` 整包 | `POST /api/raw/sessions` | 仅 L0（`shape=raw_package`） | `ios/…/RawUploader.swift` |

两条路径用**同一个 `session.id`** 关联，因此在 L0 里能同时查到同一会话的
`raw_package` 与 `match_session` 两条**独立版本线**（互不覆盖）。

> **为什么不再走 `/api/ingest/watch-session`**：旧版是"双通道"——
> `/api/prod/sessions`（原标注后端）+ `/api/ingest/watch-session`（分析后端）。
> 但两者的**租户命名空间不同**：前者用 `wx_<openid>`，后者用请求头 `X-User-Id`
> （缺省 `u_demo`）。同一场训练同时发两条，会在两个租户里各落一份 ——
> 终端用户看到 1 场，管理后台的演示租户里却凭空多出一场真实数据。
> 而 `/api/prod/sessions` 现已**完整覆盖**旧分析通道的能力（结构化走的是同一个
> `server/ingest.py`，幂等/LWW/changelog 语义一致），还多做两件事（L0 归档 +
> 学员自动开通）。故 App 侧只保留这一条会话上行通道；
> `/api/ingest/*` 仍留在服务端，供其它接入方与 curl 手工核对使用。

### 1.3 全链路图

```
┌─────────────────────── 采集端（Apple Watch） ───────────────────────┐
│                                                                     │
│  HKWorkoutSession(.tennis)          CMBatchedSensorManager          │
│  ├ 心率 / 活动能量 / 距离            ├ 加速度 800Hz                  │
│  └ 时长                              └ 设备运动 200Hz（最近邻对齐）  │
│            │                                    │                   │
│            └────────────┬───────────────────────┘                   │
│                         ▼                                           │
│              SwingDetector（启发式状态机）                          │
│              起点: |rotationRate·gravity| > 6.0                     │
│              击球: 加速度幅值 > 22 m/s²                             │
│              校验: 累计沿重力旋转 ∈ [1.5, 25] rad，去抖 300ms       │
│                         ▼                                           │
│              38 维特征 → CoreMLSwingClassifier                      │
│                         ▼                                           │
│              置信度门控 0.6 → SwingHMMSmoother（Viterbi 平滑）      │
│                         ▼                                           │
│              MatchSession { 元数据 + swings[] }                     │
└──────────┬──────────────────────────────┬───────────────────────────┘
           │ WCSession 消息               │ WCSession 文件（raw_*.json）
           ▼                              ▼
   ┌──────────────────┐          ┌──────────────────────────────────┐
   │  iOS App         │          │  iOS App                         │
   │  MatchStore      │          │  PhoneConnectivity.onRawFile     │
   │  Core Data+      │          │   └─ RawUploader：同步落盘 →     │
   │  CloudKit（主存）│          │      DEFLATE 压缩 → 上传 → 删留档│
   └────────┬─────────┘          └───────────────┬──────────────────┘
            │ SessionUploader.upload()           │ POST /api/raw/sessions
            │ POST /api/prod/sessions            │
            ▼                                    ▼
┌────────────────────────────────────────────────────────────────────┐
│  AceMate 后端 :8787（单进程，按路径分流）                          │
│                                                                    │
│  /api/prod/sessions ─┬─► L0 归档（shape=match_session，sha256 幂等）│
│                      ├─► server/ingest.py → sync.push              │
│                      │   （幂等 + changelog + LWW）→ L2            │
│                      └─► 自动开通学员档案（openid → stu-wx-…）      │
│  /api/raw/sessions  ───► L0 归档（shape=raw_package，只落文件）    │
│  /api/ingest/*      ───► L2（其它接入方 / 人工核对用，App 不再调用）│
│  /api/prod/*（读）  ───► 只读 L2 + server/analytics.py → 终端展示   │
│  /annotation        ───► L1 工作台：波形 ↔ 视频逐拍标注             │
│                                                                    │
│  库：var/raw/（L0） · var/annotation/（L1） · var/acemate.db（L2） │
└────────────────────────────────────────────────────────────────────┘
```

**关键点**：两条路径不是二选一。
只发会话结论 → 数据进得了分析与排行，但**拿不到重训模型所需的原始波形**；
只发原始波形 → 有原始数据，但终端用户看不到任何分析。

---

## 2. 职责边界

| 能力 | 采集端（NetPulse） | 分析端（AceMate backend-web） |
|---|---|---|
| 高频传感器采集 | ✅ 800Hz / 200Hz | ❌ 不接触硬件 |
| 挥拍检测与分类 | ✅ 启发式 + CoreML + HMM | ❌ 只接收分类结果 |
| 原始样本落盘 | ✅ 采集时产出 `raw_<sessionId>.json` 并**自动上传**（`RawUploader`） | ✅ 原样封存进 **L0** `var/raw/`（只追加、sha256 幂等） |
| 本地持久化 | ✅ Core Data + CloudKit（会话结论） | — |
| 标注数据与工作台 | ✅ 产出待标注样本 | ✅ L1 `var/annotation/annotations.db` + `/annotation` 工作台 |
| 模型训练 | ✅ ml-pipeline（38 维 CSV → CoreML） | ❌ 只负责导出 CSV |
| 结构化入库 | ❌ 只发整包 | ✅ 翻译 + 拆分表（L2） |
| 一致性协议 | ❌ 单向重试 | ✅ 幂等 + changelog + LWW |
| 纵向技术分析 | ❌ | ✅ |
| 跨学员排行 | ❌ | ✅（终端用户侧**匿名化**） |
| 管理站看板 | ❌ | ✅（**需登录**） |

---

## 3. 接入接口契约

### `POST /api/ingest/watch-session`

把采集端的整包会话结构化入库。

**请求头**

| 头 | 必填 | 说明 |
|---|---|---|
| `Content-Type` | ✅ | `application/json` |
| `X-User-Id` | 否 | 账号标识，缺省 `u_demo` |
| `X-Device-Id` | 否 | 设备标识，仅审计用 |

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `session` | object | ✅ | 采集端 `MatchSession` |
| `session.id` | string | ✅ | 采集端会话 UUID，**跨端幂等键** |
| `session.startedAt` | ISO8601 | ✅ | 开始时间 |
| `session.endedAt` | ISO8601 | 否 | 结束时间 |
| `session.wrist` | `left`/`right` | 否 | 佩戴手腕 |
| `session.duration` | number | 否 | 时长（秒） |
| `session.avgHeartRate` | number | 否 | 平均心率 |
| `session.maxHeartRate` | number | 否 | 最高心率 |
| `session.activeCalories` | number | 否 | 活动能量（kcal） |
| `session.distanceKm` | number | 否 | 跑动距离（km） |
| `session.swings[]` | array | 否 | 逐拍数组 |
| `session.swings[].type` | enum | ✅ | `forehand`/`backhand`/`serve`/`slice`/`volley`/`smash`；`unknown` 不入明细 |
| `session.swings[].impactTime` | number | 否 | 击球时刻（相对会话开始，秒） |
| `session.swings[].racketHeadSpeedKmh` | number | 否 | 拍头速度（对外文案称"球速"，见 §4） |
| `session.swings[].confidence` | 0~1 | 否 | 分类置信度 |
| `student_id` | string | 否 | 归属学员，缺省回落该账号第一个学员 |
| `include_strokes` | bool | 否 | 是否上行逐拍明细，默认 `true` |
| `force` | bool | 否 | 强制覆盖已入库场次，默认 `false` |
| `source` | string | 否 | 来源标记，默认 `netpulse_watch` |

**响应**（200）

```json
{
  "verdict": "created",
  "session_id": "nps-A1B2C3D4-…",
  "external_id": "A1B2C3D4-…",
  "student_id": "stu-00001001",
  "total": 116, "applied": 116, "duplicate": 0,
  "conflict_lost": 0, "rejected": 0,
  "stroke_ops": 115,
  "cursor": 204,
  "received_at": "2026-09-28T15:12:03.401Z"
}
```

`verdict` 取值：

| 值 | 含义 | 客户端应如何处理 |
|---|---|---|
| `created` | 首次入库 | 视为成功 |
| `duplicate` | 之前已完整入库过 | **视为成功**（幂等），不要重试 |
| `overwritten` | 带 `force` 覆盖了已有场次 | 成功 |
| `partial` | 部分操作 applied、部分 duplicate | 成功，通常是上次中断后补传 |
| `rejected` | 全部被拒 | 检查 `results[].reason` |

**响应**（400）：`session` 缺失、`session.id` 为空、`startedAt` 无法解析、找不到归属学员。

### `GET /api/ingest/sessions`

按会话粒度核对采集端数据是否完整落库，含配平校验。

| 参数 | 默认 | 说明 |
|---|---|---|
| `source` | `netpulse_watch` | 来源标记 |
| `limit` | 50 | 1~500 |

每条返回含 `stroke_count`（检测总数）、`parts_total`（六类和）、`unidentified`（未识别数）、
`balanced`（G2 门禁结果）、`stroke_rows`（逐拍行数）。

---

## 4. 口径约定

### 4.1 「球速」实为拍头速度

采集端的 `racketHeadSpeedKmh` = `峰值角速度(rad/s) × 换算半径 r × 3.6`，其中 **`r = 1.05 m`**
（唯一真源 `TennisContract.Biomechanics.racketRadiusMeters`）。

> ⚠️ 早期本节与 `OVERVIEW.md` §3.2 曾写 `0.685 m`（球拍长度），那是**与实现不符的错误描述**
> —— 采集端从未用过 0.685。0.685 是「腕→拍头」距离，隐含旋转中心在手腕；而 ω 取自腕部
> 陀螺仪的**合成**角速度，主要成分是整条手臂带拍绕**肩**甩动，配「肩→拍头」半径（1.05 m）
> 才自洽，否则丢掉手臂与躯干贡献、系统性偏低约 35%。实测对照（JSSM 正手综述的拍头线速度：
> 俱乐部 76–86 km/h、职业 ≈119 km/h）证实只有 1.05 落进真实区间。详见 `OVERVIEW.md` §3.2。

**该物理量的真实含义是拍头线速度，不是球体飞行速度** —— 手腕单点 IMU 测不到球速。
球速需要看到球的飞行轨迹（摄像头/雷达）。

**产品决策**：对外文案统一沿用「球速」，不另立名称。本约定记录在此，供开发与数据人员知晓。
数据列名（`peak_speed_kmh` / `serve_peak_kmh`）同样保持不变。

### 4.2 缺失 ≠ 0

采集端未采到、权限未授予、旧机型不支持时，字段**必须留空**，绝不填 0。

填 0 的后果：
- 拉低平均值（后端 G4 门禁专门拦截）
- 污染配平统计

实现上，`MatchSession.activeCalories` / `distanceKm` 声明为 `Double?`（Optional），
后端 `ingest.py` 用 `{k: v for k, v in payload.items() if v is not None}` 过滤后再入库。

### 4.3 逐拍明细的完整度差异

| 数据来源 | 逐拍明细 | 说明 |
|---|---|---|
| 采集端接入 | **全量**（每拍一行） | 表端每拍都产生 SwingEvent |
| 历史/演示数据 | 抽样（每场 12 行） | `seed.py` 为控制体积抽稀 |

因此做「击球构成占比」时，**分母必须用同源的逐拍样本数**，
不能拿它去除会话表的 `stroke_count`（两者量级不同，会算出 0.8% 这种荒谬结果）。

---

## 5. 字段映射表

| 采集端（Swift） | 分析端（SQLite） | 表 | 处理方式 |
|---|---|---|---|
| `MatchSession.id` | `external_id` | training_sessions | 原值保留 |
| — | `id` | training_sessions | 加前缀 `nps-<external_id>` |
| `startedAt` | `started_at` | training_sessions | 归一化为 ISO8601 UTC |
| `endedAt` | `ended_at` | training_sessions | 同上 |
| `duration` | `duration_sec` | training_sessions | 取整；缺失时用 ended−started 复算 |
| `wrist` | `worn_wrist` | training_sessions | 原值 |
| — | `source` | training_sessions | 固定 `netpulse_watch` |
| `avgHeartRate` | `avg_hr` | training_sessions | 取整 |
| `maxHeartRate` | `max_hr` | training_sessions | 取整 |
| `activeCalories` | `calories_kcal` | training_sessions | 直接映射 |
| `distanceKm` | `distance_km` | training_sessions | 直接映射 |
| `swings.count` | `stroke_count` | training_sessions | **检测到的总拍数**（含 unknown） |
| `swings[type=forehand].count` | `forehand_count` | training_sessions | 按类型聚合 |
| `swings[type=backhand].count` | `backhand_count` | training_sessions | 同上 |
| `swings[type=serve].count` | `serve_count` | training_sessions | 同上 |
| `swings[type=slice].count` | `slice_count` | training_sessions | 同上 |
| `swings[type=volley].count` | `volley_count` | training_sessions | 同上 |
| `swings[type=smash].count` | `smash_count` | training_sessions | 同上 |
| `max(swings[].racketHeadSpeedKmh)` | `peak_speed_kmh` | training_sessions | 全场峰值 |
| `avg(swings[].racketHeadSpeedKmh)` | `avg_speed_kmh` | training_sessions | 全场均值 |
| `max(swings[type=serve].speed)` | `serve_peak_kmh` | training_sessions | 按类型取峰 |
| `avg(swings[type=serve].speed)` | `serve_avg_kmh` | training_sessions | 按类型取均 |
| `avg(swings[type=forehand].speed)` | `forehand_avg_kmh` | training_sessions | 同上 |
| `avg(swings[type=backhand].speed)` | `backhand_avg_kmh` | training_sessions | 同上 |
| 相邻击球间隔 < 2.5s 的最长连续段 | `rally_max` | training_sessions | 后端计算 |
| `swings[i].type` | `stroke_type` | stroke_records | 逐拍 |
| `swings[i].impactTime × 1000` | `impact_ms` | stroke_records | 毫秒偏移 |
| `swings[i].racketHeadSpeedKmh` | `speed_kmh` | stroke_records | 逐拍 |
| `swings[i].confidence` | `confidence` | stroke_records | 逐拍 |
| — | `seq_in_session` | stroke_records | 数组序号（1 起） |
| — | `id` | stroke_records | `npk-<external_id>-<序号4位>` |

**不映射的维度**（采集端不产出，保持 NULL）：
`spin_rpm`、`spin_type`、`sweet_spot`、`depth_m`、`landing_zone`、
`lateral_offset_m`、`net_clearance_m`、`anomaly`。

原因是手腕单点 IMU 测不到：落点需要看到球的飞行轨迹，甜区需要拍面振动传感器。

---

## 6. 幂等与冲突

### 6.1 三道闸

```
第 1 道：operation_id 去重（sync_operations 主键）
   ing-<external_id>-s          会话行
   ing-<external_id>-k0007      第 7 拍
   → 同一场重复上传 → 命中 → 返回 duplicate，不改库

第 2 道：部分唯一索引
   UNIQUE(source, external_id) WHERE external_id IS NOT NULL
   → 即使 operation_id 被绕过，同一来源同一会话也进不了第二行

第 3 道：LWW
   client_updated_at > server.updated_at 才覆盖
   → 版本戳取 endedAt，同一场积分不变
```

### 6.2 客户端行为约定

- **收到 `duplicate` 视为成功**，不要当成失败重试。
- 断网恢复后可直接重发整包，无需先查询是否已上传。
- 需要修正已入库场次（如补传卡路里）时，显式传 `force: true`。

---

## 7. 质量门禁如何生效

| 门禁 | 在接入链路中的落点 |
|---|---|
| **G1 值域** | 采集端负责（`SwingDetector` 的 min/max 阈值、`WorkoutManager` 的物理量单位） |
| **G2 配平** | `GET /api/ingest/sessions` 计算 `balanced` 字段 |
| **G3 置信度** | 采集端 `confidenceThreshold = 0.6` 门控；后端 `stroke_records.confidence` 落库 |
| **G4 缺失** | `ingest.py` 过滤 `None`，不写 0 |
| **G5 突变** | 待接入（需要滚动中位数基线，见遗留事项） |
| **G6 交叉** | 部分生效：`duration_sec` 优先用 HealthKit 值，缺失时用时间戳复算 |

### G2 配平的实际语义

```
六类之和 ≤ stroke_count（检测到的总拍数）
差额 = 未识别拍数（分类置信度不足，归 unknown）
```

- 差额 > 5% → 判 `suspect`，不进排行榜（分类器整场失灵）
- 分项和 **超过** 总数 → 同样 `suspect`（分类计数串味）
- 2~3% 的未识别属正常波动（对应 G3 门限 0.6），不报警

> 注：该门禁的阈值从最初设想的 2% 调整为 5%。实测一场 118 拍中含 3 拍 unknown（2.5%）
> 是正常水平，2% 阈值会把健康数据误判为 suspect。

---

## 8. 部署与配置

### 8.1 后端（单进程承载全部通道）

```bash
cd /Users/Project/Atennis/Tennisbackend
.venv/bin/python run.py --port 8787 --seed      # 启动（Windows: setup.cmd + start.cmd --seed）
                                                # --seed 会重建演示数据，谨慎用于生产
```

启动后同时提供（全部同一端口 `:8787`，按路径分流）：

| 通道 | 路径前缀 | 数据落点 |
|---|---|---|
| 管理站页面 + 业务接口 | `/`、`/api/platform|students|training|sync` | `var/acemate.db`（L2） |
| **L0 原始数据层** | `/api/raw/` | `var/raw/acemate_raw.db` + `var/raw/files/` |
| **L3 终端展示层** | `/api/prod/` | 只读 L2 |
| 采集端接入（其它接入方） | `/api/ingest/` | L2 |
| 标注工作台 + 微信登录 | `/annotation`、`/api/sessions/`、`/api/wechat/` | `var/annotation/annotations.db`（L1） |

**环境变量**（生产必配前两个）：

| 变量 | 作用 |
|---|---|
| `NETPULSE_INGEST_KEY` | 设备口口令。不配则**写**口完全放开（启动日志会告警），但 L0 的**读**口仍要求管理后台登录态 —— 见 `security.read_ok` |
| `ACEMATE_ADMIN_PASSWORD` | 首次启动创建 `admin` 账号时用的口令；不配则随机生成 16 字符并写入口令文件。**配了就不写文件**（调用方自己知道口令，写文件反而多一个泄露面） |
| `ACEMATE_ADMIN_PASSWORD_FILE` | 口令文件的落点覆盖。默认**跟随数据目录**：`<ACEMATE_DB 所在目录>/admin_initial_password.txt` |
| `NETPULSE_DEV_OPENID` | 开发期让 iOS 端与小程序端用同一个 openid（见 §10） |
| `NETPULSE_RAW_DIR` / `NETPULSE_RAW_DB` | 重定向 L0 目录 / 库文件（测试、多环境用） |
| `NETPULSE_ANNOTATION_DIR` / `NETPULSE_DB` | 重定向 L1 目录 / 库文件 |
| `ACEMATE_DB` | 重定向 L2 库文件 |

### 8.2 采集端（Phone 侧）配置

> 📌 按 [WATCH_CHANNEL.md](WATCH_CHANNEL.md) 的通道方案，上行由 **Phone（ATennis）** 承担，
> 而不是 Watch。下面这份配置因此落在 `ATennis` 工程里
> （建议 `ATennis/Core/SessionUploader.swift`）。

```swift
// 只有这一台后端了（原 :8000 独立标注后端已退役）：会话结论走 /api/prod/sessions，
// 原始波形走 /api/raw/sessions，靠路径区分。
SessionUploader.baseURL          = URL(string: "http://<局域网IP>:8787")!
SessionUploader.openid           = nil        // 生产由微信开放平台移动应用 OAuth 取得；nil → dev_openid
SessionUploader.studentId        = nil        // nil → 服务端按 openid 派生并自动开通
SessionUploader.studentName      = nil
SessionUploader.deviceId         = "iPhone-Snowo"
SessionUploader.ingestKey        = nil        // 服务端配了 NETPULSE_INGEST_KEY 时填同值
SessionUploader.includeStrokes   = true

// 原始包：默认压缩、上传成功后删本地留档（L0 才是耐久存储）
RawUploader.shared.compress            = true
RawUploader.shared.keepLocalAfterUpload = false
```

真机调试必须用电脑局域网 IP，`127.0.0.1` 在手机上指向手机自身。

Watch 端开关 `collectRawForTraining`（界面上的「采集原始数据」）默认**开** ——
它是 L0 的唯一数据来源；关掉只影响原始波形，会话结论与逐拍统计不受影响。

小程序伴生端同理：`utils/config.js` 的 `BASE_URL` 为 `http://127.0.0.1:8787`。

### 8.3 数据库迁移

`schema.sql` 用 `CREATE TABLE IF NOT EXISTS`，**不会给已存在的旧表补列**。

- **补列**：`server/db.py` 维护 `MIGRATIONS` 清单，`init_db()` 时按需
  `ALTER TABLE ADD COLUMN`，旧库可原地升级、不丢数据。
  标注侧同理，见 `server/annotation/db.py` 的 `_NEW_ANNOTATION_COLUMNS`；
  L0 侧见 `server/rawstore.py` 的 `_NEW_COLUMNS`。
- **改约束**（改列类型 / 去掉 NOT NULL / 改默认值）：**`ALTER TABLE` 做不到**，
  必须重建表。`server/rawstore.py` 的 `init_db()` 里有一段 `_rebuild_payload_nullable`：
  `raw_sessions.payload` 曾经是 `NOT NULL`，改为可空后老库不会自动跟随，
  结果是**每一条 gzip / 大包上传都 500**（`NOT NULL constraint failed`），
  而全新库完全看不出问题。

  > 教训：**任何"把某列放宽"的变更都要写重建迁移**，并且**必须在旧库上验证**。
  > 判断方法：用 `PRAGMA table_info(<表>)` 逐列比对"现网库"与"从零新建的库"，
  > 差在哪一列一目了然（本仓库曾用一次性脚本 `_schema_diff.py` 做过这件事）。


### 8.4 标注通道为何独立分库

标注库（`var/annotation/annotations.db`）刻意与分析库分开，理由：

1. **写频不同** —— 标注每拍一条记录、增量保存频繁，独立库不与其争分析库的写锁。
2. **协议不同** —— 标注数据不参与后台的 LWW / changelog / 墓碑同步协议，
   混进同一库会让同步水位语义变浑。
3. **备份粒度** —— 训练集可单独快照、单独清理，不影响业务数据。

两库各自持有独立连接（均 WAL + `busy_timeout`），互不阻塞。
环境变量 `NETPULSE_ANNOTATION_DIR` 可整体重定向标注数据目录（测试用）。

---

## 9. 本次修复的缺口

| # | 缺口 | 修复 |
|---|---|---|
| ① | 卡路里 / 距离从未上行，小程序端读到恒为 0（靠 mock 撑着） | `WorkoutManager` 订阅 `activeEnergyBurned` / `distanceWalkingRunning`；`MatchSession` 加字段；`SessionUploader` 上行 |
| ② | `WorkoutManager.end()` 缺 `finishWorkout()`，HKWorkout 未真正写入 HealthKit | 补上 `finishWorkout`，并优先取 builder 最终统计 |
| ③ | 击球类型三端不一致（Swift 5 类 / 小程序 8 类 / 后端 6 类） | 统一为 6 类：小程序去掉 `lob`/`drop`，Swift 加 `smash`（预留位） |
| ④ | 后端无鉴权，仅凭 URL 上的 `openid` 就能读他人数据 | 登录签发落库 token；读接口强制 `Authorization: Bearer`；`openid` 降级为一致性自检 |
| ⑤ | **`openid` 由 `hash(code)` 派生，跨进程不稳定** —— 服务一重启身份就变，历史数据全部"查不到" | 改用 `hashlib.sha1`，与进程无关 |

第 ⑤ 项不在原始清单里，是在验证第 ④ 项时实测发现的。实测同一 `code` 三次独立
启动分别得到 `dev_2120932761` / `dev_3474682280` / `dev_7637982259`。

---

## 10. 遗留事项

> 📌 本表是**链路视角**的遗留清单；**排期与责任人视图见 [ROADMAP.md](ROADMAP.md)**。

| 优先级 | 事项 | 说明 |
|---|---|---|
| 高 | 采集端 `MatchSession` → AceMate 的 `studentId` 映射 | 已改善：服务端按 `openid` 派生 `stu-wx-<sha1(openid)[:12]>` 并自动开通，不再需要手工配置；但仍未随登录身份下发 |
| ~~高~~ | ~~分析后端无鉴权~~ | ✅ 已解决（2026-09-29）：管理站接入 `adminauth` 会话登录（PBKDF2 20 万轮 + 服务端会话表可即时吊销），设置页已不再是"谁都能改" |
| 中 | G5 突变门禁未实装 | 需要"该学员近 10 次滚动中位数"基线，可基于 `analytics.py` 现算 |
| 中 | `spin_rpm` / 落点 / 甜区无数据来源 | 纯手腕方案测不到；若要支持需引入视觉或拍柄传感器 |
| ~~中~~ | ~~标注后端 `@app.on_event("startup")` 已废弃~~ | ✅ 已解决：并入本仓库后改由 `app.py` 的 `lifespan` 统一 `annotation.init()` |
| 中 | 开发期 openid 两端对不上 | 服务端 `dev_<sha1(code)[:16]>` vs iOS 恒定 `dev_openid`。已加 `NETPULSE_DEV_OPENID` 环境变量对齐，**仍待拍板**采用哪种方案 |
| 中 | `/api/prod/*` 响应结构变更未同步到客户端 | 历史从"整包 JSON 原文"改为 L2 结构化字段（见 API.md §2.15），小程序端需按新结构适配 |
| 中 | 管理站旧接口在守卫下未逐个复验 | `/api/students`、`/api/training/*` 等在登录态下功能是否完好，只覆盖了 `/api/platform/overview` 与 `/api/admin/me` |
| 低 | 演示数据的 `students` 汇总字段与记录不一致 | `seed.py` 人造值（如 `sessions_count=124` 而实际 8 场）。分析层已刻意不读这些字段 |
| 低 | 三套并行的分类体系 | `SHOT_TYPES`(6类) / `typeBreakdown`(6类细分，正手上旋等) / 标注工具(8类)。真实链路上只有第一套生效；三端统一口径见 [OVERVIEW.md](OVERVIEW.md) §4.1 |
| ~~低~~ | ~~采集端 Xcode 工程文件缺失~~ | ✅ 已解决（2026-10-01）：`ATennis` 与 `WatchTennis` 均为完整的独立 Xcode 工程，`.xcodeproj` 齐备 |

---

## 11. 验证记录

| 项目 | 结果 |
|---|---|
| 字段迁移 | 34 → 43 列，40 行数据完整保留，重复执行幂等 |
| 首次接入 | `verdict=created`，116 ops（1 会话 + 115 拍），全 applied |
| 重复接入 | `verdict=duplicate`，116 duplicate，0 applied，逐拍行数不变 |
| 强制覆盖 | `verdict=overwritten`，卡路里正确更新 |
| 配平校验 | 检测 118 / 六类和 115 / 未识别 3 → `balanced=true` |
| 进入分析链 | 该学员场次 8 → 9，出现在逐场记录、击球构成、技术分析 |
| 鉴权（13 项） | 无 token→401；越权读→0 条；冒充 openid→403；伪造 token→401；登出即失效→401 |
| 标注通道并入（2026-09-28） | 标注库 5 表结构+行数迁移前后完全一致；7 页全 200；标注侧 15 条接口全 200；`/openapi.json` 33 条路径；端到端 8 步全通过（上传→标注→波形→双标注者→一致性 κ=0.6735→导出 38 维 CSV）；`:8000` 已退役 |
| L0–L3 + 管理登录（2026-09-29） | 端到端 **79 项断言全通过 / 0 失败**，另在**配了口令**的独立实例上 **10 项全通过**。覆盖：未登录 302/401、开放重定向 `//evil.com` 被挡、L0 首次 created / 同字节 duplicate / 异字节 revision=2、触发器拦 UPDATE+DELETE、L1 `raw_id` 双库一致、波形改从 L0 取、L3 上传→历史→逐拍→构成→分析→排行、排行榜匿名化（`李**` 且无 `student_id`/`avatar_url`）、跨租户读 404 / 冒充 openid 403、幂等重传、两形态独立版本线、gzip 10× 压缩与透明解压、登出即失效 |
| 迁移缺口修复（2026-09-29） | 现网库 `raw_sessions.payload` 仍是旧的 `NOT NULL` → gzip/大包上传 500。补表重建迁移，启动日志实测「搬运 25 行历史版本」后 gzip 用例转绿。用「新库 vs 现网库」逐列比对确认这是唯一一处结构漂移 |
| 采集端端到端断点（2026-09-29） | ① `PhoneConnectivity.onRawFile` 声明后**从未被赋值** → 原始包静默丢失；② 文件回调绕主线程 → 大文件可能被系统清理；③ Swift `Date` 默认编码（距 2001 年秒数）与后端 ISO8601 口径不符 → **每一场上传都 400**；④ Watch 端 `collectRawForTraining` 默认 `false` 且**无 UI 可打开** → L0 永远拿不到数据。均已修，并在后端补了「数字时间戳 → 400 明确拒绝」的回归断言 |
