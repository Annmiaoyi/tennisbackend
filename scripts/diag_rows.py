# -*- coding: utf-8 -*-
"""扫描两张截图的主内容区垂直结构：找出"有内容"的行段（y 起点/终点），
从而精确定位偏移发生在哪个元素之间。"""
import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SHOTS = os.path.join(ROOT, '_shots')

name = sys.argv[1] if len(sys.argv) > 1 else 'dashboard'
_extra = [int(v) for v in sys.argv[2:4]]
X0 = _extra[0] if len(_extra) > 0 else 300
X1 = _extra[1] if len(_extra) > 1 else 1400


def profile(path):
    im = Image.open(path).convert('L')
    w, h = im.size
    px = im.load()
    rows = []
    for y in range(h):
        lo, hi = 255, 0
        for x in range(X0, min(X1, w), 3):
            v = px[x, y]
            if v < lo:
                lo = v
            if v > hi:
                hi = v
        rows.append(hi - lo)
    return rows


def segments(rows, thr=12, minlen=6):
    out = []
    y = 0
    n = len(rows)
    while y < n:
        if rows[y] > thr:
            y0 = y
            while y < n and rows[y] > thr:
                y += 1
            if y - y0 >= minlen:
                out.append((y0, y))
        else:
            y += 1
    return out


d = profile(os.path.join(SHOTS, '%s_design.png' % name))
o = profile(os.path.join(SHOTS, '%s_ours.png' % name))
sd = segments(d)
so = segments(o)

print('主内容区 x=[%d,%d) 有内容行段（design vs ours）' % (X0, X1))
print('%-6s %-14s %-14s %s' % ('#', 'design y0..y1', 'ours y0..y1', 'Δy0 / Δy1'))
for i in range(max(len(sd), len(so))):
    a = sd[i] if i < len(sd) else None
    b = so[i] if i < len(so) else None
    if a and b:
        print('%-6d %-14s %-14s %+d / %+d' % (i, '%d..%d' % a, '%d..%d' % b, b[0] - a[0], b[1] - a[1]))
    elif a:
        print('%-6d %-14s %-14s 仅设计稿' % (i, '%d..%d' % a, '-'))
    else:
        print('%-6d %-14s %-14s 仅实现' % (i, '-', '%d..%d' % b))
