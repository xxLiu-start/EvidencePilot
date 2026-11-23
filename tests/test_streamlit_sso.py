from types import SimpleNamespace

from frontend.auth import access_token_from_identity


def test_streamlit_oidc_uses_access_token_not_id_token():
    user = SimpleNamespace(is_logged_in=True, tokens={
        "id": "identity-token-must-never-go-to-api",
        "access": "api-access-token",
    })
    assert access_token_from_identity(user) == "api-access-token"


def test_streamlit_oidc_denies_missing_access_token():
    assert access_token_from_identity(SimpleNamespace(
        is_logged_in=True, tokens={"id": "identity-only"}
    )) is None
    assert access_token_from_identity(SimpleNamespace(
        is_logged_in=False, tokens={"access": "stale-access-token"}
    )) is None
    assert access_token_from_identity(SimpleNamespace(is_logged_in=True, tokens=None)) is None
