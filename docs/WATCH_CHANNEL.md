# 通道设计 · Watch → WCSession → Phone → Server

> **本轮已决策**：采集数据一律经 `WCSession` 交给 iPhone，由 iPhone 上行后端。
> Watch **不直连**后端。iPhone 同时承担本地缓存与待同步队列。
>
> 属于「对接规则」的组成部分；字段契约见 [CONTRACT.md](CONTRACT.md)，
> 术语与命名见 [OVERVIEW.md](OVERVIEW.md)。

---

## 0. 决策记录

| 项 | 决策 | 理由 |
|---|---|---|
| 上行路径 | Watch →(`WCSession`)→ Phone →(HTTP)→ Server | 与已跑通的参照实现一致；Phone 顺带做本地缓存与离线队列 |
| Watch 直连后端 | ❌ 不采用 | 需要在表上维护 token / baseURL / 弱网重试，且 Phone 拿不到实时数据 |
| 原始波形 | 走 `WCSession.transferFile`，不经消息通道 | 单包可达数十 MB，消息通道不适合 |
| Phone 持久化 | 本地库 = 缓存 + 待同步队列；Server = 权威源 | 见 [SYNC_PROTOCOL.md](SYNC_PROTOCOL.md) |

---

## 1. 拓扑

```
┌──────────────── Watch ────────────────┐
│  WorkoutManager                        │
│    ├─ HKWorkoutSession(.tennis)        │
│    └─ SensorCollector → SwingDetector  │
│              │                         │
│     WatchSession（采集模型）           │
│              │                         │
│     WatchConnectivity                  │
│       ├─ 消息：开始 / 逐拍 / 结束      │
│       └─ 文件：raw_*.json              │
└──────────────┬─────────────────────────┘
               │ WCSession（同一 App ID 前缀 + 配对设备）
               ▼
┌──────────────── Phone ─────────────────┐
│  PhoneConnectivity（WCSessionDelegate）│
│    ├─ onMessage → 实时 UI              │
│    ├─ onSessionEnded → 本地缓存        │
│    └─ onRawFile → 同步落盘 → 上传      │
│              │                         │
│     LocalStore（缓存 + 待同步队列）    │
│              │                         │
│     SessionUploader / RawUploader      │
└──────────────┬─────────────────────────┘
               │ HTTP
               ▼
            Server（L0 / L1 / L2 / L3）
```

---

## 2. WCSession 通道规格

### 2.1 三类传输方式怎么选

| 方式 | 适用 | 保证 | 本项目用途 |
|---|---|---|---|
| `sendMessage` | 对端**可达**时的即时小消息 | 无（失败即丢） | 实时逐拍更新、手表设置下发 |
| `transferUserInfo` | 不可达时的**排队**小消息 | 有（后台队列，保证送达） | `sendMessage` 失败后的兜底 |
| `transferFile` | 大文件 | 有（后台队列） | **原始波形整包** |
| `updateApplicationContext` | 只保留**最新一份**状态 | 覆盖式 | 手表电量 / 当前训练状态（可选） |

**规则**：`sendMessage` 失败必须回落到 `transferUserInfo`，否则手表离线时消息静默丢失。

### 2.2 消息协议（沿用参照实现，已跑通）

载荷统一为 **JSON 编码后的 `Data`**，放在字典的 `"message"` 键下 ——
不要直接发字典，字段类型会因平台差异出错。

**Watch → Phone：**

```swift
enum WatchToPhoneMessage: Codable {
    case sessionStarted(SessionStartedPayload)  // { sessionId, startedAt, wrist }
    case swingUpdate(SwingUpdatePayload)        // { sessionId, totalSwings, lastType, lastConfidence, heartRate }
    case sessionEnded(SessionEndedPayload)      // { session: MatchSession }   ← 完整会话
}
```

**Phone → Watch：**

```swift
enum PhoneToWatchMessage: Codable {
    case setWrist(SetWristPayload)                    // { wrist }  左右手设置
    case requestTrainingData(RequestTrainingDataPayload) // { sessionId } 补拉原始包
}
```

> 只有 `sessionEnded` 携带完整 `MatchSession`。实时阶段只发**计数与心率**，
> 不要每拍发一次全量数组 —— 手腕单点 IMU 的高频数据不适合走消息通道。

### 2.3 原始波形文件

```
Watch:  编码 → DEFLATE 压缩 → transferFile(url, metadata: ["sessionId": ...])
Phone:  WCSessionDelegate.session(_:didReceive:) → onRawFile(fileURL, metadata)
        └─ 在回调内**同步**拷走 → 上传 /api/raw/sessions → 成功后删本地留档
```

**压缩格式注意**：Apple 的 `NSData.compressed(using: .zlib)` 产出的是
**RFC1951 裸 DEFLATE**，不是 gzip、也不带 `0x78` 头。后端按「解压结果是不是 JSON」判定，
`.json` / `.json.zz` / `.json.gz` 三种都认。

---

## 3. 前置条件（**当前不满足，必须补**）

