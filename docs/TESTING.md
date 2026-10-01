# 测试与验收

本项目有四套自动化校验，**提交前都应该跑一遍**：

```bash
python scripts/test_sync.py           # 同步协议：54 项断言
python scripts/verify_layers.py       # L0–L3 数据分层 + 管理登录守卫：73 项断言
python scripts/visual_diff.py         # 视觉保真：5 页逐像素比对
python scripts/check_css_coverage.py  # 样式覆盖：确认无 class 漏编译
```

---

# 一、同步协议测试

```bash
python scripts/test_sync.py
```

不依赖 HTTP 服务：把 `ACEMATE_DB` 指向临时目录（`tempfile.mkdtemp`），
直接调用 `server.sync` 的 `push / pull / snapshot`，跑完自动清理。
因此**可以在 CI 里裸跑**，不会碰生产库。

### 覆盖的场景

| # | 场景 | 对应需求 |
|---|---|---|
| 1 | 同 `operation_id` 重放 → `duplicate`，不产生新行 | 幂等、不能重复插入 |
| 2 | **同批次内** `operation_id` 重复 → 只应用一次 | 幂等（闸二） |
| 3 | LWW：相同时间戳 `conflict_lost`；更晚时间戳 `applied` 且 `version+1` | LWW 以 `updated_at` 为准 |
| 4 | **4 个实体**逐个删除：行仍在 + `deleted_at` 已标记 | 软删除，不物理删 |
| 5 | 删除服务端**不存在**的记录 → 写墓碑；带 payload 时保留快照 | 删除事件可传播 |
| 5b | 墓碑无业务数据时"复活" → `rejected`；补全必填字段 → `applied` | 约束不破 |
| 6 | 重复删除 → `conflict_lost`（幂等） | 幂等 |
| 7 | 4 张表都含 7 个同步列 | 字段齐备 |
| 8 | 游标分页续拉**无重复无遗漏**；重复拉同一游标返回空 | 服务器游标增量 |
| 9 | `snapshot` 含墓碑行 + 游标 == 全库最大 seq；从该游标续拉为空 | 新设备基线 |
| 10 | 未知实体 / 缺必填 / 跨用户写入 → 全部 `rejected`，且未改库 | 校验与租户隔离 |
| 11 | `normalize_ts` 兼容 `+08:00`、空格分隔、`Z` | 字符串比较 == 时间比较的前提 |
| 12 | `sync_operations` / `sync_pulls` 审计齐全 | 可排障 |

当前结果：**54 项通过，0 项失败**。

### 这套测试抓到的真实 bug（回归用例）

初版 `stroke_records` 的 `session_id` 是 `NOT NULL`。删除一条**服务端从未见过**的记录时，
墓碑 INSERT 只填同步列 → `sqlite3.IntegrityError: NOT NULL constraint failed: stroke_records.session_id`
→ 异常穿透整个 push 事务 → **整批回滚 + HTTP 500**。

即：客户端删掉一条服务端没见过的记录，会让整批同步失败。

修复（两处）：

**① `schema.sql`** —— 业务列去掉 `NOT NULL`，改表级 CHECK 表达真实规则：

```sql
CHECK (deleted_at IS NOT NULL OR (session_id IS NOT NULL AND stroke_type IS NOT NULL))
```

**② `sync.py`** —— 墓碑插入时带上客户端 payload（若有），
并在"复活墓碑"前校验必填字段能否凑齐，凑不齐返回 `rejected` 而不是抛异常。

回归用例固定在 `scripts/test_sync.py` 的 `[5]` 与 `[5b]`。

> 教训：**"服务端没见过就写墓碑"这条路径，契约上允许业务字段为空，
> 但 schema 用 `NOT NULL` 拒绝了它。** 声明式约束与业务语义不一致时，
> 它会以"整批事务回滚"这种破坏性最大的方式暴露。

---

# 一·补、数据分层与登录守卫（`verify_layers.py`）

```bash
python scripts/verify_layers.py
```

**它会自己起服务，而且用的是隔离的临时数据目录**（`NETPULSE_RAW_DIR` /
`NETPULSE_ANNOTATION_DIR` / `ACEMATE_DB` 指向 `%TEMP%\acemate_verify_*`，
端口 `:8790` 与 `:8791`），跑完即删。这样设计解决三个问题：

1. **可以反复跑**，不会污染 `var/` 里的真实 / 演示数据，也不需要先清库；
2. 每次都是**全新库**，所以「首次归档 `revision=1`」这类断言永远成立
   —— 否则第二次跑就会因上一轮的残留而假失败（真踩过）；
