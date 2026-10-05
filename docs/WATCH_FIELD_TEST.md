# Apple Watch 真机实测方案

> **先说结论**：目前**不能**"拿手表打完 → 上传 → 管理后台看到"。  
> 采集端（Watch / Phone）**零采集实现、零网络实现、零落盘**，全部是 `MockData`。  
> 所以本方案分 4 个阶段，**每阶段都能独立验收**，不必等全部做完。
>
> 配套排查结论见本文 §1；一键上传工具见 §2.3 的 `scripts/upload_watch_session.py`。

---

## 1. 排查结论：逐拍历史数据到底哪里有问题

### 1.1 三个"逐拍"其实是三套数据，谁都不等于谁

| 位置         | 表 / 文件                                          | 在场内容          | 每场条数     | 真实吗                     |
| ---------- | ----------------------------------------------- | ------------- | -------- | ----------------------- |
| **L0 原始层** | `var/raw/acemate_raw.db` → `session.swings`     | `hist-*` 16 场 | 106 之类   | 演示，但**结构正确**（时刻单调、覆盖整场） |
| **L1 标注层** | `var/annotation/annotations.db` → `annotations` | 16 场预标注       | 与 L0 对齐  | 演示                      |
| **L2 分析层** | `var/acemate.db` → `stroke_records`             | 56 场          | **恒 12** | ❌ **纯随机**               |

`/strokes` 页面读的是 **L0 + L1**（`stats.stroke_detail` → `converter.load_raw_payload`），  
**不读 `stroke_records`**。所以页面上那 106 条与库里的 12 条是两码事 ——  
台账那 5 个逐拍列显示"106 条"、悬停提示里写"分析库另存 12 条抽稀样本"，  
就是在标注这个落差。

### 1.2 `stroke_records` 的 672 行是演示随机值（实测数据）

`server/seed.py` 的 `build_strokes(per_session=12)` 写死 12 条，且  
`impact_ms = int(1000 * (k+1) * rnd.uniform(8, 20))` —— **每条独立随机**：

| 断言                       | 实测                         | 应为      |
| ------------------------ | -------------------------- | ------- |
| `seq_in_session` 连续 1..n | 56/56 场 ✅                  | ✅       |
| `impact_ms` 随 seq 单调递增   | **仅 1/56 场** ❌             | 56/56   |
| `impact_ms` 覆盖会话时长比例     | **2.3% ~ 8.5%（中位 4.4%）** ❌ | 接近 100% |
| 每场逐拍行数                   | **恒 12**（会话真实击球 66~411） ❌  | = 会话击球数 |

覆盖率 4% 的含义：一场 70 分钟的训练，12 条"逐拍"全挤在**前 3 分钟**。  
原因是随机上限固定约 240 秒，与会话时长无关。

> 这不是本次引入的——`seed.py` 的 docstring 自称"抽稀以便体积可控"。  
> 但它同时把时刻也随机化了，于是抽稀样本连"时间轴"都不成立。

### 1.3 ⚠️ 真实上传**会**正确写 `stroke_records`（实测）

`server/ingest.py` 的 `build_operations` 把每个 swing 翻成 `entity_type='stroke_record'`  
的 operation，走 `sync.push` 落库。隔离实测（5 拍，1 拍 `unknown`）：

```
L2 入库: stroke_ops=4  →  stroke_records 4 行，时刻 [12500, 84000, 210400, 655000]（单调真实）
```

**所以 L2 通道本身是好的**，问题只在演示数据。

### 1.4 🔴 真正会咬人的：管理后台看不到"真数据"

四条上传口各落各的层，**没有任何一条能同时满足 `/strokes` 页的两个依赖**  
（标注库 `sessions` 行 + L0 `raw_package` 形态）：

| 接口                               | 归档形态            | 落哪层     | `/strokes` 能看到逐拍？ | L2 有逐拍？ | 鉴权             |
| -------------------------------- | --------------- | ------- | ----------------- | ------- | -------------- |
| `POST /api/sessions`             | `raw_package`   | L0 + L1 | ✅ **可以**          | ❌ 不写 L2 | 管理后台 Cookie    |
| `POST /api/prod/sessions`        | `match_session` | L0 + L2 | ❌ **0 条**         | ✅       | `X-Ingest-Key` |
| `POST /api/ingest/watch-session` | 不归档             | 仅 L2    | ❌ 0 条             | ✅       | `X-Ingest-Key` |
| `POST /api/raw/sessions`         | `raw_package`   | 仅 L0    | ❌                 | ❌       | `X-Ingest-Key` |

**实测证据**（隔离环境，同一份 5 拍数据）：

