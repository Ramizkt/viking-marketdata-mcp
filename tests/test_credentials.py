from app.credentials import VikingCredentials


def test_credentials_fingerprint_is_stable_and_secret():
    credentials = VikingCredentials(
        email="user@example.com",
        api_key="secret",
        role="trader",
    )
    assert credentials.fingerprint == credentials.fingerprint
    assert "secret" not in credentials.fingerprint
