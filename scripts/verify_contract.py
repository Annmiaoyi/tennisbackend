# -*- coding: utf-8 -*-
"""verify_contract.py — 三端契约一致性校验（**不需要起服务**）。

    .venv/bin/python scripts/verify_contract.py

============================================================================
 这个脚本防的是什么
============================================================================
三端最容易出的故障不是崩溃，而是**静默失配**：接口返回 200、库里也有行，
只是关键指标全是 NULL。成因有两类：

  1. Swift 里字段名与后端入参不一致（历史上真实发生过 6 处）；
  2. 契约里加了字段，但后端 `SYNCABLE` 白名单没同步加 —— 多传的字段被**直接丢弃**，
     不报错。

本脚本把这两类都变成**硬失败**：

  A. 解析 `contract/TennisContract.swift`，取出契约的**线上字段名**
     （取 CodingKeys 的 rawValue，而不是属性名 —— 两者可能不同）。
  B. 与后端**实际读取**的键对账（从 `server/ingest.py` 源码里抠 `session.get('x')`），
     双向比对：契约里后端不读的、后端读契约里没有的，都要报错。
  C. 枚举对账：`SwingType` 的六类必须与 `server/ingest.py: STROKE_TYPES` 逐字一致。
  D. 白名单对账：把契约字段实际走一遍 `ingest.build_operations`，
     断言产出的 payload 键**全部**在 `SYNCABLE` 里，且关键指标非空。
  E. 端到端行数对账：6 类各一拍 + 1 拍 unknown，断言
     `stroke_count=7`、六类分项各 1、明细写 6 行（unknown 不入明细）。

新增契约字段但没在本脚本补样例值 → 直接报错，逼作者想清楚后端的映射。

退出码 0 = 全部一致。
"""
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CONTRACT_PATH = os.path.join(ROOT, 'contract', 'TennisContract.swift')
INGEST_PATH = os.path.join(ROOT, 'server', 'ingest.py')

# 隔离库，避免导入 db 时碰到 var/ 里的真实数据
_TMP = tempfile.mkdtemp(prefix='tennis_contract_')
os.environ.setdefault('ACEMATE_DB', os.path.join(_TMP, 'acemate.db'))
os.environ.setdefault('NETPULSE_RAW_DIR', os.path.join(_TMP, 'raw'))
os.environ.setdefault('NETPULSE_ANNOTATION_DIR', os.path.join(_TMP, 'anno'))

from server import ingest, sync  # noqa: E402

FAILS = []
CHECKS = 0


def check(label, ok, detail=''):
    global CHECKS
    CHECKS += 1
    mark = 'PASS' if ok else 'FAIL'
    if not ok:
        FAILS.append(label)
    line = '%-4s %s' % (mark, label)
    if detail:
        line += '  → %s' % detail
    print(line)


# --------------------------------------------------------------------------- #
# Swift 解析
# --------------------------------------------------------------------------- #
def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def brace_block(src, header_pattern):
    """取 header_pattern 命中处起的第一个 {...} 内容（支持嵌套）。"""
    m = re.search(header_pattern, src)
    if not m:
        return None
    start = src.index('{', m.end() - 1)
    depth = 0
    for i in range(start, len(src)):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                return src[start + 1:i]
    return None


def properties(block):
    """结构体的**存储**属性名。

    必须排除计算属性（`public var x: T { ... }`）—— 它们不是线上字段。
    判据：冒号之后到行尾不含 `{`（无 getter 块）也不含 `=`（无默认值初始化）。
    """
    return re.findall(r'^\s*public var (\w+)\s*:\s*[^\n{=]+$', block, re.M)


def coding_keys(block):
    """解析嵌套的 `enum CodingKeys: String, CodingKey`，返回线上键名列表。

    支持 `case a, b, c` 与 `case name = "wire_name"` 两种写法。
    没有 CodingKeys 时返回 None（合成实现 → 线上键名 == 属性名）。
    """
    inner = brace_block(block, r'enum CodingKeys:\s*String,\s*CodingKey')
    if inner is None:
        return None
    keys = []
    for line in re.findall(r'^\s*case (.+)$', inner, re.M):
        for part in line.split(','):
            part = part.strip()
            if not part:
                continue
            mm = re.match(r'^(\w+)\s*=\s*"([^"]+)"$', part)
            if mm:
                keys.append(mm.group(2))
            elif re.match(r'^\w+$', part):
                keys.append(part)
    return keys


