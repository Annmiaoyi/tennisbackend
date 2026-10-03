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
sys.path.insert(0, HERE)
# 设计稿根目录的解析复用 build_pages 的那一份候选表（含 DESIGN_ROOT 环境变量）。
# 早先这里独立硬编码了一条相对路径，设计稿资产搬到 Resources 下之后它已失效 ——
# 而 `files.append()` 是无条件追加的，于是脚本照样打印「设计稿 5 个」，
# 实际一个都没扫到。**报告的扫描数与真实扫描数不一致**，比直接报错更危险。
from build_pages import resolve_design  # noqa: E402
CSS = os.path.join(ROOT, 'web', 'assets', 'css', 'app.css')
TEMPLATES = os.path.join(ROOT, 'server', 'templates')

DESIGN_PAGES = ['_1', '_2', '_3', '_4', '_5']

# 这些不是 Tailwind 工具类，无需检查
IGNORE = {
    'group',          # 仅作为 group-hover 的标记
    'dark',           # 写在 <html> 上
    # 仅作 JS 选择器钩子：settings.html 的分类筛选靠
    # `querySelectorAll('tr.cat-row')` + `classList.toggle('hidden')` 工作，
    # 本身不需要任何样式规则。不加进来的话每次都是 1 个假报错，
    # 久而久之会让人不再认真看这个自检的输出。
    'cat-row',
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
    """收集会出现在 class 属性中的类词。

    返回 (全部, 本项目实际渲染的, 页内 <style> 自定义类, 文件表, py 表, 设计稿文件数)。

    ⚠️ 两个集合的区别很重要：
      · **全部** 含设计稿原文。设计稿里被硬件准入下线的区块（甜区散点卡等）
        用到的类（`lg:col-span-4` / `h-36` …）永远不会出现在我们的页面上，
        不该因为它们缺失而判定失败。
      · **本项目实际渲染的** = 模板 + server/**.py + web/assets/js。只有这个集合
        缺类才是真问题 —— 其余单列为信息，避免「每次都红、于是没人看」。
    """
    all_cls = set()
    own = set()       # 本项目真正会渲染的类
    defined = set()   # 页内 <style> 里自定义的类名（Tailwind 不负责生成）
    files = []
    pyfiles = []
    jsfiles = []
    design = resolve_design()
    if design is None:
        print('⚠️ 未找到设计稿根目录（可设 DESIGN_ROOT 指定），'
              '本次只扫本项目模板 —— 模板已逐字节包含设计稿 main 内容，覆盖仍然成立。')
    else:
        for f in DESIGN_PAGES:
            files.append(os.path.join(design, f, 'code.html'))
    n_design = len(files)
    for root, _dirs, names in os.walk(TEMPLATES):
        for n in names:
            if n.endswith('.html'):
                files.append(os.path.join(root, n))
    for root, _dirs, names in os.walk(os.path.join(ROOT, 'server')):
        for n in names:
            if n.endswith('.py'):
                pyfiles.append(os.path.join(root, n))
    for root, _dirs, names in os.walk(os.path.join(ROOT, 'web', 'assets', 'js')):
        for n in names:
            if n.endswith('.js'):
                jsfiles.append(os.path.join(root, n))

    for p in files:
        if not os.path.exists(p):
            continue
        s = io.open(p, encoding='utf-8').read()
        # 段内 <style> 定义的选择器（含设计稿的「Style Scoped Hooks」）
        for st in re.findall(r'<style[^>]*>(.*?)</style>', s, re.S):
            for sel in re.findall(r'\.([A-Za-z0-9_-]+)', st):
                defined.add(sel)
        words = set()
        for m in re.finditer(r'class="([^"]*)"', s):
            words |= set(strip_jinja(m.group(1)).split())
        all_cls |= words
        if p.startswith(TEMPLATES):
            own |= words
    for p in pyfiles + jsfiles:
        lits = python_class_literals(p)
        all_cls |= lits
        own |= lits
    return all_cls, own, defined, files, pyfiles, n_design


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


def missing_in(css, raw, classes, defined):
    """返回在 app.css 里找不到规则的类（已排除忽略项与页内自定义类）。"""
    out = []
    for c in sorted(classes):
        if not c or c in IGNORE:
            continue
        if '{{' in c or '{%' in c:      # 兜底：理论上 strip_jinja 已清掉
            continue
        if c in defined:                 # 页内 <style> 自己定义
            continue
        if any(v in css or v in raw for v in css_escaped_variants(c)):
            continue
        out.append(c)
    return out


def main():
    raw = io.open(CSS, encoding='utf-8').read()
    css = unescape_css(raw)
    classes, own, defined, files, pyfiles, n_design = collect_classes()
    print('扫描 HTML %d 个（设计稿 %d + 模板 %d）+ Python %d 个；'
          'class 词 %d 个（其中本项目渲染 %d 个），页内 <style> 自定义类 %d 个'
          % (len(files), n_design, len(files) - n_design, len(pyfiles),
             len(classes), len(own), len(defined)))

    missing = missing_in(css, raw, own, defined)
    # 设计稿独有、已被硬件准入下线的区块：只做提示，不判失败
    design_only = missing_in(css, raw, classes - own, defined)

    if missing:
        print('❌ 有 %d 个 class 在 app.css 中找不到对应规则：' % len(missing))
        for c in missing:
            print('   ', c)
        print('\n排查方向：tailwind.config.js 的 content 是否涵盖了这些类所在的文件；')
        print('          若是动态拼接的类名，需把完整类名字面量写进某个被扫描的文件'
              '（模板或 server/**/*.py），或加入 safelist；')
        print('          改了模板里的类名后**必须重跑 `npm run build:css`** —— '
              'CSS 是编译产物，不会自动跟着模板变。')
    else:
        print('✅ app.css 已覆盖本项目全部页面用到的 class')

    if design_only:
        print('\nℹ️ 以下 %d 个类只出现在设计稿原文、本项目已不再渲染（硬件准入下线的区块等），'
              '无需 CSS：' % len(design_only))
        for c in design_only:
            print('   ', c)

    return 1 if missing else 0


if __name__ == '__main__':
    sys.exit(main())
