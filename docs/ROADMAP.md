# 三端统一开发规划

> 三端（Watch / Phone / Server）= 一个产品的三个部分。
> 本规划**以端到端闭环为单位**排期，而不是各端各自排 —— 因为断链的成本
> 远高于单端做不完的成本。
>
> 契约基线：[CONTRACT.md](CONTRACT.md) · 术语基线：[OVERVIEW.md](OVERVIEW.md)
> 通道基线：[WATCH_CHANNEL.md](WATCH_CHANNEL.md)

---

## 0. 现状盘点（2026-10-01 实测）

| 端 | 状态 | 结论 |
|---|---|---|
| **Server** | 🟢 基线完整 | 已从旧工程完整迁移：L0/L1/L2/L3 四层、43 条路由、鉴权、幂等、排行榜匿名化。回归 **54 + 73 项全通过 / 0 失败** |
| **Watch** | 🟡 采集可用、能产出契约包 | 传感器 + 挥拍检测 + HealthKit + 本地落盘**能跑**；**已产出 `MatchSession` DTO**（6 处字段口径已修）；仍零网络、零 WCSession —— 差的是通道，不是数据形态 |
| **Phone** | 🟡 纯 UI 原型（契约已就位） | 5486 行 SwiftUI，**29 处引用 MockData**，零网络 / 零持久化 / 零登录；但**模型命名已统一**，且已能编译共享契约层 |

**当前头号风险已从"字段对不上"变成"通道没建"**：契约已代码化并被两端引用
（`contract/TennisContract.swift`，38 项对账全过），字段口径不再是隐患；
真正的阻塞是 **Watch 与 Phone 尚未形成伴生关系，`WCSession` 无法配对**（M1-1）。

> 历史风险（已消除）：三端曾各自定义同名不同义的 `TrainingSession`，Watch 输出字段
> 与后端契约名不一致 6 处 → 会**静默写出空值**（接口 200、库里有行、指标全 NULL）。
> 见 §2 M0-5 ~ M0-8 的处理记录。

---

## 1. 里程碑总览

| 里程碑 | 目标 | 端到端可验证的结果 |
|---|---|---|
| **M0** | 契约冻结 | 三端文档统一（本文档体系），字段/枚举/时间/身份口径唯一 |
| **M1** | **最小闭环** | 手表打一场 → 手机看到 → 上传 → 后端 L2 有数据 → 手机读回并显示真实数字 |
| **M2** | 真实数据接入 | Phone 全部页面脱离 Mock，接 `/api/prod/*` + 登录 |
| **M3** | 可靠性与质量 | 离线队列、失败补传、质量门禁、排行榜 |
| **M4** | 模型化 | 原始波形 → 标注 → CoreML → 6 类分类 |

> **M1 是全局的关键路径。** 在 M1 打通之前，任何单端的"功能完善"都无法验证。

---

## 2. M0 —— 契约冻结（本次已完成）

| # | 任务 | 端 | 状态 |
|---|---|---|---|
| M0-1 | 迁移旧后端为基线 | Server | ✅ 完成，回归全过 |
| M0-2 | 三端统一文档体系（OVERVIEW / CONTRACT / WATCH_CHANNEL / RAW_LAYER_DECISION / ROADMAP） | 三端 | ✅ 完成 |
| M0-3 | 确定通道方案：Watch→WCSession→Phone→Server | 三端 | ✅ 已决策 |
| M0-4 | 记录 Watch 字段差异 6 项 | Watch | ✅ 已记录 |
| **M0-5** | **契约代码化**：`contract/TennisContract.swift` 成为三端唯一真源（上行 DTO + 下行 DTO + 通道消息 + 枚举 + 参数常量） | 三端 | ✅ 完成 |
| **M0-6** | 契约同步与对账脚本：`scripts/sync_contract.sh`（同步 / `--check` 查漂移）+ `scripts/verify_contract.py`（**38 项对账**） | 三端 | ✅ 完成，38/38 通过 |
| **M0-7** | 两端模型改名：Watch `TrainingSession`→`WatchSession`；Phone `TrainingSession`→`TrainingRecord`（共 27 处） | Phone + Watch | ✅ 完成 |
| **M0-8** | Watch 产出 `MatchSession` DTO，修掉全部 6 处字段口径差异；补 `confidence` | Watch | ✅ 完成 |

