import httpx
import pytest

from milemap.config import FuelFinderSettings
from milemap.sources.fuel_finder.client import FuelFinderClient, PaginationResult, split_records
from milemap.sources.fuel_finder.errors import (
    ApiError,
    AuthError,
    DeadlineExceeded,
    GeoBlockedError,
    RateLimitedError,
)

S = FuelFinderSettings(client_id="cid-123456", client_secret="sec-abcdef",  # noqa: S106 (fake test value)
                       base_url="https://ff.test")


def make_client(handler, **kw):
    calls = {"token": 0, "get": 0}

    def wrapped(request: httpx.Request):
        if request.url.path.endswith("generate_access_token"):
            calls["token"] += 1
        else:
            calls["get"] += 1
        return handler(request)

    http = httpx.Client(transport=httpx.MockTransport(wrapped))
    c = FuelFinderClient(S, http=http, sleep=lambda s: None, min_interval_s=0, **kw)
    return c, calls


def token_ok(request):
    return httpx.Response(200, json={"success": True, "data": {
        "access_token": "tok-zzzzzz", "refresh_token": "ref-yyyyyy", "expires_in": 3600}})


def pages(n_full, last=0, end="empty"):
    def handler(request):
        if request.url.path.endswith("generate_access_token"):
            return token_ok(request)
        assert request.headers["Authorization"] == "Bearer tok-zzzzzz"
        b = int(request.url.params["batch-number"])
        if b <= n_full:
            return httpx.Response(200, json=[{"node_id": f"n{b}-{i}"} for i in range(500)])
        if b == n_full + 1 and last:
            return httpx.Response(200, json=[{"node_id": "x"}] * last)
        if end == "404":
            return httpx.Response(404, json={"message": "Invalid batch"})
        return httpx.Response(200, json=[])
    return handler


def test_paginates_until_empty_and_reuses_token():
    c, calls = make_client(pages(3, last=7))
    res = PaginationResult()
    batches = list(c.iter_batches("/api/v1/pfs/fuel-prices", {"x": 1}, result=res))
    assert [len(b.records) for b in batches] == [500, 500, 500, 7, 0]
    assert res.stopped_because == "empty"
    assert calls["token"] == 1  # one token for every request


def test_stops_on_404():
    c, _ = make_client(pages(2, end="404"))
    res = PaginationResult()
    assert len(list(c.iter_batches("/p", result=res))) == 2
    assert res.stopped_because == "404"


def test_cap():
    c, _ = make_client(pages(10))
    res = PaginationResult()
    assert len(list(c.iter_batches("/p", max_batches=3, result=res))) == 3
    assert res.stopped_because == "cap"


def test_403_is_geoblocked():
    def handler(request):
        if request.url.path.endswith("generate_access_token"):
            return token_ok(request)
        return httpx.Response(403)
    c, _ = make_client(handler)
    with pytest.raises(GeoBlockedError):
        list(c.iter_batches("/p"))


def test_retries_5xx_then_succeeds():
    state = {"n": 0}

    def handler(request):
        if request.url.path.endswith("generate_access_token"):
            return token_ok(request)
        state["n"] += 1
        if state["n"] <= 2:
            return httpx.Response(503)
        return httpx.Response(200, json=[])
    c, _ = make_client(handler)
    assert c.get("/p").status == 200
    assert c.retry_count == 2


def test_gives_up_after_max_retries():
    def handler(request):
        if request.url.path.endswith("generate_access_token"):
            return token_ok(request)
        return httpx.Response(500)
    c, _ = make_client(handler, max_retries=2)
    with pytest.raises(ApiError):
        c.get("/p")


def test_auth_error_redacts_secrets():
    def handler(request):
        return httpx.Response(400, text="bad client cid-123456 / sec-abcdef")
    c, _ = make_client(handler)
    with pytest.raises(AuthError) as e:
        c.tokens.token()
    assert "cid-123456" not in str(e.value) and "sec-abcdef" not in str(e.value)


def test_split_records_shapes():
    assert split_records([{"a": 1}]) == ([{"a": 1}], {})
    assert split_records({"success": True, "data": [1]}) == ([1], {"success": True})
    assert split_records({"weird": 1}) == ([], {"weird": 1})


def test_429_stops_immediately_without_retrying():
    def handler(request):
        if request.url.path.endswith("generate_access_token"):
            return token_ok(request)
        return httpx.Response(429, headers={"Retry-After": "120"})
    c, calls = make_client(handler)
    with pytest.raises(RateLimitedError) as e:
        list(c.iter_batches("/p"))
    assert calls["get"] == 1 and e.value.retry_after_s == 120


def test_deadline_stops_before_backoff_would_overrun():
    now = {"t": 0.0}

    def handler(request):
        if request.url.path.endswith("generate_access_token"):
            return token_ok(request)
        return httpx.Response(503)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    c = FuelFinderClient(S, http=http, sleep=lambda s: now.__setitem__("t", now["t"] + s),
                         clock=lambda: now["t"], min_interval_s=0, deadline=5.0)
    with pytest.raises(DeadlineExceeded):
        c.get("/p")


def test_token_cache_is_private_and_reused(tmp_path):
    cache = tmp_path / "token.json"
    c1, calls1 = make_client(pages(0), token_cache=cache)
    c1.get("/p", {"batch-number": 1})
    assert calls1["token"] == 1
    assert oct(cache.stat().st_mode & 0o777) == "0o600"
    assert "sec-abcdef" not in cache.read_text()  # client secret never cached
    c2, calls2 = make_client(pages(0), token_cache=cache)
    c2.get("/p", {"batch-number": 1})
    assert calls2["token"] == 0  # second run reuses the cached, still-valid token
