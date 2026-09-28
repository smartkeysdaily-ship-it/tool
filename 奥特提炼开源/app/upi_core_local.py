#!/usr/bin/env python3
r"""UPI 支付链接 / QR 生成子系统。

从 ``gen_pp_link.py`` 纯搬迁拆分（零行为变化）。本模块拥有:

* ``generate_upi_qr_link`` -- 完整 7 阶段 UPI 提取流水线
  (checkout → stripe init → 免费试用检测 → 税区更新 → confirm → approve →
  轮询提取 upi:// URI → hydrate → 渲染 QR)
* ``_upi_*`` 系列 -- Stripe init / confirm 响应里的 UPI QR 数据提取助手
* ``_default_qr_path`` / ``_write_qr_png`` -- QR PNG 写出助手
* ``_method_cfg`` / ``_payment_stage_proxies_from_config`` -- 按支付方式的
  配置与阶段代理解析

依赖方向: ``gen_pp_link`` → 本模块; 本模块不得 import ``gen_pp_link``。
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# ─── 内联依赖垫片（从 sms_tool 直接抄取，去外部依赖） ──────────────────────────
# 原实现从 paypal_extract / pp_link_helpers / paypal_proxy 导入这几项，
# 这里全部内联，使本模块自包含（只依赖 requests / qrcode / 标准库）。

import requests  # noqa: E402

CURRENCY_MAP: dict[str, str] = {
    "US": "USD", "GB": "GBP", "DE": "EUR", "FR": "EUR", "JP": "JPY",
    "AU": "AUD", "CA": "CAD", "SG": "SGD", "NZ": "NZD", "IE": "EUR",
    "TH": "THB", "ID": "IDR", "IN": "INR", "BR": "BRL", "KR": "KRW",
    "TR": "TRY",
}

STRIPE_VERSION = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
DEFAULT_TIMEOUT = 30
CHATGPT_TIMEOUT = 45
DEFAULT_STRIPE_PK = (os.environ.get("PP_STRIPE_PUBLISHABLE_KEY", "") or "").strip()

#: checkout 创建阶段 sentinel token 的 flow 名（真实前端抓包确认）。
_SENTINEL_CHECKOUT_FLOW = "chatgpt_checkout"


def _new_session(proxy: str = ""):
    """Create a requests Session for non-checkout stages (Stripe, approve, etc.)."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    })
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def _stage_proxy_value(stage_proxies: dict, key: str, fallback: str = "") -> str:
    return str((stage_proxies or {}).get(key) or fallback or "").strip()


def probe_proxy(proxy: str, timeout: float = 8.0) -> tuple[bool, str, str]:
    """探测代理能否过 Cloudflare（自动试 socks5h 与 http 协议，多指纹）。

    返回 (可用?, 说明, 可用代理URL)。可用URL 带正确协议，供提取复用以保证同协议。
    """
    import concurrent.futures as _cf

    p = str(proxy or "").strip()
    if not p:
        return False, "no_proxy", ""

    body = p
    for scheme in ("http://", "https://", "socks5h://", "socks5://"):
        if body.startswith(scheme):
            body = body[len(scheme):]
            break
    candidates = [f"{s}://{body}" for s in ("socks5h", "socks5", "http")]
    fingerprints = ["chrome124", "chrome131", "chrome133a"]

    def _one(args):
        scheme_proxy, imp = args
        try:
            if _curl_requests is None:
                return False, "no_curl_cffi", scheme_proxy
            sess = _curl_requests.Session(impersonate=imp)
            sess.trust_env = False
            sess.proxies.update({"http": scheme_proxy, "https": scheme_proxy})
            r = sess.get(
                "https://chatgpt.com/",
                headers={"accept": "text/html", "referer": "https://chatgpt.com/"},
                timeout=timeout,
            )
            code = int(getattr(r, "status_code", 0) or 0)
            cf = str(r.headers.get("cf-mitigated") or "")
            try:
                sess.close()
            except Exception:
                pass
            if code == 200 and not cf:
                return True, f"ok({scheme_proxy.split('://')[0]}/{imp})", scheme_proxy
            return False, f"{scheme_proxy.split('://')[0]}:{code}{'/'+cf if cf else ''}", scheme_proxy
        except Exception as exc:
            return False, f"{scheme_proxy.split('://')[0]}:conn", scheme_proxy

    tasks = [(sp, imp) for sp in candidates for imp in fingerprints]
    with _cf.ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        results = list(ex.map(_one, tasks))
    for ok, why, url in results:
        if ok:
            return True, why, url
    first = results[0] if results else (False, "unknown", p)
    return False, first[1], p


def _get_approve_sentinel(proxy: str, access_token: str = "", device_id: str = "", page_url: str = "") -> str:
    """获取 approve 阶段需要的 openai-sentinel-token（flow=checkout_session_approval）。

    与 checkout 相同约束：token 绑定的 device_id 必须与请求头 oai-device-id 一致。
    失败返回空串（不致命）。
    """
    token, _so = _get_approve_sentinel_pair(proxy, access_token, device_id, page_url)
    return token


def _get_approve_sentinel_pair(proxy: str, access_token: str = "", device_id: str = "", page_url: str = "") -> tuple[str, str]:
    """返回 (openai-sentinel-token, openai-sentinel-so-token)。

    实测（真实浏览器抓包）：approve 请求必须同时携带：
      openai-sentinel-token    {"p","t","c","id","flow":"checkout_session_approval"}
      openai-sentinel-so-token {"so":"..."}
    只发前者会得到 {"result":"blocked"}。
    """
    try:
        from app.sentinel_sdk import issue_sentinel_token

        did = device_id or str(uuid.uuid4())
        tok = issue_sentinel_token(
            flow="checkout_session_approval",
            device_id=did,
            proxy=proxy or None,
            page_url=page_url or "",
            timeout_seconds=90,
        )
        return str(getattr(tok, "token", "") or ""), str(getattr(tok, "so_token", "") or "")
    except Exception:
        return "", ""


