from fastapi.testclient import TestClient

from app.main import app


def _client(monkeypatch) -> TestClient:
    import app.services.activity as activity

    monkeypatch.setattr(activity, "record_page_view", lambda *args, **kwargs: None)
    return TestClient(app)


def test_public_dead_link_gets_resolve(monkeypatch):
    client = _client(monkeypatch)
    paths = [
        "/search",
        "/search?q=Bible",
        "/search/suggest?q=Bible",
        "/cart",
        "/compare",
        "/branches",
        "/orders/track",
        "/auth/verify?token=bad",
        "/auth/forgot-password",
        "/auth/reset-password?token=bad",
    ]

    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, path


def test_account_dead_link_gets_redirect_to_login(monkeypatch):
    client = _client(monkeypatch)
    paths = [
        "/account",
        "/account/orders",
        "/account/wishlist",
        "/account/newsletter",
        "/account/returns",
        "/account/returns/new/JEC-000000-000",
    ]

    for path in paths:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/auth/login")


def test_dead_link_posts_are_registered(monkeypatch):
    client = _client(monkeypatch)
    form_posts = [
        "/cart/add",
        "/compare/toggle",
        "/account/wishlist/toggle",
        "/newsletter/subscribe",
    ]

    for path in form_posts:
        response = client.post(path)
        assert response.status_code == 422, path

    # Return posts take no declared form fields, so an anonymous caller reaches
    # the auth check rather than a 422 — they must be bounced to login, never
    # allowed to touch somebody's return.
    for path in [
        "/account/returns/new/JEC-000000-000",
        "/account/returns/RET-000000-001/cancel",
    ]:
        response = client.post(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/auth/login")

    response = client.post("/account/impersonation/end", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/account"
