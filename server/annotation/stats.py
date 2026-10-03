# -*- coding: utf-8 -*-
"""stats.py — 标注链路的聚合视图 + 标注规范（供 /annotation 页面与 API 使用）。

2026-10-01：标注相关内容已从 /settings 收敛到 /annotation 单一出口，
本文件的 SPEC_* 常量就是页面上那几块「逻辑与须知」的唯一真源。

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

import json
import re
import unicodedata

# 训练形式 / 场地类型的中文名。与 server/analytics.py 里的那份保持一致 ——
# 同一个枚举在两个页面必须显示同一个中文名，否则「专项练习」和「截击专项」
# 会被当成两种东西。
from ..analytics import COURT_TYPE_LABEL, SESSION_TYPE_LABEL  # noqa: E402
# 时间区间口径也复用 /training 那一份（`resolve_range` + `_range_clause`）：
# 两个页面筛「最近 7 天」必须是同一个边界，否则同一批数据在两边对不上。
# `_range_clause` 带下划线是因为它本来只服务 analytics 内部；这里主动复用它，
# 而不是在 annotation 侧再写一遍 date(started_at) 的比较。
from ..analytics import _range_clause  # noqa: E402

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


# --------------------------------------------------------------------------- #
# 标注规范（2026-10-01 全量对照 api.py / converter.py / db.py 后成文）
# --------------------------------------------------------------------------- #
# ⚠️ 本站同时跑着两条链路，极易混淆。上面 STAGES 描述的是**训练链**；
#    分析链在 server/datasources.py 的 PIPELINE。两者共用同一批手表传感器数据，
#    但产物与消费者完全不同。
CHAIN_CONTRAST = [
    {'key': 'analysis', 'name': '分析链', 'icon': 'insights', 'tone': 'text-primary-fixed',
     'route': 'Watch → iPhone 识别 → 质量门禁 → 同步队列 → 后端聚合',
     'product': 'training_sessions + stroke_records',
     'consumer': '本管理站的训练分析与排行；App 的训练历史页',
     'where': 'server/datasources.py · PIPELINE'},
    {'key': 'training', 'name': '训练链（本页）', 'icon': 'model_training', 'tone': 'text-secondary',
     'route': '手表采集原始会话 → 双通道上行 → 视频↔波形人工标注 → 导出训练集',
     'product': 'annotation.json + 38 维 dataset.csv',
     'consumer': 'CoreML 训练管线 —— 目的是让识别算法变准，不是给人看',
     'where': '本文件 · STAGES'},
]

# 标注记录的状态机。
# ⚠️ 现状由 (annotator, source, status) **三元组**决定，不能只看 status：
#    被纠错的那条记录并没有变成"人工写的"，而是原启发式行被就地改状态。
STATUS_MACHINE = [
    {'annotator': 'heuristic', 'source': 'auto', 'status': 'proposed',
     'label': '待审阅预标注', 'tone': 'text-tertiary-fixed-dim', 'icon': 'auto_fix_high',
     'body': '上传 raw_*.json 时由 Watch 的启发式检测器逐拍自动写入，界面上是暗色虚线。'
             '**标注者不处理它不会丢失** —— 导出时若无人反驳，它就直接成为训练标签。'},
    {'annotator': 'heuristic', 'source': 'auto', 'status': 'confirmed',
     'label': '机器判断被认可', 'tone': 'text-primary-fixed', 'icon': 'verified',
     'body': '标注者的打击点与某条预标注在 ±0.3s 内重合、且标签相同 → 该预标注自动记为 '
             'confirmed。不需要逐条点"确认"，标对了就会自动挂账。'},
    {'annotator': 'heuristic', 'source': 'auto', 'status': 'rejected',
     'label': '纠错 / 误检剔除', 'tone': 'text-error', 'icon': 'block',
     'body': '两种来源：① 同一打击点但标签不同（标注者纠错）；'
             '② 标注者显式删掉误检（挥手、捡球）。'
             '这一态是**难例挖掘最关键的信号** —— rejected 比例高，说明模型正是在这些场景失准。'},
    {'annotator': '标注者名', 'source': 'human', 'status': 'confirmed',
     'label': '人工新增 / 改标', 'tone': 'text-secondary', 'icon': 'draw',
     'body': '标注者手动打上的击球（含新增与更正）。导出时同一时刻若有多位标注者，'
             '取多数投票；单人场景即该标签本身。'},
]

# 标注规则（页面「标注逻辑」区块，逐条对应上面的状态机）
RULES = [
    {'step': '01', 'icon': 'edit_off', 'tone': 'text-primary-fixed',
     'title': '「你只纠错」——标错的，不标对的',
     'body': '预标注已经带着 Watch 的判断落在库里。标注者只处理两类：改标签、删误检。'
             '凡是没被动过的预标注，导出时按原标签采纳。'
             '这是本工作台的核心约定，也是能把标注成本压下来的原因。'},
    {'step': '02', 'icon': 'label', 'tone': 'text-secondary',
     'title': '标签空间 8 类 ≠ 展示 6 类',
     'body': '标注保留 8 类（多出高压 / 挑高 / 放小球），展示层只用 6 类。'
             '这是**刻意为之**：标注阶段留富标签便于难例分析，训练时再按需归并；'
             '两端都用 8 类反而会让分类器去猜它区分不了的类别。'},
    {'step': '03', 'icon': 'person_check', 'tone': 'text-tertiary-fixed-dim',
     'title': '保存按「标注者」维度替换，多人可并存',
     'body': '一次保存会先删掉**该标注者自己**上一轮的全部记录再写入，'
             '别人的标注完全不受影响 —— 这是多人交叉标注与一致性评分的前提。'
             '代价是标注者名字就是分组键，必须稳定。'},
    {'step': '04', 'icon': 'auto_delete', 'tone': 'text-error',
     'title': '难例会自动挂账，不用手动标记',
     'body': '保存时系统会找出与你的打击点 ±0.3s 重合的未审阅预标注：'
             '标签相同 → 自动置 confirmed；标签不同 → 自动置 rejected。'
             '被删掉的误检同样就地置 rejected。所以"硬例"是标出来的副产品，不需要额外操作。'},
    {'step': '05', 'icon': 'model_training', 'tone': 'text-primary-fixed',
     'title': '导出时按冲击时刻聚类成簇再取标签',
     'body': '以 0.3s 容差把同一击球的各路标注聚成一簇：簇内有人工标注则取多数投票，'
             '否则采用未审阅的预标注，只剩 rejected 的簇直接跳过。'
             '再对每簇切 [−0.05s, +0.10s] 窗口算 38 维特征'
             '（6 通道 × 6 个统计量 + 加速度/角速度两个模均值），'
             '窗口内不足 4 个采样点的样本丢弃。'},
]

# 标注注意事项（逐条来自真实踩坑，页面「作业须知」区块）
NOTES = [
    {'tone': 'text-error', 'icon': 'error', 'title': '① 「不点」等于「默认认可」——这是最大的风险',
     'body': '未审阅的预标注会被直接当作训练标签导出。如果你只标了一半就交棒，'
             '剩下那一半**没有任何人工确认过**的数据也会进训练集。'
             '交付前请确认页面上的「待纠错」计数归零，而不是只看「人工标注数」好看。'},
    {'tone': 'text-tertiary-fixed-dim', 'icon': 'badge', 'title': '② 标注者名字必须稳定',
     'body': '名字是保存替换与一致性统计的分组键。同一个人换了写法（"老王" → "wang"），'
             '系统会当成两位不同的标注者：一致性评分里凭空多出一对从未交叉标注的人，'
             '「已介入会话数」也会失真。团队内先约定好固定标识。'},
    {'tone': 'text-error', 'icon': 'front_hand', 'title': '③ 腕别错了，整场标注白做',
     'body': '会话上的 wrist 决定波形符号与正/反手判定。左手戴表却记成右手时，'
             '波形与视频互为镜像，正反手全反 —— 而且这种错误在标注界面上'
             '**看不出任何异常**，数据本身完全自洽。上传前先确认这一项。'},
    {'tone': 'text-secondary', 'icon': 'videocam', 'title': '④ 视频是可选项，但强烈建议带上',
     'body': '没有视频也能只凭波形标注，但纠错准确率会明显下降（尤其在切削/截击这类'
             '波形相似的类别上）。带视频的会话在界面上会标「有视频」徽章，优先处理它们。'},
    {'tone': 'text-primary-fixed', 'icon': 'timeline', 'title': '⑤ 视频与波形必须共用同一会话内时间轴',
     'body': '两侧共用「距会话起点」的秒轴。若录制的视频不是从会话起点开始（例如中途才开机），'
             '整段标注会系统性偏移，导出时会切错窗口取到相邻击球的波形 —— '
             '产出的样本标签看似正确，特征却是别人的。'},
    {'tone': 'text-outline', 'icon': 'content_copy', 'title': '⑥ 同一击球不要标两次',
     'body': '±0.3s 内的多条人工标注会被聚成同一簇，导出时只按多数投票出一条样本。'
             '重复标注不但不会增加样本，还会稀释多数投票的结果，让最终标签更不可控。'},
    {'tone': 'text-tertiary-fixed-dim', 'icon': 'database', 'title': '⑦ 原始数据只有一份，标注可重放',
     'body': '上传的 raw_*.json 会先封存进 L0 原始层（按 sha256 内容寻址、只追加不修改），'
             '标注层只保存一个指向它的 raw_id。重复上传同一份原始包不会产生第二份副本，'
             '识别逻辑将来改进了也可以直接重算，不需要重新采集。'},
    {'tone': 'text-secondary', 'icon': 'balance', 'title': '⑧ 一致性看 Kappa，不看一致率',
     'body': '两人一致率高可能只是因为某类球占了绝大多数（随机猜也能中）。'
             '工作台同时给出 Cohen\'s Kappa：κ<0.4 说明标注口径没对齐，'
             '此时不该急着扩大标注量，应先回到标注规范统一判读标准。'},
]


def annotation_spec():
    """页面「采集与标注逻辑 / 须知」区块的全部数据（唯一真源）。"""
    from .schemas import LABELS_ALL, LABEL_CN
    return {
        'chains': CHAIN_CONTRAST,
        'stages': STAGES,
        'status_machine': STATUS_MACHINE,
        'rules': RULES,
        'notes': NOTES,
        'labels': [{'key': k, 'cn': LABEL_CN.get(k, k),
                    'baseline': k in LABELS_ALL[:5]} for k in LABELS_ALL],
        'label_note': LABEL_NOTE,
    }


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


def _student_names():
    """`player_id` → 学员姓名。

    标注库（采集/标注侧）与分析库（`var/acemate.db` 的 students 表）是**两个库**，
    这里只做一次轻量映射，目的是让会话清单能显示「这场是谁的」。
    任何异常（分析库还没建、表不存在、被隔离测试重定向）都退化成空表 ——
    页面回退显示 player_id 原值，绝不因为跨库取名字把 /annotation 搞成 500。
    """
    try:
        from .. import db as analysis_db
        rows = analysis_db.query('SELECT id, name FROM students')
        return {r['id']: r['name'] for r in rows if r['id']}
    except Exception:
        return {}


def session_rows(limit=12):
    """会话清单（工作台与设置页共用）。按创建时间倒序。"""
    names = _student_names()
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
            pid = r['player_id']
            out.append({
                'id': r['id'],
                'wrist': r['wrist'] or '—',
                'started_at': r['started_at'],
                'duration': r['duration'],
                'duration_label': _fmt_dur(r['duration']),
                'player_id': pid,
                # 学员姓名；映射不到就原样回退成 player_id（演示会话等）
                'player_name': names.get(pid) or pid or '—',
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


# --------------------------------------------------------------------------- #
# 历史采集元数据（mock 批次）
# --------------------------------------------------------------------------- #
# 目的：把 `seed_annotation_history.py` 造出来的那批场次，**逐场逐字段**摊开，
# 让人一眼看到「每个数字是多少」，同时回答「下一批采集该采哪些字段、
# 哪些字段是期望有值的」。
#
# 布局：**字段为列、场次为行**（2026-10-03 由「字段为行」转置而来）。
#   这样「一场训练 = 一行、一个字段 = 一列」与「一条记录一行」的直觉一致，
#   也才容纳得下「按学员 + 时间范围筛选」—— 筛选筛的是**行**（场次），
#   列（字段）是稳定的口径，不该随筛选消失。
#   代价是列很多（33 列），所以表头纵向 sticky、左侧会话标识横向 sticky。
#   · 字段清单、必采标记、采集来源全部来自 server/datasources.py（唯一真源，
#     不在本页另写一份口径）；
#   · 覆盖数由 training_sessions / stroke_records 现算，**随筛选同步重算**，
#     不是写死的文案；
#   · 甜区 / 转速 / 落点 / 失误 / 制胜分等**硬件不可得**的字段不在此表 ——
#     它们已经从登记表移除，留档见 /settings#removed。
HIST_PREFIX = 'hist-'

# `artifact` 字段的落点，例如 'training_sessions.avg_hr' → ('training_sessions','avg_hr')
_ARTIFACT_RE = re.compile(r'^(training_sessions|stroke_records|students)\.([a-z_]+)')

# 每个字段在表格单元格里的呈现方式。没列到的走 _cell_default。
_CELL_FMT = {
    'duration_sec': lambda v: '%d 分' % round(v / 60.0),
    'started_at': lambda v: str(v)[5:16].replace('T', ' '),
    'ended_at': lambda v: str(v)[5:16].replace('T', ' '),
    'worn_wrist': lambda v: {'left': '左手', 'right': '右手'}.get(v, v),
    # 训练形式 / 场地类型在这里就必须转中文：页面上「drill」和「专项练习」
    # 会被当成两种东西，而 /training 显示的是后者。
    'session_type': lambda v: SESSION_TYPE_LABEL.get(v, v),
    'court_type': lambda v: COURT_TYPE_LABEL.get(v, v),
    # 心率区间在库里是一列 JSON。矩阵里要的是**五个数**，不是 JSON 文本 ——
    # 摊平成 zone1/zone2/…/zone5，一眼能比出各场强度分布。
    'hr_zone': lambda v: '/'.join(str(x) for x in _zone_list(v)) or str(v),
    'external_id': lambda v: str(v),
    'calories_kcal': lambda v: '%.0f' % v,
    'distance_km': lambda v: '%.2f' % v,
    'avg_speed_kmh': lambda v: '%.1f' % v,
    'peak_speed_kmh': lambda v: '%.1f' % v,
    'forehand_avg_kmh': lambda v: '%.1f' % v,
    'backhand_avg_kmh': lambda v: '%.1f' % v,
    'serve_avg_kmh': lambda v: '%.1f' % v,
    'serve_peak_kmh': lambda v: '%.1f' % v,
    'confidence': lambda v: '%.2f' % v,
    'anomaly': lambda v: '%d' % int(v or 0),
}

# 矩阵里每个场次列的宽度按 100px 设计。ASCII 字宽约 6.2px、CJK 约 12px，
# 所以裁剪按「显示宽度」算，CJK 记 2 个单位，预算 16 个单位。
# 超出部分裁掉并加省略号 —— 但**完整原值一定挂在 title 上**（见 _cell_pair），
# 悬停就能看到，不会因为窄列丢信息。
_CELL_BUDGET = 16


def _dw(s):
    """字符串的显示宽度：全角/CJK 记 2，其余记 1。"""
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in str(s))


def _clip(s, budget=_CELL_BUDGET):
    if _dw(s) <= budget:
        return s
    out, used = '', 0
    for c in str(s):
        w = 2 if unicodedata.east_asian_width(c) in 'WF' else 1
        if used + w > budget - 1:
            break
        out += c
        used += w
    return out + '…'


def _cell_default(v):
    if v is None:
        return '—'
    if isinstance(v, float):
        return '%.1f' % v
    return str(v)


def _cell(key, v):
    if v is None or v == '':
        return '—'
    fn = _CELL_FMT.get(key)
    return fn(v) if fn else _cell_default(v)


def _cell_pair(key, v):
    """矩阵单元格 = (显示值, 悬停原值)。

    显示值经过中文映射 / 单位化简 / 宽度裁剪；原值永远是库里那一列的内容，
    两者不同才挂 title（相同就不挂，免得悬停出一模一样的浮层）。
    """
    if v is None or v == '':
        return {'v': '—', 't': ''}
    disp = _cell(key, v)
    raw = v if isinstance(v, str) else str(v)
    shown = _clip(disp)
    # 与**裁剪后**的比较：被裁掉的那部分正是要靠 title 补回来的。
    return {'v': shown, 't': raw if raw != shown else ''}



def _field_plan():
    """登记表 → 本页「字段 × 场次」矩阵的行定义。

    每行的 level 决定它在表里的呈现：
      · session —— training_sessions 有对应列，逐场给值
      · stroke  —— stroke_records 有对应列，逐场给「样本数」
      · profile —— 落在 students（档案级），没有逐场值
      · none    —— derive / local / 待建列，压根不落库
    """
    from .. import datasources as ds
    out = []
    for r in ds.flat_rows():
        m = _ARTIFACT_RE.match(r.get('artifact') or '')
        table = m.group(1) if m else None
        col = m.group(2) if m else None
        if table in ('training_sessions', 'stroke_records'):
            level = 'session' if table == 'training_sessions' else 'stroke'
        elif table == 'students':
            level = 'profile'
        else:
            level = 'none'
        src = r.get('source') or '—'
        out.append({
            'no': r['no'], 'field': r['field'], 'key': r['key'],
            'unit': r.get('unit') or '—',
            'cat_key': r.get('cat_key') or '',
            'cat_label': r['cat_label'], 'cat_tone': r['cat_tone'],
            'cat_icon': r['cat_icon'],
            'group_name': r.get('group_name') or '',
            'required': bool(r.get('required')),
            'sync': r.get('sync'),
            'sync_label': r['sync_meta']['label'], 'sync_tone': r['sync_meta']['tone'],
            'source': src,
            # 「HealthKit · HKQuantityTypeIdentifierActiveEnergyBurned」这种全路径
            # 在表里放不下，只留框架名（' · ' 之前那截）；全路径与取数方法挂在
            # title 上，悬停即得。
            'source_short': src.split(' · ')[0] or '—',
            'acquire': (r.get('acquire') or '').strip(),
            'artifact': (r.get('artifact') or '').strip(),
            'level': level, 'col': col,
            'note': (r.get('pending') or '').strip(),
        })
    return out


# 「不落库 / 无逐场值」的行按性质分档。分档依据是 sync（采集形态），
# 不是 artifact 文案 —— 文案会改，sync 是登记表里的枚举。
_NO_VALUE_KINDS = [
    ('profile', '学员档案级', 'text-secondary', 'badge',
     '落在 students 表，按人一行 —— 有值，但**不是逐场的**。'
     '要按学员查，不在本表的列里。'),
    ('derive', '后端派生（不落库）', 'text-tertiary-fixed-dim', 'functions',
     '不单独存一列，由后端聚合时现算。因此**永远不会有逐场存储值** —— '
     '它出现在这里是为了说明「这个指标从哪来」，不是说要为它建列。'),
    ('local', '仅设备本地（不上行）', 'text-outline', 'smartphone',
     '隐私或体量原因不上行，只在手表/手机本地用。后端既收不到也存不下，'
     '**不要指望在库里看到它**。'),
    ('none', '口径已定义、尚未落地', 'text-error', 'construction',
     '登记表里有定义（采集端会产出），但后端还没有对应列或计算。'
     '要给它们留位置，就得先改 server/schema.sql。'),
]



def _hist_student_options(counts):
    """学员筛选框的候选：只列**本批真有场次**的学员。

    列一个 0 场的学员出来，点进去是空表，读者会以为数据丢了 —— 所以按
    「本批场次数 > 0」过滤，并把场次数挂在选项上。
    名单本身复用 `analytics.list_students()`，不在这里重写 SELECT ——
    「谁算一个学员、按什么排序」这个口径只该有一处。

    每个选项额外带 `search`（检索串：姓名 + 拼音全拼 + 拼音首字母 + 学员 id），
    供筛选框按拼音找人和前端即时过滤 —— 生成规则见 `server/pinyin.py`，
    这里只是把姓名与 id 拼起来，不另写一套匹配逻辑。
    """
    from .. import analytics
    from .. import pinyin
    out = []
    try:
        for s in analytics.list_students():
            n = counts.get(s['id'], 0)
            if not n:
                continue
            out.append({
                'id': s['id'], 'name': s['name'],
                'initial': s.get('initial') or s['name'][:1],
                'avatar_url': s.get('avatar_url'),
                'tier': s.get('tier') or '',
                'nt_label': s.get('nt_label') or '—',
                'search': pinyin.haystack(s['name'], s['id']),
                'n': n,
            })
    except Exception:
        pass
    out.sort(key=lambda s: (-s['n'], s['name']))
    return out


def _hist_picker(q, students, student):
    """筛选框的搜索状态 + 「唯一命中自动选中」。

    为什么要有「唯一命中即自动选中」这条规则
    ----------------------------------------
    没有 JS 时，筛选框退化成「输入 → 提交 → 看结果」：若只是把命中的人列出来
    等用户再点一次，就得点两步。而筛选框最常见的用法是**心里已经有这个人**
    （打 zzh 回车），此时唯一命中还要求二次确认纯属添堵。
    多命中时不做任何猜测，老老实实把候选列出来让用户点。

    返回的 `hit` 标记会被写回 `students`（模板据此 hidden 掉不命中的选项），
    这样「禁用 JS 也能用」：不命中的选项在服务端就不渲染出来。

    注意：`q` **只用来找人，不参与表格筛选**（不命中就退回原筛选）。
    它也不进任何链接的 query（见 pages._hist_filter_links）—— 搜索词是一次性的，
    留在 URL 里会变成「分享出去的链接带着别人的搜索词」。
    """
    from .. import pinyin
    q = (q or '').strip()
    hits = [s for s in students if pinyin.match(q, s['search'])] if q else []
    auto = ''
    if q and not student and len(hits) == 1:
        auto = hits[0]['id']
    for s in students:
        s['hit'] = pinyin.match(q, s['search']) if q else True
        s['active'] = s['id'] == (student or auto)
    return {
        'q': q,
        'n': len(students),
        'n_hits': len(hits),
        'auto_id': auto,
        'auto_name': (hits[0]['name'] if auto else ''),
        'miss': bool(q and not hits),
    }



def history_metadata(student=None, rng=None, q=None):
    """「历史采集元数据」区块的全部数据。分析库不可用时退化成空表，不抛错。

    参数
      student —— 学员 id；None/空 = 全部学员
      rng     —— analytics.resolve_range() 的返回值；None = 不限时间
      q       —— 筛选框里的搜索词（姓名 / 拼音 / id）。**只用来找学员**：
                 唯一命中时自动当作 student（省掉「搜到再点一下」），
                 不参与也不破坏表格筛选 —— 见 _hist_picker。

    返回的行（sessions）携带 `cells`，与列（fields）**下标一一对应**：
    `sessions[i]['cells'][j]` 就是 `fields[j]` 在第 i 场的那一格。
    """
    from .. import db as analysis_db

    plan = _field_plan()
    # 能成为**列**的只有「逐场/逐拍」两种 level —— 其余 21 个字段没有
    # 「这一场的那一个数」，硬做成列只会是一列「—」。它们改由下方
    # 「另外 N 个字段没有逐场值」分档块承担说明职责。
    col_fields = [f for f in plan if f['level'] in ('session', 'stroke')]

    # ---- ① 全批计数（不受筛选影响）---------------------------------------
    # 必须**先于**逐场查询：学员名单要按它剔掉 0 场的学员，而搜索词又可能
    # 把 student 从空变成一个具体的人 —— 顺序颠倒就会「搜到了却筛出空表」。
    counts, n_all, students = {}, 0, []
    try:
        for r in analysis_db.query(
                "SELECT student_id, COUNT(*) AS n FROM training_sessions"
                " WHERE id LIKE ? AND deleted_at IS NULL GROUP BY student_id",
                (HIST_PREFIX + '%',)):
            counts[r['student_id']] = r['n']
        n_all = sum(counts.values())
        students = _hist_student_options(counts)
    except Exception:
        pass

    # ---- ② 搜索词 → 学员 → 逐场查询 --------------------------------------
    picker = _hist_picker(q, students, student)
    if picker['auto_id']:
        student = picker['auto_id']

    where = ["s.id LIKE ?", "s.deleted_at IS NULL"]
    params = [HIST_PREFIX + '%']
    if student:
        where.append("s.student_id = ?")
        params.append(student)
    if rng:
        # `_range_clause` 返回的是 ' AND a AND b'（为接在既有 WHERE 后面而设计）；
        # 这里从零拼 WHERE，所以把开头那个 AND 切掉再拼。
        clause, rparams = _range_clause('s', rng)
        if clause.startswith(' AND '):
            clause = clause[len(' AND '):]
        if clause:
            where.append(clause)
            params.extend(rparams)

    sess_rows, stroke_rows = [], []
    try:
        sess_rows = analysis_db.query(
            "SELECT s.*, st.name AS student_name, st.tier AS student_tier,"
            "       st.avatar_url AS student_avatar,"
            "       (SELECT COUNT(*) FROM stroke_records r WHERE r.session_id = s.id)"
            "         AS n_strokes"
            "  FROM training_sessions s LEFT JOIN students st ON st.id = s.student_id"
            " WHERE " + ' AND '.join(where) + " ORDER BY s.id", tuple(params))
        ids = [r['id'] for r in sess_rows]
        if ids:
            stroke_rows = analysis_db.query(
                "SELECT session_id, speed_kmh, impact_ms, seq_in_session,"
                "       confidence, anomaly"
                "  FROM stroke_records WHERE session_id IN (%s)"
                % ','.join('?' * len(ids)), tuple(ids))
    except Exception:
        sess_rows, stroke_rows = [], []

    if not sess_rows:
        return {
            'exists': bool(n_all),          # 全批有数据、只是被筛空了 → 仍算 exists
            'empty_filtered': bool(n_all),  # 用于区分「库里没有」和「筛没了」
            'sessions': [], 'fields': [], 'col_groups': [],
            'no_value': [], 'no_value_groups': [], 'gaps': [],
            'students': students, 'picker': picker,
            'totals': {'sessions': 0, 'sessions_all': n_all, 'fields': len(plan),
                       'field_rows': len(col_fields), 'required': 0,
                       'required_filled': 0, 'no_value': 0,
                       'stroke_samples': 0, 'sessions_with_stroke': 0},
            'filter': _hist_filter_info(student, rng, 0, n_all, students),
        }


    # ---- 逐场（表的行）---------------------------------------------------
    sessions = []
    for r in sess_rows:
        sessions.append({
            'id': r['id'],
            'short': r['id'][5:],                     # 去掉 'hist-' 前缀
            'name': r.get('student_name') or r.get('student_id') or '—',
            'student_id': r.get('student_id') or '',
            'initial': (r.get('student_name') or '?')[:1],
            'avatar_url': r.get('student_avatar'),
            'tier': r.get('student_tier') or '',
            'date_label': str(r['started_at'])[:10],
            'time_label': str(r['started_at'])[11:16],
            'day_label': str(r['started_at'])[5:10],
            'started_at': r['started_at'],
            'type_label': SESSION_TYPE_LABEL.get(r.get('session_type'), '训练'),
            'court_label': COURT_TYPE_LABEL.get(r.get('court_type'), '—'),
            'duration_label': _fmt_dur(r.get('duration_sec')),
            'hr_zone': _zone_list(r.get('hr_zone')),
            'n_strokes': r.get('n_strokes') or 0,
            'cells': [],
        })
    by_id = {s['id']: s for s in sessions}

    stroke_by_sess = {}
    for sr in stroke_rows:
        stroke_by_sess.setdefault(sr['session_id'], []).append(sr)

    # ---- 逐列（表头 + 覆盖数）-------------------------------------------
    # 覆盖数现在是「**筛选后**有多少场这一列非空 / 筛选后共多少场」，
    # 因为筛选筛的就是行。切学员后看到 4/4 表示该学员这 4 场都采到了。
    fields = []
    for f in col_fields:
        item = {k: f[k] for k in (
            'no', 'field', 'key', 'unit', 'cat_key', 'cat_label', 'cat_tone',
            'cat_icon', 'group_name', 'required', 'sync', 'sync_label',
            'sync_tone', 'source', 'source_short', 'acquire', 'artifact',
            'level', 'col', 'note')}
        coverage = 0
        if f['level'] == 'session':
            # 取值一律走 `sess_rows`（库里那一行），不是 `sessions`（已重命名过的
            # 展示字典）—— 后者只有 id/name/... 那几个键，用它取 `duration_sec`
            # 会静默全空，表现为「所有列 0/16 覆盖」这种最难查的假数据。
            for r in sess_rows:
                v = r.get(f['col'])
                if v is not None and v != '':
                    coverage += 1
                by_id[r['id']]['cells'].append(_cell_pair(f['key'], v))
        else:
            for r in sess_rows:
                sam = stroke_by_sess.get(r['id'], [])
                if any(s.get(f['col']) is not None for s in sam):
                    coverage += 1
                by_id[r['id']]['cells'].append(_stroke_cell(f['col'], sam))
        item['coverage'] = coverage
        item['total'] = len(sessions)
        item['filled'] = coverage == len(sessions)
        item['status'] = 'per_session'
        fields.append(item)

    # ---- 列分组（表头第一行，用 group_name 的连续段）--------------------
    # 用 group_name 而不是 cat_key：登记表里 group 是连续的 6 段，而 cat 是
    # 横向交错的（会话概要 / 采集标识 / 生理负荷 …），按 cat 分会碎成十几个
    # 单格 colspan，表头反而更乱。cat 的信息放在列头的色点 + title 上。
    col_groups = []
    for f in fields:
        if not col_groups or col_groups[-1]['name'] != f['group_name']:
            col_groups.append({'name': f['group_name'], 'span': 0,
                               'tone': f['cat_tone']})
        col_groups[-1]['span'] += 1
    # 分组的第一列给模板一个显式标记：33 列里没有它，分组边界就看不出来，
    # 读者横着滚过去分不清「这一列属于哪一组」。在 Python 里算好，
    # 免得模板里比较相邻两项（Jinja 的 loop.previtem 只对 items 有效）。
    for i, f in enumerate(fields):
        f['group_first'] = (i == 0
                            or fields[i - 1]['group_name'] != f['group_name'])

    required = [f for f in fields if f['required']]

    # ---- 没有逐场值的 21 个字段（与筛选无关，是字段性质）----------------
    no_value = [dict(f, coverage=None, total=len(sessions),
                     filled=False, status='no_value') for f in plan
                if f['level'] not in ('session', 'stroke')]
    buckets = {k[0]: [] for k in _NO_VALUE_KINDS}
    for f in no_value:
        kind = 'profile' if f['level'] == 'profile' else (f['sync'] or 'none')
        buckets.setdefault(kind, buckets['none']).append(f)
    no_value_groups = []
    for key, label, tone, icon, desc in _NO_VALUE_KINDS:
        rows_k = buckets.get(key) or []
        if not rows_k:
            continue
        no_value_groups.append({
            'key': key, 'label': label, 'tone': tone, 'icon': icon,
            'desc': desc, 'fields': rows_k, 'count': len(rows_k),
            'required': sum(1 for f in rows_k if f['required']),
        })

    # 台账里"必采"却在本筛选下没填满的字段 —— 页面要能把它们点出来，
    # 否则「必采 20 / 填满 19」这个数字读者不知道差在哪。
    gaps = [{'no': f['no'], 'field': f['field'], 'coverage': f['coverage'],
             'total': f['total'], 'source': f['source']}
            for f in fields if f['required'] and not f['filled']]

    n_sess_with_stroke = len(stroke_by_sess)

    return {
        'exists': True,
        'empty_filtered': False,
        'sessions': sessions,
        'fields': fields,
        'col_groups': col_groups,
        'no_value': no_value,
        'no_value_groups': no_value_groups,
        'gaps': gaps,
        'students': students,
        'picker': picker,
        'totals': {
            'sessions': len(sessions),
            'sessions_all': n_all,
            'fields': len(plan),
            'field_rows': len(col_fields),
            'required': len(required),
            'required_filled': sum(1 for f in required if f['filled']),
            'no_value': len(no_value),
            'stroke_samples': sum(len(v) for v in stroke_by_sess.values()),
            'sessions_with_stroke': n_sess_with_stroke,
            'cells': len(sessions) * len(col_fields),
        },
        'filter': _hist_filter_info(student, rng, len(sessions), n_all, students),
    }


def _hist_filter_info(student, rng, n_shown, n_all, students):
    """筛选状态（给模板画筛选条用）。文案一律现算，不写死。

    学员的头像 / 首字也从这里出：模板要在「已选学员」胶囊上画出跟候选列表里
    一致的样子，而模板里不该再去 `students` 里按 id 找一遍（Jinja 里做查找
    既啰嗦又容易写错）。注意 `student` 可能来自「搜索唯一命中自动选中」——
    所以这里不能用调用方传进来的原始值判断，一律以最终生效的 id 为准。
    """
    rng = rng or {}
    active = bool(student) or bool(rng.get('from'))
    hit = next((s for s in students if s['id'] == student), None)
    return {
        'student': student or '',
        'student_label': (hit or {}).get('name') or '全部学员',
        'student_avatar': (hit or {}).get('avatar_url') or '',
        'student_initial': (hit or {}).get('initial') or '*',
        'range_key': rng.get('key') or 'all',
        'range_label': rng.get('label') or '全部记录',
        'range_display': rng.get('display') or '不限',
        'from_input': rng.get('from_input') or '',
        'to_input': rng.get('to_input') or '',
        'active': active,
        'n_shown': n_shown,
        'n_all': n_all,
    }


def _stroke_cell(col, samples):
    """逐拍字段的单元格：值恒为「本场样本条数」，title 补列本身的分布。

    stroke_records 是**抽样**表（本批每场 12 条），所以这一格不是「全量计数」。
    不写成具体数值是为了不让读者拿它跟会话汇总列做除法（分母不同）。
    """
    n = len(samples)
    if not n:
        return {'v': '—', 't': '该场没有逐拍样本'}
    tips = ['该场逐拍样本 %d 条（stroke_records 是抽样表，不是全量）' % n]
    vals = [s.get(col) for s in samples if s.get(col) is not None]
    if vals:
        try:
            lo, hi = min(vals), max(vals)
            unit = {'speed_kmh': ' km/h', 'impact_ms': ' ms',
                    'confidence': '', 'seq_in_session': ''}.get(col, '')
            fmt = (lambda x: '%.1f' % x) if isinstance(lo, float) else (lambda x: '%d' % x)
            tips.append('本列 %d 个非空样本，%s ~ %s%s' % (len(vals), fmt(lo), fmt(hi), unit))
        except TypeError:
            pass
    else:
        tips.append('本列在样本里全为空')
    return {'v': '%d 条' % n, 't': ' · '.join(tips)}



def _zone_list(raw):
    """hr_zone（JSON 文本）→ 五个区间的占比列表；解析失败返回空列表。"""
    try:
        return [json.loads(raw)['zone%d' % i] for i in range(1, 6)]
    except Exception:
        return []
