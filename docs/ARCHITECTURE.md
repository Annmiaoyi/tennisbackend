# 架构与实现说明

## 1. 总体分层

```
┌──────────────────────────────────────────────────────────────┐
│  浏览器（管理站）                                              │
│  5 个设计稿页面 + 1 个设置页                                   │
│  样式：web/assets/css/app.css（Tailwind 离线预编译产物）        │
│  字体：web/assets/fonts/*.woff2（本地，无 CDN 依赖）           │
└───────────────────────────┬──────────────────────────────────┘
                            │ HTTP
┌───────────────────────────▼──────────────────────────────────┐
│  FastAPI (server/app.py)                                      │
│                                                               │
│  pages.py     6 个页面路由（Jinja2 渲染）                      │
│  data_api.py  /api/*          只读业务数据（管理页取数）        │
│  sync_api.py  /api/sync/*     同步协议（App 读写入口）          │
│                                                               │
│  sync.py      同步引擎：幂等 / LWW / 软删除 / 游标             │
│  db.py        连接、事务（BEGIN IMMEDIATE）、时间归一化         │
└───────────────────────────┬──────────────────────────────────┘
                            │ SQLite WAL
┌───────────────────────────▼──────────────────────────────────┐
│  var/acemate.db                                               │
│  业务表 ×5：students / training_sessions / stroke_records      │
│            feedback_tickets / persona_segments                │
│  聚合表 ×2：nt_benchmarks / platform_metrics                   │
│  同步表 ×3：sync_operations（幂等）                            │
│            sync_changelog（游标）                              │
│            sync_pulls（审计）                                  │
└──────────────────────────────────────────────────────────────┘
```

## 2. 页面保真策略：逐字节复用，而不是照着截图手抄

这是本项目最重要的一个决策。

设计稿的每个 `code.html` 都是**自包含**的（Tailwind Play CDN + 内联 `tailwind.config`），
而且**每个页面都自带完整外壳**（`<aside>` 侧边栏 + `<header>` 顶栏 + `<main>` 主体）。
实测结构偏移对所有 5 个页面完全一致：

| 区段 | 字符区间 | md5 |
|---|---|---|
| `<head>` | `[0, 3919)` | 5 页**完全相同** |
| 外壳 `<body>…<main>` | `[3919, 11164)` | 5 页**仅导航激活项不同** |
| `<main>` 主体 | `[11164, rfind('</main>'))` | 各页不同 |

因此 `scripts/build_pages.py` 做的是**抽取**而不是重写：

- 外壳 → `templates/base.html`（逐字节，仅 4 处必要替换：去 Google Fonts link、去内联 `<style>`、去 CDN `<script>`、换本地图片）
- 每页 `<main>` → `templates/pages/<name>.html`（逐字节）
- 导航激活态 → `{% set nav_active %}` 模板变量，由 `pages.py` 按路由注入

**收益**：不需要「目测像素、写 CSS 逼近」。任何设计稿更新，重跑脚本即可。
**代价**：模板里带着设计稿的冗余结构，不能手工精简（`build_pages.py` 会在生成时覆盖）。

### 需要人工维护的替换项

`base.html` 相对设计稿只有 4 处改动，都在文件顶部注释里标了原因：

1. 删除两条 Google Fonts `<link>` → 改为本地 `@font-face`
2. 删除内联 `<style>`（已被 `src/tailwind.input.css` 的 base 层吸收）
3. 删除 Tailwind Play CDN `<script>` → 改为引用编译产物 `app.css`
4. 远程图片 URL → `/assets/img/<语义化文件名>`

## 3. 前端从 Play CDN 迁到离线编译

| | 设计稿（Play CDN） | 本项目（CLI 预编译） |
|---|---|---|
| 运行时 | 浏览器里现编译，首屏白屏 + 闪一下 | 零运行时，`app.css` 34KB 直接下发 |
| 离线 | 断网即完全无样式 | 完全自包含 |
| 配置 | 内联 `tailwind.config` | `tailwind.config.js`，**从设计稿 1:1 提取** |

