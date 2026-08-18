#!/usr/bin/env python3
"""The EDGAR User-Agent, in one place instead of ten.

SEC requires every automated request to carry a contact identity, so ten modules each
hardcoded the same string containing a personal email. That is a bad shape twice over:
changing the contact meant a grep-and-edit across the engine, and personal data sat in
source that is otherwise publishable. Both problems have the same fix — read it from the
gitignored config directory, where every other credential on this project already lives.

    from edgar_identity import UA
    urllib.request.Request(url, headers=UA)

Resolution order: EDGAR_UA env var -> _engine/config/edgar.json -> hard failure. There is
no silent generic fallback on purpose: an anonymous request gets the box rate-limited or
blocked by SEC, and a fetch pipeline that quietly degrades to "blocked" is exactly the
silent-failure shape this project keeps designing out.
"""
import json
import os

_CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "edgar.json")
_cached = None


def user_agent():
    global _cached
    if _cached:
        return _cached
    ua = os.environ.get("EDGAR_UA")
    if not ua:
        try:
            with open(_CFG) as f:
                ua = json.load(f).get("user_agent", "").strip()
        except FileNotFoundError:
            raise RuntimeError(
                f"EDGAR identity missing: create {_CFG} with "
                '{"user_agent": "Your Name your@email"} or set EDGAR_UA. '
                "SEC blocks unidentified automated requests.")
    if not ua:
        raise RuntimeError(f"EDGAR identity empty in {_CFG} and EDGAR_UA unset")
    _cached = ua
    return ua


class _Headers(dict):
    """Behaves like the {'User-Agent': ...} dict the call sites already pass around, but
    resolves lazily — so importing a module never fails just because config is absent."""

    def __getitem__(self, k):
        if k == "User-Agent":
            return user_agent()
        return super().__getitem__(k)

    def get(self, k, default=None):
        return user_agent() if k == "User-Agent" else super().get(k, default)

    def items(self):
        return (("User-Agent", user_agent()),)

    def keys(self):
        return ("User-Agent",)

    def values(self):
        return (user_agent(),)

    def __iter__(self):
        return iter(("User-Agent",))

    def __len__(self):
        return 1

    def copy(self):
        return {"User-Agent": user_agent()}


UA = _Headers()
