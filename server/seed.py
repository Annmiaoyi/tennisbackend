# -*- coding: utf-8 -*-
"""演示数据灌入（内容取自设计稿）。

    python -m server.seed            # 幂等：已有数据则跳过
    python -m server.seed --force    # 删库重建后灌入

为什么要 seed：
  1. 让后端接口返回**真实可查**的数据，而不是空壳；
  2. 让同步协议的 push / pull 有可验证的基线数据；
  3. 种子里学员、工单等实体的内容与设计稿一致，便于与设计稿逐项核对。

注意：平台聚合类指标（热力图、构成分布、雷达坐标…）存在 platform_metrics
的 value_json 里；这些是服务端算出来的展示型聚合，与设计稿数值保持一致。
"""
import argparse
import json
import random
import uuid
from datetime import datetime, timedelta, timezone

from . import db

NOW = datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def uid(prefix=''):
    return prefix + str(uuid.uuid4())


# ===========================================================================
# 学员档案（内容与设计稿 _3 用户管理表一致）
# ===========================================================================
STUDENTS = [
    dict(name='张哲恒', initial='张', tier='VIP', device='AC-88204', watch='Watch Ultra 2',
         years='4年', hand='右手', backhand='双反', racket='Wilson Pro Staff 97',
         tension='52 lbs', nt='3.5', score=78.4, sessions=124, hours=142, strokes=48200,
         forehand=118, serve=168, last_at='今天 16:40',
         last_note='刚结束 · 截击专项', online=1, batch='2024 春季班',
         location='北京 · 朝阳', img='training-b4240d.jpg'),
    dict(name='李思源', initial='李', tier='Club', device='AC-79102', watch='Series 9',
         years='2年', hand='右手', backhand='单反', racket='Babolat Pure Aero',
         tension='50 lbs', nt='3.0', score=69.1, sessions=78, hours=88, strokes=29400,
         forehand=96, serve=132, last_at='昨天 19:15',
         last_note='底线对抗训练', online=0, batch='2024 春季班',
         location='上海 · 浦东', img='training-9815c2.jpg'),
    dict(name='陈雨菲', initial='陈', tier='Elite', device='AC-90412', watch='Watch Ultra 2',
         years='6年', hand='左手', backhand='双反', racket='Head Speed MP',
         tension='54 lbs', nt='4.5', score=91.2, sessions=260, hours=310, strokes=112000,
         forehand=129, serve=179, last_at='今天 11:30',
         last_note='高强度实战对抗', online=1, batch='精英巡回班',
         location='深圳 · 南山', img='personas-50f800.jpg'),
    dict(name='王浩然', initial='王', tier='Pro', device='AC-62310', watch='Series 8',
         years='5年', hand='右手', backhand='双反', racket='Yonex EZONE 98',
         tension='55 lbs', nt='3.5', score=76.8, sessions=110, hours=130, strokes=42100,
         forehand=112, serve=158, last_at='前天 09:20',
         last_note='发球专项加练', online=1, batch='教练认证班',
         location='北京 · 海淀', img='feedback-b6f6f8.jpg'),
    dict(name='赵明', initial='赵', tier='Standard', device='AC-51009', watch='SE 2',
         years='1.5年', hand='右手', backhand='双反', racket='Wilson Clash 100',
         tension='48 lbs', nt='2.5', score=54.0, sessions=42, hours=46, strokes=14300,
         forehand=84, serve=115, last_at='3天前 18:00',
         last_note='入门多球训练', online=0, batch='新手体验班',
         location='广州 · 天河', img='users-7ee98c.jpg'),
]