def wire_keys(src, struct_header, fallback_prop_block=None):
    """结构体的线上字段名。优先 CodingKeys，否则用属性名。"""
    block = brace_block(src, struct_header)
    if block is None:
        return None
    ck = coding_keys(block)
    if ck is not None:
        return ck
    return properties(fallback_prop_block if fallback_prop_block else block)


def enum_raw_values(src, enum_header):
    block = brace_block(src, enum_header)
    if block is None:
        return None
    return re.findall(r'^\s*case \w+\s*=\s*"([^"]+)"', block, re.M)


# --------------------------------------------------------------------------- #
print('=' * 72)
print('契约一致性校验 · 三端')
print('=' * 72)
print()

swift = _read(CONTRACT_PATH)
ingest_src = _read(INGEST_PATH)

# ---- 契约版本 ----
ver = re.search(r'public static let version\s*=\s*"([^"]+)"', swift)
check('契约文件带版本号', ver is not None, ver.group(1) if ver else '未找到')

# ---- A. 解析契约字段 ----
session_keys = wire_keys(swift, r'public struct MatchSession:\s*Codable,\s*Sendable')
swing_keys = wire_keys(swift, r'public struct MatchSwing:\s*Codable,\s*Sendable')
request_keys = wire_keys(swift, r'public struct SessionUploadRequest:\s*Codable,\s*Sendable')
swing_types = enum_raw_values(swift, r'public enum SwingType:\s*String,\s*Codable')

check('解析到 MatchSession 契约字段', bool(session_keys), '%d 个' % len(session_keys or []))
check('解析到 MatchSwing 契约字段', bool(swing_keys), '%d 个' % len(swing_keys or []))
check('解析到请求体字段', bool(request_keys), '%d 个' % len(request_keys or []))
check('解析到 SwingType 取值', bool(swing_types), '%d 个' % len(swing_types or []))

print()

# ---- B. 枚举对账 ----
print('--- B. 枚举口径 ---')
backend_types = list(ingest.STROKE_TYPES)
contract_classified = [t for t in (swing_types or []) if t != 'unknown']
check('SwingType 六类与后端 STROKE_TYPES 逐字一致',
      contract_classified == backend_types,
      '契约 %s / 后端 %s' % (contract_classified, backend_types))
check('SwingType 含 unknown（未识别拍计入总数但不入明细）',
      'unknown' in (swing_types or []))
check('后端 STROKE_TYPES 不含 unknown', 'unknown' not in backend_types)
check('契约常量 rallyGapSeconds == 后端 RALLY_GAP_SEC',
      abs(float(re.search(r'rallyGapSeconds:\s*Double\s*=\s*([\d.]+)', swift).group(1))
          - ingest.RALLY_GAP_SEC) < 1e-9,
      '契约 %s / 后端 %s' % (
          re.search(r'rallyGapSeconds:\s*Double\s*=\s*([\d.]+)', swift).group(1),
          ingest.RALLY_GAP_SEC))

print()

# ---- C. 字段对账（契约 ↔ 后端实际读取） ----
print('--- C. 字段对账 ---')

# 后端从 session 对象里读的键（源码抠取，随代码自动更新）
backend_session_reads = set(re.findall(r"session\.get\('([^']+)'\)", ingest_src))
backend_swing_reads = set(re.findall(r"\bs\.get\('([^']+)'\)", ingest_src))

# 兼容别名：后端为老客户端保留的备选键，不属于契约主键
SESSION_ALIASES = {'calories'}
SWING_ALIASES = {'speed_kmh'}

missing_in_contract = backend_session_reads - set(session_keys or []) - SESSION_ALIASES
check('后端读取的每个会话键都在契约里', not missing_in_contract,
      '后端读了但契约没有：%s' % sorted(missing_in_contract))

dead_in_contract = set(session_keys or []) - backend_session_reads
check('契约里没有后端不读的会话字段', not dead_in_contract,
      '契约有但后端不读（会被静默丢弃）：%s' % sorted(dead_in_contract))

missing_swing = backend_swing_reads - set(swing_keys or []) - SWING_ALIASES
check('后端读取的每个逐拍键都在契约里', not missing_swing,
      '后端读了但契约没有：%s' % sorted(missing_swing))

dead_swing = set(swing_keys or []) - backend_swing_reads
check('契约里没有后端不读的逐拍字段', not dead_swing,
      '契约有但后端不读：%s' % sorted(dead_swing))

check('请求体字段与后端入参一致（openid / session / student_id / student_name / include_strokes）',
      set(request_keys or []) == {'openid', 'session', 'student_id', 'student_name',
                                  'include_strokes'},
      str(sorted(request_keys or [])))

