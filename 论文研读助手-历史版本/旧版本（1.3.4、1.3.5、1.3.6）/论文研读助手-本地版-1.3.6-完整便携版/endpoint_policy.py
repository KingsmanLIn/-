"""Endpoint policy: which API targets this software is willing to talk to.

Two modes, and the mode is part of the stored profile:

* ``local_loopback`` — a model server on this machine. HTTP is allowed, but only to a
  literal loopback address with an explicit port. No hostnames, so a name that resolves
  somewhere else cannot pretend to be local.
* ``cloud_https`` — a remote provider. HTTPS only; the resolved addresses must all be
  public; loopback, private, link-local, carrier-grade NAT, metadata and reserved ranges
  are refused, including IPv4-mapped IPv6 forms.

The policy is a pure function over an explicit target. Nothing here reads the environment,
which keeps proxy and resolver behaviour out of the decision.

Why a typed policy instead of a regular expression
--------------------------------------------------
The previous version accepted any string matching ``^https?://\\S+$``. That admits
``http://user:pass@10.0.0.5:8080/v1?key=…``, ``http://169.254.169.254/`` and
``https://evil.example.com.attacker.tld/v1`` — all of which would have received the
Authorization header. A URL is parsed, decomposed and re-assembled from a whitelist of
parts here, so what the transport connects to is exactly what was approved.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
from dataclasses import dataclass, field
from urllib.parse import urlsplit, quote

MODE_LOCAL = "local_loopback"
MODE_CLOUD = "cloud_https"
MODES = (MODE_LOCAL, MODE_CLOUD)
DEFAULT_MODE = MODE_LOCAL

DEFAULT_PORTS = {"http": 80, "https": 443}
MAX_URL_CHARS = 300
MAX_HOST_CHARS = 253
MAX_BASE_PATH_CHARS = 200

# codes returned to callers; all are members of secret_guard's fixed vocabulary where a
# matching public code exists
CODE_OK = "ok"
CODE_INVALID = "endpoint_invalid"
CODE_BLOCKED = "endpoint_blocked"
CODE_REDIRECT = "redirect_blocked"
CODE_PROXY = "proxy_blocked"
CODE_TLS = "tls_failed"
CODE_REBIND = "endpoint_changed"

_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*$")
_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?)*\.?$")
_CONTROL_RE = re.compile(r"[\x00-\x20\x7f]")
_ENCODED_SLASH_RE = re.compile(r"%2f|%5c", re.IGNORECASE)


class PolicyError(ValueError):
    """Raised with a fixed code plus a short sentence that never quotes the input URL."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Target:
    """A parsed, normalised, approved target."""

    scheme: str
    host: str                 # normalised hostname (IDNA) or literal IP
    port: int
    base_path: str            # normalised, no trailing slash, may be empty
    mode: str
    is_ip_literal: bool = False

    @property
    def origin(self) -> str:
        default = DEFAULT_PORTS.get(self.scheme, 0)
        suffix = "" if self.port == default else f":{self.port}"
        return f"{self.scheme}://{self.host}{suffix}"

    @property
    def identity(self) -> str:
        """The string a credential binding is tied to."""
        return f"{self.origin}{self.base_path}"

    def url_for(self, path: str) -> str:
        """Join a *validated* relative path onto this target."""
        return f"{self.origin}{join_path(self.base_path, path)}"

    def describe(self) -> dict:
        """Safe to store and show: no credentials, no query, no user input echoed."""
        return {"mode": self.mode, "scheme": self.scheme, "host": self.host,
                "port": self.port, "base_path": self.base_path,
                "identity": self.identity}


def join_path(base_path: str, path: str) -> str:
    """Segment-wise join. A path may not escape, contain `..` or an encoded slash."""
    raw = str(path or "")
    if not raw:
        return base_path or "/"
    if not raw.startswith("/"):
        raise PolicyError(CODE_INVALID, "内部路径必须以 / 开头")
    if _CONTROL_RE.search(raw) or "\\" in raw:
        raise PolicyError(CODE_INVALID, "内部路径含控制字符或反斜杠")
    lowered = raw.lower()
    if _ENCODED_SLASH_RE.search(raw) or "%2e" in lowered:
        # %2f, %5c and %2e are refused outright: they are how a path-traversal reachable
        # from a model or an error message tries to escape the approved base path.
        raise PolicyError(CODE_INVALID, "内部路径含编码的斜杠或点段")
    segments = []
    for segment in raw.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise PolicyError(CODE_INVALID, "内部路径不能包含 ..")
        segments.append(segment)
    joined = (base_path or "") + "/" + "/".join(segments)
    return "/" + joined.lstrip("/")


