# -*- coding: utf-8 -*-
"""在指定 y 带内，按 x 列统计「两图是否有内容」的差异，输出差异所在的 x 区间，
用于定位多出高度的具体元素（x 坐标映射到第几张卡片）。"""
import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SHOTS = os.path.join(ROOT, '_shots')

name, Y0, Y1 = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])

a = Image.open(os.path.join(SHOTS, '%s_design.png' % name)).convert('L')
b = Image.open(os.path.join(SHOTS, '%s_ours.png' % name)).convert('L')
w = min(a.width, b.width)
pa, pb = a.load(), b.load()


def has(px, x):
    for y in range(Y0, Y1):
        if px[x, y] > 40:
            return True
    return False


diffs = [x for x in range(w) if has(pa, x) != has(pb, x)]
print('y=[%d,%d) 上「有内容与否」不一致的 x 列数 = %d / %d' % (Y0, Y1, len(diffs), w))
if diffs:
    runs = []
    s = diffs[0]
    p = diffs[0]
    for x in diffs[1:]:
        if x - p > 6:
            runs.append((s, p))
            s = x
        p = x
    runs.append((s, p))
    print('差异 x 区间：')
    for s, e in runs:
        tag_a = 'design有' if has(pa, (s + e) // 2) else 'design无'
        tag_b = 'ours有' if has(pb, (s + e) // 2) else 'ours无'
        print('  x=%4d..%4d  (%s / %s)' % (s, e, tag_a, tag_b))

# 同时报告每张卡片的实际底边（最后一行有内容的 y）
for tag, px in (('design', pa), ('ours', pb)):
    print('--- %s 每张卡片底边 ---' % tag)
    for i, (x0, x1) in enumerate([(320, 590), (600, 870), (880, 1150), (1160, w - 1)]):
        last = None
        for x in range(x0, x1, 2):
            for y in range(210, 470):
                if px[x, y] > 40:
                    if last is None or y > last:
                        last = y
        print('  卡片%d  x=[%d,%d)  内容底边 y=%s' % (i + 1, x0, x1, last))