print()

# ---- D. 行为对账：走一遍 build_operations ----
print('--- D. 契约字段走一遍后端翻译层 ---')

SESSION_SAMPLES = {
    'id': 'VERIFY-CONTRACT-0001',
    'startedAt': '2026-10-01T02:00:00Z',
    'endedAt': '2026-10-01T03:00:00Z',
    'duration': 3600,
    'wrist': 'left',
    'title': '契约校验用例',
    'sessionType': 'rally',
    'location': 'A 场地',
    'courtType': 'hard',
    'avgHeartRate': 138,
    'maxHeartRate': 176,
    'activeCalories': 486.0,
    'distanceKm': 3.21,
}

SWING_SAMPLES = {
    'impactTime': 12.5,
    'racketHeadSpeedKmh': 108.4,
    'confidence': 0.82,
}

unmapped = sorted(set(session_keys or []) - set(SESSION_SAMPLES) - {'swings'})
check('会话契约字段都有样例值（新增字段须补进本脚本）', not unmapped,
      '缺少样例：%s' % unmapped)

unmapped_swing = sorted(set(swing_keys or []) - set(SWING_SAMPLES) - {'type'})
check('逐拍契约字段都有样例值', not unmapped_swing,
      '缺少样例：%s' % unmapped_swing)

if FAILS:
    print()
    print('存在解析/对账失败，跳过行为校验。')
    sys.exit(1)

# 6 类各一拍 + 1 拍 unknown（验证「计入总数、不入明细」）
swings = []
t = 1.0
for i, tp in enumerate(backend_types):
    sw = {'type': tp}
    sw.update(SWING_SAMPLES)
    sw['impactTime'] = t
    t += 1.0            # 间隔 1s < 2.5s → 同一回合
    swings.append(sw)
unknown_swing = {'type': 'unknown'}
unknown_swing.update(SWING_SAMPLES)
unknown_swing['impactTime'] = t
swings.append(unknown_swing)

session = dict(SESSION_SAMPLES)
session['swings'] = swings

ops = ingest.build_operations(session, student_id='stu-verify')
session_op = ops[0]
stroke_ops = ops[1:]
payload = session_op['payload']

ALLOWED_SESSION_FIELDS = set(sync.SYNCABLE['training_session']['fields'])
extra = set(payload) - ALLOWED_SESSION_FIELDS
check('会话 payload 无字段被白名单静默丢弃', not extra,
      '不在 SYNCABLE 里：%s' % sorted(extra))

required_non_null = [
    'student_id', 'started_at', 'ended_at', 'duration_sec', 'stroke_count',
    'worn_wrist', 'source', 'external_id',
    'avg_hr', 'max_hr', 'calories_kcal', 'distance_km',
    'peak_speed_kmh', 'avg_speed_kmh', 'serve_peak_kmh', 'serve_avg_kmh',
    'forehand_avg_kmh', 'backhand_avg_kmh', 'rally_max',
    'forehand_count', 'backhand_count', 'serve_count',
    'slice_count', 'volley_count', 'smash_count',
]
nulls = [k for k in required_non_null if payload.get(k) is None]
check('关键指标全部落地（无 NULL）', not nulls, '仍为 NULL：%s' % nulls)
check('stroke_count = 检测到的总拍数（含未识别）', payload['stroke_count'] == 7,
      str(payload.get('stroke_count')))
check('六类分项各 1', all(payload.get('%s_count' % t) == 1 for t in backend_types),
      str({t: payload.get('%s_count' % t) for t in backend_types}))
check('六类之和 < stroke_count（差额 = 未识别拍）',
      sum(payload['%s_count' % t] for t in backend_types) < payload['stroke_count'])
check('rally_max 算得出（依赖 impactTime 为相对秒数）',
      payload['rally_max'] == 7, str(payload.get('rally_max')))

check('逐拍明细恰好 6 行（unknown 不入明细）', len(stroke_ops) == 6,
      '%d 行' % len(stroke_ops))

ALLOWED_STROKE_FIELDS = set(sync.SYNCABLE['stroke_record']['fields'])
stroke_extra = set()
for op in stroke_ops:
    stroke_extra |= (set(op['payload']) - ALLOWED_STROKE_FIELDS)
check('逐拍 payload 无字段被白名单静默丢弃', not stroke_extra,
      '不在 SYNCABLE 里：%s' % sorted(stroke_extra))

