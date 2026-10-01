# 三端总览 · 统一约定

> **本文是整个项目「统一口径」的唯一入口。** 三端（iPhone / Apple Watch / 管理后端）的
> 职责边界、术语表、命名规范、枚举、时间口径、身份模型全部以本文为准。
> 任何一端要引入新概念、新字段、新枚举，先改本文，再改代码。

---

## 0. 三个工程 = 一个产品

本项目是**一个产品的三个部分**，不是三个独立项目。它们各自独立仓库、独立构建产物，
但共享**同一套数据契约**。

| 代号 | 工程 | 仓库 | 平台 | 构建产物 |
|---|---|---|---|---|
| **Watch** | WatchTennis | `https://github.com/Annmiaoyi/watchtennis.git` | watchOS 独立 App | `WatchTennis.app` |
| **Phone** | ATennis | `https://github.com/Annmiaoyi/atennis.git` | iOS | `ATennis.app` |
| **Server** | Tennisbackend | `https://github.com/Annmiaoyi/tennisbackend.git` | Python / FastAPI | 独立部署，**不随 App 打包** |

本地工作区：

```
/Users/Project/Atennis/
├── ATennis/          Phone  —— iOS 展示与分析
├── WatchTennis/      Watch  —— 传感器采集
└── Tennisbackend/    Server —— 数据权威源 + 管理后台
```

> ⚠️ 三端各自是**独立 git 仓库**。跨端契约的改动必须**三端同时改**，
> 否则会出现「接口返回 200、库里也有行、但关键指标全空」这类静默丢数据。
> 契约的唯一真源是 [CONTRACT.md](CONTRACT.md)。

---

## 1. 职责边界（谁能做什么，谁绝不能做什么）

| 能力 | Watch | Phone | Server |
|---|---|---|---|
| 高频传感器采集 | ✅ 唯一来源 | ❌ | ❌ 不接触硬件 |
| 挥拍检测与分类 | ✅ 启发式 / 未来 CoreML | ❌ 只消费结果 | ❌ 只接收分类结果 |
| HealthKit 心率 / 卡路里 / 距离 | ✅ | ❌ | ❌ |
| 原始波形落盘与上行 | ✅ 产出 | ✅ 中转 + 补传 | ✅ 封存进 L0 |
| 本地持久化 | ✅ 会话 JSON | ✅ 本地缓存 + 待同步队列 | — |
| **结构化入库** | ❌ 只发整包 | ❌ 只转发 | ✅ 翻译 + 拆表（L2） |
| **一致性协议（幂等 / LWW）** | ❌ 单向重试 | ✅ 队列重试 | ✅ 权威裁决 |
| 纵向分析 / 排行榜 / 管理看板 | ❌ | ✅ 只读展示 | ✅ 计算 |

**三条铁律：**

1. **数据权威源只有一个** —— Server 的 SQLite。Watch 与 Phone 上的都只是缓存。
2. **Watch 不直接连后端**（本轮决策）—— 采集数据一律经 `WCSession` 交给 Phone，由 Phone 上行。
3. **任何一端不得自行拼 SQL / 直连数据库** —— 只能走 HTTP 接口。

---

## 2. 数据流一张图

```
┌──────────── Watch（WatchTennis）────────────┐
│  HKWorkoutSession(.tennis)  +  CMMotionManager / CMBatchedSensorManager
│      ├─ 心率 / 活动能量 / 距离（HealthKit）
│      └─ 加速度 + 陀螺仪 → SwingDetector → SwingEvent[]
│                        │
│              WatchSession（采集模型，本地落盘）
└────────────────────────┼────────────────────┘
                         │  WCSession
                         │   · 消息：开始 / 逐拍更新 / 结束（实时)
                         │   · 文件：原始波形整包（走 transferFile）
                         ▼
┌──────────── Phone（ATennis）───────────────┐
│  PhoneConnectivity 接住消息与文件
│      ├─ 本地缓存（会话 + 待同步队列，可离线）
│      ├─ 上行：POST /api/prod/sessions   （会话结论）
│      └─ 上行：POST /api/raw/sessions    （原始波形，可选）
│  UI 只读本地缓存，永远秒开
└────────────────────────┼────────────────────┘
                         │  HTTPS / HTTP
                         ▼
┌──────────── Server（Tennisbackend）────────┐
│  L0 原始层  var/raw/         只追加 · sha256 幂等 · 可重放
│  L1 标注层  var/annotation/  逐拍真值标签（训练模型用）
│  L2 分析层  var/acemate.db   结构化结果（可重算）
│  L3 展示层  /api/prod/*      只读 L2，给 Phone 看
└────────────────────────────────────────────┘
```

通道设计的完整规格见 [WATCH_CHANNEL.md](WATCH_CHANNEL.md)。

---

## 3. 术语表（**最容易出错的地方**）

### 3.1 三个「会话」名字不能混用

