# Tennisbackend

**网球运动训练分析系统 · 后端管理站**

> 本项目是**一个产品的三个部分**之一：
>
> | 端 | 仓库 | 平台 | 角色 |
> |---|---|---|---|
> | Watch | `Annmiaoyi/watchtennis` | watchOS | **传感器采集** —— 唯一能拿到运动数据的一端 |
> | Phone | `Annmiaoyi/atennis` | iOS | **展示与分析** —— 本地缓存 + 上行 |
> | **Server（本仓库）** | `Annmiaoyi/tennisbackend` | Python / FastAPI | **数据权威源 + 管理后台** |
>
> 数据流：`Watch --WCSession--> Phone --HTTP--> Server`
>
> ⚠️ 本工程**独立部署，产物不随 iOS App 打包**。App 只需调用它的 HTTP 接口。

---

## 先读这两个

| 文档 | 为什么 |
|---|---|
| **[docs/OVERVIEW.md](docs/OVERVIEW.md)** | 三端职责边界 · 术语表 · 枚举 · 时间口径 · 身份模型 —— **动手前必读** |
| **[docs/CONTRACT.md](docs/CONTRACT.md)** | 接口契约 · **字段映射** · 对接参数速查表 |

> ⚠️ **最容易踩的坑**：三端各自可能定义同名不同义的模型
> （`TrainingSession`）。字段名不一致时**接口不会报错**，
> 只会静默写出空值。命名规范见 OVERVIEW §3.1。

完整的文档地图见 **[docs/README.md](docs/README.md)**。

---

## 快速开始

前置：Python ≥ 3.9、Node ≥ 18（Node 只在编译 CSS 时用，运行网站不需要）。

```bash
cd /Users/Project/Atennis/Tennisbackend

# 0) 首次：建虚拟环境 + 装依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip

# 1) 前端样式（Tailwind 离线预编译）
npm ci && npm run build:css

# 2) 建库 + 灌演示数据 + 起服务
.venv/bin/python run.py --port 8787 --seed
```

Windows 上一键脚本更省事：`setup.cmd`（环境准备）→ `start.cmd --seed`（启动）。

启动后控制台会打印站点地址、数据库路径与 `admin` 初始口令。打开 <http://127.0.0.1:8787>。

| 参数 | 说明 |
|---|---|
| `--port 8787` | 监听端口（默认 8787） |
| `--host 0.0.0.0` | 对外暴露（默认 `127.0.0.1` 仅本机） |
| `--seed` | **重建**数据库并灌入演示数据（会清空现有数据） |
| `--reload` | 改代码自动重载，开发时用 |

`run.py` 是启动器：它比 `python -m server.app` 多做两件事 —— 先把工作目录切到项目根，
再在启动前检查依赖与 CSS 产物，缺什么直接给出可复制的修复命令。

curl 验证：

```bash
curl http://127.0.0.1:8787/api/sync/status   # 同步水位
```

### 起不来时先查这三条

| 现象 | 原因与处理 |
|---|---|
| 页面**完全没样式**（白底黑字堆在一起） | `web/assets/css/app.css` 不存在 → 跑 `npm ci && npm run build:css` |
| `ModuleNotFoundError: fastapi` | 装依赖：`.venv/bin/pip install -r requirements.txt`；注意 `python` 指向的解释器和装包的那个要一致 |
| `address already in use` | 8787 被占 → 换端口 `--port 8788` |

数据库落在 `var/`：
- `var/acemate.db`（L2 分析库）
- `var/raw/`（L0 原始层）
- `var/annotation/`（L1 标注层）

删掉 `var/` + `--seed` 即可回到干净演示数据。

---

## 数据分层

```
L0 原始数据层   var/raw/           只追加 · sha256 内容寻址 · 可重放
L1 标注层       var/annotation/    逐拍真值标签（训练识别模型用）
L2 识别·分析层  var/acemate.db     结构化结果（可重算）
L3 终端展示层   /api/prod/*        只读 L2，给 Phone / 小程序看
```

**为什么原始层要单独拆出来**：采集端上传的是高频整包（加速度 800Hz + 设备运动 200Hz，
一场 1 小时 ≈ 700 MB）。它有两个性质不能当普通业务表对待 ——
① 它是所有派生结果的唯一来源，解析逻辑改了必须能重跑；
② 它可能比当前算法更"聪明"，今天读不出的字段不代表以后读不出。

所以 L0 的铁律是：**只 INSERT，永不 UPDATE / DELETE**（由 SQLite 触发器在库层面强制）。
采不采原始波形的取舍见 [docs/RAW_LAYER_DECISION.md](docs/RAW_LAYER_DECISION.md)。

---

## 回归测试（提交前都该跑）

```bash
.venv/bin/python scripts/test_sync.py        # 同步协议端到端（54 项断言）
.venv/bin/python scripts/verify_layers.py    # L0–L3 + 鉴权端到端（73 项断言）
```

> ⚠️ 若你的环境设了 `HTTP_PROXY`，本机回环请求会被代理拦掉（表现为满屏 502）。
> 需要放行回环地址：`no_proxy=127.0.0.1,localhost`。

---

## 常用命令

```bash
npm run build:css                              # 编译 Tailwind（改样式后必跑）
.venv/bin/python run.py --port 8787            # 启动
.venv/bin/python run.py --port 8787 --seed     # 重建库 + 灌演示数据

.venv/bin/python scripts/visual_diff.py        # 5 页与设计稿逐像素比对
.venv/bin/python scripts/check_css_coverage.py # 确认 app.css 覆盖所有用到的 class
.venv/bin/python scripts/reset_admin_password.py
```

---

## 已知限制

1. **默认管理员口令**：首次启动随机生成并写入 `var/admin_initial_password.txt`。**上线前必须改**。
2. **写口默认放开**：未配 `NETPULSE_INGEST_KEY` 时采集上传口无口令保护（启动日志会告警）。
3. **SQLite 单写者**：适合单机。多实例水平扩展需换 PostgreSQL 并迁移幂等去重表。
4. **`var/` 不入版本管理**：其中含个人运动数据与初始口令，绝不入库。

完整限制与环境变量清单见 [docs/README.md](docs/README.md)。