def _get_checkout_sentinel(proxy: str, device_id: str) -> str:
    """同步获取 checkout 创建所需的 openai-sentinel-token（flow=chatgpt_checkout）。

    实测（真实浏览器抓包）：POST /backend-api/payments/checkout 必须带
    openai-sentinel-token（PoW，绑定 oai-device-id），否则 400 unusual activity。
    复用 app.sentinel 的纯 Python PoW 实现；失败返回空串（不致命）。
    """
    try:
        from app import sentinel as _S

        req_p = _S._generate_requirements_token(_S._SENTINEL_UA)
        body = json.dumps({"p": req_p, "id": device_id, "flow": _SENTINEL_CHECKOUT_FLOW})
        headers = {
            "Accept": "*/*",
            "Referer": _S._SENTINEL_REFERER,
            "Origin": "https://sentinel.openai.com",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "text/plain;charset=UTF-8",
            "User-Agent": _S._SENTINEL_UA,
        }
        proxies = {"http": proxy, "https": proxy} if proxy else None
        if _curl_requests is not None:
            r = _curl_requests.post(
                _S._SENTINEL_REQ_URL, data=body, headers=headers,
                proxies=proxies, impersonate="chrome136", timeout=25,
            )
        else:
            r = requests.post(
                _S._SENTINEL_REQ_URL, data=body, headers=headers,
                proxies=proxies, timeout=25,
            )
        if int(getattr(r, "status_code", 0) or 0) != 200:
            return ""
        try:
            challenge = r.json()
        except Exception:
            return ""
        c_value = str(challenge.get("token") or "").strip()
        pow_info = challenge.get("proofofwork") or {}
        if pow_info.get("required") and pow_info.get("seed"):
            p_value = _S._solve_pow(
                str(pow_info.get("seed")), str(pow_info.get("difficulty") or "0"), _S._SENTINEL_UA
            )
        else:
            p_value = req_p
        return json.dumps(
            {"p": p_value, "t": "", "c": c_value, "id": device_id, "flow": _SENTINEL_CHECKOUT_FLOW}
        )
    except Exception:
        return ""


def _account_device_id(access_token: str) -> str:
    """由 access token 派生稳定设备 ID（同一账号各阶段一致，模拟 oai-did）。"""
    try:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"oai-did:{(access_token or '')[:200]}"))
    except Exception:
        return str(uuid.uuid4())


try:
    from curl_cffi import requests as _curl_requests
except Exception:  # pragma: no cover
    _curl_requests = None


def _random_fingerprint_order() -> list[str]:
    """随机生成一个指纹尝试顺序。

    实测：chrome124 等旧 Chrome 指纹会被 ChatGPT 判 400；而
    firefox / safari / edge 及较新 chrome 均正常。这里把"安全的"指纹
    随机打乱，保证每个账号拿到的顺序不同，避免整批特征一致被识别。
    """
    safe = [
        "firefox135", "firefox144", "firefox147", "firefox133",
        "safari18_0", "safari17_0", "safari15_5", "safari260_ios",
        "edge101", "edge99",
        "chrome146", "chrome145", "chrome143", "chrome141", "chrome139",
        "chrome137", "chrome136", "chrome135", "chrome134", "chrome133a",
        "chrome132", "chrome131", "chrome130", "chrome129", "chrome128",
    ]
    out = list(safe)
    random.shuffle(out)
    return out


def preflight_check(access_token: str, proxy: str = "", timeout: float = 25.0) -> list[tuple[str, str, bool]]:
    """提取前预检：账号验证 / 账号检查 / 促销资格。

    返回 [(步骤名, 结果文本, 是否通过), ...]，供任务时间线展示。
    不通过也不中断（只作为诊断信息），由后续 checkout 决定成败。
    """
    import json as _json

    results: list[tuple[str, str, bool]] = []
    px = {"http": proxy, "https": proxy} if proxy else None
    base = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Referer": "https://chatgpt.com/",
    }
    imp = "chrome146"

    def _get(url: str, params: dict | None = None):
        if _curl_requests is not None:
            return _curl_requests.get(url, params=params, headers=base, proxies=px, impersonate=imp, timeout=timeout)
        return requests.get(url, params=params, headers=base, proxies=px, timeout=timeout)

    # 1) 账号验证 /backend-api/me
    try:
        r = _get("https://chatgpt.com/backend-api/me")
        results.append(("账号验证 /me", str(r.status_code), r.status_code == 200))
    except Exception:
        results.append(("账号验证 /me", "连接失败", False))

    # 2) 账号检查
    try:
        r = _get("https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27")
        results.append(("账号检查", str(r.status_code), r.status_code == 200))
    except Exception:
        results.append(("账号检查", "连接失败", False))

    # 3) 促销资格
    try:
        r = _get(
            "https://chatgpt.com/backend-api/promo_campaign/check_coupon",
            params={"coupon": "plus-1-month-free", "is_coupon_from_query_param": "true"},
        )
        state = ""
        try:
            d = r.json()
            if isinstance(d, dict):
                state = str(d.get("state") or "")
        except Exception:
            state = ""
        results.append(("促销资格", state or str(r.status_code), state == "eligible" or r.status_code == 200))
    except Exception:
        results.append(("促销资格", "连接失败", False))

    return results