# ===========================================================================
# 反馈工单（内容与设计稿 _5 一致）
# ===========================================================================
TICKETS = [
    dict(code='FB-20241028-09', title='建议训练报告中增加底线切削专项的球速统计',
         body='当前系统在反手切削（Backhand Slice）时只记录了触球与球速，'
              '但切削与平击的出球速度差异对破网战术评估至关重要，'
              '希望能按击球类型分档以雷达图展示。',
         category='功能新增', status='sprint', status_label='已规划至 v2.5', priority='高',
         votes=142, reporter='张哲恒', meta='NTRP 3.5 · Yonex EZONE 98',
         device='3小时前 来自 iPhone 15 Pro', roadmap='已规划至 v2.5', source='iOS App',
         occurred_at='2024-10-28T09:00:00.000Z'),
    dict(code='FB-20241028-04', title='希望 Apple Watch 在正手击球出现发力代偿（如翻腕过早）时提供轻微触觉反馈',
         body='连续发力击球时很难觉察自己的手腕提前下压翻折，如果陀螺仪检测到危险旋转角，'
              '通过 Apple Watch 的 Haptic Engine 震动提醒，可以有效预防网球肘！',
         category='硬件交互', status='triage', status_label='评估中 (Triage)', priority='紧急',
         votes=218, reporter='陈雨菲', meta='NTRP 4.0 · Apple Watch Ultra 2',
         device='6小时前 来自 实时训练记录', roadmap='', source='Apple Watch',
         occurred_at='2024-10-28T02:00:00.000Z'),
    dict(code='FB-20241027-11', title='在横向对比中，希望能与职业巡回赛选手（如辛纳、阿尔卡拉斯）的数据模型作为对照',
         body='目前对比功能只支持在学员之间拉取。业余高阶训练者非常渴望看到自己平击发球'
              '引拍轨迹和 ATP 职业选手的标杆偏差范围。',
         category='功能新增', status='new', status_label='待审核 (New)', priority='中',
         votes=96, reporter='王浩然', meta='NTRP 4.5 · Babolat Pure Aero 98',
         device='昨天 来自 iPhone App', roadmap='', source='iOS App',
         occurred_at='2024-10-27T10:00:00.000Z'),
    dict(code='FB-20241026-03', title='建议增加教练端一键批量布置训练计划并同步至学员 Apple Watch',
         body='在青训营执教时，每次课后都需要挨个提醒学员。如果教练看板能圈选学员群发'
              '“底线多球500次”计划并在手表上实时倒计时督促，会极大地提高执教效率。',
         category='UI 与交互', status='sprint', status_label='开发中 (Sprint 24B)', priority='高',
         votes=88, reporter='Carlos 教练', meta='认证巡回赛教练 · Head Speed Pro',
         device='2天前 来自 Web 教练控制台', roadmap='Sprint 24B', source='Web Console',
         occurred_at='2024-10-26T06:00:00.000Z'),
    dict(code='FB-20241025-07', title='雨后潮湿人造草地球场的击球球速估计补偿',
         body='湿地球弹起偏低滑行长，击球球速估计的系统偏差明显增大，'
              '希望加入场地湿度补偿模型。',
         category='算法优化', status='triage', status_label='算法组验证', priority='中',
         votes=61, reporter='杭州聚星俱乐部', meta='14 场关联日志',
         device='昨天 18:40', roadmap='', source='iOS App',
         occurred_at='2024-10-25T10:40:00.000Z'),
]


# ===========================================================================
# 用户画像分群（内容与设计稿 _4 一致）
# ===========================================================================
SEGMENTS = [
    dict(code='ARCHETYPE 01', name='底线进攻型重炮手', name_en='Baseline Power Attacker',
         subtitle='正手主导型', headcount=14650, share=34.2, nt_range='NTRP 2.5 - 4.5',
         color='#c3f400', sort=1,
         description='正手攻击占比超过 60%，以正手强攻压制对手底线，'
                     '平均发球时速 152 km/h，偏好硬地与红土场。',
         metrics={'主武器': '正手 INSIDE-OUT', '发球均速': 152, '正手均速': 122,
                  '场地偏好': '硬地 / 红土'},
         insight='正手击球占比 +20%，下压球（高压 / 截击）38 次/100 拍，'
                 '平均击球间隔比同段位短 15%。'),
    dict(code='ARCHETYPE 02', name='稳定防守反击型', name_en='Counter-Puncher / Grinder',
         subtitle='相持忍耐型', headcount=12210, share=28.5, nt_range='NTRP 2.0 - 3.5',
         color='#7bd0ff', sort=2,
         description='平均回合拍数最长，8 拍以上长回合占比最高。'
                     '以稳定多拍拉锯消耗对手，相持续航能力突出。',
         metrics={'主武器': '双手反拍直线', '发球均速': 138, '平均回合': 9.4,
                  '场地偏好': '慢速硬地'},
         insight='8 拍以上长回合占比 61%，长回合中段的平均球速衰减不足 5%。'),
    dict(code='ARCHETYPE 03', name='全能进攻与发球上网', name_en='All-Court / Serve & Volley',
         subtitle='发球局主导型', headcount=7750, share=18.1, nt_range='NTRP 3.5 - 5.0',
         color='#ffb783', sort=3,
         description='发球均速显著高于同段位，网前截击与高压球使用率 25%，'
                     '是四类画像中上网最积极的一类。',
         metrics={'主武器': '发球 + 上网', '发球均速': 168, '网前占比': '25%',
                  '场地偏好': '草地 / 快速硬地'},
         insight='发球均速 168 km/h 为四类最高，一发与二发速差控制在 18 km/h 以内。'),
    dict(code='ARCHETYPE 04', name='休闲健身与进阶新手', name_en='Beginner & Casual Fitness',
         subtitle='健康与技能并重', headcount=8240, share=19.2, nt_range='NTRP 1.0 - 2.5',
         color='#ffb4ab', sort=4,
         description='每周打球 1-2 次，平均心率 118 bpm，训练负荷以健身为主，'
                     '对技术指标敏感度低，更关注卡路里与心率区间。',
         metrics={'主武器': '稳定多拍', '平均心率': 118, '周频次': '1.7 次',
                  '场地偏好': '室内 / 灯光球场'},
         insight='训练负荷偏低但坚持度 87%，是内容与社区运营的核心人群。'),
]


