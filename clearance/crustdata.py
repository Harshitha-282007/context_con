"""The ONLY file that talks to Crustdata: disk cache, retries, timeouts, credit cap.

Calls never raise. Every failure comes back as an ApiResult with ok=False and a
reason, which the pipeline turns into UNVERIFIABLE.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import requests

BASE_URL = "https://api.crustdata.com"
API_VERSION = "2025-11-01"
RETRYABLE = {429, 500, 502, 503, 504}


ERROR_TEXT = {
    "offline_no_fixture": "no saved response for this lookup; turn off offline replay to run it live",
    "missing_api_key": "no Crustdata API key is configured",
    "credit_cap_reached": "the per-run credit cap was reached",
    "http_401": "the API key was rejected",
    "http_403": "this endpoint is not enabled on the API key",
    "http_404": "no data was found",
    "http_429": "the API rate limit was still exceeded after retries",
}


def describe_error(error: Optional[str]) -> str:
    return ERROR_TEXT.get(error or "", f"the lookup failed ({error})")


@dataclass
class ApiResult:
    ok: bool
    status: int
    data: Any = None
    credits: float = 0.0
    from_cache: bool = False
    error: Optional[str] = None
    endpoint: str = ""
    retrieved_at: str = ""
    fixture_key: Optional[str] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cache_key(method: str, endpoint: str, body: Any) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{method} {endpoint} {canonical}".encode()).hexdigest()[:24]


class CrustdataClient:
    def __init__(
        self,
        api_key: Optional[str],
        fixtures_dir: Path,
        offline: bool = False,
        credit_cap: float = 150.0,
        timeout: float = 20.0,
        max_retries: int = 3,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._api_key = api_key
        self.fixtures_dir = Path(fixtures_dir)
        self.offline = offline
        self.credit_cap = credit_cap
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = session or requests.Session()
        self._sleep = sleep
        self.credits_spent = 0.0     # charged by live calls in this run
        self.credits_replayed = 0.0  # originally charged for responses served from cache
        self.offline_misses = 0      # lookups offline replay could not answer

    def post(self, endpoint: str, body: dict) -> ApiResult:
        return self._call("POST", endpoint, body)

    def get(self, endpoint: str, params: Optional[dict] = None) -> ApiResult:
        return self._call("GET", endpoint, params or {})

    def _call(self, method: str, endpoint: str, body: Any) -> ApiResult:
        key = cache_key(method, endpoint, body)
        cached = self._read_fixture(key)
        # A saved 400 or 403 replays offline so decisions stay deterministic, but online
        # we re-ask, in case the endpoint has since been enabled on this key.
        if cached is not None and (cached.ok or cached.status == 404 or self.offline):
            self.credits_replayed += cached.credits
            return cached
        if self.offline:
            self.offline_misses += 1
            return ApiResult(False, 0, error="offline_no_fixture", endpoint=endpoint, fixture_key=key)
        if not self._api_key:
            return ApiResult(False, 0, error="missing_api_key", endpoint=endpoint, fixture_key=key)
        if self.credits_spent >= self.credit_cap:
            return ApiResult(False, 0, error="credit_cap_reached", endpoint=endpoint, fixture_key=key)
        return self._live(method, endpoint, body, key)

    def _live(self, method: str, endpoint: str, body: Any, key: str) -> ApiResult:
        headers = {
            "authorization": f"Bearer {self._api_key}",
            "x-api-version": API_VERSION,
            "content-type": "application/json",
        }
        url = BASE_URL + endpoint
        last_error, status, resp = "unknown", 0, None
        for attempt in range(self.max_retries + 1):
            try:
                if method == "POST":
                    resp = self._session.post(url, headers=headers, json=body, timeout=self.timeout)
                else:
                    resp = self._session.get(url, headers=headers, params=body, timeout=self.timeout)
                status = resp.status_code
                if status not in RETRYABLE:
                    break
                last_error = f"http_{status}"
            except requests.RequestException as e:
                resp, status = None, 0
                last_error = type(e).__name__  # never the message: it can echo request details
            if attempt < self.max_retries:
                self._sleep(self._retry_delay(resp, attempt))

        if resp is None:
            return ApiResult(False, 0, error=last_error, endpoint=endpoint, fixture_key=key)

        credits = self._credits(resp)
        self.credits_spent += credits
        try:
            data = resp.json()
        except ValueError:
            data = {"unparsed_body": resp.text[:2000]}
        retrieved_at = _now()
        ok = 200 <= status < 300
        # 400, 403 and 404 are stable answers, so they replay offline too. Account calls
        # are never saved: they describe the account, not a vendor.
        if (ok or status in (400, 403, 404)) and not endpoint.startswith("/account/"):
            self._write_fixture(key, method, endpoint, body, status, credits, retrieved_at, data)
        return ApiResult(
            ok, status, data, credits,
            error=None if ok else f"http_{status}",
            endpoint=endpoint, retrieved_at=retrieved_at, fixture_key=key,
        )

    @staticmethod
    def _retry_delay(resp: Any, attempt: int) -> float:
        if resp is not None:
            try:
                return min(float(resp.headers.get("retry-after", "")), 30.0)
            except (TypeError, ValueError):
                pass
        return float(2 ** attempt)

    @staticmethod
    def _credits(resp: Any) -> float:
        try:
            return float(resp.headers.get("x-credits-used", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _fixture_path(self, key: str) -> Path:
        return self.fixtures_dir / f"{key}.json"

    def _read_fixture(self, key: str) -> Optional[ApiResult]:
        path = self._fixture_path(key)
        if not path.exists():
            return None
        try:
            rec = json.loads(path.read_text())
            status = rec["status"]
            ok = 200 <= status < 300
            return ApiResult(
                ok, status, rec["response"], float(rec.get("credits", 0)), True,
                None if ok else f"http_{status}",
                rec["endpoint"], rec.get("retrieved_at", ""), key,
            )
        except (ValueError, KeyError, TypeError):
            return None  # A corrupt fixture behaves like a cache miss.

    def _write_fixture(self, key, method, endpoint, body, status, credits, retrieved_at, data) -> None:
        self.fixtures_dir.mkdir(parents=True, exist_ok=True)
        rec = {
            "method": method, "endpoint": endpoint, "request": body, "status": status,
            "credits": credits, "retrieved_at": retrieved_at, "response": data,
        }
        self._fixture_path(key).write_text(json.dumps(rec, indent=1, sort_keys=True, ensure_ascii=False))
