from __future__ import annotations

import json
import uuid

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from telethon import errors

from app.config import get_settings
from app.crypto import encrypt_text, decrypt_text
from app.db import (
    init_db,
    create_pending_auth,
    get_pending_auth,
    update_pending_auth,
    upsert_account,
    list_accounts,
    get_account,
    mark_account_offboarded,
    add_log,
)
from app.telegram_ops import (
    request_login_code,
    verify_login_code,
    verify_two_factor_password,
    onboard_employee_session,
    remove_company_folder,
)

app = FastAPI(title='Telegram Enterprise Onboarding')
app.mount('/static', StaticFiles(directory='app/static'), name='static')
templates = Jinja2Templates(directory='app/templates')


@app.on_event('startup')
async def startup() -> None:
    init_db()


def _require_admin(request: Request) -> None:
    settings = get_settings()
    token = request.headers.get('x-admin-token') or request.query_params.get('token')
    if token != settings.admin_token:
        raise HTTPException(status_code=401, detail='Invalid admin token')


def _public_error(exc: Exception) -> str:
    # Keep errors useful but avoid exposing internal session details.
    if isinstance(exc, errors.PhoneNumberInvalidError):
        return '手机号格式无效，请使用国际区号，例如 +8613800000000。'
    if isinstance(exc, errors.PhoneCodeInvalidError):
        return '验证码错误。'
    if isinstance(exc, errors.PhoneCodeExpiredError):
        return '验证码已过期，请重新获取。'
    if isinstance(exc, errors.FloodWaitError):
        return f'Telegram 限制请求过快，请稍后再试。等待秒数：{exc.seconds}'
    if isinstance(exc, errors.PasswordHashInvalidError):
        return '二步验证密码错误。'
    return str(exc)


@app.get('/', response_class=HTMLResponse)
async def index(request: Request):
    settings = get_settings()
    return templates.TemplateResponse('index.html', {
        'request': request,
        'folder_title': settings.folder_title,
        'bot_usernames': settings.bot_usernames,
    })


@app.post('/api/auth/request-code')
async def api_request_code(phone: str = Form(...)):
    phone = phone.strip()
    auth_id = str(uuid.uuid4())
    try:
        result = await request_login_code(phone)
        create_pending_auth(
            auth_id=auth_id,
            phone=phone,
            phone_code_hash=result.phone_code_hash,
            temp_session_enc=encrypt_text(result.temp_session),
        )
        add_log(phone, 'request_code', 'info', 'Login code requested')
        return {'ok': True, 'auth_id': auth_id, 'message': '验证码已发送，请在 Telegram 官方 App 中查看。'}
    except Exception as exc:
        add_log(phone, 'request_code', 'error', _public_error(exc))
        raise HTTPException(status_code=400, detail=_public_error(exc))


@app.post('/api/auth/verify-code')
async def api_verify_code(auth_id: str = Form(...), code: str = Form(...), consent: str = Form('')):
    pending = get_pending_auth(auth_id)
    if not pending:
        raise HTTPException(status_code=404, detail='授权流程不存在或已过期。')
    if consent != 'yes':
        raise HTTPException(status_code=400, detail='需要员工确认授权后才能继续。')

    phone = pending['phone']
    try:
        temp_session = decrypt_text(pending['temp_session_enc'])
        login_result = await verify_login_code(
            phone=phone,
            code=code,
            phone_code_hash=pending['phone_code_hash'],
            temp_session=temp_session,
        )

        if login_result.needs_password:
            update_pending_auth(
                auth_id,
                status='password_required',
                temp_session_enc=encrypt_text(login_result.session),
            )
            return {'ok': True, 'needs_password': True, 'message': '该账号开启了二步验证，请输入 Telegram 2FA 密码。'}

        onboarding = await onboard_employee_session(login_result.session)
        result_text = json.dumps(onboarding, ensure_ascii=False)
        upsert_account(
            phone=phone,
            telegram_user_id=login_result.user_id,
            username=login_result.username,
            first_name=login_result.first_name,
            session_enc=encrypt_text(login_result.session),
            last_result=result_text,
        )
        update_pending_auth(auth_id, status='done')
        add_log(phone, 'onboard', 'info', result_text)
        return {'ok': True, 'needs_password': False, 'onboarding': onboarding}
    except Exception as exc:
        add_log(phone, 'verify_code', 'error', _public_error(exc))
        raise HTTPException(status_code=400, detail=_public_error(exc))


@app.post('/api/auth/verify-password')
async def api_verify_password(auth_id: str = Form(...), password: str = Form(...)):
    pending = get_pending_auth(auth_id)
    if not pending:
        raise HTTPException(status_code=404, detail='授权流程不存在或已过期。')
    if pending['status'] != 'password_required':
        raise HTTPException(status_code=400, detail='当前流程不需要二步验证密码。')

    phone = pending['phone']
    try:
        temp_session = decrypt_text(pending['temp_session_enc'])
        login_result = await verify_two_factor_password(temp_session, password)
        onboarding = await onboard_employee_session(login_result.session)
        result_text = json.dumps(onboarding, ensure_ascii=False)
        upsert_account(
            phone=phone,
            telegram_user_id=login_result.user_id,
            username=login_result.username,
            first_name=login_result.first_name,
            session_enc=encrypt_text(login_result.session),
            last_result=result_text,
        )
        update_pending_auth(auth_id, status='done')
        add_log(phone, 'onboard_2fa', 'info', result_text)
        return {'ok': True, 'onboarding': onboarding}
    except Exception as exc:
        add_log(phone, 'verify_password', 'error', _public_error(exc))
        raise HTTPException(status_code=400, detail=_public_error(exc))


@app.get('/admin/accounts')
async def admin_accounts(request: Request):
    _require_admin(request)
    return {'ok': True, 'accounts': list_accounts()}


@app.post('/admin/accounts/{account_id}/rerun')
async def admin_rerun(account_id: int, request: Request):
    _require_admin(request)
    account = get_account(account_id)
    if not account or not account.get('session_enc'):
        raise HTTPException(status_code=404, detail='账号不存在或 session 已删除。')
    try:
        session = decrypt_text(account['session_enc'])
        onboarding = await onboard_employee_session(session)
        result_text = json.dumps(onboarding, ensure_ascii=False)
        upsert_account(
            phone=account['phone'],
            telegram_user_id=onboarding['employee']['user_id'],
            username=onboarding['employee']['username'],
            first_name=onboarding['employee']['first_name'],
            session_enc=account['session_enc'],
            last_result=result_text,
        )
        add_log(account['phone'], 'admin_rerun', 'info', result_text)
        return {'ok': True, 'onboarding': onboarding}
    except Exception as exc:
        add_log(account['phone'], 'admin_rerun', 'error', _public_error(exc))
        raise HTTPException(status_code=400, detail=_public_error(exc))


@app.post('/admin/accounts/{account_id}/offboard')
async def admin_offboard(account_id: int, request: Request, remove_remote_folder: bool = Form(False)):
    _require_admin(request)
    account = get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail='账号不存在。')

    result = 'local_session_removed'
    try:
        if remove_remote_folder and account.get('session_enc'):
            session = decrypt_text(account['session_enc'])
            result = await remove_company_folder(session)
        mark_account_offboarded(account_id, result)
        add_log(account['phone'], 'offboard', 'info', result)
        return {'ok': True, 'result': result}
    except Exception as exc:
        add_log(account['phone'], 'offboard', 'error', _public_error(exc))
        raise HTTPException(status_code=400, detail=_public_error(exc))


@app.get('/health')
async def health():
    return JSONResponse({'ok': True})
