# -*- coding: utf-8 -*-
"""Apple Watch → 后端 的**数据源目录**（唯一的字段事实源）。

这个文件同时驱动两件事：
  1. `/settings` 页面的「数据源与采集口径」区块渲染
  2. `GET /api/datasources` 接口 —— 供 iOS / 小程序端对齐字段名，
     避免两端各自起名导致同步字段漂移（历史教训：客户端叫 speed、
     服务端叫 speed_kmh，推送后字段对不上，数据静默丢失）

每个字段回答四个问题：
  · source   数据从哪来（HealthKit / CoreMotion / CoreML 派生 / 应用层 / 设备）
  · acquire  具体怎么取（类型标识符、API、采样率）
  · judge    取到之后怎么判定（有效 / 可疑 / 丢弃）
  · sync     是否进入同步协议，以及落到哪张表
             session = training_sessions · stroke = stroke_records
             derive  = 只由后端聚合算出、不单独存储
             local   = 仅设备本地使用，不上行（隐私或体量原因）

单位一律用 **SI 或行业惯用单位**并与数据库列名一致，不要在中途换单位；
换算是展示层的事（见 analytics.fmt）。
"""

# --------------------------------------------------------------------------- #
# 采集链路（Watch → iPhone → 同步队列 → 后端）
# --------------------------------------------------------------------------- #
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
     'body': '逐拍算法置信度 < 0.60 的样本不参与球速/转速统计（仍保留原始行以便回溯），'
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
             'judge': '0 < 值 ≤ 6h（网球单次上限）；与 ended−started 之差 ≤ 2s；否则以 HealthKit 为准', 'required': True},
            {'field': '开始时间', 'key': 'started_at', 'unit': 'ISO8601 UTC', 'sync': 'session',
             'source': 'HealthKit · HKWorkout.startDate',
             'acquire': 'HKWorkout.startDate，统一转 UTC 并保留毫秒',
             'judge': '不得晚于 ended_at；不得晚于服务器当前时间（时钟回拨检测）', 'required': True},
            {'field': '结束时间', 'key': 'ended_at', 'unit': 'ISO8601 UTC', 'sync': 'session',
             'source': 'HealthKit · HKWorkout.endDate',
             'acquire': 'HKWorkout.endDate，统一转 UTC',
             'judge': '必须晚于 started_at；与 started_at 同日或跨夜均可，时长仍受 G1 约束', 'required': True},
            {'field': '佩戴手腕', 'key': 'worn_wrist', 'unit': '枚举', 'sync': 'session',
             'source': '应用层 · 用户选择 / iPhone 下发',
             'acquire': 'left 左手戴表 / right 右手戴表；开始训练前在手表或 iPhone 上设定',
             'judge': '枚举内取值。⚠️ 必须正确：正/反手的旋转符号完全依赖它，'
                      '设反了会把正手整场判成反手，且「没有任何其它字段能交叉发现」', 'required': True},
            {'field': '数据来源端', 'key': 'source', 'unit': '枚举', 'sync': 'session',
             'source': '应用层 · 接入时标记',
             'acquire': 'acemate（自有 App 采集）/ netpulse_watch（Apple Watch 采集端接入）',
             'judge': '枚举内取值；与 external_id 组成 (source, external_id) 部分唯一索引，'
                      '同一场重复上传只能落一行', 'required': True},
            {'field': '外部会话 ID', 'key': 'external_id', 'unit': '文本 (UUID)', 'sync': 'session',
             'source': '应用层 · 采集端原生会话 ID',
             'acquire': '取采集端 MatchSession.id，落库时加前缀映射（nps-<uuid>），'
                        '避免与 AceMate 自身生成的 id 相撞',
             'judge': '非空且唯一；同一 external_id 重复上传时派生出的 operation_id 相同，'
                      '会命中幂等闸并返回 duplicate，「不会重复入库」', 'required': True},
            {'field': '活动能量 (卡路里)', 'key': 'calories_kcal', 'unit': 'kcal', 'sync': 'session',
             'source': 'HealthKit · HKQuantityTypeIdentifierActiveEnergyBurned',
             'acquire': 'HKStatisticsQuery，按 session 时间窗取 .cumulativeSum，单位换算到 kcal',
             'judge': '0 < 值 < 2500 kcal；与时长比 4~20 kcal/min；超出多为未绑定体重或误判运动类型', 'required': True},
            {'field': '基础代谢能量', 'key': 'basal_kcal', 'unit': 'kcal', 'sync': 'derive',
             'source': 'HealthKit · HKQuantityTypeIdentifierBasalEnergyBurned',
             'acquire': '同一时间窗取累加值；只用于区分「净消耗」与「总消耗」',
             'judge': '恒小于活动能量；若大于则判定为设备心率脱落导致，整场标 suspect', 'required': False},
            {'field': '训练形式', 'key': 'session_type', 'unit': '枚举', 'sync': 'session',
             'source': '应用层 · 用户选择',
             'acquire': '手表开始训练时四选一：drill 专项 / match 实战 / rally 对拉 / serve 发球',
             'judge': '必须落在枚举内；为空时按「击球构成」自动推断并标记 inferred', 'required': True},
            {'field': '场地类型', 'key': 'court_type', 'unit': '枚举', 'sync': 'session',
             'source': '应用层 · 用户选择 或 GPS+POI 匹配',
             'acquire': 'hard 硬地 / clay 红土 / grass 草地 / indoor 室内；'
                        '未选择时用定位落点匹配已知球场库',
             'judge': '枚举内取值；POI 匹配置信度 < 0.7 时回落为 unknown 而不是硬猜', 'required': False},
            {'field': '训练地点', 'key': 'location', 'unit': '文本', 'sync': 'session',
             'source': 'CoreLocation · CLGeocoder',
             'acquire': '取训练时段定位中位数坐标 → 逆地理编码为「城市 · 行政区」',
             'judge': '只保留到行政区级（不存精确坐标，隐私）；定位缺失时留空，不写「未知」占位', 'required': False},
            {'field': '心率区间分布', 'key': 'hr_zone', 'unit': 'JSON', 'sync': 'session',
             'source': '派生 · 由心率序列分箱',
             'acquire': '按最大心率百分比分 5 区（50/60/70/80/90%），统计各区停留秒数',
             'judge': '五区秒数之和 ≈ 训练时长（±5%）；不满足说明心率存在长时间缺失', 'required': False},
            {'field': '最长相持', 'key': 'rally_max', 'unit': '拍', 'sync': 'session',
             'source': '派生 · 由击球间隔序列',
             'acquire': '相邻击球间隔 < 2.5s 视为同一回合，取最长连续段长度',
             'judge': '2 ≤ 值 ≤ 120；> 120 多为漏检导致把两回合并成一段', 'required': False},
        ],
    },
    {
        'key': 'stroke', 'name': '击球识别与球质', 'icon': 'sports_tennis',
        'framework': 'CoreMotion + CoreML',
        'desc': '逐拍明细。惯导流做检测与分类，球速/转速由拍头线速度与球的飞行时间联合估计。'
                '⚠️ 逐拍明细在库里是「抽样存储」（一场约 10~20 条），'
                '而「击球总数」是手表统计的完整计数 —— 两者分母不同，做占比时必须用抽样数做分母。',
        'rows': [
            {'field': '击球总数', 'key': 'stroke_count', 'unit': '次', 'sync': 'session',
             'source': 'CoreMotion + 算法 · 峰值检测',
             'acquire': '三轴加速度计合矢量 + 陀螺仪角速度，过自适应阈值即计一拍；'
                        '间隔 < 200ms 的去抖（一次挥拍会产生多个峰）',
             'judge': '100 ≤ 值 ≤ 5000 次/场；与时长比 3~60 次/分钟；'
                      '低于 3 次/分钟通常意味着中间长时间休息或漏检', 'required': True},
            {'field': '正手击球次数', 'key': 'forehand_count', 'unit': '次', 'sync': 'session',
             'source': 'CoreML · 分类输出（作 session 汇总列）',
             'acquire': '逐拍分类结果按 session 聚合计数',
             'judge': '参与 G2 配平：六类之和 = 击球总数（差额 ≤ 2%）', 'required': True},
            {'field': '反手击球次数', 'key': 'backhand_count', 'unit': '次', 'sync': 'session',
             'source': 'CoreML · 分类输出（作 session 汇总列）',
             'acquire': '逐拍分类结果按 session 聚合计数',
             'judge': '同正手；正反手之和为 0 时整场无效（说明分类器未工作）', 'required': True},
            {'field': '发球次数', 'key': 'serve_count', 'unit': '次', 'sync': 'session',
             'source': 'CoreML · 分类输出（作 session 汇总列）',
             'acquire': '逐拍分类结果按 session 聚合计数；发球判定额外结合「站位固定 + 高抛」特征',
             'judge': '只能出现在每局开始段；若一场发球数 > 200 判定为误检（把高压当发球）', 'required': True},
            {'field': '切削次数', 'key': 'slice_count', 'unit': '次', 'sync': 'session',
             'source': 'CoreML · 分类输出（作 session 汇总列）',
             'acquire': '逐拍分类结果按 session 聚合计数',
             'judge': '同配平门禁；切削与反手击球轨迹相似，置信度 < 0.7 时归入反手', 'required': True},
            {'field': '截击次数', 'key': 'volley_count', 'unit': '次', 'sync': 'session',
             'source': 'CoreML · 分类输出（作 session 汇总列）',
             'acquire': '逐拍分类结果按 session 聚合计数',
             'judge': '同配平门禁；截击通常平均球速低于抽球，若高于正手均速需人工复核', 'required': True},
            {'field': '高压球次数', 'key': 'smash_count', 'unit': '次', 'sync': 'session',
             'source': 'CoreML · 分类输出（作 session 汇总列）',
             'acquire': '逐拍分类结果按 session 聚合计数',
             'judge': '同配平门禁；单场一般 ≤ 20 次，超出判定为与发球混淆', 'required': True},
            {'field': '单拍球速', 'key': 'speed_kmh', 'unit': 'km/h', 'sync': 'stroke',
             'source': 'CoreML · 拍头线速度估计',
             'acquire': '由挥拍峰值角速度 × 臂长推算拍头线速度，再用球的飞行时间做标定校正',
             'judge': '10 ≤ 值 ≤ 260 km/h；发球 100~240、抽球 40~180 为常见区间；'
                      '越界丢弃并计入 invalid_count', 'required': True},
            {'field': '单拍转速', 'key': 'spin_rpm', 'unit': 'RPM', 'sync': 'stroke',
             'source': 'CoreML · 拍面角速度积分',
             'acquire': '拍面在撞击窗口内的角速度积分推算旋转，上旋/下旋/平击分别输出',
             'judge': '0 ≤ 上旋 ≤ 5000 RPM；下旋 ≤ 3000 RPM；'
                      '物理上上旋与下旋不可能同时显著（互斥校验）', 'required': False},
            {'field': '旋转类型', 'key': 'spin_type', 'unit': '枚举', 'sync': 'stroke',
             'source': 'CoreML · 分类输出',
             'acquire': 'top 上旋 / back 下旋 / flat 平击',
             'judge': '枚举内取值；与 spin_rpm 的一致性校验：flat 的转速应 < 800 RPM', 'required': False},
            {'field': '甜区命中', 'key': 'sweet_spot', 'unit': '布尔', 'sync': 'stroke',
             'source': 'CoreML · 撞击点估计',
             'acquire': '由拍面振动频谱的基频偏移推断撞击点，落在甜区半径内记 1',
             'judge': '布尔值；单场命中率 20%~98% 为合理区间，'
                      '> 98% 说明阈值过宽（几乎全部命中等于没测）', 'required': False},
            {'field': '落点深度', 'key': 'depth_m', 'unit': '米 (m)', 'sync': 'stroke',
             'source': 'CoreML · 弹道估计',
             'acquire': '由出球角度与速度积分弹道，估计落点距底线的深度',
             'judge': '0 ≤ 值 ≤ 24 m（全场长度）；> 20m 视为出界标 out，不参与深度统计', 'required': False},
            {'field': '落点区域', 'key': 'landing_zone', 'unit': '枚举', 'sync': 'stroke',
             'source': '派生 · 由 depth_m 分箱',
             'acquire': 'deep 深区（后 1/3）/ mid 中区 / short 短球（前 1/3）',
             'judge': '必须与 depth_m 的分箱结果一致（防两处口径漂移）', 'required': False},
            {'field': '左右偏差', 'key': 'lateral_offset_m', 'unit': '米 (m)', 'sync': 'stroke',
             'source': 'CoreML · 弹道估计',
             'acquire': '落点相对球场中轴的横向偏移，右正左负',
             'judge': '|值| ≤ 5.5 m（单打边线）；越界标 out', 'required': False},
            {'field': '过网高度', 'key': 'net_clearance_m', 'unit': '米 (m)', 'sync': 'stroke',
             'source': 'CoreML · 弹道估计',
             'acquire': '弹道在球网处的垂直高度减去网高（中心 0.914m，网柱 1.07m）',
             'judge': '−1.0 ≤ 值 ≤ 4.0 m；为负表示下网；> 4m 多见于高吊球或估计失准', 'required': False},
            {'field': '击球时间偏移', 'key': 'impact_ms', 'unit': '毫秒 (ms)', 'sync': 'stroke',
             'source': '派生 · 相对会话起点',
             'acquire': 'impact 时刻 − session.started_at，统一到会话内毫秒轴',
             'judge': '0 ≤ 值 ≤ 训练时长×1000；用于还原击球时间线，越界说明时基错乱', 'required': False},
            {'field': '会话内序号', 'key': 'seq_in_session', 'unit': '整数', 'sync': 'stroke',
             'source': '应用层 · 自增',
             'acquire': '每场从 1 开始单调递增，服务端据此排序还原时间线',
             'judge': '同一 session 内不得重复（服务端以 (session_id, seq) 建唯一索引校验）', 'required': True},
            {'field': '算法置信度', 'key': 'confidence', 'unit': '0~1', 'sync': 'stroke',
             'source': 'CoreML · 输出概率',
             'acquire': '分类器 softmax 最大概率值',
             'judge': 'G3：< 0.60 不参与统计；整场均值 < 0.70 则该场分类结论不对外展示', 'required': True},
            {'field': '异常抖动标记', 'key': 'anomaly', 'unit': '布尔', 'sync': 'stroke',
             'source': '派生 · 传感器自检',
             'acquire': '检测到加速度突变（摔倒/撞击/摘表）时置 1',
             'judge': '为 1 的样本不参与球速统计；单场占比 > 10% 时整场标 suspect', 'required': False},
        ],
    },
    {
        'key': 'physio', 'name': '生理与恢复', 'icon': 'favorite',
        'framework': 'HealthKit',
        'desc': '心率是全场唯一「连续生理信号」，既是强度指标也是质量校验的锚点。'
                '所有生理量都必须先过 G4（区分缺失与无效）。',
        'rows': [
            {'field': '平均心率', 'key': 'avg_hr', 'unit': 'BPM', 'sync': 'session',
             'source': 'HealthKit · HKQuantityTypeIdentifierHeartRate',
             'acquire': 'HKStatisticsQuery，按时间窗取 .discreteAverage',
             'judge': '60 ≤ 值 ≤ 200 BPM；且必须满足 max_hr > avg_hr；'
                      '手表未佩戴导致整段缺失时标 missing，不写 0', 'required': True},
            {'field': '最高心率', 'key': 'max_hr', 'unit': 'BPM', 'sync': 'session',
             'source': 'HealthKit · HKQuantityTypeIdentifierHeartRate',
             'acquire': '同一时间窗取 .discreteMax',
             'judge': '必须 ≥ avg_hr；> 220 − 年龄 视为传感器噪点，按次高值回退', 'required': True},
            {'field': '心率恢复 (HRR60)', 'key': 'hrr_60', 'unit': 'BPM', 'sync': 'derive',
             'source': '派生 · 由心率序列计算',
             'acquire': '一次高强度相持结束后第 60 秒心率相对峰值下降幅度',
             'judge': '0 ≤ 值 ≤ 60 BPM；下降 < 12 提示恢复能力不足；'
                      '为负值说明期间还在持续高强度（分段错误）', 'required': False},
            {'field': '心率变异性 (SDNN)', 'key': 'hrv_sdnn', 'unit': 'ms', 'sync': 'local',
             'source': 'HealthKit · HKQuantityTypeIdentifierHeartRateVariabilitySDNN',
             'acquire': '训练结束静息 5 分钟后的 SDNN 样本（HealthKit 自动采集）',
             'judge': '10 ≤ 值 ≤ 200 ms；明显低于个人基线 30% 以上提示疲劳积累；'
                      '⚠️ 属敏感健康数据，默认不上行，只在设备本地用于恢复建议', 'required': False},
            {'field': '静息心率', 'key': 'resting_hr', 'unit': 'BPM', 'sync': 'local',
             'source': 'HealthKit · HKQuantityTypeIdentifierRestingHeartRate',
             'acquire': 'HealthKit 每日自动计算，取训练当日值',
             'judge': '35 ≤ 值 ≤ 110 BPM；与个人 7 日均值比较，突增 > 7 BPM 提示未恢复', 'required': False},
            {'field': '呼吸频率', 'key': 'respiratory_rate', 'unit': '次/分', 'sync': 'local',
             'source': 'HealthKit · HKQuantityTypeIdentifierRespiratoryRate',
             'acquire': '睡眠期间自动采集（watchOS 8+）',
             'judge': '8 ≤ 值 ≤ 30 次/分；越界丢弃', 'required': False},
            {'field': '最大摄氧量 (VO₂max)', 'key': 'vo2max', 'unit': 'ml/kg·min', 'sync': 'local',
             'source': 'HealthKit · HKQuantityTypeIdentifierVO2Max',
             'acquire': '系统在户外步行/跑步时估算，网球训练本身不产出该样本',
             'judge': '20 ≤ 值 ≤ 80；更新频率低（月级），只作长期趋势参考，不与单场关联', 'required': False},
            {'field': '步数', 'key': 'step_count', 'unit': '步', 'sync': 'session',
             'source': 'HealthKit · HKQuantityTypeIdentifierStepCount',
             'acquire': '按训练时间窗取累加值',
             'judge': '0 ≤ 值 ≤ 20000；与跑动距离一致性校验（步幅 0.4~1.2 m/步）', 'required': False},
            {'field': '跑动距离', 'key': 'distance_km', 'unit': '公里 (km)', 'sync': 'session',
             'source': 'HealthKit · HKQuantityTypeIdentifierDistanceWalkingRunning',
             'acquire': '同一时间窗取累加值',
             'judge': '0 ≤ 值 ≤ 10 km/场；与时长比 0.02~0.25 km/min；'
                      '超出多为室内定位漂移，按步数×步幅回退重算', 'required': False},
            {'field': '体重', 'key': 'body_mass', 'unit': '公斤 (kg)', 'sync': 'local',
             'source': 'HealthKit · HKQuantityTypeIdentifierBodyMass',
             'acquire': 'HKHealthStore 取最近一条记录',
             'judge': '30 ≤ 值 ≤ 200 kg；只用于卡路里与代谢当量换算，不展示、不上行', 'required': False},
        ],
    },
    {
        'key': 'context', 'name': '环境与位置', 'icon': 'location_on',
        'framework': 'CoreLocation + WeatherKit',
        'desc': '解释「为什么这场数据反常」的关键上下文 —— 温度、海拔、场地都会显著影响球速读数。',
        'rows': [
            {'field': 'GPS 轨迹', 'key': 'gps_track', 'unit': '坐标序列', 'sync': 'local',
             'source': 'CoreLocation · CLLocationManager',
             'acquire': '训练期间 1Hz 采集，做抽稀后仅保留球场地块级精度',
             'judge': '定位精度（horizontalAccuracy）> 50m 的点丢弃；'
                      '⚠️ 精确轨迹属隐私数据，默认本地留存不外传', 'required': False},
            {'field': '场地海拔', 'key': 'altitude_m', 'unit': '米 (m)', 'sync': 'derive',
             'source': 'CoreLocation · GPS 高程 或 CMAltimeter',
             'acquire': '取训练时段高程中位数',
             'judge': '−100 ≤ 值 ≤ 4000 m；高原球速更快，用于球速读数归一化说明', 'required': False},
            {'field': '环境温度', 'key': 'temperature_c', 'unit': '摄氏度', 'sync': 'derive',
             'source': 'WeatherKit（按训练时间与地点）',
             'acquire': '训练时段的逐小时温度，取中位数',
             'judge': '−20 ≤ 值 ≤ 50 ℃；低温会显著降低球压与弹性，'
                      '跨场比较球速时应带上这一项做说明', 'required': False},
            {'field': '相对湿度', 'key': 'humidity_pct', 'unit': '%', 'sync': 'derive',
             'source': 'WeatherKit',
             'acquire': '训练时段逐小时湿度中位数',
             'judge': '0 ≤ 值 ≤ 100；湿度影响球重与毛毡摩擦，间接影响转速读数', 'required': False},
            {'field': '风速', 'key': 'wind_speed_ms', 'unit': '米/秒 (m/s)', 'sync': 'derive',
             'source': 'WeatherKit',
             'acquire': '训练时段平均风速与主导风向',
             'judge': '0 ≤ 值 ≤ 25 m/s；> 5 m/s 时应按风向对球速做标注'
                      '（顺风场次的球速不可与无风场次直接并列排行）', 'required': False},
        ],
    },
    {
        'key': 'device', 'name': '设备与数据质量', 'icon': 'memory',
        'framework': '应用层 + 传感器自检',
        'desc': '没有这一层，前面所有数字都无法判断可信度。'
                '设备信息随每次训练一起上传，用于事后解释异常批次。',
        'rows': [
            {'field': '设备型号', 'key': 'watch_model', 'unit': '文本', 'sync': 'local',
             'source': '应用层 · WKInterfaceDevice',
             'acquire': '读取硬件型号标识（如 Watch Ultra 2）',
             'judge': '不同代的陀螺仪量程与采样率不同，'
                      '跨机型比较球速前需确认采样率一致', 'required': True},
            {'field': '系统版本', 'key': 'os_version', 'unit': '文本', 'sync': 'local',
             'source': '应用层 · WKInterfaceDevice.systemVersion',
             'acquire': 'watchOS / iOS 版本号',
             'judge': 'HealthKit 字段可用性随版本变化（如 PhysicalEffort 需 iOS 16+），'
                      '版本过低时对应字段应标 unsupported 而非 missing', 'required': True},
            {'field': '佩戴状态', 'key': 'wrist_on', 'unit': '布尔', 'sync': 'local',
             'source': '派生 · 由心率信号质量推断',
             'acquire': '心率传感器持续无信号且加速度长时间静止 → 判定未佩戴',
             'judge': 'G4 的输入：未佩戴期间的心率标 missing，'
                      '且该时段击球样本置信度整体下调', 'required': False},
            {'field': '采样率', 'key': 'sample_rate_hz', 'unit': '赫兹 (Hz)', 'sync': 'local',
             'source': 'CoreMotion · CMDeviceMotion',
             'acquire': '实际生效的惯导采样率（watchOS 常见 50 / 100 Hz）',
             'judge': '< 50 Hz 时高速球拍可能被欠采样，'
                      '该场球速上限估计不可信，需标注 low_fidelity', 'required': True},
            {'field': '丢包率', 'key': 'drop_rate', 'unit': '%', 'sync': 'local',
             'source': '派生 · 采样序号连续性',
             'acquire': '按期望样本数与实际样本数之比计算',
             'judge': '> 5% 则该场标 suspect；> 15% 整场作废，不进入排行与趋势', 'required': True},
            {'field': '电量', 'key': 'battery_pct', 'unit': '%', 'sync': 'local',
             'source': '应用层 · WKInterfaceDevice.batteryLevel',
             'acquire': '训练开始与结束各取一次',
             'judge': '< 10% 时需警惕系统降频导致采样率下跌（与 sample_rate 联合判断）', 'required': False},
            {'field': '算法版本', 'key': 'algorithm_version', 'unit': '文本', 'sync': 'session',
             'source': '应用层 · 随包发布的模型版本号',
             'acquire': '如 CoreML-Tennis-v4.2.1',
             'judge': '⚠️ 跨版本的历史数据不可直接排行：'
                      '模型升级会整体改变球速标定。排行页必须按 version 分组或标注',
             'required': True},
        ],
    },
]

# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
# 上行目标的中文说明（模板里直接用，避免在 Jinja 里写条件）
SYNC_LABEL = {
    'session': {'label': '会话级上行', 'tone': 'primary-fixed',
                'hint': '写入 training_sessions，一场一行'},
    'stroke': {'label': '逐拍上行', 'tone': 'secondary',
               'hint': '写入 stroke_records，按 seq_in_session 排序'},
    'derive': {'label': '后端派生', 'tone': 'tertiary-fixed-dim',
               'hint': '不单独存储，由后端聚合时现算'},
    'local': {'label': '仅本地', 'tone': 'outline',
              'hint': '隐私或体量原因不上行，仅设备内使用'},
}


def _decorate(rows):
    out = []
    for i, r in enumerate(rows, 1):
        item = dict(r)
        item['idx'] = i
        item['sync_meta'] = SYNC_LABEL.get(r.get('sync'), SYNC_LABEL['local'])
        out.append(item)
    return out


def groups():
    """返回带序号与上行元数据的完整目录（页面与接口共用）。"""
    out = []
    for g in GROUPS:
        item = dict(g)
        item['rows'] = _decorate(g['rows'])
        item['count'] = len(item['rows'])
        item['required_count'] = sum(1 for r in g['rows'] if r.get('required'))
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
        'gates': len(GATES),
        'frameworks': ['HealthKit', 'CoreMotion', 'CoreML',
                       'CoreLocation', 'WeatherKit', '应用层'],
    }


def catalog():
    """/api/datasources 的完整响应体。"""
    return {
        'pipeline': PIPELINE,
        'gates': GATES,
        'groups': groups(),
        'summary': summary(),
    }