现状里 `TrainingSession` 这个名字在 Watch 和 Phone 上**指的是完全不同的东西**。
这是当前最大的架构隐患，本文定义新命名，新代码一律按此执行：

| 术语 | 归属 | 含义 | 现有代码里的名字 |
|---|---|---|---|
| **`WatchSession`** | Watch 内部 | 采集域模型：一场训练 + `swings[]` + `heartRates[]` | 现名 `TrainingSession`（待改名） |
| **`MatchSession`** | 传输契约 | **上行 DTO**，字段名 = 后端入参字段名 | 参照实现 `Shared/Models.swift` |
| **`TrainingRecord`** | Phone UI | 列表里的一行展示数据（标题 / 日期 / 分数） | 现名 `TrainingSession`（待改名） |
| **`SessionRow`** | Phone UI | 上面那条记录的 **SwiftUI 行视图**（已是 View，**不改**） | 已存在，`Views/HomeView.swift` |
| **`training_sessions`** | Server | L2 表名，一场训练一行 | — |

> 📌 **落地要求**：Phone 侧的 UI 模型改名 `TrainingRecord`，
> Watch 侧的采集模型改名 `WatchSession`，
> 把 `TrainingSession` 这个词**只留给后端表概念**。
> 见 [ROADMAP.md](ROADMAP.md) 的 M1-3。
>
> ⚠️ **改名目标名已于 2026-10-01 修正**：上一版文档写的是 Phone 模型改名 `SessionRow`，
> 这是错的。`ATennis/Views/HomeView.swift:530` 已经有一个 `struct SessionRow: View`
> （首页与全部记录页共用的行视图），照那个方案改名会直接**编译冲突**。
> 现改为 `TrainingRecord`（与 `Models.swift` 里 `// MARK: - 训练记录` 的既有命名意图一致），
> `SessionRow` 保持为 View 名不动。

### 3.1.1 另有一组同名但**不需要改**的类型

`HomeView`、`ContentView` 在两个工程里都存在，但它们是 SwiftUI 的常规入口名，
且各自在独立模块内，**不构成隐患**——除非将来把某个文件同时加进两个 target。
真正要防的是「一个文件进两个 target」的场景，届时凡是重名类型都会立刻编译失败，
所以新增共享文件后要立刻跑一次双 target 编译。

### 3.2 其他统一术语

| 术语 | 定义 |
|---|---|
| **球速** | 对外文案统一叫「球速」，实际物理量是**拍头线速度**（`峰值角速度 × 0.685m × 3.6`）。手腕单点 IMU **测不到真实球速**，文案沿用但不另立名称。字段名保持 `speed_kmh` / `peak_speed_kmh`。 |
| **缺失 ≠ 0** | 采不到、没权限、旧机型不支持的字段**必须留空**，绝不填 0。填 0 会拉低均值、污染门禁，且与"真的消耗了 0 卡"无法区分。 |
| **未识别拍** | 分类置信度不足、归类 `unknown` 的拍。它**计入** `stroke_count`，但**不入** `stroke_records` 明细，也不计入六类分项。 |
| **配平** | `六类之和 ≤ stroke_count`，差额 = 未识别拍数。差额 > 5% 判 `suspect`，不进排行榜。 |

---

## 4. 枚举口径（**必须三端一致**）

### 4.1 `SwingType` —— 6 类，不是 4 类

```
forehand  正手
backhand  反手
serve     发球
slice     切削
volley    截击
smash     高压（预留位，现有启发式不产出）
```

| 端 | 现状 | 目标 |
|---|---|---|
| Watch | 只有 4 类（缺 `volley` / `smash`），且用 `case slice = "slice"` 显式 rawValue | 补齐 6 类 + `unknown` |
| Phone | UI 里只有 4 类中文名（正手/反手/切削/发球） | 展示层可只展示 4 类，但**解析层必须能接受 6 类** |
| Server | 6 类（`server/ingest.py` 的 `STROKE_TYPES`） | 不变，作为基准 |

> `smash` 是**预留位**：高压与发球在单拍窗口内都是「过顶下压」，仅靠手腕 IMU 难以区分，
> 强行输出只会制造假数据。等模型升级后自然有值。
> **不要为了「三端一致」把 `volley`/`smash` 从后端删掉** —— 删了以后模型升级要改三端。

### 4.2 佩戴手腕 `wrist`

`left` / `right`，**决定正手/反手的符号**。配错会导致正反手整体互换。
Watch 端必须从 `WKInterfaceDevice.current().wristLocation` 读取，不允许硬编码。

### 4.3 训练形式 `sessionType`

`serve`（发球训练）/ `rally`（对拉）/ `match`（实战对抗）/ `drill`（专项练习）。
采集端不产出时，由后端 `infer_session_type()` 按击球构成推断。

---

## 5. 时间口径（**错一处就是 400 或 31 年偏差**）

