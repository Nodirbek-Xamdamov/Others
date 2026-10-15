"""Ulanishni qayta ishlatadigan HTTPS (keep-alive) — Fleetio so'rovlari uchun.

`urllib.request.urlopen` har chaqiruvda yangi TCP + TLS ulanish ochadi: shu serverdan Fleetio'gacha bu har
so'rovga ~0.3 s qo'shadi (bitta javobda 5-20 ta so'rov bo'ladi). Bu yerda bitta umumiy httpx2.Client
(ulanishlar havzasi, thread-safe) ishlatiladi.

Interfeys `urlopen` bilan bir xil: Request -> `with ... as resp: resp.read()`, HTTP xato -> urllib.error.HTTPError,
tarmoq xatosi -> urllib.error.URLError, timeout -> TimeoutError. Shuning uchun chaqiruvchi kod (xatolarni ajratish,
429 kutish, "javob kelmadi" holati) va testlardagi soxta opener'lar o'zgarmaydi.
"""
from __future__ import annotations

import io
import threading
import urllib.error
import urllib.request

import httpx2 as httpx

# server bo'sh ulanishni ~60 s da yopadi; undan oldin o'zimiz yopamiz — eskirgan ulanishga POST yuborilmasin
KEEPALIVE_S = 25
_LIMITS = httpx.Limits(max_connections=32, max_keepalive_connections=16, keepalive_expiry=KEEPALIVE_S)

_client: httpx.Client | None = None
_client_lock = threading.Lock()


def client() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            # retries=1: faqat ulanib bo'lmaganda (so'rov hali yuborilmagan) — POST uchun ham xavfsiz
            _client = httpx.Client(transport=httpx.HTTPTransport(retries=1, limits=_LIMITS), follow_redirects=False)
        return _client


class Response:
    """`urlopen` javobi o'rniga: read(), status, headers, context manager."""

    def __init__(self, status: int, headers, body: bytes):
        self.status, self.headers, self._body = status, headers, body

    def read(self, *_):
        body, self._body = self._body, b""
        return body

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def urlopen(req: urllib.request.Request, timeout: float | None = None) -> Response:
    method = req.get_method()
    headers = dict(req.header_items())
    for attempt in range(2):
        try:
            resp = client().request(method, req.full_url, headers=headers, content=req.data, timeout=timeout)
            break
        except httpx.TimeoutException as e:
            raise TimeoutError(str(e) or "timed out") from None
        except (httpx.RemoteProtocolError, httpx.ReadError) as e:
            # havzadagi ulanishni server yopib qo'ygan bo'lishi mumkin: GET'ni (o'zgartirmaydi) bir marta qaytaramiz
            if method == "GET" and attempt == 0:
                continue
            raise urllib.error.URLError(e) from None
        except httpx.TransportError as e:
            raise urllib.error.URLError(e) from None
    if resp.status_code >= 400:
        raise urllib.error.HTTPError(req.full_url, resp.status_code, resp.reason_phrase, resp.headers,
                                     io.BytesIO(resp.content))
    return Response(resp.status_code, resp.headers, resp.content)