def _chatgpt_checkout_post(
    url: str,
    json_body: dict,
    access_token: str,
    *,
    proxy: str = "",
    timeout: float = 45,
    extra_headers: dict | None = None,
    device_id: str = "",
):
    """ChatGPT checkout 创建（带 openai-sentinel-token）。

    实测（真实浏览器抓包对比）：
      仅带最小请求头 POST /backend-api/payments/checkout →
        400 {"detail":"Our systems have detected unusual activity..."}
      前端真实请求额外携带 openai-sentinel-token（PoW，绑定 oai-device-id）
      及一组 oai-* 头 → 200。浏览器内重放验证通过。
    UA 必须与 sentinel PoW 内嵌 UA 一致（chrome136 macOS）。
    """
    from app import sentinel as _S

    did = device_id or _account_device_id(access_token)
    sentinel_token = _get_checkout_sentinel(proxy, did)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": "https://chatgpt.com/",
        "User-Agent": _S._SENTINEL_UA,
        "oai-device-id": did,
        "oai-session-id": str(uuid.uuid4()),
        "oai-language": "en-US",
        "oai-client-version": "prod-71c4ba1079ebac861d68a9bec3ce2e36fd733c1a",
        "oai-client-build-number": "10961682",
        "oai-telemetry": "[1,null]",
        "x-openai-target-path": "/backend-api/payments/checkout",
        "x-openai-target-route": "/backend-api/payments/checkout",
        "x-openai-web-frontend": "core_web",
        "x-oai-is-client-observation": "v1.r.p." + uuid.uuid4().hex[:12],
    }
    if sentinel_token:
        headers["openai-sentinel-token"] = sentinel_token
    if extra_headers:
        headers.update(extra_headers)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    if _curl_requests is not None:
        last = None
        for attempt in range(2):
            try:
                r = _curl_requests.post(
                    url, json=json_body, headers=headers, proxies=proxies,
                    timeout=timeout, impersonate="chrome136",
                )
                code = int(getattr(r, "status_code", 0) or 0)
                if code == 429 and attempt < 1:
                    last = r
                    time.sleep(2.0)
                    continue
                return r
            except Exception:
                continue
        if last is not None:
            return last
    return requests.post(url, json=json_body, headers=headers, proxies=proxies, timeout=timeout)

# ─── 输出 ────────────────────────────────────────────────────────────────────


def _emit(step: str, msg: str, **kw: Any) -> None:
    """Top-level progress/error sink (sunk copy; see ``gen_pp_link._emit``)."""
    print(f"[{step}] {msg}", file=sys.stderr)


# ─── 路径 / 配置装载 (下沉副本, 与 gen_pp_link 同语义) ──────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")


