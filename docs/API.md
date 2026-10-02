# HTTP 接口参考

Base URL：`http://127.0.0.1:8787`
交互式文档：`/docs`（FastAPI 自带 OpenAPI）

## 0. 约定

### 用户与设备识别

同步接口用请求头识别调用方，**优先级从高到低**：

| 来源 | 示例 |
|---|---|
| `X-User-Id` 请求头 | `X-User-Id: u_demo` |
| `?user_id=` 查询参数 | `?user_id=u_demo` |
| 缺省 | `u_demo`（演示用户） |

设备：`X-Device-Id`（可空，仅用于审计排障）。

### 鉴权与守卫（2026-09-29 起）

**管理后台已加登录**，不再是"无鉴权"。守卫是一个全局 HTTP 中间件（`server/app.py`），
按**路径前缀**分三档：

| 档 | 路径 | 放行条件 |
|---|---|---|
| 永远公开 | `/login`、`/logout`、`/assets/`、`/favicon.ico`、`/docs`、`/openapi.json`、`/redoc` | — |
| 自带鉴权 | `/api/prod/*`（Bearer token）、`/api/wechat/*` | 各自的令牌体系 |
| 设备口 | `/api/ingest/*`、`/api/raw/*` | 管理后台登录态 **或** `X-Ingest-Key`，二选一。⚠️ L0 的**读**口更严一档：未配置口令时只认登录态，见 §2.14 |
| 其余全部 | 管理站页面、`/api/students`、`/api/training`、`/api/sync` 等 | **必须登录** |

未登录时的响应形态**按路径决定**（不是按 `Accept` 头 —— curl / SDK 常常不带 Accept，
按 Accept 判断会让未登录的页面请求拿到 401 JSON 而不是跳转）：

- 非 `/api/*` → `302` 跳 `/login?next=<原路径>`
- `/api/*` → `401` + `{"error":"unauthenticated","login_url":"/login"}`

> ⚠️ `next` 参数**只接受站内相对路径**（挡 `//evil.com` 这类开放重定向）。
>
> ⚠️ 采集口口令 `NETPULSE_INGEST_KEY` 若未配置，设备口**完全放开**（开发期便利），
> 启动日志会打印告警。生产部署前必须设置。
>
> 采集端的身份仍是自报（`X-User-Id` / `openid`），`sync.py` 会拒绝跨 `user_id` 的写入，
> 这只是"防误写"，**不是安全边界**。

### 时间格式

统一 ISO8601 UTC 毫秒 + `Z`：`2026-09-24T10:00:00.000Z`。
服务端 `db.normalize_ts()` 兼容 `+08:00`、空格分隔等写法，但客户端应统一输出标准格式。

### 错误响应

| 状态 | 场景 | 体 |
|---|---|---|
| 400 | 请求不合法（`SyncError`） | `{"error":"bad_request","detail":"…"}` |
| 404 | 资源不存在或已软删除 | `{"detail":"学员不存在或已删除"}` |
| 500 | 未捕获异常 | `{"error":"internal_error","detail":"…"}` |

> ⚠️ **push 不会因为单条操作不合法而返回 400**。
> 只有「整个请求体格式错误」（如 `operations` 不是数组、超过 500 条）才是 400。
> 单条操作的失败体现在响应体 `results[i].result = "rejected"`。

---

# 一、页面路由

服务端渲染（Jinja2）。模板主体逐字节来自设计稿，见 [ARCHITECTURE.md](ARCHITECTURE.md)。

| 方法 | 路径 | 模板 | 导航激活 |
|---|---|---|---|
| GET | `/` | `pages/dashboard.html` | `overview` |
| GET | `/users` | `pages/users.html` | `user-management` |
| GET | `/training` | `pages/training.html` | `analytics-comparison` |
| GET | `/personas` | `pages/personas.html` | `user-personas` |
| GET | `/feedback` | `pages/feedback.html` | `feedback` |
| GET | `/settings` | `pages/settings.html` | `system-settings` |
| GET | `/annotation` | `pages/annotation.html` | `data-annotation` |

