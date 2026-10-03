# -*- coding: utf-8 -*-
"""Apple Watch → 后端 的**数据源目录**（唯一的字段事实源）。

这个文件同时驱动两件事：
  1. `/settings` 页面的「数据源与采集口径」区块渲染
  2. `GET /api/datasources` 接口 —— 供 iOS / 小程序端对齐字段名，
     避免两端各自起名导致同步字段漂移（历史教训：客户端叫 speed、
     服务端叫 speed_kmh，推送后字段对不上，数据静默丢失）

每个字段回答**六个**问题（2026-10-01 由四个扩到六个，补齐「怎么算出来」
与「在哪儿看得见」两段，此前只有「从哪来、怎么取、怎么判、是否上行」）：

  · source     数据从哪来（HealthKit / CoreMotion / CoreML 派生 / 应用层 / 设备）
  · acquire    具体怎么取（类型标识符、API、采样率）
  · compute    取到之后**怎么算**（公式 / 聚合口径 / 是否原值透传）
  · artifact   算完**变成什么**（落哪张表哪一列、是派生量、还是不落库）
  · judge      怎么判定（有效 / 可疑 / 丢弃）
  · surface    在训练分析里的**哪个位置展示**（页面 › 区块 › 元素）
  · sync       是否进入同步协议，以及落到哪张表
               session = training_sessions · stroke = stroke_records
               derive  = 只由后端聚合算出、不单独存储
               local   = 仅设备本地使用，不上行（隐私或体量原因）
  · shown      是否真的在界面上能看见（False = 只落库/只喂门禁/只给 App 接口）

**2026-10-02 起新增两个正交维度**（见 CATEGORIES / CATEGORY_OF）：
  · no         全局唯一编号 `MD-001`…（统一表里引用某条字段时用它）
  · cat        分类标签（会话概要 / 生理负荷 / 击球识别 / 球速估计 /
               环境上下文 / 采集质量 / 设备信息 / 采集标识）—— 页面筛选栏按它过滤

**采集准入的硬门槛（2026-10-02 确立）**：只有「腕部单点 IMU（加速度计 + 陀螺仪）
+ HealthKit」物理上**观测得到**的量，才允许出现在本表里。
观测不到的（甜区 / 球旋转 / 落点 / 弹道类）一律移出，列在 REMOVED 里留档。
理由：**源数据表里不该有采不到的东西，前端更不该有基于它的分析。**

单位一律用 **SI 或行业惯用单位**并与数据库列名一致，不要在中途换单位；
换算是展示层的事（见 analytics.fmt）。

⚠️ 本目录的「展示位置」是**实测**出来的（对照 server/analytics.py、各页面模板
与 server/routers/user_api.py），不是照文档推断。凡写「未在任何页面展示」的，
都是真的查过没有任何模板渲染它。核查结论见下方 AUDIT。
"""

# --------------------------------------------------------------------------- #
# 采集链路（Watch → iPhone → 同步队列 → 后端）
# --------------------------------------------------------------------------- #
# ⚠️ 本站有**两条**容易混淆的链路，务必区分：
#   · 分析链（本常量）：Watch 采集 → iPhone 识别 → 质量门禁 → 同步 → 后端聚合
#     服务于「训练分析与排行」，终点是 training_sessions + stroke_records。
#   · 训练链（见 server/annotation/stats.py 的 STAGES）：
#     原始会话采集 → 双通道上行 → 视频↔波形人工标注 → 导出 CoreML 训练集
#     服务于「让识别算法变准」，终点是 dataset.csv。
#   两条链共用同一批手表传感器数据，但产物与消费者完全不同，不要混着看。
PIPELINE = [
    {'step': '01', 'icon': 'watch',
     'title': 'Watch 端会话采集',
     'body': '用户在手表发起网球训练 → 同时开启 HKWorkoutSession（拿心率/能量/时长）'
             '与 CoreMotion 高频惯导流（拿挥拍），两条流共用同一时基。'},
    {'step': '02', 'icon': 'phone_iphone',
     'title': 'iPhone 端实时识别',
     'body': '惯导窗口经 CoreML 模型做击球检测与分类（正手/反手/发球/切削/截击/高压），'
             '输出逐拍样本与置信度；HealthKit 侧的会话汇总同时落本地库。'},
    {'step': '03', 'icon': 'verified_user',
     'title': '本地质量门禁',
     'body': '按下方 6 道门禁过滤后才允许入库：值域、配平、置信度、佩戴状态、'
             '突变、交叉比对。不合格的样本标 suspect 或直接丢弃，不污染统计。'},
    {'step': '04', 'icon': 'cloud_sync',
     'title': '离线优先同步',
     'body': '先写本地库并标记 pending，联网后按队列推送；'
             '服务端以 operation_id 幂等去重，LWW 裁决冲突，软删除保留墓碑。'},
    {'step': '05', 'icon': 'insights',
     'title': '后端权威聚合',
     'body': '本管理站的所有排行与分析都由 training_sessions + stroke_records 现算，'
             '不缓存中间结果，保证任何数字都能用一条 SQL 复现。'},
]

# --------------------------------------------------------------------------- #
# 质量门禁（6 道）
# --------------------------------------------------------------------------- #
GATES = [
    {'code': 'G1', 'name': '值域门禁', 'icon': 'straighten',
     'body': '每个字段给定物理合理区间（见下表「判定」列）。越界直接丢弃该样本并计入 '
             'invalid_count，不做钳位（钳位会把异常值伪装成正常值，更难排查）。'},
    {'code': 'G2', 'name': '配平门禁', 'icon': 'balance',
     'body': '正手 + 反手 + 发球 + 切削 + 截击 + 高压 ≤ 检测到的挥拍总数，'
             '差额即「未识别拍数」（分类置信度不足、归入 unknown 的拍）。'
             '差额 > 5% 说明本场分类器整场失灵，标记 suspect 且不进入排行榜'
             '（2~3% 的未识别属正常波动，对应 G3 门限 0.60）；'
             '分项之和「超过」总数则说明分类计数串味，同样判 suspect。'},
    {'code': 'G3', 'name': '置信度门禁', 'icon': 'percent',
     'body': '逐拍算法置信度 < 0.60 的样本不参与球速统计（仍保留原始行以便回溯），'
             '避免把误检的「挥手」当成击球拉低均速。'},
    {'code': 'G4', 'name': '传感器门禁', 'icon': 'sensor_occupied',
     'body': '区分「缺失」与「无效」：手表未佩戴或摘表时心率整段缺失，'
             '应标 missing 而不是 0，否则会污染平均值（0 会把均值严重拉低）。'},
    {'code': 'G5', 'name': '突变门禁', 'icon': 'trending_up',
     'body': '与该学员近 10 次的滚动中位数比较，偏离超过 3×MAD 的场次标 suspect 并进入'
             '人工复核队列 —— 例如球速突然 +40%，多半是换拍/换场地而不是真实进步。'},
    {'code': 'G6', 'name': '交叉门禁', 'icon': 'compare_arrows',
     'body': '与 HealthKit 原始样本交叉核对：训练时长、活动能量、平均/最高心率'
             '必须与 HKWorkout 记录一致（容差见下表），不一致以 HealthKit 为准。'},
]

# --------------------------------------------------------------------------- #
# 采集侧注意事项（页面「接入作业须知」卡片，逐条来自真实踩坑）
# --------------------------------------------------------------------------- #
ACQUISITION_NOTES = [
    {'tone': 'text-primary-fixed', 'icon': 'abc', 'title': '① 字段名 = 列名，不另起别名',
     'body': '客户端上行字段必须与下表「字段 / 列名」完全一致（如 speed_kmh 而非 speed）。'
             '服务端按列名白名单校验，未登记字段直接 rejected，不做静默丢弃。'
             '历史上客户端叫 speed、服务端叫 speed_kmh，推送成功但数据全空。'},
    {'tone': 'text-secondary', 'icon': 'block', 'title': '② 缺失 ≠ 0',
     'body': '采集不到的字段一律留空并置 missing，严禁填 0。'
             '写 0 会把平均值拉低，且事后无法区分「真的没有」与「没采到」。'
             '典型：手表未佩戴时心率整段缺失，写成 0 会让均心率从 150 掉到 60。'},
    {'tone': 'text-tertiary-fixed-dim', 'icon': 'percent', 'title': '③ 分母别用错（最容易错的一条）',
     'body': '本场「击球总数」是手表统计的完整计数，而逐拍明细在库里是**抽样存储**'
             '（一场约 10~20 条）。算构成占比必须用逐拍样本数作分母；'
             '拿样本数去除总数会得出「发球占 0.8%」这种错得离谱的结果。'
             '展示侧对这两类数字都显式标注了口径。'},
    {'tone': 'text-error', 'icon': 'front_hand', 'title': '④ 佩戴手腕决定正/反手符号',
     'body': 'worn_wrist 设反了会把整场正手判成反手，而且**没有任何其它字段能交叉发现**'
             '——陀螺仪符号整体翻转，数据本身完全自洽。开始训练前必须确认这一项。'},
    {'tone': 'text-primary-fixed', 'icon': 'schedule', 'title': '⑤ 时基统一：绝对时间走 UTC，相对时间走会话内毫秒',
     'body': '会话起止一律 UTC ISO8601（保留毫秒），服务端不接受其它时区写法；'
             '会话内的击球时刻统一用 impact_ms（距 started_at 的毫秒偏移），'
             '不要下发带时区的绝对时间戳做逐拍对齐。'},
    {'tone': 'text-secondary', 'icon': 'fingerprint', 'title': '⑥ 幂等键要覆盖「重传」与「重装」',
     'body': 'operation_id 保证重试不重复写；external_id 保证同一场会话只在库里留一行。'
             'App 卸载重装后本地 id 重新生成，此时若不带 external_id，同一场训练会入库两次。'},
    {'tone': 'text-tertiary-fixed-dim', 'icon': 'history', 'title': '⑦ 跨算法版本不可直接排行',
     'body': '模型升级会整体改变球速标定值（同一球在不同版本下读数不同）。'
             '算法版本必须随会话上行，排行与趋势必须按版本分组或显式标注，否则「进步」可能只是换了模型。'},
    {'tone': 'text-outline', 'icon': 'lock', 'title': '⑧ 敏感数据不出设备',
     'body': '标「仅本地」的字段（HRV / 静息心率 / 体重 / GPS 精确轨迹 / 呼吸频率 / VO₂max）'
             '只在设备内用于恢复建议，不进同步队列，服务端也不落库。'},
]

