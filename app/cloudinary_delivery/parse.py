# Validate and decompose Cloudinary video delivery URLs. This is the SSRF boundary.

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from ..errors import InvalidCloudinaryUrl, UnsupportedCloudinaryUrl

#: Audio is delivered under the ``video`` resource type, never ``raw``.
REQUIRED_RESOURCE_TYPE = "video"

#: Delivery types whose asset already lives in the account.
STORED_DELIVERY_TYPES = frozenset({"upload", "authenticated", "private"})

#: Delivery types where Cloudinary pulls from a third party on request. The
#: public-id position holds a remote URL (or a provider-specific id), so it is
#: preserved verbatim and the output format is set with ``f_`` rather than by
#: changing an extension. Gated behind ``ALLOW_FETCH_DELIVERY`` because it makes
#: Cloudinary fetch an attacker-chosen URL.
REMOTE_DELIVERY_TYPES = frozenset(
    {"fetch", "youtube", "vimeo", "dailymotion", "hulu", "animoto", "worldstarhiphop"}
)

#: Known Cloudinary transformation parameter prefixes, used to tell a
#: transformation component apart from a folder segment of the public id.
TRANSFORM_PREFIXES = frozenset(
    {
        "a", "ac", "af", "ar", "b", "bo", "br", "c", "co", "cs", "d", "dl",
        "dn", "dpr", "du", "e", "eo", "f", "fl", "fn", "g", "h", "ki", "ky",
        "l", "n", "o", "p", "pg", "q", "r", "so", "sp", "t", "u", "vc", "vs",
        "w", "x", "y", "z",
        # Conditional transformations: if_w_lt_200, if_else, if_end.
        "if",
    }
)

#: Query parameters that affect authorization or delivery and must therefore be
#: carried onto a derived URL. Everything else (SDK analytics, cache busters) is
#: dropped, because it describes the original request rather than the new one.
DEFAULT_FORWARD_QUERY_PARAMS: tuple[str, ...] = ("__cld_token__",)

_VERSION_RE = re.compile(r"^v\d+$")
_SIGNATURE_RE = re.compile(r"^s--[A-Za-z0-9_-]+--$")
_TRANSFORM_PART_RE = re.compile(r"^([a-z]{1,3})_(.*)$")
_RESOURCE_TYPES = frozenset({"image", "video", "raw"})
_REMOTE_PREFIXES = ("http:", "https:", "//")


class UpstreamRedirectRejected(UnsupportedCloudinaryUrl):
    """A redirect pointed somewhere outside the Cloudinary allowlist."""

    code = "redirect_rejected"
    public_message = "Media redirect pointed outside the allowed hosts."


@dataclass(frozen=True)
class CloudinaryAsset:
    """A parsed, validated delivery URL, decomposed so it can be rebuilt."""

    scheme: str
    host: str
    cloud_name: str
    resource_type: str
    delivery_type: str
    signature: str | None
    transformations: tuple[str, ...]
    version: str | None
    public_id: str
    extension: str
    query: str

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.host}"

    @property
    def is_remote(self) -> bool:
        """Whether the public-id position holds a third-party URL or id."""
        return self.delivery_type in REMOTE_DELIVERY_TYPES


def is_host_allowed(host: str, extra_hosts: tuple[str, ...] = ()) -> bool:
    """True for Cloudinary's delivery hosts and explicitly configured CNAMEs."""
    # Exact or anchored matching only, never an unanchored suffix, so
    # res.cloudinary.com.attacker.example does not pass.
    host = host.lower().rstrip(".")
    if host == "res.cloudinary.com":
        return True
    if re.fullmatch(r"res-\d+\.cloudinary\.com", host):
        return True
    return host in extra_hosts