def _load_json(path: str) -> dict:
    """Load a JSON object from disk, accepting UTF-8 files with or without BOM.

    直抄自 sms_tool：原实现对 DEFAULT_CONFIG_PATH 会走 load_merged_config()，
    本项目无该模块；此处改为读文件本身，缺失则返回空 dict（调用方显式传参）。
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ─── UPI 常量 ──────────────────────────────────────────────────────────────────

UPI_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
UPI_CHECKOUT_CONFIRM_URL = "https://chatgpt.com/backend-api/payments/checkout/confirm"
UPI_CHECKOUT_APPROVE_URL = "https://chatgpt.com/backend-api/payments/checkout/approve"
STRIPE_PAYMENT_PAGE_INIT_URL_T = "https://api.stripe.com/v1/payment_pages/{cs_id}/init"
STRIPE_PAYMENT_PAGE_CONFIRM_URL_T = "https://api.stripe.com/v1/payment_pages/{cs_id}/confirm"
STRIPE_PAYMENT_PAGE_GET_URL_T = "https://api.stripe.com/v1/payment_pages/{cs_id}"
UPI_APPROVAL_MAX_ATTEMPTS = 60
UPI_QR_POLL_MAX_ATTEMPTS = 40
UPI_QR_POLL_INTERVAL = 2.5

UPI_BILLING_IN = {
    "name": "Rahul Sharma",
    "email": "upi-scanner@example.com",
    "line1": "Flat 302, Sai Residency",
    "line2": "MG Road, Andheri East",
    "city": "Mumbai",
    "state": "Maharashtra",
    "postal": "400069",
    "country": "IN",
}


def _normalize_hosted_checkout_url(url: str) -> str:
    value = str(url or "").strip()
    if value:
        return value.replace("checkout.stripe.com", "pay.openai.com")
    return value


def _default_qr_path(prefix: str = "upi") -> str:
    directory = Path(PROJECT_ROOT) / "runtime" / "upi_qr"
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory / f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:8]}.png")


def _write_qr_png(data: str, qr_path: str = "") -> str:
    url = str(data or "").strip()
    if not url:
        return ""
    path = Path(qr_path or _default_qr_path("upi"))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import qrcode
    except Exception as exc:  # pragma: no cover - exercised only when dependency missing
        raise RuntimeError("qrcode package is required for UPI QR generation; run pip install qrcode[pil]") from exc
    img = qrcode.make(url)
    img.save(str(path))
    return str(path)


# ─── UPI 辅助函数 ──────────────────────────────────────────────────────────────


def _upi_nested_get(data: Any, path: list[str]) -> Any:
    """安全地按路径取嵌套值."""
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _upi_amount_minor(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(value) if value == value else None  # reject NaN
    if isinstance(value, dict):
        for key in ("amount", "amount_due", "minor", "value"):
            nested = _upi_amount_minor(value.get(key))
            if nested is not None:
                return nested
    return None


def _upi_extract_payment_amount(init_data: Any) -> int:
    return (
        _upi_amount_minor(_upi_nested_get(init_data, ["total_summary", "due"]))
        or _upi_amount_minor(_upi_nested_get(init_data, ["invoice", "amount_due"]))
        or _upi_amount_minor(_upi_nested_get(init_data, ["elements_options", "amount"]))
        or 0
    )


def _upi_get_payment_method_types(init_data: Any) -> list[str]:
    candidates = [
        _upi_nested_get(init_data, ["elements_options", "payment_method_types"]),
        init_data.get("payment_method_types") if isinstance(init_data, dict) else None,
        _upi_nested_get(init_data, ["payment_method_preference", "payment_method_types"]),
        _upi_nested_get(init_data, ["session", "payment_method_types"]),
        init_data.get("ordered_payment_method_types") if isinstance(init_data, dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, list) and candidate:
            return [str(item).lower() for item in candidate]
    return []


def _upi_scan_free_trial(value: Any, depth: int = 0, signals: dict | None = None) -> dict:
    """递归搜索 Stripe init 响应中的免费试用信号."""
    if signals is None:
        signals = {"coupon_name": "", "percent_off": None, "duration_months": None}
    if depth > 8 or not value or not isinstance(value, (dict, list)):
        return signals
    if isinstance(value, list):
        for item in value:
            _upi_scan_free_trial(item, depth + 1, signals)
        return signals
    for key, next_val in value.items():
        lower_key = key.lower()
        if isinstance(next_val, str):
            lower_val = next_val.lower()
            if not signals["coupon_name"] and (
                lower_val.startswith("upi://")
                or "free trial" in lower_val
                or "1 month free" in lower_val
                or "one month free" in lower_val
                or "plus-1-month-free" in lower_val
                or "coupon" in lower_key
                or "promotion" in lower_key
            ):
                signals["coupon_name"] = next_val
        elif isinstance(next_val, (int, float)) and not isinstance(next_val, bool):
            if lower_key in ("percent_off", "percentoff"):
                signals["percent_off"] = max(signals["percent_off"] or 0, next_val)
            if lower_key in ("duration_in_months", "durationmonths"):
                signals["duration_months"] = max(signals["duration_months"] or 0, next_val)
        if next_val and isinstance(next_val, (dict, list)):
            _upi_scan_free_trial(next_val, depth + 1, signals)
    return signals


def _upi_get_free_trial_status(init_data: Any) -> dict:
    """分析 Stripe init 响应判断是否有免费试用."""
    due = _upi_extract_payment_amount(init_data)
    signals = _upi_scan_free_trial(init_data)
    pm_types = _upi_get_payment_method_types(init_data)
    coupon = signals["coupon_name"].strip()
    coupon_lower = coupon.lower()
    looks_like_trial = any(s in coupon_lower for s in ("free trial", "1 month free", "one month free", "plus-1-month-free"))
    looks_like_full_discount = (signals["percent_off"] is not None and signals["percent_off"] >= 100) or looks_like_trial
    return {
        "has_free_trial": due == 0 or (looks_like_full_discount and signals["percent_off"] is not None and signals["percent_off"] >= 100),
        "has_upi": "upi" in pm_types,
        "due": due,
        "coupon_name": coupon,
        "percent_off": signals["percent_off"],
        "duration_months": signals["duration_months"],
        "payment_method_types": pm_types,
    }


def _upi_merge_qr_key(result: dict, key: str, value: Any) -> None:
    """将 UPI QR 数据字段合并到 result dict."""
    if value is None:
        return
    normalized_key = key.lower()
    if isinstance(value, str):
        if value.startswith("upi://") and not result.get("upi_uri"):
            result["upi_uri"] = value
            result["mobile_auth_url"] = value
        elif value.startswith("https://payments.stripe.com/upi/instructions/") and not result.get("hosted_instructions_url"):
            result["hosted_instructions_url"] = value
        elif value.startswith("https://qr.stripe.com/") and "svg" in value.lower() and not result.get("qr_image_url_svg"):
            result["qr_image_url_svg"] = value
        elif value.startswith("https://qr.stripe.com/") and "png" in value.lower() and not result.get("qr_image_url_png"):
            result["qr_image_url_png"] = value
    known_keys = {
        "hosted_instructions_url": "hosted_instructions_url",
        "mobile_auth_url": "mobile_auth_url",
        "upi_uri": "upi_uri",
        "image_url_svg": "qr_image_url_svg",
        "qr_image_url_svg": "qr_image_url_svg",
        "image_url_png": "qr_image_url_png",
        "qr_image_url_png": "qr_image_url_png",
    }
    if normalized_key in known_keys and isinstance(value, str) and value:
        out_key = known_keys[normalized_key]
        result.setdefault(out_key, value)
    if normalized_key in ("expires_at", "expires_after_timestamp", "qr_expires_at"):
        try:
            expires = int(value)
            if expires > 0 and not result.get("expires_at"):
                result["expires_at"] = expires
        except (ValueError, TypeError):
            pass


def _upi_extract_next_action(data: Any) -> dict:
    """递归遍历 Stripe 响应提取 UPI QR 数据."""
    result: dict[str, Any] = {}
    def walk(value: Any, key: str = "") -> None:
        _upi_merge_qr_key(result, key, value)
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        for child_key, child_value in value.items():
            if child_key == "qr_code" and isinstance(child_value, dict):
                _upi_merge_qr_key(result, "qr_expires_at", child_value.get("expires_at"))
                _upi_merge_qr_key(result, "image_url_svg", child_value.get("image_url_svg"))
                _upi_merge_qr_key(result, "image_url_png", child_value.get("image_url_png"))
            walk(child_value, child_key)
    walk(data)
    return result


def _upi_extract_qr_from_html(html: str) -> dict:
    """从 Stripe hosted instructions HTML 页面解析 UPI QR 数据."""
    result: dict[str, Any] = {}
    # 解析 <meta id="payload" data-message="..." />
    meta_match = re.search(r'<meta\b[^>]*\bid=["\']payload["\'][^>]*\bdata-message=["\']([^"\']+)["\']', html, re.I)
    if not meta_match:
        meta_match = re.search(r'<meta\b[^>]*\bdata-message=["\']([^"\']+)["\'][^>]*\bid=["\']payload["\']', html, re.I)
    if meta_match:
        import base64
        raw = meta_match.group(1).replace("&quot;", '"')
        raw = raw.replace("-", "+").replace("_", "/")
        padded = raw + "=" * (4 - len(raw) % 4) if len(raw) % 4 else raw
        try:
            payload = json.loads(base64.b64decode(padded).decode("utf-8"))
            if isinstance(payload, dict):
                _upi_merge_qr_key(result, "mobile_auth_url", payload.get("mobile_auth_url"))
                _upi_merge_qr_key(result, "upi_uri", payload.get("upi_uri"))
                _upi_merge_qr_key(result, "expires_at", payload.get("expires_at") or payload.get("expires_after_timestamp"))
        except Exception:
            pass
    # 解析 <img src="https://qr.stripe.com/..." />
    for img_match in re.finditer(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']', html, re.I):
        src = img_match.group(1).replace("&amp;", "&")
        tag = img_match.group(0)
        if "qr.stripe.com" in src or "QRCode-image" in tag:
            _upi_merge_qr_key(result, "png" if "png" in src.lower() else "svg", src)
            break
    return result


def _upi_hydrate_qr_data(qr_data: dict, proxy_url: str) -> dict:
    """如果 JSON 中没有 upi://，访问 hosted_instructions_url 从 HTML 中解析."""
    result = dict(qr_data)
    hosted_url = result.get("hosted_instructions_url")
    if hosted_url and not result.get("upi_uri"):
        try:
            session = _new_session(proxy_url)
            resp = session.get(hosted_url, timeout=DEFAULT_TIMEOUT, headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://js.stripe.com/",
            })
            if resp.status_code < 400:
                extracted = _upi_extract_qr_from_html(resp.text)
                for k, v in extracted.items():
                    if v and not result.get(k):
                        result[k] = v
        except Exception:
            pass
    return result


