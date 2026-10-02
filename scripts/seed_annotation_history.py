# -*- coding: utf-8 -*-
"""seed_annotation_history.py — 往标注库灌**历史采集数据**（16 场，挂在 5 位学员名下）。

    # 1) 先看会写什么（默认 dry-run，不碰库）
    .venv/bin/python scripts/seed_annotation_history.py

    # 2) 真正写入
    .venv/bin/python scripts/seed_annotation_history.py --apply

    # 3) 完全不碰真实库（三个库/目录重定向到临时目录）
    NETPULSE_DB=/tmp/ann-hist/annotations.db \
    NETPULSE_ANNOTATION_DIR=/tmp/ann-hist \
    NETPULSE_RAW_DIR=/tmp/ann-hist/raw \
    .venv/bin/python scripts/seed_annotation_history.py --apply

为什么需要它
------------
`/annotation`（数据采集与标注）默认只有联调时手工上传的零散几场，会话清单看不出
「这是谁的训练、采了多少场」。本脚本按产品里 5 位真实学员（与 /users 的用户管理
同一批人，id 来自 server/seed.py 的 STUDENTS）造出他们的**历史采集记录**：

    张哲恒 5 场 · 李思源 4 场 · 陈雨菲 3 场 · 王浩然 2 场 · 赵明 2 场   = 16 场

每场都会：
  · 合成一份 raw 包（会话 + 逐拍 swing + 波形 samples）封存进 L0 原始层；
  · 写入标注库 sessions 行，`player_id` 指向学员 id（页面从而显示学员姓名）；
  · 逐拍写入启发式预标注（`heuristic/auto/proposed`）；部分场次叠加人工纠错，
    让「待纠错 / 人工标注数 / 一致率」不至于全 0 或全满。

⚠️ 造的是**合成数据**：波形是叠加正弦的伪信号；逐拍时刻是按会话时长均匀铺开的，
   不是真实击球时刻。别拿它当物理真值。
⚠️ 会话 id 一律以 `hist-` 开头，与 `seed_annotation_demo.py` 的 `demo-` 互不干扰。
⚠️ 本脚本**没有**删除入口：删数据永远由人来做（清理语句见文件末尾）。
"""
import argparse
import gzip
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from server import rawstore                                    # noqa: E402
from server.annotation.db import DATA_DIR, get_conn, init_db    # noqa: E402
from seed_annotation_demo import POOL, WRONG_PICK, make_samples  # noqa: E402

# 5 位学员：(标注库 player_id, 姓名, 场次, 持拍, 起始日期偏移天数)
# player_id 必须与 server/seed.py 的 `stu-%08d`(1001+i) 对齐，页面才能把
# 会话挂到 /users 里同一个人身上。
STUDENT_PLAN = [
    ('stu-00001001', '张哲恒', 5, 'right', 0),
    ('stu-00001002', '李思源', 4, 'right', 1),
    ('stu-00001003', '陈雨菲', 3, 'left', 2),
    ('stu-00001004', '王浩然', 2, 'right', 3),
    ('stu-00001005', '赵明', 2, 'right', 4),
]

# 波形存储率：采集端是 800Hz，但这里是**历史存档演示**。
# 5Hz 足够画波形，并且压缩后每场只占几百 KB；按 800Hz 存会造出几十 MB。
STORE_STEP = 160          # 800 / 160 = 5 Hz


def utc_iso(dt):
    return dt.replace(tzinfo=None).isoformat()