`tailwind.config.js` 的生成方式：把设计稿里 `tailwind.config = {...}` 的最外层花括号剥掉，直接拼进
`module.exports = { content: [...], <设计稿原内容> }`。

> ⚠️ 别用 `module.exports = { ...(设计稿原文), content: [...] }` —— `{...}` 里是对象字面量语法，
> 展开会得到 `SyntaxError: Unexpected token ':'`。必须剥括号后整段拼。

实测 `extend` 的键恰好是 `colors / borderRadius / spacing / fontFamily / fontSize`。
注意这里的 `primary` 是**纯白**、电光绿在 `primary-fixed`（详见 [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md)）。

## 4. ⚠️ 图标字号陷阱（曾导致整页垂直错位 12px，务必先读这条）

设计稿里图标的写法是：

```html
<span class="px-2 py-0.5 rounded-full bg-... flex items-center gap-1">
  <span class="material-symbols-outlined text-[12px]">trending_up</span>+12.8%
</span>
```

它依赖 **Tailwind 的 `text-[12px]` 去覆盖** `.material-symbols-outlined` 自带的 `font-size: 24px`。

Google Fonts 下发的 CSS 是这样的（顺序很关键：它在 `<head>` 里，**早于** Tailwind 注入的样式）：

```css
.material-symbols-outlined {
  font-family: 'Material Symbols Outlined';
  font-size: 24px;      /* ← 会被 text-[12px] 覆盖 */
  line-height: 1;       /* ← 不会被覆盖，保留 */
  /* 注意：Google 的版本里没有 font-variation-settings */
}
```

**踩的坑**：本项目最初把 `.material-symbols-outlined` 写在输入 CSS 的**末尾**（`@tailwind utilities` 之后）。
两者选择器优先级相同（都是单类），**后出现者胜** → 编译产物里 `font-size: 24px` 反而压掉了 `text-[12px]`。

后果链条：

```
全站图标被强制 24px
  → 徽章内 line-height:1 × 24px，比设计稿高 12px
  → 4 张 KPI 卡片等高拉伸，整行高 12px
  → 该行之后的所有内容整体下移 12px
  → 整页像素差异 10.90%
```

**正确做法**（现已如此）：

1. `.material-symbols-outlined` 必须放进 `@layer base`（层叠顺序早于 utilities）；
2. **不要**加 `font-variation-settings`。Google 的版本没有它，加了会禁用
   `font-optical-sizing: auto`（该属性让可变字体的 `opsz` 轴随 `font-size` 自动取值）。
   多加 `'opsz' 24` 会让所有尺寸的图标都用光学尺寸 24 的字形，与设计稿不一致。

修复后：5 页差异 **10.90% → 0.06%~0.13%**，且残留经二维搜索验证为
`(dx,dy) = (0,0)`，属字体光栅化噪点而非位置偏差。

> 教训：**同时使用框架工具类 + 手写基础类时，基础类要么进 `@layer base`，要么确保它排在 utilities 之前。**
> 「同名同优先级、靠书写顺序决胜负」在压缩产物里极难排查。

## 5. 字体本地化

| 字体 | 文件 | 大小 | 说明 |
|---|---|---|---|
| Inter | `inter-latin.woff2` | 48KB | 变量字体 `wght 100..900`，只取 latin |
| Inter | `inter-latin-ext.woff2` | 85KB | latin-ext 子集 |
| Material Symbols | `material-symbols-outlined.woff2` | 1.09MB | 见下 |

界面文案是中文，中文字形交给系统字体（PingFang SC / 微软雅黑），
Inter 只负责拉丁字母与数字，因此**不需要**其它语种子集。

### Material Symbols 选哪份？

设计稿引用了两条 Google Fonts 链接：

1. `...:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200` → 4 轴版，**4.0MB**
2. `...:wght,FILL@100..700,0..1&display=swap` → 2 轴版，**1.09MB**

两条的 `@font-face` 描述符（family / style / weight）完全相同，按 CSS 规则**后者覆盖前者**，
所以浏览器实际加载的是 **2 轴版（1.09MB）**。本项目采用同一份，
体积从 4MB 降到 1.09MB，且与设计稿渲染一致。

