# -*- coding: utf-8 -*-
"""stats.py — 标注链路的聚合视图（供 /settings 的「数据采集与标注」区块与 API 使用）。

只读，不改任何状态。所有数字一次连接算完，避免多次开库。
统计口径：
  · 会话 = sessions 表行数（每场训练一份 raw_*.json）
  · 标注 = annotations 表行数，按 (annotator, source, status) 拆开：
      - 人工确认   annotator != 'heuristic' 且 status='confirmed'
      - 待纠错     annotator='heuristic' 且 source='auto' 且 status='proposed'
      - 已认可     annotator='heuristic' 且 status='confirmed'（人工确认了预标注）
      - 误检已删   annotator='heuristic' 且 status='rejected'
"""
from .db import RAW_DIR, VIDEO_DIR, get_conn  # noqa: F401  (RAW_DIR/VIDEO_DIR 供外部引用)

# 采集 → 标注 → 训练 → 分析 四段链路（静态描述，与 datasources.PIPELINE 衔接）
STAGES = [
    {'step': '01', 'key': 'capture', 'icon': 'watch',
     'title': '手表采集',
     'body': 'Apple Watch 以 800Hz 采加速度、200Hz 采设备运动，'
             '经启发式检测器切出每拍窗口，落盘为 raw_<sessionId>.json。'},
    {'step': '02', 'key': 'upload', 'icon': 'cloud_upload',
     'title': '双通道上行',
     'body': '同一场会话同时发往标注库（原始样本 + 视频，供人工纠错）'
             '与分析库（结构化训练记录，供排行与分析）。'},
    {'step': '03', 'key': 'annotate', 'icon': 'draw',
     'title': '视频↔波形同步标注',
     'body': 'Watch 启发式结果自动预标注（暗色虚线）；标注者只做纠错与补漏，'
             '「你只纠错」模式把未更正的预标注直接当作训练标签。'},
    {'step': '04', 'key': 'train', 'icon': 'model_training',
     'title': '训练集导出',
     'body': '按冲击时刻聚类解析出最终标签，切 ±窗口生成 38 维 dataset.csv，'
             '交给 CoreML 训练管线。'},
]

# 标签集说明（与 schemas.LABELS_ALL 一致）
LABEL_NOTE = ('标注标签保留 8 类（正手/反手/发球/截击/切削/高压/挑高/放小球），'
              '而展示层只用 6 类 —— 标注阶段刻意留富标签以便难例分析，训练时再按需归并。')


def overview():
    """标注链路总览。空库时各计数为 0，不会抛错。"""
    conn = get_conn()
    try:
        q = conn.execute

        sessions = q('SELECT COUNT(*) c FROM sessions').fetchone()['c']
        with_video = q('SELECT COUNT(*) c FROM sessions '
                       'WHERE video_path IS NOT NULL').fetchone()['c']

        total_anno = q('SELECT COUNT(*) c FROM annotations').fetchone()['c']

        human = q("SELECT COUNT(*) c FROM annotations "
                  "WHERE annotator<>'heuristic' AND status='confirmed'").fetchone()['c']
        proposed = q("SELECT COUNT(*) c FROM annotations "
                     "WHERE annotator='heuristic' AND source='auto' "
                     "AND status='proposed'").fetchone()['c']
        accepted = q("SELECT COUNT(*) c FROM annotations "
                     "WHERE annotator='heuristic' AND status='confirmed'").fetchone()['c']
        rejected = q("SELECT COUNT(*) c FROM annotations "
                     "WHERE annotator='heuristic' AND status='rejected'").fetchone()['c']

        annotators = q("SELECT COUNT(DISTINCT annotator) c FROM annotations "
                       "WHERE annotator<>'heuristic'").fetchone()['c']

        # 已经有人工介入的会话（有任意人工标注行）
        touched = q("SELECT COUNT(DISTINCT session_id) c FROM annotations "
                    "WHERE annotator<>'heuristic'").fetchone()['c']

        total_dur = q('SELECT COALESCE(SUM(duration),0) s FROM sessions').fetchone()['s'] or 0

        # 被人工纠错（判为硬例）的比例 —— 难例挖掘的关键信号
        heuristic_total = proposed + accepted + rejected
        hard_rate = (round(rejected * 100.0 / heuristic_total, 1)
                     if heuristic_total else None)

        return {
            'sessions': sessions,
            'with_video': with_video,
            'sessions_touched': touched,
            'sessions_pending': max(0, sessions - touched),
            'total_annotations': total_anno,
            'human_confirmed': human,
            'heuristic_proposed': proposed,
            'heuristic_accepted': accepted,
            'heuristic_rejected': rejected,
            'heuristic_total': heuristic_total,
            'hard_rate': hard_rate,
            'annotators': annotators,
            'total_duration_sec': int(total_dur),
            'labels_note': LABEL_NOTE,
            'stages': STAGES,
            'empty': sessions == 0,
        }
    finally:
        conn.close()


def _fmt_dur(sec):
    """秒数 → 「1 时 05 分 / 12 分 30 秒 / 45 秒」。

    不能简单用 //60：30 秒的会话会显示成「0 分」，读不通。
    """
    if not sec:
        return '—'
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return '%d 时 %02d 分' % (h, m)
    if m:
        return ('%d 分 %d 秒' % (m, s)) if s else ('%d 分' % m)
    return '%d 秒' % s


def session_rows(limit=12):
    """会话清单（工作台与设置页共用）。按创建时间倒序。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            'SELECT s.id, s.wrist, s.started_at, s.ended_at, s.duration, s.player_id,'
            '       s.video_path,'
            '       (SELECT COUNT(*) FROM annotations a WHERE a.session_id=s.id) AS n_total,'
            '       (SELECT COUNT(*) FROM annotations a WHERE a.session_id=s.id'
            '         AND a.annotator=\'heuristic\' AND a.source=\'auto\''
            '         AND a.status=\'proposed\') AS n_proposed,'
            '       (SELECT COUNT(*) FROM annotations a WHERE a.session_id=s.id'
            '         AND a.annotator<>\'heuristic\') AS n_human'
            '  FROM sessions s ORDER BY s.created_at DESC LIMIT ?',
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                'id': r['id'],
                'wrist': r['wrist'] or '—',
                'started_at': r['started_at'],
                'duration': r['duration'],
                'duration_label': _fmt_dur(r['duration']),
                'player_id': r['player_id'],
                'has_video': bool(r['video_path']),
                'n_total': r['n_total'],
                'n_proposed': r['n_proposed'],
                'n_human': r['n_human'],
                'progress': (round(r['n_human'] * 100.0 / r['n_total'], 0)
                             if r['n_total'] else 0),
            })
        return out
    finally:
        conn.close()
