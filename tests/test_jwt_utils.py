"""
Unit tests สำหรับ common/jwt_utils.py — encode/decode/expiry/invalid token
ใช้ร่วมกันโดย Admin/auth/authAdmin.py และ users/auth/authUser.py
"""

from datetime import timedelta

import pytest
from jose import jwt

from common.jwt_utils import ALGORITHM, JWTError, decode_token, encode_token


def test_encode_then_decode_roundtrip():
    token = encode_token({"sub": "a@example.com", "role": "Admin"}, timedelta(minutes=5))
    payload = decode_token(token)

    assert payload["sub"] == "a@example.com"
    assert payload["role"] == "Admin"
    assert "exp" in payload


def test_encode_sets_expiry_claim():
    token = encode_token({"user_id": "u1"}, timedelta(minutes=10))
    payload = decode_token(token)
    assert isinstance(payload["exp"], int)


def test_decode_expired_token_raises_jwt_error():
    # exp ในอดีต -> ต้องถือว่าหมดอายุ
    token = encode_token({"user_id": "u1"}, timedelta(minutes=-5))
    with pytest.raises(JWTError):
        decode_token(token)


def test_decode_invalid_token_raises_jwt_error():
    with pytest.raises(JWTError):
        decode_token("this-is-not-a-valid-jwt")


def test_decode_token_signed_with_wrong_secret_raises_jwt_error():
    bad_token = jwt.encode({"user_id": "u1"}, "a-completely-different-secret", algorithm=ALGORITHM)
    with pytest.raises(JWTError):
        decode_token(bad_token)


def test_decode_tampered_token_raises_jwt_error():
    token = encode_token({"user_id": "u1", "role": "Student"}, timedelta(minutes=5))
    # แก้ไขตัวอักษรกลาง signature segment เพื่อให้ signature ไม่ตรงกับ payload แน่นอน
    header, payload_part, signature = token.split(".")
    mid = len(signature) // 2
    flipped_char = "A" if signature[mid] != "A" else "B"
    tampered_signature = signature[:mid] + flipped_char + signature[mid + 1:]
    tampered = f"{header}.{payload_part}.{tampered_signature}"

    with pytest.raises(JWTError):
        decode_token(tampered)