| 项 | 规格 |
|---|---|
| 传输格式 | ISO8601 字符串，**UTC，带 `Z` 后缀**。如 `2026-10-01T02:26:47Z` |
| 服务端存储 | 统一归一化为 **毫秒精度**：`2026-10-01T02:26:47.000Z` |
| 编码方式（Swift） | `JSONEncoder` **必须**设 `dateEncodingStrategy = .iso8601` |

```swift
// ✅ 必须这样
let e = JSONEncoder(); e.dateEncodingStrategy = .iso8601

// ❌ 绝不能这样 —— Swift 默认编码为「距 2001-01-01 的秒数」
let e = JSONEncoder()
```

**踩过的坑**：Swift 的 `Date` 默认编码是 Apple 参考纪元秒数（浮点），
后端只认 ISO8601 字符串，收到数字直接 400 ——「每一场上传都失败」。
更危险的是别让后端去猜数字是 Unix 还是 Apple 纪元：两者差 **978307200 秒 ≈ 31 年**。
客户端发 ISO 字符串是唯一没有歧义的做法。

**字符串字典序 = 时间序**：因为统一了 UTC + 毫秒 + `Z`，
LWW（最后写入优先）可以直接用字符串比较，无需解析时间。

**相对时间**：逐拍明细的 `impactTime` 是**相对会话开始的秒数（数字，不是字符串）**。
不要传绝对时间戳。见 [CONTRACT.md](CONTRACT.md) §4。

---

## 6. 身份模型（三个 id，别搞混）

```
openid  ──派生──▶  user_id  = "wx_" + openid          （L2 多租户过滤键）
openid  ──派生──▶  student_id = "stu-wx-" + sha1(openid)[:12]   （学员档案主键）
```

| id | 谁生成 | 用途 | 能不能当参数传 |
|---|---|---|---|
| `openid` | 微信开放平台 OAuth | 身份根 | 仅作**一致性自检**，与 token 不符则 403 |
| `token` | `POST /api/wechat/login` | 读接口鉴权 | ✅ `Authorization: Bearer <token>` |
| `user_id` | 服务端派生 | L2 表多租户隔离 | ❌ 不接受客户端指定 |
| `student_id` | 服务端派生 / 可显式指定 | 会话归属 | ✅ 可传，但必须是本租户下的 |
| `X-Ingest-Key` | 运维配置 | **写**口设备口令 | ✅ 请求头 |

> ⚠️ `openid` 曾经由 `hash(code)` 派生，而 Python 的 str hash 带随机盐 ——
> 服务一重启用户身份就变，历史数据全部"查不到"。**已修为 `hashlib.sha1`，与进程无关。**
> 别改回去。

---

## 7. 目录与命名规范

| 项 | 规范 |
|---|---|
| 实体 id 前缀 | Server 侧：会话 `nps-<externalId>`、逐拍 `npk-<externalId>-<序号4位>`、学员 `stu-wx-…`、演示学员 `stu-…` |
| 幂等键 | `id`（UUID）**由客户端生成**，是跨端幂等键。服务端据此派生 `operation_id` |
| Swift 文件名 | 一律 UpperCamelCase，一个文件一个主类型 |
| JSON 字段名 | **camelCase**（Swift 侧天然如此），服务端入参用 camelCase，出参混用 camelCase/snake 见 API.md |
| 数据库列名 | **snake_case** |
| 环境变量 | `SCREAMING_SNAKE`，服务端前缀见 [INTEGRATION.md](INTEGRATION.md) §8 |

---

## 8. 文档地图

| 文档 | 内容 | 什么时候看 |
|---|---|---|
| **OVERVIEW.md**（本文） | 三端统一约定 · 术语 · 枚举 · 时间 · 身份 | **动手前先看这个** |
| [CONTRACT.md](CONTRACT.md) | 接口契约 · 字段映射 · 对接参数表 | 写对接代码 |
| [WATCH_CHANNEL.md](WATCH_CHANNEL.md) | Watch→Phone→Server 通道设计 | 改通信层 |
| [RAW_LAYER_DECISION.md](RAW_LAYER_DECISION.md) | 原始波形采不采的论证 | 决策前 |
| [ROADMAP.md](ROADMAP.md) | 三端统一开发规划与优先级 | 排期 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 后端内部架构与踩坑记录 | 改后端 |
| [SYNC_PROTOCOL.md](SYNC_PROTOCOL.md) | 离线优先同步协议完整规格 | 写 Phone 同步层 |
| [DATA_MODEL.md](DATA_MODEL.md) | 后端 9 张表结构与同步列语义 | 改表 |
| [API.md](API.md) | 全部 HTTP 接口参考 | 查接口 |
| [INTEGRATION.md](INTEGRATION.md) | 采集端接入与端到端闭环 | 查采集链路细节 |
| [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) | 后端 UI 令牌 | 改后端页面 |
| [TESTING.md](TESTING.md) | 验收与回归 | 提交前 |
| [DEPLOY.md](DEPLOY.md) | 部署与运维 | 上线 |
