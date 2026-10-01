# 部署与运维

## 1. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ACEMATE_DB` | `var/acemate.db` | SQLite 数据库路径。测试脚本用它指向临时库 |
| `ACEMATE_USER` | `u_demo` | 管理页默认归属用户（将来由登录态注入，届时可删） |

## 2. 构建与启动

### 首次部署

```bash
cd backend-web

# 1) 依赖（Windows 上直接跑 setup.cmd 可自动完成这三步）
python3 -m venv .venv && . .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -r requirements.txt
npm ci

# 2) 编译样式（产物 web/assets/css/app.css 需要一并部署）
npm run build:css

# 3) 建库（生产环境不要 --seed）
python run.py --port 8787
```

### 启动参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | |
| `--port` | `8787` | |
| `--reload` | 关 | 仅开发 |
| `--seed` | 关 | **启动前重建库 + 灌演示数据**。生产禁用 |

### 生产建议

```bash
# 多 worker（SQLite 是文件锁，worker 数不宜多，2~4 足够）
uvicorn server.app:app --host 0.0.0.0 --port 8787 --workers 4

# 或交给进程管理器（systemd / NSSM / supervisor），示例 unit：
```

```ini
[Unit]
Description=AceMate Backend
After=network.target

[Service]
WorkingDirectory=/srv/acemate/backend-web
Environment=ACEMATE_DB=/srv/acemate/data/acemate.db
ExecStart=/srv/acemate/.venv/bin/uvicorn server.app:app --host 0.0.0.0 --port 8787 --workers 4
Restart=always
User=acemate

[Install]
WantedBy=multi-user.target
```

> **把数据库放到数据盘**（如 `/srv/acemate/data/`），不要留在代码目录 ——
> 重新部署代码时容易误删。

## 3. 部署产物清单

必须随代码部署：

```
server/                    全部 Python 与 schema.sql / templates/
web/assets/css/app.css     Tailwind 编译产物（不是源文件）
web/assets/fonts/*.woff2   本地字体
web/assets/img/*           本地图片
src/                       保留（后续要重编译样式）
tailwind.config.js         ← 重编译样式必需
package.json               ← 重编译样式必需
```

不必部署：

```
node_modules/              构建期依赖
_shots/                    视觉比对产物
var/                       数据库（生产用 ACEMATE_DB 指向数据盘）
scripts/*.py               开发/校验脚本（CI 可用，生产不必）
docs/                      文档
```

> ⚠️ **`web/assets/css/app.css` 是必需品**。若只拷了源码没跑 `npm run build:css`，
> 页面会**完全没有样式**（Tailwind 的 class 全部是源码期生成的，没有运行时）。

## 4. 反向代理（Nginx）

```nginx
server {
    listen 443 ssl http2;
    server_name acemate-admin.example.com;

    ssl_certificate     /etc/ssl/acemate/fullchain.pem;
    ssl_certificate_key /etc/ssl/acemate/privkey.pem;

    # 字体与图片体积较大，开长缓存
    location /assets/fonts/ { proxy_pass http://127.0.0.1:8787; expires 30d; }
    location /assets/img/   { proxy_pass http://127.0.0.1:8787; expires 7d;  }
    # CSS 带内容哈希前必须短缓存（当前文件名无哈希）
    location /assets/       { proxy_pass http://127.0.0.1:8787; expires 1h;  }

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;      # push 批量可达 500 条，别用默认的 30s
    }
}
```

> `proxy_read_timeout` 要留够：单次 push 最多 500 条操作，虽然都在一个本地事务里，
> 但网络慢时整体耗时会上升。

## 5. 备份

SQLite 跑在 WAL 模式下，**不要直接 `cp` 主数据库文件**（会漏掉 `-wal` 里尚未 checkpoint 的数据）。

### 正确做法（推荐）

```bash
sqlite3 /srv/acemate/data/acemate.db ".backup /backup/acemate-$(date +%F).db"
```

`.backup` 会用 SQLite 的在线备份 API，对运行中的库安全。

### 或用 VACUUM INTO（SQLite ≥3.27）

```bash
sqlite3 /srv/acemate/data/acemate.db "VACUUM INTO '/backup/acemate-$(date +%F).db'"
```

### 定时任务

```cron
# 每天 03:15 备份，保留 30 天
15 3 * * * sqlite3 /srv/acemate/data/acemate.db ".backup /backup/acemate-$(date +\%F).db" && find /backup -name 'acemate-*.db' -mtime +30 -delete
```

