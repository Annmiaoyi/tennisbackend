# -*- coding: utf-8 -*-
"""把设计稿里外链的头像/图片本地化到 web/assets/img/。

设计稿（Stitch 产出）把配图放在 Google 图床（lh3.googleusercontent.com），
外链会在离线、墙内或图床失效时导致空白。按本项目一贯做法（素材必须本地化），
这里统一抓取到本地并按「页面_序号」命名，同时产出 img/manifest.json 映射表，
供 build_pages.py 原样替换 src。

用法：
    python scripts/fetch-assets.py            # 增量下载（已存在则跳过）
    python scripts/fetch-assets.py --force    # 强制重新下载
"""
import hashlib
import io
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DESIGN = os.path.join(ROOT, '..', 'stitch_acemate_tennis_tracker_ui',
                      'stitch_acemate_tennis_tracker_backend', 'stitch_acemate_tennis_tracker_ui')
OUT_DIR = os.path.join(ROOT, 'web', 'assets', 'img')
MANIFEST = os.path.join(OUT_DIR, 'manifest.json')

PAGES = [('_1', 'dashboard'), ('_2', 'training'), ('_3', 'users'),
         ('_4', 'personas'), ('_5', 'feedback')]

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0 Safari/537.36')


def collect():
    """返回 [(url, [用到它的页面名], data-alt 提示)]，去重且保持顺序。"""
    seen = {}
    order = []
    for folder, page in PAGES:
        path = os.path.join(DESIGN, folder, 'code.html')
        html = io.open(path, encoding='utf-8').read()
        # 只取 <img ...> 标签上的 src，避免误抓 <script src=cdn>
        for m in re.finditer(r'<img\b[^>]*?src="(https?://[^"]+)"[^>]*>', html):
            tag, url = m.group(0), m.group(1)
            alt = ''
            am = re.search(r'data-alt="([^"]*)"', tag)
            if am:
                alt = am.group(1)
            if url not in seen:
                seen[url] = {'url': url, 'pages': [], 'alt': alt}
                order.append(url)
            if page not in seen[url]['pages']:
                seen[url]['pages'].append(page)
    return [seen[u] for u in order]


def guess_name(item, idx):
    """按语义提示生成稳定文件名（保证多次运行命名一致）。"""
    alt = item['alt'].lower()
    if 'tennis ball app logo' in alt:
        return 'brand-app-icon', None          # 侧边栏品牌 Logo
    if alt.strip() == 'profile':
        return 'avatar-analyst', None          # 侧边栏 Live Telemetry 分析师头像
    if 'female tennis athlete' in alt:
        return 'avatar-female-player', None
    # 其余配图：用 URL 的稳定哈希做后缀，保证同一图每次命名一致
    return None, hashlib.md5(item['url'].encode()).hexdigest()[:6]


def main():
    force = '--force' in sys.argv
    os.makedirs(OUT_DIR, exist_ok=True)
    items = collect()

    # 先读旧 manifest，命中同 URL 则复用已有文件名（保证幂等）
    old = {}
    if os.path.exists(MANIFEST):
        try:
            for e in json.load(io.open(MANIFEST, encoding='utf-8')):
                old[e['url']] = e['file']
        except Exception:
            old = {}

    used = set()
    out = []
    for i, it in enumerate(items):
        base, h = guess_name(it, i)
        if it['url'] in old:
            fname = old[it['url']]
        elif base:
            fname = base + '.jpg'
        else:
            fname = '%s-%s.jpg' % (it['pages'][0], h)
        # 防重名
        while fname in used and fname != old.get(it['url']):
            fname = fname.replace('.jpg', '-' + h[:3] + '.jpg')
        used.add(fname)

        dest = os.path.join(OUT_DIR, fname)
        if force or not os.path.exists(dest):
            req = urllib.request.Request(it['url'], headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            io.open(dest, 'wb').write(data)
            status = 'downloaded'
        else:
            status = 'cached'
        out.append({'url': it['url'], 'file': fname, 'pages': it['pages'],
                    'alt': it['alt'][:120]})
        print('%-12s %-28s %6d B  %s' % (status, fname, os.path.getsize(dest),
                                         ','.join(it['pages'])))

    io.open(MANIFEST, 'w', encoding='utf-8').write(
        json.dumps(out, ensure_ascii=False, indent=2) + '\n')
    print('\n共 %d 张，manifest -> %s' % (len(out), MANIFEST))


if __name__ == '__main__':
    main()
