# -*- coding: utf-8 -*-
"""pinyin.py — 姓名 → 拼音索引。只服务一件事：**筛选框里能按拼音找到人**。

需求（/annotation 的历史采集元数据筛选条）
------------------------------------------
学员名单会越来越长，把所有人都铺成 chip 让人用眼睛找是不成立的，
所以要支持「输入任意片段」把人搜出来。允许的输入有四种：

  · 中文任意字      「哲」      → 张哲恒
  · 拼音首字母      「zzh」     → 张哲恒
  · 拼音全拼（可截断）「zheheng」 → 张哲恒
  · 学员 id（可粘贴）「stu-00001」→ 张哲恒

匹配规则**只有一处实现**（`match()`），JS 侧镜像同一套（见
`web/assets/js/annotation.js` 的 `histMatch`）—— 改了一边必须改另一边。

为什么用 pypinyin 而不是自己写映射表
-----------------------------------
姓名用字几乎没有边界（生僻字、多音字、外文名混排），手写映射表必然是
「演示的那 5 个名字能过、真实名单里一半搜不到」。pypinyin 是纯 Python
轮子（无编译依赖），装进 .venv 即可。

依赖缺失时**降级而不是崩**：没有 pypinyin 就只按原名匹配（中文仍然能搜，
拼音检索失效），页面照常可用 —— 本仓库对「可选能力」的一贯处理方式。
"""
import re
from functools import lru_cache

try:
    from pypinyin import lazy_pinyin
except Exception:                       # pragma: no cover - 依赖缺失时的降级路径
    lazy_pinyin = None

# 基本汉字 + 扩展 A + 兼容汉字。整段交给 pypinyin 处理（它能自己做多音字与连读）
_HAN = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')
# 名字里的空白不进索引：「张 哲恒」与「张哲恒」应该搜得到同一个人
_BLANK = re.compile(r'\s+')

HAS_PYPINYIN = lazy_pinyin is not None


@lru_cache(maxsize=2048)
def index(name):
    """姓名 → {'py': 全拼, 'initials': 首字母}。

    逐段处理：连续汉字整段交给 pypinyin，非汉字段（英文名、数字、符号）
    原样小写保留。这样「Anna 张」→ py='annazhang'、initials='az'，
    英文名也能被首字母搜到，而不会被拼音库吞掉。
    """
    name = _BLANK.sub('', name or '')
    # 先把名字切成「连续汉字段」与「连续非汉字段（英文名 / 数字 / 符号）」交替的片段。
    chunks = [m.group(0) for m in re.finditer(
        r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+|[^\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+',
        name)]
    parts = []
    for c in chunks:
        if _HAN.match(c[0]):
            # 整段交给 pypinyin：它自己处理多音字与连读（'欧阳' → 'ouyang'）
            parts.extend(lazy_pinyin(c) if HAS_PYPINYIN else [c.lower()])
        else:
            # 非汉字段作为一个**词**：整词进全拼、首字符进首字母。
            # 不能逐字符追加，否则 'Anna' 的首字母会变成 'annal' 里的 'anna'。
            parts.append(c.lower())
    py = ''.join(parts)
    return {'py': py, 'initials': ''.join(p[0] for p in parts if p)}


def haystack(name, extra=''):
    """检索串：**全部小写**，把姓名 / 全拼 / 首字母 / 附加标识（学员 id）拼在一起。

    用一个 haystack 而不是「分别比较」是为了让规则简单到可以口述：
    **检索词是 haystack 的子串就算命中**。四种输入因此走同一条路径，
    不存在「拼音分支对、中文分支错」这种只在某个词上翻车的情况。
    """
    idx = index(name)
    parts = [(name or '').lower(), idx['py'], idx['initials'], extra or '']
    return '|'.join(p for p in parts if p)


def match(query, hay):
    """检索词 vs 检索串。规则（**与 JS 侧 histMatch 完全一致**）：

      · 不区分大小写；
      · 空白切成多个词，**每个词都要命中**（AND）—— 「张 zzh」逐步收窄；
      · 空检索词恒命中（= 不过滤）。

    「每个词都是 hay 的子串」而不是「等于」：所以「zheheng」这种
    掐头去尾的输入也能找到张哲恒，符合「随便打几个字母试试」的直觉。
    """
    tokens = (query or '').lower().split()
    if not tokens:
        return True
    return all(t in hay for t in tokens)
