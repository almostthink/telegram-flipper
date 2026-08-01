"""Проверки шифрования в формате CryptoJS.

Tonnel требует в теле покупки и выставления поле ``wtf`` — отметку
времени, зашифрованную тем же способом, что и мини-апп. Ошибка в формате
не проявится ничем, кроме отказа площадки, и выглядеть будет как «покупка
почему-то не проходит». Поэтому формат закреплён тестами: и на
совместимость с OpenSSL, и на устойчивость к собственным изменениям.
"""

from __future__ import annotations

import base64

import pytest
from app.adapters.cryptojs import MAGIC, decrypt, derive_key_iv, encrypt

KEY = "yowtfisthispieceofshitiiit"


def test_round_trip():
    assert decrypt(encrypt("1754046467", KEY), KEY) == "1754046467"


def test_container_looks_like_openssl():
    """CryptoJS пишет тот же контейнер, что и openssl enc -salt."""
    blob = base64.b64decode(encrypt("42", KEY))

    assert blob.startswith(MAGIC)
    # Заголовок, соль и хотя бы один блок шифртекста.
    assert len(blob) >= 8 + 8 + 16


def test_salt_is_random():
    """Иначе шифртекст повторялся бы, и подпись стала бы константой."""
    assert encrypt("42", KEY) != encrypt("42", KEY)


def test_known_vector_decrypts():
    """Вектор снят с эталонной реализации (openssl -aes-256-cbc -md md5).

    Он и ловит подмену способа вывода ключа: round-trip своей же
    реализацией прошёл бы с любым, даже неверным.
    """
    payload = "U2FsdGVkX1+MvzkBqi9vbjlwQi+NQNaZgV28cOpeCJY="
    assert decrypt(payload, KEY) == "1754046467"


def test_key_and_iv_come_from_md5():
    """EVP_BytesToKey на MD5 в одну итерацию — требование совместимости."""
    import hashlib

    salt = b"12345678"
    key, iv = derive_key_iv(KEY.encode(), salt)

    first = hashlib.md5(KEY.encode() + salt).digest()
    assert key[:16] == first
    assert len(key) == 32
    assert len(iv) == 16


def test_wrong_key_is_rejected_loudly():
    """Тихо вернуть мусор хуже, чем упасть: мусор уедет на площадку."""
    with pytest.raises(ValueError):
        decrypt(encrypt("42", KEY), "другой ключ")


def test_foreign_payload_is_rejected():
    with pytest.raises(ValueError, match="Salted__"):
        decrypt(base64.b64encode("нет заголовка".encode()).decode(), KEY)


def test_padding_is_added_even_to_full_blocks():
    """PKCS#7: получателю всегда есть что отрезать, иначе съест данные."""
    text = "A" * 16
    blob = base64.b64decode(encrypt(text, KEY))
    body = blob[16:]

    assert len(body) == 32, "полный блок дополняется ещё одним"
    assert decrypt(encrypt(text, KEY), KEY) == text