# ===========================================================================
# 平台聚合指标（数值与设计稿一致）
# ===========================================================================
def build_metrics():
    m = {}

    m['online_now'] = 1428

    m['overview_kpis'] = [
        dict(key='active_students', label='注册与活跃学员', value='42,850', unit='人',
             trend='+12.8%', trend_dir='up', footer_label='活跃度指数',
             footer_value='76.4% Active', footer_tone='primary',
             chart='sparkline', accent='primary'),
        dict(key='total_strokes', label='累计记录击球量', value='3.86M', unit='次',
             badge='Live Sensor', badge_tone='secondary',
             footer_label='今日新增', footer_value='128,490', footer_tone='default',
             extra='均速 114 km/h', accent='secondary'),
        dict(key='total_hours', label='总累计训练时长', value='84,210', unit='小时',
             footer_label='人均单次 64 分钟', footer_value='45.2M kcal',
             footer_tone='tertiary', accent='primary'),
        dict(key='pending_tickets', label='待处理建议与工单', value='38', unit='条未结',
             badge='12 高优', badge_tone='error',
             footer_label='服务满意度', footer_value='98.2%', footer_tone='primary',
             accent='error'),
    ]

    # 7 天 × 12 时段（每格代表 2 小时）的负荷等级；数值越大越饱和
    m['heatmap_24h_7d'] = {
        'days': ['周一', '周二', '周三', '周四', '周五', '周六', '周日'],
        'slots': ['02', '04', '06', '08', '10', '12', '14', '16', '18', '20', '22', '24'],
        'values': [
            [0, 0, 0.5, 0.2, 0.4, 0.3, 0.2, 0.5, 0.8, 1.0, 0.7, 0.3],
            [0, 0, 0.4, 0.3, 0.5, 0.4, 0.3, 0.6, 0.8, 1.0, 0.6, 0.2],
            [0, 0, 0.6, 0.4, 0.4, 0.5, 0.4, 0.7, 1.0, 1.0, 0.5, 0.2],
            [0, 0, 0.5, 0.3, 0.4, 0.4, 0.3, 0.6, 0.9, 1.0, 0.6, 0.3],
            [0, 0, 0.4, 0.3, 0.5, 0.6, 0.5, 0.8, 1.0, 1.0, 0.8, 0.5],
            [0, 0, 1.0, 0.8, 1.0, 1.0, 0.8, 1.0, 1.0, 0.9, 0.7, 0.3],
            [0, 0, 0.7, 0.7, 1.0, 1.0, 0.9, 1.0, 0.9, 0.7, 0.4, 0.5],
        ],
        'peak_note': '高峰负荷：周六/周日 09:00 - 11:30 & 18:00 - 21:00',
    }

    m['stroke_mix'] = [
        dict(label='正手击球', pct=48, count='1.85M', tone='primary'),
        dict(label='反手击球', pct=32, count='1.23M', tone='secondary'),
        dict(label='发球时速监控', pct=14, count='540K', tone='error'),
        dict(label='网前截击与高压', pct=6, count='231K', tone='bright'),
    ]

    m['hardware_telemetry'] = {
        'watch_rate': '84.6%', 'watch_note': 'Series 8/9/Ultra 居多',
        'avg_hr': 144, 'avg_hr_note': '有氧耐力核心区间',
        'anomaly_count': 4, 'anomaly_note': '传感器测得瞬时超限 (>220km/h 异常抖动)',
        'sync_state': 'SYNCED',
    }

    m['live_sessions'] = [
        dict(name='林浩 (Kevin)', place='北京国家网球中心 · 场地 3', metric='148 拍 · 42min',
             note='正手 128km/h', tone='primary', avatar='dashboard-2fe75d.jpg'),
        dict(name='Sarah Jenkins', place='上海仙霞网球中心 · 室内 1', metric='312 拍 · 1h 15m',
             note='发球均速 156km/h', tone='secondary', avatar='avatar-female-player.jpg'),
        dict(name='陈柏言', place='深圳湾体育中心 · 室外 4', metric='88 拍 · 18min',
             note='双反切削练习', tone='primary', initial='陈'),
        dict(name='Matteo Rossi', place='广州天河网球场 · 红土 2', metric='420 拍 · 58min',
             note='心率 168 bpm 🔥', tone='tertiary', initial='MR'),
        dict(name='周芷晴', place='杭州黄龙体育馆 · 室内 6', metric='205 拍 · 35min',
             note='最长相持 14 拍', tone='primary', initial='周'),
    ]

    m['ntrp_distribution'] = [
        dict(label='NTRP 1.0 - 2.0 (入门启蒙阶段)', count='8,570 人', pct=20.0, tone='bright'),
        dict(label='NTRP 2.5 - 3.0 (进阶稳定与业余初赛)', count='16,280 人', pct=38.0,
             tone='primary'),
        dict(label='NTRP 3.5 - 4.0 (中高阶竞技与巡回赛主力)', count='12,420 人', pct=29.0,
             tone='secondary'),
        dict(label='NTRP 4.5 - 5.0+ (精英与专业教练级别)', count='5,580 人', pct=13.0,
             tone='tertiary'),
    ]
    m['ntrp_insight'] = {
        'kicker': '分群核心洞察',
        'title': 'NTRP 3.0+ 学员单周连线频次高达 4.2 次',
        'body': '高阶球员重点沉浸在击球出球初速、击球量与心率负荷指标分析。',
    }

    m['feedback_digest'] = [
        dict(category='功能新增', title='希望增加底线反手切削 (Slice) 专项训练模式',
             body='“目前算法把强力切削误判成了平击防守球，希望能提供单独的切削球速与手腕角速度分析。”',
             status='排期评审中', status_tone='plain',
             meta='反馈人: 顾教练 (NTRP 4.5) · 32 位学员附议', time='2 小时前',
             category_tone='primary'),
        dict(category='硬件交互', title='Apple Watch 击球强度振动反馈可否自定义档位？',
             body='“打多球时默认触觉振动稍弱，在手汗较多或移动急促时容易漏掉击球强度提醒。”',
             status='v2.5 灰度中', status_tone='accent',
             meta='反馈人: Michael L. · 89 位学员附议', time='5 小时前',
             category_tone='secondary'),
        dict(category='算法优化', title='雨后潮湿人造草地球场的反弹球速测量补偿',
             body='“湿地球弹起偏低滑行长，击球球速估计的系统偏差明显增大。”',
             status='算法组验证', status_tone='plain',
             meta='反馈人: 杭州聚星俱乐部 · 14 场关联日志', time='昨天 18:40',
             category_tone='tertiary'),
    ]
    m['feedback_hot_tags'] = [dict(tag='#切削识别', count=42),
                              dict(tag='#Watch振动', count=29),
                              dict(tag='#击球提示音', count=18)]
    m['feedback_positive_rate'] = '正面评价 94.2%'

    # ---------------- 反馈中心（_5） ----------------
    m['feedback_kpis'] = [
        dict(key='received', label='累计反馈总数', value='1,280', unit='条',
             note='↑14.2% 周环比 · 98.4% 响应率', accent='secondary'),
        dict(key='triage', label='待处理 / 评估中', value='24', unit='条',
             note='高优缺陷 4 条 · 响应 1.8h', accent='error', badge='4 条待评估中'),
        dict(key='roadmap', label='已采纳并规划版本', value='142', unit='条',
             note='v1.5 完成 58 条 · 转化率 11.1%', accent='primary'),
        dict(key='happiness', label='用户平均满意度', value='4.85', unit='/ 5.0',
             note='NPS 72 · 交行业组 2% 水准', accent='tertiary'),
    ]
    m['feedback_status_filters'] = [
        dict(key='all', label='全部建议', count=1280, tone='primary'),
        dict(key='triage', label='待审核', count=24, tone='plain'),
        dict(key='review', label='评估中 (In Review)', count=18, tone='plain', indent=True),
        dict(key='sprint', label='开发排期中', count=32, tone='plain'),
        dict(key='shipped', label='已上线发布', count=86, tone='plain'),
        dict(key='done', label='已归档 / 关闭', count=1120, tone='plain'),
    ]
    m['feedback_hardware_split'] = {
        'total_note': '硬件与来源渠道',
        'items': [dict(label='iPhone App', pct=68, tone='primary'),
                  dict(label='Apple Watch (Ultra/9/9)', pct=24, tone='secondary'),
                  dict(label='线下教练端 / iPad Mount 8%', pct=8, tone='tertiary')],
    }
    m['feedback_categories'] = [
        dict(label='算法与击球识别', count=320, tone='primary'),
        dict(label='UI 与交互体验', count=280, tone='secondary'),
        dict(label='数据报表与导出', count=240, tone='tertiary'),
        dict(label='硬件与蓝牙同步', count=180, tone='error'),
        dict(label='教学课程与训练库', count=260, tone='plain'),
    ]
    m['feedback_timeline'] = [
        dict(day='产品决策迭代会', time='10:30', tone='primary',
             title='产品 @ Elena 评审通过 · 列入 v2.5 切削专项数据维度扩展需求',
             meta='Grooming 会'),
        dict(day='算法可行性评估', time='昨天 16:45', tone='secondary',
             title='算法专家 @ Dr. Zhao 确认现有 Apple Watch 陀螺仪采样率已具备手腕角速度捕捉精度，可直接调用 IMU Raw Data 输出。',
             meta='算法评审'),
        dict(day='工程启动与排期意见', time='昨天 09:12', tone='tertiary',
             title='NLP 引擎已从 iOS 客户端闭环结果写入「切削数据维度扩展」需求池。',
             meta='工程评估'),
        dict(day='反馈推送 / Push 设置提醒', time='', tone='primary',
             title='感谢你的建议！切削球速分析已列入 v2.5 排期。',
             meta='Push 通知'),
    ]
    m['feedback_nextgen'] = {
        'code': 'Session #TR-8821', 'title': '关联会话数据快照',
        'angle_note': '当前拍摄动态', 'fps': '120 FPS',
        'stroke_label': '反手一刀 (Backhand Slice)',
        'metrics': [dict(label='切削出球球速', value='88', unit='km/h'),
                    dict(label='手腕角速度峰值', value='2,140', unit='°/s'),
                    dict(label='击球节奏稳定度', value='91', unit='%')],
        'engine': '算法模型特征', 'engine_value': 'CoreML-Tennis-v4.2.1',
    }

    # ---------------- 训练对比（_2） ----------------
    m['ntrp_benchmark'] = dict(level='3.5', sample=14890, avg_speed=148,
                               version='v4.2.8 Standard')
    m['ai_diagnosis'] = {
        'confidence': '96.4%',
        'text': ('张哲恒 具有显著更强的主动进攻能力（正手进攻平均球速高出李思源 10 km/h，'
                 '发球均速高出同段位标杆 6 km/h）；但在超过 8 拍以上的多拍相持中，'
                 '李思源 的击球节奏更稳，长回合平均球速衰减比张哲恒少 8%，'
                 '多拍相持中的球速保持能力更好。'),
    }
    m['compare_speed_bars'] = [
        dict(label='一发最高速 (First Serve)', value=176, unit='km/h', pct=95, tone='primary'),
        dict(label='正手球均速 (Forehand)', value=122, unit='km/h', pct=78, tone='primary'),
        dict(label='反手平击与推挡 (Backhand Drive)', value=98, unit='km/h', pct=62,
             tone='secondary'),
        dict(label='网前截击出球速度 (Net Volley)', value=84, unit='km/h', pct=48,
             tone='tertiary'),
        dict(label='高压空中抽球 (Overhead Smash)', value=138, unit='km/h', pct=70,
             tone='error'),
    ]
    m['multi_rally'] = dict(
        hr_avg=158, hr_max=144, delta='-24 vs 第 7 拍', first='第 7 拍', second='第 13 拍',
        note='8 拍以上长回合占比显著下降，体能拐点出现在第 9 拍（历时 4 分钟以上重相持）')
    # 硬件准入：原先的 spin（球旋转）列不可测，已换成 A/B 双方「得分 / 均速」与最长相持。
    # 硬件准入（2026-10-03）：原先的 a_score / b_score 是**比分**，属对抗结果，
    # 腕部 IMU 无法观测，已整列去掉；verdict 里「ACE / 致胜 / 终结比赛」同样去掉。
    m['training_history'] = [
        dict(date='2024-10-24', time='16:30', location='北京·朝阳红土场', type='高强度实战对抗',
             a_speed=124, b_speed=114, rally='14 拍',
             note='背靠背5盘2.5分钟',
             verdict='张哲恒 发球均速与峰值均为上风，发球局节奏更主动。'),
        dict(date='2024-10-18', time='19:00', location='上海·浦东', type='底线多球与切削',
             a_speed=118, b_speed=116, rally='22 拍',
             note='红土场连续多拍训练, 多拍稳定性提升',
             verdict='红土降速后多拍相持明显拉长，李思源的最长相持拍数更高。'),
        dict(date='2024-10-12', time='10:15', location='深圳·南山', type='定点标定训练',
             a_speed=126, b_speed=113, rally='—',
             note='定点喂球测试',
             verdict='张哲恒 击球均速更高，李思源 最长相持更长。'),
    ]
    m['training_history_total'] = 88

    # ---------------- 画像分群（_4） ----------------
    m['persona_total_users'] = 42850
    m['persona_skill_histogram'] = {
        'title': '等级段频次分布', 'peak': 'NTRP 3.5 段位为峰值 8,570 人', 'peak_pct': 58,
        'bins': [18, 24, 30, 46, 58, 42, 34, 25, 20, 16, 10],
        'current': 46,
    }
    m['persona_donuts'] = {
        'long_term': dict(pct=42, label='长期稳定型占比', note='≥2 年左右 42%'),
        'self_learner': dict(pct=88, label='自学网球特征', note='自学人群 88%'),
    }
    m['persona_insight'] = {
        'kicker': '分群核心洞察',
        'title': '涵盖 90 个高网用户的活跃规模 2,300',
        'body': '消耗 112.2 7% 覆盖 7 Apple Watch...',
        'load': '3.5 / 5', 'load_label': '训练负荷 (LOAD)', 'hr_zone': '居中 19:30',
        'hr_zone_note': '心率区间 80% 处',
        'cost': '540', 'cost_unit': 'kcal / 日',
    }
    m['persona_spotlight'] = dict(
        name='张哲恒 (Alex Zhang)', meta='VIP · 4 年球龄 · 球龄 3.5 年 · 项目',
        tags=['物理量大球龄', '力量型打法', '发球强攻偏好', '正手主导进攻',
              '当前排名与年龄'],
        nt='4.5', serves='128', forehand='128', backhand='82', court='红土硬地')
    m['persona_recommendations'] = [
        dict(title='推荐专注于拓展数据 API 接及 1.0', body='帮助张哲恒进行数据 API 接及 1.0',
             tag='接口优先级', tone='primary'),
        dict(title='品牌信息与广告对齐', body='帮助更高效率与数据对齐', tag='品牌', tone='plain'),
        dict(title='关注硬地场景', body='数据可视化与反馈场景更优', tag='场景', tone='plain'),
    ]
    # 硬件准入：球旋转（spin/spin_zone）与甜区（sweet）不可测，已移除，只留可得的速度/负荷。
    m['persona_comparison'] = [
        dict(name='底线进攻型重炮手', nt='3.0 / 3.8', speed=152, hr='94.2%', tone='primary'),
        dict(name='稳定防守反击型', nt='4.1 / 5.2', speed=138, hr='88.6%', tone='secondary'),
        dict(name='全能进攻与跑动', nt='4.2 / 5.4', speed=168, hr='87.1%', tone='tertiary'),
        dict(name='休闲健身与进阶', nt='2.3 / 5.7', speed=118, hr='68.5%', tone='error'),
    ]

    return m


