from codex_hub.security import create_api_token, hash_password, hash_token, verify_password


def test_password_hash_verifies_without_storing_plaintext():
    encoded = hash_password("correct horse battery staple")

    assert "correct horse" not in encoded
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong", encoded)


def test_api_token_is_random_prefixed_and_hashable():
    first = create_api_token()
    second = create_api_token()

    assert first.raw != second.raw
    assert first.raw.startswith("ch_")
    assert first.prefix == first.raw[:12]
    assert hash_token(first.raw) == hash_token(first.raw)
    assert hash_token(first.raw) != hash_token(second.raw)