```
A) 标注通道 → /strokes: exists=True 逐拍 5 条 raw.has_package=True
B) 终端通道 → /strokes: exists=True 逐拍 0 条 raw.has_package=False
              （但 L2 stroke_records 有 4 行真实数据）
```

根因：`converter.load_raw_payload` **显式只认 `shape='raw_package'`**，  
且要求标注库 `sessions` 表里有该会话的行；而 App 走的是 `/api/prod/sessions`，  
它写的是 `match_session`，且不建标注会话行。**全程不报错。**

补充：台账 `/annotation#history` 的 `history_metadata()` 硬编码  
`WHERE s.id LIKE 'hist-%'`，因此**任何真实会话都不会出现在台账里**。

### 1.5 采集端现状（头号阻塞）

| 端     | 采集                                               | 网络               | 落盘                    |
| ----- | ------------------------------------------------ | ---------------- | --------------------- |
| Watch | ❌ 全 mock（无 `CoreMotion` / `HKWorkoutSession` 数据） | ❌ 无 `URLSession` | ❌ **无 `FileManager`** |
| Phone | ❌ 全 `MockData`（29 处引用）                           | ❌ 无 `URLSession` | ❌ 无                   |

- `WatchTennis` 是**独立** watchOS App，`ATennis` 未内嵌 watch target（`WATCH_CHANNEL.md` §3 记为 M1-1）。
- ⚠️ **文档与代码漂移**：`WATCH_CHANNEL.md` 第 193-195 行称"已落盘在  
  `Documents/sessions/match_<id>.json`，真机可直接核对"—— 全工程搜不到  
  `FileManager`，**这一步并没有实现**。按文档操作会扑空。

---

## 2. 测试方案

### 阶段 0 · 后端链路自检（**今天可做，不需要手表**）

目的：把"上传 → 落库 → 展示"每一环单独验证掉，把手表接上后**只剩硬件变量**。

1. 造一份符合契约的整包 JSON（`{"session": {...}, "samples": [...]}`，swings 时刻单调）。
2. 三条通道各传一次，记录基线：
   ```bash
   # 取管理后台 Cookie（标注通道要）
   curl -s -c /tmp/ace.jar -o /dev/null \
        -d 'username=admin&password=admin123&next=/' \
        http://172.20.49.43:8787/login

   # ① 标注通道 → /strokes 页能看见
   curl -s -b /tmp/ace.jar \
        -F 'id=TEST-0001' -F 'wrist=right' \
        -F "raw=@/path/to/match_TEST-0001.json" \
        http://172.20.49.43:8787/api/sessions

   # ② 终端通道 → L2 + App
   curl -s -X POST http://172.20.49.43:8787/api/prod/sessions \
        -H "X-Ingest-Key: $(ssh acemate 'grep -h NETPULSE_INGEST_KEY /srv/acemate/backend/deploy/acemate.env | cut -d= -f2')" \
        -H 'Content-Type: application/json' \
        -d '{"openid":"dev_field_test","student_id":"stu-00001001","session":{...}}'
   ```
   > 上述两条已封装成 `scripts/upload_watch_session.py`（见 §2.3），  
   > 带格式校验与"该去哪个页面核对"的提示。
3. **验收**：能明确回答"我的数据在哪条通道、哪个页面能看到"。  
   若 ① 可见、② 不可见 —— 那是设计使然（见 §1.4），**不是 bug**，但必须知道。

### 阶段 1 · Watch 真机采集（最小闭环，推荐先做这个）

目的：让表产出**真实** `MatchSession`，**先不碰自动上行**，用手动导出验证端到端。

| #   | 要做的事                                                        | 代码位置                                  | 验收                      |
| --- | ----------------------------------------------------------- | ------------------------------------- | ----------------------- |
| 1.1 | `HKWorkoutSession(.tennis)` + `HKLiveWorkoutBuilder` 取心率/时长 | 新建 `WorkoutManager.swift`             | 表上能起一场训练并看到实时心率         |
| 1.2 | `CMMotionManager.deviceMotion` 100Hz + 挥拍门限检测               | 新建 `SwingDetector.swift`              | 打一拍 → 计数 +1             |
| 1.3 | 组装 `MatchSession`（契约类型，已在 `Shared/TennisContract.swift`）    | `WorkoutManager.finishedMatchSession` | 结束训练得到完整对象              |
| 1.4 | **落盘** `Documents/sessions/match_<id>.json`（当前完全没有）         | 同上                                    | Xcode → Devices 能下载到该文件 |
| 1.5 | 手动导出发到 Mac，用 §2.3 脚本上传                                      | —                                     | `/strokes` 页能看到你打的每一拍   |