### 2.1 契约代码化解决了什么

之前"契约"只是文档里的一张表，靠人肉遵守。现在它是**能编译的类型**：

| 原来的隐患 | 现在怎么被挡住 |
|---|---|
| Watch 字段名与后端不一致 → 静默 NULL | 类型编译不过；`verify_contract.py` 还会从 `ingest.py` 源码里抠出后端实际读取的键做双向对账 |
| 契约加了字段、后端白名单没加 → 被静默丢弃 | D 段把契约字段真跑一遍 `build_operations`，断言产出的 payload 键**全部**在 `SYNCABLE` 里 |
| 两端各定义一个同名类型 | 共享文件进两个 target 时会直接编译冲突（`SwingType` 就是这么被发现的） |
| 日期发成数字 → 后端 400 | `MatchSession` 手写 Codable，**强制** ISO8601，与调用方配的 encoder 无关 |
| 同步副本悄悄漂移 | `sync_contract.sh --check` 比对 sha256 |

---

## 3. M1 —— 最小闭环（**关键路径**）

**DoD**：真机上打一场球，手机能显示这场球的真实数据（拍数 / 心率 / 卡路里），
后端 L2 能查到，且手机重启后数据还在（来自后端或本地缓存）。

### 3.1 前置改造（阻塞项）

| # | 任务 | 端 | 依赖 | 说明 |
|---|---|---|---|---|
| M1-1 | 把 watchOS target 并入 Phone 工程 / 建立伴生配对，两端开 **Watch Connectivity** capability | Phone + Watch | — | 不做这步 `WCSession` 无法配对。见 [WATCH_CHANNEL.md](WATCH_CHANNEL.md) §3。**当前唯一的头号阻塞** |
| M1-2 | 建 `Shared/TennisContract.swift`，**同时加入两个 target** | Phone + Watch | — | ✅ **已完成**（Watch 端工程文件已登记，Phone 端为文件夹自动同步） |
| M1-3 | 模型改名：Watch → `WatchSession`；Phone → `TrainingRecord` | Phone + Watch | — | ✅ **已完成**（27 处，两端类型检查 0 错误） |
| M1-4 | Watch 产出 `MatchSession` DTO（字段逐字对齐契约） | Watch | — | ✅ **已完成**（`WatchSession.toMatchSession()`，6 处口径全修） |
| M1-5 | Watch 补 `finishWorkout()`；补心率/卡路里的**标量**聚合（avg/max） | Watch | — | 标量聚合 ✅ 已完成（`scalarAvgHeartRate` / `scalarMaxHeartRate`）；`finishWorkout()` 仍待做 |
| M1-6 | 两端实现 `WCSessionDelegate` 封装（消息 + 文件） | Phone + Watch | M1-1 | 参照实现 5 个坑见 [WATCH_CHANNEL.md](WATCH_CHANNEL.md) §5。消息类型已在契约里定义好 |

### 3.2 数据流打通

| # | 任务 | 端 | 依赖 |
|---|---|---|---|
| M1-7 | Watch 结束时发 `sessionEnded(MatchSession)` | Watch | M1-4, M1-6 |
| M1-8 | Phone `onSessionEnded` → 本地落盘 → 入待同步队列 | Phone | M1-6 |
| M1-9 | Phone 实现 `SessionUploader` → `POST /api/prod/sessions` | Phone | M1-8 |
| M1-10 | Phone 用 `http://<局域网IP>:8787` 联调，验证响应 `raw_layer.status=created` | Phone + Server | M1-9 |
| M1-11 | Server 侧核对：`GET /api/ingest/sessions` 看 `balanced` 与 `unidentified` | Server | M1-10 |

### 3.3 M1 验收清单（逐项打勾才算过）

- [ ] 手表打一场 → 手机实时显示增加的拍数
- [ ] 结束后手机出现该场记录，拍数 / 时长 / 心率 / 卡路里**均非空**
- [ ] `POST /api/prod/sessions` 返回 `raw_layer.status = created`
- [ ] 后端 `training_sessions` 该行 `avg_hr`、`max_hr`、`peak_speed_kmh` **均非 NULL**
- [ ] `stroke_records` 该场 `speed_kmh`、`impact_ms` **均非 NULL**
- [ ] 同一场重传 → `duplicate`，逐拍行数不变
- [ ] `GET /api/ingest/sessions` 的 `balanced = true`
- [ ] 手机杀进程重启后，该场记录仍在

