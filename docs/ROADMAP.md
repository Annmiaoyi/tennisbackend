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
| **Watch** | 🟡 采集可用、链路为零 | 传感器 + 挥拍检测 + HealthKit + 本地落盘**能跑**；但零网络、零 WCSession，字段与契约不一致 6 处 |
| **Phone** | 🔴 纯 UI 原型 | 5486 行 SwiftUI，**29 处引用 MockData**，零网络 / 零持久化 / 零登录 |

**当前最大风险**：三端各自定义了同名不同义的 `TrainingSession`，且 Watch 的输出字段
与后端契约名不一致 → 一旦接上会**静默写出空值**（接口 200、库里有行、指标全 NULL）。

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
| M0-4 | 记录 Watch 字段差异 6 项 | Watch | ✅ 已记录（待修） |

---

## 3. M1 —— 最小闭环（**关键路径**）

**DoD**：真机上打一场球，手机能显示这场球的真实数据（拍数 / 心率 / 卡路里），
后端 L2 能查到，且手机重启后数据还在（来自后端或本地缓存）。

### 3.1 前置改造（阻塞项）

| # | 任务 | 端 | 依赖 | 说明 |
|---|---|---|---|---|
| M1-1 | 把 watchOS target 并入 Phone 工程 / 建立伴生配对，两端开 **Watch Connectivity** capability | Phone + Watch | — | 不做这步 `WCSession` 无法配对。见 [WATCH_CHANNEL.md](WATCH_CHANNEL.md) §3 |
| M1-2 | 建 `Shared/Models.swift`，**同时加入两个 target** | Phone + Watch | M1-1 | 消灭"同名不同义" |
| M1-3 | 模型改名：Watch 采集模型 → `WatchSession`；Phone UI 行模型 → `SessionRow` | Phone + Watch | M1-2 | 见 [OVERVIEW.md](OVERVIEW.md) §3.1 |
| M1-4 | Watch 产出 `MatchSession` DTO（字段逐字对齐契约） | Watch | M1-3 | **本里程碑最核心的一步**，差异清单见 [CONTRACT.md](CONTRACT.md) §3.3 |
| M1-5 | Watch 补 `finishWorkout()`；补心率/卡路里的**标量**聚合（avg/max） | Watch | — | 现在 `HKWorkout` 没真正写入 HealthKit |
| M1-6 | 两端实现 `WCSessionDelegate` 封装（消息 + 文件） | Phone + Watch | M1-1 | 参照实现 5 个坑见 [WATCH_CHANNEL.md](WATCH_CHANNEL.md) §5 |

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
| M4-5 | 补 `SwingType` 补齐 6 类 + `unknown`（三端一致） | 三端 | — |
| M4-6 | 受试者采样 + 标注工作台出 `dataset.csv`（38 维） | Server + Watch | M4-4 |
| M4-7 | 训练 CoreML 模型并**同时加入 iOS / watchOS target** | Phone + Watch | M4-6 |
| M4-8 | 模型置信度门控 0.60 + HMM 平滑；`confidence` 上行 | Watch | M4-7 |
| M4-9 | 评估「800Hz vs 200Hz」精度代价，决定长期采集档位 | 决策 | M4-6 |

---

## 7. 跨端风险与阻塞项

| # | 风险 | 影响 | 处置 |
|---|---|---|---|
| R1 | `TrainingSession` 同名不同义 | 改一处以为改了三处，静默出错 | M1-3 改名 |
| R2 | 服务端字段白名单是**静默**的 | 新增字段不加白名单会被丢弃，无报错 | 改契约必须同步改 `server/sync.py` 的 `SYNCABLE` |
| R3 | 三端独立仓库，无 CI 联动 | 一端先改会导致线上静默失配 | 契约变更走 [CONTRACT.md](CONTRACT.md) §7 流程；建议后续加共享契约校验脚本 |
| R4 | `openid ↔ student_id` 仍靠派生 | 多端身份对不上时数据"查不到" | M3-6 |
| R5 | Phone UI 有后端无数据源的展示项 | 用假数据撑场面会误导用户与决策 | M2-6 明确处理 |
| R6 | Watch 是独立 App，不是伴生 | `WCSession` 无法配对 | M1-1（**M1 的头号阻塞**） |
| R7 | 默认管理员口令 / 默认放开的写口 | 生产安全事故 | M3-8 |

---

## 8. 建议的推进顺序（一句话）

```
M1-1 / M1-2 / M1-3 / M1-4        ← 先解决"能不能连上、字段对不对"
        ↓
M1-5 … M1-11 + M1 验收清单        ← 打通一场球的端到端闭环
        ↓
M2-1 / M2-2                      ← 登录 + 装配层（Phone 接真实的两个前提）
        ↓
M2-3 … M2-8 → M3 → M4
```

> **不要并行做 M2 的页面接入和 M1 的通道** —— Phone 的装配层要照着真实响应结构写，
> 而真实响应结构要等 M1 跑通一次才能确认。
