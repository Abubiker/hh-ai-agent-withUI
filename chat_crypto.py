"""Шифрование переписки в чате (content/image_ref в chat_turns) — не вся
SQLite целиком, точечно эти два поля. Ключ привязан к этому компьютеру
(см. _machine_id): скопированная на другую машину или после переустановки
системы база расшифровке не поддаётся — осознанный компромисс, который
защищает именно от кражи/находки файла с диска, а не от входа под тем же
пользователем на этой же машине."""
import base64
import hashlib
import os
import platform
import subprocess

from cryptography.fernet import Fernet, InvalidToken

from settings import user_file

UNREADABLE_PLACEHOLDER = "[не удалось расшифровать это сообщение]"

_key_cache = None


def _machine_id() -> str:
    try:
        if platform.system() == "Darwin":
            out = subprocess.check_output(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], text=True, timeout=5)
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    return line.split('"')[-2]
        elif platform.system() == "Linux":
            with open("/etc/machine-id") as f:
                return f.read().strip()
        elif platform.system() == "Windows":
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
            return winreg.QueryValueEx(key, "MachineGuid")[0]
    except Exception:
        pass
    return ""  # не нашли — соль ниже всё равно даст стабильный (но не machine-bound) ключ


def _get_key() -> bytes:
    global _key_cache
    if _key_cache is not None:
        return _key_cache
    salt_path = user_file("chat.salt")
    if salt_path.exists():
        salt = salt_path.read_bytes()
    else:
        salt = os.urandom(16)
        salt_path.write_bytes(salt)
        salt_path.chmod(0o600)
    material = _machine_id().encode() + salt
    digest = hashlib.sha256(material).digest()
    _key_cache = base64.urlsafe_b64encode(digest)
    return _key_cache


def encrypt(text: str) -> str:
    return Fernet(_get_key()).encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return Fernet(_get_key()).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return UNREADABLE_PLACEHOLDER