def _normalise_host(raw_host: str) -> str:
    """Lower-case, strip a single trailing dot, IDNA-encode, validate."""
    host = (raw_host or "").strip()
    if not host:
        raise PolicyError(CODE_INVALID, "地址里没有主机名")
    if _CONTROL_RE.search(host):
        raise PolicyError(CODE_INVALID, "主机名含控制字符")
    if len(host) > MAX_HOST_CHARS:
        raise PolicyError(CODE_INVALID, "主机名过长")
    lowered = host.lower()
    if lowered.endswith("."):
        lowered = lowered[:-1]
        if not lowered:
            raise PolicyError(CODE_INVALID, "主机名无效")
    if lowered.startswith("[") or lowered.endswith("]"):
        raise PolicyError(CODE_INVALID, "IPv6 字面量请去掉方括号")
    try:
        ipaddress.ip_address(lowered)
        return lowered
    except ValueError:
        pass
    try:
        ascii_host = lowered.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        raise PolicyError(CODE_INVALID, "主机名无法规范化")
    if not _HOSTNAME_RE.match(ascii_host):
        raise PolicyError(CODE_INVALID, "主机名格式不被接受")
    return ascii_host


def parse_base_url(raw, mode=DEFAULT_MODE) -> Target:
    """Parse and validate a base URL for a mode. Raises PolicyError with a fixed code."""
    if mode not in MODES:
        raise PolicyError(CODE_INVALID, "未知的连接模式")
    text = str(raw or "").strip()
    if not text:
        raise PolicyError(CODE_INVALID, "地址为空")
    if len(text) > MAX_URL_CHARS:
        raise PolicyError(CODE_INVALID, "地址过长")
    if _CONTROL_RE.search(text) or "\\" in text:
        # a backslash is treated as a path separator by some proxies and browsers
        raise PolicyError(CODE_INVALID, "地址含控制字符或反斜杠")

    parts = urlsplit(text)
    scheme = (parts.scheme or "").lower()
    if not scheme or not _SCHEME_RE.match(scheme):
        raise PolicyError(CODE_INVALID, "地址缺少协议")
    if scheme not in ("http", "https"):
        raise PolicyError(CODE_INVALID, "只支持 http 或 https")
    if "@" in (parts.netloc or ""):
        raise PolicyError(CODE_INVALID, "地址不能包含用户名或密码")
    if parts.query:
        raise PolicyError(CODE_INVALID, "地址不能带查询参数")
    if parts.fragment:
        raise PolicyError(CODE_INVALID, "地址不能带片段")
    if not parts.netloc:
        raise PolicyError(CODE_INVALID, "地址缺少主机名")

    host_raw = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        raise PolicyError(CODE_INVALID, "端口不是有效数字")
    host = _normalise_host(host_raw)
    try:
        parsed_ip = ipaddress.ip_address(host)
    except ValueError:
        parsed_ip = None

    if port is None:
        port = DEFAULT_PORTS[scheme]
    if not (1 <= int(port) <= 65535):
        raise PolicyError(CODE_INVALID, "端口超出范围")

    base_path = _normalise_base_path(parts.path)

    if mode == MODE_CLOUD:
        if scheme != "https":
            raise PolicyError(CODE_BLOCKED, "云端接口必须使用 https")
    # local_loopback: a literal address must be loopback here, and a hostname is checked in
    # `authorize()` once its addresses are known. `localhost` therefore keeps working while a
    # name that resolves to a private address is refused before any request.
    elif parsed_ip is not None and not parsed_ip.is_loopback:
        raise PolicyError(CODE_BLOCKED, "本机模式只接受回环地址")

    return Target(scheme=scheme, host=host, port=int(port), base_path=base_path,
                  mode=mode, is_ip_literal=parsed_ip is not None)


