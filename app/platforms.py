from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class Platform:
    id: str
    name: str
    hosts: tuple[str, ...]
    mode: str
    initials: str
    tint: str
    adult: bool = False
    limitation: str | None = None
    alternative: str | None = None

    def public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "mode": self.mode,
            "initials": self.initials,
            "tint": self.tint,
            "adult": self.adult,
            "limitation": self.limitation,
            "alternative": self.alternative,
        }


_LIVE = "Live cam rooms are not recorded. InfinityX does not capture live performers."
_LIVE_ALT = "Watch the show in the official player on the site itself."
_PAY = "This catalogue is sold behind a login or subscription. InfinityX does not bypass paywalls or accounts."
_PAY_ALT = "Open the title on the official site while you are signed in."
_TEXT = "This is a text library. InfinityX does not copy stories or articles."
_TEXT_ALT = "Read the material on the original site if you have permission."
_IMAGE = "This is an image host. InfinityX downloads video and audio files, not image galleries."
_IMAGE_ALT = "View the gallery on the original site."


def _p(*args, **kwargs) -> Platform:
    return Platform(*args, **kwargs)


PLATFORMS: tuple[Platform, ...] = (
    _p("youtube", "YouTube", ("youtube.com", "youtu.be", "youtube-nocookie.com"), "public", "YT", "#ff5a5f"),
    _p("instagram", "Instagram", ("instagram.com",), "public", "IG", "#e1306c"),
    _p("tiktok", "TikTok", ("tiktok.com",), "public", "TT", "#69c9d0"),
    _p("facebook", "Facebook", ("facebook.com", "fb.watch", "fb.com"), "public", "FB", "#4d8dff"),
    _p("x", "X", ("x.com", "twitter.com"), "public", "X", "#d7dee9"),
    _p("reddit", "Reddit", ("reddit.com", "redd.it", "v.redd.it"), "public", "RD", "#ff6a2b"),
    _p("vimeo", "Vimeo", ("vimeo.com",), "public", "VM", "#19d4ff"),
    _p("twitch", "Twitch", ("twitch.tv",), "public", "TW", "#a970ff"),
    _p("dailymotion", "Dailymotion", ("dailymotion.com", "dai.ly"), "public", "DM", "#3d7bff"),
    _p("pornhub", "Pornhub", ("pornhub.com",), "public", "PH", "#ff9000", True),
    _p("xvideos", "XVideos", ("xvideos.com", "xvideos.es", "xvideos2.com"), "public", "XV", "#d3112a", True),
    _p("xnxx", "XNXX", ("xnxx.com",), "public", "XN", "#f45d01", True),
    _p("xhamster", "xHamster", ("xhamster.com",), "public", "XH", "#f5a623", True),
    _p("redtube", "RedTube", ("redtube.com",), "public", "RT", "#e11d2e", True),
    _p("youporn", "YouPorn", ("youporn.com",), "public", "YP", "#ff4f6d", True),
    _p("spankbang", "SpankBang", ("spankbang.com",), "public", "SB", "#ff4b4b", True),
    _p("eporner", "Eporner", ("eporner.com",), "public", "EP", "#7c5cff", True),
    _p("txxx", "Txxx", ("txxx.com",), "public", "TX", "#ff5c8a", True),
    _p("hqporner", "HQporner", ("hqporner.com",), "public", "HQ", "#8b6cff", True),
    _p("beeg", "Beeg", ("beeg.com",), "public", "BG", "#ffd23f", True),
    _p("tiava", "Tiava", ("tiava.com",), "public", "TV", "#46d6c5", True),
    _p("pornhat", "Pornhat", ("pornhat.com",), "public", "HT", "#ff7a59", True),
    _p("brazzers", "Brazzers", ("brazzers.com",), "paywall", "BZ", "#f5c16c", True, _PAY, _PAY_ALT),
    _p("realitykings", "Reality Kings", ("realitykings.com",), "paywall", "RK", "#f0b429", True, _PAY, _PAY_ALT),
    _p("mofos", "Mofos", ("mofos.com",), "paywall", "MF", "#ff8a3d", True, _PAY, _PAY_ALT),
    _p("bangbros", "Bang Bros", ("bangbros.com",), "paywall", "BB", "#ff5d4a", True, _PAY, _PAY_ALT),
    _p("naughtyamerica", "Naughty America", ("naughtyamerica.com",), "paywall", "NA", "#c084fc", True, _PAY, _PAY_ALT),
    _p("bellesa", "Bellesa", ("bellesa.com", "bellesa.co"), "paywall", "BL", "#f472b6", True, _PAY, _PAY_ALT),
    _p("lustcinema", "Lust Cinema", ("lustcinema.com",), "paywall", "LC", "#fb7185", True, _PAY, _PAY_ALT),
    _p("erikalust", "Erika Lust", ("erikalust.com",), "paywall", "EL", "#fda4af", True, _PAY, _PAY_ALT),
    _p("chaturbate", "Chaturbate", ("chaturbate.com",), "live", "CB", "#f97316", True, _LIVE, _LIVE_ALT),
    _p("stripchat", "Stripchat", ("stripchat.com",), "live", "SC", "#fb7185", True, _LIVE, _LIVE_ALT),
    _p("bongacams", "BongaCams", ("bongacams.com",), "live", "BC", "#fb923c", True, _LIVE, _LIVE_ALT),
    _p("livejasmin", "LiveJasmin", ("livejasmin.com",), "live", "LJ", "#e879f9", True, _LIVE, _LIVE_ALT),
    _p("cam4", "Cam4", ("cam4.com",), "live", "C4", "#38bdf8", True, _LIVE, _LIVE_ALT),
    _p("pornpics", "PornPics", ("pornpics.com",), "image", "PP", "#fb7185", True, _IMAGE, _IMAGE_ALT),
    _p("sexcom", "Sex.com", ("sex.com",), "image", "SX", "#f43f5e", True, _IMAGE, _IMAGE_ALT),
    _p("imagefap", "ImageFap", ("imagefap.com",), "image", "IF", "#a78bfa", True, _IMAGE, _IMAGE_ALT),
    _p("imagebam", "ImageBam", ("imagebam.com",), "image", "IB", "#94a3b8", True, _IMAGE, _IMAGE_ALT),
    _p("literotica", "Literotica", ("literotica.com",), "text", "LT", "#f9a8d4", True, _TEXT, _TEXT_ALT),
    _p("asstr", "ASSTR", ("asstr.org",), "text", "AS", "#cbd5e1", True, _TEXT, _TEXT_ALT),
)

OTHER = Platform(
    "other",
    "Other source",
    (),
    "public",
    "URL",
    "#22d3ee",
    False,
    None,
    None,
)

_BY_LENGTH: list[tuple[str, Platform]] = []
for platform in PLATFORMS:
    for host in platform.hosts:
        _BY_LENGTH.append((host, platform))
_BY_LENGTH.sort(key=lambda item: len(item[0]), reverse=True)


def detect_platform(url: str) -> Platform:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        bare = host[4:]
    else:
        bare = host
    for known, platform in _BY_LENGTH:
        if host == known or host.endswith(f".{known}") or bare == known or bare.endswith(f".{known}"):
            return platform
    return OTHER


def all_platforms() -> list[dict]:
    return [platform.public() for platform in PLATFORMS]
