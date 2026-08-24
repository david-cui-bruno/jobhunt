from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import hashlib
import ipaddress
import re
import urllib.parse


RESOLVER_NAME = "dreamwork-original-v1"
PUBLIC_TARGET_ERROR = "resolved target is not a public http(s) URL"
MISSING_LINK_ERROR = "original posting link not found"


@dataclass(frozen=True)
class ResolutionResult:
    source_url: str
    resolved_url: str | None
    resolver: str
    source_hash: str
    error: str


@dataclass(frozen=True)
class _Anchor:
    href: str
    text: str


class _DreamworkAnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[_Anchor] = []
        self._capture_href: str | None = None
        self._capture_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attr_map = {name.lower(): value or "" for name, value in attrs}
        classes = attr_map.get("class", "").split()
        href = attr_map.get("href", "")
        if "job-cta-secondary" not in classes or not href:
            return
        self._capture_href = href
        self._capture_text = []

    def handle_data(self, data: str) -> None:
        if self._capture_href is not None:
            self._capture_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._capture_href is not None:
            self.anchors.append(_Anchor(self._capture_href, "".join(self._capture_text)))
            self._capture_href = None
            self._capture_text = []


def validate_public_target(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or "@" in parsed.netloc
    ):
        raise ValueError(PUBLIC_TARGET_ERROR)

    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError(PUBLIC_TARGET_ERROR)

    address = _ip_address_from_host_literal(host)
    if address is not None:
        if not address.is_global:
            raise ValueError(PUBLIC_TARGET_ERROR)
    elif not _is_public_dns_name(host):
        raise ValueError(PUBLIC_TARGET_ERROR)

    netloc = host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urllib.parse.urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, ""))


def _ip_address_from_host_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass

    if re.fullmatch(r"0x[0-9a-f]+", host, re.I):
        return ipaddress.ip_address(int(host, 16))
    if re.fullmatch(r"\d+", host):
        return ipaddress.ip_address(int(host, 10))
    parts = host.split(".")
    if 1 < len(parts) < 4 and all(part.isdigit() for part in parts):
        value = 0
        for index, part in enumerate(parts):
            number = int(part, 10)
            if number > 255:
                raise ValueError(PUBLIC_TARGET_ERROR)
            if index < len(parts) - 1:
                value = (value << 8) + number
            else:
                remaining_octets = 4 - len(parts) + 1
                if number >= 256 ** remaining_octets:
                    raise ValueError(PUBLIC_TARGET_ERROR)
                value = (value << (8 * remaining_octets)) + number
        return ipaddress.ip_address(value)
    if any(part.isdigit() for part in parts) and len(parts) != 4:
        raise ValueError(PUBLIC_TARGET_ERROR)
    return None


def _is_public_dns_name(host: str) -> bool:
    labels = host.split(".")
    if len(labels) < 2:
        return False
    if labels[-1] in {"corp", "home", "internal", "lan", "local"}:
        return False
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", labels[-1]):
        return False
    return all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    )


def resolve_dreamwork_html(source_url: str, html: str) -> ResolutionResult:
    source_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
    parser = _DreamworkAnchorParser()
    parser.feed(html)

    for anchor in parser.anchors:
        if _normalize_text(anchor.text) != "View original posting":
            continue
        target = urllib.parse.urljoin(source_url, anchor.href)
        try:
            resolved_url = validate_public_target(target)
        except ValueError as exc:
            return _result(source_url, source_hash, None, str(exc))
        return _result(source_url, source_hash, resolved_url, "")

    return _result(source_url, source_hash, None, MISSING_LINK_ERROR)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _result(source_url: str, source_hash: str, resolved_url: str | None, error: str) -> ResolutionResult:
    return ResolutionResult(
        source_url=source_url,
        resolved_url=resolved_url,
        resolver=RESOLVER_NAME,
        source_hash=source_hash,
        error=error,
    )
