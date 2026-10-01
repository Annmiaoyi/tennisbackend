# -*- coding: utf-8 -*-
"""Material Symbols 图标工具（离线自测 / 图标名核对用）

用途：
  1. list   —— 枚举字体里所有连字名（图标名），支持关键字过滤
  2. render —— 把若干图标名渲染成一张对比 PNG，便于与设计稿截图逐一比对

为什么不用 PIL 直接写字：
  Material Symbols 用 GSUB 连字实现「输入图标名 → 显示图标」，
  而本机 Pillow 未编译 RAQM（features.check('raqm') == False），
  FreeType 的 BASIC 布局不处理连字，写名字只会得到一串字母。
  因此这里直接从 glyf 取轮廓，自己做**偶奇填充**（正确处理字形内孔，
  例如 psychology / build_circle 这类中空图标）并展平二次贝塞尔。
"""
import os
import sys

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw

FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'web', 'assets',
                    'fonts', 'material-symbols-outlined.woff2')


def load_font():
    return TTFont(FONT)


def ligature_map(font):
    """返回 {连字名(小写): 目标字形名}。

    Material Symbols 的图标名是多组件连字：例如 "sync" 由 s + y + n + c 组合。
    GSUB ligature lookup 的键是「首字符字形」，值是其余 Component 字形列表，
    所以名字 = 首字符 + 各 Component 字符，需要借 cmap 反查字符。
    注意 lookup 被 Extension(7) 包了一层，必须拆开。
    """
    rev = {}
    for cp, gname in font.getBestCmap().items():
        rev.setdefault(gname, chr(cp))

    out = {}

    def _from_subtable(st):
        if getattr(st, 'LookupType', None) != 4:
            return
        for first, items in (getattr(st, 'ligatures', None) or {}).items():
            for lig in items:
                name = rev.get(first, '') + ''.join(rev.get(c, '') for c in lig.Component)
                if name:
                    out[name.lower()] = lig.LigGlyph

    for lookup in font['GSUB'].table.LookupList.Lookup:
        for st in lookup.SubTable:
            if lookup.LookupType == 7:
                inner = st.ExtSubTable
                for s in (inner if isinstance(inner, list) else [inner]):
                    _from_subtable(s)
            else:
                _from_subtable(st)
    return out


def find(font, keyword):
    lm = ligature_map(font)
    kw = keyword.lower()
    return sorted(n for n in lm if kw in n)


def _contours(font, gname):
    """取字形轮廓（已展平的折线点集，y 轴向上）。"""
    glyf = font['glyf']
    glyph = glyf[gname]
    if glyph.numberOfContours <= 0:
        return []
    coords, endpts, flags = glyph.getCoordinates(glyf)

    polys, start = [], 0
    for end in endpts:
        pts = coords[start:end + 1]
        fl = flags[start:end + 1]
        start = end + 1
        poly, i, n = [], 0, len(pts)
        while i < n:
            x, y = pts[i]
            if fl[i] & 1:  # 在曲线上
                poly.append((x, y))
                i += 1
            else:          # 二次贝塞尔：控制点 + 下一个点（可能是隐含中点）
                cx, cy = x, y
                if i + 1 < n and not (flags[start + 0] if False else 0):
                    pass
                nx, ny = pts[(i + 1) % n]
                if not (fl[(i + 1) % n] & 1):
                    nx, ny = (cx + nx) / 2, (cy + ny) / 2
                px, py = poly[-1] if poly else (nx, ny)
                for t in (0.25, 0.5, 0.75):
                    mt = 1 - t
                    poly.append((mt * mt * px + 2 * mt * t * cx + t * t * nx,
                                 mt * mt * py + 2 * mt * t * cy + t * t * ny))
                poly.append((nx, ny))
                i += 2
        if poly:
            polys.append(poly)
    return polys


def _mask(polys, upm, size):
    """把轮廓栅格化为布尔掩码，用 XOR 实现偶奇填充（内孔会被挖空）。"""
    import numpy as np

    pts = [p for poly in polys for p in poly]
    if not pts:
        return None
    minx = min(p[0] for p in pts)
    maxx = max(p[0] for p in pts)
    miny = min(p[1] for p in pts)
    maxy = max(p[1] for p in pts)
    gw, gh = max(maxx - minx, 1), max(maxy - miny, 1)

    pad = size * 0.12
    avail = size - 2 * pad
    sc = avail / max(gw, gh)
    W = H = size
    ox = pad + (avail - gw * sc) / 2
    oy = pad + (avail - gh * sc) / 2

    acc = np.zeros((H, W), dtype=bool)
    for poly in polys:
        m = Image.new('L', (W, H), 0)
        dr = ImageDraw.Draw(m)
        screen = [(ox + (x - minx) * sc, oy + (maxy - y) * sc) for x, y in poly]
        if len(screen) >= 3:
            dr.polygon(screen, fill=255)
        elif len(screen) == 2:
            dr.line(screen, fill=255, width=max(1, int(sc * 2)))
        acc ^= (np.array(m) > 127)
    return acc


def render(font, names, out_path, cell=88, cols=6, label_h=26):
    lm = ligature_map(font)
    rows = (len(names) + cols - 1) // cols
    W = cols * cell
    H = rows * (cell + label_h)
    img = Image.new('RGB', (W, H), (15, 20, 18))
    dr = ImageDraw.Draw(img)
    try:
        lbl = ImageFont.truetype('arial.ttf', 11)
    except Exception:
        lbl = None

    for i, raw in enumerate(names):
        name = raw.strip()
        r, c = divmod(i, cols)
        x0, y0 = c * cell, r * (cell + label_h)
        dr.rectangle([x0, y0, x0 + cell - 1, y0 + cell - 1], outline=(38, 56, 47))
        dr.text((x0 + 4, y0 + cell + 6), name.lower(), fill=(196, 201, 172), font=lbl)

        gname = lm.get(name.lower())
        if gname is None:
            dr.text((x0 + cell // 2 - 12, y0 + cell // 2 - 6), 'N/A', fill=(255, 180, 171), font=lbl)
            continue
        m = _mask(_contours(font, gname), font['head'].unitsPerEm, cell - 16)
        if m is None:
            continue
        tile = Image.new('RGB', (m.shape[1], m.shape[0]), (223, 228, 224))
        img.paste(tile, (x0 + 8, y0 + 8), Image.fromarray((m * 255).astype('uint8')).crop((0, 0, m.shape[1], m.shape[0])))

    img.save(out_path)
    return out_path


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'list'
    f = load_font()
    if mode == 'list':
        kw = sys.argv[2] if len(sys.argv) > 2 else ''
        names = find(f, kw)
        print(len(names), 'matches')
        if kw:
            print(' '.join(names))
    elif mode == 'render':
        names = sys.argv[2].split(',')
        print(render(f, names, sys.argv[3]))