### 恢复

```bash
systemctl stop acemate-backend
rm -f /srv/acemate/data/acemate.db /srv/acemate/data/acemate.db-wal /srv/acemate/data/acemate.db-shm
cp /backup/acemate-2026-09-24.db /srv/acemate/data/acemate.db
systemctl start acemate-backend
```

> 必须先停服务、并清掉 `-wal` / `-shm`。残留的 WAL 会让 SQLite 认为它与主库不匹配。

## 6. 数据保留与清理

`sync_operations` 与 `sync_pulls` 是**只增不长**的审计表，长期运行会持续变大。

```sql
-- 清理 90 天前的操作记录（保留同步能力不受影响：幂等只对"近期重试"有意义）
DELETE FROM sync_operations WHERE received_at < datetime('now', '-90 days');

-- 清理 30 天前的拉取审计
DELETE FROM sync_pulls WHERE created_at < datetime('now', '-30 days');

-- 回收空间
VACUUM;
```

> ⚠️ **清理 `sync_operations` 会削弱幂等保护**：
> 若客户端在 90 天后重发一个老 `operation_id`，去重表已无记录，
> 会退化到第二层保护（LWW 严格大于 → `conflict_lost`，仍不会损坏数据，见
> [SYNC_PROTOCOL.md § 7](SYNC_PROTOCOL.md)）。因此 90 天是安全的清理窗口。

> ⛔ **绝不要清理 `sync_changelog`**：它是游标的事实源。
> 删掉中间的行会让客户端 `seq > cursor` 的查询永久跳过那段变更 → 客户端永久丢数据。
> 若确实要压缩，只能整体重建（把所有客户端下线 → 重新 snapshot）。

### 软删除数据的归位

已软删除的行默认永久保留（墓碑需要传播给新设备）。
若要彻底清理，注意**不能简单物理删除** —— 否则新设备 snapshot 时看不到该 id 已删除，
会把它当作"从未存在"。若确实要清，必须同时把所有设备的下游数据一并清空重建。

## 7. 健康检查与监控

```bash
# 进程存活
curl -sf http://127.0.0.1:8787/api/sync/status > /dev/null && echo ok

# 关键业务指标
curl -s http://127.0.0.1:8787/api/sync/status | python -m json.tool
```

重点盯这几个数（来自 `/api/sync/status`）：

| 指标 | 健康特征 | 异常含义 |
|---|---|---|
| `server_cursor` | 持续增长 | 长期不动 → 没有写入（或服务挂了） |
| `operations.rejected` | 接近 0 | 升高 → 客户端在发不合法的操作，去查 `sync_operations.reason` |
| `operations.conflict_lost` | 有但不占多数 | 占比过高 → 多设备并发编辑同一记录，或客户端时钟异常 |
| `operations.duplicate` | 有属正常 | 占比极高 → 客户端没正确处理响应（收到成功却不出队），反复重发 |
| `entities.*.tombstone` | 缓慢增长 | 突增 → 客户端在误删数据 |
| `recent_pulls` | 有近期记录 | 长时间为空 → 客户端没在拉取 |

## 8. 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| 页面**完全没有样式** | 部署时漏了 `web/assets/css/app.css`，或没跑 `build:css` | 补编译产物 |
| 页面样式**部分缺失** | 改了模板 class 但没重编译 | `npm run build:css`，然后 `check_css_coverage.py` |
| 图标**异常巨大** | `.material-symbols-outlined` 的 `font-size:24px` 压掉了 `text-[*]` | 见 [ARCHITECTURE.md § 4](ARCHITECTURE.md) |
| `database is locked` | 并发写冲突 | 已配 `busy_timeout=30000`；仍报错则降 worker 数 |
| push 返回 500 | 事务内抛异常 → 整批回滚 | 查服务日志；历史上出现过墓碑违反 NOT NULL（已修） |
| 客户端拉不到数据 | `cursor` 没推进 | 确认 `has_more=true` 时客户端在继续拉；查 `sync_pulls` |
| 前端显示"能透看到后面的字" | 毛玻璃失效 | 检查外壳是否被加了 `will-change: transform` / `translateZ` |
| 数据库文件删不掉（Windows） | `init_db(force=True)` 未先关连接 | 用独立进程执行重建 |
