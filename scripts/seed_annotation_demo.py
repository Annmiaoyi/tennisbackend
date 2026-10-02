# -*- coding: utf-8 -*-
"""seed_annotation_demo.py — 往标注库灌 3 场**合成**演示会话，便于把 /annotation 看全。

    # 1) 先看会写什么（默认 dry-run，不碰库）
    .venv/bin/python scripts/seed_annotation_demo.py

    # 2) 真正写入（会往标注库里加 3 行会话 + 若干标注）
    .venv/bin/python scripts/seed_annotation_demo.py --apply

    # 3) 想完全不碰真实库：把三个库/目录都重定向到临时目录
    NETPULSE_DB=/tmp/ann-demo/annotations.db \
    NETPULSE_ANNOTATION_DIR=/tmp/ann-demo \
    NETPULSE_RAW_DIR=/tmp/ann-demo/raw \
    .venv/bin/python scripts/seed_annotation_demo.py --apply

为什么要它：标注库默认为空，/annotation 的「标注进度」表、难例率、一致性评分
都会落在空态上，只有一个 0 可看；联调工作台也需要一份 raw_*.json。
本脚本把三种典型介入程度各造一场，正好覆盖状态机的四种取值：

  · demo-S1-untouched  18 条预标注，无人介入          → 全 proposed，进度 0%
  · demo-S2-partial    20 条预标注 + 1 位标注者纠错    → proposed/confirmed/rejected 齐
  · demo-S3-cross      16 条预标注 + 2 位标注者交叉    → 一致性评分与 Kappa 有值

**造的是合成数据**：波形是叠加正弦的伪信号，别拿它当物理真值。
会话 id 一律以 `demo-` 开头，方便清理。
本脚本**没有**删除入口：删数据永远由人来做。
"""
import argparse
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import rawstore                              # noqa: E402
from server.annotation.db import DATA_DIR, get_conn, init_db   # noqa: E402

# (会话 id, 预标注条数, 标注者→[(与第几条预标注同标签认可, 纠错改标的条号), 显式删的条号])
SESSIONS = [
    dict(sid='demo-S1-untouched', wrist='right', dur=300, n_auto=18,
         annotators=[]),
    dict(sid='demo-S2-partial', wrist='right', dur=360, n_auto=20,
         annotators=[dict(name='小林', confirm=[0, 1, 2, 3, 4, 5, 12, 13],
                          correct=[6, 7, 8], drop=[9, 10])]),
    dict(sid='demo-S3-cross', wrist='left', dur=270, n_auto=16,
         annotators=[dict(name='小林', confirm=[0, 1, 2, 3, 4, 11],
                          correct=[5, 6], drop=[]),
                     dict(name='老王', confirm=[0, 1, 2, 3, 5, 11, 12],
                          correct=[4, 7, 8], drop=[13])]),
]

# 预标注用的类别池：故意混入展示层用不到的富标签，方便看「8 类 ≠ 6 类」
POOL = ['forehand', 'backhand', 'forehand', 'serve', 'backhand', 'volley',
        'forehand', 'slice', 'serve', 'backhand', 'smash', 'forehand',
        'lob', 'backhand', 'drop', 'serve', 'forehand', 'volley']
# 纠错时改成什么（与预标注刻意不同）
WRONG_PICK = {'forehand': 'backhand', 'backhand': 'slice', 'volley': 'smash',
              'slice': 'drop', 'serve': 'lob', 'smash': 'volley',
              'lob': 'serve', 'drop': 'slice'}


def utc_iso(dt):
    return dt.replace(tzinfo=None).isoformat()


def make_samples(dur_sec, hz=800, step=16):
    """合成三轴加速度 + 三轴角速度。

    叠加两个正弦 + 白噪声 —— 只是为了让波形 Canvas 有东西可画，
    不模拟任何真实力学。

    存储率取 `hz/step = 50Hz`（而非采集时的 800Hz）：这是**演示数据**，
    按 800Hz 存一场 6 分钟会话会造出 30 万点、十几 MB 的 JSON。
    50Hz 足够画波形，也仍高于导出窗口所需的密度
    （每 0.15s 窗口至少 4 个采样点 ≈ 27Hz）。
    """
    out = []
    n = int(dur_sec * hz)
    for i in range(0, n, step):
        t = i / hz
        out.append({
            't': round(t, 4),
            'ax': round(0.35 * math.sin(2 * math.pi * 1.7 * t) + random.uniform(-.02, .02), 4),
            'ay': round(0.28 * math.cos(2 * math.pi * 2.3 * t) + random.uniform(-.02, .02), 4),
            'az': round(1.0 + 0.12 * math.sin(2 * math.pi * 0.9 * t), 4),
            'rx': round(1.6 * math.sin(2 * math.pi * 2.9 * t), 4),
            'ry': round(0.9 * math.cos(2 * math.pi * 1.3 * t), 4),
            'rz': round(0.6 * math.sin(2 * math.pi * 3.7 * t), 4),
        })
    return out