def _normalise_base_path(raw_path: str) -> str:
    path = str(raw_path or "")
    if not path or path == "/":
        return ""
    if _CONTROL_RE.search(path) or "\\" in path:
        raise PolicyError(CODE_INVALID, "路径含控制字符或反斜杠")
    if _ENCODED_SLASH_RE.search(path):
        raise PolicyError(CODE_INVALID, "路径含编码的斜杠")
    segments = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise PolicyError(CODE_INVALID, "路径不能包含 ..")
        if len(segment) > 64:
            raise PolicyError(CODE_INVALID, "路径段过长")
        segments.append(quote(segment, safe="._~-"))
    joined = "/".join(segments)
    if len(joined) > MAX_BASE_PATH_CHARS:
        raise PolicyError(CODE_INVALID, "路径过长")
    return f"/{joined}" if joined else ""


# --- address classification ---------------------------------------------------------
BLOCKED_V4 = (
    ipaddress.ip_network("0.0.0.0/8"),          # unspecified / this network
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),      # carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),     # link-local + cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),       # IETF protocol assignments
    ipaddress.ip_network("192.0.2.0/24"),       # TEST-NET-1
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),      # benchmarking
    ipaddress.ip_network("198.51.100.0/24"),    # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),     # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),        # multicast
    ipaddress.ip_network("240.0.0.0/4"),        # reserved
)
BLOCKED_V6 = (
    ipaddress.ip_network("::/128"),             # unspecified
    ipaddress.ip_network("::1/128"),            # loopback
    ipaddress.ip_network("::ffff:0:0/96"),      # IPv4-mapped: classify by the v4 part
    ipaddress.ip_network("64:ff9b::/96"),       # NAT64
    ipaddress.ip_network("100::/64"),           # discard-only
    ipaddress.ip_network("2001:db8::/32"),      # documentation
    ipaddress.ip_network("2002::/16"),          # 6to4
    ipaddress.ip_network("fc00::/7"),           # unique local
    ipaddress.ip_network("fe80::/10"),          # link-local
    ipaddress.ip_network("ff00::/8"),           # multicast
)


def classify_ip(value) -> str:
    """'public' or a short reason why an address may not receive a request."""
    try:
        address = value if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)) \
            else ipaddress.ip_address(str(value))
    except ValueError:
        return "unparsable"
    if address.version == 6:
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            return classify_ip(mapped)
        if address.is_loopback:
            return "loopback"
        if address.is_link_local:
            return "link_local"
        if address.is_multicast:
            return "multicast"
        if address.is_unspecified:
            return "unspecified"
        if address.is_private:
            return "private"
        for network in BLOCKED_V6:
            if address in network:
                return "blocked_range"
        if address.is_reserved:
            return "reserved"
        return "public"
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link_local"
    if address.is_multicast:
        return "multicast"
    if address.is_unspecified:
        return "unspecified"
    if address.is_private:
        return "private"
    if address.is_reserved:
        return "reserved"
    for network in BLOCKED_V4:
        if address in network:
            return "blocked_range"
    return "public"


_RESOLVER_SLOTS = threading.BoundedSemaphore(4)

def bounded_resolve(host, port, timeout=10):
    if not _RESOLVER_SLOTS.acquire(blocking=False):
        raise PolicyError('timeout', 'DNS resolver capacity exhausted')
    complete, result = threading.Event(), []
    def worker():
        try:
            result.append((True, resolve(host, port)))
        except Exception as exc:
            result.append((False, exc))
        finally:
            _RESOLVER_SLOTS.release()
            complete.set()
    threading.Thread(target=worker, daemon=True, name='bounded-dns').start()
    if not complete.wait(max(0.001, timeout)):
        raise PolicyError('timeout', 'DNS deadline exceeded')
    ok, value = result[0]
    if not ok:
        raise value
    return value


def resolve(host: str, port: int, family=socket.AF_UNSPEC):
    """All addresses a host resolves to, as (family, address) pairs."""
    try:
        infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise PolicyError(CODE_BLOCKED, f"无法解析主机名（{getattr(exc, 'errno', '')}）")
    seen, out = set(), []
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.add(address)
            out.append((info[0], address))
    if not out:
        raise PolicyError(CODE_BLOCKED, "主机名没有解析到任何地址")
    return out


