# -*- coding: utf-8 -*-
"""校验编译出的 app.css 是否覆盖了设计稿用到的**全部** Tailwind 类名。

为什么需要：Tailwind CLI 只生成扫描到的类。若 content 配置漏了某个目录、
或某个类名只出现在动态拼接的字符串里，该样式会**静默缺失**（不报错），
页面就悄悄偏离设计稿。所以发布前必须跑一次覆盖自检。

用法：
    python scripts/check_css_coverage.py
退出码：0 = 全覆盖；1 = 有缺失
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DESIGN = os.path.join(ROOT, '..', 'stitch_acemate_tennis_tracker_ui',
                      'stitch_acemate_tennis_tracker_backend', 'stitch_acemate_tennis_tracker_ui')
CSS = os.path.join(ROOT, 'web', 'assets', 'css', 'app.css')
TEMPLATES = os.path.join(ROOT, 'server', 'templates')

DESIGN_PAGES = ['_1', '_2', '_3', '_4', '_5']

# 这些不是 Tailwind 工具类，无需检查
IGNORE = {
    'group',          # 仅作为 group-hover 的标记
    'dark',           # 写在 <html> 上
}

# Tailwind 工具类前缀白名单。
# 用于从 **Python 源码** 里捞「会拼进 class 属性」的类名字面量
# （例：settings.html 的 `{{ d.tone_class }}`，值写在 routers/pages.py 里）。
# 只认带这些前缀的 token，避免把普通字符串（'triage' / 'not-exists' / 中文文案）
# 误当成类名。
UTIL_PREFIX = (
    'text-', 'bg-', 'border-', 'font-', 'from-', 'via-', 'to-', 'ring-', 'divide-',
    'p-', 'px-', 'py-', 'pt-', 'pb-', 'pl-', 'pr-',
    'm-', 'mx-', 'my-', 'mt-', 'mb-', 'ml-', 'mr-',
    'gap-', 'space-', 'w-', 'h-', 'min-', 'max-', 'size-',
    'inset-', 'top-', 'left-', 'right-', 'bottom-',
    'col-', 'row-', 'order-', 'self-', 'items-', 'justify-', 'place-',
    'rounded', 'shadow', 'opacity-', 'overflow-', 'leading-', 'tracking-', 'z-',
    'translate-', 'scale-', 'rotate-', 'origin-', 'duration-', 'delay-', 'ease-',
    'blur-', 'backdrop-', 'brightness-', 'saturate-',
    'object-', 'cursor-', 'pointer-events-', 'select-',
    'fill-', 'stroke-', 'list-', 'decoration-', 'indent-',
    'aspect-', 'columns-', 'basis-', 'flex-',
    'animate-', 'will-change-', 'whitespace-', 'break-', 'align-', 'table-',
)

JINJA = re.compile(r'\{\{.*?\}\}|\{%.*?%\}', re.S)


def strip_jinja(text):
    """删掉 Jinja 表达式。

    ⚠️ 必须做这一步。`class="material-symbols-outlined text-{{ d.tone }} text-[20px]"`
    按空白切分会得到 `text-{{` / `dev.tone` / `}}` / `text-[20px]`，
    其中 `dev.tone` 不是类名却会被当成类名报缺失（曾产生 2 个假报警）。
    """
    return JINJA.sub(' ', text)


def python_class_literals(path):
    """从 Python 源码里捞可能被拼进 class 属性的类名字面量。"""
    s = io.open(path, encoding='utf-8').read()
    out = set()
    for lit in re.findall(r'[\'"]([^\'"]{2,120})[\'"]', s):
        for token in lit.split():
            if not token.islower() and not token.replace('-', '').replace('[', '').islower():
                continue
            if any(token.startswith(p) for p in UTIL_PREFIX):
                out.add(token)
    return out


def collect_classes():
    """收集设计稿 + 模板 + 路由层 Python 里会出现在 class 属性中的全部类词，
    并附上「页内 <style> 自定义类」。"""
    out = set()
    defined = set()   # 页内 <style> 里自定义的类名（Tailwind 不负责生成）
    files = []
    pyfiles = []
    for f in DESIGN_PAGES:
        files.append(os.path.join(DESIGN, f, 'code.html'))
    for root, _dirs, names in os.walk(TEMPLATES):
        for n in names:
            if n.endswith('.html'):
                files.append(os.path.join(root, n))
    for root, _dirs, names in os.walk(os.path.join(ROOT, 'server')):
        for n in names:
            if n.endswith('.py'):
                pyfiles.append(os.path.join(root, n))

    for p in files:
        if not os.path.exists(p):
            continue
        s = io.open(p, encoding='utf-8').read()
        # 段内 <style> 定义的选择器（含设计稿的「Style Scoped Hooks」）
        for st in re.findall(r'<style[^>]*>(.*?)</style>', s, re.S):
            for sel in re.findall(r'\.([A-Za-z0-9_-]+)', st):
                defined.add(sel)
        for m in re.finditer(r'class="([^"]*)"', s):
            for c in strip_jinja(m.group(1)).split():
                out.add(c.strip())
    for p in pyfiles:
        out |= python_class_literals(p)
    return out, defined, files, pyfiles


def unescape_css(css):
    """把 CSS 选择器里的转义还原成字面量。

    Tailwind 压缩产物会转义特殊字符，且逗号用**十六进制转义**：
      .shadow-\\[0_0_20px_rgba\\(195\\2c 244\\2c 0\\2c 0\\.2\\)\\]
    注意 `\\2c ` 后面那个空格是转义的一部分（用于终止十六进制），
    所以不能简单地删空格，必须按 CSS 规则解码。
    """
    out = []
    i, n = 0, len(css)
    while i < n:
        ch = css[i]
        if ch != '\\':
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= n:
            break
        nxt = css[i]
        if nxt in '0123456789abcdefABCDEF':
            j = i
            while j < n and j - i < 6 and css[j] in '0123456789abcdefABCDEF':
                j += 1
            code = css[i:j]
            out.append(chr(int(code, 16)))
            i = j
            if i < n and css[i] in ' \t\n':   # 十六进制转义的终止空格
                i += 1
        else:
            out.append(nxt)
            i += 1
    return ''.join(out)


def css_escaped_variants(cls):
    """返回该 class 在 CSS 中可能出现的几种写法。"""
    v = {cls, '.' + cls}
    esc = cls
    for ch in '.:[](),%/#+*~!"\'':
        esc = esc.replace(ch, '\\' + ch)
    v.add(esc)
    v.add('.' + esc)
    return v


def main():
    raw = io.open(CSS, encoding='utf-8').read()
    css = unescape_css(raw)
    classes, defined, files, pyfiles = collect_classes()
    print('扫描 HTML %d 个（设计稿 5 + 模板 %d）+ Python %d 个；'
          'class 词 %d 个，页内 <style> 自定义类 %d 个'
          % (len(files), len(files) - 5, len(pyfiles), len(classes), len(defined)))

    missing = []
    for c in sorted(classes):
        if not c or c in IGNORE:
            continue
        if '{{' in c or '{%' in c:      # 兜底：理论上 strip_jinja 已清掉
            continue
        if c in defined:                 # 页内 <style> 自己定义
            continue
        if any(v in css or v in raw for v in css_escaped_variants(c)):
            continue
        missing.append(c)

    if not missing:
        print('✅ app.css 已覆盖设计稿与模板的全部 class')
        return 0

    print('❌ 有 %d 个 class 在 app.css 中找不到对应规则：' % len(missing))
    for c in missing:
        print('   ', c)
    print('\n排查方向：tailwind.config.js 的 content 是否涵盖了这些类所在的文件；')
    print('          若是动态拼接的类名，需把完整类名字面量写进某个被扫描的文件'
          '（模板或 server/**/*.py），或加入 safelist。')
    return 1


if __name__ == '__main__':
    sys.exit(main())