登录页（**不套 console 外壳**，独立模板，配色与主站一致）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/login` | 登录表单页；`?next=` 指定登录后回跳（仅站内相对路径） |
| POST | `/login` | 表单 `username` / `password`；成功 `302` 回跳并下发会话 Cookie |
| GET | `/logout` | 清会话与 Cookie，`302` 回 `/login` |

首次启动会创建默认管理员 `admin`：

- 口令优先取环境变量 `ACEMATE_ADMIN_PASSWORD`；
- 未设置则随机生成（`token_urlsafe(12)` = 16 字符），写入口令文件并**打印到启动日志**；
- ⚠️ **只有「随机生成」这一支才写口令文件**。走环境变量的调用方（部署脚本、隔离测试实例）本来就知道口令，再写文件既多一个泄露面，也正是 2026-09-29「隔离实例覆盖主库口令」那次事故的放大器；
- 口令文件**跟随数据目录**：默认 `<ACEMATE_DB 所在目录>/admin_initial_password.txt`（即 `var/admin_initial_password.txt`），可用 `ACEMATE_ADMIN_PASSWORD_FILE` 显式覆盖。**不要**把它写死在仓库路径上 —— 隔离实例（`ACEMATE_DB` 指向临时目录）会因为「临时库还没有账号」而走创建分支，把主库的口令文件覆盖掉；
- 口令以 `pbkdf2_sha256$200000$<salt>$<hash>` 存于 `admin_users`（20 万轮 + 每用户随机盐）；
- Cookie 名 `acemate_admin`，`HttpOnly` + `SameSite=Lax`，有效期 12 小时（本地 http 不加 `Secure`）；
- 会话存**服务端表** `admin_sessions`（不用签名 Cookie），因此可即时吊销；
- 改密后该账号的其它会话全部失效。二者都不在 `schema.sql` 里，由 `adminauth.init()` 建。
- **口令忘了或丢了**（哈希不可逆，反推不出来）就重置：
  `.venv/bin/python scripts/reset_admin_password.py`（Windows: `.venv\Scripts\python`）
  （`--list` 看有哪些账号、`--password 'xxx'` 指定新口令、`--print` 直接在终端显示；
  重置会**同时吊销该账号的全部会话**，这是有意的）

所有页面共享上下文：

| 变量 | 说明 |
|---|---|
| `nav_active` | 侧边栏高亮项 |
| `online_now` / `online_label` | 顶栏「实时在线 N 名学员」。取自 `platform_metrics.online_now`；**取数失败回退设计稿原值 1,428**，保证页面永不空白 |

`/settings` 额外注入（**真实水位，非演示**）：

| 变量 | 说明 |
|---|---|
| `sync` | `server_cursor`、`operations.{applied,duplicate,conflict_lost,rejected}`、`entities.{<type>.{live,tombstone}}` |
| `entity_desc` | 4 个可同步实体的中文说明 |
| `devices` / `protocols` | 硬件接入与 6 条协议说明（展示用） |
| `ds` | Apple Watch 训练数据源目录（`pipeline` / `gates` / `notes` / `audit` / `groups` / `summary`），与 `GET /api/datasources` 同一份事实源 |
| `annot_pointer` | 「数据采集与标注」**入口块**的两个计数（`sessions` / `heuristic_proposed`），与 `GET /api/annotation/overview` 同一份事实源 |

> ⚠️ **`/settings` 不再注入 `annot` / `annot_sessions`**（2026-10-01 起）。
> 原先本页把「数据采集与标注」整块渲染了一遍（概览数字 + 四段链路 + 进度表 + 标注口径），
> 与 `/annotation` 完全重复，且两处各自查库、口径容易慢慢漂移。
> 现在标注相关内容**只有 `/annotation` 一个出口**，本页只留一条带待办计数的入口块
> （`id="annotation-panel"`）。改标注文案请改 `server/annotation/stats.py`，不要在本页再加一份。

`/annotation`（数据采集与标注工作台，**这件事的唯一出口**）额外注入：

| 变量 | 说明 |
|---|---|
| `ov` | 总览数字，同 `GET /api/annotation/overview` 的 `overview` |
| `spec` | 标注规范（唯一真源，见 `server/annotation/stats.py`）：`chains`（分析链 vs 训练链对照）/ `stages`（四段链路）/ `status_machine`（4 个状态）/ `rules`（5 条标注逻辑）/ `notes`（8 条作业须知）/ `labels`（8 类标签 + 哪 5 类进 CoreML 基线）/ `label_note` |
| `progress` | 标注进度表，最近 12 场（`limit=12`），字段同 `sessions[]` |

页面脚本 `/assets/js/annotation.js` 从同源相对路径调下面 §2.13 的接口。
两个页面都带**页内锚点跳转带**（`#workbench` / `#chains` / `#state` / `#rules` / `#notes` / `#labels` / `#progress`；
`/settings` 侧为 `#sync` / `#protocol` / `#audit` / `#pipeline` / `#gates` / `#catalog` / `#acq-notes` / `#annotation-panel`），
落点用 `<div class="scroll-mt-24" id="...">` 提供。

长文案里的 `**强调**` 由 Jinja 过滤器 `bold`（注册于 `server/routers/pages.py`）渲染成 `<strong>`；
新增展示这些长文案的列时记得挂 `| bold`，否则页面上会原样露出两对星号。
`scripts/verify_annotation_pages.py` 会断言渲染结果里没有残留的 `**`。

### `/training` 的查询参数（新增）

页面模板 = 设计稿主体（保真） + `{% include "pages/_training_extra.html" %}`（功能）。
功能区块由 `scripts/build_pages.py` 追加，重新生成页面不会覆盖它。

| 参数 | 默认 | 说明 |
|---|---|---|
| `student` | 列表第一个（按 `nt_score` 降序） | 学员 id；传不存在的 id 会回落到第一个 |
| `range` | `30` | `7` / `14` / `30` / `all` |
| `from` / `to` | — | 自定义区间 `YYYY-MM-DD`；**优先级高于 `range`**。起止填反会自动纠正 |
| `metric` | 有数据的第一个指标 | 排行榜激活指标，见 `GET /api/training/leaderboards` |

所有筛选都走 query param（服务端渲染），因此每个视图都可直接分享/收藏，禁 JS 也可用。

`/training` 注入的上下文：

| 变量 | 说明 |
|---|---|
| `selector` / `student_links` | 全部学员及切换链接（带 `active` 标记） |
| `range_links` | 4 个时间档位链接（切换时自动丢弃 `from`/`to`） |
| `metric_links` | 排行榜指标切换链接 |
| `analysis` | 选定学员的区间纵向分析（KPI / 击球构成 / 逐场明细 / 规则化技术分析 / 强项短板标签） |
| `boards` | 全体学员横向对比（全指标矩阵 + 激活指标的最大值榜与平均值榜） |
| `rng` | 解析后的时间区间（`label` / `display` / `from_input` / `to_input`） |

---

# 二、业务数据接口（只读）

全部挂在 `/api` 前缀下。**所有列表查询都过滤 `deleted_at IS NULL`**。

## 2.1 概览看板

```
GET /api/platform/overview
```

无参数。返回 `kpis` / `online_now` / `heatmap` / `stroke_mix` / `hardware` /
`live_sessions` / `ntrp_distribution` / `ntrp_insight` / `ntrp_radar` /
`feedback_digest` / `hot_tags` / `positive_rate`。

> 全部来自 `platform_metrics`，缺 key 返回空数组/空对象而不是报错。

## 2.2 学员列表

