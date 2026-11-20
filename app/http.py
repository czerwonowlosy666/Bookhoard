"""Shared outbound HTTP session."""
import threading

import requests

from .config import config

_local = threading.local()


def session() -> requests.Session:
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept-Language": "pl,en;q=0.8",
        })
        if config.HTTP_PROXY:
            s.proxies = {"http": config.HTTP_PROXY, "https": config.HTTP_PROXY}
        _local.s = s
    return s


def get(url: str, **kw) -> requests.Response:
    kw.setdefault("timeout", 30)
    r = session().get(url, **kw)
    r.raise_for_status()
    return r
