"""ChatGPT pure-HTTP login — port from ideal_qr_tool / rust auth/mod.rs."""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Final
from urllib.parse import urljoin, urlparse

import pyotp
from curl_cffi.requests import AsyncSession

from app.models import AccountRecord, SessionEntry
from app.sentinel import get_sentinel_token
from app.session_cache import SessionCache

IMPERSONATE = "chrome136"

class LoginError(Exception):
    def __init__(self, code: str = "", reason: str | None = None, message: str = "") -> None:
        self.code = code or (reason or "login_network_error")
        self.reason = reason or code or "login_network_error"
        super().__init__(message or self.code)

# Map ideal reasons -> our codes
_REASON_MAP = {
    "invalid_credential": "login_credentials_invalid",
    "network_error": "login_network_error",
    "account_deactivated": "login_credentials_invalid",
    "account_locked": "login_credentials_invalid",
    "mfa_required": "login_credentials_invalid",
}


# ---------------------------------------------------------------------------
# Constants — endpoints
# ---------------------------------------------------------------------------

_CHATGPT_BASE: Final[str] = "https://chatgpt.com"
_AUTH_BASE: Final[str] = "https://auth.openai.com"

_URL_AUTH_LOGIN: Final[str] = f"{_CHATGPT_BASE}/auth/login"
_URL_CSRF: Final[str] = f"{_CHATGPT_BASE}/api/auth/csrf"
_URL_SIGNIN_OPENAI: Final[str] = f"{_CHATGPT_BASE}/api/auth/signin/openai"
_URL_SESSION: Final[str] = f"{_CHATGPT_BASE}/api/auth/session"

_URL_AUTHORIZE_CONTINUE: Final[str] = (
    f"{_AUTH_BASE}/api/accounts/authorize/continue"
)
_URL_PASSWORD_VERIFY: Final[str] = f"{_AUTH_BASE}/api/accounts/password/verify"
_URL_MFA_ISSUE: Final[str] = f"{_AUTH_BASE}/api/accounts/mfa/issue_challenge"
_URL_MFA_VERIFY: Final[str] = f"{_AUTH_BASE}/api/accounts/mfa/verify"


#: Regex trích ``challenge_id`` từ ``continue_url`` khi MFA required.
_MFA_CHALLENGE_RE: Final[re.Pattern[str]] = re.compile(
    r"/mfa-challenge/([a-f0-9]+)"
)

#: Số hop tối đa follow-redirect (mirror Rust ``max_hops = 12``).
_MAX_REDIRECT_HOPS: Final[int] = 12

#: Số lần thử consume callback nếu session-token cookie chưa set (Cloudflare
#: chunk cookie đôi khi trễ, retry 3 lần cách 1s).
_CALLBACK_VERIFY_ATTEMPTS: Final[int] = 3

#: Số retry cho prime + CSRF khi gặp 403 (Cloudflare warm-up).
_HTTP_RETRY_ATTEMPTS: Final[int] = 3

#: Cookie name của NextAuth session-token — split ``.0/.1`` khi JWT > 4KB.
_SESSION_TOKEN_COOKIE_BASE: Final[str] = "__Secure-next-auth.session-token"

# ---------------------------------------------------------------------------
# Constants — 4 error reason classes (khớp Requirement 1.4).
# ---------------------------------------------------------------------------

_LOGIN_ERROR_INVALID_CREDENTIAL: Final[str] = "invalid_credential"
#: Account đã bị xoá hoặc deactivated vĩnh viễn — không thể phục hồi
#: bằng đổi password/proxy/retry. Khác `account_locked` (tạm thời) và
#: `invalid_credential` (sai password có thể fix).
#: Detect từ body 403 password/verify: "You do not have an account
#: because it has been deleted or deactivated..." (ChatGPT server 2026).
#: Caller (flow.py) map thành error_code chuyên biệt `account_deactivated`
#: (thay vì generic `login_failed`) để JobManager hook auto-blocklist.
_LOGIN_ERROR_ACCOUNT_DEACTIVATED: Final[str] = "account_deactivated"
#: Keyword match trong response body (lowercase). "deactivated" là chính xác
#: nhất (server luôn dùng chữ này); "does not exist" phòng khi account chưa
#: từng tồn tại. KHÔNG match "deleted" đơn lẻ vì có thể xuất hiện trong
#: context khác (delete session, delete cookie...).
_KEYWORDS_ACCOUNT_DEACTIVATED: Final[tuple[str, ...]] = (
    "deleted or deactivated",
    "has been deactivated",
    "does not have an account",
)
_LOGIN_ERROR_MFA_REQUIRED: Final[str] = "mfa_required"
_LOGIN_ERROR_ACCOUNT_LOCKED: Final[str] = "account_locked"
_LOGIN_ERROR_NETWORK: Final[str] = "network_error"


