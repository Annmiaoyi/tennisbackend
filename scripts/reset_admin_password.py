# -*- coding: utf-8 -*-
"""重置管理后台账号口令。

为什么需要这个脚本
------------------
`admin_users.password_hash` 是 PBKDF2 单向哈希，**口令一旦忘记/丢失就无法反推**。
2026-09-29 就发生过一次：`scripts/verify_layers.py` 的隔离实例把主库的
`var/admin_initial_password.txt` 覆盖成了测试口令，主库真实口令就此不可知，
唯一出路就是「直接改哈希」—— 也就是本脚本做的那一步。

（根因已修：口令文件路径现在跟随数据目录，见 `server/adminauth.py`
 的 `password_file()`；本脚本同时也是那道修复的**恢复手段**。）

用法
----
    # 生成一个随机口令（推荐）
    .venv/Scripts/python scripts/reset_admin_password.py

    # 指定口令
    .venv/Scripts/python scripts/reset_admin_password.py --password 'MyPassw0rd'

    # 直接在终端显示新口令（便于复制；注意终端历史会留痕）
    .venv/Scripts/python scripts/reset_admin_password.py --print

    # 看一下有哪些账号
    .venv/Scripts/python scripts/reset_admin_password.py --list

做的事
------
1. 更新 `admin_users.password_hash`
2. **吊销该账号的全部会话**（改了口令，旧会话必须立刻失效）
3. 把新口令写回口令文件（默认路径见 `server/adminauth.py:password_file()`）
"""
import argparse
import os
import secrets
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from server import adminauth, db          # noqa: E402


def _banner(text):
    print('=' * 66)
    print(' ' + text)
    print('=' * 66)


def main():
    ap = argparse.ArgumentParser(description='重置 AceMate 管理后台口令')
    ap.add_argument('--username', default=adminauth.DEFAULT_USERNAME,
                    help='要重置的账号（默认 admin）')
    ap.add_argument('--password', default='',
                    help='新口令；不传则随机生成 16 字符')
    ap.add_argument('--print', dest='show', action='store_true',
                    help='把新口令打印到终端（默认只写入文件）')
    ap.add_argument('--list', dest='do_list', action='store_true',
                    help='只列出已有账号，不修改任何东西')
    args = ap.parse_args()

    _banner('AceMate 管理后台口令重置')
    print(' 数据库      ：%s' % db.db_path())
    print(' 口令文件    ：%s' % adminauth.password_file())
    print()

    # 确保库与表都在（库不存在时会建出来）
    db.init_db()
    adminauth.init()                       # 建 admin_users/admin_sessions；无账号时会创建默认账号

    conn = db.connect()
    rows = conn.execute(
        'SELECT id, username, display_name, role, is_active, created_at,'
        ' last_login_at FROM admin_users ORDER BY created_at').fetchall()

    if not rows:
        print('⚠️  库里一个账号都没有，且自动创建也失败了，请检查日志。')
        return 1

    if args.do_list:
        print('已有账号（%d 个）：' % len(rows))
        for r in rows:
            print('  - %-12s role=%-6s active=%s created=%s last_login=%s'
                  % (r['username'], r['role'], r['is_active'],
                     r['created_at'], r['last_login_at']))
        return 0

    target = None
    for r in rows:
        if r['username'] == args.username:
            target = r
    if target is None:
        print('✗ 账号不存在：%s' % args.username)
        print('  现有账号：%s' % ', '.join(r['username'] for r in rows))
        print('  提示：用 --list 查看，或用 --username 指定正确的账号名。')
        return 2

    new_pw = args.password or secrets.token_urlsafe(12)
    if len(new_pw) < 8:
        print('✗ 口令太短（至少 8 位）。')
        return 2

    before = conn.execute('SELECT COUNT(*) FROM admin_sessions WHERE admin_id=?',
                          (target['id'],)).fetchone()[0]
    # change_password 内部：更新哈希 + 删除该账号的**全部**会话
    adminauth.change_password(target['id'], new_pw)
    after = conn.execute('SELECT COUNT(*) FROM admin_sessions WHERE admin_id=?',
                         (target['id'],)).fetchone()[0]

    path = adminauth.password_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('username: %s\npassword: %s\n' % (target['username'], new_pw))

    # 立刻回验一次：确认写进库的哈希真的能通过校验（避免「改了但没生效」）
    verify_ok = adminauth.authenticate(target['username'], new_pw) is not None

    print('✓ 账号 %s 口令已重置' % target['username'])
    print('  旧会话吊销：%d -> %d 条' % (before, after))
    print('  哈希回验    ：%s' % ('PASS' if verify_ok else 'FAIL（异常，请检查）'))
    print('  新口令已写入：%s' % path)
    print()
    if args.show:
        print('  新口令：%s' % new_pw)
    else:
        print('  新口令**未**打印到终端 —— 请打开上面那个文件查看')
        print('  （想直接显示就加 --print）')
    return 0 if verify_ok else 3


if __name__ == '__main__':
    sys.exit(main())
