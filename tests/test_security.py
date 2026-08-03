"""Криптографические примитивы: подпись запросов устройств, токены, шифрование."""

from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag

from core.security import (
    ACTIVATION_ALPHABET,
    SecretBox,
    compute_device_signature,
    decrypt_secret,
    encrypt_secret,
    generate_activation_code,
    generate_token,
    hash_password,
    normalize_activation_code,
    normalize_mac,
    password_needs_rehash,
    token_hash,
    verify_device_signature,
    verify_password,
)


class TestTokens:
    def test_token_hash_is_deterministic(self):
        token = generate_token()
        assert token_hash(token) == token_hash(token)
        assert len(token_hash(token)) == 64

    def test_tokens_are_unique_and_long_enough(self):
        tokens = {generate_token() for _ in range(100)}
        assert len(tokens) == 100
        assert all(len(t) >= 32 for t in tokens)


class TestActivationCodes:
    def test_generated_code_matches_format(self):
        code = generate_activation_code()
        groups = code.split("-")
        assert len(groups) == 3
        assert all(len(group) == 4 for group in groups)
        assert all(ch in ACTIVATION_ALPHABET for ch in code.replace("-", ""))

    def test_normalization_accepts_sloppy_input(self):
        code = generate_activation_code()
        assert normalize_activation_code(code.lower()) == code
        assert normalize_activation_code(code.replace("-", "")) == code
        assert normalize_activation_code(f"  {code.replace('-', ' ')}  ") == code

    @pytest.mark.parametrize("bad", ["", "ABC", "AAAA-BBBB", "AAAA-BBBB-CCCC-DDDD", "AAAA-BBBB-CCC0"])
    def test_invalid_codes_rejected(self, bad):
        assert normalize_activation_code(bad) == ""


class TestMac:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("a0:b1:c2:d3:e4:f5", "A0:B1:C2:D3:E4:F5"),
            ("A0-B1-C2-D3-E4-F5", "A0:B1:C2:D3:E4:F5"),
            ("a0b1c2d3e4f5", "A0:B1:C2:D3:E4:F5"),
            (" a0 b1 c2 d3 e4 f5 ", "A0:B1:C2:D3:E4:F5"),
        ],
    )
    def test_normalization(self, raw, expected):
        assert normalize_mac(raw) == expected

    @pytest.mark.parametrize("bad", ["", "zz:zz:zz:zz:zz:zz", "a0:b1:c2:d3:e4", "a0b1c2d3e4f5aa"])
    def test_invalid_mac_returns_empty(self, bad):
        assert normalize_mac(bad) == ""


class TestDeviceSignature:
    secret = "s3cret-device-key"
    args = ("POST", "/api/v1/device/heartbeat", "1754200000", "nonce-1", b'{"uptime":10}')

    def test_valid_signature_passes(self):
        signature = compute_device_signature(self.secret, *self.args)
        assert verify_device_signature(self.secret, signature, *self.args)

    def test_signature_is_case_insensitive_and_trimmed(self):
        signature = compute_device_signature(self.secret, *self.args)
        assert verify_device_signature(self.secret, f" {signature.upper()} ", *self.args)

    def test_wrong_secret_fails(self):
        signature = compute_device_signature("another-secret", *self.args)
        assert not verify_device_signature(self.secret, signature, *self.args)

    def test_body_tampering_fails(self):
        signature = compute_device_signature(self.secret, *self.args)
        method, path, ts, nonce, _ = self.args
        assert not verify_device_signature(self.secret, signature, method, path, ts, nonce, b'{"uptime":999}')

    def test_replayed_timestamp_produces_different_signature(self):
        method, path, _, nonce, body = self.args
        first = compute_device_signature(self.secret, method, path, "1754200000", nonce, body)
        second = compute_device_signature(self.secret, method, path, "1754200300", nonce, body)
        assert first != second

    def test_path_is_part_of_signature(self):
        signature = compute_device_signature(self.secret, *self.args)
        _, _, ts, nonce, body = self.args
        assert not verify_device_signature(
            self.secret, signature, "POST", "/api/v1/device/activate", ts, nonce, body
        )


class TestEncryption:
    def test_roundtrip(self):
        box = SecretBox(key=os.urandom(32))
        payload = box.encrypt("device-secret-value")
        assert payload != "device-secret-value"
        assert box.decrypt(payload) == "device-secret-value"

    def test_same_plaintext_gives_different_ciphertext(self):
        box = SecretBox(key=os.urandom(32))
        assert box.encrypt("same") != box.encrypt("same")

    def test_wrong_key_fails(self):
        payload = SecretBox(key=os.urandom(32)).encrypt("secret")
        with pytest.raises(InvalidTag):
            SecretBox(key=os.urandom(32)).decrypt(payload)

    def test_aad_must_match(self):
        box = SecretBox(key=os.urandom(32))
        payload = box.encrypt("secret", aad="device:1")
        assert box.decrypt(payload, aad="device:1") == "secret"
        with pytest.raises(InvalidTag):
            box.decrypt(payload, aad="device:2")

    def test_settings_backed_helpers(self):
        payload = encrypt_secret("from-settings")
        assert decrypt_secret(payload) == "from-settings"


class TestPasswords:
    def test_hash_and_verify(self):
        stored = hash_password("Str0ng-Passw0rd!")
        assert stored != "Str0ng-Passw0rd!"
        assert verify_password("Str0ng-Passw0rd!", stored)
        assert not verify_password("wrong", stored)

    def test_garbage_hash_is_rejected(self):
        assert not verify_password("any", "not-a-hash")
        assert password_needs_rehash("not-a-hash")

    def test_fresh_hash_does_not_need_rehash(self):
        assert not password_needs_rehash(hash_password("x"))