```
GET /api/students
```

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `q` | str | | 模糊搜索 `name` / `device_id` / `racket` / `location` |
| `tier` | str | | `VIP\|Elite\|Pro\|Club\|Standard`，逗号分隔多选 |
| `nt_level` | str | | NTRP 档位，逗号分隔多选 |
| `hand` | str | | `右手` / `左手` |
| `backhand` | str | | `双反` / `单反` |
| `online` | bool | | 仅看在线 |
| `page` | int | 1 | |
| `page_size` | int | 10 | 1~200 |
| `sort` | str | `updated_at` | `updated_at\|nt_score\|sessions_count\|name\|forehand_avg\|serve_peak` |
| `order` | str | `desc` | `asc\|desc` |

响应：`{total, page, page_size, pages, items[], facets:{tiers[], nt_levels[]}}`

> `sort` 参数做了**白名单校验**（非法值静默回落 `updated_at`）—— 防止 SQL 注入。

## 2.3 单个学员档案

```
GET /api/students/{student_id}
```

响应：`{student, stats, recent_sessions[≤20]}`

- `stats`：`sessions` / `strokes` / `seconds` / `serve_peak` / `forehand_avg`
- `recent_sessions`：按 `started_at DESC`
- 学员不存在或已软删除 → **404**

## 2.4 训练对比

```
GET /api/training/compare?a=&b=&level=3.5
```

| 参数 | 说明 |
|---|---|
| `a` / `b` | 对比双方学员 id（当前实现返回 `nt_score DESC LIMIT 3` 的候选，`a`/`b` 为预留） |
| `level` | 对比基准 NTRP 档位 |

响应：`{players[], level, benchmark, diagnosis, radar, speed_bars, multi_rally,
history[], history_total}`

> 硬件准入（2026-10-02）：原 `landing_quadrant`（落点象限）已移除 —— 需球的飞行轨迹。

## 2.5 训练会话列表

```
GET /api/training/sessions?student_id=&session_type=&page=1&page_size=20
```

响应：`{total, page, page_size, items[]}`，按 `started_at DESC`。

## 2.6 某次训练的击球明细

```
GET /api/training/sessions/{session_id}/strokes?limit=500
```

| 参数 | 默认 | 范围 |
|---|---|---|
| `limit` | 500 | 1~5000 |

响应：

```json
{
  "session":  { "...": "会话对象（**未**过滤 deleted_at，历史数据）" },
  "count":    386,
  "by_type":  [ {"stroke_type":"forehand","n":210,"avg_speed":112.3,"peak_speed":120.1} ],
  "strokes":  [ "...", "按 seq_in_session 升序" ]
}
```

会话不存在 → 404。（会话已软删除时仍返回其击球数据，因为这是历史分析场景。）

## 2.7 单学员纵向分析（区间）

```
GET /api/training/student-analysis?student=stu-00001001&range=30
GET /api/training/student-analysis?student=stu-00001001&from=2026-09-15&to=2026-09-20
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `student` | ✔ | 学员 id |
| `range` | | `7\|14\|30\|all`（默认 `30`） |
| `from` / `to` | | 自定义区间，**优先于 `range`** |

响应：`{student, range, kpi[], mix[], total_strokes, sessions[], totals,
insights[], tags[], peer, headline, benchmark, empty, empty_hint}`

- `kpi[]` 7 张卡：场次 / 时长 / 击球量 / 消耗 / 发球最高速 / 正反手均速 / 心率
- `sessions[]` 逐场明细，每场带格式化字段与 `mix`（该场击球构成）
- `insights[]` 规则引擎产出的技术分析条目：`{tone: good|warn|info, icon, title, body}`
- `peer` = `{total, serve_rank}`，该学员在区间内发球最高速榜的位置

学员不存在 → 404。

> ⚠️ 算法与 `/training` 页面共用 `server/analytics.py`。**不要**在接口层另写 SQL ——
> 两处口径一旦分叉，页面与接口会给出不同数字且极难发现。

## 2.8 跨学员横向排行

```
GET /api/training/leaderboards?range=30&student=stu-00001001&metric=serve
GET /api/training/leaderboards?range=30&metric=slice&full=true
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `range` / `from` / `to` | `30` | 同 2.7 |
| `student` | — | 高亮该学员在榜中的位置（`it.selected`） |
| `metric` | 有数据的第一个 | `serve`\|`overall`\|`forehand`\|`backhand`\|`slice`\|`stroke_count`\|`duration`\|`calories`\|`avg_hr` |
| `full` | `false` | `true` 时额外返回 `matrix_rows`（全指标矩阵，体积较大） |

响应：`{range, students[], metrics[], matrix_rows[], metric_tabs[], active,
max_board[], avg_board[], active_metric, total_sessions, empty}`

每个榜项：`{rank, student_id, name, initial, avatar_url, tier, nt_label,
value, display, n, pct, selected}`。

### 两种数据口径（`active_metric.basis` / 每个指标的 `basis`）

| 口径 | 适用指标 | 说明 |
|---|---|---|
| **会话汇总** | 发球 / 整体 / 正手 / 反手球速，以及击球量 / 时长 / 消耗 / 心率 | 取自 `training_sessions` 的会话字段，即手表对**整场**的统计。无抽样偏差，每条记录齐全 |
| **逐拍抽样** | 切削球速 | `stroke_records` 在库中是**抽样存储**（实测一场十几条，并非该场全部击球）。会话表没有切削列，只能用样本 —— 因此它的峰值会系统性低于真实峰值，界面上必须标注样本量 |

**最大值 / 平均值 的定义**：最大值 = 该学员在区间内最好一场的该指标；
平均值 = 各场该指标的均值。排名一律按数值**降序**，`better` 字段（`high`/`low`/`null`）
只用于前端语义标注（心率是「越低越省力」），不参与排序。

## 2.9 Apple Watch 数据源目录

```
GET /api/datasources
```

响应：`{pipeline[], gates[], notes[], audit, groups[], summary}`