def _method_cfg(cfg: dict, payment_method: str) -> dict:
    method = str(payment_method or "").strip().lower().replace("-", "_")
    section = cfg.get(method) if isinstance(cfg.get(method), dict) else {}
    return section if isinstance(section, dict) else {}


def _payment_stage_proxies_from_config(cfg: dict, payment_method: str) -> dict:
    method = str(payment_method or "").strip().lower().replace("-", "_")
    method_cfg = _method_cfg(cfg, method)
    method_stage = method_cfg.get("stage_proxies") if isinstance(method_cfg.get("stage_proxies"), dict) else {}
    paypal_cfg = cfg.get("paypal") if isinstance(cfg.get("paypal"), dict) else {}
    paypal_stage = paypal_cfg.get("stage_proxies") if isinstance(paypal_cfg.get("stage_proxies"), dict) else {}
    proxy_default = (cfg.get("proxy") or {}).get("default") or ""

    def pick(key: str, fallback: str = "") -> str:
        value = _stage_proxy_value(method_stage, key)
        if value:
            return value
        return _stage_proxy_value(paypal_stage, key, fallback)

    checkout = pick("checkout", proxy_default)
    provider = pick("provider") or pick("stripe_init") or proxy_default
    approve = pick("approve") or pick("confirm") or provider or proxy_default
    return {"checkout": checkout, "provider": provider, "approve": approve}