# --------------------------------------------------------------------------- #
# 元数据分类（统一表的「备注」列 = 分类；筛选栏按分类过滤）
# --------------------------------------------------------------------------- #
# 分类是**横切**采集分组的：例如「生理负荷」同时来自 HKWorkout 与 HealthKit 心率。
#   · 采集分组（GROUPS）回答「这条数据从哪个采集口来」
#   · 分类（CATEGORIES）回答「这条数据是什么性质的量」
# 两者回答的问题不同，所以统一表既保留分组又给分类，不重复。
CATEGORIES = [
    {'key': 'session', 'label': '会话概要', 'icon': 'event_note',
     'tone': 'text-primary-fixed',
     'desc': '一次训练的骨架：什么时候、练了多久、什么形式、在哪。'},
    {'key': 'load', 'label': '生理负荷', 'icon': 'favorite',
     'tone': 'text-error',
     'desc': '心率与能量代谢 —— 衡量强度与消耗。全部来自 HealthKit 系统级记录。'},
    {'key': 'stroke', 'label': '击球识别', 'icon': 'sports_tennis',
     'tone': 'text-secondary',
     'desc': '挥拍检测与分类的计数、时序与置信度。来自 CoreMotion + CoreML。'},
    {'key': 'speed', 'label': '球速估计', 'icon': 'speed',
     'tone': 'text-primary-fixed',
     'desc': '由腕部角速度推算的**拍头线速度**（标定系数 1.05m）。'
             '它是估计值、不是实测球速 —— 球速需要看到球的飞行轨迹。'},
    {'key': 'context', 'label': '环境上下文', 'icon': 'location_on',
     'tone': 'text-tertiary-fixed-dim',
     'desc': '地点与天气 —— 用来解释「为什么这场数据反常」，不参与排行。'},
    {'key': 'quality', 'label': '采集质量', 'icon': 'verified_user',
     'tone': 'text-outline',
     'desc': '采样率 / 丢包率 / 佩戴 / 突变 —— 判断前面那些数字可不可信。'},
    {'key': 'device', 'label': '设备信息', 'icon': 'memory',
     'tone': 'text-on-surface-variant',
     'desc': '机型 / 系统 / 电量 —— 事后解释异常批次用。'},
    {'key': 'identity', 'label': '采集标识', 'icon': 'fingerprint',
     'tone': 'text-on-surface-variant',
     'desc': '幂等键、来源端、腕别、算法版本 —— 不展示，但决定数据能否与其它端对上。'},
]
CATEGORY_MAP = {c['key']: c for c in CATEGORIES}

# 字段 key → 分类 key。**集中一处定义**，便于一眼看全分类体系；
# 新增字段忘了归类时，_cat_of() 会回落到「会话概要」并在页面上显示为未分类。
CATEGORY_OF = {
    # 会话概要
    'duration_sec': 'session', 'started_at': 'session', 'ended_at': 'session',
    'session_type': 'session', 'court_type': 'session',
    # 生理负荷
    'calories_kcal': 'load', 'basal_kcal': 'load', 'hr_zone': 'load',
    'avg_hr': 'load', 'max_hr': 'load', 'hrr_60': 'load', 'hrv_sdnn': 'load',
    'resting_hr': 'load', 'respiratory_rate': 'load', 'vo2max': 'load',
    'step_count': 'load', 'distance_km': 'load', 'body_mass': 'load',
    # 击球识别
    'rally_max': 'stroke', 'stroke_count': 'stroke', 'forehand_count': 'stroke',
    'backhand_count': 'stroke', 'serve_count': 'stroke', 'slice_count': 'stroke',
    'volley_count': 'stroke', 'smash_count': 'stroke', 'impact_ms': 'stroke',
    # 球速估计
    'avg_speed_kmh': 'speed', 'peak_speed_kmh': 'speed', 'forehand_avg_kmh': 'speed',
    'backhand_avg_kmh': 'speed', 'serve_avg_kmh': 'speed', 'serve_peak_kmh': 'speed',
    'speed_kmh': 'speed',
    # 环境上下文
    'location': 'context', 'gps_track': 'context', 'altitude_m': 'context',
    'temperature_c': 'context', 'humidity_pct': 'context', 'wind_speed_ms': 'context',
    # 采集质量
    'confidence': 'quality', 'anomaly': 'quality', 'wrist_on': 'quality',
    'sample_rate_hz': 'quality', 'drop_rate': 'quality', 'hit_rate': 'quality',
    # 设备信息
    'watch_model': 'device', 'os_version': 'device', 'battery_pct': 'device',
    # 采集标识
    'wrist': 'identity', 'worn_wrist': 'identity', 'source': 'identity',
    'external_id': 'identity', 'seq_in_session': 'identity',
    'algorithm_version': 'identity',
}

# --------------------------------------------------------------------------- #
# 已从源数据表移除的字段（2026-10-02）
# --------------------------------------------------------------------------- #
# 判据只有一条：**腕部单点 IMU（加速度计 + 陀螺仪）+ HealthKit 能不能观测到这个量。**
# 观测不到的一律从登记表里删掉 —— 它不该出现在「训练需要采集的源数据表」里，
# 前端也不该出现基于它的分析。原先这些字段的展示值全部是 seed 随机数冒充的，
# 属于 ROADMAP R5「用假数据撑场面会误导用户与决策」。
#
# 移除不等于遗忘：数据库列暂时保留（三端同步契约刚统一，不宜同轮改动），
# 但**不再写入任何值**（恒为 NULL），也不再有任何页面或接口消费它们。
# 待硬件方案（拍面传感器 / 视觉）落地后，再决定是启用还是彻底删列。
REMOVED = [
    {'key': 'sweet_spot_rate', 'field': '整场甜区命中率', 'art': 'training_sessions.sweet_spot_rate',
     'reason': '甜区是球在**拍面**上的撞击点。腕部 IMU 看不到拍面 —— 需要拍面/拍柄传感器或高速摄像。'},
    {'key': 'sweet_spot', 'field': '甜区命中（逐拍）', 'art': 'stroke_records.sweet_spot',
     'reason': '同上。原口径写「由拍面振动频谱基频偏移推断」——腕部传感器也测不到拍面振动。'},
    {'key': 'spin_rpm', 'field': '整场平均转速', 'art': 'training_sessions.spin_rpm',
     'reason': '转速是**球**的自转。腕部 IMU 测的是手腕自身转动，与球的自转不是同一个量。'},
    {'key': 'spin_rpm', 'field': '单拍转速', 'art': 'stroke_records.spin_rpm',
     'reason': '同上（与 training_sessions.spin_rpm 同名不同表）。'},
    {'key': 'spin_type', 'field': '旋转类型', 'art': 'stroke_records.spin_type',
     'reason': '上旋/下旋/平击由球的自转方向决定，腕部 IMU 无法判定。'},
    {'key': 'depth_m', 'field': '落点深度', 'art': 'stroke_records.depth_m',
     'reason': '落点需要看到**球的飞行轨迹**（摄像/雷达）。腕部看不到球飞出去之后的事。'},
    {'key': 'landing_zone', 'field': '落点区域', 'art': 'stroke_records.landing_zone',
     'reason': '由 depth_m 分箱而来，源头既已不可得，派生值同样不可得。'},
    {'key': 'lateral_offset_m', 'field': '左右偏差', 'art': 'stroke_records.lateral_offset_m',
     'reason': '同落点深度 —— 横向落点同样需要球的飞行轨迹。'},
    {'key': 'net_clearance_m', 'field': '过网高度', 'art': 'stroke_records.net_clearance_m',
     'reason': '同落点深度 —— 弹道推断的前提是先能观测到弹道。'},
    # 2026-10-03 复查补录：上一轮审计关键词只有「甜区/旋转/落点/过网/弹道」，
    # 漏掉了「结果类」（进球 / 得分 / 失误 / 制胜分）。这几个字段此前**从未进过本登记表**，
    # 所以「登记表驱动」的复核结构上看不见它们 —— 教训：登记表 ≠ DB 全集，
    # 必须另做一次 schema 列 vs 本表 key 的对账。
    {'key': 'unforced_errors', 'field': '非受迫性失误', 'art': 'training_sessions.unforced_errors',
     'reason': '判定「失误」要先知道球有没有落在界内、以及这一分是否因此丢掉 —— '
               '两者都需要视觉或人工计分。腕部 IMU 只知道你挥了几拍。'},
    {'key': 'winners', 'field': '制胜分', 'art': 'training_sessions.winners',
     'reason': '同上。制胜分是「这一分的对抗结果」，属计分数据，不是腕部可观的运动学量。'},
]