stroke_nulls = [
    i for i, op in enumerate(stroke_ops, 1)
    if op['payload'].get('speed_kmh') is None
    or op['payload'].get('impact_ms') is None
    or op['payload'].get('confidence') is None
]
check('逐拍 speed_kmh / impact_ms / confidence 均非空', not stroke_nulls,
      '第 %s 拍为空' % stroke_nulls)

check('impact_ms 由相对秒数 ×1000 得到', stroke_ops[0]['payload']['impact_ms'] == 1000,
      str(stroke_ops[0]['payload'].get('impact_ms')))

check('stroke_record.session_id 指向会话行',
      all(op['payload']['session_id'] == session_op['entity_id'] for op in stroke_ops))

# ---- E. 幂等键 ----
print()
print('--- E. 幂等键 ---')
check('会话 operation_id 由 externalId 决定，不含时间戳',
      session_op['operation_id'] == 'ing-VERIFY-CONTRACT-0001-s',
      session_op['operation_id'])
ops2 = ingest.build_operations(session, student_id='stu-verify')
check('同一场重复构建 → operation_id 完全相同（幂等）',
      [o['operation_id'] for o in ops] == [o['operation_id'] for o in ops2])

# ---- F. 时间口径 ----
print()
print('--- F. 时间口径 ---')
from server import db  # noqa: E402
n1 = db.normalize_ts('2026-10-01T02:00:00Z')
check('后端可解析契约产出的无毫秒 ISO8601（...Z）', n1 == '2026-10-01T02:00:00.000Z',
      str(n1))

# ---- G. 下行字段对账 ----
print()
print('--- G. 下行字段对账（GET /api/prod/sessions）---')

user_api_src = _read(os.path.join(ROOT, 'server', 'routers', 'user_api.py'))

# 后端 out.append({...}) 里的键 —— 从源码抠，随代码自动更新
item_block = brace_block(user_api_src, r'out\.append\(\{')
backend_item_keys = set(re.findall(r"'(\w+)':", item_block or ''))
contract_item_keys = set(wire_keys(swift, r'public struct SessionListItem'))

check('解析到后端下行会话字段', bool(backend_item_keys), '%d 个' % len(backend_item_keys))
check('解析到契约下行会话字段', bool(contract_item_keys), '%d 个' % len(contract_item_keys))

check('下行：后端返回的每个字段都在契约里', not (backend_item_keys - contract_item_keys),
      '后端有但契约没有：%s' % sorted(backend_item_keys - contract_item_keys))
check('下行：契约没有后端不返回的字段', not (contract_item_keys - backend_item_keys),
      '契约有但后端没有：%s' % sorted(contract_item_keys - backend_item_keys))

# 外层响应壳
resp_keys = set(re.findall(r"'(\w+)':\s+(?:len\(out\)|rng\['key'\]|sid|out)",
                           user_api_src))
contract_resp_keys = set(wire_keys(swift, r'public struct SessionListResponse'))
check('下行：响应壳字段一致（count / range / studentId / sessions）',
      contract_resp_keys == {'count', 'range', 'studentId', 'sessions'},
      str(sorted(contract_resp_keys)))

# counts 的键名必须与后端拼列名用的六个类型一致
counts_keys = properties(brace_block(swift, r'public struct StrokeCounts')) or []
check('下行：StrokeCounts 键名 == 六类类型', counts_keys == backend_types,
      '契约 %s / 后端 %s' % (counts_keys, backend_types))
check('下行：后端 counts 的列名由同一组类型拼出',
      re.sub(r'\s+', ' ', user_api_src).find(
          "('forehand', 'backhand', 'serve', 'slice', 'volley', 'smash')") >= 0)

# 单场详情接口目前直接 dump 数据库行（snake_case，且含 user_id / deleted_at 等内部列），
# 与列表接口的 camelCase 不一致。此处只做**存在性断言**把现状固定下来，
# 待后端清理后再补契约类型。见 docs/CONTRACT.md §4.3。
check('单场详情接口为原始行 dump（已知不一致，已登记待清理）',
      "return {\n        'session': dict(row)," in user_api_src)

print()
print('=' * 72)
if FAILS:
    print('失败 %d / %d：' % (len(FAILS), CHECKS))
    for f in FAILS:
        print('  ✗ %s' % f)
    print('=' * 72)
    sys.exit(1)

print('全部通过：%d / %d' % (CHECKS, CHECKS))
print('契约版本 v%s' % (ver.group(1) if ver else '?'))
print('=' * 72)
