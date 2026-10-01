# contract/ — 三端共享契约（**唯一真源**）

```
TennisContract.swift   ← 改这里。两个 App 里的副本是同步产物，不要手改。
```

## 三步

```bash
# 改完真源（记得递增文件头 version）
bash scripts/sync_contract.sh                     # 真源 → ATennis / WatchTennis
bash scripts/sync_contract.sh --check             # 只查漂移（sha256）
.venv/bin/python scripts/verify_contract.py       # 38 项对账：契约 ↔ 后端
```

## 里面有什么

| 类别 | 类型 |
|---|---|
| 上行 DTO | `MatchSession`、`MatchSwing`、`SessionUploadRequest`、`SessionUploadResponse` |
| 下行 DTO | `SessionListResponse`、`SessionListItem`、`StrokeCounts` |
| 通道消息 | `WatchToPhoneMessage`、`PhoneToWatchMessage` |
| 枚举 | `SwingType`（6 + `unknown`）、`SessionType`、`Wrist` |
| 常量 | 接口路径、请求头名、超时/批大小、`Biomechanics.racketRadiusMeters` |

## 为什么副本要跟着各自仓库提交

三端是**三个独立 git 仓库**。跨仓库引用文件会让"单独 clone 一个 App 仓库"无法编译，
所以副本随各自仓库提交，靠 `--check` 的 sha256 比对防漂移。

## 这是"同一个文件进两个 target"

契约文件同时被 Watch 与 Phone 两个 target 编译，因此：

- **任何一端自行定义一个同名的契约类型 → 立刻编译冲突**
  （`SwingType` 就是这么被抓出来的重复定义）；
- 一次改动必须**三个仓库一起提交**，漏一个那端就编译不过。

配套文档：[docs/CONTRACT.md](../docs/CONTRACT.md)（字段表与变更流程）·
[docs/OVERVIEW.md](../docs/OVERVIEW.md)（术语与枚举口径）
