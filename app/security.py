import ipaddress
import re
import socket
from urllib.parse import urlparse, urlunparse

from .errors import AppError

MAX_URL_LENGTH = 2048
ALLOWED_TOKENS = {"title", "creator", "platform", "date", "ext", "id"}
MEDIA_EXTENSIONS = {
    ".mp4",
    ".webm",
    ".mkv",
    ".mov",
    ".m4v",
    ".mp3",
    ".wav",
    ".m4a",
    ".ogg",
    ".opus",
    ".aac",
    ".flac",
}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".opus", ".aac", ".flac"}
BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
}
MINOR_TERMS = re.compile(
    r"(under\s?age|pre[\s-]?teen|jail\s?bait|loli(ta)?|csam|"
    r"child[\s_-]?porn|pa?edo(?:phile)?|"
    r"\b1[0-7]\s?(?:yo|yr|yrs|years?\s?old)\b)",
    re.IGNORECASE,
)
_TOKEN = re.compile(r"\{([^{}]+)\}")
_TEMPLATE = re.compile(r"[A-Za-z0-9._\- {}]+")


def minor_reference(*values: str | None) -> bool:
    return any(MINOR_TERMS.search(value or "") for value in values)


def _bad_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None and _bad_ip(mapped):
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _parse_ip(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


_PORNHUB_HOST = re.compile(r"^(?:[a-z]{2}\.)?pornhub\.com$", re.IGNORECASE)
_SAVEPORN = frozenset({"saveporn.net", "www.saveporn.net"})
_SAVEPORN_HOME = re.compile(r"^/(?:[a-z]{2})?/?$", re.IGNORECASE)
_PORNHUB_VIDEO = re.compile(r"view_video|embed", re.IGNORECASE)


def _bare_host(host: str) -> str:
    host = (host or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def normalize_user_url(url: str) -> str:
    """Rewrite known mirror sites (e.g. SavePorn) to the source host."""
    raw = (url or "").strip()
    parsed = urlparse(raw)
    bare = _bare_host(parsed.hostname or "")
    if _PORNHUB_HOST.fullmatch(bare):
        return urlunparse(("https", "www.pornhub.com", parsed.path or "/", "", parsed.query, ""))
    if bare not in _SAVEPORN:
        return raw
    path = parsed.path or "/"
    query = parsed.query or ""
    if _SAVEPORN_HOME.match(path) or path.rstrip("/").lower() in {"", "/it", "/en", "/index.php"}:
        raise AppError(
            "Questa è solo la home di SavePorn. Apri il video su Pornhub e nell’indirizzo sostituisci "
            "pornhub.com con saveporn.net, oppure incolla direttamente il link Pornhub del video "
            "(view_video.php?viewkey=...).",
            code="needs_video_url",
        )
    if _PORNHUB_VIDEO.search(path) or "viewkey" in query.lower():
        return urlunparse(("https", "www.pornhub.com", path, "", query, ""))
    raise AppError(
        "Link SavePorn non valido. Deve essere la pagina del video, come "
        "https://www.saveporn.net/view_video.php?viewkey=...",
        code="needs_video_url",
    )


def prepare_source_url(url: str) -> str:
    return assert_public_url(normalize_user_url(url))


def assert_public_url(url: str) -> str:
    if url is None:
        raise AppError("Enter a video URL.")
    raw = url.strip()
    if not raw or len(raw) > MAX_URL_LENGTH:
        raise AppError("That URL is empty or too long.")
    if "\\" in raw or any(char in raw for char in "\r\n\t"):
        raise AppError("That URL is not valid.")
    if minor_reference(raw):
        raise AppError("This link is blocked.", code="blocked")

    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise AppError("Only public http and https links are allowed.")
    if parsed.username or parsed.password:
        raise AppError("Links with embedded usernames or passwords are blocked.")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise AppError("That URL does not have a public host.")
    if host in BLOCKED_HOSTS or host.endswith((".local", ".localhost", ".internal", ".home.arpa")):
        raise AppError("Local and private-network targets are blocked.", code="ssrf")
    if host.isdigit():
        raise AppError("Obfuscated network targets are blocked.", code="ssrf")

    try:
        port = parsed.port
    except ValueError as exc:
        raise AppError("That URL has an invalid port.") from exc
    if port == 0:
        raise AppError("That URL has an invalid port.")

    literal = _parse_ip(host)
    if literal is not None:
        if _bad_ip(literal):
            raise AppError("Local and private-network targets are blocked.", code="ssrf")
    else:
        try:
            infos = socket.getaddrinfo(host, port or 80, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise AppError("Could not resolve that host.") from exc
        addresses = []
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError as exc:
                raise AppError("Could not resolve that host.") from exc
            if _bad_ip(ip):
                raise AppError("Local and private-network targets are blocked.", code="ssrf")
            addresses.append(ip)
        if not addresses:
            raise AppError("Could not resolve that host.")

    hostname = host if literal is None else str(literal)
    if ":" in hostname and literal is not None and literal.version == 6:
        hostname = f"[{hostname}]"
    netloc = hostname if port is None else f"{hostname}:{port}"
    path = parsed.path or "/"
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def safe_thumbnail(url: str | None) -> str | None:
    if not url or not url.startswith("https://"):
        return None
    try:
        return assert_public_url(url)
    except AppError:
        return None


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", name or "")
    cleaned = cleaned.replace("\\", " ").replace("/", " ").replace(":", " ")
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    cleaned = re.sub(r"\.{2,}", ".", cleaned)
    stem = cleaned.split(".", 1)[0].lower()
    if stem in {"con", "prn", "aux", "nul"} or re.fullmatch(r"(com|lpt)[0-9]", stem or ""):
        cleaned = f"file-{cleaned}"
    return (cleaned or "download")[:140]


def validate_template(template: str) -> str:
    value = (template or "").strip()
    if not value or len(value) > 120 or not _TEMPLATE.fullmatch(value):
        raise AppError("Use only letters, numbers, and the tokens {title} {creator} {platform} {date} {id} {ext}.")
    tokens = _TOKEN.findall(value)
    if any(token not in ALLOWED_TOKENS for token in tokens):
        raise AppError("That filename template contains an unknown token.")
    if "{ext}" not in value:
        value = f"{value}.{{ext}}"
    return value


def render_filename(template: str, meta: dict) -> str:
    template = validate_template(template)

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key == "ext":
            ext = re.sub(r"[^A-Za-z0-9]+", "", str(meta.get("ext", "bin")))[:8]
            return ext or "bin"
        return sanitize_filename(str(meta.get(key, "")))

    rendered = _TOKEN.sub(replace, template)
    return sanitize_filename(rendered)


def clean_text(value: str | None, limit: int = 180) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", "", value or "")
    return re.sub(r"\s+", " ", text).strip()[:limit]


def extension_of(url: str) -> str:
    path = urlparse(url).path.lower()
    for ext in MEDIA_EXTENSIONS:
        if path.endswith(ext):
            return ext
    return ""


def is_audio_extension(ext: str) -> bool:
    return ext.lower() if ext.startswith(".") else f".{ext.lower()}" in AUDIO_EXTENSIONS


def friendly_download_error(exc: Exception) -> str:
    if isinstance(exc, AppError):
        return exc.message
    text = " ".join(str(exc).split())
    lowered = text.lower()
    if "drm" in lowered:
        return "This media uses DRM. InfinityX does not remove DRM."
    if any(word in lowered for word in ("private video", "sign in", "login", "cookies", "authentication")):
        return "This media requires a login. InfinityX does not use accounts, cookies, or passwords."
    if "larger than max" in lowered or "max-filesize" in lowered or "max_filesize" in lowered:
        return "The file is larger than the size limit in Settings."
    if "timed out" in lowered or "timeout" in lowered or "stall" in lowered:
        return "The download stopped because the connection was too slow or timed out."
    if any(word in lowered for word in ("unsupported url", "no video formats", "unable to extract", "no formats")):
        return "Questo video non ha un file pubblico scaricabile."
    if "410" in lowered or "gone" in lowered:
        return "Pornhub ha rifiutato questa pagina. Riprova tra qualche secondo."
    return "Questo video non si può scaricare da qui."