def human_window(n, rng, force=False):
    """给出「人工介入」的条号范围；空列表表示这场没人工介入。

    设计成：越早采集的场次越可能被审过 —— 符合「历史数据大多已标完、
    最近的还在队列里」的真实分布。`force=True`（该学员最早那场）必定被审阅，
    保证每位学员至少有一场能算出一致性评分。
    """
    if not force and rng.random() < 0.4:
        return []
    k = rng.randint(max(2, n // 6), max(3, n // 2))
    start = rng.randint(0, max(0, n - k))
    return list(range(start, start + k))


def build_session(student, idx, rng, is_oldest=False):
    """生成一场历史采集会话：raw 包 + 逐条标注。"""
    _pid, name, _n, wrist, _off = student
    sid = 'hist-S%s-%02d' % (student[0][-2:], idx + 1)

    dur = rng.choice([2400, 2700, 3000, 3300, 3600, 4200, 4800])
    n_swings = rng.randint(70, 220)

    # 击球时刻均匀铺在会话内，两侧各留一点空白
    impacts = sorted(round(5.0 + i * (dur - 12.0) / n_swings + rng.uniform(-0.6, 0.6), 3)
                     for i in range(n_swings))
    swings = []
    for i, t in enumerate(impacts):
        swings.append({'type': POOL[i % len(POOL)], 'impactTime': t,
                       'confidence': round(rng.uniform(0.58, 0.97), 3)})

    # 采集时间：从「今天」往前铺，同一学员的场次依次更早
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    started = base - timedelta(days=idx * rng.choice([2, 3, 4]) + rng.randint(0, 1),
                               hours=rng.choice([9, 16, 19]))
    raw = {
        'session': {
            'id': sid,
            'wrist': wrist,
            'playerId': student[0],
            'playerName': name,
            'startedAt': utc_iso(started),
            'endedAt': utc_iso(started + timedelta(seconds=dur)),
            'duration': dur,
            'swings': swings,
        },
        'samples': make_samples(dur, step=STORE_STEP),
    }

    # 标注行：(impact_time, label, annotator, source, status, confidence)
    rows = [(t, sw['type'], 'heuristic', 'auto', 'proposed', sw['confidence'])
            for t, sw in zip(impacts, swings)]

    win = human_window(n_swings, rng, force=is_oldest)
    annotator = rng.choice(['小林', '老王', '阿May'])
    for j, i in enumerate(win):
        mode = rng.random()
        if mode < 0.7:              # 认可预标注
            t, lb, _a, _s, _st, c = rows[i]
            rows[i] = (t, lb, 'heuristic', 'auto', 'confirmed', c)
            rows.append((impacts[i], lb, annotator, 'human', 'confirmed', None))
        elif mode < 0.92:           # 纠错改标
            t, lb, _a, _s, _st, c = rows[i]
            new_lb = WRONG_PICK.get(lb, 'forehand')
            rows[i] = (t, lb, 'heuristic', 'auto', 'rejected', c)
            rows.append((impacts[i], new_lb, annotator, 'human', 'confirmed', None))
        else:                       # 判为误检，直接删掉预标注
            t, lb, _a, _s, _st, c = rows[i]
            rows[i] = (t, lb, 'heuristic', 'auto', 'rejected', c)
    return sid, wrist, name, raw, rows, started, annotator if win else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='真正写库；不给这个参数只打印将要做什么（dry-run）')
    ap.add_argument('--seed', type=int, default=20261002)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    print('标注库： %s' % (os.environ.get('NETPULSE_DB')
                          or '<默认 var/annotation/annotations.db>'))
    print('数据目录：%s' % DATA_DIR)
    print('模式：   %s' % ('APPLY（会写库）' if args.apply else 'dry-run（不写库）'))
    print()

    built = []
    for student in STUDENT_PLAN:
        for i in range(student[2]):
            built.append(build_session(student, i, rng,
                                       is_oldest=(i == student[2] - 1)))

    # ---- 预览 ----------------------------------------------------------
    per = {}
    for sid, wrist, name, raw, rows, started, ann in built:
        props = sum(1 for r in rows if r[4] == 'proposed')
        rej = sum(1 for r in rows if r[4] == 'rejected')
        hum = sum(1 for r in rows if r[3] == 'human')
        per.setdefault(name, []).append(
            (sid, raw['session']['duration'], len(rows), props, rej, hum, ann))
    for student in STUDENT_PLAN:
        name = student[1]
        print('%s（%d 场）' % (name, student[2]))
        for sid, dur, n, props, rej, hum, ann in per.get(name, []):
            print('   %-14s 时长 %4ds  标注 %3d 行｜待纠错 %3d / 误检 %2d / 人工 %3d  %s'
                  % (sid, dur, n, props, rej, hum, ('标注者 ' + ann) if ann else '未介入'))
    print()
    print('合计 %d 场会话。' % len(built))
    if not args.apply:
        print('以上为预览。确认无误后加 --apply 真正写入。')
        return 0

    # ---- 写盘 ----------------------------------------------------------
    # 与上传接口同一条路：先封存 L0（只追加、按 sha256 内容寻址），
    # 会话行只保存 raw_id 指针。这样历史数据与真实上传的数据形态完全一致。
    conn = get_conn()
    init_db(conn)
    rawstore.init_db()
    now = utc_iso(datetime.now(timezone.utc))
    for sid, wrist, name, raw, rows, started, _ann in built:
        # 压成 gzip 再归档：rawstore 会识别 gzip magic，落文件但不内联，
        # 16 场也就几 MB（不压缩会是几十 MB）。
        blob = gzip.compress(json.dumps(raw, ensure_ascii=False,
                                        separators=(',', ':')).encode('utf-8'), 6)
        archived = rawstore.archive(blob, session_id=sid, source='seed_history',
                                    filename='raw_%s.json.gz' % sid)
        conn.execute(
            'INSERT OR REPLACE INTO sessions'
            '(id, wrist, started_at, ended_at, duration, player_id,'
            ' raw_id, raw_path, video_path, created_at)'
            ' VALUES(?,?,?,?,?,?,?,?,?,?)',
            (sid, wrist, raw['session']['startedAt'], raw['session']['endedAt'],
             raw['session']['duration'], raw['session']['playerId'],
             archived['raw_id'], archived['file_path'], None, now))
        # 幂等：同会话重跑先清掉自己的标注，避免堆叠
        conn.execute('DELETE FROM annotations WHERE session_id=?', (sid,))
        for t, lb, a, src, st, cf in rows:
            conn.execute(
                'INSERT INTO annotations(session_id, impact_time, label, confidence,'
                ' annotator, source, status, created_at, updated_at)'
                ' VALUES(?,?,?,?,?,?,?,?,?)',
                (sid, t, lb, cf, a, src, st, now, now))
        print('写入 %-14s %s → L0 raw_id=%s（%d 条标注，%d 字节 gzip）'
              % (sid, name, archived['raw_id'][:24], len(rows),
                 archived['byte_size']))
    conn.commit()
    conn.close()
    print()
    print('完成。打开 /annotation 的「会话清单」即可看到这 16 场历史采集数据。')
    print('清理：DELETE FROM annotations WHERE session_id LIKE "hist-%%";'
          ' 再 DELETE FROM sessions WHERE id LIKE "hist-%%";')
    return 0


if __name__ == '__main__':
    sys.exit(main())
