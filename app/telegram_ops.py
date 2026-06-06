from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from telethon import TelegramClient, functions, types, errors
from telethon.sessions import StringSession

from app.config import get_settings

USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{5,32}$')


@dataclass
class LoginCodeResult:
    phone_code_hash: str
    temp_session: str


@dataclass
class LoginResult:
    session: str
    user_id: int
    username: str | None
    first_name: str | None
    needs_password: bool = False


def normalize_username(username: str) -> str:
    value = username.strip().lstrip('@')
    if not USERNAME_RE.fullmatch(value):
        raise ValueError(f'Invalid Telegram username: {username}')
    return value.lower()


def _new_client(session: str | None = None) -> TelegramClient:
    settings = get_settings()
    return TelegramClient(
        StringSession(session or ''),
        settings.tg_api_id,
        settings.tg_api_hash,
        receive_updates=False,
        sequential_updates=True,
    )


def _session_save(client: TelegramClient) -> str:
    return StringSession.save(client.session)


def _title_text(value: Any) -> str:
    if hasattr(value, 'text'):
        return str(value.text)
    return str(value)


def _make_title(value: str) -> Any:
    # Current Telegram layers use TextWithEntities for folder titles.
    # Telethon 1.43 accepts this object for DialogFilter.title.
    return types.TextWithEntities(text=value, entities=[])


def _dialog_filters(value: Any) -> list[Any]:
    return list(getattr(value, 'filters', value))


def _peer_key(peer: Any) -> tuple[str, int] | None:
    for attr, kind in (('user_id', 'user'), ('channel_id', 'channel'), ('chat_id', 'chat')):
        if hasattr(peer, attr):
            return kind, int(getattr(peer, attr))
    return None


async def request_login_code(phone: str) -> LoginCodeResult:
    client = _new_client()
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        return LoginCodeResult(
            phone_code_hash=sent.phone_code_hash,
            temp_session=_session_save(client),
        )
    finally:
        await client.disconnect()


async def verify_login_code(phone: str, code: str, phone_code_hash: str, temp_session: str) -> LoginResult:
    client = _new_client(temp_session)
    await client.connect()
    try:
        try:
            user = await client.sign_in(phone=phone, code=code.strip(), phone_code_hash=phone_code_hash)
        except errors.SessionPasswordNeededError:
            return LoginResult(session=_session_save(client), user_id=0, username=None, first_name=None, needs_password=True)

        return LoginResult(
            session=_session_save(client),
            user_id=int(user.id),
            username=getattr(user, 'username', None),
            first_name=getattr(user, 'first_name', None),
        )
    finally:
        await client.disconnect()


async def verify_two_factor_password(temp_session: str, password: str) -> LoginResult:
    client = _new_client(temp_session)
    await client.connect()
    try:
        user = await client.sign_in(password=password)
        return LoginResult(
            session=_session_save(client),
            user_id=int(user.id),
            username=getattr(user, 'username', None),
            first_name=getattr(user, 'first_name', None),
        )
    finally:
        await client.disconnect()


async def resolve_exact_bot(client: TelegramClient, username: str) -> tuple[types.User, Any]:
    target = normalize_username(username)
    resolved = await client(functions.contacts.ResolveUsernameRequest(username=target))

    if not resolved.users:
        raise RuntimeError(f'@{username} not found')

    user = resolved.users[0]
    actual = normalize_username(getattr(user, 'username', '') or '')
    if actual != target:
        raise RuntimeError(f'Username mismatch: expected @{target}, got @{actual}')
    if not bool(getattr(user, 'bot', False)):
        raise RuntimeError(f'@{target} is not a bot')

    bot_peer = await client.get_input_entity(user)
    return user, bot_peer


async def start_bot(client: TelegramClient, bot_peer: Any, start_param: str = '') -> None:
    try:
        await client(functions.messages.StartBotRequest(
            bot=bot_peer,
            peer=bot_peer,
            start_param=start_param,
        ))
    except errors.RPCError as exc:
        # Bot may already be started or Telegram may return a harmless RPC edge case.
        # Re-raise only flood waits and auth problems; log other errors at caller level if needed.
        if isinstance(exc, errors.FloodWaitError):
            raise
        # Do not fail the whole onboarding if the dialog already exists.