> ⚠️ 第 4、5 项是专门为"静默丢数据"设的验收点。
> **只要有一项为 NULL，说明契约没对齐**，接口不会报错，必须靠这两条抓出来。

---

## 4. M2 —— Phone 真实数据接入

**DoD**：Phone 的核心页面（首页 / 训练记录 / 分析）全部读真实数据，Mock 只在预览里保留。

| # | 任务 | 端 | 依赖 | 说明 |
|---|---|---|---|---|
| M2-1 | Phone 接入登录（`POST /api/wechat/login`）拿 token | Phone + Server | — | 开发期可用 `NETPULSE_DEV_OPENID` 对齐身份 |
| M2-2 | 建 **`Response → UI 模型` 装配层** | Phone | M2-1 | 后端返回业务字段，UI 要展示结构，中间必须有一层。**不要让 View 直接读 JSON** |
| M2-3 | 训练记录列表接 `GET /api/prod/sessions` | Phone | M2-2 | |
| M2-4 | 单场分析页接 `GET /api/prod/sessions/{sid}` | Phone | M2-2 | 逐拍明细 + 击球构成 |
| M2-5 | 趋势 / 分析页接 `GET /api/prod/analysis` | Phone | M2-2 | |
| M2-6 | **清点并处理"后端无数据源"的展示项** | Phone | M2-4 | 甜区 / 旋转 / 落点 / 逐拍速度离散序列 —— 二选一：标注"暂不实现"，或删掉。**不允许用随机数冒充真实数据** |
| M2-7 | 本地缓存读优先（UI 永远秒开），远端刷新在后台 | Phone | M2-3 | |
| M2-8 | 教学中心 / 课程 / 个人页：明确哪些是静态内容、哪些要接后端 | Phone | — | 课程类内容后端当前没有，需决定是否新增接口 |

---

## 5. M3 —— 可靠性与质量

| # | 任务 | 端 | 说明 |
|---|---|---|---|
| M3-1 | 待同步队列落盘 + 指数退避重试（1→2→4…60s） | Phone | 上行幂等，重试零成本 |
| M3-2 | 断网恢复后自动补传；启动时 `retryPending()` | Phone + Watch | |
| M3-3 | 原始包失败保留本地留档（`keepLocalAfterUpload` 语义） | Phone | 见 [WATCH_CHANNEL.md](WATCH_CHANNEL.md) §5 |
| M3-4 | 质量门禁可视化：后端 `balanced` 结果反馈到管理后台 | Server | G2 已实装，只差展示 |
| M3-5 | 排行榜接入（**确认匿名化**：只有名次 / 脱敏昵称 / `isMe`） | Phone + Server | |
| M3-6 | 服务端 `openid → student_id` 随登录身份下发（不再靠派生猜） | Server | 见 §7 遗留 |
| M3-7 | G5 突变门禁（基于近 10 次滚动中位数） | Server | 需要新写 |
| M3-8 | 生产部署：HTTPS + `NETPULSE_INGEST_KEY` + 修改默认管理员口令 | Server | **上线前必做** |

---

## 6. M4 —— 模型化（依赖 M0-4 的原始波形决策）

| # | 任务 | 端 | 依赖 |
|---|---|---|---|
| M4-1 | 按 [RAW_LAYER_DECISION.md](RAW_LAYER_DECISION.md) §7 拍板采集策略 | 决策 | — |
| M4-2 | Watch 换 `CMBatchedSensorManager`（800Hz/200Hz 对齐） | Watch | M4-1 |
| M4-3 | Watch 原始样本落盘 + 压缩 + `transferFile` | Watch | M4-2 |
| M4-4 | Phone `onRawFile` → 落盘 → `POST /api/raw/sessions` → 成功删留档 | Phone | M4-3 |
| M4-5 | 分类器产出 `volley` / `smash` / `unknown`（枚举与契约**已就位**，缺的是模型能力） | Watch | M4-7 |
| M4-6 | 受试者采样 + 标注工作台出 `dataset.csv`（38 维） | Server + Watch | M4-4 |
| M4-7 | 训练 CoreML 模型并**同时加入 iOS / watchOS target** | Phone + Watch | M4-6 |
| M4-8 | 模型置信度门控 0.60 + HMM 平滑；`confidence` 上行（**字段与启发式量化值已就位**） | Watch | M4-7 |
| M4-9 | 评估「800Hz vs 200Hz」精度代价，决定长期采集档位 | 决策 | M4-6 |