3. 顺手覆盖「**配了口令**」与「**没配口令**」两种放行分支 ——
   只跑一种会漏掉一整类问题（`require_device_or_admin` 手工调用时的
   `AttributeError` 就是因为只在"没配口令"的实例上测过，才被 `ingest_key_ok`
   的提前 return 掩盖了）；
4. **连口令文件也一起隔离**（`ACEMATE_ADMIN_PASSWORD_FILE`），并在收尾断言主库的
   `var/admin_initial_password.txt` 指纹没变。这是 2026-09-29 事故的直接产物：
   当时隔离实例（临时库、还没账号）走了「创建默认账号」分支，顺手把**主库的口令
   文件覆盖成测试口令**，而主库 `admin_users` 里的哈希没动 —— 于是登录报
   「用户名或口令不正确」，且口令已无从恢复，只能
   `scripts/reset_admin_password.py` 重设。根因见 `server/adminauth.py:password_file()`。

覆盖范围（A–K 共 73 项）：

| 组 | 内容 |
|---|---|
| A/B | 登录守卫：未登录 302/401、静态资源公开、`next` 三段链路（守卫→隐藏字段→回跳）、开放重定向 `//evil.com` 被改写、Cookie `HttpOnly+SameSite` |
| C | L0：首次 `created` / 同字节 `duplicate` / 异字节 `revision=2` / 内容寻址 `raw_id` / 只追加触发器拦 UPDATE+DELETE |
| D | L0 压缩与大包：gzip 10× 压缩、`.gz` 后缀、`byte_size` 为压缩后、读取透明解压、超 `INLINE_LIMIT` 只落文件（`payload` 为 NULL） |
| E | L0 **读写分档**：读口无凭证/口令错 → 401，口令对 或 登录态 → 200；写口无口令 → 401 |
| F | L1：`raw_id` 指针与 L0 同一行、`raw_path` 指向 `var/raw/files`、预标注 5 条、波形从 L0 取到 |
| G | L3：登录→上传（L0 归档 + L2 结构化 + 开通学员）→历史→逐拍→构成→分析→排行**匿名化**、跨租户 404 / 冒充 openid 403、重传幂等、两形态独立版本 |
| H | 采集端时间口径：数字时间戳（Swift `Date` 默认编码）→ **400 明确拒绝**（不能静默错成别的日期） |
| I | 登出即失效（服务端会话吊销，不只是清 Cookie） |
| J | 未配口令实例：读口仍 401、写口 200（开发期放开） |
| K | **隔离完整性**：跑完隔离实例后，主库 `var/admin_initial_password.txt` 的指纹必须不变（打印 `before/after` 前 8 位，对不上即 FAIL）—— 把 2026-09-29「隔离实例覆盖主库口令」那次事故变成可回归的检查项 |

---

# 二、视觉保真验收

```bash
python scripts/visual_diff.py            # 全部 5 页
python scripts/visual_diff.py dashboard  # 只跑一页
```

### 怎么做的

设计稿 HTML 是自包含的（Play CDN + Google Fonts 外链），所以可以**让 Chrome 同时渲染
"设计稿原页"和"我们的实现页"，在同一视口、同一 DPR 下截图，再逐像素比对**。

| 项 | 值 |
|---|---|
| 视口 | 1440 × 1600 |
| DPR | 1（`--force-device-scale-factor=1`） |
| 引擎 | `--headless=new` |
| 参考站 | 设计稿副本经由临时 HTTP 服务提供（避免 `file://` 跨域） |
| 容差 | 灰阶差 > 24 才算不一致（忽略字体抗锯齿） |

产出：

```
_shots/<name>_design.png   设计稿渲染
_shots/<name>_ours.png     我们的渲染
_shots/<name>_diff.png     差异可视化（差异像素涂成 #FF3C5A）
_shots/<name>_side.png     左右并排图
```

### 当前结果

| 页面 | 差异 |
|---|---|
| dashboard | 0.08% |
| training | 0.06% |
| users | 0.13% |
| personas | 0.07% |
| feedback | 0.12% |

残余差异经二维搜索验证为 `(dx,dy) = (0,0)`（不存在整体位移），是字体光栅化噪点。

### ⚠️ Chrome 截图的坑（会让人误判）

`--user-data-dir` 若指向一个**已存在实例**持有的目录，Chrome 会把请求
**转交给那个进程后立刻退出**。此时：