- `pipeline[]` 采集链路 5 步（Watch 采集 → iPhone 识别 → 本地门禁 → 离线同步 → 后端聚合）
- `gates[]` 6 道质量门禁（值域 / 配平 / 置信度 / 传感器 / 突变 / 交叉）
- `notes[]` 8 条**接入作业须知**（字段名=列名 / 缺失≠0 / 分母别用错 / 腕别决定符号 / 时基统一 /
  幂等键 / 跨算法版本不可比 / 敏感数据不出设备），每项 `{tone, icon, title, body}`
- `audit` 目录**核查结论**：`{date, baseline, before_fields, after_fields, findings[]}`，
  每条 `findings` 为 `{level, title, body}`，`level ∈ fix | add | warn`。
  这是 2026-10-01 全量比对 `schema.sql` / `analytics.py` / 各接口 / 全部页面模板后的结论，
  **不是文档描述，是实测**；页面上按 `fix → 已订正` / `add → 已补充` / 其它 → `需注意` 渲染
- `groups[]` 6 个分组共 **62 个字段**，每个字段带**八个**属性：

  | 属性 | 含义 |
  |---|---|
  | `field` / `key` / `unit` | 中文名 / 数据库列名（白名单键） / 单位 |
  | `source` | 采集来源（哪个框架 / 手表哪一路） |
  | `acquire` | 获取方式（怎么取到） |
  | `compute` | **采集后怎么算**（公式 / 聚合口径 / 是否原值透传） |
  | `artifact` | **算完变成什么**（落哪张表哪一列 / 派生量 / 不落库） |
  | `judge` | 判定规则（有效性门槛） |
  | `surface` | **在训练分析里的哪个位置展示**（页面 › 区块 › 元素） |
  | `required` / `sync` / `sync_meta` | 是否必填 / 上行目标 / 标签与提示 |
  | `shown` | `surface` 是否**实测**过（`false` = 已定义口径但当前界面看不到） |
  | `pending` | 非空表示「口径已定、后端尚未落地」的具体缺口 |

  分组：训练元数据(13) / **整场球质汇总（会话级）(8)** / 击球识别与球质（逐拍抽样）(19) /
  生理与恢复(10) / 环境与位置(5) / 设备与数据质量(7)。
- `summary` = `{total_groups, total_fields, required_fields, session_fields,
  stroke_fields, derive_fields, local_fields, shown_fields, hidden_fields,
  pending_fields, gates, notes, frameworks}`

`sync` 取值：`session`（写 `training_sessions`）/ `stroke`（写 `stroke_records`）/
`derive`（后端现算不落库）/ `local`（隐私或体量原因不上行）。

> ⚠️ **两条链路别看混**：本目录描述的是**分析链**（`server/datasources.py` 的 `PIPELINE`）——
> 产物是 `training_sessions` + `stroke_records`，用于训练分析与排行。
> 还有一条**训练链**（采集原始会话 → 人工标注 → 导出 CoreML 训练集），
> 定义在 `server/annotation/stats.py` 的 `STAGES`，两条链路共用同一批手表传感器数据。
> `/api/annotation/overview` 的 `chains` 字段给出逐项对照。

> ⚠️ 字段名必须与数据库列名一致。客户端上行字段走白名单校验，未登记字段直接
> `rejected`，不做静默丢弃。**缺失一律留空，严禁填 0。**
>
> ⚠️ **分母别用错**：`stroke_count` 是手表统计的**完整击球总数**，而 `stroke_records`
> 是**抽样存储**（一场约 10~20 条）。算构成占比必须用逐拍样本数作分母，
> 拿样本数除总数会得出「发球占 0.8%」这种错得离谱的结果。

## 2.10 用户画像

```
GET /api/personas
```

响应：`{segments[], total_users, histogram, donuts, insight, spotlight,
recommendations[], comparison[]}`

`segments` 每项额外带 `metrics`（由 `metrics_json` 解析而来）。

## 2.11 反馈工单

```
GET /api/feedback?status=&category=&q=&page=1&page_size=20
```

| 参数 | 说明 |
|---|---|
| `status` | `triage\|new\|sprint\|rejected\|done` |
| `category` | 分类精确匹配 |
| `q` | 模糊搜索 `title` / `body` / `code` |

响应：`{total, page, page_size, items[], by_status{}, kpis[], status_filters[],
hardware_split, categories[], timeline[], tags[], nextgen}`

排序：`occurred_at DESC, updated_at DESC`。

---

## 2.12 采集端接入（Apple Watch 上传）

> 完整数据流、字段映射表与口径约定见 **[INTEGRATION.md](INTEGRATION.md)**。

```
POST /api/ingest/watch-session
GET  /api/ingest/sessions?source=netpulse_watch&limit=50
```

**`POST /api/ingest/watch-session`** —— 把采集端的整包 `MatchSession` 结构化入库。

请求头 `X-User-Id`（默认 `u_demo`）/ `X-Device-Id`（审计用）。

请求体：

| 字段 | 必填 | 说明 |
|---|---|---|
| `session` | ✅ | 采集端会话对象 |
| `session.id` | ✅ | 采集端 UUID，**跨端幂等键** |
| `session.startedAt` | ✅ | ISO8601（不可解析则 400） |
| `session.swings[].type` | — | `forehand`/`backhand`/`serve`/`slice`/`volley`/`smash`；`unknown` 不入明细但仍计入 `stroke_count` |
| `student_id` | 否 | 归属学员，缺省回落该账号第一个学员 |
| `include_strokes` | 否 | 默认 `true` |
| `force` | 否 | 默认 `false`；`true` 时覆盖已入库场次（走 LWW） |

响应 `verdict` 取值与客户端处理约定：