class EndpointPolicy:
    """Approves one target and answers every later question about it."""

    def __init__(self, target: Target, pinned=None):
        self.target = target
        self._pinned = list(pinned) if pinned else None
        self._verified = list(pinned) if pinned else None

    # -- construction ------------------------------------------------------
    @classmethod
    def for_settings(cls, base_url, mode=None, default_local="http://127.0.0.1:11434"):
        mode = mode or DEFAULT_MODE
        raw = (base_url or "").strip()
        if not raw and mode == MODE_LOCAL:
            raw = default_local
        target = parse_base_url(raw, mode)
        return cls(target)

    @property
    def mode(self):
        return self.target.mode

    @property
    def identity(self):
        return self.target.identity

    def describe(self):
        return self.target.describe()

    # -- authorisation -----------------------------------------------------
    def authorize(self, resolve_now=True, resolver=None):
        """Resolve the target and refuse it when any address is not allowed.

        Every answer is checked, not just the first: a mixed A/AAAA response that contains
        one forbidden address is refused, because the client cannot choose which one the
        operating system will use.
        """
        host, port = self.target.host, self.target.port
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            if self.target.mode == MODE_LOCAL and not literal.is_loopback:
                raise PolicyError(CODE_BLOCKED, "本机模式只接受回环地址")
            if self.target.mode == MODE_CLOUD:
                reason = classify_ip(literal)
                if reason != "public":
                    raise PolicyError(CODE_BLOCKED, f"云端模式不接受这个地址类型（{reason}）")
            self._verified = [(socket.AF_INET6 if literal.version == 6 else socket.AF_INET,
                               str(literal))]
            return self._verified
        if not resolve_now:
            return self._verified or []
        pairs = (resolver or resolve)(host, port)
        for _family, address in pairs:
            reason = classify_ip(address)
            if self.target.mode == MODE_LOCAL:
                if reason != "loopback":
                    raise PolicyError(CODE_BLOCKED, "本机模式的目标解析到了非回环地址")
            elif reason != "public":
                raise PolicyError(CODE_BLOCKED, f"云端模式的目标解析到了禁止地址（{reason}）")
        self._verified = pairs
        return pairs

    def connection_addresses(self):
        """The addresses the transport may connect to — the ones that were verified."""
        if self._verified is None:
            raise PolicyError(CODE_BLOCKED, "目标还没有通过地址校验")
        return list(self._verified)

    def check_connected(self, address):
        """The socket must be on an address the policy approved."""
        if self._verified is None:
            raise PolicyError(CODE_BLOCKED, "目标还没有通过地址校验")
        approved = {item[1] for item in self._verified}
        if address not in approved:
            # resolution changed between approval and connect
            raise PolicyError(CODE_REBIND, "实际连接地址与已校验地址不一致，已中止")
        return True

    # -- redirects ---------------------------------------------------------
    def check_redirect(self, location, base_url=""):
        """Any automatic redirect is refused; the user must approve the new target."""
        from urllib.parse import urljoin
        target = urljoin(base_url or self.target.origin + "/", str(location or ""))
        try:
            parsed = urlsplit(target)
        except ValueError:
            raise PolicyError(CODE_REDIRECT, "服务商返回了无法解析的跳转地址")
        same = (_normalise_host_safe(parsed.hostname or "") == self.target.host
                and (parsed.port or DEFAULT_PORTS.get((parsed.scheme or "").lower(), 0))
                == self.target.port)
        if same and (parsed.scheme or "").lower() == self.target.scheme:
            raise PolicyError(CODE_REDIRECT,
                              "服务商要求跳转到另一个路径，已停止；请直接填写最终地址")
        raise PolicyError(CODE_REDIRECT,
                          "服务商要求跳转到另一个地址，已停止；请确认后再填写该地址")


def _normalise_host_safe(host):
    try:
        return _normalise_host(host)
    except PolicyError:
        return ""


def proxy_environment_present(environ=None):
    """Names of proxy variables in the environment. Reported, never honoured."""
    import os
    env = os.environ if environ is None else environ
    names = [name for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                               "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")
             if env.get(name)]
    return names
