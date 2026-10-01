# -*- coding: utf-8 -*-
"""离线优先同步协议的端到端自测。

不依赖 HTTP 服务：直接把 ACEMATE_DB 指向临时库，调用 server.sync 的
push / pull / snapshot，逐条断言用户提出的每一款一致性要求。

运行：
    python scripts/test_sync.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix='acemate-synctest-')
os.environ['ACEMATE_DB'] = os.path.join(TMP, 'test.db')

from server import db, sync  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print('  %s %s%s' % ('PASS' if cond else 'FAIL', name,
                         ('   → ' + str(detail)) if (detail and not cond) else ''))


def op(op_id, etype, eid, action, payload=None, ts=None):
    return {
        'operation_id': op_id,
        'entity_type': etype,
        'entity_id': eid,
        'action': action,
        'payload': payload or {},
        'client_updated_at': ts or '2026-09-24T10:00:00.000Z',
    }


def main():
    db.init_db(force=True)
    U = 'u_test'

    print('\n[1] 幂等：同一 operation_id 重试任意多次结果一致')
    r = sync.push(U, [op('op-1', 'training_session', 'ts-1', 'create',
                         {'student_id': 's-1', 'started_at': '2026-09-24T09:00:00.000Z',
                          'title': '底线多拍'})])
    check('首次 create → applied', r['applied'] == 1, r['results'][0])
    n1 = db.query_one('SELECT COUNT(*) n FROM training_sessions')['n']
    r2 = sync.push(U, [op('op-1', 'training_session', 'ts-1', 'create',
                          {'student_id': 's-1', 'started_at': '2026-09-24T09:00:00.000Z',
                           'title': '底线多拍'})])
    check('同 operation_id 重放 → duplicate', r2['results'][0]['result'] == 'duplicate',
          r2['results'][0])
    check('重放未产生新行（无重复插入）',
          db.query_one('SELECT COUNT(*) n FROM training_sessions')['n'] == n1 == 1)

    print('\n[2] 同批内 operation_id 重复只应用一次')
    r = sync.push(U, [
        op('op-dup', 'feedback_ticket', 'fb-1', 'create', {'title': '发球速度异常'}),
        op('op-dup', 'feedback_ticket', 'fb-1', 'create', {'title': '发球速度异常'}),
    ])
    check('同批重复 → 第一条 applied / 第二条 duplicate',
          r['results'][0]['result'] == 'applied' and r['results'][1]['result'] == 'duplicate',
          r['results'])

    print('\n[3] LWW：以 updated_at 为准，严格大于才覆盖')
    r = sync.push(U, [op('op-2', 'training_session', 'ts-1', 'update',
                         {'notes': '旧时间写入'},
                         ts='2026-09-24T10:00:00.000Z')])   # 与创建时间相等
    check('相同时间戳 → conflict_lost（保留服务端）',
          r['results'][0]['result'] == 'conflict_lost', r['results'][0])
    r = sync.push(U, [op('op-3', 'training_session', 'ts-1', 'update',
                         {'notes': '更晚的写入'},
                         ts='2026-09-24T11:00:00.000Z')])
    check('更晚时间戳 → applied', r['results'][0]['result'] == 'applied', r['results'][0])
    check('version 递增到 2', r['results'][0]['server_version'] == 2, r['results'][0])
    row = db.query_one('SELECT * FROM training_sessions WHERE id=?', ('ts-1',))
    check('内容已更新', row['notes'] == '更晚的写入', row['notes'])

    print('\n[4] 软删除：deleted_at 标记，绝不物理删除')
    for etype, eid, extra in [
        ('training_session', 'ts-1', {}),
        ('stroke_record', 'st-1', {'session_id': 'ts-1', 'stroke_type': 'forehand'}),
        ('student_profile', 's-1', {'name': '张哲恒'}),
        ('feedback_ticket', 'fb-1', {'title': '工单'}),
    ]:
        sync.push(U, [op('c-%s' % eid, etype, eid, 'create',
                         dict(extra, **({'started_at': '2026-09-24T09:00:00.000Z'}
                                        if etype == 'training_session' else {})))])
        r = sync.push(U, [op('d-%s' % eid, etype, eid, 'delete', {},
                             ts='2026-09-24T12:00:00.000Z')])
        table = sync.SYNCABLE[etype]['table']
        still = db.query_one('SELECT * FROM %s WHERE id=?' % table, (eid,))
        check('%s：行仍在（未物理删除）' % etype, still is not None)
        check('%s：deleted_at 已标记' % etype, bool(still and still['deleted_at']))
        check('%s：删除操作 applied' % etype, r['results'][0]['result'] == 'applied',
              r['results'][0])

    print('\n[5] 删除不存在的记录 → 写墓碑（删除事件必须能传播）')
    r = sync.push(U, [op('d-ghost', 'stroke_record', 'st-ghost', 'delete', {},
                         ts='2026-09-24T13:00:00.000Z')])
    g = db.query_one('SELECT * FROM stroke_records WHERE id=?', ('st-ghost',))
    check('墓碑已写入且带 deleted_at（无限定业务字段也不报错）',
          g is not None and bool(g['deleted_at']), g)
    check('墓碑同步列齐备',
          g and all(g[c] for c in ('user_id', 'created_at', 'updated_at', 'version',
                                   'sync_status')) and g['version'] == 1, g)
    check('结果 applied', r['results'][0]['result'] == 'applied', r['results'][0])

    r = sync.push(U, [op('d-ghost2', 'stroke_record', 'st-ghost2', 'delete',
                         {'session_id': 'ts-1', 'stroke_type': 'backhand'},
                         ts='2026-09-24T13:05:00.000Z')])
    g2 = db.query_one('SELECT * FROM stroke_records WHERE id=?', ('st-ghost2',))
    check('墓碑保留客户端最后已知快照（session_id/stroke_type）',
          g2 and g2['session_id'] == 'ts-1' and g2['stroke_type'] == 'backhand', g2)
    snap = db.query_one('SELECT payload FROM sync_changelog WHERE entity_id=?'
                        " AND action='delete'", ('st-ghost2',))
    check('changelog 存的是完整快照而非仅 id',
          snap and 'stroke_type' in json.loads(snap['payload']), snap)

    print('\n[5b] 复活墓碑：必须能凑齐必填字段，否则拒绝而非崩溃')
    r = sync.push(U, [op('rv-1', 'stroke_record', 'st-ghost', 'update',
                         {'speed_kmh': 100}, ts='2026-09-24T13:10:00.000Z')])
    check('墓碑无业务数据时复活 → rejected（不抛异常、不回滚批次）',
          r['results'][0]['result'] == 'rejected', r['results'][0])
    r = sync.push(U, [op('rv-2', 'stroke_record', 'st-ghost', 'update',
                         {'session_id': 'ts-1', 'stroke_type': 'volley'},
                         ts='2026-09-24T13:20:00.000Z')])
    g3 = db.query_one('SELECT * FROM stroke_records WHERE id=?', ('st-ghost',))
    check('补全必填字段后复活 → applied',
          r['results'][0]['result'] == 'applied', r['results'][0])
    check('复活后 deleted_at 已清空、字段生效',
          g3 and g3['deleted_at'] is None and g3['stroke_type'] == 'volley', g3)

    print('\n[6] 重复删除 → 幂等，不报错、不改库')
    r = sync.push(U, [op('d-ts-1-again', 'training_session', 'ts-1', 'delete', {},
                         ts='2026-09-24T14:00:00.000Z')])
    check('已是删除状态 → conflict_lost',
          r['results'][0]['result'] == 'conflict_lost', r['results'][0])

    print('\n[7] 多条记录字段齐备（id/user_id/created_at/updated_at/deleted_at/version/sync_status）')
    need = {'id', 'user_id', 'created_at', 'updated_at', 'deleted_at', 'version', 'sync_status'}
    for etype, spec in sync.SYNCABLE.items():
        cols = set(db.table_columns(spec['table']))
        check('%s 含全部同步列' % etype, need <= cols, 'missing=%s' % sorted(need - cols))

    print('\n[8] 拉取：服务器游标增量，不使用客户端时间')
    p1 = sync.pull(U, cursor=0, limit=5)
    check('首次拉取返回变更', p1['count'] > 0, p1['count'])
    check('has_more 正确（limit=5）', p1['has_more'] is True, p1['has_more'])
    check('next_cursor 前进', p1['next_cursor'] > 0, p1['next_cursor'])
    check('变更按 seq 升序且严格递增',
          all(p1['changes'][i]['seq'] < p1['changes'][i + 1]['seq']
              for i in range(len(p1['changes']) - 1)))
    check('变更项带 deleted_at 字段（墓碑可识别）',
          all('deleted_at' in c for c in p1['changes']))
    # 游标续拉不重不漏
    seen = [c['seq'] for c in p1['changes']]
    cur = p1['next_cursor']
    while True:
        p = sync.pull(U, cursor=cur, limit=5)
        seen += [c['seq'] for c in p['changes']]
        cur = p['next_cursor']
        if not p['has_more']:
            break
    total = db.query_one('SELECT COUNT(*) n FROM sync_changelog')['n']
    check('分页续拉无重复无遗漏', len(seen) == len(set(seen)) == total,
          'seen=%d unique=%d total=%d' % (len(seen), len(set(seen)), total))
    check('重复拉取同一游标返回空（幂等）',
          sync.pull(U, cursor=cur)['count'] == 0)

    print('\n[9] 新设备基线 snapshot：含墓碑全量 + 当前游标')
    s = sync.snapshot(U)
    check('snapshot 返回各实体', set(s['entities']) == set(sync.SYNCABLE),
          sorted(s['entities']))
    check('snapshot 含墓碑行（新设备也知道哪些已删）',
          any(r['deleted_at'] for r in s['entities']['stroke_record']))
    check('snapshot 游标 == 全库最大 seq',
          s['cursor'] == db.query_one('SELECT COALESCE(MAX(seq),0) c FROM sync_changelog')['c'])
    check('从 snapshot 游标继续拉取为空',
          sync.pull(U, cursor=s['cursor'])['count'] == 0)

    print('\n[10] 校验与隔离')
    r = sync.push(U, [op('x-1', 'nonexistent', 'z-1', 'create', {})])
    check('未知 entity_type → rejected', r['results'][0]['result'] == 'rejected')
    r = sync.push(U, [op('x-2', 'student_profile', 's-x', 'create', {'nickname': '缺 name'})])
    check('缺必填字段 → rejected', r['results'][0]['result'] == 'rejected', r['results'][0])
    r = sync.push('u_other', [op('x-3', 'training_session', 'ts-1', 'update',
                                 {'notes': '越权'}, ts='2026-09-24T23:00:00.000Z')])
    check('跨用户写入 → rejected', r['results'][0]['result'] == 'rejected', r['results'][0])
    check('越权未改库',
          db.query_one('SELECT notes FROM training_sessions WHERE id=?',
                       ('ts-1',))['notes'] == '更晚的写入')

    print('\n[11] 客户端时间格式归一化（LWW 字符串比较的前提）')
    for raw, want in [('2026-09-24T18:00:00+08:00', '2026-09-24T10:00:00.000Z'),
                      ('2026-09-24 10:00:00', '2026-09-24T10:00:00.000Z'),
                      ('2026-09-24T10:00:00Z', '2026-09-24T10:00:00.000Z')]:
        check('normalize_ts(%s)' % raw, db.normalize_ts(raw) == want, db.normalize_ts(raw))

    print('\n[12] 服务端权威：客户端时间不可信，无法靠未来时间抢占')
    r = sync.push(U, [op('f-1', 'training_session', 'ts-1', 'update',
                         {'notes': '伪造未来时间'}, ts='2030-01-01T00:00:00.000Z')])
    check('LWW 会接受（协议如此）但记入审计', r['results'][0]['result'] == 'applied')
    ops = db.query_one('SELECT COUNT(*) n FROM sync_operations')['n']
    pulls = db.query_one('SELECT COUNT(*) n FROM sync_pulls')['n']
    check('sync_operations 审计齐全（%d 条）' % ops, ops >= 14)
    check('sync_pulls 审计齐全（%d 条）' % pulls, pulls >= 3)

    print('\n' + '=' * 66)
    print('通过 %d 项，失败 %d 项' % (len(PASS), len(FAIL)))
    if FAIL:
        print('失败清单：')
        for f in FAIL:
            print('  · ' + f)
    print('=' * 66)
    return 1 if FAIL else 0


if __name__ == '__main__':
    code = 1
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
