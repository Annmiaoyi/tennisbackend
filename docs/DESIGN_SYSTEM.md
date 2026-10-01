# 设计系统

**事实源**：`tailwind.config.js`（由设计稿 `_1/code.html` 的内联 `tailwind.config` **原样提取**，5 个页面配置完全一致）。
本文件说明各令牌的用途与**容易踩的坑**。

---

## 1. 颜色

### ⚠️ 头号陷阱：电光绿在 `primary-fixed`，不在 `primary`

| 令牌 | 色值 | 真实用途 |
|---|---|---|
| `primary` | `#ffffff` | **纯白**。大标题、关键数字 |
| `primary-fixed` | `#c3f400` | **电光绿**。品牌强调色、进度条、选中态 |
| `primary-container` | `#c3f400` | 同上（绿底） |
| `primary-fixed-dim` | `#abd600` | 绿色降饱和版 |
| `on-primary-fixed` | `#161e00` | **绿底上的深字** |
| `on-primary-container` | `#556d00` | 绿底上的次要深字 |

命名来自 Material 3 的语义配色体系：`primary` 是"主色"，
但这份设计稿把主色定成了**白色文字色**，品牌绿被命名为 `primary-fixed`。

> 改 UI 时如果写 `text-primary` 期望得到绿色 → 会得到白色。
> 想要绿色必须写 `text-primary-fixed`。同理 `bg-primary-container` 才是绿底。

### 中性色（暖绿灰，"surface" 系列）

由深到浅，用于层次分明的深色界面：

| 令牌 | 色值 | 用途 |
|---|---|---|
| `surface-container-lowest` | `#0a0f0d` | 画布底色、侧边栏底 |
| `background` / `surface` / `surface-dim` | `#0f1412` | 页面背景 |
| `surface-container-low` | `#181d1a` | 次级面板 |
| `surface-container` | `#1c211e` | 卡片 |
| `surface-container-high` | `#262b29` | 卡片内的次级块、徽章底 |
| `surface-container-highest` / `surface-variant` | `#313633` | 最高的层级块 |
| `surface-bright` | `#353a38` | 高亮点缀 |

### 文字色

| 令牌 | 色值 | 用途 |
|---|---|---|
| `on-surface` | `#dfe4e0` | 主文字 |
| `on-surface-variant` | `#c4c9ac` | 次要文字（带一点暖绿） |
| `outline` | `#8e9379` | 三级弱文字、标签 |
| `outline-variant` | `#444933` | 分隔线 |

### 语义色

| 令牌 | 色值 | 用途 |
|---|---|---|
| `secondary` | `#7bd0ff` | 蓝。信息、对比基准 B |
| `secondary-container` | `#00a6e0` | 蓝实底 |
| `tertiary` | `#ffffff` | ⚠️ 白（不是橙色） |
| `tertiary-fixed-dim` | `#ffb783` | **橙**（实际用到的橙） |
| `error` | `#ffb4ab` | 柔红。告警、下降趋势 |
| `error-container` | `#93000a` | 红实底 |

> 与 `primary` 一样，`tertiary` 也是白色，橙色藏在 `tertiary-fixed-dim`。

### 深色模式

`darkMode: "class"`，`<html class="dark">`。设计稿本身是 OLED 深色，
**没有提供浅色方案** —— 不要自行发明浅色主题。

---

## 2. 字号阶梯

`fontSize` 里每一项都是 `[字号, {lineHeight, letterSpacing, fontWeight}]`，
所以 Tailwind 生成的 `text-headline-lg` 会**一次性设好字号 + 行高 + 字距 + 字重**。

| 类名 | 字号 | 行高 | 字距 | 字重 |
|---|---|---|---|---|
| `display-hero` | 56px | 60px | -0.03em | 800 |
| `metric-digit` | 44px | 48px | -0.03em | 800 |
| `display-hero-mobile` | 40px | 44px | -0.02em | 800 |
| `headline-lg` | 32px | 38px | -0.02em | 700 |
| `headline-lg-mobile` | 26px | 32px | -0.015em | 700 |
| `headline-md` | 22px | 28px | -0.01em | 600 |
| `headline-sm` | 18px | 24px | 0 | 600 |
| `body-lg` | 16px | 24px | -0.005em | 400 |
| `body-md` | 14px | 20px | 0 | 400 |
| `label-md` | 12px | 16px | 0.04em | 600 |
| `label-sm` | 10px | 12px | **0.08em** | 700 |

> **负字距是刻意的**：大标题用 -0.02 ~ -0.03em 收紧，这是设计稿的观感来源。
> 别把它们"修"成正字距。
>
> **正字距只有 label 系列**（大写标签需要放开）。注意 `label-sm` 是 0.08em；
> 而正文里出现 `uppercase tracking-wider` 的地方用的是 Tailwind 默认的 **0.05em** —— 两者不同，别混。

### 字体族

`<body class="bg-background font-body-md text-on-surface antialiased">`

注意字体的施加方式：`fontFamily` 里每个**文本样式名**都映射到 `Inter`，
所以 `font-body-md` 既设 `font-family: Inter`（来自 `fontFamily.body-md`）
又设字号行高（来自 `fontSize.body-md`）。

**body 靠 `font-body-md` 拿到 Inter**，而不是靠 `font-sans`（后者没被覆盖，
仍是 Tailwind 默认栈）。因此自定义元素想用 Inter 必须显式加某个 `font-*` 文本类。

