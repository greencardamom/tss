# -*- coding: utf-8 -*-
#
# tss_wiki.py - MediaWiki API reads for TSS loaders, as a WMF good citizen.
#
# Ported from the bup tool's wiki.py (long-tested), but stdlib-only (urllib) to
# match the TSS loaders' no-dependency rule. Used wherever a TSS loader must read
# from a *main* Wikimedia API (e.g. commons.wikimedia.org), which -- unlike the
# Cloud-VPS services (iabot.wmcloud.org, tools-static.wmflabs.org) -- ENFORCES the
# User-Agent policy and rejects UA-less requests with HTTP 403 (T400119).
#
# Two WMF-specific manners on top of tss_http's plain backoff:
#   * a policy-compliant User-Agent  (https://w.wiki/4wJS). Contact = the public
#     project repo; NO personal info.
#   * `maxlag`: sent on every request and escalated per retry (strict first try,
#     more lag-tolerant later) so we read politely during replication-lag spikes.
# Plus the WMF failure modes tss_http already knows (429/503, Varnish HTML pages,
# empty/truncated JSON) and the API's own error.code=maxlag/ratelimited.
#
# Anonymous reads only (TSS reads public pages occasionally); no OAuth. If a future
# need hits anonymous rate limits, add OAuth the way bup's wiki.py does.

import json
import time
import urllib.error
import urllib.parse
import urllib.request

# WMF User-Agent policy: <client>/<version> (<contact>) <library>/<version>
TOOL = "tss"
VERSION = "1.0"
DEFAULT_CONTACT = "https://github.com/greencardamom/tss"   # contactable; no personal info


def build_user_agent(contact=DEFAULT_CONTACT, tool=TOOL, version=VERSION):
    """A policy-compliant User-Agent string. `contact` must be a way to reach the
    operator (a project URL suffices); keep personal info out of it."""
    return "%s/%s (%s) python-urllib" % (tool, version, contact)


USER_AGENT = build_user_agent()

INITIAL_MAXLAG = 5         # replication-lag tolerance on the first try (seconds)
MAXLAG_STEP = 5            # ... grows by this each retry (linear)
BATCH_RETRIES = 20         # background loaders can afford to wait


class WmfError(Exception):
    """A non-retryable WMF API failure (bad HTTP, or a genuine error.code)."""


def _classify(status, body, n):
    """-> (kind, data, wait, reason). kind: 'ok' | 'fatal' | 'retry'. Mirrors
    tss_http._evaluate, plus the API's JSON error.code=maxlag/ratelimited."""
    if status is None:
        return ("retry", None, 15 + n * 10, "no response")
    if status == 429:
        return ("retry", None, 15 + n * 10, "rate limited")
    if status in (502, 503, 504):
        return ("retry", None, 15 + n * 5, "server busy")
    if status != 200:
        return ("fatal", None, 0, "HTTP %s" % status)
    text = body or ""
    if not text.strip():
        return ("retry", None, 15 + n * 10, "empty response")
    head = text.lstrip()[:14].lower()
    if head.startswith("<!doctype html") or head.startswith("<html"):
        return ("retry", None, 15 + n * 5, "gateway error")
    try:
        data = json.loads(text)
    except ValueError:
        return ("retry", None, 15 + n * 10, "incomplete response")
    err = data.get("error") if isinstance(data, dict) else None
    if err:
        code = err.get("code", "")
        if code in ("maxlag", "ratelimited"):
            return ("retry", None, 15 + n * 10, code)
        return ("fatal", None, 0, "api error: %s" % code)   # genuine API error
    return ("ok", data, 0, "")


def query(params, api_url, user_agent=USER_AGENT, max_retries=BATCH_RETRIES,
          on_retry=None):
    """Issue ONE MediaWiki API query (GET) and return the parsed JSON dict.

    `format=json` is forced; `maxlag` is set per attempt (5, 10, 15, ...). Raises
    WmfError on a non-retryable failure or once the retry budget is exhausted.
    `on_retry(attempt, wait, reason)` (if given) runs just before each backoff.
    """
    p = dict(params)
    p["format"] = "json"
    headers = {"User-Agent": user_agent}

    for attempt in range(max_retries + 1):
        p["maxlag"] = INITIAL_MAXLAG + attempt * MAXLAG_STEP
        url = api_url + "?" + urllib.parse.urlencode(p)
        status = body = None
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as resp:
                status = resp.status
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                body = ""
        except (urllib.error.URLError, TimeoutError, OSError):
            status = None

        kind, data, wait, reason = _classify(status, body, attempt + 1)
        if kind == "ok":
            return data
        if kind == "fatal":
            raise WmfError("%s: %s" % (reason, (body or "")[:200]))
        if attempt >= max_retries:
            raise WmfError("retry budget exhausted (%s)" % reason)
        if on_retry:
            try:
                on_retry(attempt + 1, wait, reason)
            except Exception:
                pass
        time.sleep(wait)


def fetch_page_revisions(page, api_url, rvprop="timestamp|content",
                         rvslots="main", user_agent=USER_AGENT,
                         max_retries=BATCH_RETRIES, on_retry=None):
    """Return a list of revision dicts for `page`, following the `continue`
    protocol. Each dict has the requested rvprop fields; content (if requested)
    is at rev['slots'][rvslots]['*']."""
    params = {
        "action": "query", "prop": "revisions", "titles": page,
        "rvprop": rvprop, "rvslots": rvslots, "rvlimit": "max",
    }
    revs = []
    for _ in range(100):                       # guard against runaway continue
        data = query(params, api_url, user_agent=user_agent,
                     max_retries=max_retries, on_retry=on_retry)
        for _pid, p in data.get("query", {}).get("pages", {}).items():
            revs.extend(p.get("revisions", []))
        cont = data.get("continue")
        if not cont:
            break
        params.update(cont)
    return revs
