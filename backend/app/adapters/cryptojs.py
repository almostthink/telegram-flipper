"""Шифрование в формате CryptoJS — ровно то, что ждёт Tonnel.

Площадка требует в теле покупки и выставления поле ``wtf``: это отметка
времени, зашифрованная на общем ключе. Ключ лежит открытым текстом в коде
мини-аппа, так что защитой от подделки это не является — скорее заслон от
самых простых ботов и признак того, что запрос собрал именно их фронтенд.
Нам остаётся повторить формат в точности: без него площадка отвечает
отказом, и покупка не проходит вовсе.

Формат — тот же, что у ``openssl enc -aes-256-cbc -md md5 -salt -base64``:

    base64( "Salted__" + salt(8) + AES-256-CBC(данные) )

Ключ и вектор выводятся из пароля и соли по OpenSSL EVP_BytesToKey на MD5.
MD5 здесь не выбор, а требование совместимости: так делает CryptoJS.

Шифруем через ``pyaes`` — он уже есть в сборке как зависимость Pyrogram, и
это чистый Python. Брать ради двух вызовов ``cryptography`` не стоит: на
Windows её в окружении может не оказаться вовсе.
"""

from __future__ import annotations

import base64
import hashlib
import os

import pyaes

#: Заголовок OpenSSL-совместимого контейнера. CryptoJS пишет ровно его.
MAGIC = b"Salted__"

SALT_SIZE = 8
KEY_SIZE = 32
IV_SIZE = 16
BLOCK_SIZE = 16


def derive_key_iv(passphrase: bytes, salt: bytes) -> tuple[bytes, bytes]:
    """EVP_BytesToKey на MD5 в одну итерацию — как в OpenSSL и CryptoJS."""
    data = b""
    block = b""
    while len(data) < KEY_SIZE + IV_SIZE:
        block = hashlib.md5(block + passphrase + salt).digest()
        data += block
    return data[:KEY_SIZE], data[KEY_SIZE : KEY_SIZE + IV_SIZE]


def _pad(data: bytes) -> bytes:
    """PKCS#7. Полный блок добавляется даже когда длина уже кратна — так
    получателю всегда есть что отрезать."""
    padding = BLOCK_SIZE - len(data) % BLOCK_SIZE
    return data + bytes([padding]) * padding


def _unpad(data: bytes) -> bytes:
    if not data:
        return data
    padding = data[-1]
    if padding < 1 or padding > BLOCK_SIZE or data[-padding:] != bytes([padding]) * padding:
        raise ValueError("Неверное дополнение — не тот ключ или испорченные данные")
    return data[:-padding]


def encrypt(text: str, passphrase: str, *, salt: bytes | None = None) -> str:
    """Шифруем строку так же, как ``CryptoJS.AES.encrypt(text, passphrase)``.

    ``salt`` задаётся только в тестах: в жизни он случайный, иначе шифртекст
    повторялся бы от запроса к запросу.
    """
    salt = salt if salt is not None else os.urandom(SALT_SIZE)
    key, iv = derive_key_iv(passphrase.encode("utf-8"), salt)

    aes = pyaes.AESModeOfOperationCBC(key, iv=iv)
    padded = _pad(text.encode("utf-8"))
    encrypted = b"".join(
        aes.encrypt(padded[offset : offset + BLOCK_SIZE])
        for offset in range(0, len(padded), BLOCK_SIZE)
    )
    return base64.b64encode(MAGIC + salt + encrypted).decode("ascii")


def decrypt(payload: str, passphrase: str) -> str:
    """Обратная операция. Нужна тестам: без неё формат нечем проверить."""
    blob = base64.b64decode(payload)
    if not blob.startswith(MAGIC):
        raise ValueError("Не формат CryptoJS: нет заголовка Salted__")

    salt = blob[SALT_SIZE : SALT_SIZE * 2]
    key, iv = derive_key_iv(passphrase.encode("utf-8"), salt)

    aes = pyaes.AESModeOfOperationCBC(key, iv=iv)
    body = blob[SALT_SIZE * 2 :]
    decrypted = b"".join(
        aes.decrypt(body[offset : offset + BLOCK_SIZE])
        for offset in range(0, len(body), BLOCK_SIZE)
    )
    return _unpad(decrypted).decode("utf-8")