# ===========================================================================
# 训练会话 + 击球记录（同步主体的真实数据）
# ===========================================================================
STROKE_TYPES = ['forehand', 'backhand', 'serve', 'slice', 'volley', 'smash']
STROKE_WEIGHT = [0.46, 0.30, 0.12, 0.06, 0.04, 0.02]


def _split_counts(total, rnd=None):
    """按真实击球构成的权重，把击球总数拆成六类分项计数。

    用「最大余数法」分配，保证 **六类之和恰好等于击球总数** —— 这是 G2
    配平门禁能通过的前提（若用各自 round()，四舍五入误差会让总和漂移，
    门禁会对着一个本来正确的数据误报 suspect）。
    """
    raw = [total * w for w in STROKE_WEIGHT]
    base = [int(x) for x in raw]
    rest = total - sum(base)
    order = sorted(range(len(raw)), key=lambda i: raw[i] - base[i], reverse=True)
    for i in range(rest):
        base[order[i % len(order)]] += 1
    return dict(zip(STROKE_TYPES, base))


def build_sessions(student_id, user_id, student_name, seed):
    """按学员的累计数据反推若干次训练会话，并生成击球明细。"""
    rnd = random.Random(seed)
    sessions = []
    total_sessions = 8
    base = NOW - timedelta(days=1)
    for i in range(total_sessions):
        started = base - timedelta(days=i * 3, hours=rnd.randint(0, 6))
        dur = rnd.randint(45, 130) * 60
        strokes = rnd.randint(60, 420)
        stype = rnd.choice(['drill', 'match', 'rally', 'serve'])
        title = {'drill': '截击专项', 'match': '高强度实战对抗',
                 'rally': '底线对抗训练', 'serve': '发球专项加练'}[stype]
        court = rnd.choice(['hard', 'clay', 'indoor'])
        loc = rnd.choice(['北京国家网球中心', '上海仙霞网球中心', '深圳湾体育中心',
                          '广州天河网球场', '杭州黄龙体育馆'])
        fore = rnd.uniform(84, 129)
        serve = rnd.uniform(115, 179)
        counts = _split_counts(strokes)
        sessions.append(dict(
            id='sess-%s-%02d' % (student_id[5:13], i + 1),
            user_id=user_id, student_id=student_id, title=title, session_type=stype,
            location='%s · 场地 %d' % (loc, rnd.randint(1, 8)), court_type=court,
            started_at=iso(started), ended_at=iso(started + timedelta(seconds=dur)),
            duration_sec=dur, stroke_count=strokes,
            # 采集侧元数据：seed 数据模拟 AceMate 自有 App 采集
            worn_wrist=rnd.choice(['right', 'right', 'right', 'left']),
            source='acemate', external_id=None,
            # 击球分项计数（六类之和 == stroke_count，供配平门禁校验）
            forehand_count=counts['forehand'], backhand_count=counts['backhand'],
            serve_count=counts['serve'], slice_count=counts['slice'],
            volley_count=counts['volley'], smash_count=counts['smash'],
            rally_max=rnd.randint(4, 28), distance_km=round(rnd.uniform(0.8, 4.2), 2),
            calories_kcal=round(rnd.uniform(180, 760), 1),
            avg_hr=rnd.randint(118, 158), max_hr=rnd.randint(150, 186),
            avg_speed_kmh=round(rnd.uniform(72, 118), 1),
            peak_speed_kmh=round(serve + rnd.uniform(0, 12), 1),
            forehand_avg_kmh=round(fore, 1),
            backhand_avg_kmh=round(fore * rnd.uniform(0.76, 0.9), 1),
            serve_avg_kmh=round(serve * rnd.uniform(0.85, 0.95), 1),
            serve_peak_kmh=round(serve, 1),
            # 硬件准入（2026-10-02）：spin_rpm / sweet_spot_rate 需要拍面传感器或
            # 球的高速视觉轨迹，腕部单点 IMU 测不到 —— 不再写入（列暂留，恒为 NULL）。
            # 硬件准入（2026-10-03）补：unforced_errors / winners 是**对抗结果**，
            # 需要知道球有没有落在界内、这一分谁赢 —— 同样测不到，不再写入。
            # 详见 server/datasources.py 的 REMOVED 与 /settings#removed。
            hr_zone=json.dumps({'zone1': 12, 'zone2': 26, 'zone3': 38, 'zone4': 19,
                                'zone5': 5}, ensure_ascii=False),
            notes='%s · %s' % (student_name, title),
        ))
    return sessions


