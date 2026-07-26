from app.credentials import credentials_from_scope


def _scope(headers: list[tuple[bytes, bytes]]):
    return {"type": "http", "headers": headers}


def test_credentials_are_read_from_request_headers():
    credentials, missing = credentials_from_scope(
        _scope(
            [
                (b"x-viking-email", b"user@example.com"),
                (b"x-viking-api-key", b"secret"),
                (b"x-viking-role", b"Trader"),
            ]
        )
    )
    assert missing == []
    assert credentials is not None
    assert credentials.email == "user@example.com"
    assert credentials.api_key == "secret"
    assert credentials.role == "trader"


def test_missing_credentials_are_reported_without_values():
    credentials, missing = credentials_from_scope(
        _scope([(b"x-viking-email", b"user@example.com")])
    )
    assert credentials is None
    assert missing == ["X-Viking-API-Key", "X-Viking-Role"]