- 命令看似成功；
- 截图由后台进程**稍后**才写出；
- 如果脚本此时已经关掉参考站的 HTTP 服务 → 拍到 `ERR_CONNECTION_REFUSED` 页面。

`visual_diff.py` 的 `shoot()` 因此做了两件事：

1. **每次用独立的临时 profile 目录**（`tempfile.mkdtemp`），杜绝进程移交；
2. 调用后**轮询等待输出文件出现且大小稳定**，才认为截图完成。

### 定位偏移的辅助脚本

`visual_diff.py` 只告诉你"差多少"，不告诉你"差在哪"。为此有三个诊断脚本：

| 脚本 | 用途 |
|---|---|
| `diag_shift.py <页>` | 逐条带穷举垂直位移 `dy`，判断偏移是**常量**（结构错位）、**随 y 递增**（缩放差异）还是**局部突变**（某元素高度不同） |
| `diag_rows.py <页> [x0] [x1]` | 扫描"有内容"的行段，输出设计稿 vs 实现的 `y0..y1` 与 `Δy`。用不同 x 窗口跑可**定位到具体卡片** |
| `diag_cols.py <页> <y0> <y1>` | 在指定 y 带内按 x 列对比"有内容与否"，输出差异 x 区间；并报告每张卡片的内容底边 |

**这次就是靠它们定位到 ±12px 根因的**：逐个卡片扫描发现 4 张 KPI 卡片的
标签行都是 `245..260`（设计稿）vs `245..272`（实现）—— 同一行高了 12px，
从而锁定图标字号问题（详见 [ARCHITECTURE.md § 4](ARCHITECTURE.md)）。

> 方法论：**先判断偏移的"形状"，再缩小 x 窗口定位到元素。**
> 直接肉眼看 diff 图会被"满屏红"误导成"到处都是问题"。

---

# 三、样式覆盖率

```bash
python scripts/check_css_coverage.py
```

确认编译出的 `app.css` 覆盖了**设计稿 HTML + 本项目模板**里出现的所有 class。
漏掉一个 class，页面就会静默丢一段样式 —— 这正是 Tailwind CLI 相比 Play CDN
最容易出的问题（Play CDN 是在浏览器里按实际 DOM 现编译的）。

### 两个必须处理的细节

1. **十六进制转义逗号**。Tailwind 压缩产物会把选择器里的逗号写成 `\2c `：

   ```css
   .md\:col-span-2,.lg\:grid-cols-4      ← 压缩前
   .md\:col-span-2\,.lg\:grid-cols-4     ← 压缩后（转义形式）
   ```

   朴素字符串匹配会漏掉后半部分 → 误报缺失。脚本里的 `unescape_css()` 按 CSS 规则
   正确解码（含转义终止空格）。

2. **页面内联 `<style>` 里定义的自定义类**。例如设计稿 `_4`（画像页）在页面内
   自己 `<style>` 里定义了 `ambient-glow`。它不在 `tailwind.config` 里，
   也不该由 Tailwind 生成 → 脚本**收集页面内联 style 的选择器**并从缺失清单排除。

当前结果：✅ 全覆盖（468 个 class 词）。

---

# 四、手工验收清单

自动化跑完之后，人工确认几条自动化测不到的：

- [ ] 6 个页面在 1280 / 1440 / 1920 宽度下都不出现横向滚动条或元素重叠
- [ ] 顶栏与侧边栏的**毛玻璃生效**（能看见背后内容被模糊，而不是"透明直透"）
- [ ] 鼠标悬停侧边栏导航项有 `hover` 反馈，当前页高亮为电光绿实底 + 深色字
- [ ] 图标**不是**超大尺寸（若发现图标异常大，就是 `.material-symbols-outlined` 的
      `font-size` 又压掉了 `text-[*]` 工具类，见 ARCHITECTURE § 4）
- [ ] 断网后页面样式完整（验证无 CDN 运行时依赖）
- [ ] `/settings` 页的同步水位数字随 push/pull 变化
- [ ] `/docs`（OpenAPI）能打开且接口分组正确

---

# 五、CI 建议

```bash
set -e
npm ci
npm run build:css
python scripts/check_css_coverage.py
python scripts/test_sync.py
# 视觉比对需要 Chrome，视 CI 环境决定是否纳入
# python scripts/visual_diff.py
```

> `visual_diff.py` 依赖本机 Chrome/Edge 与设计稿目录（`../stitch_...`），
> 在 CI 里可能不可用。建议作为**本地提交前**的检查，而不是 CI 门禁。
