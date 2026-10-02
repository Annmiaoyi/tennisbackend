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
    ('personas', 'lit',
     '受限于二发入界率 (58%) 与反拍变线失误',
     '受限于二发稳定性 (58%) 与反拍变线失误',
     '平台期说明去「入界率」'),
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
WATCH = ['甜区', '上旋', '下旋', '转速', 'RPM', 'rpm', '落点', '深区', '弹道', '过网']

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
    """列出 5 个设计页里仍然出现的敏感词，供人工确认是否有漏网之鱼。"""
    print('\n=== 残留审计 ===')
    left = 0
    for page in GATED_PAGES:
        path = os.path.join(TPL_DIR, page + '.html')
        if not os.path.exists(path):
            continue
        for i, line in enumerate(io.open(path, encoding='utf-8').read().split('\n'), 1):
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