**导出方式**：Xcode → `Window > Devices and Simulators` → 选设备 → 选 App →  
`…` → `Download Container` → 右键"显示包内容" → `AppData/Documents/sessions/`。

> 阶段 1 刻意**不接 WCSession**：把"采集对不对"和"传输通不通"两个问题分开，  
> 否则出问题时无法判断是哪一段。

### 阶段 2 · WCSession 自动上行

| #   | 要做的事                                                          | 依据                              |
| --- | ------------------------------------------------------------- | ------------------------------- |
| 2.1 | 把 watchOS target 并入 Phone 工程（或同 App ID 前缀配对）← **头号阻塞**        | `WATCH_CHANNEL.md` §3           |
| 2.2 | 两端 `WCSessionDelegate`（`sendMessage` 失败回落 `transferUserInfo`） | §2.2 消息类型已在契约里                  |
| 2.3 | Phone 侧 `SessionUploader` → `POST /api/prod/sessions`         | `WATCH_CHANNEL.md` §4.1 写入顺序不能反 |
| 2.4 | 本地队列 + 指数退避重试                                                 | §4.3                            |

**验收**：打完一场，**不碰 Mac**，几分钟内 App 里能看到该场；  
重传同 id 返回 `duplicate` 且不改库。

### 阶段 3 · 原始波形（可选，看是否要重训模型）

`CMBatchedSensorManager` 800Hz/200Hz → 压缩 → `transferFile` → `POST /api/raw/sessions`。  
不做的话波形页与"重算识别结果"能力就一直没有。

---

## 3. 需要先决策的两件事

### 3.1 管理后台要不要看真实采集数据？

现阶段 `/strokes` 只认标注通道，App 上传的数据（哪怕波形齐全）**在管理后台是隐形的**。  
三种做法：

| 方案                                                           | 改动                 | 代价                                    |
| ------------------------------------------------------------ | ------------------ | ------------------------------------- |
| **A. 不动**（推荐先这样）                                             | 无                  | 测试时须手动走标注通道；要说清"两个后台看两拨数据"            |
| B. 让 `load_raw_payload` 在缺 `raw_package` 时回落 `match_session` | `converter.py` 一处  | 回落到的包**没有 samples**，波形页会空 —— 得同时改页面提示 |
| C. 台账放开 `hist-%` 过滤，改为列全部标注会话                                | `stats.py` 两处 + 页面 | 会混入真实会话，需同时处理学员归属与筛选                  |

### 3.2 采集口径（会直接决定"测出来对不对"）

- **球速**：腕上只能测**拍头速度**（`racketHeadSpeedKmh`），不是球速。契约已按此收口，  
  但界面上仍叫"球速"，实测时别拿它跟雷达枪比。
- **硬件准入**：11 个字段腕上物理测不到（甜区 / 球自转 / 落点 / 胜负），  
  已从上行白名单移除；实测时不要期待这些字段有值。详见  
  `.workbuddy/skills/hardware-gate-audit/`。

---

## 4. 验收清单（可直接勾）

**阶段 0**

- [ ] 标注通道上传后 `/strokes?session=<id>` 逐拍条数 = JSON 里的 swings 数
- [ ] 终端通道上传后 `/api/prod/sessions/<id>` 的 `strokeCount` = 非 unknown 拍数
- [ ] 重复上传两次，第二次返回 `duplicate` / `raw_deduped=true`

**阶段 1**

- [ ] 表上打完一场，`match_<id>.json` 落盘且能被 Xcode 导出
- [x] 文件里 `swings[].impactTime` **单调递增**、末值 ≤ `duration`
- [x] `/strokes` 页显示的条数与文件一致
- [x] 心率/时长/卡路里与表上训练 App 一致

**阶段 2**

- [x] 打完即刻自动上行，App 内可见
- [x] 飞行模式下打完 → 恢复网络后自动补传成功
- [x] 同一场重复上行不产生重复记录

---

## 5. 已知"错了都不报错"的点（照抄前先读）

1. **形态不匹配 → 页面静默为空**（§1.4）。`match_session` ≠ `raw_package`。
2. **`Date` 编码策略**：`JSONEncoder` 必须 `.iso8601`；发数字后端每场 400  
   （Unix 纪元 vs Apple 参考纪元差 31 年）。
3. **`samples` 里的 `id` 字段**：800Hz × UUID ≈ 110 MB/小时纯噪声，须排除出 `CodingKeys`。
4. **大包不能走 `sendMessage`**，一律 `transferFile`。
5. **`onRawFile` 回调必须接线**：声明了不赋值 = 数据采了拿不出来（参照实现踩过）。
