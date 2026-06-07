# -*- coding: utf-8 -*-
#
# Hardened HTTP for talking to the TSS API.
#
# Adapted from the long-tested retry loop in the bup tool's wiki.py
# (_api_call / _evaluate). The Toolforge front proxy sheds load much like the
# WMF edge: 429s, 503s, gateway HTML error pages, and empty/truncated bodies.
# Each response is classified ok / fatal / retry; only transient failures are
# retried, with linear, patient waits that grow per attempt and honor
# Retry-After. Genuine 4xx (bad batch, auth, unknown metric) are FATAL -- they
# mean misconfiguration, so we surface them and never hammer.
#
# Two adaptations from the WMF original:
#   * no `maxlag` -- that is a WMF-API parameter; TSS is our own service. The
#     escalating-tolerance spirit lives on in the per-attempt wait ladder.
#   * 4xx (other than 429) is fatal, matching "a genuine error is not retried".
#
# Stdlib only (urllib) so it runs on acre with no dependencies. Shared by the
# backfill loader and the acre->TSS outbox uploader.

import json
import time
import urllib.error
import urllib.request

# Retry budgets (retries after the first try). The loader/uploader are
# batch-style background jobs that can afford to wait, like the bot the
# algorithm came from.
INTERACTIVE_RETRIES = 8
BATCH_RETRIES = 20


class FatalHTTP(Exception):
    """A non-retryable failure (4xx other than 429, or a genuine API error)."""

    def __init__(self, status, body):
        super().__init__("HTTP %s: %s" % (status, (body or "")[:300]))
        self.status = status
        self.body = body


def _retry_after(headers, default=0.0):
    if headers is None:
        return default
    try:
        return float(headers.get("Retry-After", default))
    except (TypeError, ValueError):
        return default


def _evaluate(status, body, n):
    """Classify one response -> (kind, data, wait, reason).

      kind 'ok'    : data = parsed JSON (success)
      kind 'fatal' : non-retryable; caller raises so it degrades, never hammers
      kind 'retry' : back off `wait` seconds and try again

    `n` is the 1-based number of the attempt that just finished (waits scale with
    it). Mirrors bup wiki.py _evaluate: beyond status codes, detects gateway HTML
    pages and empty/truncated bodies -- the failure modes a shared edge produces.
    """
    if status is None:
        return ("retry", None, 15 + n * 10, "no response")       # network/timeout
    if status == 429:
        return ("retry", None, 15 + n * 10, "rate limited")
    if status in (502, 503, 504):
        return ("retry", None, 15 + n * 5, "server busy")
    if status != 200:
        return ("fatal", None, 0, "")                            # 4xx: bad request

    text = body or ""
    if not text.strip():
        return ("retry", None, 15 + n * 10, "empty response")    # gateway drop
    head = text.lstrip()[:14].lower()
    if head.startswith("<!doctype html") or head.startswith("<html"):
        return ("retry", None, 15 + n * 5, "gateway error")      # proxy HTML page
    try:
        data = json.loads(text)
    except ValueError:
        return ("retry", None, 15 + n * 10, "incomplete response")  # truncated
    return ("ok", data, 0, "")


def _send(method, url, headers=None, body=None, timeout=120,
          max_retries=BATCH_RETRIES, on_retry=None):
    """Issue one request with hardened, escalating backoff; return parsed JSON.

    Raises FatalHTTP on a non-retryable error, or RuntimeError when the retry
    budget is exhausted. `on_retry(attempt, wait, reason)` (if given) is called
    just before each backoff sleep; `attempt` is the 1-based number of the try
    that just failed.
    """
    for attempt in range(max_retries + 1):
        status = resp_headers = text = None
        try:
            req = urllib.request.Request(url, data=body, method=method,
                                         headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                resp_headers = resp.headers
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status = e.code
            resp_headers = e.headers
            try:
                text = e.read().decode("utf-8", "replace")
            except Exception:
                text = ""
        except (urllib.error.URLError, TimeoutError, OSError):
            status = None  # network failure -> retry as "no response"

        kind, data, wait, reason = _evaluate(status, text, attempt + 1)
        if kind == "ok":
            return data
        if kind == "fatal":
            raise FatalHTTP(status, text or "")
        if attempt >= max_retries:
            raise RuntimeError("retry budget exhausted (%s)" % reason)

        wait = max(wait, _retry_after(resp_headers, 0.0))
        if on_retry:
            try:
                on_retry(attempt + 1, wait, reason)
            except Exception:
                pass  # progress reporting must never break the request
        time.sleep(wait)
    raise RuntimeError("unreachable")


def post_json(url, token, payload, timeout=120, max_retries=BATCH_RETRIES,
              on_retry=None):
    """POST `payload` as JSON to `url` with hardened, escalating backoff."""
    headers = {
        "Authorization": "Bearer %s" % token,
        "Content-Type": "application/json",
    }
    body = json.dumps(payload).encode("utf-8")
    return _send("POST", url, headers=headers, body=body, timeout=timeout,
                 max_retries=max_retries, on_retry=on_retry)


def get_json(url, headers=None, timeout=120, max_retries=BATCH_RETRIES,
             on_retry=None):
    """GET `url` (anonymous by default) and return parsed JSON, same backoff."""
    return _send("GET", url, headers=headers, body=None, timeout=timeout,
                 max_retries=max_retries, on_retry=on_retry)

    raise RuntimeError("unreachable")