---

## 7. 跨端风险与阻塞项

| # | 风险 | 影响 | 处置 |
|---|---|---|---|
| R1 | ~~`TrainingSession` 同名不同义~~ | ~~改一处以为改了三处~~ | ✅ **已消除**（M0-7 改名：Watch→`WatchSession`，Phone→`TrainingRecord`） |
| R2 | 服务端字段白名单是**静默**的 | 新增字段不加白名单会被丢弃，无报错 | 改契约必须同步改 `server/sync.py` 的 `SYNCABLE`；现已由 `verify_contract.py` D 段自动对账 |
| R3 | 三端独立仓库，无 CI 联动 | 一端先改会导致线上静默失配 | 已有 `sync_contract.sh --check` + `verify_contract.py`；**建议后续接进 CI**（目前靠手动跑） |
| R4 | `openid ↔ student_id` 仍靠派生 | 多端身份对不上时数据"查不到" | M3-6 |
| R5 | Phone UI 有后端无数据源的展示项 | 用假数据撑场面会误导用户与决策 | M2-6 明确处理 |
| R6 | Watch 是独立 App，不是伴生 | `WCSession` 无法配对 | M1-1（**唯一的头号阻塞**） |
| R7 | 默认管理员口令 / 默认放开的写口 | 生产安全事故 | M3-8 |

---

## 7.1 待拍板的开放项（**不是 bug，是需要产品决策**）

| # | 项 | 现状 | 影响 | 建议 |
|---|---|---|---|---|
| D1 | **球速换算半径**：`0.685 m` vs `1.05 m` | 文档（[OVERVIEW.md](OVERVIEW.md) §3.2）与后端 `ingest.py` 注释写 **0.685（球拍长度）**；Watch 采集端实现用 **1.05（拍臂等效半径）** | 差 **35%**，会让**全部已展示的球速数字变化**（100 km/h → 65 km/h） | 统一改造里**没有擅自改**：已把值收敛进 `TennisContract.Biomechanics.racketRadiusMeters = 1.05`（= 现有实现值，保证线上数字不变）。拍板后改这一处 + 同步文档 |
| D2 | **单场详情接口字段风格不一致** | `GET /api/prod/sessions` 返回 camelCase 业务字段；`GET /api/prod/sessions/{sid}` 直接 `dict(row)` dump 数据库行（**snake_case，且含 `user_id` / `deleted_at` 等内部列**） | 同一套 API 两种风格；且把内部列暴露给终端用户 | 待清理成 camelCase 显式字段后再补契约类型；`verify_contract.py` G 段已把现状固定为一条断言，清理时会提醒 |
| D3 | **原始波形采集策略** | `WorkoutManager` 收了 `rawSamples`（上限 3 万条）但 `finalizeSession()` **没把它放进会话**，会话结束即丢弃 | 影响 M4 全部（CoreML 训练数据的唯一来源） | 见 [RAW_LAYER_DECISION.md](RAW_LAYER_DECISION.md)，M4-1 |

---

## 8. 建议的推进顺序（一句话）

```
M0-5 … M0-8                       ← ✅ 契约代码化 + 改名 + Watch 产出 DTO（本次已完成）
        ↓
M1-1                             ← 把 watchOS target 并入 Phone 工程（唯一的头号阻塞）
        ↓
M1-6 … M1-11 + M1 验收清单        ← 打通一场球的端到端闭环
        ↓
M2-1 / M2-2                      ← 登录 + 装配层（Phone 接真实的两个前提）
        ↓
M2-3 … M2-8 → M3 → M4
```

> **不要并行做 M2 的页面接入和 M1 的通道** —— Phone 的装配层要照着真实响应结构写，
> 而真实响应结构要等 M1 跑通一次才能确认。
>
> **契约改动一律走 `sync_contract.sh` + `verify_contract.py`**，不要手改 App 里的副本。
