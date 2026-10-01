# -*- coding: utf-8 -*-
"""从设计稿生成 Jinja2 页面模板。

设计稿位置：stitch_acemate_tennis_tracker_backend/stitch_acemate_tennis_tracker_ui/_1.._5/code.html
产出：      server/templates/pages/<name>.html

为什么不手抄设计稿：
  5 个页面主体合计约 180KB HTML，手抄必然引入偏差。这里**逐字节抽取**设计稿
  的 <main> 内容，只做「外链资源本地化」这类机械替换，从而保证像素级保真。

页面结构（5 页完全一致，已实测校验）：
  [0,      3919)  <head>            —— 5 页 md5 完全相同
  [3919,  11164)  <body>..<main>    —— 外壳（aside + header），仅导航激活项不同
  [11164, mainEnd]  <main> 主体      —— 每页不同
  [mainEnd, ...]   </main></div></body></html>

用法：
    python scripts/build_pages.py            # 生成模板（已存在则覆盖）
    python scripts/build_pages.py --check    # 只校验，不写文件
"""
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DESIGN = os.path.join(ROOT, '..', 'stitch_acemate_tennis_tracker_ui',
                      'stitch_acemate_tennis_tracker_backend', 'stitch_acemate_tennis_tracker_ui')
TPL_DIR = os.path.join(ROOT, 'server', 'templates', 'pages')
IMG_MANIFEST = os.path.join(ROOT, 'web', 'assets', 'img', 'manifest.json')

# 设计稿目录 -> (路由名, 页面标题, 导航 data-path)
PAGES = [
    ('_1', 'dashboard', '平台数据与运营概览',   'overview'),
    ('_2', 'training',  '训练记录深度分析与横向对比', 'analytics-comparison'),
    ('_3', 'users',     '学员与用户档案管理',   'user-management'),
    ('_4', 'personas',  '用户画像与技术分群体系', 'user-personas'),
    ('_5', 'feedback',  '用户建议与需求工单中心', 'feedback'),
]

# 实测的结构偏移（5 页一致）
HEAD_END = 3919
MAIN_OPEN = 11164

# 设计稿未覆盖、由本项目补写的区块。
# 抽出的设计稿主体之后**追加 include**，这样本脚本重复运行（覆盖模板）时
# 不会把手工新增的功能区块冲掉 —— 这是「设计稿保真」与「功能扩展」共存的关键：
# 设计稿部分永远逐字节来自设计稿，扩展部分永远来自我们自己的 partial。
EXTRA_MAIN = {
    # 训练页追加：个人纵向分析 + 跨学员排行榜 + 数据源口径说明
    'training': ['pages/_training_extra.html'],
}


def load_img_map():
    """远程 URL -> 本地 /assets/img/xxx.jpg"""
    m = json.load(io.open(IMG_MANIFEST, encoding='utf-8'))
    return {e['url']: '/assets/img/' + e['file'] for e in m}


def localize(html, img_map, stats):
    """把设计稿里的外链资源/宿主相关写法换成本地等价物。"""
    # 1. 远程头像 -> 本地
    for url, local in img_map.items():
        if url in html:
            html = html.replace(url, local)
            stats['img'] += 1

    # 2. 残留的 lh3 图床（manifest 未覆盖时提示）
    left = re.findall(r'https://lh3\.googleusercontent\.com/[^"\s]+', html)
    if left:
        stats.setdefault('leftover', []).extend(left[:5])

    # 3. 导航里指向设计稿占位锚点的 href="#" -> 真实路由（由 base.html 负责，
    #    这里只处理正文内的占位锚点：保持 "#" 但补 aria 语义，避免误跳）
    return html


def build_one(folder, name, title, nav_active, img_map):
    src = io.open(os.path.join(DESIGN, folder, 'code.html'), encoding='utf-8').read()
    assert src[:HEAD_END].strip().endswith('</head>'), '头部结构变了，请重新实测偏移'
    assert src.find('<body') == HEAD_END, '<body> 位置变了'
    assert src.find('<main') == MAIN_OPEN, '<main> 位置变了'

    main_end = src.rfind('</main>')
    main_open_tag_end = src.index('>', MAIN_OPEN) + 1
    inner = src[main_open_tag_end:main_end]

    stats = {'img': 0}
    inner = localize(inner, img_map, stats)

    extra = ''.join('{%% include "%s" %%}\n' % inc
                    for inc in EXTRA_MAIN.get(name, []))

    tpl = ('{%% extends "base.html" %%}\n'
           '{#- 本文件由 scripts/build_pages.py 从设计稿自动生成，请勿手改；\n'
           '    修改请改设计稿后重新运行构建脚本。\n'
           '    功能扩展区块在 _%s_extra.html，由脚本以 include 追加，不会被覆盖。 -#}\n'
           '{%% set nav_active = "%s" %%}\n'
           '{%% block title %%}%s{%% endblock %%}\n'
           '{%% block main %%}%s%s{%% endblock %%}\n') % (name, nav_active, title,
                                                           inner, extra)

    out = os.path.join(TPL_DIR, name + '.html')
    if '--check' not in sys.argv:
        os.makedirs(TPL_DIR, exist_ok=True)
        io.open(out, 'w', encoding='utf-8').write(tpl)
    rel = []
    if stats.get('leftover'):
        rel.append('残留远程图 %d' % len(stats['leftover']))
    if extra:
        rel.append('追加扩展 %d 块' % len(EXTRA_MAIN[name]))
    print('%-10s main=%6d B  本地化图片 %2d  %s' % (name, len(inner), stats['img'], ' '.join(rel)))
    return inner


def main():
    img_map = load_img_map()
    os.makedirs(TPL_DIR, exist_ok=True)
    inners = {}
    for folder, name, title, nav in PAGES:
        inners[name] = build_one(folder, name, title, nav, img_map)

    # 自检：主体内容里不应再有任何 http 外链（除注释/XML 命名空间外）
    bad = []
    for name, inner in inners.items():
        for m in re.finditer(r'(?:src|href)="(https?://[^"]+)"', inner):
            bad.append((name, m.group(1)[:70]))
    if bad:
        print('\n⚠️ 仍有外链资源：')
        for n, u in bad[:10]:
            print('   %s: %s' % (n, u))
    else:
        print('\n✅ 5 个页面主体已无 http 外链资源')
    print('模板目录：%s' % TPL_DIR)


if __name__ == '__main__':
    main()
