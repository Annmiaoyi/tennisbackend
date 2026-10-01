# -*- coding: utf-8 -*-
"""诊断设计稿与实现截图的垂直/水平偏移性质。

对若干水平条带，穷举垂直位移 dy（-40..40）计算最小均差，看偏移是
  · 常量（结构错位，如 header 高度不同）
  · 随 y 递增（缩放差异，如根字号/行高不同）
  · 局部突变（某元素高度不同）
"""
import os
import sys

from PIL import Image, ImageChops

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SHOTS = os.path.join(ROOT, '_shots')

name = sys.argv[1] if len(sys.argv) > 1 else 'dashboard'
a = Image.open(os.path.join(SHOTS, '%s_design.png' % name)).convert('L')
b = Image.open(os.path.join(SHOTS, '%s_ours.png' % name)).convert('L')
w = min(a.width, b.width)
h = min(a.height, b.height)
print('尺寸 design=%s ours=%s' % (a.size, b.size))

BAND = 24
MAXDY = 60
X0, X1 = 260, w            # 只看主内容区，跳过侧边栏
rows = []
for y0 in range(0, h - BAND, BAND):
    band_a = a.crop((X0, y0, X1, y0 + BAND))
    best = None
    for dy in range(-MAXDY, MAXDY + 1):
        yy0 = y0 + dy
        if yy0 < 0 or yy0 + BAND > h:
            continue
        band_b = b.crop((X0, yy0, X1, yy0 + BAND))
        d = ImageChops.difference(band_a, band_b)
        s = sum(d.getdata()) / (d.width * d.height)
        if best is None or s < best[1]:
            best = (dy, s)
    rows.append((y0, best[0], best[1]))

print('%-7s %-6s %s' % ('y0', 'bestdy', 'mean|d|'))
for y0, dy, s in rows:
    bar = '#' * int(min(s, 40))
    print('%-7d %-6d %8.2f %s' % (y0, dy, s, bar))

# 未位移时的基线差异
d0 = []
for y0 in range(0, h - BAND, BAND):
    band_a = a.crop((X0, y0, X1, y0 + BAND))
    band_b = b.crop((X0, y0, X1, y0 + BAND))
    d = ImageChops.difference(band_a, band_b)
    d0.append(sum(d.getdata()) / (d.width * d.height))
print('\n未位移平均差 %.2f ；位移后平均差 %.2f'
      % (sum(d0) / len(d0), sum(r[2] for r in rows) / len(rows)))