# --------------------------------------------------------------------------- #
# 核查结论（2026-10-01 全量复核 schema.sql / analytics.py / user_api.py 后得出）
# --------------------------------------------------------------------------- #
AUDIT = {
    'date': '2026-10-01',
    'date2': '2026-10-02',
    'baseline': 'server/schema.sql（列权威） · server/analytics.py（计算与消费） · '
                'server/routers/user_api.py 与 ingest_api.py（接口去向） · 全部页面模板',
    'before_fields': 40,
    'after_fields': 53,
    'removed_fields': len(REMOVED),
    'findings': [
        {'level': 'fix', 'title': '② 剔除 9 个「腕部 IMU 观测不到」的字段（2026-10-02）',
         'body': '甜区（整场率 + 逐拍）、转速（整场 + 逐拍）、旋转类型、落点深度、落点区域、'
                 '左右偏差、过网高度 —— 这 9 项都需要拍面传感器或球的飞行轨迹才能测到，'
                 '腕部单点 IMU 物理上不可观测。原登记表把它们描述成可采集，'
                 '而页面上的值全部是 `seed.py` 随机数冒充的。现已从源数据表移除，'
                 '对应展示与分析同步下线，并停止写入（列暂留、恒为 NULL）。详见 REMOVED。'},
        {'level': 'fix', 'title': '② 登记表补齐「编号 + 分类」两个维度（2026-10-02）',
         'body': '每个字段给出全局唯一编号（MD-001…）与分类标签（会话概要 / 生理负荷 / '
                 '击球识别 / 球速估计 / 环境上下文 / 采集质量 / 设备信息 / 采集标识）。'
                 '分类是横切采集分组的独立维度，设置页改为**一张统一表**并按分类筛选。'},
        {'level': 'fix', 'title': '「步数」的落地状态标错了',
         'body': '原标「会话级上行」，但 training_sessions 里根本没有 step_count 列，'
                 '同步白名单与 ingest 也没有它 —— 实际是采了不上行。已改为「仅本地」并标「待建列」。'},
        {'level': 'fix', 'title': '「算法版本」的落地状态标错了',
         'body': '原标「会话级上行」，但 training_sessions 无该列；'
                 '实际上它只落在 nt_benchmarks（段位常模表），且 analytics 算出的版本值也没有页面渲染。'
                 '已标「待建列」，并注明跨版本排行因此无法在库内分组。'},
        {'level': 'warn', 'title': '6 个击球分项计数在管理页一个都没展示',
         'body': 'forehand_count / backhand_count / serve_count / slice_count / volley_count / '
                 'smash_count 已落库，但它们的用途只有两个：喂 G2 配平门禁、以及 App 历史接口的 counts。'
                 '管理页上的「击球构成占比」用的是逐拍抽样样本，与这 6 列**不是同一口径**。'},
        {'level': 'warn', 'title': 'hr_zone（心率区间分布）落库了却无人渲染',
         'body': '列已建、同步白名单里有、种子数据也写了，但 analytics 与全部模板都不读它 ——'
                 '库里躺着、页面上看不见。属于「要么补展示、要么明确停用」的悬置字段。'},
        {'level': 'warn', 'title': 'impact_ms 没有被任何时间线图使用',
         'body': '「击球时间偏移」已落库并随 App 逐拍接口下发，但**没有**用于任何球速离散序列图。'
                 '/training 页上那张「每拍球速与出球分布对比」是设计稿的静态示意图，与数据无关。'},
        {'level': 'warn', 'title': '22 个字段已定义口径但后端未落地',
         'body': '标 derive / local 的字段（基础代谢、HRR60、HRV、静息心率、呼吸频率、VO₂max、体重、'
                 'GPS 轨迹、海拔、温湿度、风速、系统版本、佩戴状态、采样率、丢包率、电量等）'
                 '在训练分析中零展示。它们不是错误，但**接入方不应期待回读**，已逐条标注。'},
        {'level': 'warn', 'title': '5 个页面仍是纯静态设计稿，零数据绑定',
         'body': '概览看板 / 用户管理 / 用户画像 / 用户反馈，以及训练页的设计稿主体（A、B 选手对比、'
                 '雷达图、球速分布等），模板里没有任何 Jinja 表达式，数字全是硬编码。'
                 '2026-10-02 起：其中**依赖不可得字段**的展示项（甜区集中率、旋转、落点深度等）已删除，'
                 '其余静态展示项保留但仍是设计稿占位，未接真实数据。'},
    ],
}