# ---------------------------------------------------------------------------
# Header helpers — khớp browser Chrome desktop
# ---------------------------------------------------------------------------


def _nav_headers_html(referer: str, fetch_site: str) -> dict[str, str]:
    """Headers cho navigation request (HTML load)."""
    return {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
        "Referer": referer,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": fetch_site,
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


def _common_json_headers(referer: str, origin: str) -> dict[str, str]:
    """Headers cho XHR JSON request."""
    return {
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
        "Referer": referer,
        "Origin": origin,
    }


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _has_session_token(client: Any) -> bool:
    """True nếu cookie jar có ``__Secure-next-auth.session-token`` (base hoặc .0)."""
    for cookie in client.cookies.jar:
        if cookie.name == _SESSION_TOKEN_COOKIE_BASE and cookie.value:
            return True
        if cookie.name == f"{_SESSION_TOKEN_COOKIE_BASE}.0" and cookie.value:
            return True
    return False


def _first_cookie_value(client: Any, name: str) -> str | None:
    """Lấy value của cookie đầu tiên khớp `name` — an toàn với multi-domain.

    ``curl_cffi.requests.Cookies.get(name)`` raise `CookieConflict` khi có nhiều cookie
    cùng tên khác domain/path (ví dụ ``oai-did`` được set ở cả
    ``chatgpt.com`` và ``.openai.com``). Iterate jar để tránh raise.
    """
    for cookie in client.cookies.jar:
        if cookie.name == name and cookie.value:
            return cookie.value
    return None


def _short(s: str, n: int = 200) -> str:
    """Trim string cho log — cắt tại char boundary an toàn."""
    if len(s) <= n:
        return s
    return s[:n] + "…"


#: Whitelist cookies ESSENTIAL cho reuse session ChatGPT (fix HTTP 431 —
#: 2026-07). Chỉ những cookies trực tiếp phục vụ auth mới được cache; các
#: cookies ephemeral (`__cf_bm` TTL 30 phút, `_dd_s` DataDog analytics,
#: `intercom-*`, `ajs_*` Segment/marketing) KHÔNG lưu — chúng sẽ được
#: session mới tự tạo lại khi cần.
#:
#: Bối cảnh bug:
#:     NextAuth JWT `__Secure-next-auth.session-token` khi payload > 4KB
#:     bị chunk thành ``.0/.1/.2/…`` — mỗi chunk ~2KB. Snapshot rộng
#:     (29 cookies) gồm cả CF/DD/analytics → khi restore lại jar và gửi
#:     CSRF ``GET /api/auth/csrf``, tổng header vượt 8KB → Cloudflare
#:     trả **431 Request Header Fields Too Large** (curl_cffi khác httpx
#:     ở chỗ lưu cookies vào ``http.cookiejar.CookieJar`` chuẩn, cookies
#:     domain rỗng được libcurl gửi kèm mọi request → jar bị pollute).
#:
#: Prefix match: cookies bắt đầu bằng bất kỳ prefix nào trong whitelist đều
#: được lưu — cover cả biến thể chunked ``.0/.1/…``.
_ESSENTIAL_COOKIE_PREFIXES: Final[tuple[str, ...]] = (
    # NextAuth JWT session-token (base + chunked variants).
    "__Secure-next-auth.session-token",
    # NextAuth CSRF token — cần cho một số flow refresh.
    "__Host-next-auth.csrf-token",
    "__Secure-next-auth.csrf-token",
    # Device ID chatgpt/openai — server-side rate-limit key.
    "oai-did",
    "oai-sc",
    # Cloudflare challenge cookie (đã pass challenge) — thường ~150 chars,
    # essential để đi qua CF cho request kế tiếp mà không bị challenge lại.
    "cf_clearance",
)


def _is_essential_cookie(name: str) -> bool:
    """True nếu cookie ``name`` thuộc whitelist essential.

    Prefix match để cover session-token chunked variants (``.0/.1/…``).
    """
    return any(name.startswith(prefix) for prefix in _ESSENTIAL_COOKIE_PREFIXES)


def _snapshot_cookies(client: Any) -> dict[str, str]:
    """Snapshot cookies ESSENTIAL từ jar → dict name→value (Requirement 10.4).

    Filter theo 2 layer:
        1. Domain phải thuộc chatgpt.com / openai.com (loại third-party).
        2. Tên cookie phải khớp whitelist ``_ESSENTIAL_COOKIE_PREFIXES``
           — loại ephemeral (`__cf_bm`, `_dd_s`, `intercom-*`, `ajs_*`,
           analytics) để hạn chế header bloat khi revalidate/restore.

    Cache size mục tiêu: 3-5 cookies (session-token chunks + csrf + oai-did
    + cf_clearance) → tổng header cookies < 6KB, an toàn dưới ngưỡng
    Cloudflare 431.
    """
    result: dict[str, str] = {}
    for cookie in client.cookies.jar:
        domain = (cookie.domain or "").lstrip(".")
        if domain and "chatgpt.com" not in domain and "openai.com" not in domain:
            continue
        if not _is_essential_cookie(cookie.name):
            continue
        result[cookie.name] = cookie.value or ""
    return result


# ---------------------------------------------------------------------------
# Step 0-3: prime + CSRF + signin + authorize
# ---------------------------------------------------------------------------


async def _prime(client: Any, logger: logging.Logger) -> None:
    """Step 0: warm Cloudflare cookie (GET `/auth/login`).

    Skip nếu jar đã có ``__cf_bm`` (Cloudflare bot management token).
    Retry tối đa 3 lần nếu 403.
    """
    if _first_cookie_value(client, "__cf_bm"):
        return
    logger.info("[login] [0/9] prime chatgpt.com")
    headers = _nav_headers_html(f"{_CHATGPT_BASE}/", "same-origin")

    for attempt in range(_HTTP_RETRY_ATTEMPTS):
        try:
            response = await client.get(
                _URL_AUTH_LOGIN, headers=headers, allow_redirects=True
            )
        except (
            Exception,
            Exception,
            Exception,
        ) as exc:
            logger.warning("[login] prime transport error: %s", exc)
            raise LoginError(reason=_LOGIN_ERROR_NETWORK) from exc

        if response.status_code == 403 and attempt < _HTTP_RETRY_ATTEMPTS - 1:
            wait = (attempt + 1) * 5.0
            logger.info("[login] prime 403 → retry in %ss", wait)
            await asyncio.sleep(wait)
            continue

        if response.status_code >= 400:
            raise LoginError(reason=_LOGIN_ERROR_NETWORK)
        return


async def _step_csrf(
    client: Any, logger: logging.Logger
) -> str:
    """Step 1: GET `/api/auth/csrf` → csrfToken. Có retry cho 403."""
    await _prime(client, logger)
    logger.info("[login] [1/9] CSRF token")
    headers = _common_json_headers(f"{_CHATGPT_BASE}/auth/login", _CHATGPT_BASE)

    for attempt in range(_HTTP_RETRY_ATTEMPTS):
        try:
            response = await client.get(
                _URL_CSRF, headers=headers, allow_redirects=False
            )
        except (
            Exception,
            Exception,
            Exception,
        ) as exc:
            logger.warning("[login] CSRF transport error: %s", exc)
            raise LoginError(reason=_LOGIN_ERROR_NETWORK) from exc

        if response.status_code == 403 and attempt < _HTTP_RETRY_ATTEMPTS - 1:
            wait = (attempt + 1) * 5.0
            logger.info("[login] CSRF 403 → retry in %ss", wait)
            await asyncio.sleep(wait)
            continue

        if response.status_code != 200:
            logger.warning("[login] CSRF non-2xx status=%s", response.status_code)
            raise LoginError(reason=_LOGIN_ERROR_NETWORK)

        try:
            payload = response.json()
        except ValueError as exc:
            logger.warning("[login] CSRF invalid JSON: %s", exc)
            raise LoginError(reason=_LOGIN_ERROR_NETWORK) from exc

        token = payload.get("csrfToken") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            logger.warning("[login] CSRF missing csrfToken")
            raise LoginError(reason=_LOGIN_ERROR_NETWORK)
        return token

    raise LoginError(reason=_LOGIN_ERROR_NETWORK)


async def _step_auth_url(
    client: Any,
    csrf: str,
    device_id: str,
    login_hint: str,
    logger: logging.Logger,
) -> str:
    """Step 2: POST `/api/auth/signin/openai` → authorize URL."""
    logger.info("[login] [2/9] authorize URL")

    params: list[tuple[str, str]] = [
        ("prompt", "login"),
        ("ext-passkey-client-capabilities", "01001"),
        ("screen_hint", "login_or_signup"),
    ]
    if device_id:
        params.append(("ext-oai-did", device_id))
    if login_hint:
        params.append(("login_hint", login_hint))

    headers = _common_json_headers(f"{_CHATGPT_BASE}/auth/login", _CHATGPT_BASE)
    form = {
        "csrfToken": csrf,
        "callbackUrl": f"{_CHATGPT_BASE}/",
        "json": "true",
    }

    try:
        response = await client.post(
            _URL_SIGNIN_OPENAI,
            params=params,
            data=form,
            headers=headers,
            allow_redirects=False,
        )
    except (Exception, Exception, Exception) as exc:
        logger.warning("[login] signin/openai transport error: %s", exc)
        raise LoginError(reason=_LOGIN_ERROR_NETWORK) from exc

    if response.status_code != 200:
        logger.warning(
            "[login] signin/openai HTTP %s: %s",
            response.status_code,
            _short(response.text, 200),
        )
        raise LoginError(reason=_LOGIN_ERROR_NETWORK)

    try:
        payload = response.json()
    except ValueError as exc:
        raise LoginError(reason=_LOGIN_ERROR_NETWORK) from exc

    auth_url = payload.get("url") if isinstance(payload, dict) else None
    if not isinstance(auth_url, str) or not auth_url:
        logger.warning("[login] signin/openai no url")
        raise LoginError(reason=_LOGIN_ERROR_NETWORK)
    if "auth.openai.com" not in auth_url:
        # CSRF/anti-bot rejected — NextAuth trả URL redirect về signin page
        # thay vì auth provider.
        logger.warning(
            "[login] signin/openai url is not auth.openai.com: %s",
            _short(auth_url, 200),
        )
        raise LoginError(reason=_LOGIN_ERROR_NETWORK)
    return auth_url


async def _get_follow(
    client: Any,
    url: str,
    headers: dict[str, str],
    logger: logging.Logger,
    max_hops: int = _MAX_REDIRECT_HOPS,
) -> tuple[Any, str]:
    """GET với manual follow-redirect. Trả (response cuối, final URL).

    Cần thủ công vì curl_cffi (và httpx) với ``allow_redirects=True`` không expose từng
    hop → không capture được cookies trung gian. Ở đây tự loop, mỗi hop
    ``allow_redirects=False`` để jar tự accumulate Set-Cookie.

    Transport errors (timeout/DNS/TLS/proxy) được convert thành
    ``LoginError(reason=network_error)`` để caller (`_bootstrap` →
    `login_pure_request` → `chatgpt_client.login`) xử lý nhất quán thay vì
    leak raw ``curl_cffi.exceptions.Timeout`` lên tận `_run_handler`.
    """
    current = url
    for _ in range(max_hops):
        try:
            response = await client.get(
                current, headers=headers, allow_redirects=False
            )
        except (
            Exception,
            Exception,
            Exception,
        ) as exc:
            logger.warning(
                "[login] follow-redirect transport error at %s: %s",
                _short(current, 200),
                exc,
            )
            raise LoginError(reason=_LOGIN_ERROR_NETWORK) from exc
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                break
            current = urljoin(current, location)
            continue
        return response, current
    raise LoginError(reason=_LOGIN_ERROR_NETWORK)


async def _bootstrap(
    client: Any,
    email: str,
    use_login_hint: bool,
    logger: logging.Logger,
) -> tuple[str, str]:
    """Bootstrap: CSRF + signin + GET authorize. Trả (device_id, landing_url)."""
    default_did = str(uuid.uuid4())
    csrf = await _step_csrf(client, logger)
    login_hint = email if use_login_hint else ""
    auth_url = await _step_auth_url(client, csrf, default_did, login_hint, logger)

    logger.info("[login] [3/9] OAuth init (GET authorize)")
    headers = _nav_headers_html(f"{_CHATGPT_BASE}/", "cross-site")
    _, landing = await _get_follow(client, auth_url, headers, logger)
    device_id = _first_cookie_value(client, "oai-did") or default_did
    return device_id, landing


def _detect_flow(landing: str) -> str | None:
    """Map landing URL → `"password"` | `"otp"` | ``None``."""
    if "/log-in/password" in landing:
        return "password"
    if "/email-verification" in landing:
        return "otp"
    return None


# ---------------------------------------------------------------------------
# Step 4-5: password/MFA verify
# ---------------------------------------------------------------------------


async def _authorize_continue(
    client: Any,
    email: str,
    sentinel: str,
    device_id: str,
    logger: logging.Logger,
) -> dict[str, Any]:
    """Fallback resolve khi landing URL không xác định — POST authorize/continue.

    Response chứa ``page.type`` (`login_password` | `email_otp_verification`
    | ...) + ``continue_url`` — dùng để rẽ flow.
    """
    headers = _common_json_headers(f"{_AUTH_BASE}/log-in", _AUTH_BASE)
    if sentinel:
        headers["openai-sentinel-token"] = sentinel
    if device_id:
        headers["oai-device-id"] = device_id
    payload = {"username": {"value": email, "kind": "email"}, "screen_hint": "login"}

    try:
        response = await client.post(
            _URL_AUTHORIZE_CONTINUE,
            json=payload,
            headers=headers,
            allow_redirects=False,
        )
    except (Exception, Exception, Exception) as exc:
        raise LoginError(reason=_LOGIN_ERROR_NETWORK) from exc

    if response.status_code != 200:
        logger.warning(
            "[login] authorize/continue HTTP %s: %s",
            response.status_code,
            _short(response.text, 300),
        )
        raise LoginError(reason=_LOGIN_ERROR_NETWORK)

    try:
        return response.json()
    except ValueError as exc:
        raise LoginError(reason=_LOGIN_ERROR_NETWORK) from exc


async def _password_verify(
    client: Any,
    password: str,
    device_id: str,
    sentinel: str,
    logger: logging.Logger,
) -> dict[str, Any]:
    """Step 6: POST `/api/accounts/password/verify` → tiếp continue_url."""
    logger.info("[login] [4/9] password/verify")
    headers = _common_json_headers(f"{_AUTH_BASE}/log-in/password", _AUTH_BASE)
    if device_id:
        headers["oai-device-id"] = device_id
    if sentinel:
        headers["openai-sentinel-token"] = sentinel

    try:
        response = await client.post(
            _URL_PASSWORD_VERIFY,
            json={"password": password},
            headers=headers,
            allow_redirects=False,
        )
    except (Exception, Exception, Exception) as exc:
        raise LoginError(reason=_LOGIN_ERROR_NETWORK) from exc

    if response.status_code == 401 or response.status_code == 403:
        body_lower = response.text.lower()
        # Priority: MFA required trước credential fail (MFA có thể trả 401).
        if any(k in body_lower for k in ("mfa_required", "totp_required", "mfa")):
            raise LoginError(reason=_LOGIN_ERROR_MFA_REQUIRED)
        # Account deactivated/deleted vĩnh viễn — kiểm TRƯỚC `account_locked`
        # vì `deactivated` là terminal (không phục hồi được), cần blocklist
        # để tránh burn thêm proxy/PoW quota cho batch sau. `account_locked`
        # là tạm thời (rate limit/suspicious activity), user có thể retry.
        if any(k in body_lower for k in _KEYWORDS_ACCOUNT_DEACTIVATED):
            logger.warning(
                "[login] password/verify %s (deactivated): %s",
                response.status_code,
                _short(response.text, 200),
            )
            raise LoginError(reason=_LOGIN_ERROR_ACCOUNT_DEACTIVATED)
        if any(
            k in body_lower
            for k in ("account_locked", "account_disabled", "banned", "suspended")
        ):
            raise LoginError(reason=_LOGIN_ERROR_ACCOUNT_LOCKED)
        # 401/403 khác → invalid_credential.
        logger.warning(
            "[login] password/verify %s: %s",
            response.status_code,
            _short(response.text, 200),
        )
        raise LoginError(reason=_LOGIN_ERROR_INVALID_CREDENTIAL)

    if response.status_code != 200:
        logger.warning(
            "[login] password/verify non-2xx status=%s body=%s",
            response.status_code,
            _short(response.text, 300),
        )
        raise LoginError(reason=_LOGIN_ERROR_NETWORK)

    try:
        return response.json()
    except ValueError as exc:
        raise LoginError(reason=_LOGIN_ERROR_NETWORK) from exc


async def _mfa_issue(
    client: Any,
    challenge_id: str,
    device_id: str,
    logger: logging.Logger,
) -> None:
    """Non-fatal: POST `/mfa/issue_challenge` — best-effort.

    Đôi khi server yêu cầu step này trước khi accept verify, nhưng phần lớn
    account đã có TOTP có thể skip. Lỗi log warn, KHÔNG raise.
    """
    headers = _common_json_headers(f"{_AUTH_BASE}/mfa-challenge", _AUTH_BASE)
    if device_id:
        headers["oai-device-id"] = device_id
    payload = {
        "id": challenge_id,
        "type": "totp",
        "force_fresh_challenge": False,
    }
    try:
        response = await client.post(
            _URL_MFA_ISSUE, json=payload, headers=headers, allow_redirects=False
        )
        if response.status_code != 200:
            logger.info(
                "[login] issue_challenge HTTP %s (non-fatal)", response.status_code
            )
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.info("[login] issue_challenge exc %s (non-fatal)", exc)


async def _mfa_verify(
    client: Any,
    challenge_id: str,
    code: str,
    device_id: str,
    logger: logging.Logger,
) -> dict[str, Any]:
    """Step 7: POST `/api/accounts/mfa/verify` với TOTP code."""
    logger.info("[login] [5/9] MFA verify (TOTP)")
    headers = _common_json_headers(f"{_AUTH_BASE}/mfa-challenge", _AUTH_BASE)
    if device_id:
        headers["oai-device-id"] = device_id
    payload = {"id": challenge_id, "type": "totp", "code": code}

    try:
        response = await client.post(
            _URL_MFA_VERIFY, json=payload, headers=headers, allow_redirects=False
        )
    except (Exception, Exception, Exception) as exc:
        raise LoginError(reason=_LOGIN_ERROR_NETWORK) from exc

    if response.status_code != 200:
        logger.warning(
            "[login] MFA verify %s: %s",
            response.status_code,
            _short(response.text, 200),
        )
        # 400/401 khi TOTP code sai → coi là MFA required (secret sai / clock skew).
        if response.status_code in (400, 401, 403):
            raise LoginError(reason=_LOGIN_ERROR_MFA_REQUIRED)
        raise LoginError(reason=_LOGIN_ERROR_NETWORK)

    try:
        return response.json()
    except ValueError as exc:
        raise LoginError(reason=_LOGIN_ERROR_NETWORK) from exc


# ---------------------------------------------------------------------------
# Step 8-9: redirect chain + consume callback
# ---------------------------------------------------------------------------


async def _follow_redirects_to_callback(
    client: Any,
    start_url: str,
    logger: logging.Logger,
) -> str | None:
    """Follow redirect chain đến URL chứa `/callback/openai` + `code=`.

    Trả URL callback (còn full query string chứa ``code``) hoặc ``None``.
    """
    current = start_url
    headers = _nav_headers_html(f"{_CHATGPT_BASE}/", "cross-site")

    for _ in range(_MAX_REDIRECT_HOPS):
        # Đã có sẵn callback trước khi request.
        if "/api/auth/callback/openai" in current and "code=" in current:
            return current
        try:
            response = await client.get(
                current, headers=headers, allow_redirects=False
            )
        except (
            Exception,
            Exception,
            Exception,
        ) as exc:
            logger.warning("[login] follow_redirect transport: %s", exc)
            return None

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                return None
            current = urljoin(current, location)
            if "/api/auth/callback/openai" in current and "code=" in current:
                return current
        else:
            return None
    return None


async def _consume_callback_once(
    client: Any,
    callback_url: str,
    logger: logging.Logger,
) -> bool:
    """Consume 1 lần: follow callback hop-by-hop, capture set-cookie session-token."""
    if "code=" not in callback_url:
        return False
    headers = _nav_headers_html(f"{_AUTH_BASE}/", "cross-site")
    current = callback_url

    for _ in range(_MAX_REDIRECT_HOPS):
        try:
            response = await client.get(
                current, headers=headers, allow_redirects=False
            )
        except (
            Exception,
            Exception,
            Exception,
        ) as exc:
            logger.info("[login] consume_callback transport: %s", exc)
            return _has_session_token(client)

        if _has_session_token(client):
            return True

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                break
            current = urljoin(current, location)
            continue
        break
    return _has_session_token(client)


async def _consume_callback_verified(
    client: Any,
    callback_url: str,
    logger: logging.Logger,
) -> bool:
    """Consume callback với retry — Cloudflare chunk cookie đôi khi trễ."""
    if "code=" not in callback_url:
        return False
    for attempt in range(1, _CALLBACK_VERIFY_ATTEMPTS + 1):
        consumed = await _consume_callback_once(client, callback_url, logger)
        if _has_session_token(client):
            logger.info(
                "[login] [6/9] callback verified (attempt %s/%s, consumed=%s)",
                attempt,
                _CALLBACK_VERIFY_ATTEMPTS,
                consumed,
            )
            return True
        if attempt < _CALLBACK_VERIFY_ATTEMPTS:
            logger.info(
                "[login] callback session cookie not yet set (attempt %s/%s) → retry 1s",
                attempt,
                _CALLBACK_VERIFY_ATTEMPTS,
            )
            await asyncio.sleep(1.0)
    return False


# ---------------------------------------------------------------------------
# Step 10: fetch session JSON
# ---------------------------------------------------------------------------


async def _get_session(
    client: Any, logger: logging.Logger
) -> dict[str, Any]:
    """Step 10: GET `/api/auth/session` → access_token + user info."""
    logger.info("[login] [9/9] GET /api/auth/session")
    headers = _common_json_headers(f"{_CHATGPT_BASE}/", _CHATGPT_BASE)

    try:
        response = await client.get(
            _URL_SESSION, headers=headers, allow_redirects=False
        )
    except (Exception, Exception, Exception) as exc:
        raise LoginError(reason=_LOGIN_ERROR_NETWORK) from exc

    if response.status_code != 200:
        logger.warning(
            "[login] /api/auth/session HTTP %s: %s",
            response.status_code,
            _short(response.text, 200),
        )
        raise LoginError(reason=_LOGIN_ERROR_NETWORK)

    try:
        return response.json()
    except ValueError as exc:
        raise LoginError(reason=_LOGIN_ERROR_NETWORK) from exc


# ---------------------------------------------------------------------------
# Public: login orchestrator
# ---------------------------------------------------------------------------


async def login_pure_request(
    email: str,
    password: str,
    totp_secret: str | None,
    http_client: Any,
    logger: logging.Logger,
) -> SessionEntry:
    """Full login flow → trả ``SessionBundle`` với access_token + cookies snapshot.

    Args:
        email: Account email (đã parse). Bắt buộc.
        password: Password non-empty (định dạng 3-part).
        totp_secret: TOTP base32 secret hoặc ``None`` nếu account không MFA.
        http_client: ``Any`` đã config UA/proxy/headers default.
            Client sẽ được dùng CHUNG cho toàn bộ flow login — cookie jar
            tự accumulate. Client này thường là ``Any`` của
            ``IdealFlowHandler.run()``, và sẽ được dùng tiếp cho checkout/stripe
            sau đó → session cookies được reuse tự nhiên.
        logger: Logger để log tiến trình.

    Returns:
        ``SessionEntry(email, access_token, cookies={dict})`` — cookies chỉ
        chứa domain ``chatgpt.com`` / ``openai.com``.

    Raises:
        LoginError: reason ∈ {invalid_credential, mfa_required, account_locked,
            network_error} theo Requirement 1.4. Caller (`IdealFlowHandler`)
            bắt tại boundary flow.
    """
    logger.info("[login] start — email=%s", email)

    # Fast path: bootstrap WITH login_hint → server thường redirect thẳng
    # /log-in/password (skip authorize/continue).
    device_id, landing = await _bootstrap(http_client, email, True, logger)
    logger.info("[login] landing: %s", _short(landing, 100))
    flow = _detect_flow(landing)

    page_type = ""
    continue_url = ""

    if flow is None:
        # Fallback: retry bootstrap KHÔNG login_hint → server có thể trả
        # landing khác. Cookie jar hiện tại có thể vướng state cũ, nhưng
        # curl_cffi không cho clear jar per-request — cứ tiếp tục với jar chung.
        logger.info("[login] landing not recognized — retrying without login_hint")
        device_id, landing2 = await _bootstrap(http_client, email, False, logger)
        logger.info("[login] retry landing: %s", _short(landing2, 100))
        flow = _detect_flow(landing2)

        if flow is None:
            logger.info("[login] resolving via authorize/continue")
            sentinel = await get_sentinel_token(
                http_client, device_id, "login", logger
            )
            ac_data = await _authorize_continue(
                http_client, email, sentinel, device_id, logger
            )
            page_info = ac_data.get("page") or {}
            page_type = (page_info.get("type") or "").strip()
            continue_url = (ac_data.get("continue_url") or "").strip()

            if page_type == "login_password" or "/log-in/password" in continue_url:
                flow = "password"
            elif page_type in ("email_otp_verification", "email_verification") or (
                "/email-verification" in continue_url
            ):
                flow = "otp"

    if flow is None:
        raise LoginError(reason=_LOGIN_ERROR_NETWORK)

    if flow == "otp":
        # Passwordless flow — cần đọc mailbox OTP. Combo email|pass|totp
        # KHÔNG hỗ trợ.
        logger.warning(
            "[login] account uses passwordless OTP — email|pass|totp combo is not supported"
        )
        raise LoginError(reason=_LOGIN_ERROR_INVALID_CREDENTIAL)

    # --- Password flow ---
    sentinel = await get_sentinel_token(http_client, device_id, "login", logger)
    data = await _password_verify(
        http_client, password, device_id, sentinel, logger
    )
    page_info = data.get("page") or {}
    page_type = (page_info.get("type") or "").strip()
    continue_url = (data.get("continue_url") or "").strip()
    logger.info(
        "[login] post-password: page_type=%s continue=%s",
        page_type,
        _short(continue_url, 80),
    )

    # --- MFA (TOTP) ---
    if "mfa" in page_type or "mfa" in continue_url:
        match = _MFA_CHALLENGE_RE.search(continue_url)
        if not match:
            logger.warning(
                "[login] MFA required but challenge_id not found in continue_url: %s",
                _short(continue_url, 200),
            )
            raise LoginError(reason=_LOGIN_ERROR_MFA_REQUIRED)
        challenge_id = match.group(1)

        if not totp_secret:
            logger.warning("[login] MFA required but totp_secret is missing")
            raise LoginError(reason=_LOGIN_ERROR_MFA_REQUIRED)

        await _mfa_issue(http_client, challenge_id, device_id, logger)

        try:
            code = pyotp.TOTP(totp_secret).now()
        except (TypeError, ValueError) as exc:
            logger.warning("[login] totp_secret invalid base32: %s", exc)
            raise LoginError(reason=_LOGIN_ERROR_MFA_REQUIRED) from exc

        mfa_data = await _mfa_verify(
            http_client, challenge_id, code, device_id, logger
        )
        continue_url = (mfa_data.get("continue_url") or "").strip()

    # Normalize continue_url về absolute.
    if continue_url.startswith("/"):
        continue_url = urljoin(_AUTH_BASE, continue_url)

    # --- Follow redirect chain + consume callback ---
    if (
        continue_url
        and "auth.openai.com" in continue_url
        and "code=" not in continue_url
    ):
        # continue_url vẫn là auth page (ví dụ /log-in/success) → cần
        # reauthorize để lấy callback URL chứa code=.
        logger.info("[login] continue_url is an auth page → reauthorizing")
        csrf2 = await _step_csrf(http_client, logger)
        auth_url2 = await _step_auth_url(http_client, csrf2, "", "", logger)
        cb = await _follow_redirects_to_callback(http_client, auth_url2, logger)
        if cb:
            await _consume_callback_verified(http_client, cb, logger)
        else:
            logger.warning("[login] reauthorize did not find a callback URL")
    elif continue_url:
        cb = await _follow_redirects_to_callback(http_client, continue_url, logger)
        if not cb:
            logger.warning(
                "[login] callback URL not found in redirect chain"
            )
            raise LoginError(reason=_LOGIN_ERROR_NETWORK)
        if not await _consume_callback_verified(http_client, cb, logger):
            logger.warning(
                "[login] callback consumed but session-token cookie NOT set"
            )
            raise LoginError(reason=_LOGIN_ERROR_NETWORK)
    else:
        logger.info("[login] no continue_url → trying reauthorize")
        csrf2 = await _step_csrf(http_client, logger)
        auth_url2 = await _step_auth_url(http_client, csrf2, "", "", logger)
        cb = await _follow_redirects_to_callback(http_client, auth_url2, logger)
        if cb:
            await _consume_callback_verified(http_client, cb, logger)

    if not _has_session_token(http_client):
        logger.warning(
            "[login] flow finished but session-token cookie NOT set — "
            "callback expired / Cloudflare stripped cookie / proxy"
        )
        raise LoginError(reason=_LOGIN_ERROR_NETWORK)

    # --- Fetch session JSON → access_token ---
    session_payload = await _get_session(http_client, logger)
    access_token = _extract_access_token(session_payload)
    if not access_token:
        logger.warning(
            "[login] /api/auth/session has no access_token: %s",
            _short(str(session_payload), 200),
        )
        raise LoginError(reason=_LOGIN_ERROR_NETWORK)

    cookies_snapshot = _snapshot_cookies(http_client)
    now = int(time.time())
    expires_at = _parse_expires(session_payload.get("expires"), default=now + 3600)

    logger.info(
        "[login] ✓ session OK — access_token_len=%d cookies=%d",
        len(access_token),
        len(cookies_snapshot),
    )
    return SessionEntry(
        email=email,
        access_token=access_token,
        cookies=cookies_snapshot,
        expires_at=expires_at,
        created_at=now,
        last_used_at=now,
    )


def _parse_expires(raw: Any, default: int) -> int:
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str) and raw:
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except Exception:
            return default
    return default


