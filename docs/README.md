# Tennisbackend · 后端管理站开发文档

> 本项目是**一个产品的三个部分**之一。三端统一约定见 [OVERVIEW.md](OVERVIEW.md)。
>
> | 端 | 仓库 | 角色 |
> |---|---|---|
> | Watch | `Annmiaoyi/watchtennis` | 传感器采集 |
> | Phone | `Annmiaoyi/atennis` | 展示与分析 |
> | **Server（本仓库）** | `Annmiaoyi/tennisbackend` | 数据权威源 + 管理后台 |
>
> ⚠️ **与 App 分离**：本工程独立部署，产物**不随 iOS App 打包**。
> App 只需调用它的 HTTP 接口。

---

## 1. 这个工程做什么

| 目标 | 说明 |
|---|---|
| **数据权威源** | 后端数据库是训练记录的唯一真实来源（客户端只是缓存 + 待同步队列） |
| **四层数据架构** | L0 原始层（只追加可重放）/ L1 标注层 / L2 识别·分析层 / L3 终端展示层 |
| **端侧接入** | 接收 Watch→Phone 上行的会话结论与原始波形，幂等 / 冲突裁决 |
| **运营后台** | 5 个管理页：概览看板 / 学员管理 / 训练分析 / 用户画像 / 反馈工单 |
| **终端接口** | `/api/prod/*`：训练历史 / 逐拍明细 / 纵向分析 / 排行榜 |

---

## 2. 三分钟跑起来

```bash
cd /Users/Project/Atennis/Tennisbackend

# 0) 首次：建虚拟环境 + 装依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt          # Windows: .venv\Scripts\pip

# 1) 前端样式（Tailwind 离线预编译，产物 web/assets/css/app.css）
npm ci                # 首次
npm run build:css

# 2) 建库 + 灌演示数据 + 起服务
.venv/bin/python run.py --port 8787 --seed         # Windows: .venv\Scripts\python

# 之后日常启动（不重建库）
.venv/bin/python run.py --port 8787
```

Windows 上可省事一些：`setup.cmd`（环境准备）→ `start.cmd --seed`（启动）。

打开 <http://127.0.0.1:8787>（首次启动会把 `admin` 的初始口令打印到控制台）。

| 路由 | 页面 |
|---|---|
| `/` | 概览看板（Dashboard） |
| `/users` | 学员与用户档案 |
| `/training` | 训练记录深度分析与横向对比 |
| `/personas` | 用户画像与技术分群 |
| `/feedback` | 用户建议与需求工单 |
| `/settings` | 同步协议水位 |
| `/annotation` | 数据采集与标注工作台 |
| `/docs` | OpenAPI 交互式接口文档 |

> 只要改过 `src/tailwind.input.css` 或模板里的 class，就要重跑 `npm run build:css`。
> 若只改 Python，直接重启服务即可。

### 起不来时先查这三条

| 现象 | 原因与处理 |
|---|---|
| 页面**完全没样式** | `web/assets/css/app.css` 不存在 → `npm ci && npm run build:css` |
| `ModuleNotFoundError: fastapi` | 装依赖；注意 `python` 指向的解释器和装包的那个要一致 |
| `address already in use` | 8787 被占 → 换端口 `--port 8788` |

---

## 3. 文档索引

> **先看 [OVERVIEW.md](OVERVIEW.md)。** 三端的术语、枚举、时间口径、身份模型都在那里。

### 3.1 三端统一（跨端必读）

| 文档 | 内容 | 什么时候看 |
|---|---|---|
| [OVERVIEW.md](OVERVIEW.md) | 三端职责边界 · 术语表 · 枚举 · 时间口径 · 身份模型 · **契约代码化说明** | **动手前** |
| [CONTRACT.md](CONTRACT.md) | 接口契约 · 字段映射 · **对接参数速查表** · **契约变更流程** | 写对接代码 |
| [WATCH_CHANNEL.md](WATCH_CHANNEL.md) | Watch→WCSession→Phone→Server 通道设计 | 改通信层 |
| [RAW_LAYER_DECISION.md](RAW_LAYER_DECISION.md) | 原始波形采不采的论证与决策建议 | 决策前 |
| [ROADMAP.md](ROADMAP.md) | 三端统一开发规划（M0–M4）与验收清单 · **待拍板开放项 §7.1** | 排期 |

> 📌 **契约真源不是文档，是代码**：`contract/TennisContract.swift`。
> 两个 App 里的 `Shared/TennisContract.swift` 是同步副本。
> 流程与脚本见 [../contract/README.md](../contract/README.md) 与 [CONTRACT.md](CONTRACT.md) §0.6。

### 3.2 后端细节

| 文档 | 内容 | 什么时候看 |
|---|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 架构、目录结构、关键技术决策与踩过的坑 | 接手项目 / 改前端保真 |
| [SYNC_PROTOCOL.md](SYNC_PROTOCOL.md) | **离线优先同步协议**完整规格 + 客户端实现指南 | 写 Phone 同步层 |
| [DATA_MODEL.md](DATA_MODEL.md) | 表结构、同步列语义、软删除约束 | 改表 / 加实体 |
| [API.md](API.md) | 全部 HTTP 接口参考 | 查接口 |
| [INTEGRATION.md](INTEGRATION.md) | 采集端接入与端到端闭环、字段映射、质量门禁 | 查采集链路细节 |
| [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) | 颜色 / 字体 / 毛玻璃令牌，与设计稿的对应关系 | 改 UI |
| [TESTING.md](TESTING.md) | 像素级保真验收 + 同步协议测试 | 提交前自检 |
| [DEPLOY.md](DEPLOY.md) | 部署、备份、运维 | 上线 |