def assert_host_allowed(url: str, extra_hosts: tuple[str, ...] = ()) -> None:
    """Re-validate a URL mid-flight. Used on every redirect hop."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise UpstreamRedirectRejected(f"redirect to non-https scheme {parts.scheme!r}")
    host = parts.hostname
    if not host or not is_host_allowed(host, extra_hosts):
        raise UpstreamRedirectRejected(f"redirect to disallowed host {host!r}")


def filter_query(
    query: str,
    forward_params: tuple[str, ...] = DEFAULT_FORWARD_QUERY_PARAMS,
) -> str:
    """Keep only the query parameters that should survive onto a derived URL."""
    # ("*",) forwards everything verbatim; an empty tuple drops the query.
    # __cld_token__ is forwarded by default, but it only authorizes a derived URL if
    # it was minted with an acl pattern covering it; otherwise Cloudinary answers 401.
    if not query:
        return ""
    if "*" in forward_params:
        return query
    if not forward_params:
        return ""

    wanted = {p.lower() for p in forward_params}
    kept: list[str] = []
    for pair in query.split("&"):
        if not pair:
            continue
        name = pair.split("=", 1)[0]
        if name.lower() in wanted or unquote(name).lower() in wanted:
            # Appended raw, never decoded and re-encoded: a delivery token is an
            # HMAC over its exact characters, and normalising "*" to "%2A" (as
            # urlencode would) is enough to invalidate it.
            kept.append(pair)
    return "&".join(kept)


def _looks_like_transformation(component: str) -> bool:
    """Whether a path component is a transformation rather than a folder name."""
    # A folder literally named so_cool is a false positive; that ambiguity is inherent
    # to Cloudinary's URL format and is mitigated by never testing the final component.
    parts = component.split(",")
    if not parts:
        return False
    for part in parts:
        match = _TRANSFORM_PART_RE.match(part)
        if not match or match.group(1) not in TRANSFORM_PREFIXES:
            return False
    return True


def _split_extension(final_segment: str) -> tuple[str, str]:
    base, dot, ext = final_segment.rpartition(".")
    if not dot or not ext or "/" in ext:
        return final_segment, ""
    return base, ext


def _is_remote_start(segment: str) -> bool:
    lowered = segment.lower()
    return lowered.startswith(_REMOTE_PREFIXES)


def parse_delivery_url(
    url: str,
    *,
    extra_hosts: tuple[str, ...] = (),
    allow_fetch: bool = False,
) -> CloudinaryAsset:
    """Validate ``url`` and decompose it into its Cloudinary components."""
    # InvalidCloudinaryUrl for anything implausible, UnsupportedCloudinaryUrl for URLs
    # that are Cloudinary's but that this service declines. Fragments are discarded.
    if not isinstance(url, str) or not url.strip():
        raise InvalidCloudinaryUrl("empty url")

    parts = urlsplit(url.strip())

    # --- transport and authority -------------------------------------------
    if parts.scheme != "https":
        raise InvalidCloudinaryUrl(f"scheme must be https, got {parts.scheme!r}")
    if "@" in parts.netloc:
        raise InvalidCloudinaryUrl("credentials in URL authority are not accepted")
    try:
        if parts.port is not None:
            raise InvalidCloudinaryUrl("explicit ports are not accepted")
    except ValueError as exc:  # malformed port
        raise InvalidCloudinaryUrl("malformed port in URL authority") from exc

    host = (parts.hostname or "").lower()
    if not host:
        raise InvalidCloudinaryUrl("missing host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass  # not an IP literal, which is what we want
    else:
        raise InvalidCloudinaryUrl("IP-literal hosts are not accepted")
    if not is_host_allowed(host, extra_hosts):
        raise InvalidCloudinaryUrl(
            f"host {host!r} is not a Cloudinary delivery host; "
            "add it to CLOUDINARY_EXTRA_HOSTS if it is your own CNAME"
        )

    # --- path ---------------------------------------------------------------
    raw_path = parts.path
    segments = [s for s in raw_path.split("/") if s]
    if len(segments) < 3:
        raise InvalidCloudinaryUrl("path is too short to be a delivery URL")

    # A private-CDN or CNAME distribution omits the cloud name.
    if segments[0] in _RESOURCE_TYPES:
        cloud_name = ""
        resource_type, delivery_type = segments[0], segments[1]
        rest = segments[2:]
    else:
        if len(segments) < 4:
            raise InvalidCloudinaryUrl("path is too short to be a delivery URL")
        cloud_name, resource_type, delivery_type = segments[0], segments[1], segments[2]
        rest = segments[3:]

    if resource_type != REQUIRED_RESOURCE_TYPE:
        raise UnsupportedCloudinaryUrl(
            f"resource type must be {REQUIRED_RESOURCE_TYPE!r}, got {resource_type!r}; "
            "audio and video are both delivered under 'video'"
        )

    is_remote = delivery_type in REMOTE_DELIVERY_TYPES
    if is_remote:
        if not allow_fetch:
            raise UnsupportedCloudinaryUrl(
                f"delivery type {delivery_type!r} makes Cloudinary fetch a third-party URL; "
                "enable ALLOW_FETCH_DELIVERY to permit it"
            )
    elif delivery_type not in STORED_DELIVERY_TYPES:
        raise UnsupportedCloudinaryUrl(f"unknown delivery type {delivery_type!r}")

    if not rest:
        raise InvalidCloudinaryUrl("no public id in path")

    index = 0
    signature: str | None = None
    transformations: list[str] = []
    version: str | None = None

    def at_last() -> bool:
        return index >= len(rest) - 1

    # The signature sits right after the delivery type in Cloudinary's own
    # output, but some SDKs emit it after the transformations, so both positions
    # are accepted.
    if not at_last() and _SIGNATURE_RE.match(rest[index]):
        signature = rest[index]
        index += 1

    while not at_last() and _looks_like_transformation(rest[index]):
        if _is_remote_start(rest[index]):
            break
        transformations.append(rest[index])
        index += 1

    if signature is None and not at_last() and _SIGNATURE_RE.match(rest[index]):
        signature = rest[index]
        index += 1

    if not at_last() and _VERSION_RE.match(rest[index]):
        version = rest[index]
        index += 1

    if index > len(rest) - 1:
        raise InvalidCloudinaryUrl("no public id in path")

    if is_remote:
        # The remainder is a whole third-party URL. Re-joining split segments
        # would collapse the "//" in "https://", so it is taken verbatim from the
        # original path instead.
        consumed = [seg for seg in rest[:index]]
        prefix_parts = [cloud_name, resource_type, delivery_type, *consumed]
        prefix = "/" + "/".join(p for p in prefix_parts if p) + "/"
        marker = raw_path.find(prefix)
        public_id = raw_path[marker + len(prefix) :] if marker != -1 else "/".join(rest[index:])
        extension = ""
    else:
        public_id, extension = _split_extension("/".join(rest[index:]))

    if not public_id:
        raise InvalidCloudinaryUrl("empty public id")

    return CloudinaryAsset(
        scheme=parts.scheme,
        host=host,
        cloud_name=cloud_name,
        resource_type=resource_type,
        delivery_type=delivery_type,
        signature=signature,
        transformations=tuple(transformations),
        version=version,
        public_id=public_id,
        extension=extension,
        query=parts.query,
    )