# --------------------------------------------------------------------------- #
# 字段目录
# --------------------------------------------------------------------------- #
# sync: session | stroke | derive | local
GROUPS = [
    {
        'key': 'workout', 'name': '训练元数据', 'icon': 'fitness_center',
        'framework': 'HealthKit',
        'desc': '一次训练的起止与整体负荷。来源是 HKWorkout，'
                '是全场数据里可信度最高的一层（系统级记录，不可伪造）。',
        'rows': [
            {'field': '训练时长', 'key': 'duration_sec', 'unit': '秒 (s)', 'sync': 'session',
             'source': 'HealthKit · HKWorkout',
             'acquire': 'HKWorkoutActivityType.tennis 会话的 duration；也可用 ended_at − started_at 复算',
             'compute': '原值透传，不做二次计算。换算成分钟是**展示层**的事（analytics._minutes），'
                        '入库一律存秒；区间累计用 SUM、场均用 AVG',
             'artifact': 'training_sessions.duration_sec',
             'judge': '0 < 值 ≤ 6h（网球单次上限）；与 ended−started 之差 ≤ 2s；否则以 HealthKit 为准',
             'surface': '/training › 个人分析 › KPI「累计训练时长」（区间 SUM 转分钟）; '
                        '/training › 个人分析 › 逐场明细表「时长」; '
                        '/training › 横向对比 › 指标矩阵与「单场训练时长」双榜',
             'shown': True, 'required': True},
            {'field': '开始时间', 'key': 'started_at', 'unit': 'ISO8601 UTC', 'sync': 'session',
             'source': 'HealthKit · HKWorkout.startDate',
             'acquire': 'HKWorkout.startDate，统一转 UTC 并保留毫秒',
             'compute': '原值落库；后端用 date(started_at) 做区间过滤与排序，'
                        '展示时取前 10 位做日期、第 11–16 位做时间',
             'artifact': 'training_sessions.started_at',
             'judge': '不得晚于 ended_at；不得晚于服务器当前时间（时钟回拨检测）',
             'surface': '/training › 个人分析 › 逐场明细表「日期 / 时间」; '
                        '同时是全站区间筛选与排序的依据（本身不单独展示）; '
                        'App /api/prod/sessions › startedAt',
             'shown': True, 'required': True},
            {'field': '结束时间', 'key': 'ended_at', 'unit': 'ISO8601 UTC', 'sync': 'session',
             'source': 'HealthKit · HKWorkout.endDate',
             'acquire': 'HKWorkout.endDate，统一转 UTC',
             'compute': '原值落库；仅用于与 started_at 对账时长，不参与聚合',
             'artifact': 'training_sessions.ended_at',
             'judge': '必须晚于 started_at；与 started_at 同日或跨夜均可，时长仍受 G1 约束',
             'surface': '未在任何页面展示；仅落库 + App /api/prod/sessions › endedAt',
             'shown': False, 'required': True},
            {'field': '佩戴手腕', 'key': 'worn_wrist', 'unit': '枚举', 'sync': 'session',
             'source': '应用层 · 用户选择 / iPhone 下发',
             'acquire': 'left 左手戴表 / right 右手戴表；开始训练前在手表或 iPhone 上设定',
             'compute': '原值落库；不参与任何聚合计算，但**下游旋转类指标的符号全依赖它**',
             'artifact': 'training_sessions.worn_wrist',
             'judge': '枚举内取值。⚠️ 必须正确：正/反手的旋转符号完全依赖它，'
                      '设反了会把正手整场判成反手，且「没有任何其它字段能交叉发现」',
             'surface': '未在分析页展示；仅落库 + App /api/prod/sessions › wrist。'
                        '注意 /annotation 工作台的「持拍」列来自标注库自己的 sessions.wrist，与本字段不同源',
             'shown': False, 'required': True},
            {'field': '数据来源端', 'key': 'source', 'unit': '枚举', 'sync': 'session',
             'source': '应用层 · 接入时标记',
             'acquire': 'acemate（自有 App 采集）/ netpulse_watch（Apple Watch 采集端接入）',
             'compute': '原值落库，不计算',
             'artifact': 'training_sessions.source（与 external_id 组成部分唯一索引）',
             'judge': '枚举内取值；与 external_id 组成 (source, external_id) 部分唯一索引，'
                      '同一场重复上传只能落一行',
             'surface': '未在任何页面展示；仅用于跨端幂等去重与 /api/ingest/sessions 的来源过滤',
             'shown': False, 'required': True},
            {'field': '外部会话 ID', 'key': 'external_id', 'unit': '文本 (UUID)', 'sync': 'session',
             'source': '应用层 · 采集端原生会话 ID',
             'acquire': '取采集端 MatchSession.id，落库时加前缀映射（nps-<uuid>），'
                        '避免与 AceMate 自身生成的 id 相撞',
             'compute': '原值落库；由它派生出的 operation_id 是服务端幂等判重的键',
             'artifact': 'training_sessions.external_id',
             'judge': '非空且唯一；同一 external_id 重复上传时派生出的 operation_id 相同，'
                      '会命中幂等闸并返回 duplicate，「不会重复入库」',
             'surface': '未在任何页面展示；App 单场详情用它作查询键并回传 externalId',
             'shown': False, 'required': True},
            {'field': '活动能量 (卡路里)', 'key': 'calories_kcal', 'unit': 'kcal', 'sync': 'session',
             'source': 'HealthKit · HKQuantityTypeIdentifierActiveEnergyBurned',
             'acquire': 'HKStatisticsQuery，按 session 时间窗取 .cumulativeSum，单位换算到 kcal',
             'compute': '同一时间窗内累加得单场值；后端区间累计用 SUM、场均用 AVG、单场最高用 MAX',
             'artifact': 'training_sessions.calories_kcal',
             'judge': '0 < 值 < 2500 kcal；与时长比 4~20 kcal/min；超出多为未绑定体重或误判运动类型',
             'surface': '/training › 个人分析 › KPI「累计消耗」; 逐场明细表「消耗」; '
                        '/training › 横向对比 › 矩阵与「单场消耗卡路里」双榜; '
                        'App /api/prod/sessions › calories',
             'shown': True, 'required': True},
            {'field': '基础代谢能量', 'key': 'basal_kcal', 'unit': 'kcal', 'sync': 'derive',
             'source': 'HealthKit · HKQuantityTypeIdentifierBasalEnergyBurned',
             'acquire': '同一时间窗取累加值；只用于区分「净消耗」与「总消耗」',
             'compute': '口径：同窗口累加。⚠️ **后端当前没有任何代码计算它**，'
                        'analytics 里不存在「净消耗」这一指标',
             'artifact': '不落库（derive）。后端也未派生',
             'pending': '契约定义了「净消耗 = 活动能量 + 基础代谢」，但未实现；'
                        '接入方可先不上行该字段',
             'judge': '恒小于活动能量；若大于则判定为设备心率脱落导致，整场标 suspect',
             'surface': '未在任何页面展示（无计算、无落库、无渲染）',
             'shown': False, 'required': False},
            {'field': '训练形式', 'key': 'session_type', 'unit': '枚举', 'sync': 'session',
             'source': '应用层 · 用户选择',
             'acquire': '手表开始训练时四选一：drill 专项 / match 实战 / rally 对拉 / serve 发球',
             'compute': '原值落库；后端经 SESSION_TYPE_LABEL 映射为中文展示名，'
                        '并单独统计 match 场次',
             'artifact': 'training_sessions.session_type',
             'judge': '必须落在枚举内；为空时按「击球构成」自动推断并标记 inferred',
             'surface': '/training › 个人分析 › 逐场明细表「训练内容」副行; '
                        'KPI「训练场次」副文案「其中实战对抗 N 场」（只统计 match）',
             'shown': True, 'required': True},
            {'field': '场地类型', 'key': 'court_type', 'unit': '枚举', 'sync': 'session',
             'source': '应用层 · 用户选择 或 GPS+POI 匹配',
             'acquire': 'hard 硬地 / clay 红土 / grass 草地 / indoor 室内；'
                        '未选择时用定位落点匹配已知球场库',
             'compute': '原值落库；后端经 COURT_TYPE_LABEL 映射为中文展示名',
             'artifact': 'training_sessions.court_type',
             'judge': '枚举内取值；POI 匹配置信度 < 0.7 时回落为 unknown 而不是硬猜',
             'surface': '/training › 个人分析 › 逐场明细表「场地类型」。'
                        '⚠️ App 侧：/api/prod/sessions 的 SQL 取了该列但输出字典里没映射，App 实际拿不到',
             'shown': True, 'required': False},
            {'field': '训练地点', 'key': 'location', 'unit': '文本', 'sync': 'session',
             'source': 'CoreLocation · CLGeocoder',
             'acquire': '取训练时段定位中位数坐标 → 逆地理编码为「城市 · 行政区」',
             'compute': '坐标中位数 → 逆地理编码；**只保留到行政区级**，不存精确坐标',
             'artifact': 'training_sessions.location（逐场）; '
                         '另有一份 students.location 是档案级、人工维护，两者不同源',
             'judge': '只保留到行政区级（不存精确坐标，隐私）；定位缺失时留空，不写「未知」占位',
             'surface': '/training › 个人分析 › 逐场明细表「地点」（读会话级）; '
                        '学员档案头第三行「地点」（读 students.location，不是本字段）; '
                        'App /api/prod/sessions › location',
             'shown': True, 'required': False},
            {'field': '心率区间分布', 'key': 'hr_zone', 'unit': 'JSON', 'sync': 'session',
             'source': '派生 · 由心率序列分箱',
             'acquire': '按最大心率百分比分 5 区（50/60/70/80/90%），统计各区停留秒数',
             'compute': '逐秒心率样本按阈值分箱 → 各区累加秒数 → 序列化成 JSON 存一列。'
                        '⚠️ 后端未做二次计算，直接原样存储',
             'artifact': 'training_sessions.hr_zone（TEXT/JSON）',
             'pending': '已落库且在同步白名单内，但 analytics 与全部模板都不读它 —— '
                        '属于「要么补展示、要么明确停用」的悬置字段',
             'judge': '五区秒数之和 ≈ 训练时长（±5%）；不满足说明心率存在长时间缺失',
             'surface': '未在任何页面展示（库里有、页面上看不见）',
             'shown': False, 'required': False},
            {'field': '最长相持', 'key': 'rally_max', 'unit': '拍', 'sync': 'session',
             'source': '派生 · 由击球间隔序列',
             'acquire': '相邻击球间隔 < 2.5s 视为同一回合，取最长连续段长度',
             'compute': '对 impact 时刻序列做间隔扫描：间隔 < 2.5s 归入同一回合，'
                        '取连续段长度的最大值',
             'artifact': 'training_sessions.rally_max',
             'judge': '2 ≤ 值 ≤ 120；> 120 多为漏检导致把两回合并成一段',
             'surface': '/training › 个人分析 › 逐场明细表「最长相持」; '
                        'App /api/prod/sessions › rallyMax',
             'shown': True, 'required': False},
        ],
    },
    {
        # ⚠️ 这个分组是 2026-10-01 核查时**新补的**。
        # 原目录只收录了逐拍字段，漏掉了「手表端算好后随会话上行」的整场汇总值，
        # 而这些恰恰是页面上所有球速数字的真正来源。
        'key': 'ball', 'name': '整场球质汇总（会话级）', 'icon': 'speed',
        'framework': 'CoreMotion + CoreML · 会话聚合',
        'desc': '手表在本场结束时算好的整场球速汇总，随会话一次性上行。'
                '⚠️ 与「击球识别」组的逐拍字段是**两种东西**：'
                '这一组覆盖全场每一拍、无抽样偏差，是排行榜与 KPI 的取值来源；'
                '那一组只存了十几条样本。'
                '同一学员的球速在「个人分析 / 排行榜 / 逐场明细」能保持一致，靠的就是这一组。'
                '（2026-10-02：原先还含甜区率与转速，两者腕部 IMU 均不可得，已移出，见 REMOVED。）',
        'rows': [
            {'field': '整场平均球速', 'key': 'avg_speed_kmh', 'unit': 'km/h', 'sync': 'session',
             'source': 'CoreMotion + CoreML · 会话汇总输出',
             'acquire': '手表对全场所有有效击球的球速取算术平均后随会话上行',
             'compute': '**无抽样、无换算**，直接取手表算好的值；'
                        '后端区间平均值 = 各场该值的均值（AVG）',
             'artifact': 'training_sessions.avg_speed_kmh',
             'judge': '10 ≤ 值 ≤ 260 km/h；须 ≤ 本场 peak_speed_kmh，否则标 suspect',
             'surface': '/training › 横向对比 › 「整体球速」平均值榜 + 全指标矩阵',
             'shown': True, 'required': False},
            {'field': '整场最高球速', 'key': 'peak_speed_kmh', 'unit': 'km/h', 'sync': 'session',
             'source': 'CoreMotion + CoreML · 会话汇总输出',
             'acquire': '手表取本场单拍球速最大值后随会话上行',
             'compute': '原值透传；后端区间最大值 = 各场的 MAX',
             'artifact': 'training_sessions.peak_speed_kmh',
             'judge': '须 ≥ 本场 avg_speed_kmh；> 260 km/h 视为估计失准并丢弃',
             'surface': '/training › 横向对比 › 「整体球速」最大值榜 + 全指标矩阵',
             'shown': True, 'required': False},
            {'field': '正手均速', 'key': 'forehand_avg_kmh', 'unit': 'km/h', 'sync': 'session',
             'source': 'CoreMotion + CoreML · 会话汇总输出',
             'acquire': '手表对本场全部正手击球取速度均值',
             'compute': '原值透传；后端区间平均 = AVG，最好一场 = MAX。'
                        '技术分析里的「反手为正手的 x%」= bh_avg ÷ fh_avg',
             'artifact': 'training_sessions.forehand_avg_kmh',
             'judge': '30 ≤ 值 ≤ 200 km/h；与整体均速偏差过大时复核分类器是否把反手并入正手',
             'surface': '/training › 个人分析 › KPI「正手 / 反手均速」左值; 逐场明细表「正手均速」; '
                        '/training › 横向对比 › 「正手球速」双榜 + 矩阵',
             'shown': True, 'required': False},
            {'field': '反手均速', 'key': 'backhand_avg_kmh', 'unit': 'km/h', 'sync': 'session',
             'source': 'CoreMotion + CoreML · 会话汇总输出',
             'acquire': '手表对本场全部反手击球取速度均值',
             'compute': '原值透传；后端 AVG / MAX 同正手',
             'artifact': 'training_sessions.backhand_avg_kmh',
             'judge': '20 ≤ 值 ≤ 180 km/h；与正手之比 < 0.92 会触发「反手短板」标签',
             'surface': '/training › 个人分析 › KPI「正手 / 反手均速」右值; 逐场明细表「反手均速」; '
                        '/training › 横向对比 › 「反手球速」双榜（该项最能看出能否被持续压制）',
             'shown': True, 'required': False},
            {'field': '发球均速', 'key': 'serve_avg_kmh', 'unit': 'km/h', 'sync': 'session',
             'source': 'CoreMotion + CoreML · 会话汇总输出',
             'acquire': '手表对本场全部发球取速度均值',
             'compute': '原值透传；后端对多场取均值，作为「各场均速均值」展示',
             'artifact': 'training_sessions.serve_avg_kmh',
             'judge': '80 ≤ 值 ≤ 220 km/h；须 ≤ 本场 serve_peak_kmh',
             'surface': '/training › 个人分析 › KPI「发球最高速」的副文案「各场均速均值」; '
                        '/training › 横向对比 › 「发球速度」平均值榜',
             'shown': True, 'required': False},
            {'field': '发球峰值', 'key': 'serve_peak_kmh', 'unit': 'km/h', 'sync': 'session',
             'source': 'CoreMotion + CoreML · 会话汇总输出',
             'acquire': '手表取本场发球单拍最高速后随会话上行',
             'compute': '原值透传；后端区间取 MAX，并据此算同侪排名与强弱项标签',
             'artifact': 'training_sessions.serve_peak_kmh',
             'judge': '80 ≤ 值 ≤ 240 km/h；低于 80 多为把挥拍误判成发球',
             'surface': '/training › 个人分析 › KPI「发球最高速」主值; 逐场明细表「发球峰值」; '
                        '区间摘要 headline「发球最高速出现在…」; 强弱项标签「发球强项 / 发球待提升」; '
                        '/training › 横向对比 › 「发球速度」最大值榜 + 矩阵',
             'shown': True, 'required': False},
        ],
    },
    {
        'key': 'stroke', 'name': '击球识别与单拍球速（逐拍抽样）', 'icon': 'sports_tennis',
        'framework': 'CoreMotion + CoreML',
        'desc': '逐拍明细。惯导流做检测与分类，单拍球速由拍头线速度估计（口径见「球速估计」分类）。'
                '⚠️ 逐拍明细在库里是「抽样存储」（一场约 10~20 条），'
                '而「击球总数」是手表统计的完整计数 —— 两者分母不同，做占比时必须用抽样数做分母。'
                '整场汇总值见上一组「整场球质汇总」。'
                '（2026-10-02：甜区 / 旋转 / 落点类字段腕部 IMU 不可得，已移出，见 REMOVED。）',
        'rows': [
            {'field': '击球总数', 'key': 'stroke_count', 'unit': '次', 'sync': 'session',
             'source': 'CoreMotion + 算法 · 峰值检测',
             'acquire': '三轴加速度计合矢量 + 陀螺仪角速度，过自适应阈值即计一拍；'
                        '间隔 < 200ms 的去抖（一次挥拍会产生多个峰）',
             'compute': '全场峰值计数（含未能分类的拍）。后端区间累计用 SUM、场均用 AVG、'
                        '单场最高用 MAX；同时是 G2 配平门禁的分母',
             'artifact': 'training_sessions.stroke_count',
             'judge': '100 ≤ 值 ≤ 5000 次/场；与时长比 3~60 次/分钟；'
                      '低于 3 次/分钟通常意味着中间长时间休息或漏检',
             'surface': '/training › 个人分析 › KPI「累计击球量」; 逐场明细表「击球量」; '
                        '/training › 横向对比 › 矩阵与「单场击球量」双榜。'
                        '⚠️ 注意它是完整计数，而页面上的「击球构成占比」分母是逐拍样本数',
             'shown': True, 'required': True},
            {'field': '正手击球次数', 'key': 'forehand_count', 'unit': '次', 'sync': 'session',
             'source': 'CoreML · 分类输出（作 session 汇总列）',
             'acquire': '逐拍分类结果按 session 聚合计数',
             'compute': '分类结果分组计数；只用于配平校验与 App 展示，不参与后端聚合',
             'artifact': 'training_sessions.forehand_count',
             'judge': '参与 G2 配平：六类之和 = 击球总数（差额 ≤ 2%）',
             'surface': '未在管理页展示；仅作 G2 配平门禁输入 + App /api/prod/sessions › counts',
             'shown': False, 'required': True},
            {'field': '反手击球次数', 'key': 'backhand_count', 'unit': '次', 'sync': 'session',
             'source': 'CoreML · 分类输出（作 session 汇总列）',
             'acquire': '逐拍分类结果按 session 聚合计数',
             'compute': '同正手，分组计数',
             'artifact': 'training_sessions.backhand_count',
             'judge': '同正手；正反手之和为 0 时整场无效（说明分类器未工作）',
             'surface': '未在管理页展示；仅作 G2 配平门禁输入 + App /api/prod/sessions › counts',
             'shown': False, 'required': True},
            {'field': '发球次数', 'key': 'serve_count', 'unit': '次', 'sync': 'session',
             'source': 'CoreML · 分类输出（作 session 汇总列）',
             'acquire': '逐拍分类结果按 session 聚合计数；发球判定额外结合「站位固定 + 高抛」特征',
             'compute': '同正手，分组计数',
             'artifact': 'training_sessions.serve_count',
             'judge': '只能出现在每局开始段；若一场发球数 > 200 判定为误检（把高压当发球）',
             'surface': '未在管理页展示；仅作 G2 配平门禁输入 + App /api/prod/sessions › counts',
             'shown': False, 'required': True},
            {'field': '切削次数', 'key': 'slice_count', 'unit': '次', 'sync': 'session',
             'source': 'CoreML · 分类输出（作 session 汇总列）',
             'acquire': '逐拍分类结果按 session 聚合计数',
             'compute': '同正手，分组计数',
             'artifact': 'training_sessions.slice_count',
             'judge': '同配平门禁；切削与反手击球轨迹相似，置信度 < 0.7 时归入反手',
             'surface': '未在管理页展示；仅作 G2 配平门禁输入 + App /api/prod/sessions › counts',
             'shown': False, 'required': True},
            {'field': '截击次数', 'key': 'volley_count', 'unit': '次', 'sync': 'session',
             'source': 'CoreML · 分类输出（作 session 汇总列）',
             'acquire': '逐拍分类结果按 session 聚合计数',
             'compute': '同正手，分组计数',
             'artifact': 'training_sessions.volley_count',
             'judge': '同配平门禁；截击通常平均球速低于抽球，若高于正手均速需人工复核',
             'surface': '未在管理页展示；仅作 G2 配平门禁输入 + App /api/prod/sessions › counts',
             'shown': False, 'required': True},
            {'field': '高压球次数', 'key': 'smash_count', 'unit': '次', 'sync': 'session',
             'source': 'CoreML · 分类输出（作 session 汇总列）',
             'acquire': '逐拍分类结果按 session 聚合计数',
             'compute': '同正手，分组计数',
             'artifact': 'training_sessions.smash_count',
             'judge': '同配平门禁；单场一般 ≤ 20 次，超出判定为与发球混淆',
             'surface': '未在管理页展示；仅作 G2 配平门禁输入 + App /api/prod/sessions › counts',
             'shown': False, 'required': True},
            {'field': '单拍球速', 'key': 'speed_kmh', 'unit': 'km/h', 'sync': 'stroke',
             'source': 'CoreML · 拍头线速度估计',
             'acquire': '由挥拍峰值角速度 × 臂长推算拍头线速度，再用球的飞行时间做标定校正'
                        '（半径口径已拍板 1.05m）',
             'compute': '|ω| × r × 3.6（ω=腕部陀螺仪合成角速度，r=1.05m）；'
                        '后端按 stroke_type 分组算 AVG / MAX',
             'artifact': 'stroke_records.speed_kmh',
             'judge': '10 ≤ 值 ≤ 260 km/h；发球 100~240、抽球 40~180 为常见区间；'
                      '越界丢弃并计入 invalid_count',
             'surface': '仅 /training › 横向对比 › 「切削球速」榜与技术分析里的切削均速用到'
                        '（会话表没有切削列，只能用样本）。'
                        '**其余所有球速展示一律取「整场球质汇总」组，避免抽样偏差**；'
                        'App 单场详情按逐拍返回 speed_kmh',
             'shown': True, 'required': True},
            {'field': '击球时间偏移', 'key': 'impact_ms', 'unit': '毫秒 (ms)', 'sync': 'stroke',
             'source': '派生 · 相对会话起点',
             'acquire': 'impact 时刻 − session.started_at，统一到会话内毫秒轴',
             'compute': '(击球瞬时 − 会话起始) × 1000，取整到毫秒。'
                        '⚠️ 后端只原样落库，**没有**用它做任何时间线/离散序列图',
             'artifact': 'stroke_records.impact_ms',
             'judge': '0 ≤ 值 ≤ 训练时长×1000；用于还原击球时间线，越界说明时基错乱',
             'surface': '未在任何页面展示（/training 的「每拍球速与出球分布对比」是设计稿静态图，与数据无关）；'
                        '仅 App 单场详情按逐拍返回',
             'shown': False, 'required': False},
            {'field': '会话内序号', 'key': 'seq_in_session', 'unit': '整数', 'sync': 'stroke',
             'source': '应用层 · 自增',
             'acquire': '每场从 1 开始单调递增，服务端据此排序还原时间线',
             'compute': '采集端自增原值；服务端只作 ORDER BY 键，不参与数值计算',
             'artifact': 'stroke_records.seq_in_session',
             'judge': '同一 session 内不得重复（服务端以 (session_id, seq) 建唯一索引校验）',
             'surface': '不直接展示；是 App 单场详情逐拍列表的排序依据',
             'shown': False, 'required': True},
            {'field': '算法置信度', 'key': 'confidence', 'unit': '0~1', 'sync': 'stroke',
             'source': 'CoreML · 输出概率',
             'acquire': '分类器 softmax 最大概率值',
             'compute': 'softmax 取最大概率；后端聚合出整场均值但仍未渲染',
             'artifact': 'stroke_records.confidence',
             'judge': 'G3：< 0.60 不参与统计；整场均值 < 0.70 则该场分类结论不对外展示',
             'surface': '未在任何页面展示；本质是 G3 门禁的输入，另随 App 单场详情逐拍返回',
             'shown': False, 'required': True},
            {'field': '异常抖动标记', 'key': 'anomaly', 'unit': '布尔', 'sync': 'stroke',
             'source': '派生 · 传感器自检',
             'acquire': '检测到加速度突变（摔倒/撞击/摘表）时置 1',
             'compute': '加速度突变检测置位，不参与聚合',
             'artifact': 'stroke_records.anomaly',
             'judge': '为 1 的样本不参与球速统计；单场占比 > 10% 时整场标 suspect',
             'surface': '未在任何页面展示；仅 App 单场详情按逐拍返回',
             'shown': False, 'required': False},
            {'field': '达标击球占比', 'key': 'hit_rate', 'unit': '%', 'sync': 'derive',
             'source': '派生 · 逐拍算法置信度聚合',
             'acquire': '统计 confidence ≥ 0.60 的击球数，占全部已识别击球的比例',
             'compute': 'Σ(confidence ≥ 0.60) ÷ 识别击球总数 × 100，写入学员档案随 '
                        'student_profile 上行。'
                        '⚠️ 2026-10-03 **改口径**：原注释写「有效击球占比」，字面会被读成'
                        '「球是否落在界内」—— 那是落点，需视觉，腕表测不到；'
                        '现口径只依赖**分类器置信度**，是腕部侧完全可得的量',
             'artifact': 'students.hit_rate',
             'judge': '0 ≤ 值 ≤ 100。与 G3 门禁共用 0.60 阈值：'
                      '< 60 说明该学员整体识别质量偏低，其球速结论需标注不可信',
             'surface': '未在任何页面展示（仅落库）',
             'shown': False, 'required': False},
        ],
    },
    {
        'key': 'physio', 'name': '生理与恢复', 'icon': 'favorite',
        'framework': 'HealthKit',
        'desc': '心率是全场唯一「连续生理信号」，既是强度指标也是质量校验的锚点。'
                '所有生理量都必须先过 G4（区分缺失与无效）。'
                '⚠️ 本组只有平均/最高心率、步数、跑动距离会随会话上行；'
                '其余（HRV / 静息心率 / 呼吸频率 / VO₂max / 体重）是敏感健康数据，只在设备本地用。',
        'rows': [
            {'field': '平均心率', 'key': 'avg_hr', 'unit': 'BPM', 'sync': 'session',
             'source': 'HealthKit · HKQuantityTypeIdentifierHeartRate',
             'acquire': 'HKStatisticsQuery，按时间窗取 .discreteAverage',
             'compute': '时间窗内离散样本算术平均；后端区间取各场该值的均值（AVG），'
                        '并据此判定强度档位',
             'artifact': 'training_sessions.avg_hr',
             'judge': '60 ≤ 值 ≤ 200 BPM；且必须满足 max_hr > avg_hr；'
                      '手表未佩戴导致整段缺失时标 missing，不写 0',
             'surface': '/training › 个人分析 › KPI「平均 / 峰值心率」左值; 逐场明细表「平均心率」; '
                        '技术分析「整体强度偏高 / 偏低」条目; '
                        '/training › 横向对比 › 矩阵与「平均心率」双榜（该榜刻意标 better=None）; '
                        'App /api/prod/sessions › avgHeartRate',
             'shown': True, 'required': True},
            {'field': '最高心率', 'key': 'max_hr', 'unit': 'BPM', 'sync': 'session',
             'source': 'HealthKit · HKQuantityTypeIdentifierHeartRate',
             'acquire': '同一时间窗取 .discreteMax',
             'compute': '时间窗内最大值；后端区间取各场最高心率的最大值',
             'artifact': 'training_sessions.max_hr',
             'judge': '必须 ≥ avg_hr；> 220 − 年龄 视为传感器噪点，按次高值回退',
             'surface': '/training › 个人分析 › KPI「平均 / 峰值心率」峰值位; '
                        '逐场明细表平均心率后的「峰值」; 技术分析心率条目正文; '
                        'App /api/prod/sessions › maxHeartRate',
             'shown': True, 'required': True},
            {'field': '心率恢复 (HRR60)', 'key': 'hrr_60', 'unit': 'BPM', 'sync': 'derive',
             'source': '派生 · 由心率序列计算',
             'acquire': '一次高强度相持结束后第 60 秒心率相对峰值下降幅度',
             'compute': '口径：HRR60 = 峰值心率 − 峰值后第 60 秒心率。'
                        '⚠️ **后端未实现该计算**，analytics 中不存在此指标',
             'artifact': '不落库（derive）。后端也未派生',
             'pending': '契约定义了算法但未实现；需要先有逐秒心率序列落库才能算',
             'judge': '0 ≤ 值 ≤ 60 BPM；下降 < 12 提示恢复能力不足；'
                      '为负值说明期间还在持续高强度（分段错误）',
             'surface': '未在任何页面展示（无计算、无落库、无渲染）',
             'shown': False, 'required': False},
            {'field': '心率变异性 (SDNN)', 'key': 'hrv_sdnn', 'unit': 'ms', 'sync': 'local',
             'source': 'HealthKit · HKQuantityTypeIdentifierHeartRateVariabilitySDNN',
             'acquire': '训练结束静息 5 分钟后的 SDNN 样本（HealthKit 自动采集）',
             'compute': 'HealthKit 算好原值；**不进后端**，仅在设备内与个人基线比较',
             'artifact': '不落库（local，敏感健康数据）',
             'judge': '10 ≤ 值 ≤ 200 ms；明显低于个人基线 30% 以上提示疲劳积累；'
                      '⚠️ 属敏感健康数据，默认不上行，只在设备本地用于恢复建议',
             'surface': '未在任何页面展示（按设计不上行）',
             'shown': False, 'required': False},
            {'field': '静息心率', 'key': 'resting_hr', 'unit': 'BPM', 'sync': 'local',
             'source': 'HealthKit · HKQuantityTypeIdentifierRestingHeartRate',
             'acquire': 'HealthKit 每日自动计算，取训练当日值',
             'compute': 'HealthKit 原值；设备内与个人 7 日均值比较，不上行',
             'artifact': '不落库（local）',
             'judge': '35 ≤ 值 ≤ 110 BPM；与个人 7 日均值比较，突增 > 7 BPM 提示未恢复',
             'surface': '未在任何页面展示（按设计不上行）',
             'shown': False, 'required': False},
            {'field': '呼吸频率', 'key': 'respiratory_rate', 'unit': '次/分', 'sync': 'local',
             'source': 'HealthKit · HKQuantityTypeIdentifierRespiratoryRate',
             'acquire': '睡眠期间自动采集（watchOS 8+）',
             'compute': 'HealthKit 原值，设备内使用',
             'artifact': '不落库（local）',
             'judge': '8 ≤ 值 ≤ 30 次/分；越界丢弃',
             'surface': '未在任何页面展示（按设计不上行）',
             'shown': False, 'required': False},
            {'field': '最大摄氧量 (VO₂max)', 'key': 'vo2max', 'unit': 'ml/kg·min', 'sync': 'local',
             'source': 'HealthKit · HKQuantityTypeIdentifierVO2Max',
             'acquire': '系统在户外步行/跑步时估算，网球训练本身不产出该样本',
             'compute': 'HealthKit 原值，设备内使用',
             'artifact': '不落库（local）',
             'judge': '20 ≤ 值 ≤ 80；更新频率低（月级），只作长期趋势参考，不与单场关联',
             'surface': '未在任何页面展示（按设计不上行）',
             'shown': False, 'required': False},
            {'field': '步数', 'key': 'step_count', 'unit': '步', 'sync': 'local',
             'source': 'HealthKit · HKQuantityTypeIdentifierStepCount',
             'acquire': '按训练时间窗取累加值',
             'compute': '时间窗内累加。⚠️ 该值目前**不上行**，后端无法回读，'
                        '也不能用它做跑动距离回退重算',
             'artifact': '不落库：**training_sessions 里没有 step_count 列**，'
                         '同步白名单与 ingest 也未登记',
             'pending': '原目录误标为「会话级上行」。若要用它做距离兜底，需先给 '
                        'training_sessions 建列并在 sync.SYNCABLE 登记',
             'judge': '0 ≤ 值 ≤ 20000；与跑动距离一致性校验（步幅 0.4~1.2 m/步）',
             'surface': '未在任何页面展示（采了不上行、无落库）',
             'shown': False, 'required': False},
            {'field': '跑动距离', 'key': 'distance_km', 'unit': '公里 (km)', 'sync': 'session',
             'source': 'HealthKit · HKQuantityTypeIdentifierDistanceWalkingRunning',
             'acquire': '同一时间窗取累加值',
             'compute': '时间窗内累加（HealthKit 原值。若异常则按 步数×步幅 回退——'
                        '但步数当前不上行，此回退在后端无法执行）',
             'artifact': 'training_sessions.distance_km',
             'judge': '0 ≤ 值 ≤ 10 km/场；与时长比 0.02~0.25 km/min；'
                      '超出多为室内定位漂移，按步数×步幅回退重算',
             'surface': '未在管理页展示；仅 App /api/prod/sessions › distanceKm',
             'shown': False, 'required': False},
            {'field': '体重', 'key': 'body_mass', 'unit': '公斤 (kg)', 'sync': 'local',
             'source': 'HealthKit · HKQuantityTypeIdentifierBodyMass',
             'acquire': 'HKHealthStore 取最近一条记录',
             'compute': '原值；只参与卡路里与代谢当量换算，换算在设备侧完成',
             'artifact': '不落库（local）',
             'judge': '30 ≤ 值 ≤ 200 kg；只用于卡路里与代谢当量换算，不展示、不上行',
             'surface': '未在任何页面展示（按设计不上行）',
             'shown': False, 'required': False},
        ],
    },
    {
        'key': 'context', 'name': '环境与位置', 'icon': 'location_on',
        'framework': 'CoreLocation + WeatherKit',
        'desc': '解释「为什么这场数据反常」的关键上下文 —— 温度、海拔、场地都会显著影响球速读数。'
                '本组全部为 derive（后端不落库），当前仅存在于口径定义中，尚未在任何页面展示。',
        'rows': [
            {'field': 'GPS 轨迹', 'key': 'gps_track', 'unit': '坐标序列', 'sync': 'local',
             'source': 'CoreLocation · CLLocationManager',
             'acquire': '训练期间 1Hz 采集，做抽稀后仅保留球场地块级精度',
             'compute': '抽稀（保留地块级）→ 只用于反查场地，序列本身不上行',
             'artifact': '不落库（local，隐私）',
             'judge': '定位精度（horizontalAccuracy）> 50m 的点丢弃；'
                      '⚠️ 精确轨迹属隐私数据，默认本地留存不外传',
             'surface': '未在任何页面展示（按设计不上行）',
             'shown': False, 'required': False},
            {'field': '场地海拔', 'key': 'altitude_m', 'unit': '米 (m)', 'sync': 'derive',
             'source': 'CoreLocation · GPS 高程 或 CMAltimeter',
             'acquire': '取训练时段高程中位数',
             'compute': '时段内高程中位数。⚠️ 后端未实现，也未落库',
             'artifact': '不落库（derive）。后端也未派生',
             'pending': '口径已定义但后端未落地；高原球速归一化因此尚无法自动完成',
             'judge': '−100 ≤ 值 ≤ 4000 m；高原球速更快，用于球速读数归一化说明',
             'surface': '未在任何页面展示（无落库）',
             'shown': False, 'required': False},
            {'field': '环境温度', 'key': 'temperature_c', 'unit': '摄氏度', 'sync': 'derive',
             'source': 'WeatherKit（按训练时间与地点）',
             'acquire': '训练时段的逐小时温度，取中位数',
             'compute': '逐小时温度中位数。⚠️ 后端未接入 WeatherKit，未落库',
             'artifact': '不落库（derive）',
             'pending': '需要先接 WeatherKit 并建列',
             'judge': '−20 ≤ 值 ≤ 50 ℃；低温会显著降低球压与弹性，'
                      '跨场比较球速时应带上这一项做说明',
             'surface': '未在任何页面展示（无落库）',
             'shown': False, 'required': False},
            {'field': '相对湿度', 'key': 'humidity_pct', 'unit': '%', 'sync': 'derive',
             'source': 'WeatherKit',
             'acquire': '训练时段逐小时湿度中位数',
             'compute': '逐小时湿度中位数。⚠️ 后端未接入，未落库',
             'artifact': '不落库（derive）',
             'pending': '同环境温度，需先接 WeatherKit',
             'judge': '0 ≤ 值 ≤ 100；湿度影响球重与毛毡摩擦，间接影响球的飞行与弹跳',
             'surface': '未在任何页面展示（无落库）',
             'shown': False, 'required': False},
            {'field': '风速', 'key': 'wind_speed_ms', 'unit': '米/秒 (m/s)', 'sync': 'derive',
             'source': 'WeatherKit',
             'acquire': '训练时段平均风速与主导风向',
             'compute': '时段平均风速。⚠️ 后端未接入，未落库',
             'artifact': '不落库（derive）',
             'pending': '同环境温度，需先接 WeatherKit',
             'judge': '0 ≤ 值 ≤ 25 m/s；> 5 m/s 时应按风向对球速做标注'
                      '（顺风场次的球速不可与无风场次直接并列排行）',
             'surface': '未在任何页面展示（无落库）',
             'shown': False, 'required': False},
        ],
    },
    {
        'key': 'device', 'name': '设备与数据质量', 'icon': 'memory',
        'framework': '应用层 + 传感器自检',
        'desc': '没有这一层，前面所有数字都无法判断可信度。'
                '设备信息随每次训练一起上传，用于事后解释异常批次。'
                '⚠️ 本组几乎全部是「仅本地」——它们用于采集端自检，后端不落库、页面不展示。',
        'rows': [
            {'field': '设备型号', 'key': 'watch_model', 'unit': '文本', 'sync': 'local',
             'source': '应用层 · WKInterfaceDevice',
             'acquire': '读取硬件型号标识（如 Watch Ultra 2）',
             'compute': '原值，不计算',
             'artifact': '不落库（local）。⚠️ 页面上的「设备型号」读的是 '
                         'students.watch_model —— 那是人工维护的档案字段，与这里不是同一条路径',
             'judge': '不同代的陀螺仪量程与采样率不同，'
                      '跨机型比较球速前需确认采样率一致',
             'surface': '/training › 个人分析 › 学员档案头第三行（读 students.watch_model，'
                        '非本字段）；本字段本身未在任何页面展示',
             'shown': False, 'required': True},
            {'field': '系统版本', 'key': 'os_version', 'unit': '文本', 'sync': 'local',
             'source': '应用层 · WKInterfaceDevice.systemVersion',
             'acquire': 'watchOS / iOS 版本号',
             'compute': '原值，不计算',
             'artifact': '不落库（local）',
             'judge': 'HealthKit 字段可用性随版本变化（如 PhysicalEffort 需 iOS 16+），'
                      '版本过低时对应字段应标 unsupported 而非 missing',
             'surface': '未在任何页面展示（仅本地）',
             'shown': False, 'required': True},
            {'field': '佩戴状态', 'key': 'wrist_on', 'unit': '布尔', 'sync': 'local',
             'source': '派生 · 由心率信号质量推断',
             'acquire': '心率传感器持续无信号且加速度长时间静止 → 判定未佩戴',
             'compute': '「心率无信号 + 加速度静止」双条件推断。'
                        '⚠️ 后端未实现该判定，G4 目前只是口径约定',
             'artifact': '不落库（local）',
             'judge': 'G4 的输入：未佩戴期间的心率标 missing，'
                      '且该时段击球样本置信度整体下调',
             'surface': '未在任何页面展示（仅本地）',
             'shown': False, 'required': False},
            {'field': '采样率', 'key': 'sample_rate_hz', 'unit': '赫兹 (Hz)', 'sync': 'local',
             'source': 'CoreMotion · CMDeviceMotion',
             'acquire': '实际生效的惯导采样率（watchOS 常见 50 / 100 Hz）',
             'compute': '原值，不计算。用于判断本场球速上限是否可信',
             'artifact': '不落库（local）',
             'judge': '< 50 Hz 时高速球拍可能被欠采样，'
                      '该场球速上限估计不可信，需标注 low_fidelity',
             'surface': '未在任何页面展示（仅本地）',
             'shown': False, 'required': True},
            {'field': '丢包率', 'key': 'drop_rate', 'unit': '%', 'sync': 'local',
             'source': '派生 · 采样序号连续性',
             'acquire': '按期望样本数与实际样本数之比计算',
             'compute': '1 − 实际样本数 ÷ 期望样本数；采集端自算',
             'artifact': '不落库（local）',
             'judge': '> 5% 则该场标 suspect；> 15% 整场作废，不进入排行与趋势',
             'surface': '未在任何页面展示（仅本地）',
             'shown': False, 'required': True},
            {'field': '电量', 'key': 'battery_pct', 'unit': '%', 'sync': 'local',
             'source': '应用层 · WKInterfaceDevice.batteryLevel',
             'acquire': '训练开始与结束各取一次',
             'compute': '原值，不计算',
             'artifact': '不落库（local）',
             'judge': '< 10% 时需警惕系统降频导致采样率下跌（与 sample_rate 联合判断）',
             'surface': '未在任何页面展示（仅本地）',
             'shown': False, 'required': False},
            {'field': '算法版本', 'key': 'algorithm_version', 'unit': '文本', 'sync': 'session',
             'source': '应用层 · 随包发布的模型版本号',
             'acquire': '如 CoreML-Tennis-v4.2.1',
             'compute': '原值。后端在 analytics 里能取到常模表的版本（bm_disp.version），'
                        '但没有模板渲染它',
             'artifact': '⚠️ 不在 training_sessions —— 该表没有这一列。'
                         '实际只落在 nt_benchmarks.algorithm_version（段位常模表）',
             'pending': '原目录误标为「会话级上行」。要按版本分组排行，需先给 '
                        'training_sessions 建列并纳入同步白名单',
             'judge': '⚠️ 跨版本的历史数据不可直接排行：'
                      '模型升级会整体改变球速标定。排行页必须按 version 分组或标注',
             'surface': '未在任何页面展示。设置页「硬件接入」里的 '
                        'CoreML-Tennis-v4.2.1 是**写死的展示值**，不读数据库',
             'shown': False, 'required': True},
        ],
    },
]

# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
# 上行目标的中文说明（模板里直接用，避免在 Jinja 里写条件）
SYNC_LABEL = {
    'session': {'label': '会话级上行', 'tone': 'text-primary-fixed',
                'hint': '写入 training_sessions，一场一行'},
    'stroke': {'label': '逐拍上行', 'tone': 'text-secondary',
               'hint': '写入 stroke_records，按 seq_in_session 排序'},
    'derive': {'label': '后端派生', 'tone': 'text-tertiary-fixed-dim',
               'hint': '不单独存储，由后端聚合时现算'},
    'local': {'label': '仅本地', 'tone': 'text-outline',
              'hint': '隐私或体量原因不上行，仅设备内使用'},
}


def _cat_of(key):
    """字段 key → 分类元数据。未归类时回落到「会话概要」并标记 unclassified。"""
    ck = CATEGORY_OF.get(key)
    if ck and ck in CATEGORY_MAP:
        return CATEGORY_MAP[ck], ''
    return CATEGORY_MAP['session'], '未分类（请在 CATEGORY_OF 中补登）'


def _decorate(rows, group_key=None):
    out = []
    for i, r in enumerate(rows, 1):
        item = dict(r)
        item['idx'] = i                              # 组内序号
        item['group_key'] = group_key or ''
        item['sync_meta'] = SYNC_LABEL.get(r.get('sync'), SYNC_LABEL['local'])
        # shown=False 的行在页面上加一个「无展示」标记，避免读者以为漏画了
        item['shown'] = bool(r.get('shown'))
        item['pending'] = (r.get('pending') or '').strip()
        # 分类（统一表的「备注」列）
        cat, warn = _cat_of(r.get('key'))
        item['cat_key'] = cat['key']
        item['cat_label'] = cat['label']
        item['cat_icon'] = cat['icon']
        item['cat_tone'] = cat['tone']
        item['cat_warn'] = warn
        out.append(item)
    return out