| # | 条件 | 现状 | 要做什么 |
|---|---|---|---|
| 1 | Watch App 必须是 Phone App 的**伴生 App** | ❌ WatchTennis 是**独立** watchOS App（`TARGETED_DEVICE_FAMILY=4`、无 `WKCompanionAppBundleIdentifier`），ATennis 未内嵌 watch target | 把 watchOS target 并入 Phone 工程，或确保两 target 同属一个 App ID 前缀并配对。**当前唯一的头号阻塞（M1-1）** |
| 2 | 两端 `WCSession.isSupported()` 且 `activate()` | ❌ 两端均无 `WatchConnectivity` 代码 | 各加一个 `WCSessionDelegate` 封装。消息类型已在契约里定义好（`WatchToPhoneMessage` / `PhoneToWatchMessage`），直接编解码即可 |
| 3 | 两端 **Capabilities** | Watch 有 HealthKit；Phone 无 | Phone 开 **Watch Connectivity**（后台数据接收）；两端同开 HealthKit |
| 4 | 双端共享模型文件 | ✅ **已建** `Shared/TennisContract.swift`，同时加入两个 target（Watch 端已登记进 `project.pbxproj`；Phone 端为文件夹自动同步） | 已解决。见 [OVERVIEW.md](OVERVIEW.md) §3.1.1 · [CONTRACT.md](CONTRACT.md) §0.6 |
| 5 | Watch 侧独立 DTO | ✅ **已产出** `WatchSession.toMatchSession()`，6 处字段口径全修；`SwingDetector` 补 `confidence` | 已解决。见 [CONTRACT.md](CONTRACT.md) §3.3 |

---

## 4. Phone 侧：本地缓存与待同步队列

### 4.1 写入顺序（**不能反**）

```
1. 本地生成 id（create 时）
2. 写本地库        ← UI 立刻可见
3. 入待同步队列
4. 有网就尝试 flush（失败不报错，留在队列）
```

> ⚠️ 先推后写会出现「推送成功但本地没落库」的窗口期，UI 会跳变。

### 4.2 队列持久化

队列必须**落盘**（进程被杀不丢）：`operation_id` / `entity_type` / `entity_id` /
`action` / `payload` / `client_updated_at` / `retry_count`。

### 4.3 重试策略

指数退避 `1s → 2s → 4s → … → 上限 60s`。因为上行幂等，**重试零成本**。

触发时机：App 进前台、有网（从断到通）、训练结束后即时。

### 4.4 完整协议

本地记录 7 个通用同步列（`id` / `user_id` / `created_at` / `updated_at` /
`deleted_at` / `version` / `sync_status`）、push / pull / snapshot 的完整规格，
见 [SYNC_PROTOCOL.md](SYNC_PROTOCOL.md)。

---

## 5. 参照实现踩过的 5 个坑（**改通信层前先读**）

| # | 坑 | 后果 | 正确做法 |
|---|---|---|---|
| 1 | `onRawFile` 回调**声明了但从没赋值** | 表打完了、数据也采了，**文件拿不出来** | 在 App 启动的**唯一组装点**接线 |
| 2 | 文件回调里绕主线程 | 打球时主线程忙于渲染，几百 MB 临时文件**可能已被系统清理** | 在 WCSession 后台队列回调内**同步落盘**，只是之后的上传才异步 |
| 3 | `Date` 用默认编码策略 | 后端只认 ISO8601，收到数字 → **每一场上传都 400** | `JSONEncoder` 设 `.dateEncodingStrategy = .iso8601` |
| 4 | `SensorSample.id` 进了 Codable | 36 字符 UUID × 800Hz ≈ **110 MB/小时纯噪声**（约占整包 15%） | `id` 排除出 `CodingKeys`，解码时现场生成 |
| 5 | 发大包走 `sendMessage` | 消息通道不适合大载荷 | 原始波形一律 `transferFile` |

> 坑 3 还有一个**更危险的变体**：别让服务端去猜一个数字是 Unix 纪元还是 Apple 参考纪元
> —— 两者差 **978307200 秒 ≈ 31 年**。客户端发 ISO 字符串是唯一没有歧义的做法。

---

## 6. 采集能力前置条件（Watch 侧）

| 能力 | 现状 | 目标 |
|---|---|---|
| 加速度 / 设备运动 | `CMMotionManager.deviceMotion` **100Hz** | 若采原始波形 → `CMBatchedSensorManager` **800Hz 加速度 + 200Hz 设备运动**（watchOS 10+，Series 8 / Ultra） |
| 心率 / 活动能量 | ✅ HealthKit 已订阅 | 补 **距离** `distanceWalkingRunning` |
| HKWorkout 落库 | ⚠️ `workoutSession?.end()` 后**没有 `finishWorkout()`** | 补 `finishWorkout`，并优先取 builder 最终统计 |
| 原始样本 | 收进内存数组（上限 3 万条）后**被丢弃** | 落盘 + 压缩 + `transferFile` |

> 原始波形的取舍见 [RAW_LAYER_DECISION.md](RAW_LAYER_DECISION.md)。
> 不采原始波形的话，`CMBatchedSensorManager` 与 `finishWorkout` 这两项可以不急。

---

## 7. 落地清单（按顺序）

- [ ] P0：Phone 工程内嵌 / 配对 watchOS target，两端开 Watch Connectivity capability ← **唯一的头号阻塞**
- [x] P0：建 `Shared/TennisContract.swift`，同时加入两个 target ← 2026-10-01 完成
- [x] P0：Watch 侧产出 `MatchSession` DTO（字段对齐 [CONTRACT.md](CONTRACT.md) §3）← 2026-10-01 完成
- [ ] P0：两端实现 `WCSessionDelegate` 封装（消息 + 文件）← 消息类型已在契约里，只剩接线
- [ ] P0：Phone 侧 `onSessionEnded` → 本地缓存 + 入队 → `POST /api/prod/sessions`
- [ ] P1：Phone 侧本地库 + 待同步队列 + 指数退避重试
- [ ] P1：补 `finishWorkout()`
- [ ] P2：原始波形（`CMBatchedSensorManager` + `transferFile` + `/api/raw/sessions`）

> **Watch 侧已经"只需要接线"**：`WorkoutManager.finishedMatchSession` 就是将来
> `sessionEnded` 消息要带的载荷（类型是契约的 `MatchSession`），
> 且已落盘在 `Documents/sessions/match_<id>.json` —— 真机上可直接核对内容。
