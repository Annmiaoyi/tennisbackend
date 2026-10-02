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