def groups():
    """按**采集分组**返回目录（页面与接口共用）。

    统一表按**分类**筛选，见 flat_rows()；groups() 保留分组维度是因为
    「从哪个采集口来」与「是什么性质的量」是两个不同的问题，
    /api/datasources 的调用方（iOS / 小程序）常按采集口对齐字段。
    """
    out = []
    for g in GROUPS:
        item = dict(g)
        item['rows'] = _decorate(g['rows'], group_key=g['key'])
        item['count'] = len(item['rows'])
        item['required_count'] = sum(1 for r in item['rows'] if r.get('required'))
        item['shown_count'] = sum(1 for r in item['rows'] if r['shown'])
        item['pending_count'] = sum(1 for r in item['rows'] if r['pending'])
        out.append(item)
    return out


def flat_rows():
    """**一张统一表**的全部字段，带全局编号与分类。

    编号规则：`MD-001` … 按「分组顺序 → 组内顺序」线性分配，全局唯一且稳定 ——
    分组或字段增删时，其后字段的编号会顺延，所以编号只用于**同一版本内**的引用
    （契约版本号见 contract/README.md）。页面上的分类筛选就按 `cat_key` 过滤。
    """
    out = []
    n = 0
    for g in groups():
        for r in g['rows']:
            n += 1
            item = dict(r)
            item['no'] = 'MD-%03d' % n
            item['group_name'] = g['name']
            out.append(item)
    return out