def _extract_access_token(payload: dict[str, Any]) -> str | None:
    """Trích accessToken từ payload `/api/auth/session` (camelCase NextAuth)."""
    for key in ("accessToken", "access_token"):
        top = payload.get(key)
        if isinstance(top, str) and top:
            return top
    user = payload.get("user")
    if isinstance(user, dict):
        for key in ("accessToken", "access_token"):
            nested = user.get(key)
            if isinstance(nested, str) and nested:
                return nested
    return None


def _map_login_error(exc: LoginError) -> LoginError:
    reason = getattr(exc, "reason", None) or getattr(exc, "code", "") or str(exc)
    code = _REASON_MAP.get(
        reason, reason if str(reason).startswith("login_") else "login_network_error"
    )
    if "turnstile" in str(exc).lower() or "challenge" in str(reason).lower():
        code = "login_challenge_unsupported"
    return LoginError(code=code, reason=str(reason))


def _detect_turnstile_text(body: str) -> bool:
    b = (body or "").lower()
    return (
        "turnstile" in b
        or "cf-challenge" in b
        or "challenges.cloudflare.com" in b
    )


async def _make_session() -> AsyncSession:
    return AsyncSession(impersonate=IMPERSONATE, timeout=30)


async def login(
    account: AccountRecord,
    *,
    session_cache: SessionCache | None = None,
    logger: logging.Logger | None = None,
) -> SessionEntry:
    """Public login API used by JobManager."""
    log = logger or logging.getLogger("plus_auto")
    client = await _make_session()
    try:
        try:
            entry = await login_pure_request(
                email=account.email,
                password=account.password,
                totp_secret=account.totp_secret,
                http_client=client,
                logger=log,
            )
        except LoginError as e:
            raise _map_login_error(e) from e
        except Exception as e:
            msg = str(e).lower()
            if any(x in msg for x in ("timeout", "curl", "connection", "resolve", "network")):
                raise LoginError(code="login_network_error") from e
            raise LoginError(code="login_network_error") from e

        if not entry.access_token:
            raise LoginError(code="login_credentials_invalid")
        entry.email = account.email
        if session_cache is not None:
            session_cache.write(entry)
        return entry
    finally:
        try:
            await client.close()
        except Exception:
            pass


__all__ = ["login", "login_pure_request", "LoginError", "IMPERSONATE"]