async def ensure_folder_contains_peers(client: TelegramClient, folder_title: str, peers: list[Any]) -> int:
    filters = _dialog_filters(await client(functions.messages.GetDialogFiltersRequest()))

    target_filter = None
    used_ids: set[int] = set()
    for dialog_filter in filters:
        if hasattr(dialog_filter, 'id'):
            used_ids.add(int(dialog_filter.id))
        if isinstance(dialog_filter, types.DialogFilter) and _title_text(dialog_filter.title) == folder_title:
            target_filter = dialog_filter

    if target_filter is not None:
        existing_keys = {_peer_key(peer) for peer in getattr(target_filter, 'include_peers', [])}
        existing_keys.discard(None)
        include_peers = list(getattr(target_filter, 'include_peers', []))
        for peer in peers:
            key = _peer_key(peer)
            if key not in existing_keys:
                include_peers.append(peer)
                existing_keys.add(key)
        target_filter.include_peers = include_peers
        target_filter.bots = True

        await client(functions.messages.UpdateDialogFilterRequest(
            id=int(target_filter.id),
            filter=target_filter,
        ))
        return int(target_filter.id)

    folder_id = next((i for i in range(2, 99) if i not in used_ids), 2)
    new_filter = types.DialogFilter(
        id=folder_id,
        title=_make_title(folder_title),
        pinned_peers=[],
        include_peers=peers,
        exclude_peers=[],
        contacts=False,
        non_contacts=False,
        groups=False,
        broadcasts=False,
        bots=True,
        exclude_muted=False,
        exclude_read=False,
        exclude_archived=False,
        title_noanimate=False,
        emoticon=None,
        color=None,
    )

    await client(functions.messages.UpdateDialogFilterRequest(
        id=folder_id,
        filter=new_filter,
    ))
    return folder_id


async def onboard_employee_session(session: str) -> dict[str, Any]:
    settings = get_settings()
    client = _new_client(session)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError('Telegram session is not authorized')

        me = await client.get_me()
        bot_peers: list[Any] = []
        bots: list[dict[str, Any]] = []

        # Low-speed serial execution avoids unnecessary Telegram flood limits.
        for username in settings.bot_usernames:
            bot_user, bot_peer = await resolve_exact_bot(client, username)
            await start_bot(client, bot_peer)
            bot_peers.append(bot_peer)
            bots.append({
                'username': getattr(bot_user, 'username', None),
                'id': int(bot_user.id),
                'first_name': getattr(bot_user, 'first_name', None),
            })
            await asyncio.sleep(0.8)

        folder_id = await ensure_folder_contains_peers(client, settings.folder_title, bot_peers)

        return {
            'employee': {
                'user_id': int(me.id),
                'username': getattr(me, 'username', None),
                'first_name': getattr(me, 'first_name', None),
            },
            'folder': {
                'id': folder_id,
                'title': settings.folder_title,
            },
            'bots': bots,
        }
    finally:
        await client.disconnect()


async def remove_company_folder(session: str) -> str:
    """Optional offboarding cleanup: remove only the configured company folder.

    This does not delete chats, messages, or the employee's Telegram account.
    """
    settings = get_settings()
    client = _new_client(session)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return 'session_not_authorized'
        filters = _dialog_filters(await client(functions.messages.GetDialogFiltersRequest()))
        for dialog_filter in filters:
            if isinstance(dialog_filter, types.DialogFilter) and _title_text(dialog_filter.title) == settings.folder_title:
                await client(functions.messages.UpdateDialogFilterRequest(
                    id=int(dialog_filter.id),
                    filter=None,
                ))
                return 'remote_folder_removed'
        return 'remote_folder_not_found'
    finally:
        await client.disconnect()
