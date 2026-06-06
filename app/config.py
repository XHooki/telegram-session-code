import os
from dotenv import load_dotenv
from dataclasses import dataclass


def _split_usernames(value: str) -> list[str]:
    items: list[str] = []
    for raw in value.split(','):
        username = raw.strip().lstrip('@')
        if username:
            items.append(username)
    return items


@dataclass(frozen=True)
class Settings:
    tg_api_id: int
    tg_api_hash: str
    session_secret_key: str
    bot_usernames: list[str]
    folder_title: str
    db_path: str
    admin_token: str


def get_settings() -> Settings:
    load_dotenv()
    api_id_raw = os.getenv('TG_API_ID', '0')
    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise RuntimeError('TG_API_ID must be an integer') from exc

    settings = Settings(
        tg_api_id=api_id,
        tg_api_hash=os.getenv('TG_API_HASH', '').strip(),
        session_secret_key=os.getenv('SESSION_SECRET_KEY', '').strip(),
        bot_usernames=_split_usernames(os.getenv('TG_BOT_USERNAMES', 'BotFather')),
        folder_title=os.getenv('TG_FOLDER_TITLE', '企业机器人').strip(),
        db_path=os.getenv('DB_PATH', 'data/app.db'),
        admin_token=os.getenv('ADMIN_TOKEN', 'change_me'),
    )

    if not settings.tg_api_id or not settings.tg_api_hash:
        raise RuntimeError('TG_API_ID and TG_API_HASH are required')
    if not settings.session_secret_key:
        raise RuntimeError('SESSION_SECRET_KEY is required')
    if not settings.bot_usernames:
        raise RuntimeError('At least one TG_BOT_USERNAMES value is required')
    if not settings.folder_title:
        raise RuntimeError('TG_FOLDER_TITLE is required')
    return settings