def build_strokes(sessions, user_id, seed, per_session=12):
    """为每个会话生成若干击球样本（真实 App 会记录全部，这里抽稀以便体积可控）。"""
    rnd = random.Random(seed + 7919)
    out = []
    for s in sessions:
        fore = s['forehand_avg_kmh'] or 100
        for k in range(per_session):
            st = rnd.choices(STROKE_TYPES, weights=STROKE_WEIGHT)[0]
            if st == 'serve':
                speed = s['serve_peak_kmh'] * rnd.uniform(0.8, 1.0)
            elif st == 'forehand':
                speed = fore * rnd.uniform(0.85, 1.12)
            else:
                speed = fore * rnd.uniform(0.6, 0.92)
            out.append(dict(
                id='str-%s-%03d' % (s['id'][5:20], k),
                user_id=user_id, session_id=s['id'], seq_in_session=k + 1,
                stroke_type=st, is_slice=1 if st == 'slice' else 0,
                speed_kmh=round(speed, 1),
                # 硬件准入（2026-10-02）：spin_rpm / spin_type / sweet_spot / depth_m /
                # landing_zone / lateral_offset_m / net_clearance_m
                # 全部需要拍面传感器或球的飞行轨迹，腕上测不到 —— 不再写入。
                impact_ms=int(1000 * (k + 1) * rnd.uniform(8, 20)),
                confidence=round(rnd.uniform(0.82, 0.99), 3),
                anomaly=1 if rnd.random() < 0.004 else 0,
            ))
    return out