中文字形由系统字体兜底（PingFang SC / 微软雅黑），Inter 只管拉丁字母与数字。

### `antialiased`

设计稿在 `<body>` 上用了 `antialiased`（灰度抗锯齿）。
桌面端浏览器（Chrome/Safari/Firefox）表现正常，**保持一致即可**。

> 同项目的小程序端曾因灰度 AA 在深色底上把中文笔画削细而发虚，
> 那里改成了 `subpixel-antialiased`。**本网站不要照搬** ——
> 桌面端是不同的渲染路径，改了反而与设计稿不一致。

---

## 3. 圆角与间距

```js
borderRadius: { DEFAULT: '0.25rem', lg: '0.5rem', xl: '0.75rem', full: '9999px' }
```

即 4px / 8px / 12px / 全圆。卡片多用 `rounded-xl`，徽章用 `rounded-full`。

### 间距令牌（命名间距，非 Tailwind 数字阶梯）

| 令牌 | 值 | | 令牌 | 值 |
|---|---|---|---|---|
| `space-2xs` | 2px | | `space-lg` | 24px |
| `space-xs` | 4px | | `space-xl` | 32px |
| `space-sm` | 8px | | `space-2xl` | 48px |
| `space-md` | 16px | | `gutter` | 16px |
| `margin` | 16px | | `gutter-desktop` | 24px |
| `margin-tablet` | 24px | | `margin-desktop` | 40px |

用法如 `p-space-lg` `gap-space-sm` `mt-space-md`。
**仍有大量地方直接用了 Tailwind 数字阶梯**（`p-6` `gap-4` `mb-8`），两者混用是设计稿原样，别去统一。

---

## 4. 毛玻璃（Surface 2 / 3）

设计稿在外壳上直接用了 Tailwind 的透明度语法 + `backdrop-blur`：

```html
<!-- 顶栏：Surface 2 -->
<header class="fixed top-0 left-72 right-0 h-20 bg-surface/85 backdrop-blur-xl z-40 ...">

<!-- 侧边栏底部的 Live Telemetry 卡片：Surface 3 -->
<div class="bg-surface-container-high/70 backdrop-blur-lg ...">
```

编译后等价于：

```css
background: rgba(15, 20, 18, .85);
backdrop-filter: blur(24px);            /* blur-xl */
-webkit-backdrop-filter: blur(24px);
```

### ⚠️ 两条铁律（踩过）

1. **不要给毛玻璃外壳加 `will-change: transform` / `translateZ` / `translate3d`。**
   会强制该元素成为独立合成层，**截断 `backdrop-filter` 的采样源** ——
   blur 只能采到外壳自己的透明底，采不到背后的页面内容，
   结果毛玻璃失效、只剩半透明底（观感是「透明的、能直接看到后面的字」）。

2. **不要为了"分层"把背景拆成单独的空 `<div>`（如 `.top-bar-bg`）。**
   子层的 `backdrop-filter` 同样会被父级合成层截断。blur 必须**直接写在最外层外壳上**。

> 另：`blur` 用 `px` 不要用 `rpx`/`rem` —— 某些 WebView 的 `backdrop-filter`
> 对相对单位解析异常会导致整条声明失效。（此坑来自同项目小程序端，此处保留提醒。）

---

## 5. 设计稿的结构事实（供脚本使用）

5 个页面的 HTML 结构偏移**完全一致**：

| 区段 | 字符区间 |
|---|---|
| `<head>` | `[0, 3919)` |
| 外壳 `<body>…<main>` | `[3919, 11164)` |
| `<main>` 主体 | `[11164, rfind('</main>'))` |
| 尾部 | `</main></div></body></html>` |

- `<head>` 5 页字节相同；
- 外壳 5 页**仅侧边栏导航的激活 `<a>` 不同**；
- `body` 只有 1 个包裹 `<div class="pl-72">`，`<main class="relative pt-20 ...">`
  通过 `pt-20`（=80px）避让 `h-20` 的 fixed 顶栏。

这些常量固化在 `scripts/build_pages.py`（`HEAD_END=3919`、`MAIN_OPEN=11164`，带 `assert`）。
设计稿更新后若区间变了，脚本会立刻断言失败，而不是静默产出错位的页面。

---

## 6. 新增页面的正确姿势

```bash
# 1) 把设计稿 HTML 放进设计稿目录（沿用 _N 命名）
# 2) 在 scripts/build_pages.py 的 PAGES 里加一条映射
# 3) 重新生成
python scripts/build_pages.py
# 4) 在 server/routers/pages.py 的 PAGES 字典里加路由（含 nav_active）
# 5) 重编译样式并校验覆盖率
npm run build:css
python scripts/check_css_coverage.py
# 6) 像素比对
python scripts/visual_diff.py <新页名>
```

`check_css_coverage.py` 会检查编译产物是否覆盖了设计稿 + 模板里出现的**所有** class。
它内置了 `unescape_css()`，因为 Tailwind 压缩时会把选择器里的逗号转成
十六进制转义 `\2c `（朴素字符串匹配会漏）。同时它也会收集页面内联 `<style>` 里
定义的自定义类（如 `_4` 页的 `ambient-glow`）并从缺失清单里排除。
