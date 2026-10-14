import json
import mimetypes
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


DEFAULT_GRAPH_API_BASE_URL = "https://graph.facebook.com/v24.0"
GRAPH_API_HOSTS = {"graph.facebook.com", "graph.instagram.com"}
MEDIA_HOST_SUFFIXES = (
    ".cdninstagram.com",
    ".fbcdn.net",
    ".fbsbx.com",
    ".instagram.com",
)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov"}
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_VIDEO_BYTES = 200 * 1024 * 1024
MAX_MEDIA_PER_SYNC = 20
MAX_PAGES_PER_SYNC = 5
REQUEST_TIMEOUT_SECONDS = 30


class InstagramSyncError(RuntimeError):
    pass


def _credentials() -> tuple[str, str, str]:
    access_token = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
    account_id = os.getenv("INSTAGRAM_ACCOUNT_ID", "").strip()
    api_base_url = os.getenv(
        "INSTAGRAM_GRAPH_API_BASE_URL",
        DEFAULT_GRAPH_API_BASE_URL
    ).strip().rstrip("/")
    parsed_base_url = urllib.parse.urlsplit(api_base_url)

    if not access_token or not account_id:
        raise InstagramSyncError(
            "Set INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_ACCOUNT_ID in the server environment."
        )
    if not re.fullmatch(r"\d+", account_id):
        raise InstagramSyncError("INSTAGRAM_ACCOUNT_ID must contain only digits.")
    if (
        parsed_base_url.scheme != "https"
        or parsed_base_url.hostname not in GRAPH_API_HOSTS
        or parsed_base_url.username
        or parsed_base_url.password
        or parsed_base_url.port not in (None, 443)
    ):
        raise InstagramSyncError(
            "INSTAGRAM_GRAPH_API_BASE_URL must use HTTPS with graph.facebook.com or graph.instagram.com."
        )

    return access_token, account_id, api_base_url


def instagram_is_configured() -> bool:
    return bool(
        os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
        and os.getenv("INSTAGRAM_ACCOUNT_ID", "").strip()
    )


def _graph_request(url: str, access_token: str, expected_path: str) -> dict[str, Any]:
    parsed_url = urllib.parse.urlsplit(url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname not in GRAPH_API_HOSTS
        or parsed_url.username
        or parsed_url.password
        or parsed_url.port not in (None, 443)
        or parsed_url.path != expected_path
    ):
        raise InstagramSyncError("Meta returned an invalid Instagram pagination URL.")

    query = urllib.parse.parse_qsl(parsed_url.query, keep_blank_values=True)
    safe_query = urllib.parse.urlencode([
        (key, value) for key, value in query if key != "access_token"
    ])
    safe_url = urllib.parse.urlunsplit((
        parsed_url.scheme,
        parsed_url.netloc,
        parsed_url.path,
        safe_query,
        ""
    ))
    request = urllib.request.Request(
        safe_url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "ShivbhavaniWebsite/1.0"
        }
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        message = ""
        try:
            error_payload = json.loads(error.read().decode("utf-8"))
            message = error_payload.get("error", {}).get("message", "")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            message = ""
        if access_token:
            message = message.replace(access_token, "[redacted]")
        detail = f": {message[:300]}" if message else ""
        raise InstagramSyncError(
            f"Meta Graph API returned HTTP {error.code}{detail}"
        ) from None
    except (OSError, TimeoutError) as error:
        raise InstagramSyncError(
            f"Could not connect to Meta Graph API: {type(error).__name__}."
        ) from None
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InstagramSyncError(
            f"Meta Graph API returned invalid JSON: {type(error).__name__}."
        ) from None

    if not isinstance(payload, dict):
        raise InstagramSyncError("Meta Graph API returned an unexpected response.")
    if payload.get("error"):
        message = str(payload["error"].get("message", "Instagram API request failed."))
        if access_token:
            message = message.replace(access_token, "[redacted]")
        raise InstagramSyncError(message[:300])
    return payload


