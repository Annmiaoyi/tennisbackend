# -*- coding: utf-8 -*-
"""在第 y 行带内，找出「实现有内容而设计稿没有」的 x 区间，
用于定位到底哪张卡片多出了高度。"""
import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SHOTS = os.path.join(ROOT, '_shots')

name = sys.argv[1]
Y0, Y1 = int(sys.argv[2]), int(sys.argv[3])


def rowinfo(path):
    im = Image.open(path).convert('L')
    w, h = im.size
    px = im.load()
    out = []
    for y in range(Y0, min(Y1, h)):
        lo, hi = 255, 0
        for x in range(0, w):
            v = px[x, y]
            if v < lo:
                lo = v
            if v > hi:
                hi = v
        out.append((y, hi - lo))
    return out


for tag, fn in (('design', '%s_design.png' % name), ('ours', '%s_ours.png' % name)):
    print('--- %s  y=%d..%d 每行对比度(>12 视为有内容) ---' % (tag, Y0, Y1))
    for y, rng in rowinfo(os.path.join(SHOTS, fn)):
        if rng > 12:
            print('  y=%d  contrast=%d' % (y, rng))