> 设计稿只在 `_5`（反馈页）用了一处内联 `style="font-variation-settings: 'FILL' 1;"`（星标图标），
> 所以 **FILL 轴是必需的** —— 2 轴版 `wght,FILL` 正好带它；不要换成纯 `wght` 版本。

**进一步优化（未做）**：全量字体含 4000+ 图标，本项目实际只用到 70 余个。
可用 `fontTools` + `brotli` 做字形子集化，预计压到 10KB 以内。
本机 Pillow 缺 RAQM（`features.check('raqm') == False`），无法直接渲染连字来挑选字形，
需要走 `fontTools` 解析 GSUB 里的多组件连字（`scripts/ms_icons.py` 已实现了解析逻辑，可直接复用）。

## 6. 外壳与毛玻璃

设计稿外壳本身就带毛玻璃，**直接照搬**即可：

```html
<header class="fixed top-0 left-72 right-0 h-20 bg-surface/85 backdrop-blur-xl z-40 ...">
```

编译器会把 `bg-surface/85` + `backdrop-blur-xl` 翻成 `background: rgba(...)` + `backdrop-filter: blur(24px)`。

> ⚠️ 两条经验（来自同项目小程序端，同样适用）：
> 1. **不要给外壳加 `will-change: transform` / `translateZ`** —— 会强制独立合成层，
>    截断 `backdrop-filter` 的采样，毛玻璃失效只剩半透明底（「能透看到后面的字」）。
> 2. **不要拆出「空背景层」** —— 子层的 `backdrop-filter` 同样会被外壳合成层截断。blur 必须在外壳上。

## 7. 数据访问：为什么有 `platform_metrics` 这张 JSON 表

管理页的图表既有「有实体语义」的数据（学员、会话、工单），也有大量**服务端算出来的展示型聚合**
（热力图矩阵、构成分布、雷达图坐标、NTRP 直方图……）。

- 前者用规范化表 → 保留关系约束 + 可增量同步；
- 后者是纯展示产物、结构随图表变、无实体语义 → 塞进 `platform_metrics(metric_key, value_json)`。

好处是不必为每个图表建窄表；代价是这些值没有 schema 约束，改图表时要同步改 `seed.py`。

访问入口统一是 `data_api.metric(key, default)`，**失败时返回默认值**，
页面绝不会因为某个 key 缺失而 500。

## 8. 并发与事务

SQLite 默认是「多读者 + 单写者」。同步接口的核心操作是**「查重 → 写入」**，
两步必须原子，否则两个并发的相同 `operation_id` 会同时通过查重、造成重复插入。

因此 `db.tx()` 默认用 `BEGIN IMMEDIATE`：

```python
@contextmanager
def tx(immediate=True):
    conn = connect()
    conn.execute('BEGIN IMMEDIATE' if immediate else 'BEGIN')   # 立刻取写锁
    try:
        yield conn
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        raise
```

配合 `PRAGMA busy_timeout = 30000`，并发写会排队而不是报 `database is locked`。
整个 push 批次跑在**一个**事务里：任一条被拒不会中断其余操作（逐条独立裁决），
但所有变更一起提交或一起回滚，客户端重试时不会看到「一半已写」的中间态。

## 9. 已知技术债

| 项 | 影响 | 建议 |
|---|---|---|
| 无鉴权，用户靠 `X-User-Id` 自报 | **安全风险**，可越权读写他人数据 | 上线前接入登录态，服务端签发身份 |
| 图标字体 1.09MB 未子集化 | 首屏多传约 1MB | 用 `scripts/ms_icons.py` 的连字解析做子集 |
| `sync_pulls` 只增不清理 | 长期运行表会变大 | 加定期归档任务 |
| `sync_operations` 只增不清理 | 同上 | 按 `received_at` 归档 90 天前的记录 |
| 模板含设计稿冗余结构 | 不能手工精简 | 保持「改设计稿 → 重跑 build_pages.py」的流程 |
| `db.init_db(force=True)` 未先关闭已有连接 | Windows 下可能删不掉文件 / 旧连接指向已删除 inode | 在 `force` 分支里先 `conn.close()` 并清空 `_local.conn` |
