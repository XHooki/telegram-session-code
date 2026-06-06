from cryptography.fernet import Fernet
from app.config import get_settings


def _fernet() -> Fernet:
    return Fernet(get_settings().session_secret_key.encode())


def encrypt_text(value: str) -> str:
    return _fernet().encrypt(value.encode('utf-8')).decode('utf-8')


def decrypt_text(value: str) -> str:
    return _fernet().decrypt(value.encode('utf-8')).decode('utf-8')
