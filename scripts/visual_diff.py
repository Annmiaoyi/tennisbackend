# -*- coding: utf-8 -*-
"""设计稿 vs 实现 的视觉比对（像素级保真验收）。

思路：设计稿 HTML 本身是自包含的（Tailwind/CDN + Google Fonts 外链），
所以可以让 Chrome 同时渲染「设计稿原页」和「我们的实现页」，在同一视口、
同一 DPR 下截图，然后逐像素比对。

因为实现是**逐字节复用设计稿的 main 内容**，理论上差异只应来自：
  · 字体加载方式（我们用本地 woff2，设计稿用 Google Fonts）
  · 远程配图（我们本地化，图片内容一致但解码/缩放可能差 1px）
  · 设计稿的 Tailwind Play CDN 与本地 CLI 编译版本的极微小差异

用法：
    python scripts/visual_diff.py                 # 比对全部 5 页
    python scripts/visual_diff.py dashboard       # 只比对一页
输出：_shots/<name>_design.png / <name>_ours.png / <name>_diff.png
"""
import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DESIGN = os.path.join(ROOT, '..', 'stitch_acemate_tennis_tracker_ui',
                      'stitch_acemate_tennis_tracker_backend', 'stitch_acemate_tennis_tracker_ui')
SHOTS = os.path.join(ROOT, '_shots')
REFDIR = os.path.join(SHOTS, '_ref')

PAGES = [
    ('dashboard', '_1', '/'),
    ('training', '_2', '/training'),
    ('users', '_3', '/users'),
    ('personas', '_4', '/personas'),
    ('feedback', '_5', '/feedback'),
]

CHROME_CANDIDATES = [
    # macOS（本仓库主要开发机）
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
    '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',
    # 常见 Linux 路径
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    # Windows
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
]
VIEWPORT = (1440, 1600)
# 设计稿原页与实现页都按 1440 CSS px 宽渲染，与设计稿的版心一致
SCALE = 1


def find_chrome():
    for c in CHROME_CANDIDATES:
        if os.path.exists(c):
            return c
    raise SystemExit(
        '未找到 Chrome / Chromium / Edge —— 本脚本需要本机装一个基于 Chromium 的浏览器。\n'
        'macOS:  brew install --cask google-chrome   （或 chromium / microsoft-edge）\n'
        'Linux:  apt install chromium-browser\n'
        'Windows: 装 Chrome 或 Edge 即可（默认路径已在候选里）。\n'
        '只跑功能回归、不需要像素比对的话，用 scripts/smoke_pages.py 即可。')


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve_ref():
    """把 5 个设计稿页面放到一个临时目录里用 http 提供（避免 file:// 的跨域限制）。"""
    if os.path.exists(REFDIR):
        shutil.rmtree(REFDIR)
    os.makedirs(REFDIR)
    for name, folder, _route in PAGES:
        src = os.path.join(DESIGN, folder, 'code.html')
        shutil.copyfile(src, os.path.join(REFDIR, name + '.html'))
    port = free_port()
    httpd = ThreadingHTTPServer(('127.0.0.1', port), lambda *a: Quiet(*a, directory=REFDIR))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def shoot(chrome, url, out, height=VIEWPORT[1]):
    """截图并**等待文件真正落盘**。

    踩过的坑：Chrome 在已有实例持有同一个 --user-data-dir 时，会把请求转交给
    那个进程后立刻退出，截图由后台进程稍后才写出。若此时就 shutdown 掉参考站
    的 http 服务，会拍到 ERR_CONNECTION_REFUSED。因此这里：
      1. 每次用**独立的临时 profile 目录**，杜绝进程移交；
      2. 调用后轮询等待输出文件出现且大小稳定，才认为截图完成。
    """
    profile = tempfile.mkdtemp(prefix='acemate-shot-')
    out_fs = out.replace('\\', '/')
    if os.path.exists(out):
        os.remove(out)
    cmd = [chrome, '--headless=new', '--disable-gpu', '--no-first-run',
           '--user-data-dir=' + profile.replace('\\', '/'), '--hide-scrollbars',
           '--force-device-scale-factor=%d' % SCALE,
           '--window-size=%d,%d' % (VIEWPORT[0], height),
           '--virtual-time-budget=9000',
           '--screenshot=' + out_fs, url]
    try:
        subprocess.run(cmd, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        pass

    deadline = time.time() + 40
    last = -1
    while time.time() < deadline:
        if os.path.exists(out):
            size = os.path.getsize(out)
            if size > 0 and size == last:
                break
            last = size
        time.sleep(0.4)
    shutil.rmtree(profile, ignore_errors=True)
    return os.path.exists(out)


def diff(name, a_path, b_path):
    from PIL import Image, ImageChops
    a = Image.open(a_path).convert('RGB')
    b = Image.open(b_path).convert('RGB')
    w = min(a.width, b.width)
    h = min(a.height, b.height)
    a = a.crop((0, 0, w, h))
    b = b.crop((0, 0, w, h))
    d = ImageChops.difference(a, b)
    # 容差：字体抗锯齿与图片解码的微小差异不应算作“不一致”
    gray = d.convert('L')
    mask = gray.point(lambda v: 255 if v > 24 else 0)
    changed = sum(1 for p in mask.getdata() if p)
    total = w * h
    pct = 100.0 * changed / total
    # 差异可视化：放大差异并叠到实现图上
    vis = b.copy()
    vis.paste((255, 60, 90), mask=mask)
    vis.save(os.path.join(SHOTS, '%s_diff.png' % name))
    # 并排图便于人眼核对
    side = Image.new('RGB', (w * 2 + 12, h), (15, 20, 18))
    side.paste(a, (0, 0))
    side.paste(b, (w + 12, 0))
    side.save(os.path.join(SHOTS, '%s_side.png' % name))
    return pct, changed, total


def main():
    names = [a for a in sys.argv[1:] if not a.startswith('-')]
    chrome = find_chrome()
    os.makedirs(SHOTS, exist_ok=True)
    httpd, ref_port = serve_ref()
    base = os.environ.get('ACEMATE_BASE', 'http://127.0.0.1:8787')

    rows = []
    try:
        for name, _folder, route in PAGES:
            if names and name not in names:
                continue
            d_png = os.path.join(SHOTS, '%s_design.png' % name)
            o_png = os.path.join(SHOTS, '%s_ours.png' % name)
            shoot(chrome, 'http://127.0.0.1:%d/%s.html' % (ref_port, name), d_png)
            shoot(chrome, base + route, o_png)
            if not (os.path.exists(d_png) and os.path.exists(o_png)):
                print('%-10s 截图失败（设计稿或实现页未生成）' % name)
                continue
            pct, changed, total = diff(name, d_png, o_png)
            rows.append((name, pct, changed, total))
            print('%-10s 差异像素 %7d / %8d  = %5.2f%%   -> _shots/%s_diff.png'
                  % (name, changed, total, pct, name))
    finally:
        httpd.shutdown()

    if rows:
        worst = max(rows, key=lambda r: r[1])
        print('\n最大差异：%s %.2f%%' % (worst[0], worst[1]))
        print('提示：<24 灰阶的差异已被容差忽略（字体抗锯齿）。')


if __name__ == '__main__':
    main()
