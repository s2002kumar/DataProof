"""A signed value is accepted in exactly the spelling it was issued in."""

import base64
import string
from datetime import UTC, datetime, timedelta

import pytest

from dataproof_api.sessions import Signer

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)
ALPHABET = string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"


def _other_spellings(text: str) -> list[str]:
    """The same bytes, spelled with a different final character (unused low bits set)."""
    unused_bits = {2: 4, 3: 2}.get(len(text) % 4, 0)
    if not unused_bits:
        return []
    index = ALPHABET.index(text[-1])
    return [
        text[:-1] + ALPHABET[index ^ low]
        for low in range(1, 1 << unused_bits)
        if index ^ low < len(ALPHABET)
    ]


def _decodes(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@pytest.mark.parametrize("login", ["viewer", "a-maintainer", "x" * 7])
def test_a_value_is_accepted_only_as_issued(login: str) -> None:
    signer = Signer(b"k" * 32, "session")
    expires_at = NOW + timedelta(hours=1)
    value = signer.sign({"login": login}, expires_at)
    assert signer.verify(value, NOW) == {"login": login, "exp": int(expires_at.timestamp())}

    payload, signature = value.split(".")
    signatures = _other_spellings(signature)
    assert signatures, "a 32-byte signature leaves unused bits in its last character"
    for other in signatures:
        assert _decodes(other) == _decodes(signature)
        assert signer.verify(f"{payload}.{other}", NOW) is None
    for other in _other_spellings(payload):
        assert _decodes(other) == _decodes(payload)
        assert signer.verify(f"{other}.{signature}", NOW) is None


@pytest.mark.parametrize(
    "value",
    ["", "no-dot", "a.b.c", "é.é", "e30.é", "e30.e30", "e30!.e30", "!!!.???"],
)
def test_a_malformed_value_is_refused_without_raising(value: str) -> None:
    assert Signer(b"k" * 32, "session").verify(value, NOW) is None


def test_a_value_for_another_purpose_or_key_is_refused() -> None:
    value = Signer(b"k" * 32, "session").sign({"login": "viewer"}, NOW + timedelta(hours=1))
    assert Signer(b"k" * 32, "sign-in state").verify(value, NOW) is None
    assert Signer(b"j" * 32, "session").verify(value, NOW) is None
