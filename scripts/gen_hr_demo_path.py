# -*- coding: utf-8 -*-
"""gen_hr_demo_path.py — 生成「心率负荷与相持体能恢复」演示图的 SVG 曲线。

为什么要这个脚本（而不是手写 path）
-----------------------------------
那张图在 `training.html`（设计稿保真层）里是**静态 SVG**。设计稿原稿给的两条
曲线是随手画的，与卡片上的数字（平均心率 158 / 144 BPM、峰值心率）**对不上**，
而且 path 的 y 值没有任何可解释的映射 —— 既不可复核，也没法验证「图没画错」。

本脚本把这件事变成一个可复算的过程：

  1. 先定义**每分钟的心率采样序列**（比 curve 好核对：每个点都是 BPM，
     不是莫名其妙的 y 坐标）；
  2. 对序列做一个**常数平移**，使算术平均值精确等于卡片上写的平均心率；
  3. 用 Centripetal Catmull-Rom → 三次贝塞尔转成平滑 path；
  4. 按 viewBox 打印 `d` 与统计量（平均 / 峰值 / 最小）。

输出粘进 `scripts/hardware_gate.py` 的对应规则即可。想改形状就改下面的
`RAW_*` 序列，然后：

    .venv/bin/python scripts/gen_hr_demo_path.py

坐标约定（与模板里的 `viewBox="0 0 700 80"` 一致）
--------------------------------------------------
    x = minute / 85 * 700          （0 → 700 覆盖全场 85 分钟）
    bpm 190 落在 y=0（图顶），bpm 90 落在 y=80（图底）
        y = (190 - bpm) / 1.25

纵轴刻度因此是等距的 190 / 165 / 140 / 115 / 90，与模板上那 5 个刻度标签一一对应。
"""
import io
import os
import sys

W, H = 700.0, 80.0
BPM_TOP, BPM_BOT = 190.0, 90.0
TOTAL_MIN = 85.0
STEP_MIN = 5.0

# (分钟, BPM) —— 手写的形状，只表达"哪一段高、哪一段低"。
# 张哲恒：波动剧烈、峰值高、局间回落慢（爆发消耗型）
RAW_A = [(0, 92), (5, 118), (10, 148), (15, 170), (20, 178), (25, 171),
         (30, 181), (35, 173), (40, 183), (45, 175), (50, 187), (55, 179),
         (60, 185), (65, 171), (70, 159), (75, 133), (80, 119), (85, 111)]
# 李思源：整体低一档、起伏小、收尾回得深（巡航防守型）
RAW_B = [(0, 92), (5, 112), (10, 132), (15, 146), (20, 150), (25, 144),
         (30, 148), (35, 142), (40, 152), (45, 146), (50, 158), (55, 150),
         (60, 154), (65, 146), (70, 140), (75, 120), (80, 106), (85, 98)]


def bpm_to_y(bpm):
    return (BPM_TOP - bpm) / ((BPM_TOP - BPM_BOT) / H)


def min_to_x(m):
    return m / TOTAL_MIN * W


def fit(series, target_mean):
    """整条曲线做常数平移，使算术平均精确落在 target_mean（保留 1 位小数）。"""
    mean = sum(b for _, b in series) / len(series)
    shift = target_mean - mean
    return [(m, round(b + shift, 1)) for m, b in series], shift


def catmull_to_bezier(pts):
    """Centripetal-ish Catmull-Rom → 三次贝塞尔 path（绝对坐标）。

    端点用「外推一个虚拟点」的方式处理，避免端点处控制点退化导致拐角发硬。
    """
    p = [(min_to_x(m), bpm_to_y(b)) for m, b in pts]
    ext = [p[0]] + p + [p[-1]]
    d = ['M %.2f,%.2f' % p[0]]
    for i in range(1, len(ext) - 2):
        p0, p1, p2, p3 = ext[i - 1], ext[i], ext[i + 1], ext[i + 2]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6.0, p1[1] + (p2[1] - p0[1]) / 6.0)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6.0, p2[1] - (p3[1] - p1[1]) / 6.0)
        d.append('C %.2f,%.2f %.2f,%.2f %.2f,%.2f'
                 % (c1[0], c1[1], c2[0], c2[1], p2[0], p2[1]))
    return ' '.join(d)


def report(tag, series, shift):
    bpms = [b for _, b in series]
    print('%s  n=%d  平移 %+.1f  均值 %.1f  峰值 %.1f  最低 %.1f'
          % (tag, len(bpms), shift, sum(bpms) / len(bpms), max(bpms), min(bpms)))
    return max(bpms), min(bpms)


def main():
    a, sa = fit(RAW_A, 158.0)
    b, sb = fit(RAW_B, 144.0)
    print('viewBox 0 0 %d %d ；y = (190 - bpm) / 1.25 ；x = min/85*700' % (W, H))
    print('纵轴刻度（等距）：y=0→190   y=20→165   y=40→140   y=60→115   y=80→90')
    print()
    peak_a, _ = report('张哲恒(volt #c3f400)', a, sa)
    peak_b, _ = report('李思源(blue #7bd0ff)', b, sb)
    print()
    print('无氧阈值参考线：160 BPM → y = %.2f' % bpm_to_y(160))
    print()
    print('张哲恒 d = "%s"' % catmull_to_bezier(a))
    print()
    print('李思源 d = "%s"' % catmull_to_bezier(b))
    print()
    print('→ 卡片「峰值心率」应写：张哲恒 %.0f / 李思源 %.0f BPM' % (peak_a, peak_b))
    return 0


if __name__ == '__main__':
    sys.exit(main())
