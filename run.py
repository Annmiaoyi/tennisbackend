# -*- coding: utf-8 -*-
"""AceMate 后端管理站 —— 启动器（供 start.cmd 调用，也可直接运行）。

    python run.py               等价于 python -m server.app
    python run.py --port 9000
    python run.py --seed        首次：建库 + 灌演示数据
    python run.py --reload      开发模式（改代码自动重载）

比直接 `python -m server.app` 多做的事：
  1. 先把工作目录切到项目根（双击 .cmd 时工作目录未必正确）；
  2. 启动前检查依赖与 CSS 产物，缺什么就给出**能直接复制**的修复命令，
     而不是甩一个 ModuleNotFoundError 堆栈。

所有面向用户的提示都用中文，且由 Python 输出（Windows 控制台可正确显示），
以避开 .cmd 文件在 GBK 代码页下的中文乱码问题。
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

LINE = '=' * 56


def preflight():
    """返回 (是否致命, 警告列表)。"""
    fatal = False

    try:
        import fastapi                                        # noqa: F401
        import uvicorn                                        # noqa: F401
        import jinja2                                         # noqa: F401
    except ImportError as e:
        fatal = True
        print(LINE)
        print(' 启动失败：当前解释器缺少依赖（%s）' % e.name)
        print(LINE)
        print(' 当前解释器：')
        print('   %s' % sys.executable)
        print()
        print(' 修复方式（任选其一）：')
        print('   1) 用项目自带虚拟环境启动：  start.cmd        （推荐）')
        print('   2) 在当前解释器装依赖：')
        print('      "%s" -m pip install -r requirements.txt' % sys.executable)
        print(LINE)

    warnings = []
    css = os.path.join(ROOT, 'web', 'assets', 'css', 'app.css')
    if not os.path.exists(css):
        warnings.append(
            '缺少 web/assets/css/app.css —— 页面会完全没有样式。\n'
            '     修复： npm run build:css')

    return fatal, warnings


def main():
    fatal, warnings = preflight()

    db_path = os.path.join(ROOT, 'var', 'acemate.db')
    print(LINE)
    print(' AceMate 后端管理站')
    print(LINE)
    print(' 解释器： %s' % sys.executable)
    print(' 数据库： %s' % db_path)
    if warnings:
        print()
        for w in warnings:
            print(' [警告] %s' % w)
    print(LINE)
    print()

    if fatal:
        sys.exit(1)

    from server.app import main as app_main
    app_main()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n已停止。')