| `verdict` | 含义 | 客户端 |
|---|---|---|
| `created` | 首次入库 | 成功 |
| `duplicate` | 之前已完整入库 | **视为成功**，不要重试 |
| `overwritten` | `force` 覆盖成功 | 成功 |
| `partial` | 部分 applied、部分 duplicate | 成功（通常为断点补传） |
| `rejected` | 全部被拒 | 查 `results[].reason` |

幂等由三道闸保证：`operation_id` 去重 → `(source, external_id)` 部分唯一索引 → LWW。
同一场重复上传任意多次，第二次起返回 `duplicate` 且**不改库**。

**`GET /api/ingest/sessions`** —— 按会话核对落库完整度，每条含：

`stroke_count`（检测到的总拍数）、`parts_total`（六类之和）、`unidentified`（未识别数）、
`balanced`（G2 配平门禁结果）、`stroke_rows`（逐拍行数）。

判 `balanced=false` 的两种情形：未识别占比 > 5%，或分项之和**超过**总数（分类计数串味）。

---

## 2.13 采集/标注通道（原独立标注后端 `:8000`，已并入）

> 2026-09-28 由 `miniprogram-3\backend\annotation-backend` **整体并入本进程**。
> **接口路径与请求/响应契约一字未改**，采集端只需把基址从 `:8000` 换成 `:8787`。
> 数据落在独立库 `var/annotation/annotations.db`（分库理由见 [INTEGRATION.md](INTEGRATION.md) §8.4）。

### 会话与标注

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/sessions` | 会话列表（含 `annotation_count` 等汇总） |
| POST | `/api/sessions` | 新建会话元数据（JSON） |
| **POST** | `/api/sessions`（`multipart/form-data`） | **上传整包**：`raw`(File，必需) + `video`(File，可选) + `meta`(Form JSON)。缺 `python-multipart` 会在**注册路由时**即抛 `RuntimeError` |
| GET | `/api/sessions/{sid}` | 单场元数据 |
| GET | `/api/sessions/{sid}/annotations` | 该场全部标注 |
| PUT | `/api/sessions/{sid}/annotations` | 覆盖式保存该场标注（`AnnotationSave`） |
| GET | `/api/sessions/{sid}/samples` | 波形采样（`count` / `duration` / 多通道键） |
| GET | `/api/sessions/{sid}/video` | 视频流（`Range` 支持，供 `<video>` 播放） |

### 一致性 / 导出 / 总览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/sessions/{sid}/consistency` | 单场多标注者一致性（`matched` / `agreed` / `rate` / `kappa`） |
| GET | `/api/consistency` | 全局一致性 |
| GET | `/api/sessions/{sid}/export/annotation.json` | 单场导出（匹配后的标签） |
| GET | `/api/export/annotation.json` | 全库标注导出 |
| GET | `/api/export/dataset.csv` | **训练集**：38 维特征 + `label`，共 39 列 |

**原始包的三级解析（`converter.load_raw_payload`）** —— 波形接口与训练集导出**共用同一个函数**：

1. L0 原始层按 `raw_id` 取（2026-09-29 起的权威来源，内容寻址、只追加、可重放）；
2. 会话表上的 `raw_path`（迁移期旧数据没有 `raw_id`，靠它兜底）；
3. `var/raw/files/raw_<sid>.json` 这个更早的命名约定。

> ⚠️ **不要在这里另写一套解析。** 这两条路径原先各有一份实现，`get_samples` 改用 L0 之后
> `build_dataset` 没跟上，于是出现「波形画得出来、点导出却 `404 no annotated samples`」
> 这种自相矛盾的现象，而且两端都不报错 —— 只能靠人肉对日志才发现。
> 收敛成 `load_raw_payload` 一处后不会再漂移。
| GET | `/api/annotation/overview` | 管理站用的聚合：总览 + `sessions[]`（`limit` 默认 50） |

### 标签空间（**刻意与展示层不同**）

- **标注/训练 8 类**：`forehand` / `backhand` / `serve` / `volley` / `slice` / `smash` / `lob` / `drop`（`LABELS_ALL`）
- **工作台默认 5 类**：`LABELS_5`（去掉 `smash`/`lob`/`drop` 的常用集）
- **展示层 6 类**：`SHOT_TYPES`，见 §2.12

一致性用 **Cohen's κ**（非简单一致率），因为两人都标 `forehand` 的高概率本身不构成「一致性好」的证据。

### 微信登录（身份层，`prod_api`）