---

## 4. 技术栈

| 层 | 选型 | 为什么 |
|---|---|---|
| Web 框架 | FastAPI + Uvicorn | 自带 OpenAPI 文档；同步接口需要严格的事务与并发语义 |
| 数据库 | SQLite（WAL） | 单机部署零运维；`BEGIN IMMEDIATE` 提供写串行化，正好满足同步幂等 |
| 模板 | Jinja2 | 页面主体逐字节复用设计稿 HTML |
| 样式 | Tailwind CSS v3.4（**离线 CLI 预编译**） | 线上不能依赖 CDN 运行时 |
| 字体 | Inter + Material Symbols（**本地化**） | 消除首屏字体闪烁 |

---

## 5. 常用命令

```bash
# 前端
npm run build:css

# 服务（run.py = 带环境预检查的启动器）
.venv/bin/python run.py --port 8787          # 启动（不重建库）
.venv/bin/python run.py --port 8787 --seed   # 启动前重建库 + 灌演示数据

# 回归（提交前都该跑一遍）
.venv/bin/python scripts/verify_contract.py  # 契约 ↔ 后端一致性（38 项对账）
.venv/bin/python scripts/test_sync.py        # 同步协议端到端（54 项断言）
.venv/bin/python scripts/verify_layers.py    # L0–L3 + 鉴权端到端（73 项断言）
.venv/bin/python scripts/visual_diff.py      # 5 页与设计稿逐像素比对
.venv/bin/python scripts/check_css_coverage.py

# 契约同步（改过 contract/TennisContract.swift 之后）
bash scripts/sync_contract.sh                # 真源 → 两个 App 工程
bash scripts/sync_contract.sh --check        # 只查漂移（sha256）

# 工具
.venv/bin/python scripts/build_pages.py        # 从设计稿重新生成页面模板
.venv/bin/python scripts/fetch-assets.py       # 重新本地化设计稿里的远程图片
.venv/bin/python scripts/reset_admin_password.py
```

> ⚠️ `verify_layers.py` 会在独立端口起一个隔离实例。若你的环境设了 `HTTP_PROXY`，
> 本机回环请求会被代理拦掉（表现为满屏 502），需要放行回环地址：
> `no_proxy=127.0.0.1,localhost`。

---

## 6. 目录结构

```
Tennisbackend/
├── start.cmd / setup.cmd    Windows 一键脚本（调 run.py）
├── run.py                   启动器：切工作目录 + 启动前环境预检查
├── requirements.txt         运行依赖（fastapi / uvicorn / jinja2 / python-multipart）
├── requirements-dev.txt     开发脚本依赖（pillow / fonttools / brotli）
├── contract/                **三端共享契约真源**（TennisContract.swift）
├── docs/                    文档（三端统一文档在本目录）
├── server/                  FastAPI 应用
│   ├── app.py               入口（挂载路由与静态资源 + 登录守卫）
│   ├── db.py                连接 / 事务 / 时间归一化
│   ├── sync.py              离线优先同步引擎（核心）+ 字段白名单 SYNCABLE
│   ├── ingest.py            采集端整包 → 结构化 的翻译层
│   ├── rawstore.py          L0 原始层（只追加 · sha256 内容寻址）
│   ├── security.py          采集口令 / 读口分档
│   ├── adminauth.py         管理后台会话登录（PBKDF2）
│   ├── analytics.py         分析计算（纵向分析 / 排行榜）
│   ├── schema.sql           L2 表 DDL
│   ├── seed.py              演示数据
│   ├── annotation/          L1 标注层
│   ├── routers/             pages / data_api / sync_api / ingest_api / raw_api / user_api
│   └── templates/           base.html + pages/*.html
├── web/assets/              前端产物：css / js / fonts / img
├── src/tailwind.input.css   Tailwind 输入源
├── scripts/                 构建、校验、诊断脚本
├── var/                     运行时数据（不入版本管理）
│   ├── acemate.db           L2 分析库
│   ├── raw/                 L0 原始层（库 + files/）
│   └── annotation/          L1 标注库
└── tailwind.config.js
```

---

## 7. 环境变量

| 变量 | 作用 | 生产必配 |
|---|---|---|
| `NETPULSE_INGEST_KEY` | 设备写入口令。不配则写口**完全放开**（启动日志告警） | ✅ |
| `ACEMATE_ADMIN_PASSWORD` | 首次启动创建 `admin` 用的口令。不配则随机生成并写文件 | ✅ |
| `ACEMATE_ADMIN_PASSWORD_FILE` | 口令文件落点覆盖 | 否 |
| `NETPULSE_DEV_OPENID` | 开发期让 Phone 与小程序用同一个 openid | 仅开发 |
| `NETPULSE_RAW_DIR` / `NETPULSE_RAW_DB` | 重定向 L0 目录 / 库文件 | 否 |
| `NETPULSE_ANNOTATION_DIR` / `NETPULSE_DB` | 重定向 L1 目录 / 库文件 | 否 |
| `ACEMATE_DB` | 重定向 L2 库文件 | 否 |

---

## 8. 已知限制

1. **默认管理员口令**：首次启动随机生成并写入 `var/admin_initial_password.txt`。上线前必须改。
2. **写口默认放开**：未配置 `NETPULSE_INGEST_KEY` 时采集上传口无口令保护。上线前必须配。
3. **SQLite 单写者**：适合单机。多实例水平扩展需换 PostgreSQL 并迁移幂等去重表。
4. **图标字体未子集化**：见 [ARCHITECTURE.md](ARCHITECTURE.md)。
5. **`var/` 不入版本管理**：删掉 `var/` + `--seed` 即可回到干净演示数据。