# ===========================================================================
# 写入
# ===========================================================================
def _insert(conn, table, row):
    cols = ','.join(row.keys())
    ph = ','.join('?' * len(row))
    conn.execute('INSERT OR REPLACE INTO %s (%s) VALUES (%s)' % (table, cols, ph),
                 list(row.values()))


def _base(user_id, ts=None):
    t = ts or iso(NOW)
    return dict(user_id=user_id, created_at=t, updated_at=t, deleted_at=None,
                version=1, sync_status='synced', server_seq=None)


def run(force=False, user_id='u_demo'):
    path = db.init_db(force=force)
    conn = db.connect()

    existing = db.query_one('SELECT COUNT(*) AS n FROM students')['n']
    if existing and not force:
        print('数据库已有 %d 名学员，跳过灌入（用 --force 重建）' % existing)
        return path

    ts = iso(NOW)
    # 让最新一条学员数据排在最前，保证 /api/students 默认排序与设计稿一致
    order_ts = [iso(NOW - timedelta(minutes=i)) for i in range(len(STUDENTS))]

    sids = []
    for i, s in enumerate(STUDENTS):
        sid = 'stu-%08d' % (1001 + i)
        sids.append(sid)
        row = _base(user_id, order_ts[i])
        row.update(dict(
            id=sid, name=s['name'], avatar_url='/assets/img/' + s['img'],
            avatar_initial=s['initial'], tier=s['tier'], device_id=s['device'],
            watch_model=s['watch'], batch=s['batch'], years_playing=s['years'],
            hand=s['hand'], backhand=s['backhand'], racket=s['racket'],
            racket_tension=s['tension'], nt_level=s['nt'], nt_score=s['score'],
            sessions_count=s['sessions'], hours_total=s['hours'],
            strokes_total=s['strokes'], forehand_avg=s['forehand'],
            serve_peak=s['serve'],
            # 硬件准入（2026-10-02）：sweet_spot / spin_rate 需要拍面传感器，
            # 腕部单点 IMU 测不到 —— 不再写入（列暂留，恒为 NULL）。
            # hit_rate（2026-10-03 改口径）：识别置信度达标（confidence ≥ 0.60）的
            # 击球占比。原口径「有效击球占比」会被读成「球是否落在界内」——那是落点，
            # 测不到；新口径只依赖分类器置信度，腕部侧可得，故保留。
            hit_rate=round(88 + i * 1.6, 1), training_load='Optimal', acwr=1.12,
            last_training_at=s['last_at'], last_training_note=s['last_note'],
            location=s['location'], is_online=s['online'],
        ))
        _insert(conn, 'students', row)

    # ---- 反馈工单 ----
    for i, t in enumerate(TICKETS):
        row = _base(user_id, iso(NOW - timedelta(hours=i)))
        row.update(dict(
            id='fb-%04d' % (9001 + i), student_id=sids[i % len(sids)], code=t['code'],
            title=t['title'], body=t['body'], category=t['category'], status=t['status'],
            status_label=t['status_label'], priority=t['priority'], votes=t['votes'],
            reporter_name=t['reporter'], reporter_meta=t['meta'],
            reporter_device=t['device'], source=t['source'], roadmap=t['roadmap'],
            assignee='Elena' if i % 2 == 0 else 'Dr. Zhao', occurred_at=t['occurred_at'],
        ))
        _insert(conn, 'feedback_tickets', row)

    # ---- 画像分群 ----
    for i, g in enumerate(SEGMENTS):
        row = _base('system', ts)
        row.update(dict(
            id='seg-%02d' % (i + 1), code=g['code'], name=g['name'], name_en=g['name_en'],
            subtitle=g['subtitle'], headcount=g['headcount'], share_pct=g['share'],
            description=g['description'], metrics_json=json.dumps(g['metrics'],
                                                                 ensure_ascii=False),
            nt_range=g['nt_range'], insight=g['insight'], color=g['color'],
            sort_order=g['sort'],
        ))
        _insert(conn, 'persona_segments', row)

    # ---- NTRP 基准 ----
    # 硬件准入（2026-10-02）：sweet_spot_rate / spin_rpm 需拍面传感器，不再写入
    #（列暂留、恒为 NULL），只保留球速类标杆值。
    for lv, speed in (('2.5', 128), ('3.0', 138), ('3.5', 148), ('4.0', 158), ('4.5', 166)):
        row = _base('system', ts)
        row.update(dict(id='bm-%s' % lv, level=lv, sample_size=14890,
                        avg_speed_kmh=speed,
                        forehand_kmh=speed - 26, serve_kmh=speed + 20,
                        algorithm_version='v4.2.8 Standard'))
        _insert(conn, 'nt_benchmarks', row)

    # ---- 训练会话 + 击球 ----
    n_sess = n_str = 0
    for i, sid in enumerate(sids):
        for s in build_sessions(sid, user_id, STUDENTS[i]['name'], seed=1000 + i):
            row = _base(user_id, s['started_at'])
            row.update(s)
            _insert(conn, 'training_sessions', row)
            n_sess += 1
        sess_rows = db.query('SELECT * FROM training_sessions WHERE student_id = ?', (sid,))
        for st in build_strokes(sess_rows, user_id, seed=1000 + i):
            row = _base(user_id, iso(NOW))
            row.update(st)
            _insert(conn, 'stroke_records', row)
            n_str += 1

    # ---- 平台聚合指标 ----
    for key, value in build_metrics().items():
        row = _base('system', ts)
        row.update(dict(id='pm-' + key, metric_key=key,
                        value_json=json.dumps(value, ensure_ascii=False), label=key))
        _insert(conn, 'platform_metrics', row)

    # ---- 初始 changelog：让新设备 snapshot 后 cursor 与库内状态一致 ----
    head = db.query_one('SELECT COALESCE(MAX(seq), 0) AS c FROM sync_changelog')['c']
    print('数据库：%s' % path)
    print('  学员 %d · 反馈工单 %d · 画像分群 %d · 训练会话 %d · 击球记录 %d · 聚合指标 %d'
          % (len(STUDENTS), len(TICKETS), len(SEGMENTS), n_sess, n_str,
             len(build_metrics())))
    print('  同步游标起始值：%d' % head)
    return path


def main():
    ap = argparse.ArgumentParser(description='灌入 AceMate 演示数据')
    ap.add_argument('--force', action='store_true', help='删库重建')
    ap.add_argument('--user', default='u_demo')
    args = ap.parse_args()
    run(force=args.force, user_id=args.user)


if __name__ == '__main__':
    main()