def generate_upi_qr_link(
    access_token: str,
    proxy: Any = None,
    auth_context: dict[str, Any] | None = None,
    checkout_proxy: str | None = None,
    provider_proxy: str | None = None,
    approve_proxy: str | None = None,
    target_country: str | None = None,
    checkout_country: str | None = None,
    payment_country: str | None = None,
    require_zero: bool | None = None,
    qr_path: str | None = None,
    runtime_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a UPI payment link with full Stripe Confirm + Approve flow.

    Implements the complete 7-stage UPI extraction pipeline:
      1. ChatGPT checkout (create cs_id)
      2. Stripe init (get payment page data)
      3. Free trial detection (coupon / discount analysis)
      4. Tax region update (set IN billing address)
      5. Stripe confirm (submit UPI payment method)
      6. ChatGPT approve (trigger payment approval)
      7. Poll payment page → extract upi:// URI → hydrate → render QR

    Returns ``upi://`` deep link + QR PNG path on success, or
    ``stripe_hosted_url`` as fallback if UPI data is not available.
    """
    cfg = dict(runtime_config) if isinstance(runtime_config, Mapping) else _load_json(DEFAULT_CONFIG_PATH)
    upi_cfg = _method_cfg(cfg, "upi")
    stage_proxies = _payment_stage_proxies_from_config(cfg, "upi")
    _checkout = checkout_proxy or proxy or stage_proxies["checkout"]
    _provider = provider_proxy or proxy or stage_proxies["provider"]
    _approve = approve_proxy or proxy or stage_proxies["approve"]
    checkout_proxy = str(_checkout or "").strip()
    provider_proxy = str(_provider or "").strip()
    approve_proxy = str(_approve or "").strip()
    regions = upi_cfg.get("billing_regions") if isinstance(upi_cfg.get("billing_regions"), list) else []
    checkout_country = str(
        checkout_country
        or upi_cfg.get("checkout_country")
        or upi_cfg.get("checkout_billing_country")
        or upi_cfg.get("billing_country")
        or target_country
        or upi_cfg.get("target_country")
        or (regions[0] if regions else "IN")
        or "IN"
    ).upper()
    payment_country = str(
        payment_country
        or upi_cfg.get("payment_country")
        or upi_cfg.get("payment_method_country")
        or "IN"
    ).upper()
    target_country = checkout_country
    currency = CURRENCY_MAP.get(checkout_country, "INR")
    payment_currency = CURRENCY_MAP.get(payment_country, "INR")
    if require_zero is None:
        paypal_cfg = cfg.get("paypal") if isinstance(cfg.get("paypal"), dict) else {}
        require_zero = bool(upi_cfg.get("require_zero_due", paypal_cfg.get("require_zero_due", True)))

    emit = _emit

    try:
        # ── Stage 1: ChatGPT checkout ────────────────────────────────────
        emit("checkout", f"Stage 1: using {checkout_proxy or 'DIRECT'} for UPI checkout")
        cs = _new_session(checkout_proxy)
        cs.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": "https://chatgpt.com/",
        })
        checkout_body = {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": checkout_country, "currency": currency},
            "promo_campaign": {"promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": False},
            "checkout_ui_mode": str(upi_cfg.get("checkout_ui_mode") or "custom"),
        }
        # 关键：ChatGPT 前置 Cloudflare，必须用 curl_cffi 的 TLS 指纹伪装，
        # 否则返回 400 "unusual activity"。见 sms_tool.paypal_extract._checkout_post。
        r = _chatgpt_checkout_post(
            UPI_CHECKOUT_URL,
            checkout_body,
            access_token,
            proxy=checkout_proxy,
            timeout=CHATGPT_TIMEOUT,
        )
        if r.status_code == 401:
            return {"ok": False, "error": "access_token invalid or expired (401)", "error_code": "checkout_unauthorized", "payment_method": "upi"}
        if r.status_code >= 400:
            return {"ok": False, "error": f"checkout failed: {r.status_code} {r.text[:300]}", "error_code": "checkout_failed", "payment_method": "upi"}
        checkout_data = r.json() or {}
        cs_id = checkout_data.get("checkout_session_id") or checkout_data.get("id", "")
        if not str(cs_id).startswith("cs_"):
            return {"ok": False, "error": f"checkout response missing cs_id: {json.dumps(checkout_data, ensure_ascii=False)[:200]}", "error_code": "checkout_bad_response", "payment_method": "upi"}
        stripe_pk = checkout_data.get("publishable_key") or DEFAULT_STRIPE_PK
        processor_entity = checkout_data.get("processor_entity") or ("openai_llc" if checkout_country == "US" else "openai_ie")
        emit("checkout", f"checkout success: cs_id={cs_id}")

        # ── Stage 2: Stripe init (custom mode) ───────────────────────────
        emit("stripe_init", f"Stage 2: using {provider_proxy or 'DIRECT'} for Stripe init")
        stripe = _new_session(provider_proxy)
        stripe_js_id = str(uuid.uuid4())
        init_body = {
            "browser_locale": "en-US",
            "browser_timezone": "Asia/Kolkata",
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[stripe_js_id]": stripe_js_id,
            "elements_session_client[locale]": "en",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "key": stripe_pk,
            "_stripe_version": STRIPE_VERSION,
        }
        init_resp = stripe.post(
            STRIPE_PAYMENT_PAGE_INIT_URL_T.format(cs_id=cs_id),
            data=init_body, timeout=DEFAULT_TIMEOUT,
        )
        if init_resp.status_code >= 400:
            return {"ok": False, "error": f"stripe init failed: {init_resp.status_code} {init_resp.text[:300]}", "error_code": "stripe_init_failed", "payment_method": "upi", "cs_id": cs_id}
        init = init_resp.json() or {}
        emit("stripe_init", f"init success, analyzing free trial...")

        # ── Stage 3: Free trial detection ────────────────────────────────
        ft_status = _upi_get_free_trial_status(init)
        amount = ft_status["due"]
        pm_types = ft_status["payment_method_types"]
        emit("stripe_init", f"free_trial={ft_status['has_free_trial']} due={amount} coupon={ft_status['coupon_name']} upi={ft_status['has_upi']}")
        if require_zero and (not ft_status["has_free_trial"] or amount != 0):
            return {
                "ok": False, "error": f"no_free_trial: due={amount} coupon={ft_status['coupon_name']} percent_off={ft_status['percent_off']}",
                "error_code": "no_free_trial", "payment_method": "upi", "cs_id": cs_id,
                "amount": amount, "currency": payment_currency.upper(),
                "target_country": target_country, "checkout_country": checkout_country,
                "billing_country": checkout_country, "payment_country": payment_country,
                "coupon_name": ft_status["coupon_name"], "percent_off": ft_status["percent_off"],
            }
        if pm_types and not ft_status["has_upi"]:
            return {"ok": False, "error": f"UPI not available for checkout; payment_method_types={pm_types}", "error_code": "upi_not_available", "payment_method": "upi", "cs_id": cs_id, "payment_method_types": pm_types, "amount": amount, "currency": payment_currency.upper(), "target_country": target_country, "checkout_country": checkout_country, "billing_country": checkout_country, "payment_country": payment_country}

        # ── Stage 4: Tax region update ───────────────────────────────────
        emit("tax_region", f"Stage 4: updating tax region to IN")
        tax_body = {
            "tax_region[country]": UPI_BILLING_IN["country"],
            "tax_region[postal_code]": UPI_BILLING_IN["postal"],
            "tax_region[state]": UPI_BILLING_IN["state"],
            "tax_region[city]": UPI_BILLING_IN["city"],
            "tax_region[line1]": UPI_BILLING_IN["line1"],
            "tax_region[line2]": UPI_BILLING_IN["line2"],
            "key": stripe_pk,
            "_stripe_version": STRIPE_VERSION,
        }
        tax_resp = stripe.post(
            STRIPE_PAYMENT_PAGE_GET_URL_T.format(cs_id=cs_id),
            data=tax_body, timeout=DEFAULT_TIMEOUT,
        )
        if tax_resp.status_code >= 400:
            emit("tax_region", f"tax region update failed (non-fatal): {tax_resp.status_code} {tax_resp.text[:200]}")
        else:
            emit("tax_region", "tax region updated")
            # Use tax-updated data for confirm
            init = tax_resp.json() or init

        # ── Stage 5: Stripe confirm (submit UPI payment method) ──────────
        emit("stripe_confirm", f"Stage 5: Stripe confirm with UPI payment method")
        confirm_body = {
            "payment_method_data[type]": "upi",
            "payment_method_data[billing_details][name]": UPI_BILLING_IN["name"],
            "payment_method_data[billing_details][email]": UPI_BILLING_IN["email"],
            "payment_method_data[billing_details][address][line1]": UPI_BILLING_IN["line1"],
            "payment_method_data[billing_details][address][line2]": UPI_BILLING_IN["line2"],
            "payment_method_data[billing_details][address][city]": UPI_BILLING_IN["city"],
            "payment_method_data[billing_details][address][state]": UPI_BILLING_IN["state"],
            "payment_method_data[billing_details][address][postal_code]": UPI_BILLING_IN["postal"],
            "payment_method_data[billing_details][address][country]": UPI_BILLING_IN["country"],
            "expected_amount": str(amount),
            "expected_payment_method_type": "upi",
            "return_url": f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}",
            "client_attribution_metadata[client_session_id]": stripe_js_id,
            "client_attribution_metadata[checkout_session_id]": cs_id,
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "custom",
            "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
            "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "key": stripe_pk,
            "_stripe_version": STRIPE_VERSION,
        }
        init_checksum = init.get("init_checksum") if isinstance(init, dict) else None
        if init_checksum:
            confirm_body["init_checksum"] = str(init_checksum)
        confirm_resp = stripe.post(
            STRIPE_PAYMENT_PAGE_CONFIRM_URL_T.format(cs_id=cs_id),
            data=confirm_body, timeout=DEFAULT_TIMEOUT,
        )
        if confirm_resp.status_code >= 400:
            emit("stripe_confirm", f"confirm failed: {confirm_resp.status_code} {confirm_resp.text[:300]}")
            # Fallback to hosted URL
            hosted_url = _normalize_hosted_checkout_url(str(init.get("stripe_hosted_url") or "")) or f"https://pay.openai.com/c/pay/{cs_id}"
            written_qr_path = _write_qr_png(hosted_url, qr_path or "")
            return {
                "ok": True, "payment_method": "upi", "method": "upi",
                "link_type": "upi_hosted_fallback", "url": hosted_url, "qr_data": hosted_url,
                "qr_path": written_qr_path, "cs_id": cs_id, "processor_entity": processor_entity,
                "amount": amount, "currency": payment_currency.upper(),
                "target_country": target_country, "checkout_country": checkout_country,
                "billing_country": checkout_country, "payment_country": payment_country,
                "payment_method_types": pm_types, "checkout_proxy": checkout_proxy,
                "provider_proxy": provider_proxy, "approve_proxy": approve_proxy,
                "warning": f"stripe_confirm_failed: {confirm_resp.status_code}",
            }
        confirm_data = confirm_resp.json() or {}
        emit("stripe_confirm", "confirm success")

        # ── Stage 6: ChatGPT approve ─────────────────────────────────────
        emit("approve", f"Stage 6: ChatGPT approve using {approve_proxy or 'DIRECT'}")
        approve_device_id = _account_device_id(access_token)
        checkout_page_url = f"https://chatgpt.com/checkout/{processor_entity}/{cs_id}"
        from app import sentinel as _S
        _approve_ua = _S._SENTINEL_UA
        approve_base_headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": checkout_page_url,
            "User-Agent": _approve_ua,
            "oai-device-id": approve_device_id,
            "oai-session-id": str(uuid.uuid4()),
            "oai-language": "en-US",
            "oai-client-version": "prod-71c4ba1079ebac861d68a9bec3ce2e36fd733c1a",
            "oai-client-build-number": "10961682",
            "oai-telemetry": "[1,null]",
            "x-openai-web-frontend": "core_web",
            "x-oai-is-client-observation": "v1.r.p." + uuid.uuid4().hex[:12],
        }
        _approve_proxies = {"http": approve_proxy, "https": approve_proxy} if approve_proxy else None

        def _approve_post(url: str, body: dict, headers: dict):
            if _curl_requests is not None:
                return _curl_requests.post(
                    url, json=body, headers=headers, proxies=_approve_proxies,
                    timeout=CHATGPT_TIMEOUT, impersonate="chrome136",
                )
            return requests.post(url, json=body, headers=headers, proxies=_approve_proxies, timeout=CHATGPT_TIMEOUT)

        approval_ok = False
        approval_data: dict[str, Any] = {}
        # 先尝试 confirm 端点
        try:
            _ch = dict(approve_base_headers)
            _ch["x-openai-target-path"] = "/backend-api/payments/checkout/confirm"
            _ch["x-openai-target-route"] = "/backend-api/payments/checkout/confirm"
            confirm_chatgpt = _approve_post(
                UPI_CHECKOUT_CONFIRM_URL,
                {"checkout_session_id": cs_id, "selected_payment_method_type": "upi"},
                _ch,
            )
            if confirm_chatgpt.status_code < 400:
                confirm_json = confirm_chatgpt.json() or {}
                approval_data = confirm_json
                if str(confirm_json.get("result", "")).lower() == "approved":
                    emit("approve", "approved via confirm endpoint")
                    approval_ok = True
        except Exception:
            approval_data = {}

        # 未通过则走 approve 端点（sentinel 与 checkout 同一 device_id）。
        # 实测：approve 结果存在风控随机性（同一请求有时 approved 有时 blocked），
        # 因此每轮重新求解 sentinel token 再重试，直到通过或轮次耗尽。
        if not approval_ok:
            _approve_cycles = 5
            for cycle in range(1, _approve_cycles + 1):
                sentinel_token = ""
                sentinel_so_token = ""
                try:
                    sentinel_token, sentinel_so_token = _get_approve_sentinel_pair(
                        approve_proxy, access_token, approve_device_id, checkout_page_url
                    )
                    emit("approve", f"cycle {cycle}/{_approve_cycles}: sentinel token {'ok (len=' + str(len(sentinel_token)) + ', so=' + str(len(sentinel_so_token)) + ')' if sentinel_token else '获取失败'}")
                except Exception as ex:
                    emit("approve", f"cycle {cycle}: sentinel 获取异常: {str(ex)[:140]}")
                    continue
                if not sentinel_token:
                    continue
                _ah = dict(approve_base_headers)
                _ah["x-openai-target-path"] = "/backend-api/payments/checkout/approve"
                _ah["x-openai-target-route"] = "/backend-api/payments/checkout/approve"
                _ah["openai-sentinel-token"] = sentinel_token
                if sentinel_so_token:
                    _ah["openai-sentinel-so-token"] = sentinel_so_token
                for attempt in (1, 2):
                    try:
                        approve_resp = _approve_post(
                            UPI_CHECKOUT_APPROVE_URL,
                            {"checkout_session_id": cs_id, "processor_entity": processor_entity},
                            _ah,
                        )
                        if approve_resp.status_code < 400:
                            approve_json = approve_resp.json() or {}
                            result = str(approve_json.get("result", "")).lower()
                            if result == "approved":
                                emit("approve", f"approved (cycle {cycle}, attempt {attempt})")
                                approval_ok = True
                                approval_data = approve_json
                                break
                            emit("approve", f"blocked (cycle {cycle}, attempt {attempt})")
                        else:
                            emit("approve", f"HTTP {approve_resp.status_code} (cycle {cycle}, attempt {attempt})")
                    except Exception as ex:
                        emit("approve", f"cycle {cycle} attempt {attempt} 异常: {str(ex)[:120]}")
                if approval_ok:
                    break

        if not approval_ok:
            emit("approve", "approval failed after all attempts, trying hosted fallback")

        # ── Stage 7: Poll payment page for upi:// URI ────────────────────
        emit("poll", f"Stage 7: polling payment page for UPI QR data")
        qr_data: dict[str, Any] = {}
        # First check confirm/approve responses
        for source in (confirm_data, approval_data):
            extracted = _upi_extract_next_action(source)
            for k, v in extracted.items():
                if v and not qr_data.get(k):
                    qr_data[k] = v

        # Poll Stripe payment page
        for attempt in range(UPI_QR_POLL_MAX_ATTEMPTS):
            if qr_data.get("upi_uri") or qr_data.get("hosted_instructions_url") or qr_data.get("qr_image_url_svg") or qr_data.get("qr_image_url_png"):
                break
            emit("poll", f"poll attempt {attempt + 1}/{UPI_QR_POLL_MAX_ATTEMPTS}")
            if attempt > 0:
                time.sleep(UPI_QR_POLL_INTERVAL)
            try:
                page_resp = stripe.get(
                    STRIPE_PAYMENT_PAGE_GET_URL_T.format(cs_id=cs_id),
                    params={"key": stripe_pk, "_stripe_version": STRIPE_VERSION},
                    timeout=DEFAULT_TIMEOUT,
                )
                if page_resp.status_code == 200:
                    extracted = _upi_extract_next_action(page_resp.json() or {})
                    for k, v in extracted.items():
                        if v and not qr_data.get(k):
                            qr_data[k] = v
                else:
                    if page_resp.status_code >= 400:
                        break
            except Exception:
                pass

        # If still no upi://, try re-init then hydrate
        if not qr_data.get("upi_uri") and not qr_data.get("hosted_instructions_url"):
            emit("poll", "re-init to check for UPI data")
            try:
                refresh_resp = stripe.post(
                    STRIPE_PAYMENT_PAGE_INIT_URL_T.format(cs_id=cs_id),
                    data=init_body, timeout=DEFAULT_TIMEOUT,
                )
                if refresh_resp.status_code == 200:
                    extracted = _upi_extract_next_action(refresh_resp.json() or {})
                    for k, v in extracted.items():
                        if v and not qr_data.get(k):
                            qr_data[k] = v
            except Exception:
                pass

        # Hydrate: fetch hosted_instructions_url HTML if no upi://
        emit("hydrate", "hydrating UPI QR data from hosted instructions")
        qr_data = _upi_hydrate_qr_data(qr_data, provider_proxy)

        upi_uri = qr_data.get("upi_uri") or qr_data.get("mobile_auth_url") or ""
        hosted_instructions_url = str(qr_data.get("hosted_instructions_url") or "")
        hosted_url = _normalize_hosted_checkout_url(str(init.get("stripe_hosted_url") or "")) or f"https://pay.openai.com/c/pay/{cs_id}"
        expires_at = qr_data.get("expires_at") or int(time.time()) + 300

        # 主链接优先级（业务要求）：
        #   1) Stripe UPI instructions 页（https://payments.stripe.com/upi/instructions/...）
        #   2) upi:// 深链
        #   3) 托管收银台链接（兜底）
        if hosted_instructions_url:
            emit("done", f"UPI instructions URL extracted: {hosted_instructions_url[:60]}...")
            link_type = "upi_instructions"
            primary_link = hosted_instructions_url
        elif upi_uri:
            emit("done", f"UPI URI extracted: {upi_uri[:40]}...")
            link_type = "upi_deep_link"
            primary_link = upi_uri
        else:
            emit("done", "no upi:// URI found, falling back to hosted URL")
            link_type = "upi_hosted_fallback"
            primary_link = hosted_url

        written_qr_path = _write_qr_png(primary_link, qr_path or "")
        return {
            "ok": True,
            "payment_method": "upi",
            "method": "upi",
            "link_type": link_type,
            "url": primary_link,
            "upi_uri": upi_uri,
            "hosted_instructions_url": hosted_instructions_url,
            "hosted_url": hosted_url,
            "qr_data": primary_link,
            "qr_path": written_qr_path,
            "expires_at": expires_at,
            "cs_id": cs_id,
            "processor_entity": processor_entity,
            "amount": amount,
            "currency": payment_currency.upper(),
            "target_country": target_country,
            "checkout_country": checkout_country,
            "billing_country": checkout_country,
            "payment_country": payment_country,
            "payment_method_types": pm_types,
            "coupon_name": ft_status["coupon_name"],
            "approval_ok": approval_ok,
            "checkout_proxy": checkout_proxy,
            "provider_proxy": provider_proxy,
            "approve_proxy": approve_proxy,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "upi_qr_failed", "payment_method": "upi", "url": "", "qr_path": ""}