def fetch_instagram_media(limit: int = MAX_MEDIA_PER_SYNC) -> list[dict[str, Any]]:
    access_token, account_id, api_base_url = _credentials()
    parsed_base_url = urllib.parse.urlsplit(api_base_url)
    media_path = f"{parsed_base_url.path.rstrip('/')}/{account_id}/media"
    fields = "id,caption,media_type,media_url,permalink,timestamp,media_product_type"
    query = urllib.parse.urlencode({"fields": fields, "limit": min(limit, 100)})
    next_url = urllib.parse.urlunsplit((
        parsed_base_url.scheme,
        parsed_base_url.netloc,
        media_path,
        query,
        ""
    ))
    media: list[dict[str, Any]] = []

    for _ in range(MAX_PAGES_PER_SYNC):
        payload = _graph_request(next_url, access_token, media_path)
        page_items = payload.get("data", [])
        if not isinstance(page_items, list):
            raise InstagramSyncError("Meta Graph API returned an invalid media list.")
        media.extend(item for item in page_items if isinstance(item, dict))
        if len(media) >= limit:
            return media[:limit]

        paging = payload.get("paging", {})
        next_url = paging.get("next", "") if isinstance(paging, dict) else ""
        if not next_url:
            break

    return media[:limit]


class _SafeMediaRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        parsed_url = urllib.parse.urlsplit(new_url)
        hostname = (parsed_url.hostname or "").lower()
        if (
            parsed_url.scheme != "https"
            or parsed_url.username
            or parsed_url.password
            or parsed_url.port not in (None, 443)
            or not any(hostname.endswith(suffix) for suffix in MEDIA_HOST_SUFFIXES)
        ):
            raise InstagramSyncError("Instagram media redirected to an untrusted host.")
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )


def _validate_media_url(media_url: str) -> urllib.parse.SplitResult:
    parsed_url = urllib.parse.urlsplit(media_url)
    hostname = (parsed_url.hostname or "").lower()
    if (
        parsed_url.scheme != "https"
        or parsed_url.username
        or parsed_url.password
        or parsed_url.port not in (None, 443)
        or not any(hostname.endswith(suffix) for suffix in MEDIA_HOST_SUFFIXES)
    ):
        raise InstagramSyncError("Instagram returned a media URL from an untrusted host.")
    return parsed_url


def download_instagram_media(
    media_url: str,
    media_type: str,
    upload_dir: str
) -> str:
    parsed_url = _validate_media_url(media_url)
    media_kind = media_type.upper()
    if media_kind == "IMAGE":
        max_bytes = MAX_IMAGE_BYTES
        allowed_extensions = IMAGE_EXTENSIONS
        fallback_extension = ".jpg"
    elif media_kind in {"VIDEO", "REELS"}:
        max_bytes = MAX_VIDEO_BYTES
        allowed_extensions = VIDEO_EXTENSIONS
        fallback_extension = ".mp4"
    else:
        raise InstagramSyncError(f"Unsupported Instagram media type: {media_kind}.")

    url_extension = Path(urllib.parse.unquote(parsed_url.path)).suffix.lower()
    opener = urllib.request.build_opener(_SafeMediaRedirectHandler())
    request = urllib.request.Request(
        media_url,
        headers={
            "Accept": "image/*,video/*",
            "User-Agent": "ShivbhavaniWebsite/1.0"
        }
    )
    temporary_path = ""
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get_content_type().lower()
            if media_kind == "IMAGE" and not content_type.startswith("image/"):
                raise InstagramSyncError("Instagram image URL returned a non-image file.")
            if media_kind in {"VIDEO", "REELS"} and not content_type.startswith("video/"):
                raise InstagramSyncError("Instagram video URL returned a non-video file.")
            extension = url_extension if url_extension in allowed_extensions else ""
            if not extension:
                guessed_extension = mimetypes.guess_extension(content_type) or ""
                if guessed_extension == ".jpe":
                    guessed_extension = ".jpg"
                extension = guessed_extension if guessed_extension in allowed_extensions else fallback_extension

            os.makedirs(upload_dir, exist_ok=True)
            filename = f"instagram_{uuid.uuid4().hex}{extension}"
            file_descriptor, temporary_path = tempfile.mkstemp(
                prefix=".instagram-",
                suffix=".part",
                dir=upload_dir
            )
            written = 0
            with os.fdopen(file_descriptor, "wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise InstagramSyncError(
                            f"Instagram {media_kind.lower()} exceeds the configured upload size limit."
                        )
                    output.write(chunk)
            if not written:
                raise InstagramSyncError("Instagram returned an empty media file.")
            final_path = os.path.join(upload_dir, filename)
            os.replace(temporary_path, final_path)
            return filename
    except InstagramSyncError:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)
        raise
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)
        raise InstagramSyncError(
            f"Could not download Instagram media: {type(error).__name__}."
        ) from None
