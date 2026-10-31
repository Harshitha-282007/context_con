"""Crustdata client failure paths with a fake HTTP session. No network."""
import requests

from clearance.crustdata import CrustdataClient

KEY = "secret-test-key"


class FakeResp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else []
        self.headers = headers or {}
        self.text = "not json"

    def json(self):
        if self._body == "BAD":
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, headers, json, timeout):
        self.calls.append(headers)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(tmp_path, *responses, **kw):
    return CrustdataClient(KEY, tmp_path, session=FakeSession(*responses), sleep=lambda s: None, **kw)


def test_sends_required_headers_and_caches(tmp_path):
    c = client(tmp_path, FakeResp(200, [{"ok": 1}], {"x-credits-used": "2"}))
    first = c.post("/company/enrich", {"domains": ["a.com"]})
    second = c.post("/company/enrich", {"domains": ["a.com"]})
    assert first.ok and first.credits == 2 and second.from_cache
    assert len(c._session.calls) == 1
    h = c._session.calls[0]
    assert h["authorization"] == f"Bearer {KEY}" and h["x-api-version"] == "2025-11-01"
    assert c.credits_spent == 2 and c.credits_replayed == 2


def test_key_never_written_to_fixture(tmp_path):
    client(tmp_path, FakeResp(200, [1])).post("/x", {"a": 1})
    assert all(KEY not in p.read_text() for p in tmp_path.glob("*.json"))


def test_retries_then_succeeds(tmp_path):
    c = client(tmp_path, FakeResp(429), FakeResp(503), FakeResp(200, [1]))
    assert c.post("/x", {}).ok


def test_exhausted_retries_degrade_without_raising(tmp_path):
    c = client(tmp_path, *[FakeResp(500) for _ in range(4)])
    r = c.post("/x", {})
    assert not r.ok and r.error == "http_500"


def test_timeouts_degrade_without_raising(tmp_path):
    c = client(tmp_path, *[requests.Timeout("boom") for _ in range(4)])
    r = c.post("/x", {})
    assert not r.ok and r.error == "Timeout"


def test_forbidden_not_retried_or_cached(tmp_path):
    c = client(tmp_path, FakeResp(403, {"error": {}}))
    r = c.post("/x", {})
    assert r.status == 403 and not r.ok and not list(tmp_path.glob("*.json"))


def test_unparseable_body_does_not_crash(tmp_path):
    r = client(tmp_path, FakeResp(200, "BAD")).post("/x", {})
    assert r.ok and "unparsed_body" in r.data


def test_credit_cap_stops_calls(tmp_path):
    c = client(tmp_path, FakeResp(200, [1], {"x-credits-used": "5"}), credit_cap=5)
    assert c.post("/x", {"n": 1}).ok
    r = c.post("/x", {"n": 2})
    assert not r.ok and r.error == "credit_cap_reached"


def test_offline_miss_and_replay(tmp_path):
    client(tmp_path, FakeResp(200, [7])).post("/x", {"n": 1})
    off = CrustdataClient(None, tmp_path, offline=True)
    assert off.post("/x", {"n": 1}).data == [7]
    miss = off.post("/x", {"n": 2})
    assert not miss.ok and miss.error == "offline_no_fixture"


def test_missing_key_degrades(tmp_path):
    r = CrustdataClient(None, tmp_path).post("/x", {})
    assert not r.ok and r.error == "missing_api_key"