def category_counts(rows=None):
    """各分类的字段数（筛选栏上的计数徽章）。"""
    rows = rows if rows is not None else flat_rows()
    counts = {c['key']: 0 for c in CATEGORIES}
    for r in rows:
        counts[r['cat_key']] = counts.get(r['cat_key'], 0) + 1
    out = []
    for c in CATEGORIES:
        item = dict(c)
        item['count'] = counts.get(c['key'], 0)
        out.append(item)
    return out


def summary():
    """总量统计，用于页面顶部的概览数字。"""
    gs = groups()
    all_rows = [r for g in gs for r in g['rows']]
    by_sync = {}
    for r in all_rows:
        by_sync[r.get('sync')] = by_sync.get(r.get('sync'), 0) + 1
    return {
        'total_groups': len(gs),
        'total_fields': len(all_rows),
        'required_fields': sum(1 for r in all_rows if r.get('required')),
        'session_fields': by_sync.get('session', 0),
        'stroke_fields': by_sync.get('stroke', 0),
        'derive_fields': by_sync.get('derive', 0),
        'local_fields': by_sync.get('local', 0),
        # 口径统计：能看见的 / 看不见的 / 待后端落地的
        'shown_fields': sum(1 for r in all_rows if r['shown']),
        'hidden_fields': sum(1 for r in all_rows if not r['shown']),
        'pending_fields': sum(1 for r in all_rows if r['pending']),
        # 2026-10-02：分类维度 + 已移除字段数
        'total_categories': len(CATEGORIES),
        'removed_fields': len(REMOVED),
        'unclassified_fields': sum(1 for r in all_rows if r.get('cat_warn')),
        'gates': len(GATES),
        'notes': len(ACQUISITION_NOTES),
        'frameworks': ['HealthKit', 'CoreMotion', 'CoreML',
                       'CoreLocation', 'WeatherKit', '应用层'],
    }


def catalog():
    """/api/datasources 的完整响应体。"""
    rows = flat_rows()
    return {
        'pipeline': PIPELINE,
        'gates': GATES,
        'notes': ACQUISITION_NOTES,
        'audit': AUDIT,
        'categories': category_counts(rows),
        'groups': groups(),
        # 2026-10-02 起新增：统一扁平表（带编号 + 分类），页面/接口共用
        'rows': rows,
        'removed': REMOVED,
        'summary': summary(),
    }