`prod_api` 的职责已**收窄为身份层**：只签发/吊销令牌、把令牌换成 openid。
产品数据接口全部搬到 §2.15（`server/routers/user_api.py`）。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/wechat/login` | `code` → 落库 token（30 天），返回 `{token, openid}` |
| POST | `/api/wechat/logout` | 令牌失效 |

> **开发期身份对齐**：iOS 端不掌握小程序 `wx.login` 的 `code`，无法自行推导出同一 `openid`（服务端派生规则是 `dev_<sha1(code)[:16]>`）。
> 设 `NETPULSE_DEV_OPENID=<openid>` 环境变量后，未配 `WECHAT_APPID`/`WECHAT_SECRET` 时会直接采用该值，便于两端联调。

---

## 2.14 L0 原始数据层（`/api/raw/*`）

> 定位、铁律与主键设计见 **[INTEGRATION.md](INTEGRATION.md)** 与
> `server/rawstore.py` 文件头（后者是唯一权威）。分层总览见
> `ATennis/docs/DATA_LAYERS.md`。

**放行规则**：**写口与读口不同档**（`server/security.py`）。

| 口 | 端点 | 未配置 `NETPULSE_INGEST_KEY` | 已配置 |
|---|---|---|---|
| 写 | 两个 `POST` | 放开（开发期方便采集端联调，启动日志告警） | `X-Ingest-Key` 或管理登录态 |
| 读 | 五个 `GET` | **只认管理后台登录态** | `X-Ingest-Key` 或管理登录态 |

读口更严的原因：它吐出的是**整包原始运动数据**（含 `openid` 与逐样本波形），
只要有人能碰到端口就能全量拖走；而采集端**从不读**（它只上传），
所以读口没有任何理由跟着写口一起裸奔。
未授权时返回 401 + 明确文案（不是 403/404 —— 这层不隐藏资源存在性，
因为能到达这个接口本身就说明对方已经持有了凭证或就在本机）。

```text
POST /api/raw/sessions                 multipart 上传（采集端 / 工作台补传）
POST /api/raw/sessions/json            JSON 体内联上传（整包已在内存里时用）
GET  /api/raw/sessions?limit&openid&source&shape    版本列表（不含正文）
GET  /api/raw/sessions/{sid}?shape                  某会话最新版本元数据
GET  /api/raw/sessions/{sid}/revisions?shape        某会话全部版本
GET  /api/raw/sessions/{sid}/payload?revision&shape 整包正文（透明解压）
GET  /api/raw/stats                                 原始层水位
```

`POST /api/raw/sessions` 表单字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `raw` | ✅ | **文件字段**，`raw_*.json`（也接受已压缩的 `.gz` / `.zz` / 裸 deflate） |
| `id` | ✅ | 采集端会话 ID —— **跨端幂等键** |
| `openid` | 否 | 归属账号，便于按人筛 |
| `source` | 否 | 来源标记，如 `netpulse_watch` / `annotation_workbench` |
| `device_id` | 否 | 审计用 |
| `shape` | 否 | `raw_package`（默认，含 `samples` 波形）或 `match_session`（仅结论） |

响应：`{status, raw_id, session_id, shape, revision, sha256, byte_size, encoding, file_path}`，
`status` = `created` | `duplicate`。

**幂等与版本语义**（三条一起记，缺一个就会踩坑）：

| 情况 | 结果 |
|---|---|
| 同形态 + 同字节重传 | `duplicate`，`raw_id` 不变，**不产生新行** |
| 同形态 + **不同**字节 | `created`，`revision + 1`，**历史版本保留** |
| 同会话不同 `shape` | 各自独立记版本，**互不覆盖**（`raw_package` 的波形不会被 `match_session` 盖掉） |

- `raw_id = sha1(session_id | shape | sha256)[:16]` —— **内容寻址**，同一份字节必然同一个 id。
- **只追加**：`UPDATE` / `DELETE` 由 SQLite 触发器在库层面 `RAISE(ABORT)` 拦下，
  绕过应用直接写库也会被拒。
- **压缩与内联**：`INLINE_LIMIT = 1 MB`。未压缩且 ≤ 1 MB 的正文**内联**在 `payload` 列，
  同时留档到 `var/raw/files/`；超过上限或为压缩包则**只落文件**，`payload` 为 NULL。
- **三种压缩都认**（判据是"解压结果是不是 JSON"，不只看 magic）：
  `gzip`（RFC1952，`1f 8b`）/ `zlib`（RFC1950）/ `deflate`（RFC1951 **裸流**）。
  ⚠️ Apple 的 `NSData.compressed(using: .zlib)` 产出的是**裸 deflate**，
  名字叫 zlib 但既不是 gzip 也没有 zlib 头 —— 只看 magic 会误判为未压缩。
- 读取（`payload` 接口、标注工作台的波形接口）一律**透明解压**，
  调用方拿到的永远是原始 JSON，不需要知道它落盘时是否压缩。

> ⚠️ **升级环境注意**：`raw_sessions.payload` 曾经是 `NOT NULL`。该列改为可空后，
> `CREATE TABLE IF NOT EXISTS` **不会修改已存在的表** —— 于是老库上每条 gzip / 大包上传
> 都会 500（`NOT NULL constraint failed: raw_sessions.payload`），而**全新库完全正常**。
> `rawstore.init_db()` 里有一段表重建迁移会处理它，启动日志会打印
> `[rawstore] 迁移：raw_sessions.payload 去掉 NOT NULL，搬运 N 行历史版本`。

---

## 2.15 L3 终端展示层（`/api/prod/*`）

> 给微信端（小程序 / 多端 App）与 iOS App 用的接口。
> **只读 L0 + L2 中的 L2**，一次都不读 `raw_sessions`、也不读标注库 ——
> 终端用户看到的是**识别之后、对用户有用的数据**，而不是原始整包。

鉴权：读接口 `Authorization: Bearer <token>`（由 `/api/wechat/login` 签发）；
`openid` 是**校验结果**而非入参，传 `?openid=` 只作一致性自检（不一致 → 403）。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/sessions` | **上传口**（见下） |
| GET | `/me` | 当前身份、学员档案、场次总数 |
| POST | `/profile` | 自助更新昵称/持拍手/球拍/NTRP 等 |
| GET | `/sessions` | 训练历史（含会话级指标），`range` / `date_from` / `date_to` / `limit` |
| GET | `/sessions/{sid}` | 单场详情：逐拍明细 + 击球构成（含 `share` / 均速 / 极速） |
| GET | `/analysis` | 纵向技术分析（KPI / 构成 / 趋势 / 洞察），复用 `analytics.student_analysis` |
| GET | `/leaderboard` | 排行榜（**对终端用户匿名化**） |

**`POST /api/prod/sessions`** —— 采集端会话结论上行，鉴权走 `X-Ingest-Key`。
一次做三件事（原先只把整包塞进标注库的 `prod_sessions`，终端用户永远拿不到分析）：

1. **归档**：原样封存进 L0，形态 `match_session`（sha256 幂等，日后可重算）
2. **结构化**：翻译成 `training_sessions` + `stroke_records` 写入 L2
   （复用 `server/ingest.py`，因此幂等 / changelog / LWW 语义与 App 同步完全一致）
3. **开通学员**：`openid` 首次出现时自动建档（幂等）
   - `user_id_of(openid) = 'wx_' + openid`（与演示账号 `u_demo` 隔离）
   - `student_id_of(openid) = 'stu-wx-' + sha1(openid)[:12]`（**由 openid 派生**，天然幂等）
   - 走 `sync.push` 建档，因此会进 changelog，小程序端增量能拉到

请求体：

| 字段 | 必填 | 说明 |
|---|---|---|
| `openid` | ✅ | 采集端登录身份 |
| `session` | ✅ | `MatchSession`；`session.id` 必填（跨端幂等键） |
| `session.startedAt` | ✅ | **ISO8601 字符串** |
| `student_id` / `student_name` | 否 | 缺省按 openid 派生 |
| `include_strokes` | 否 | 默认 `true` |

响应含 `raw_layer{status,raw_id,revision,sha256,byte_size}`、
`analysis_db{applied,duplicate}`、`student_id`、`student_provisioned`。

> ⚠️ **`startedAt` 必须是 ISO8601 字符串，不能是数字。**
> Swift 的 `JSONEncoder` 默认把 `Date` 编成"距 2001-01-01 的秒数"，
> 服务端 `normalize_ts` 收到数字会返回 None → 400「缺失或无法解析为时间」。
> iOS 端已用 `dateEncodingStrategy = .iso8601`（`SessionUploader.encoder`）修掉。
> 服务端**刻意不接受数字**：Unix 纪元与 Apple 参考纪元差 978307200 秒，
> 猜错就是 31 年偏差 —— 唯一没有歧义的做法是客户端发 ISO。
>
> ⚠️ 时间过滤用 `date(started_at) >= ?` 而非字符串比较：`'2026-09-28'` 与
> `'2026-09-28T10:00:00.000Z'` 做字符串比较时，同一天的记录会因"前缀相同但更长"
> 被判为大于，从而漏掉最后一天。

**`GET /api/prod/leaderboard`** —— **隐私**：管理台的榜单带完整姓名与头像，
终端用户看到的**必须匿名化**：只透出 `rank` / 脱敏昵称（中文保留姓、英文保留首字母）/
数值 / `isMe`，**不透出 `student_id` / 头像 / 设备型号**，否则任何人都能通过榜单反查他人。
返回前 `top` 名 + 自己（自己若在榜外也能看到自己的名次）。

> ⚠️ `/api/prod/*` 的响应结构在 2026-09-29 有变更（历史从"整包 JSON 原文"改为
> L2 的结构化字段），小程序端需按上表核对适配。

---

# 三、同步接口

> 完整协议规格与客户端实现指南见 **[SYNC_PROTOCOL.md](SYNC_PROTOCOL.md)**。
> 这里是接口速查。

## 3.1 推送 `POST /api/sync/push`

**请求体是裸数组**（不是对象）：

```json
[
  {
    "operation_id": "8f14e45f-…",
    "entity_type": "training_session",
    "entity_id": "ts-01J8Z9K2M4",
    "action": "update",
    "payload": {"notes": "改了备注"},
    "client_updated_at": "2026-09-24T10:00:00.000Z"
  }
]
```

限制：**最多 500 条**（`MAX_OPS_PER_PUSH`），超出 → 400。

响应：

```json
{
  "received_at": "2026-09-24T11:02:33.412Z",
  "total": 1, "applied": 1, "duplicate": 0, "conflict_lost": 0, "rejected": 0,
  "cursor": 128,
  "results": [
    { "operation_id": "…", "entity_type": "training_session", "entity_id": "ts-…",
      "action": "update", "result": "applied", "applied": true,
      "server_version": 2, "server_updated_at": "2026-09-24T11:02:33.412Z",
      "server_seq": 128, "reason": "LWW 客户端版本更新，已应用" }
  ]
}
```

| `result` | 客户端处理 |
|---|---|
| `applied` | 本地标 `synced`，出队 |
| `duplicate` | **出队**（重试成功的正常结果，非错误） |
| `conflict_lost` | 用响应里的 `server_version` 更新本地，出队；等下次 pull 拿服务端内容 |
| `rejected` | 出队 + 记录日志。重试无用，`reason` 给出原因 |

**事务语义**：整批一个事务，逐条独立裁决；全部一起提交或一起回滚。

## 3.2 增量拉取 `GET /api/sync/pull`

| 参数 | 默认 | 说明 |
|---|---|---|
| `cursor` | 0 | 上次的 `next_cursor` |
| `limit` | 200 | 1~1000 |
| `entity_types` | 全部 | 逗号分隔 |

```json
{
  "cursor": 128, "next_cursor": 340, "has_more": true, "count": 200,
  "server_time": "2026-09-24T11:05:00.000Z",
  "changes": [
    { "seq": 129, "entity_type": "training_session", "entity_id": "ts-1",
      "action": "update", "version": 3, "updated_at": "…", "deleted_at": null,
      "payload": { "…": "实体完整快照" } }
  ]
}
```

- `has_more = true` → **继续拉**。
- 客户端必须**原子保存「数据 + next_cursor」**（写数据 → 写游标 → 提交）。
  先写游标后写数据、中间崩溃会**永久丢数据**。
- 每次 pull 都会写一行 `sync_pulls` 审计。

## 3.3 全量基线 `GET /api/sync/snapshot`

新设备首次同步用，避免从 `cursor=0` 回放全部历史。

| 参数 | 说明 |
|---|---|
| `entity_types` | 逗号分隔，缺省为全部 |

```json
{
  "cursor": 340,
  "server_time": "…",
  "entity_types": ["student_profile", "training_session", "stroke_record", "feedback_ticket"],
  "counts": { "student_profile": 1, "training_session": 40, "stroke_record": 480, "feedback_ticket": 1 },
  "entities": { "student_profile": [ "…含墓碑行（deleted_at 非空）" ] }
}
```

> **含墓碑行**：新设备因此也能得知哪些 id 已被删除。

## 3.4 确认 `POST /api/sync/ack`

```json
{ "cursor": 340, "device_id": "iphone-15-pro" }
```

响应：`{ "ok": true, "ack_cursor": 340, "server_time": "…" }`

服务端**不保存待确认队列**（changelog 是无状态的只增表），
这里只把确认水位记进 `sync_pulls`，便于排查「某设备卡在哪个游标」。

## 3.5 同步水位 `GET /api/sync/status`

```json
{
  "user_id": "u_demo",
  "server_cursor": 128,
  "server_time": "…",
  "operations": { "total": 12, "applied": 9, "duplicate": 3, "conflict_lost": 0, "rejected": 0 },
  "entities": {
    "training_session": { "live": 40, "tombstone": 2 },
    "stroke_record":    { "live": 480, "tombstone": 1 }
  },
  "recent_pulls": [ "…最近 5 次" ],
  "entity_types": ["feedback_ticket", "stroke_record", "student_profile", "training_session"]
}
```

`entities.<type>` 的 `live` / `tombstone` 分别统计 `deleted_at IS NULL` / `IS NOT NULL` ——
用它一眼看出软删除是否按预期工作。

---

## 四、curl 示例

```bash
# 概览
curl -s 'http://127.0.0.1:8787/api/platform/overview' | python -m json.tool | head -30

# 搜索 3.5 档在线学员
curl -s 'http://127.0.0.1:8787/api/students?nt_level=3.5&online=true&page_size=5'

# 推送一条更新
curl -s -X POST 'http://127.0.0.1:8787/api/sync/push' \
  -H 'Content-Type: application/json' -H 'X-User-Id: u_demo' \
  -d '[{"operation_id":"demo-op-001","entity_type":"training_session",
        "entity_id":"ts-1","action":"update",
        "payload":{"notes":"curl 测试"},
        "client_updated_at":"2026-09-24T12:00:00.000Z"}]'

# 再推一次完全相同的请求 → 应该得到 duplicate（幂等）
# （把上面那条命令再跑一遍即可观察）

# 增量拉取
curl -s 'http://127.0.0.1:8787/api/sync/pull?cursor=0&limit=5'

# 新设备基线
curl -s 'http://127.0.0.1:8787/api/sync/snapshot'

# 同步水位
curl -s 'http://127.0.0.1:8787/api/sync/status'
```

### 管理后台登录之后才有权限的调用

```bash
# 1) 登录，把会话 Cookie 存进 cookie.jar
#    口令在 var/admin_initial_password.txt（跟随数据目录）；
#    忘了就重置：.venv/bin/python scripts/reset_admin_password.py --print
curl -s -c cookie.jar -o /dev/null -w '%{http_code} %{redirect_url}\n' \
  -X POST 'http://127.0.0.1:8787/login' \
  -d 'username=admin&password=<口令文件里的口令>'
# → 401 = 口令错（返回登录页，文案「用户名或口令不正确」）
#   302 到 / 或 next 指定的路径 = 成功

# 2) 带上 Cookie 调管理接口
curl -s -b cookie.jar 'http://127.0.0.1:8787/api/admin/me'
curl -s -b cookie.jar 'http://127.0.0.1:8787/api/raw/stats' | python -m json.tool

# 3) 退出（服务端会话立即吊销，不只是清 Cookie）
curl -s -b cookie.jar -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:8787/logout'
```

### L0 原始层：上传 / 回看 / 取正文

```bash
BASE=http://127.0.0.1:8787
KEY=''   # 服务端设了 NETPULSE_INGEST_KEY 时填同值，否则留空
AUTH=${KEY:+-H "X-Ingest-Key: $KEY"}

# 上传（multipart；shape 省略即 raw_package，含波形的整包）
curl -s -X POST "$BASE/api/raw/sessions" $AUTH \
  -F 'id=VFY-CURL-001' -F 'openid=dev_openid' -F 'source=curl' \
  -F 'shape=raw_package' -F 'raw=@./raw_VFY-CURL-001.json'

# 同一份字节再传一次 → status=duplicate，raw_id 不变（幂等）
curl -s -X POST "$BASE/api/raw/sessions" $AUTH \
  -F 'id=VFY-CURL-001' -F 'raw=@./raw_VFY-CURL-001.json'

# 该会话有几个版本（改过一个字节就会多一条 revision）
curl -s "$BASE/api/raw/sessions/VFY-CURL-001/revisions" $AUTH | python -m json.tool

# 取整包正文（压缩包会在这里被透明解压）
curl -s "$BASE/api/raw/sessions/VFY-CURL-001/payload?shape=raw_package" $AUTH \
  | python -m json.tool | head -20

# 原始层水位（版本数 / 会话数 / 各形态分布 / 总字节）
curl -s "$BASE/api/raw/stats" $AUTH | python -m json.tool
```

### L3 终端用户上行（采集端用的那条通道）

```bash
# 会话结论上行：归档 L0(match_session) + 结构化 L2 + 自动开通学员
curl -s -X POST 'http://127.0.0.1:8787/api/prod/sessions' \
  -H 'Content-Type: application/json' -H 'X-Ingest-Key: <可选>' \
  -d '{
    "openid": "dev_openid",
    "session": {
      "id": "VFY-CURL-L3-001",
      "startedAt": "2026-09-29T02:00:00Z",
      "endedAt": "2026-09-29T03:00:00Z",
      "wrist": "right", "duration": 3600,
      "avgHeartRate": 138, "maxHeartRate": 172,
      "activeCalories": 480, "distanceKm": 2.4,
      "swings": [{"type":"forehand","impactTime":12.5,
                  "racketHeadSpeedKmh":121.3,"confidence":0.93}]
    }
  }'
# ⚠️ startedAt 必须是 ISO8601 字符串。传数字（Swift 的 Date 默认编码）会被 400 拒绝。
```
