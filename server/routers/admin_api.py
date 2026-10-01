# -*- coding: utf-8 -*-
"""管理后台登录/登出与会话管理接口。

    GET  /login              登录页
    POST /login              提交口令 → 下发服务端会话 Cookie
    GET  /logout             吊销当前会话并清 Cookie
    GET  /api/admin/me       当前登录者信息
    POST /api/admin/password 修改口令（改后该账号其它会话一律失效）

口令校验与 Cookie 细节见 `server/adminauth.py`；这里只负责 HTTP 层。
"""
import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from server import adminauth

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(HERE, 'templates'))

router = APIRouter(include_in_schema=False)


def safe_next(value):
    """只接受站内相对路径，挡掉 `//evil.com` 这类协议相对跳转（开放重定向）。"""
    v = (value or '').strip()
    if not v.startswith('/') or v.startswith('//'):
        return '/'
    return v


def _cookie(resp, token, expires_at, max_age):
    resp.set_cookie(
        adminauth.SESSION_COOKIE, token or '',
        max_age=max_age, expires=max_age,
        httponly=True,          # JS 读不到 —— 挡 XSS 窃取会话
        samesite='lax',         # 挡跨站表单 CSRF，同时不影响站内跳转
        path='/',
    )
    return resp


@router.get('/login', response_class=HTMLResponse)
def login_page(request: Request, next: str = '/'):
    token = request.cookies.get(adminauth.SESSION_COOKIE)
    if adminauth.session_of(token):
        return RedirectResponse(safe_next(next), status_code=302)
    return templates.TemplateResponse(request, 'pages/login.html', {
        'request': request,
        'error': None,
        'next_url': safe_next(next),
    })


@router.post('/login')
def login_submit(request: Request,
                 username: str = Form(''),
                 password: str = Form(''),
                 next: str = Form('/')):
    admin = adminauth.authenticate(username, password)
    target = safe_next(next)
    if not admin:
        return templates.TemplateResponse(request, 'pages/login.html', {
            'request': request,
            'error': '用户名或口令不正确。',
            'next_url': target,
        }, status_code=401)

    token, expires_at = adminauth.create_session(
        admin,
        user_agent=request.headers.get('user-agent'),
        ip=request.client.host if request.client else None,
    )
    resp = RedirectResponse(target, status_code=302)
    return _cookie(resp, token, expires_at, adminauth.SESSION_TTL_HOURS * 3600)


@router.get('/logout')
def logout(request: Request):
    token = request.cookies.get(adminauth.SESSION_COOKIE)
    adminauth.revoke(token)
    resp = RedirectResponse('/login', status_code=302)
    return _cookie(resp, '', None, 0)


@router.get('/api/admin/me')
def whoami(request: Request):
    token = request.cookies.get(adminauth.SESSION_COOKIE)
    sess = adminauth.session_of(token)
    if not sess:
        return JSONResponse(status_code=401, content={'error': 'unauthenticated'})
    admin = adminauth.get_admin(sess['admin_id']) or {}
    return {
        'username': sess['username'],
        'display_name': admin.get('display_name'),
        'role': admin.get('role'),
        'last_login_at': admin.get('last_login_at'),
        'session_expires_at': sess['expires_at'],
        'ingest_key_configured': bool(os.environ.get('NETPULSE_INGEST_KEY', '').strip()),
    }


@router.post('/api/admin/password')
def change_password(request: Request, old: str = Form(''), new: str = Form('')):
    token = request.cookies.get(adminauth.SESSION_COOKIE)
    sess = adminauth.session_of(token)
    if not sess:
        return JSONResponse(status_code=401, content={'error': 'unauthenticated'})
    if len(new or '') < 8:
        return JSONResponse(status_code=400,
                            content={'error': 'too_short', 'detail': '新口令至少 8 位'})
    admin = adminauth.get_admin(sess['admin_id']) or {}
    if not adminauth.verify_password(old, admin.get('password_hash', '')):
        return JSONResponse(status_code=403,
                            content={'error': 'bad_old_password'})
    adminauth.change_password(sess['admin_id'], new)
    return {'ok': True, 'detail': '口令已更新，请重新登录。'}
