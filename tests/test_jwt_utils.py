"""
Unit tests สำหรับ common/jwt_utils.py — encode/decode/expiry/invalid token
ใช้ร่วมกันโดย Admin/auth/authAdmin.py และ users/auth/authUser.py

ไลบรารี JWT คือ PyJWT (เดิมใช้ python-jose ซึ่งลาก `ecdsa` ที่มีช่องโหว่ CVE-2024-23342 ที่ไม่มีแผนแก้ตามมา
แม้โปรเจ็คใช้แค่ HS256 ก็ตาม) เทสต์ส่วน "เข้ากันได้กับ token เดิม" ยืนยันว่า token ที่ผู้ใช้ถือค้างอยู่
(ออกโดย python-jose) ยังตรวจผ่าน จึงไม่มีใครถูกเด้งออกจากระบบตอนสลับไลบรารี
"""

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import timedelta

import jwt
import pytest

from common.jwt_utils import ALGORITHM, JWTError, decode_token, encode_token


def _secret() -> str:
    return os.environ["JWT_SECRET_KEY"]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _handmade_hs256(claims: dict, secret: str | None = None) -> str:
    """ประกอบ JWT HS256 ตามมาตรฐานด้วยมือ — จำลอง token ที่ไลบรารีอื่น (เช่น python-jose เดิม) เคยออกให้"""
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    sig = hmac.new((secret or _secret()).encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64(sig)}"


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


def test_encode_returns_str_token():
    assert isinstance(encode_token({"user_id": "u1"}, timedelta(minutes=1)), str)


def test_decode_expired_token_raises_jwt_error():
    # exp ในอดีต -> ต้องถือว่าหมดอายุ
    token = encode_token({"user_id": "u1"}, timedelta(minutes=-5))
    with pytest.raises(JWTError):
        decode_token(token)


def test_decode_invalid_token_raises_jwt_error():
    with pytest.raises(JWTError):
        decode_token("this-is-not-a-valid-jwt")


@pytest.mark.parametrize("bad", ["", "a.b", "a.b.c", "....", " "])
def test_decode_malformed_strings_raise_jwt_error(bad):
    with pytest.raises(JWTError):
        decode_token(bad)


def test_decode_token_signed_with_wrong_secret_raises_jwt_error():
    bad_token = jwt.encode({"user_id": "u1"}, "a-completely-different-secret" * 3, algorithm=ALGORITHM)
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


def test_decode_rejects_claims_swapped_under_valid_signature():
    """เปลี่ยน payload (เช่น role) แต่ใช้ signature เดิม ต้องถูกปฏิเสธ"""
    token = encode_token({"user_id": "u1", "role": "Student"}, timedelta(minutes=5))
    header, _, signature = token.split(".")
    forged_payload = _b64(json.dumps({"user_id": "u1", "role": "Admin", "exp": int(time.time()) + 3600}).encode())
    with pytest.raises(JWTError):
        decode_token(f"{header}.{forged_payload}.{signature}")


# ── การโจมตีระดับอัลกอริทึม ──────────────────────────────────────────────────

def test_decode_rejects_alg_none_token():
    header = _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    body = _b64(json.dumps({"user_id": "u1", "exp": int(time.time()) + 3600}).encode())
    with pytest.raises(JWTError):
        decode_token(f"{header}.{body}.")


def test_decode_rejects_other_hmac_algorithms_even_with_correct_secret():
    hs512 = jwt.encode({"user_id": "u1"}, _secret(), algorithm="HS512")
    with pytest.raises(JWTError):
        decode_token(hs512)


# ── เข้ากันได้กับ token เดิมที่ออกโดย python-jose ────────────────────────────

def test_tokens_from_other_hs256_implementations_still_verify():
    """cookie/token ที่ผู้ใช้ถือค้างไว้ก่อนสลับไลบรารีต้องใช้ต่อได้ (ไม่ถูกเด้งออก)"""
    claims = {"user_id": "U123", "exp": int(time.time()) + 3600}
    assert decode_token(_handmade_hs256(claims)) == claims


def test_admin_style_token_with_float_iat_and_sub_verifies():
    """token แอดมินมี sub (สตริง) และ iat แบบทศนิยม — ต้องถอดได้และค่าไม่เพี้ยน"""
    iat = time.time()
    token = encode_token({"sub": "admin@example.com", "user_id": "abc", "role": "Admin", "FullName": "x", "iat": iat},
                         timedelta(hours=1))
    payload = decode_token(token)
    assert payload["iat"] == pytest.approx(iat)
    assert payload["sub"] == "admin@example.com"


def test_tokens_minted_here_are_standard_hs256_verifiable_by_hand():
    """ทิศกลับกัน: token ที่ระบบออกต้องเป็น HS256 มาตรฐาน (ไลบรารีใดก็ตรวจได้)"""
    token = encode_token({"user_id": "u1"}, timedelta(minutes=5))
    header, body, sig = token.split(".")
    expected = _b64(hmac.new(_secret().encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
    assert sig == expected
    assert json.loads(base64.urlsafe_b64decode(header + "==")) ["alg"] == "HS256"


def test_registration_token_shape_verifies():
    token = encode_token({"user_id": "line-uid", "registration": True}, timedelta(minutes=15))
    payload = decode_token(token)
    assert payload["registration"] is True


def test_token_with_iat_slightly_in_future_is_accepted():
    """python-jose เดิมไม่ตรวจ iat: คงพฤติกรรมเดิมไว้เพื่อกัน instance ที่นาฬิกาเร็วกว่าอีกเครื่องปฏิเสธ token ของกันและกัน"""
    token = encode_token({"user_id": "u1", "iat": time.time() + 120}, timedelta(hours=1))
    assert decode_token(token)["user_id"] == "u1"


def test_jwt_error_is_the_pyjwt_base_error():
    """โค้ดทั้งระบบ catch JWTError จาก common.jwt_utils — ต้องครอบคลุมข้อผิดพลาดทุกชนิดของ PyJWT"""
    assert JWTError is jwt.PyJWTError
