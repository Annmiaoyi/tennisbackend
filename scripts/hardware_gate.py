# -*- coding: utf-8 -*-
"""硬件准入清洗（页面层）—— 2026-10-02

原则（与 server/datasources.py、server/analytics.py 保持一致）
--------------------------------------------------------------
本产品**唯一的采集端**是 Apple Watch：腕部单点 IMU（加速度计 + 陀螺仪）
+ HealthKit。只有这套硬件物理上**观测得到**的量，才允许出现在页面上。

下面这些量需要**拍面传感器**（甜区）或**球的高速视觉轨迹**（旋转 / 落点 /
弹道），腕上测不到，因此从设计稿生成页面后统一清洗掉：

    甜区（球在拍面的撞击点）· 球旋转（上旋 / 下旋 / RPM）
    · 落点 / 深度 / 深区 · 过网高度 · 滞空弧线 / 弹道 / 飞行轨迹

以及 **2026-10-03 补上的一整类「比赛结果」**：判「进球 / 失误 / 得分 / 制胜分」
必须先看到球落在哪、这一分归谁 —— 那是计分数据，腕部 IMU 只知道你挥了几拍。
同一判据下被点名的还有「入界率」「破发成功率」「胜负归因」「发球雷达」
「智能球拍传感器」「球迹追踪」这类说法。后端与 App 在 2026-10-03 已收干净，
本轮把**设计稿保真层这 5 个页面**补齐（此前只覆盖了甜区/旋转/落点/过网/弹道）。

字段留档见 /settings 的「已移除字段（硬件不可得）」区块。

不在此列的**保留**项（容易被误伤，特此说明）
-------------------------------------------
* ``反手下旋 (Backhand Slice)`` —— 这是**击球类型标签**，属于保留的
  「击球识别」元数据；切片只是这一类挥拍的叫法，不是在报球的自转。
* ``陀螺仪检测到危险旋转角`` / ``平击发球引拍轨迹`` —— 腕部**自身**的
  旋转角与挥拍轨迹，陀螺仪直接可得。
* ``#AppleWatch深度用户`` / ``《双手反拍深度加压》`` 里的「深度」不是球落点深度。

用法
----
    python scripts/hardware_gate.py            # 对已生成的 5 个页面模板就地清洗
    python scripts/hardware_gate.py --check    # 只审计残留，不写文件

``scripts/build_pages.py`` 也会 import 本模块，在「设计稿 <main> 抽取完成、
写模板之前」调用 :func:`scrub`，因此**重跑构建不会把清洗结果冲掉**。
规则是幂等的：跑第二遍找不到原文即跳过，不会重复插入注释。
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TPL_DIR = os.path.join(ROOT, 'server', 'templates', 'pages')

GATED_PAGES = ['dashboard', 'training', 'users', 'personas', 'feedback']

# (页面, 类型, 原文, 替换为, 说明)
#   类型 'lit' = 字面量替换；'re' = 正则替换（用锚点，避免手工转录多行空白的风险）
RULES = [
    # ------------------------------------------------------------------ training
    ('training', 're',
     r'(<div class="grid grid-cols-)2( gap-space-sm pt-space-xs">\s*'
     r'<div class="p-space-sm rounded-lg bg-surface-container flex flex-col">\s*'
     r'<span class="font-label-sm text-label-sm text-on-surface-variant">段位常模均速</span>)',
     r'\g<1>1\g<2>',
     '基准 C 卡删掉第二张卡后，指标卡拉成单列（学员对比卡 A/B 两项均可得，保持 2 列）'),
    ('training', 're',
     r'<div class="p-space-sm rounded-lg bg-surface-container flex flex-col">\s*'
     r'<span class="font-label-sm text-label-sm text-on-surface-variant">甜区命中率标杆</span>.*?'
     r'<span class="font-label-sm text-label-sm text-outline">%</span>\s*</div>\s*</div>',
     '{#- 硬件准入移除：原「甜区命中率标杆 68.2%」卡。\n'
     '    甜区 = 球在**拍面**上的撞击点，腕部单点 IMU 测不到（需拍面传感器或高速摄像）。 -#}',
     '甜区命中率标杆卡'),
    ('training', 'lit',
     '，反手防守切削落点深区率达到 <span class="text-secondary font-bold">71%</span>。',
     '。',
     'AI 战术对比里的落点深区率'),
    ('training', 're',
     r'<!-- Serve: 62 .*?-->',
     '<!-- Serve: 62, Forehand: 68, Stroke volume: 84, Stamina: 88, '
     'Mobility: 76, Backhand: 86 -->',
     '雷达图 polygon 注释里的 Sweetspot 半径'),
    ('training', 're',
     r'<!-- Serve: 94 .*?-->',
     '<!-- Serve: 94, Forehand: 92, Stroke volume: 78, Stamina: 66, '
     'Mobility: 82, Backhand: 54 -->',
     '雷达图 polygon 注释里的 Sweetspot 半径'),
    ('training', 'lit',
     '<!-- Axis Labels with Telemetry -->',
     '{#- 六维轴均为**腕部 IMU + HealthKit 可得**的指标。\n'
     '    2026-10-02 前这里有三条腕上测不到的轴（正手上旋深度 / 甜区击球率 / 体能恢复），已换成可得指标。 -#}',
     '雷达轴说明注释'),
    ('training', 'lit', '正手上旋深度 (92/68)', '正手球速 (92/68)', '雷达轴：上旋深度 → 球速'),
    ('training', 'lit', '甜区击球率 (78/84)', '击球量 (78/84)', '雷达轴：甜区击球率 → 击球量'),
    ('training', 'lit', '体能恢复 (66/88)', '训练时长 (66/88)', '雷达轴：体能恢复 → 训练时长'),
    ('training', 'lit', '反手稳定性 (54/86)', '反手球速 (54/86)', '雷达轴：稳定性 → 球速'),
    ('training', 'lit',
     '<span>正手上旋进攻球 (Forehand Aggression)</span>',
     '<span>正手进攻球 (Forehand Aggression)</span>',
     '击球卡标题去「上旋」'),
    ('training', 'lit',
     '<span>反手均速与深度 (Backhand Drive)</span>',
     '<span>反手均速 (Backhand Drive)</span>',
     '击球卡标题去「深度」'),
    ('training', 'lit',
     'class="lg:col-span-8 bg-surface-container rounded-xl p-space-lg shadow-md flex flex-col justify-between"',
     'class="lg:col-span-12 bg-surface-container rounded-xl p-space-lg shadow-md flex flex-col justify-between"',
     '右栏卡片删掉后，左栏占满 12 栅格'),
    ('training', 're',
     r'<!-- Live Racket & Sweetspot Heatmap Snapshot -->.*?'
     r'轻微牺牲容错率。\s*</p>\s*</div>\s*</div>',
     '{#- 硬件准入移除：「击球甜区散点重叠 / 拍面甜区集中率对比」整块卡片。\n'
     '    甜区是球在**拍面**上的撞击点，腕部单点 IMU 观测不到（需拍面传感器或高速摄像）；\n'
     '    原卡片的散点与聚集度 84.2%/78.5% 都是硬编码示例值。字段留档见 /settings#removed。 -#}',
     '甜区散点整块卡片'),
    ('training', 'lit',
     '张哲恒爆发力更强，李思源击球散点落点更为密集',
     '张哲恒爆发力更强，李思源多拍相持更稳',
     'AI 结论去「散点落点」'),
    ('training', 'lit', '>查看散点</button>', '>查看记录</button>', '按钮文案'),
    ('training', 'lit', '室外硬地 · 发球机特定深度巡回', '室外硬地 · 发球机定点往复', '场次标签去「深度巡回」'),
    ('training', 'lit',
     '<!-- Shot: 正手上旋 (Forehand Topspin) -->',
     '<!-- Shot: 正手进攻 (Forehand Aggression) -->',
     '击球卡注释'),

    # ------------------------------------- training · 2026-10-03 「结果类」复查
    # 上一轮（2026-10-03 上午）已认定「进球 / 得分 / 失误 / 制胜分」与甜区、球旋转
    # 同属腕部 IMU 观测不到的量（判据见 datasources.REMOVED 的 unforced_errors /
    # winners 两条），后端与 App 都已收干净 —— 但**设计稿保真层这 5 个页面漏了**，
    # 因为本文件的规则表只覆盖了「甜区 / 旋转 / 落点 / 过网 / 弹道」，
    # 审计词表 WATCH 里也没有结果类关键词（与登记表那条教训是同一个盲区）。
    # 本轮补齐：整页凡是「比赛结果」性质的展示，一律换成可观的运动学量或训练事实。
    ('training', 're',
     r'<span class="font-label-sm text-label-sm text-on-surface-variant">'
     r'高强度相持失误转折点</span>.*?'
     r'张哲恒相持后体能下降失误率剧增</span>',
     '{#- 硬件准入移除：「高强度相持失误转折点 第 7 拍 vs 第 13 拍」指标卡。\n'
     '    「失误」要先知道球有没有落在界内、这一分是否因此丢掉 —— 属计分数据，\n'
     '    腕部 IMU 只知道你挥了几拍。已换成本场峰值心率（HealthKit 直接可得）。 -#}\n'
     '<span class="font-label-sm text-label-sm text-on-surface-variant">单场峰值心率 (Max HR)</span>\n'
     '<div class="flex items-baseline gap-space-xs">\n'
     '<span class="font-metric-digit text-headline-lg font-extrabold text-primary-fixed">188</span>\n'
     '<span class="font-body-md text-body-md text-on-surface-variant">vs</span>\n'
     '<span class="font-metric-digit text-headline-lg font-extrabold text-secondary">167</span>\n'
     '<span class="font-label-sm text-label-sm text-outline">BPM</span>\n'
     '</div>\n'
     '<span class="font-label-sm text-label-sm text-on-surface-variant">'
     '张哲恒峰值更高，与其局间回落更慢一致</span>',
     '心率卡第三格：失误转折点 → 峰值心率'),
    ('training', 're',
     r'<!-- Simulated Apple Health Cardio Chart Range -->.*?'
     r'<span>时长 85 分钟</span>\s*</div>\s*</div>',
     '<div class="relative w-full bg-surface-container-low rounded-xl p-space-md flex flex-col gap-space-sm overflow-hidden">\n'
     '<div class="flex flex-wrap items-center justify-between gap-space-sm text-outline font-label-sm text-label-sm">\n'
     '<span>全程心率轨迹 · 时长 85 分钟（腕部 PPU 连续采样）</span>\n'
     '<div class="flex items-center gap-space-md">\n'
     '<span class="text-primary-fixed font-bold">● 张哲恒 (波动剧烈, 爆发消耗型)</span>\n'
     '<span class="text-secondary font-bold">● 李思源 (平稳持久, 巡航防守型)</span>\n'
     '</div>\n'
     '</div>\n'
     '<div class="flex gap-space-sm">\n'
     '<div class="shrink-0 relative text-outline font-label-sm text-label-sm" style="width: 34px; height: 112px;">\n'
     '<span class="absolute right-0" style="top: 0%; transform: translateY(-50%);">190</span>\n'
     '<span class="absolute right-0" style="top: 25%; transform: translateY(-50%);">165</span>\n'
     '<span class="absolute right-0" style="top: 50%; transform: translateY(-50%);">140</span>\n'
     '<span class="absolute right-0" style="top: 75%; transform: translateY(-50%);">115</span>\n'
     '<span class="absolute right-0" style="top: 100%; transform: translateY(-50%);">90</span>\n'
     '</div>\n'
     '<div class="flex-1 min-w-0 flex flex-col gap-space-2xs">\n'
     '<div style="width: 100%; height: 112px;">\n'
     '{#- 曲线由 scripts/gen_hr_demo_path.py 生成：\n'
     '    x = 分钟/85*700，y = (190−BPM)/1.25；两条曲线 5 分钟采样均值恰为\n'
     '    卡片上的平均心率 158 / 144 BPM，峰值 188 / 167。可复算，不是手绘 y 值。\n'
     '    preserveAspectRatio="none" + vector-effect 保证曲线铺满绘图区、线宽不被拉伸。 -#}\n'
     '<svg class="w-full h-full" preserveAspectRatio="none" viewBox="0 0 700 80">\n'
     '<line stroke="#313633" stroke-width="1" vector-effect="non-scaling-stroke" x1="0" x2="700" y1="0" y2="0"></line>\n'
     '<line stroke="#313633" stroke-width="1" vector-effect="non-scaling-stroke" x1="0" x2="700" y1="20" y2="20"></line>\n'
     '<line stroke="#313633" stroke-width="1" vector-effect="non-scaling-stroke" x1="0" x2="700" y1="40" y2="40"></line>\n'
     '<line stroke="#313633" stroke-width="1" vector-effect="non-scaling-stroke" x1="0" x2="700" y1="60" y2="60"></line>\n'
     '<line stroke="#313633" stroke-width="1" vector-effect="non-scaling-stroke" x1="0" x2="700" y1="80" y2="80"></line>\n'
     '<line opacity="0.45" stroke="#ffb4ab" stroke-dasharray="5,4" stroke-width="1" vector-effect="non-scaling-stroke" x1="0" x2="700" y1="24" y2="24"></line>\n'
     '<path d="M 0.00,71.44 C 6.86,68.77 27.45,60.77 41.18,55.44 C 54.90,50.11 68.63,43.97 82.35,39.44 C 96.08,34.91 109.80,30.64 123.53,28.24 C 137.25,25.84 150.98,24.77 164.71,25.04 C 178.43,25.31 192.16,29.57 205.88,29.84 C 219.61,30.11 233.33,26.37 247.06,26.64 C 260.78,26.91 274.51,31.97 288.24,31.44 C 301.96,30.91 315.69,23.97 329.41,23.44 C 343.14,22.91 356.86,29.04 370.59,28.24 C 384.31,27.44 398.04,19.17 411.76,18.64 C 425.49,18.11 439.22,24.51 452.94,25.04 C 466.67,25.57 480.39,21.31 494.12,21.84 C 507.84,22.37 521.57,26.37 535.29,28.24 C 549.02,30.11 562.75,29.57 576.47,33.04 C 590.20,36.51 603.92,44.51 617.65,49.04 C 631.37,53.57 645.10,57.31 658.82,60.24 C 672.55,63.17 693.14,65.57 700.00,66.64" fill="none" stroke="#7bd0ff" stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" vector-effect="non-scaling-stroke"></path>\n'
     '<path d="M 0.00,77.92 C 6.86,74.45 27.45,64.59 41.18,57.12 C 54.90,49.65 68.63,40.05 82.35,33.12 C 96.08,26.19 109.80,19.52 123.53,15.52 C 137.25,11.52 150.98,9.25 164.71,9.12 C 178.43,8.99 192.16,15.12 205.88,14.72 C 219.61,14.32 233.33,6.99 247.06,6.72 C 260.78,6.45 274.51,13.39 288.24,13.12 C 301.96,12.85 315.69,5.39 329.41,5.12 C 343.14,4.85 356.86,12.05 370.59,11.52 C 384.31,10.99 398.04,2.45 411.76,1.92 C 425.49,1.39 439.22,8.05 452.94,8.32 C 466.67,8.59 480.39,2.45 494.12,3.52 C 507.84,4.59 521.57,11.25 535.29,14.72 C 549.02,18.19 562.75,19.25 576.47,24.32 C 590.20,29.39 603.92,39.79 617.65,45.12 C 631.37,50.45 645.10,53.39 658.82,56.32 C 672.55,59.25 693.14,61.65 700.00,62.72" fill="none" stroke="#c3f400" stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" vector-effect="non-scaling-stroke"></path>\n'
     '</svg>\n'
     '</div>\n'
     '<div class="flex items-center text-outline font-label-sm text-label-sm pt-space-2xs border-t border-surface-container-high/40">\n'
     '<span style="width: 47%;">第 1 盘 (对攻期)</span>\n'
     '<span class="text-center" style="width: 35%;">第 2 盘 (决胜期)</span>\n'
     '<span class="text-right" style="width: 18%;">赛后放松</span>\n'
     '</div>\n'
     '</div>\n'
     '</div>\n'
     '<p class="font-label-sm text-label-sm text-outline">'
     '虚红线 = 160 BPM 无氧阈值参考线 · 纵轴 90–190 BPM 等距刻度 · 横轴 = 距开场分钟数</p>\n'
     '</div>',
     '心率曲线：修纵横比留白 + 轴标签错行 + 曲线与卡片数字脱钩'),
    ('training', 'lit',
     '发球制胜分率高出同段位标杆 <span class="text-primary-fixed font-bold">24%</span>',
     '发球极速高出同段位标杆 <span class="text-primary-fixed font-bold">19%</span>',
     'AI 诊断横幅：制胜分率 → 发球极速'),
    ('training', 'lit',
     '非受迫性失误率比张哲恒低 <span class="text-secondary font-bold">18%</span>',
     '长回合（≥8 拍）占比高出张哲恒 <span class="text-secondary font-bold">23 个百分点</span>',
     'AI 诊断横幅：非受迫失误率 → 长回合占比'),
    ('training', 'lit',
     '<th class="py-space-sm px-space-md text-primary-fixed">张哲恒 (A) 得分/均速</th>',
     '<th class="py-space-sm px-space-md text-primary-fixed">张哲恒 (A) 均速 / 极速</th>',
     '明细表：得分 → 极速'),
    ('training', 'lit',
     '<th class="py-space-sm px-space-md text-secondary">李思源 (B) 得分/均速</th>',
     '<th class="py-space-sm px-space-md text-secondary">李思源 (B) 均速 / 极速</th>',
     '明细表：得分 → 极速'),
    ('training', 'lit',
     '<th class="py-space-sm px-space-md">胜负关键归因</th>',
     '<th class="py-space-sm px-space-md">本场要点</th>',
     '明细表：胜负归因 → 本场要点'),
    ('training', 'lit',
     '<th class="py-space-sm px-space-md rounded-l-lg">对局日期 / 场地类型</th>',
     '<th class="py-space-sm px-space-md rounded-l-lg">训练日期 / 场地类型</th>',
     '明细表：对局 → 训练'),
    ('training', 'lit',
     '<th class="py-space-sm px-space-md">对局形式</th>',
     '<th class="py-space-sm px-space-md">训练形式</th>',
     '明细表：对局形式 → 训练形式'),
    ('training', 'lit',
     '<div class="text-headline-sm">7 <span class="text-body-md text-on-surface-variant font-normal">(124 km/h)</span></div>\n'
     '<div class="font-label-sm text-label-sm text-primary-fixed">Ace球 4 记 · 制胜分 9</div>',
     '<div class="text-headline-sm">124 <span class="text-body-md text-on-surface-variant font-normal">km/h</span></div>\n'
     '<div class="font-label-sm text-label-sm text-primary-fixed">正手均速 122 · 发球极速 176</div>',
     '第 1 行 A 列：比分 + Ace/制胜分 → 均速与极速'),
    ('training', 'lit',
     '<div class="text-headline-sm">5 <span class="text-body-md text-on-surface-variant font-normal">(114 km/h)</span></div>\n'
     '<div class="font-label-sm text-label-sm text-secondary">非受迫失误仅 2 次</div>',
     '<div class="text-headline-sm">114 <span class="text-body-md text-on-surface-variant font-normal">km/h</span></div>\n'
     '<div class="font-label-sm text-label-sm text-secondary">正手均速 112 · 发球极速 162</div>',
     '第 1 行 B 列：比分 + 非受迫失误 → 均速与极速'),
    ('training', 'lit',
     '<span class="block font-label-sm text-label-sm text-secondary">李思源反手穿越得分</span>',
     '<span class="block font-label-sm text-label-sm text-secondary">全场最长多拍回合</span>',
     '第 1 行：穿越得分 → 最长回合说明'),
    ('training', 'lit',
     '张哲恒抢七发球连续 2 记外角 Ace 终结比赛',
     '张哲恒发球极速 176 km/h 为全场最高，李思源多拍相持更稳',
     '第 1 行要点：Ace 终结 → 可观的球速事实'),
    ('training', 'lit',
     '<div class="text-headline-sm">4 <span class="text-body-md text-on-surface-variant font-normal">(118 km/h)</span></div>\n'
     '<div class="font-label-sm text-label-sm text-error">非受迫失误 16 次</div>',
     '<div class="text-headline-sm">118 <span class="text-body-md text-on-surface-variant font-normal">km/h</span></div>\n'
     '<div class="font-label-sm text-label-sm text-primary-fixed">正手均速 116 · 反手均速 98</div>',
     '第 2 行 A 列：比分 + 非受迫失误 → 均速'),
    ('training', 'lit',
     '<div class="text-headline-sm">6 <span class="text-body-md text-on-surface-variant font-normal">(116 km/h)</span></div>\n'
     '<div class="font-label-sm text-label-sm text-secondary">破发成功率 75%</div>',
     '<div class="text-headline-sm">116 <span class="text-body-md text-on-surface-variant font-normal">km/h</span></div>\n'
     '<div class="font-label-sm text-label-sm text-secondary">正手均速 114 · 反手均速 105</div>',
     '第 2 行 B 列：比分 + 破发成功率 → 均速'),
    ('training', 'lit',
     '<span class="block font-label-sm text-label-sm text-secondary">张哲恒反手出界</span>',
     '<span class="block font-label-sm text-label-sm text-secondary">红土场地最长多拍回合</span>',
     '第 2 行：出界 → 最长回合说明'),
    ('training', 'lit',
     '红土降速后多拍相持拉长，李思源反手深球致胜',
     '红土场地球速整体下降约 6 km/h，多拍回合数相应上升',
     '第 2 行要点：致胜球 → 场地与球速的客观差异'),
    ('training', 'lit',
     '<div class="text-headline-sm">92% <span class="text-body-md text-on-surface-variant font-normal">入界率</span></div>\n'
     '<div class="font-label-sm text-label-sm text-primary-fixed">均速 126 km/h (顶尖)</div>',
     '<div class="text-headline-sm">126 <span class="text-body-md text-on-surface-variant font-normal">km/h</span></div>\n'
     '<div class="font-label-sm text-label-sm text-primary-fixed">正手均速 · 100 球定点极速</div>',
     '第 3 行 A 列：入界率 → 定点标定均速'),
    ('training', 'lit',
     '<div class="text-headline-sm">96% <span class="text-body-md text-on-surface-variant font-normal">入界率</span></div>\n'
     '<div class="font-label-sm text-label-sm text-secondary">均速 113 km/h (平稳)</div>',
     '<div class="text-headline-sm">113 <span class="text-body-md text-on-surface-variant font-normal">km/h</span></div>\n'
     '<div class="font-label-sm text-label-sm text-secondary">正手均速 · 100 球定点均值</div>',
     '第 3 行 B 列：入界率 → 定点标定均速'),
    ('training', 'lit',
     '<span>历史训练对局与对抗详细日志</span>',
     '<span>历史训练记录详细日志</span>',
     '明细表标题去「对局与对抗」'),
    ('training', 'lit',
     '<span class="px-space-xs py-0.5 rounded bg-surface-container-high text-outline font-label-sm text-label-sm font-semibold">近 5 场对局遥测切片</span>',
     '<span class="px-space-xs py-0.5 rounded bg-surface-container-high text-outline font-label-sm text-label-sm font-semibold">近 5 场训练遥测切片</span>',
     '明细表角标去「对局」'),
    ('training', 'lit',
     '展示两人近 30 天内直接对抗 (Head-to-Head) 及同条件发球机测试实测记录',
     '展示两人近 30 天内的同场训练与同条件发球机标定记录',
     '明细表副标题去「直接对抗」'),
    ('training', 'lit',
     '<span class="font-label-sm text-label-sm text-on-surface-variant">对局平均心率</span>',
     '<span class="font-label-sm text-label-sm text-on-surface-variant">单场平均心率 (Avg HR)</span>',
     '心率卡第一格：对局 → 单场'),
    ('training', 'lit',
     '<span>共检索到 18 条跨学员关联对抗记录</span>',
     '<span>共检索到 18 条跨学员关联训练记录</span>',
     '明细表页脚去「对抗记录」'),

    # ------------------------------------------------------------------- personas
    # 同一批次（结果类 + 球迹/雷达/球拍传感器）：这些都是「需看到球或计分」的量。
    ('personas', 'lit',
     '基于千万级击球传感器、心率负荷与球迹追踪数据的多维用户画像聚类、打法风格标签及成长路径预测。',
     '基于千万级逐拍击球识别、心率负荷与多拍相持数据的多维用户画像聚类、打法风格标签及成长路径预测。',
     '分群页副标题去「球迹追踪」'),
    ('personas', 'lit',
     '<span>正手主动得分率</span>',
     '<span>正手主动进攻占比</span>',
     'Archetype 属性条：得分率 → 击球占比'),
    ('personas', 'lit',
     '<span>网前截击成功率</span>',
     '<span>网前截击占比</span>',
     'Archetype 属性条：成功率 → 击球占比'),
    ('personas', 'lit',
     '<span class="font-label-sm text-label-sm text-outline">左手持拍者在反手位发球得分率高 14.6%</span>',
     '<span class="font-label-sm text-label-sm text-outline">左手持拍者在反手位击球占比高 14.6%</span>',
     '持拍手结构：发球得分率 → 击球占比'),
    ('personas', 'lit',
     '且超过 71% 配备了 Apple Watch 或 AceMate 智能球拍传感器。',
     '且超过 71% 配备了 Apple Watch 等兼容穿戴设备。',
     '分群洞察：智能球拍传感器 → 兼容穿戴设备'),
    ('personas', 'lit',
     '受限于二发入界率 (58%) 与反拍变线失误',
     '受限于二发稳定性 (58%) 与反拍变线球速偏低',
     '平台期说明：去「入界率」与「反拍变线失误」（一次改到位，不依赖规则先后）'),
    ('personas', 'lit',
     '基于穿戴式传感器采样与高精度发球雷达实测统计均值',
     '基于穿戴式传感器的分级采样统计均值',
     '分群表说明去「发球雷达」'),

    # --------------------------------------------------------------------- users
    ('users', 'lit',
     '<div class="w-24 h-1.5 bg-surface-container-highest rounded-full overflow-hidden mt-1">\n'
     '<div class="h-full bg-primary-container rounded-full" style="width: 74.2%;"></div>\n'
     '</div>\n'
     '<span class="text-[10px] text-outline font-label-sm">甜区命中 74.2%</span>',
     '<div class="flex items-center justify-between gap-2 text-label-sm font-label-sm">\n'
     '<span class="text-on-surface-variant">最长相持</span>\n'
     '<span class="font-bold text-on-surface">14 拍</span>\n'
     '</div>',
     '学员卡：甜区命中 → 最长相持（逐拍击球识别可得）'),
    ('users', 'lit',
     '<div class="w-24 h-1.5 bg-surface-container-highest rounded-full overflow-hidden mt-1">\n'
     '<div class="h-full bg-secondary rounded-full" style="width: 63.8%;"></div>\n'
     '</div>\n'
     '<span class="text-[10px] text-outline font-label-sm">甜区命中 63.8%</span>',
     '<div class="flex items-center justify-between gap-2 text-label-sm font-label-sm">\n'
     '<span class="text-on-surface-variant">最长相持</span>\n'
     '<span class="font-bold text-on-surface">9 拍</span>\n'
     '</div>',
     '同上'),
    ('users', 'lit',
     '<div class="w-24 h-1.5 bg-surface-container-highest rounded-full overflow-hidden mt-1">\n'
     '<div class="h-full bg-primary-container rounded-full" style="width: 82.5%;"></div>\n'
     '</div>\n'
     '<span class="text-[10px] text-outline font-label-sm">甜区命中 82.5%</span>',
     '<div class="flex items-center justify-between gap-2 text-label-sm font-label-sm">\n'
     '<span class="text-on-surface-variant">最长相持</span>\n'
     '<span class="font-bold text-on-surface">18 拍</span>\n'
     '</div>',
     '同上'),
    ('users', 'lit',
     '<div class="w-24 h-1.5 bg-surface-container-highest rounded-full overflow-hidden mt-1">\n'
     '<div class="h-full bg-primary-container rounded-full" style="width: 71.0%;"></div>\n'
     '</div>\n'
     '<span class="text-[10px] text-outline font-label-sm">甜区命中 71.0%</span>',
     '<div class="flex items-center justify-between gap-2 text-label-sm font-label-sm">\n'
     '<span class="text-on-surface-variant">最长相持</span>\n'
     '<span class="font-bold text-on-surface">11 拍</span>\n'
     '</div>',
     '同上'),
    ('users', 'lit',
     '<div class="w-24 h-1.5 bg-surface-container-highest rounded-full overflow-hidden mt-1">\n'
     '<div class="h-full bg-outline rounded-full" style="width: 52.4%;"></div>\n'
     '</div>\n'
     '<span class="text-[10px] text-outline font-label-sm">甜区命中 52.4%</span>',
     '<div class="flex items-center justify-between gap-2 text-label-sm font-label-sm">\n'
     '<span class="text-on-surface-variant">最长相持</span>\n'
     '<span class="font-bold text-on-surface">6 拍</span>\n'
     '</div>',
     '同上'),
    ('users', 'lit', '发球落点加练', '发球加练', '场次标签去「落点」'),

    # ----------------------------------------------------------------- dashboard
    ('dashboard', 'lit', '正手上旋 128km/h', '正手球速 128km/h', '列表项去「上旋」'),
    ('dashboard', 'lit',
     '<span class="font-label-sm text-label-sm text-on-surface-variant">甜区命中率 81%</span>',
     '', '在线学员行的甜区命中率'),
    ('dashboard', 'lit',
     '高阶球员重点沉浸在击球出球初速、落点深度及上旋 RPM 指标分析。',
     '高阶球员重点沉浸在击球出球初速、击球量与多拍相持指标分析。',
     '分群洞察文案'),
    ('dashboard', 'lit',
     '“目前算法把强力切削误判成了平击防守球，希望能提供单独的切削下旋转速与滞空弧线分析。”',
     '“目前算法把强力切削误判成了平击防守球，希望能提供单独的切削击球识别与连续多拍分析。”',
     '反馈条目正文'),
    ('dashboard', 're',
     r'<!-- Feedback Item 2 -->.*?(?=<!-- Feedback Item 3 -->)',
     '',
     '整条「Apple Watch 击球甜区振动反馈」需求（主题即甜区，硬件不可得）'),
    ('dashboard', 'lit',
     '“湿地球弹起偏低滑行长，AI 视觉预测落点深度有约 15cm 的系统偏差。”',
     '“湿地球弹起偏低滑行长，AI 视觉的反弹球速估计有约 1.5 km/h 的系统偏差。”',
     '反馈条目正文去「落点深度」'),
    ('dashboard', 'lit',
     '<span class="px-2 py-0.5 rounded bg-surface-container-low text-on-surface-variant font-label-sm text-label-sm">#甜区提示音 (18)</span>',
     '', '热门标签去「#甜区提示音」'),

    # ------------------------------------------------------------------ personas
    ('personas', 'lit',
     '切削滑步与网前截击比例高出大盘 240%。发球落点极度分散，追求击球节奏转换与网前下压截击。',
     '切削滑步与网前截击比例高出大盘 240%。发球节奏变化频繁，追求击球节奏转换与网前下压截击。',
     '分群画像文案去「发球落点」'),
    ('personas', 'lit', '<span>甜区击球命中一致性</span>', '<span>多拍相持稳定性</span>', '画像属性条标签'),
    ('personas', 'lit', '#正手暴力上旋 (3200rpm)', '#正手暴力进攻', '标签去球旋转'),
    ('personas', 'lit', '发球旋转度指标', '发球球速指标', '技术特长项去球旋转'),
    ('personas', 'lit',
     '截击落点深区率仅 32%，网前迎球拍面角度常出现后仰。',
     '截击击球量占比仅 32%，网前迎球拍面角度常出现后仰。',
     '技术短板文案去「落点深区率」'),
    ('personas', 'lit', '《双手反拍深度加压》', '《双手反拍加压》', '推荐训练包名去「深度」'),
    ('personas', 'lit', '《二发侧上旋入界率专项特训》', '《二发稳定性专项特训》', '推荐训练包名去球旋转/入界率'),
    ('personas', 'lit',
     '<th class="py-space-md px-space-md">正手平均转速 (RPM)</th>',
     '', '分群表：删除「正手平均转速 (RPM)」整列（表头）'),
    ('personas', 'lit', '<td class="py-space-md px-space-md">2,850 rpm</td>', '', '分群表：转速列单元格'),
    ('personas', 'lit', '<td class="py-space-md px-space-md">2,620 rpm</td>', '', '分群表：转速列单元格'),
    ('personas', 'lit', '<td class="py-space-md px-space-md">2,310 rpm</td>', '', '分群表：转速列单元格'),
    ('personas', 'lit', '<td class="py-space-md px-space-md">1,480 rpm</td>', '', '分群表：转速列单元格'),

    # ------------------------------------------------------------------ feedback
    ('feedback', 'lit',
     '建议训练报告中增加底线切削专项的球速与旋转度统计',
     '建议训练报告中增加底线切削专项的球速与击球类型统计',
     '工单标题'),
    ('feedback', 'lit',
     '当前系统在反手切削（Backhand Slice）时只记录了触球与落点，但切削产生的强烈下旋转速（RPM）和出球弧度对破网战术评估至关重要，希望能以雷达图展示。',
     '当前系统在反手切削（Backhand Slice）时只记录了触球与击球类型，但切削的挥拍节奏与出球初速对破网战术评估至关重要，希望能以雷达图展示。',
     '工单正文去球旋转/落点/弧度'),
    ('feedback', 'lit', '高频采样 · 120 FPS 轨迹捕捉', '高频采样 · 腕部 IMU 逐拍捕捉', '快照角标去「轨迹捕捉」'),
    ('feedback', 'lit',
     '<span class="font-label-sm text-label-sm text-outline">切削旋转转速</span>',
     '<span class="font-label-sm text-label-sm text-outline">切削击球量占比</span>',
     '遥测小格：转速 → 击球量占比'),
    ('feedback', 'lit',
     '<span class="font-headline-sm text-headline-sm text-primary-fixed font-bold mt-0.5">2,140 <span class="text-label-sm font-label-sm text-outline font-normal">RPM</span></span>',
     '<span class="font-headline-sm text-headline-sm text-primary-fixed font-bold mt-0.5">62 <span class="text-label-sm font-label-sm text-outline font-normal">%</span></span>',
     '遥测小格数值'),
    ('feedback', 'lit',
     '<span class="font-label-sm text-label-sm text-outline">甜区击球率</span>',
     '<span class="font-label-sm text-label-sm text-outline">击球类型识别置信度</span>',
     '遥测小格：甜区击球率 → 识别置信度'),
    ('feedback', 'lit',
     '算法专家 @Dr. Zhao 确认现有 Apple Watch 陀螺仪采样率已具备切削下旋角度识别精度，可直接调用 IMU Raw Data 输出。',
     '算法专家 @Dr. Zhao 确认现有 Apple Watch 陀螺仪采样率已具备切削挥拍模式识别精度，可直接调用 IMU Raw Data 输出。',
     '处理流步骤去「下旋角度」（腕上测不到球的自转）'),
    ('feedback', 'lit',
     'placeholder="感谢你的建议！切削旋转与球速分析已列入 v2.5 排期..."',
     'placeholder="感谢你的建议！切削识别与球速分析已列入 v2.5 排期..."',
     '回复框占位文案'),
]

# 审计用：清洗后这些词不应再出现在 5 个设计页里（注释里的留档说明除外）
#
# 2026-10-03 补「结果类」关键词：上一轮词表只有「甜区 / 旋转 / 落点 / 过网 / 弹道」，
# 于是 `Ace球 4 记 · 制胜分 9`、`非受迫失误 16 次`、`破发成功率 75%`、`入界率`、
# `胜负关键归因`、`发球得分率` 等等一整批「比赛结果」性质的展示全都漏过了审计。
# 它们与 unforced_errors / winners 同属一个判据：判「是不是失误 / 这一分归谁」
# 必须先看到球落在哪，腕部 IMU 只知道你挥了几拍。
WATCH = ['甜区', '上旋', '下旋', '转速', 'RPM', 'rpm', '落点', '深区', '弹道', '过网',
         # 结果类（计分 / 输赢）
         '失误', '得分', '制胜', '入界', '胜负', '破发', '出界',
         # 其它「需要看到球或用到额外硬件」的说法
         '球迹', '发球雷达', '智能球拍']

# 审计白名单：确实是「腕部可得」或非球体数据的用法，不算漏网
ALLOW = [
    '反手下旋 (Backhand Slice)',      # 击球类型标签（IMU 可识别挥拍模式）
    '#AppleWatch深度用户',            # 「深度」= 重度使用，非球落点
    '陀螺仪检测到危险旋转角',          # 腕部自身旋转角，陀螺仪直接可得
    '平击发球引拍轨迹',                # 手腕挥拍轨迹，可得
    '切削挥拍模式识别',                # 挥拍模式，可得
]

# 自家留档注释，不计入残留
_SKIP_MARKS = ['{#-', '硬件准入', '移除：', '腕上测不到', '拍面传感器', '字段留档']


def scrub(page, html):
    """对单个页面主体应用硬件准入规则，返回清洗后的 HTML。幂等。"""
    hits = []
    for p, kind, old, new, note in RULES:
        if p != page:
            continue
        n = html.count(old) if kind == 'lit' else len(re.findall(old, html, flags=re.S))
        if not n:
            continue
        if kind == 'lit':
            html = html.replace(old, new)
        else:
            html = re.sub(old, new, html, flags=re.S)
        hits.append((note, n))
    return html, hits


def scrub_only(check=False):
    """对已生成的页面模板就地清洗（不需要设计稿）。"""
    total = 0
    for page in GATED_PAGES:
        path = os.path.join(TPL_DIR, page + '.html')
        if not os.path.exists(path):
            print('%-10s 模板不存在，跳过' % page)
            continue
        src = io.open(path, encoding='utf-8').read()
        out, hits = scrub(page, src)
        total += sum(n for _, n in hits)
        if hits and not check:
            # 这 5 页与仓库既有约定一致采用 CRLF（设计稿是 LF，历史生成结果均为 CRLF）
            with io.open(path, 'w', encoding='utf-8', newline='\r\n') as fh:
                fh.write(out)
        print('%-10s %s  %s' % (page,
                                '命中 %d 处' % len(hits) if hits else '已干净',
                                '; '.join('%s×%d' % (k, v) for k, v in hits)))
    print('\n合计处理 %d 处（--check 模式下不写文件）' % total)


def audit():
    """列出 5 个设计页里仍然出现的敏感词，供人工确认是否有漏网之鱼。

    ⚠️ 必须跟踪「是否处于 Jinja 注释块内」：留档说明常写成多行
    `{#- ... -#}`，其中只有首行含 `{#-`。早先的实现只逐行做关键词匹配，
    于是注释的第 2 行起会被当成真实残留报出来（2026-10-03 实测踩到），
    既产生假阳性，也会诱使人把关键词塞进注释来「消警」。
    """
    print('\n=== 残留审计 ===')
    left = 0
    for page in GATED_PAGES:
        path = os.path.join(TPL_DIR, page + '.html')
        if not os.path.exists(path):
            continue
        text = io.open(path, encoding='utf-8').read()
        in_comment = False
        for i, line in enumerate(text.split('\n'), 1):
            if in_comment:
                if '#}' in line:
                    in_comment = False
                continue
            if '{#' in line:
                # 单行注释 `{#- ... -#}` 不改变状态；多行注释则进入注释态
                in_comment = '#}' not in line[line.index('{#'):]
                continue
            if any(m in line for m in _SKIP_MARKS):
                continue
            if any(a in line for a in ALLOW):
                continue
            for w in WATCH:
                if w in line:
                    print('%-10s %4d  [%s]  %s' % (page, i, w, line.strip()[:110]))
                    left += 1
                    break
    print('残留 %d 行' % left)
    return left


if __name__ == '__main__':
    check = '--check' in sys.argv
    scrub_only(check=check)
    audit()