def build_session(spec, rng):
    """生成一场会话的 raw 包 + 逐条标注（含人工介入后的状态）。"""
    n = spec['n_auto']
    # 击球时刻均匀铺在会话内，带一点抖动
    impacts = sorted(round(2.0 + i * (spec['dur'] - 6.0) / n + rng.uniform(-0.3, 0.3), 3)
                     for i in range(n))
    swings = []
    for i, t in enumerate(impacts):
        swings.append({'type': POOL[i % len(POOL)], 'impactTime': t,
                       'confidence': round(rng.uniform(0.62, 0.97), 3)})
    started = datetime(2026, 9, 28, 9, 30, tzinfo=timezone.utc) + timedelta(days=rng.randint(0, 3))
    raw = {
        'session': {
            'id': spec['sid'],
            'wrist': spec['wrist'],
            'startedAt': utc_iso(started),
            'endedAt': utc_iso(started + timedelta(seconds=spec['dur'])),
            'duration': spec['dur'],
            'swings': swings,
        },
        'samples': make_samples(spec['dur']),
    }

    # 标注行：(impact_time, label, annotator, source, status)
    rows = [(t, sw['type'], 'heuristic', 'auto', 'proposed',
             sw['confidence']) for t, sw in zip(impacts, swings)]

    def _mark(idx, status):
        t, lb, a, s, _st, c = rows[idx]
        rows[idx] = (t, lb, a, s, status, c)

    for an in spec['annotators']:
        nm = an['name']
        for idx in an['confirm']:                      # 认可 → 预标注置 confirmed
            if idx >= n:
                continue
            _mark(idx, 'confirmed')
            rows.append((impacts[idx], rows[idx][1], nm, 'human', 'confirmed', None))
        for idx in an['correct']:                      # 纠错 → 预标注置 rejected
            if idx >= n:
                continue
            _mark(idx, 'rejected')
            rows.append((impacts[idx], WRONG_PICK.get(rows[idx][1], 'forehand'),
                         nm, 'human', 'confirmed', None))
        for idx in an['drop']:                         # 显式删误检 → rejected，不写人工行
            if idx >= n:
                continue
            _mark(idx, 'rejected')
    return raw, rows, started


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='真正写库；不给这个参数只打印将要做什么（dry-run）')
    ap.add_argument('--seed', type=int, default=20261001)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    print('标注库： %s' % (os.environ.get('NETPULSE_DB')
                          or '<默认 var/annotation/annotations.db>'))
    print('数据目录：%s' % DATA_DIR)
    print('模式：   %s' % ('APPLY（会写库）' if args.apply else 'dry-run（不写库）'))
    print()

    built = [(s, *build_session(s, rng)) for s in SESSIONS]

    # ---- 预览 ----------------------------------------------------------
    for spec, raw, rows, _started in built:
        props = sum(1 for r in rows if r[4] == 'proposed')
        conf = sum(1 for r in rows if r[4] == 'confirmed')
        rej = sum(1 for r in rows if r[4] == 'rejected')
        human = sum(1 for r in rows if r[3] == 'human')
        names = sorted({r[2] for r in rows if r[3] == 'human'})
        print('%s  dur=%ds  采样%d点' % (spec['sid'], spec['dur'], len(raw['samples'])))
        print('   标注 %2d 行：proposed %2d / confirmed %2d / rejected %2d｜人工 %2d 行'
              '（%s）' % (len(rows), props, conf, rej, human, '、'.join(names) or '无'))
    print()
    if not args.apply:
        print('以上为预览。确认无误后加 --apply 真正写入。')
        return 0

    # ---- 写盘 ----------------------------------------------------------
    # 走**与上传接口同一条路**：先封存进 L0 原始层（只追加、按 sha256 内容寻址），
    # 会话行只保存 raw_id 指针。这样演示数据与真实上传的数据形态完全一致，
    # 波形接口与训练集导出都会照着同一条解析链找到它。
    conn = get_conn()
    init_db(conn)
    # L0 原始层是**另一个库**（var/raw/acemate_raw.db），首次使用要单独建表。
    # 少了这一步，archive() 会报 "no such table: raw_sessions"。
    rawstore.init_db()
    now = utc_iso(datetime.now(timezone.utc))
    for spec, raw, rows, started in built:
        archived = rawstore.archive(raw, session_id=spec['sid'],
                                    source='seed_demo',
                                    filename='raw_%s.json' % spec['sid'])
        conn.execute(
            'INSERT OR REPLACE INTO sessions'
            '(id, wrist, started_at, ended_at, duration, player_id,'
            ' raw_id, raw_path, video_path, created_at)'
            ' VALUES(?,?,?,?,?,?,?,?,?,?)',
            (spec['sid'], spec['wrist'], raw['session']['startedAt'],
             raw['session']['endedAt'], spec['dur'], 'demo-player',
             archived['raw_id'], archived['file_path'], None, now))
        # 幂等：同会话重跑先清空自己的标注，避免堆叠
        conn.execute('DELETE FROM annotations WHERE session_id=?', (spec['sid'],))
        for t, lb, ann, src, st, cf in rows:
            conn.execute(
                'INSERT INTO annotations(session_id, impact_time, label, confidence,'
                ' annotator, source, status, created_at, updated_at)'
                ' VALUES(?,?,?,?,?,?,?,?,?)',
                (spec['sid'], t, lb, cf, ann, src, st, now, now))
        print('写入 %s → L0 raw_id=%s（%d 条标注）%s'
              % (spec['sid'], archived['raw_id'][:24], len(rows),
                 '  [命中已有字节，未重复落盘]' if archived['status'] == 'duplicate' else ''))
    conn.commit()
    conn.close()
    print()
    print('完成。打开 /annotation 即可看到标注进度、难例率与一致性评分。')
    print('清理：DELETE FROM annotations WHERE session_id LIKE "demo-%%";'
          ' 再 DELETE FROM sessions WHERE id LIKE "demo-%%";')
    return 0


if __name__ == '__main__':
    sys.exit(main())
