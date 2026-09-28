from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse


def normalize_proxy(raw: str) -> str | None:
    """把一行代理文本标准化为可提交的字符串。

    支持格式：
      - host:port:user:pass
      - host:port
      - http(s)://user:pass@host:port
      - socks5://user:pass@host:port
      - user:pass@host:port
    返回标准化后的字符串，无法识别返回 None。
    """
    s = (raw or "").strip()
    if not s or s.startswith("#"):
        return None

    # 已带 scheme
    if "://" in s:
        try:
            u = urlparse(s)
            if u.hostname and u.port:
                auth = ""
                if u.username:
                    auth = u.username
                    if u.password:
                        auth += f":{u.password}"
                    auth += "@"
                return f"{u.scheme}://{auth}{u.hostname}:{u.port}"
        except Exception:
            return None
        return None

    # user:pass@host:port
    if "@" in s:
        cred, _, hostpart = s.rpartition("@")
        if ":" in hostpart:
            return f"http://{cred}@{hostpart}"
        return None

    parts = s.split(":")
    # host:port:user:pass
    if len(parts) == 4:
        host, port, user, pwd = parts
        if host and port.isdigit():
            return f"http://{user}:{pwd}@{host}:{port}"
        return None
    # host:port
    if len(parts) == 2:
        host, port = parts
        if host and port.isdigit():
            return f"http://{host}:{port}"
        return None
    return None


_IPV4_RE = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")


def proxy_identity(normalized: str) -> str:
    """提取代理的唯一身份（用于去重/判重）。

    注意：同一网关（host:port）可以用不同用户名/密码区分不同出口 IP
    （如 rotate 代理的 session 标记在密码里），所以有认证信息时必须带上 user:pass。
    """
    try:
        u = urlparse(normalized)
        if u.hostname and u.port:
            if u.username:
                cred = u.username + (":" + u.password if u.password else "")
                return f"{cred}@{u.hostname}:{u.port}"
            return f"{u.hostname}:{u.port}"
    except Exception:
        pass
    m = _IPV4_RE.search(normalized)
    return m.group(1) if m else normalized


def parse_proxy_list(text: str) -> list[str]:
    """解析多行代理文本 → 标准化列表（保序去重）。"""
    seen: set[str] = set()
    out: list[str] = []
    for line in (text or "").splitlines():
        norm = normalize_proxy(line)
        if not norm:
            continue
        ident = proxy_identity(norm)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(norm)
    return out


class ProxyPool:
    """并发安全的代理分配器（轮转重用）。

    规则：
      - 代理列表循环使用（round-robin），永不耗尽
      - 每次取用返回列表中的下一条，走到末尾后从头再来
      - reload() 可重新载入代理列表
    """

    def __init__(self) -> None:
        self._all: list[str] = []
        self._cursor = 0
        self._count = 0
        self._lock = asyncio.Lock()

    def load(self, text: str) -> int:
        """（同步）载入代理列表并重置轮转游标。返回条数。"""
        self._all = parse_proxy_list(text)
        self._cursor = 0
        self._count = 0
        return len(self._all)

    async def acquire(self) -> str | None:
        """取一条代理（轮转）；列表为空返回 None。"""
        async with self._lock:
            if not self._all:
                return None
            idx = self._cursor % len(self._all)
            self._cursor = (idx + 1) % len(self._all)
            self._count += 1
            return self._all[idx]

    async def release(self, proxy: str | None) -> None:
        """轮转模式下无需归还。"""
        return None

    @property
    def total(self) -> int:
        return len(self._all)

    @property
    def used(self) -> int:
        return self._count

    @property
    def remaining(self) -> int:
        # 轮转模式：永不为 0（只要列表非空）
        return len(self._all)

    def stats(self) -> dict[str, int]:
        return {"total": self.total, "used": self.used, "remaining": self.remaining}
