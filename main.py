import asyncio
from contextlib import asynccontextmanager, suppress
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException, status, Response
import logging
import threading
from typing import List
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn
from datetime import datetime
import json
import os
import re
import secrets
import shutil
import uuid
import hmac
import mimetypes
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit
from dotenv import load_dotenv
from PIL import Image, ImageOps
from instagram_sync import (
    InstagramSyncError,
    download_instagram_media,
    fetch_instagram_media,
    instagram_is_configured,
)

load_dotenv()
mimetypes.add_type("image/webp", ".webp")

logger = logging.getLogger(__name__)
INSTAGRAM_SYNC_INTERVAL_SECONDS = int(os.getenv("INSTAGRAM_SYNC_INTERVAL_SECONDS", "300"))
if not 60 <= INSTAGRAM_SYNC_INTERVAL_SECONDS <= 3600:
    raise ValueError("INSTAGRAM_SYNC_INTERVAL_SECONDS must be between 60 and 3600.")
INSTAGRAM_SYNC_LOCK = threading.Lock()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    sync_task = None
    if instagram_is_configured():
        sync_task = asyncio.create_task(instagram_sync_loop())
    else:
        logger.info("Instagram auto-sync is disabled until its server credentials are configured.")
    try:
        yield
    finally:
        if sync_task:
            sync_task.cancel()
            with suppress(asyncio.CancelledError):
                await sync_task


app = FastAPI(
    title="Shivbhavani Safa Paghdi",
    description="Developed by Hit Savani",
    version="2.0.0",
    lifespan=lifespan,
)

RENDER_DISK_DIR = "/opt/render/project/src/persistent"
configured_data_dir = os.getenv("PERSISTENT_DATA_DIR", "").strip()
if configured_data_dir and not os.path.isdir(configured_data_dir):
    if os.path.isdir(RENDER_DISK_DIR):
        logger.warning(
            "PERSISTENT_DATA_DIR does not exist: %s. Using the mounted Render disk at %s.",
            configured_data_dir,
            RENDER_DISK_DIR
        )
        configured_data_dir = RENDER_DISK_DIR
    else:
        logger.warning(
            "PERSISTENT_DATA_DIR does not exist: %s, and no Render disk is mounted. "
            "Continuing with ephemeral storage; uploaded media and JSON data will not "
            "survive an instance restart or redeploy.",
            configured_data_dir
        )
        configured_data_dir = ""
if configured_data_dir and not os.access(configured_data_dir, os.W_OK):
    raise RuntimeError(
        f"PERSISTENT_DATA_DIR is not writable: {configured_data_dir}. "
        "Verify the persistent disk permissions before starting the app."
    )
PERSISTENT_DATA_DIR = configured_data_dir or (
    RENDER_DISK_DIR if os.path.isdir(RENDER_DISK_DIR) else "."
)
UPLOAD_DIR = os.path.join(PERSISTENT_DATA_DIR, "uploads") if PERSISTENT_DATA_DIR != "." else os.path.join("static", "uploads")
MEDIA_URL_PREFIX = "/media" if PERSISTENT_DATA_DIR != "." else "/static/uploads"
PUBLIC_SITE_URL = os.getenv("SITE_URL", "").strip().rstrip("/")
MAX_VIDEO_BYTES = 200 * 1024 * 1024
MAX_IMAGE_BYTES = 30 * 1024 * 1024

# Ensure required directories exist
os.makedirs("templates", exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
if PERSISTENT_DATA_DIR != ".":
    bundled_uploads = os.path.join("static", "uploads")
    if os.path.isdir(bundled_uploads):
        for filename in os.listdir(bundled_uploads):
            source = os.path.join(bundled_uploads, filename)
            target = os.path.join(UPLOAD_DIR, filename)
            if os.path.isfile(source) and not os.path.exists(target):
                shutil.copy2(source, target)
    for data_filename in (
        "collection.json", "business.json", "design.json",
        "admin_history.json", "inquiries.json", "orders.json", "logo_config.json",
        "categories.json", "hero_config.json", "admin_label_colors.json",
        "hero_companion_config.json"
    ):
        source = data_filename
        target = os.path.join(PERSISTENT_DATA_DIR, data_filename)
        if os.path.isfile(source) and not os.path.exists(target):
            shutil.copy2(source, target)


def uploaded_file_path(directory: str, filename: str) -> str:
    if not filename or filename != os.path.basename(filename):
        raise HTTPException(status_code=404, detail="Media file not found")
    base_dir = os.path.realpath(directory)
    filepath = os.path.realpath(os.path.join(base_dir, filename))
    if os.path.commonpath((base_dir, filepath)) != base_dir or not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="Media file not found")
    return filepath


def iter_file_range(filepath: str, start: int, end: int):
    with open(filepath, "rb") as media_file:
        media_file.seek(start)
        remaining = end - start + 1
        while remaining:
            chunk = media_file.read(min(256 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def uploaded_file_response(request: Request, filepath: str):
    size = os.path.getsize(filepath)
    headers = {"Accept-Ranges": "bytes"}
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(filepath, headers=headers)

    range_match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not range_match or not any(range_match.groups()):
        return Response(
            status_code=416,
            headers={**headers, "Content-Range": f"bytes */{size}"}
        )

    first, last = range_match.groups()
    if first:
        start = int(first)
        end = int(last) if last else size - 1
    else:
        suffix_length = int(last)
        if suffix_length <= 0:
            return Response(
                status_code=416,
                headers={**headers, "Content-Range": f"bytes */{size}"}
            )
        start = max(size - suffix_length, 0)
        end = size - 1

    if start >= size or end < start:
        return Response(
            status_code=416,
            headers={**headers, "Content-Range": f"bytes */{size}"}
        )
    end = min(end, size - 1)
    headers.update({
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Content-Length": str(end - start + 1)
    })
    media_type = mimetypes.guess_type(filepath)[0] or "application/octet-stream"
    if request.method == "HEAD":
        return Response(status_code=206, media_type=media_type, headers=headers)
    return StreamingResponse(
        iter_file_range(filepath, start, end),
        status_code=206,
        media_type=media_type,
        headers=headers
    )


@app.api_route("/static/uploads/{filename}", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_static_upload(request: Request, filename: str):
    upload_dir = os.path.join("static", "uploads")
    return uploaded_file_response(request, uploaded_file_path(upload_dir, filename))


@app.api_route("/media/{filename}", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_persistent_upload(request: Request, filename: str):
    return uploaded_file_response(request, uploaded_file_path(UPLOAD_DIR, filename))


app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/media", StaticFiles(directory=UPLOAD_DIR), name="media")
templates = Jinja2Templates(directory="templates")


def create_webp_variant(filepath: str) -> str | None:
    if os.path.splitext(filepath)[1].lower() not in {".jpg", ".jpeg", ".png"}:
        return None

    webp_path = os.path.splitext(filepath)[0] + ".webp"
    temporary_path = webp_path + ".tmp"
    try:
        with Image.open(filepath) as source:
            image = ImageOps.exif_transpose(source)
            has_alpha = "A" in image.getbands() or "transparency" in image.info
            image = image.convert("RGBA" if has_alpha else "RGB")
            original_size = image.size
            image.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
            image.save(temporary_path, format="WEBP", quality=82, method=4)
        dimensions_reduced = image.size != original_size
        if not dimensions_reduced and os.path.getsize(temporary_path) >= os.path.getsize(filepath):
            os.remove(temporary_path)
            logger.info("Keeping original image because its WebP variant is not smaller: %s", filepath)
            return None
        os.replace(temporary_path, webp_path)
    except OSError:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
        raise
    return webp_path


def webp_variant_url(source_url: str | None) -> str:
    if not source_url:
        return ""
    parsed_url = urlsplit(source_url)
    if parsed_url.scheme or parsed_url.netloc:
        return ""

    source_path = unquote(parsed_url.path)
    if source_path.startswith("/static/"):
        base_dir = os.path.realpath("static")
        relative_path = source_path[len("/static/"):]
    elif source_path.startswith(f"{MEDIA_URL_PREFIX}/"):
        base_dir = os.path.realpath(UPLOAD_DIR)
        relative_path = source_path[len(MEDIA_URL_PREFIX) + 1:]
    else:
        return ""

    if os.path.splitext(relative_path)[1].lower() not in {".jpg", ".jpeg", ".png"}:
        return ""
    variant_path = os.path.realpath(
        os.path.join(base_dir, os.path.splitext(relative_path)[0] + ".webp")
    )
    if os.path.commonpath((base_dir, variant_path)) != base_dir or not os.path.isfile(variant_path):
        return ""
    return os.path.splitext(source_path)[0] + ".webp"


templates.env.filters["webp_url"] = webp_variant_url


def persistent_file(filename):
    return os.path.join(PERSISTENT_DATA_DIR, filename) if PERSISTENT_DATA_DIR != "." else filename


def get_public_site_url(request: Request) -> str:
    return PUBLIC_SITE_URL or str(request.base_url).rstrip("/")


def save_upload(upload: UploadFile, filepath: str, max_bytes: int, media_label: str):
    written = 0
    try:
        with open(filepath, "wb") as buffer:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"{media_label} is too large. Maximum allowed size is {max_bytes // (1024 * 1024)} MB."
                    )
                buffer.write(chunk)
        if media_label.lower() != "video":
            create_webp_variant(filepath)
    except HTTPException:
        if os.path.exists(filepath):
            os.remove(filepath)
        raise
    except OSError as error:
        if os.path.exists(filepath):
            os.remove(filepath)
        raise HTTPException(status_code=500, detail=f"Could not save {media_label.lower()}: {error}") from error
    return written


LEADS_FILE = persistent_file("inquiries.json")
ORDERS_FILE = persistent_file("orders.json")
COLLECTION_FILE = persistent_file("collection.json")
HISTORY_FILE = persistent_file("admin_history.json")
LOGO_CONFIG_FILE = persistent_file("logo_config.json")
DESIGN_FILE = persistent_file("design.json")
ADMIN_LABEL_COLORS_FILE = persistent_file("admin_label_colors.json")
BUSINESS_FILE = persistent_file("business.json")
CATEGORY_FILE = persistent_file("categories.json")
HERO_CONFIG_FILE = persistent_file("hero_config.json")
HERO_COMPANION_CONFIG_FILE = persistent_file("hero_companion_config.json")
INSTAGRAM_SYNC_FILE = persistent_file("instagram_sync.json")
DEFAULT_LOGO_URL = "/static/logo.svg"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "S@vani6055")
ACTIVE_ADMIN_SESSIONS = set()

DEFAULT_DESIGN = {
    "primary_color": "#5B0E1E",
    "accent_color": "#C5A059",
    "background_color": "#FBF8F1"
}

DEFAULT_ADMIN_LABEL_COLORS = {
    "category": "#0F766E",
    "badge": "#6D28D9",
    "product_title": "#5B0E1E",
    "branch": "#B45309"
}

DEFAULT_CATEGORY_LABEL_COLORS = {
    "Safa": "#0F766E",
    "Paghdi": "#1D4ED8",
    "Brooch": "#7E22CE",
    "Kalgi": "#B45309",
    "Achkan": "#9F1239",
    "Indo-Western": "#0E7490",
    "Talwar": "#4338CA",
    "Katar - Dhal": "#047857",
    "Angarkha": "#A21CAF",
    "Jodhpuri": "#C2410C",
    "Jewellery": "#A16207",
    "Kamarbandh / Kamalbelt": "#BE123C",
    "Koti": "#0369A1",
    "Blazer": "#6D28D9",
    "Kurta": "#15803D",
    "Scarf": "#C026D3"
}

DEFAULT_BUSINESS = {
    "phone": "919925245246",
    "instagram": "shivbhavani_safa_bhavnagar",
    "headline": "Royal Elegance. Authentic Heritage.",
    "announcement": "Custom wedding turbans and royal groom accessories in Bhavnagar and Surat.",
    "timings": "Open daily by appointment"
}

DEFAULT_CATEGORIES = [
    "Safa", "Paghdi", "Brooch", "Kalgi", "Achkan", "Indo-Western",
    "Talwar", "Katar - Dhal", "Angarkha", "Jodhpuri", "Jewellery",
    "Kamarbandh / Kamalbelt", "Koti", "Blazer", "Kurta", "Scarf"
]

# Pre-seeded collection using your showroom assets
DEFAULT_COLLECTION = [
    {
        "id": "item_1",
        "title": "Handcrafted Silver Talwar & Katar",
        "category": "Talwar & Accessories",
        "type": "image",
        "url": "/static/uploads/silver_talwar.jpg",
        "badge": "Royal Armor"
    },
    {
        "id": "item_2",
        "title": "Ivory Embroidered Indo-Western Set",
        "category": "Indo-Western",
        "type": "video",
        "url": "/static/uploads/indo_western.mp4",
        "badge": "Reel Showcase"
    },
    {
        "id": "item_3",
        "title": "Maroon Velvet Embroidered Jodhpuri",
        "category": "Jodhpuri",
        "type": "video",
        "url": "/static/uploads/maroon_jodhpuri.mp4",
        "badge": "Groom's Choice"
    },
    {
        "id": "item_4",
        "title": "Royal Horse Embroidered Jodhpuri Suit",
        "category": "Jodhpuri",
        "type": "video",
        "url": "/static/uploads/horse_jodhpuri.mp4",
        "badge": "Signature"
    },
    {
        "id": "item_5",
        "title": "Rajwadi Achkan with Embroidered Kamarbandh",
        "category": "Achkan",
        "type": "video",
        "url": "/static/uploads/rajwadi_achkan.mp4",
        "badge": "Heritage"
    },
    {
        "id": "item_6",
        "title": "Traditional Mint & Gold Angarkha",
        "category": "Angarkha",
        "type": "image",
        "url": "/static/uploads/royal_angarkha.jpg",
        "badge": "Classic"
    }
]


def get_collection():
    if not os.path.exists(COLLECTION_FILE):
        if PERSISTENT_DATA_DIR != "." and os.path.exists("collection.json"):
            shutil.copy2("collection.json", COLLECTION_FILE)
        else:
            with open(COLLECTION_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_COLLECTION, f, indent=4, ensure_ascii=False)
            return DEFAULT_COLLECTION
    try:
        with open(COLLECTION_FILE, "r", encoding="utf-8") as f:
            collection = json.load(f)
            if not isinstance(collection, list):
                return []
            normalized = []
            for item in collection:
                if not isinstance(item, dict):
                    continue
                item.setdefault("id", f"item_{uuid.uuid4().hex}")
                item.setdefault("title", "Untitled item")
                item.setdefault("category", "Wedding Accessories")
                item.setdefault("type", "image")
                item.setdefault("url", "")
                item.setdefault("badge", "Royal Look")
                item.setdefault("description", "")
                item.setdefault("colors", "")
                if not isinstance(item.get("occasions"), str) or not item["occasions"].strip():
                    item["occasions"] = "Wedding"
                item.setdefault("availability", "Available")
                item.setdefault("price_label", "")
                item.setdefault("featured", False)
                if not isinstance(item.get("images"), list):
                    item["images"] = []
                normalized.append(item)
            collection = normalized
            for item in collection:
                if item.get("url", "").startswith("/static/uploads/") and PERSISTENT_DATA_DIR != ".":
                    item["url"] = item["url"].replace("/static/uploads/", f"{MEDIA_URL_PREFIX}/")
                for image in item.get("images", []):
                    if isinstance(image, dict) and image.get("url", "").startswith("/static/uploads/") and PERSISTENT_DATA_DIR != ".":
                        image["url"] = image["url"].replace("/static/uploads/", f"{MEDIA_URL_PREFIX}/")
            return [item for item in collection if media_file_exists(item)]
    except Exception:
        return DEFAULT_COLLECTION


def homepage_upload_path(photo_url):
    if not isinstance(photo_url, str):
        return None
    parsed_url = urlsplit(photo_url)
    if parsed_url.scheme or parsed_url.netloc or parsed_url.query or parsed_url.fragment:
        return None

    for url_prefix, directory in (
        (MEDIA_URL_PREFIX, UPLOAD_DIR),
        ("/static/uploads", os.path.join("static", "uploads"))
    ):
        prefix = f"{url_prefix.rstrip('/')}/"
        if not parsed_url.path.startswith(prefix):
            continue
        filename = unquote(parsed_url.path[len(prefix):])
        if not filename or filename != os.path.basename(filename):
            return None
        base_dir = os.path.realpath(directory)
        filepath = os.path.realpath(os.path.join(base_dir, filename))
        if os.path.commonpath((base_dir, filepath)) != base_dir or not os.path.isfile(filepath):
            return None
        return filepath
    return None


def get_homepage_image_options(items):
    image_extensions = {".jpg", ".jpeg", ".png", ".webp"}
    options_by_filename = {}

    def add_option(photo_url, title, category="Uploaded photo"):
        photo_path = homepage_upload_path(photo_url)
        if not photo_path:
            return
        filename = os.path.basename(photo_path)
        extension = os.path.splitext(filename)[1].lower()
        if extension not in image_extensions:
            return
        if extension == ".webp" and any(
            os.path.isfile(os.path.splitext(photo_path)[0] + source_extension)
            for source_extension in (".jpg", ".jpeg", ".png")
        ):
            return
        options_by_filename.setdefault(filename, {
            "url": photo_url,
            "title": title,
            "category": category
        })

    for item in items:
        if item.get("type") != "image":
            continue
        item_title = str(item.get("title") or "Collection photo").strip()
        item_category = str(item.get("category") or "Collection").strip()
        gallery = item.get("images") if isinstance(item.get("images"), list) else []
        item_images = [item.get("url")]
        item_images.extend(
            image.get("url") if isinstance(image, dict) else image
            for image in gallery
        )
        seen_urls = set()
        view_number = 0
        for photo_url in item_images:
            if not isinstance(photo_url, str) or photo_url in seen_urls:
                continue
            seen_urls.add(photo_url)
            view_number += 1
            title = item_title if view_number == 1 else f"{item_title} · photo {view_number}"
            add_option(photo_url, title, item_category)

    directories = [(UPLOAD_DIR, MEDIA_URL_PREFIX)]
    static_uploads = os.path.join("static", "uploads")
    if os.path.realpath(static_uploads) != os.path.realpath(UPLOAD_DIR):
        directories.append((static_uploads, "/static/uploads"))

    for directory, url_prefix in directories:
        if not os.path.isdir(directory):
            continue
        with os.scandir(directory) as directory_entries:
            for entry in sorted(directory_entries, key=lambda file_entry: file_entry.name.casefold()):
                if not entry.is_file(follow_symlinks=False):
                    continue
                extension = os.path.splitext(entry.name)[1].lower()
                if extension not in image_extensions:
                    continue
                photo_url = f"{url_prefix.rstrip('/')}/{entry.name}"
                if entry.name not in options_by_filename:
                    title = os.path.splitext(entry.name)[0].replace("_", " ").replace("-", " ").strip()
                    add_option(photo_url, title.title() or "Uploaded photo")

    return sorted(options_by_filename.values(), key=lambda option: option["title"].casefold())


def get_configured_hero_media(
    items,
    config_path,
    default_title,
    default_url="",
    fallback_terms=()
):
    image_items = [item for item in items if item.get("type") == "image"]
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as config_file:
                hero_config = json.load(config_file)
            if not isinstance(hero_config, dict):
                raise ValueError("Homepage photo config must be a JSON object.")
            selected_id = hero_config.get("media_id", "")
            selected = next(
                (item for item in image_items if item.get("id") == selected_id),
                None
            )
            if selected:
                return selected
            configured_url = hero_config.get("url", "")
            if homepage_upload_path(configured_url):
                return {
                    "id": "uploaded-hero",
                    "title": hero_config.get("title", default_title),
                    "category": "Homepage",
                    "type": "image",
                    "url": configured_url
                }
            logger.warning("Configured hero photo is no longer available.")
        except (OSError, json.JSONDecodeError, ValueError, AttributeError):
            logger.exception("Could not read configured hero photo from %s.", config_path)
    if default_url:
        default_path = os.path.join("static", default_url.removeprefix("/static/"))
        if os.path.isfile(default_path):
            return {
                "id": "default-hero-companion",
                "title": default_title,
                "category": "Homepage",
                "type": "image",
                "url": default_url
            }
    if not fallback_terms:
        return None
    matching_item = next(
        (
            item for item in image_items
            if any(
                term in f"{item.get('title', '')} {item.get('category', '')}".casefold()
                for term in fallback_terms
            )
        ),
        None
    )
    return matching_item


def get_hero_media(items):
    return get_configured_hero_media(
        items,
        HERO_CONFIG_FILE,
        "Homepage Royal Photo",
        fallback_terms=("paghdi", "pagdi", "safa", "turban")
    )


def get_hero_companion_media(items):
    return get_configured_hero_media(
        items,
        HERO_COMPANION_CONFIG_FILE,
        "Homepage Companion Photo",
        default_url="/static/uploads/hero_companion_navy.jpg"
    )


def save_hero_config(config, config_path=HERO_CONFIG_FILE):
    temporary_path = f"{config_path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary_path, "w", encoding="utf-8") as config_file:
            json.dump(config, config_file, indent=4, ensure_ascii=False)
        os.replace(temporary_path, config_path)
    except OSError as error:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
        logger.exception("Could not save the homepage hero photo.")
        raise HTTPException(status_code=500, detail="Could not save the homepage hero photo.") from error


def media_file_exists(item):
    url = item.get("url", "")
    if PERSISTENT_DATA_DIR != "." and url.startswith(f"{MEDIA_URL_PREFIX}/"):
        return os.path.isfile(os.path.join(UPLOAD_DIR, url.split(f"{MEDIA_URL_PREFIX}/", 1)[1]))
    if not url.startswith("/static/"):
        return True
    relative_path = url.split("/static/", 1)[1]
    return os.path.isfile(os.path.join("static", relative_path))


def get_inquiries():
    if os.path.exists(LEADS_FILE):
        try:
            with open(LEADS_FILE, "r", encoding="utf-8") as f:
                records = json.load(f)
                return [record for record in records if isinstance(record, dict)] if isinstance(records, list) else []
        except Exception:
            return []
    return []


def get_orders():
    if os.path.exists(ORDERS_FILE):
        try:
            with open(ORDERS_FILE, "r", encoding="utf-8") as f:
                records = json.load(f)
                return [record for record in records if isinstance(record, dict)] if isinstance(records, list) else []
        except Exception:
            return []
    return []


def get_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                records = json.load(f)
                return [record for record in records if isinstance(record, dict)] if isinstance(records, list) else []
        except Exception:
            return []
    return []


def get_logo_url():
    if os.path.exists(LOGO_CONFIG_FILE):
        try:
            with open(LOGO_CONFIG_FILE, "r", encoding="utf-8") as f:
                logo_url = json.load(f).get("url", "")
            logo_path = logo_url.removeprefix("/static/")
            if logo_url.startswith(f"{MEDIA_URL_PREFIX}/") and os.path.exists(os.path.join(UPLOAD_DIR, logo_path)):
                return logo_url
            if logo_url.startswith("/static/") and os.path.exists(os.path.join("static", logo_path)):
                return logo_url
        except Exception:
            pass
    return DEFAULT_LOGO_URL


def get_design():
    if os.path.exists(DESIGN_FILE):
        try:
            with open(DESIGN_FILE, "r", encoding="utf-8") as f:
                design = {**DEFAULT_DESIGN, **json.load(f)}
                if all(re.fullmatch(r"#[0-9a-fA-F]{6}", design[key]) for key in DEFAULT_DESIGN):
                    return design
        except Exception:
            pass
    return DEFAULT_DESIGN.copy()


def contrast_text_color(color: str) -> str:
    channels = [int(color[index:index + 2], 16) for index in (1, 3, 5)]
    luminance = (0.299 * channels[0] + 0.587 * channels[1] + 0.114 * channels[2]) / 255
    return "#211A1D" if luminance > 0.58 else "#FFFAF0"


def get_admin_label_colors():
    colors = DEFAULT_ADMIN_LABEL_COLORS.copy()
    category_overrides = {}
    if os.path.exists(ADMIN_LABEL_COLORS_FILE):
        try:
            with open(ADMIN_LABEL_COLORS_FILE, "r", encoding="utf-8") as color_file:
                saved_colors = json.load(color_file)
            if not isinstance(saved_colors, dict):
                raise ValueError("Admin label colors must be a JSON object.")
            for key in colors:
                value = saved_colors.get(key)
                if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value):
                    colors[key] = value.upper()
            saved_category_colors = saved_colors.get("categories", {})
            if isinstance(saved_category_colors, dict):
                category_overrides = {
                    category: value.upper()
                    for category, value in saved_category_colors.items()
                    if isinstance(category, str)
                    and isinstance(value, str)
                    and re.fullmatch(r"#[0-9a-fA-F]{6}", value)
                }
        except (OSError, json.JSONDecodeError, ValueError):
            logger.exception("Could not read Admin label colors from %s.", ADMIN_LABEL_COLORS_FILE)
    return {
        **colors,
        "category_overrides": category_overrides,
        "category_text": contrast_text_color(colors["category"]),
        "badge_text": contrast_text_color(colors["badge"]),
        "branch_text": contrast_text_color(colors["branch"])
    }


def get_category_label_colors(categories):
    saved_colors = get_admin_label_colors()
    category_overrides = saved_colors["category_overrides"]
    return {
        category: {
            "background": category_overrides.get(
                category,
                DEFAULT_CATEGORY_LABEL_COLORS.get(category, saved_colors["category"])
            ),
            "text": contrast_text_color(category_overrides.get(
                category,
                DEFAULT_CATEGORY_LABEL_COLORS.get(category, saved_colors["category"])
            ))
        }
        for category in categories
        if isinstance(category, str) and category
    }


def get_business():
    if os.path.exists(BUSINESS_FILE):
        try:
            with open(BUSINESS_FILE, "r", encoding="utf-8") as f:
                return {**DEFAULT_BUSINESS, **json.load(f)}
        except Exception:
            pass
    return DEFAULT_BUSINESS.copy()


def get_categories():
    if os.path.exists(CATEGORY_FILE):
        try:
            with open(CATEGORY_FILE, "r", encoding="utf-8") as f:
                categories = json.load(f)
            if isinstance(categories, list):
                clean = [str(category).strip() for category in categories if str(category).strip()]
                return list(dict.fromkeys(clean))
        except Exception:
            pass
    return DEFAULT_CATEGORIES.copy()


def save_categories(categories):
    clean = list(dict.fromkeys(
        str(category).strip() for category in categories if str(category).strip()
    ))
    with open(CATEGORY_FILE, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=4, ensure_ascii=False)
    return clean


def remember_category(category):
    category = category.strip()
    if category and category.casefold() not in {item.casefold() for item in get_categories()}:
        save_categories(get_categories() + [category])


def get_instagram_sync_status():
    configured = instagram_is_configured()
    default_state = {
        "status": "ready" if configured else "needs_configuration",
        "last_sync_at": "",
        "imported": 0,
        "skipped": 0,
        "message": "" if configured else (
            "Set INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_ACCOUNT_ID in the server environment."
        ),
        "interval_seconds": INSTAGRAM_SYNC_INTERVAL_SECONDS,
        "errors": []
    }
    if not os.path.exists(INSTAGRAM_SYNC_FILE):
        return {**default_state, "configured": configured}
    try:
        with open(INSTAGRAM_SYNC_FILE, "r", encoding="utf-8") as sync_file:
            state = json.load(sync_file)
        if not isinstance(state, dict):
            raise ValueError("Instagram sync status must be a JSON object.")
        return {**default_state, **state, "configured": configured}
    except (OSError, json.JSONDecodeError, ValueError) as error:
        logger.exception("Could not read Instagram sync status.")
        return {
            **default_state,
            "status": "error",
            "message": f"Could not read sync status: {error}",
            "configured": configured
        }


def save_instagram_sync_status(sync_status):
    temporary_path = f"{INSTAGRAM_SYNC_FILE}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary_path, "w", encoding="utf-8") as sync_file:
            json.dump(sync_status, sync_file, indent=4, ensure_ascii=False)
        os.replace(temporary_path, INSTAGRAM_SYNC_FILE)
    except OSError:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
        raise


def sync_instagram_media():
    if not INSTAGRAM_SYNC_LOCK.acquire(blocking=False):
        return {**get_instagram_sync_status(), "status": "running", "message": "A sync is already running."}

    try:
        if not instagram_is_configured():
            result = {
                **get_instagram_sync_status(),
                "status": "needs_configuration",
                "message": "Set INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_ACCOUNT_ID in the server environment."
            }
            save_instagram_sync_status(result)
            return result

        feed = fetch_instagram_media()
        items = get_collection()
        imported_ids = {
            str(item.get("instagram_media_id"))
            for item in items
            if item.get("instagram_media_id")
        }
        new_items = []
        errors = []
        skipped = 0

        for post in feed:
            media_id = str(post.get("id", "")).strip()
            media_type = str(post.get("media_type", "")).upper()
            if not media_id or media_id in imported_ids:
                skipped += 1
                continue
            if media_type not in {"IMAGE", "VIDEO"}:
                skipped += 1
                continue

            media_url = str(post.get("media_url", "")).strip()
            if not media_url:
                errors.append(f"{media_id}: Meta did not provide a media URL.")
                continue

            try:
                filename = download_instagram_media(media_url, media_type, UPLOAD_DIR)
            except InstagramSyncError as error:
                errors.append(f"{media_id}: {error}")
                continue

            media_kind = "video" if media_type == "VIDEO" else "image"
            if media_kind == "image":
                try:
                    create_webp_variant(os.path.join(UPLOAD_DIR, filename))
                except OSError as error:
                    logger.exception("Could not create WebP variant for Instagram image %s", media_id)
                    errors.append(f"{media_id}: imported original image, but WebP optimization failed: {error}")
            caption = str(post.get("caption", "")).strip()
            title = next(
                (line.strip() for line in caption.splitlines() if line.strip()),
                f"Instagram {media_kind.title()}"
            )[:120]
            new_items.append({
                "id": f"instagram_{uuid.uuid4().hex[:16]}",
                "title": title,
                "category": "Instagram",
                "type": media_kind,
                "url": f"{MEDIA_URL_PREFIX}/{filename}",
                "badge": "Instagram",
                "description": caption[:500],
                "colors": "",
                "availability": "Available",
                "price_label": "",
                "featured": False,
                "instagram_media_id": media_id,
                "instagram_permalink": str(post.get("permalink", "")),
                "instagram_timestamp": str(post.get("timestamp", ""))
            })
            imported_ids.add(media_id)

        if new_items:
            items = list(reversed(new_items)) + items
            try:
                with open(COLLECTION_FILE, "w", encoding="utf-8") as collection_file:
                    json.dump(items, collection_file, indent=4, ensure_ascii=False)
            except OSError as error:
                for item in new_items:
                    filename = item["url"].rsplit("/", 1)[-1]
                    filepath = os.path.join(UPLOAD_DIR, filename)
                    if os.path.exists(filepath):
                        os.remove(filepath)
                raise InstagramSyncError(f"Could not save imported Instagram media: {error}") from None

            remember_category("Instagram")
            for item in reversed(new_items):
                append_history(
                    "instagram import",
                    item["title"],
                    item["type"],
                    "instagram",
                    item["instagram_permalink"]
                )

        result = {
            "status": "partial" if errors else "success",
            "last_sync_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "imported": len(new_items),
            "skipped": skipped,
            "message": (
                f"Imported {len(new_items)} Instagram item(s)."
                if not errors else
                f"Imported {len(new_items)} item(s); {len(errors)} media item(s) need attention."
            ),
            "errors": errors[:10]
        }
        save_instagram_sync_status(result)
        if errors:
            logger.warning("Instagram sync completed with %s media error(s).", len(errors))
        return {**result, "configured": True}
    except InstagramSyncError as error:
        logger.error("Instagram sync failed: %s", error)
        result = {
            "status": "error",
            "last_sync_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "imported": 0,
            "skipped": 0,
            "message": str(error),
            "errors": []
        }
        save_instagram_sync_status(result)
        return {**result, "configured": instagram_is_configured()}
    except OSError as error:
        logger.exception("Instagram sync could not access local storage.")
        result = {
            "status": "error",
            "last_sync_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "imported": 0,
            "skipped": 0,
            "message": f"Could not access local storage: {error}",
            "errors": []
        }
        save_instagram_sync_status(result)
        return {**result, "configured": instagram_is_configured()}
    finally:
        INSTAGRAM_SYNC_LOCK.release()


async def instagram_sync_loop():
    while True:
        result = await asyncio.to_thread(sync_instagram_media)
        if result["status"] in {"error", "partial"}:
            logger.warning("Instagram auto-sync status: %s", result["message"])
        await asyncio.sleep(INSTAGRAM_SYNC_INTERVAL_SECONDS)


def append_history(action: str, item_title: str = "", item_type: str = "", admin: str = "admin", details: str = ""):
    history = get_history()
    entry = {
        "id": f"hist_{int(datetime.now().timestamp())}_{len(history)}",
        "action": action,
        "item_title": item_title,
        "item_type": item_type,
        "admin": admin,
        "details": details,
        "timestamp": datetime.now().strftime("%d-%m-%Y %I:%M %p")
    }
    history.insert(0, entry)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=4, ensure_ascii=False)
    return entry


def is_admin_authenticated(request: Request):
    session_token = request.cookies.get("admin_session")
    return bool(session_token and session_token in ACTIVE_ADMIN_SESSIONS)


@app.get("/", response_class=HTMLResponse)
async def serve_home(request: Request):
    items = get_collection()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "items": items,
        "category_label_colors": get_category_label_colors({
            item.get("category", "") for item in items
        }),
        "hero_item": get_hero_media(items),
        "hero_companion_media": get_hero_companion_media(items),
        "logo_url": get_logo_url(),
        "design": get_design(),
        "business": get_business(),
        "site_url": get_public_site_url(request)
    })


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt(request: Request):
    site_url = get_public_site_url(request)
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /api/\n"
        f"Sitemap: {site_url}/sitemap.xml\n"
    )


@app.get("/sitemap.xml")
async def sitemap_xml(request: Request):
    namespace = "http://www.sitemaps.org/schemas/sitemap/0.9"
    urlset = ET.Element("urlset", {"xmlns": namespace})
    url = ET.SubElement(urlset, f"{{{namespace}}}url")
    ET.SubElement(url, f"{{{namespace}}}loc").text = f"{get_public_site_url(request)}/"
    return Response(
        content=ET.tostring(urlset, encoding="utf-8", xml_declaration=True),
        media_type="application/xml"
    )


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "persistent_storage": PERSISTENT_DATA_DIR != ".",
        "media_route": MEDIA_URL_PREFIX
    }


@app.post("/api/inquiry")
async def receive_inquiry(
    name: str = Form(...),
    phone: str = Form(...),
    branch: str = Form("Bhavnagar (Main Branch)"),
    event_date: str = Form(""),
    service_type: str = Form(...),
    note: str = Form("")
):
    record = {
        "id": f"inq_{int(datetime.now().timestamp())}",
        "name": name,
        "phone": phone,
        "branch": branch,
        "event_date": event_date,
        "service_type": service_type,
        "note": note,
        "status": "New",
        "submitted_at": datetime.now().strftime("%d-%m-%Y %I:%M %p")
    }
    inquiries = get_inquiries()
    inquiries.insert(0, record)
    with open(LEADS_FILE, "w", encoding="utf-8") as f:
        json.dump(inquiries, f, indent=4, ensure_ascii=False)
    return JSONResponse(status_code=200, content={"status": "success"})


@app.post("/api/admin/inquiries/{inquiry_id}/status")
async def update_inquiry_status(
    inquiry_id: str,
    request: Request,
    inquiry_status: str = Form(...)
):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")
    allowed_statuses = {"New", "Contacted", "Confirmed", "Completed", "Cancelled"}
    if inquiry_status not in allowed_statuses:
        raise HTTPException(status_code=400, detail="Invalid inquiry status")

    inquiries = get_inquiries()
    inquiry = next((entry for entry in inquiries if entry.get("id") == inquiry_id), None)
    if inquiry is None:
        raise HTTPException(status_code=404, detail="Inquiry not found")
    old_status = inquiry.get("status", "New")
    inquiry["status"] = inquiry_status
    with open(LEADS_FILE, "w", encoding="utf-8") as f:
        json.dump(inquiries, f, indent=4, ensure_ascii=False)
    append_history(
        "inquiry status update",
        inquiry.get("name", "Customer"),
        "inquiry",
        "admin",
        f"{old_status} -> {inquiry_status}"
    )
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)

# --- ADMIN PANEL ROUTES ---


@app.get("/admin", response_class=HTMLResponse)
async def serve_admin(request: Request, error: str = ""):
    is_authenticated = is_admin_authenticated(request)

    items = get_collection() if is_authenticated else []
    hero_media = get_hero_media(items) if is_authenticated else None
    hero_companion_media = get_hero_companion_media(items) if is_authenticated else None
    inquiries = get_inquiries() if is_authenticated else []
    orders = get_orders() if is_authenticated else []
    history = get_history() if is_authenticated else []
    categories = get_categories() if is_authenticated else []
    category_color_categories = list(dict.fromkeys(
        categories + [
            item.get("category", "")
            for item in items
            if item.get("category")
        ]
    ))
    logo_url = get_logo_url() if is_authenticated else DEFAULT_LOGO_URL
    design = get_design() if is_authenticated else DEFAULT_DESIGN.copy()
    admin_label_colors = get_admin_label_colors() if is_authenticated else {
        **DEFAULT_ADMIN_LABEL_COLORS,
        "category_text": contrast_text_color(DEFAULT_ADMIN_LABEL_COLORS["category"]),
        "badge_text": contrast_text_color(DEFAULT_ADMIN_LABEL_COLORS["badge"]),
        "branch_text": contrast_text_color(DEFAULT_ADMIN_LABEL_COLORS["branch"])
    }
    business = get_business() if is_authenticated else DEFAULT_BUSINESS.copy()
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "is_authenticated": is_authenticated,
        "items": items,
        "hero_photo_options": get_homepage_image_options(items) if is_authenticated else [],
        "hero_media": hero_media,
        "hero_media_id": (hero_media or {}).get("id", ""),
        "hero_companion_media": hero_companion_media,
        "hero_companion_media_id": (hero_companion_media or {}).get("id", ""),
        "hero_updated": request.query_params.get("hero") == "updated",
        "inquiries": inquiries,
        "orders": orders,
        "history": history,
        "logo_url": logo_url,
        "design": design,
        "admin_label_colors": admin_label_colors,
        "category_color_categories": category_color_categories,
        "category_label_colors": get_category_label_colors(category_color_categories),
        "business": business,
        "categories": categories,
        "error": error,
        "instagram_sync": get_instagram_sync_status() if is_authenticated else {}
    })


def select_homepage_photo(photo_url, config_path):
    photo = next(
        (
            option for option in get_homepage_image_options(get_collection())
            if option["url"] == photo_url
        ),
        None
    )
    if not photo:
        raise HTTPException(status_code=400, detail="Choose an available uploaded photo.")
    save_hero_config({"url": photo["url"], "title": photo["title"]}, config_path)
    return photo


@app.post("/api/admin/hero-media")
async def admin_update_hero_media(request: Request, photo_url: str = Form(...)):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")

    photo = select_homepage_photo(photo_url, HERO_CONFIG_FILE)
    append_history("hero photo update", photo["title"], "image", "admin", photo_url)
    return RedirectResponse(url="/admin?hero=updated", status_code=status.HTTP_303_SEE_OTHER)


def save_homepage_photo_upload(file: UploadFile, config_path, title, filename_prefix):
    extension = os.path.splitext(file.filename or "")[1].lower()
    if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(status_code=400, detail="Homepage photo must be JPG, PNG, or WEBP.")
    signature = file.file.read(12)
    file.file.seek(0)
    valid_signature = (
        (extension in {".jpg", ".jpeg"} and signature.startswith(b"\xff\xd8\xff"))
        or (extension == ".png" and signature.startswith(b"\x89PNG\r\n\x1a\n"))
        or (
            extension == ".webp"
            and signature.startswith(b"RIFF")
            and signature[8:12] == b"WEBP"
        )
    )
    if not valid_signature:
        raise HTTPException(status_code=400, detail="The selected file does not match its image format.")

    filename = f"{filename_prefix}_{uuid.uuid4().hex}{extension}"
    filepath = os.path.join(UPLOAD_DIR, filename)
    save_upload(file, filepath, MAX_IMAGE_BYTES, "Homepage photo")
    hero_url = f"{MEDIA_URL_PREFIX}/{filename}"
    try:
        save_hero_config({"url": hero_url, "title": title}, config_path)
    except HTTPException:
        for failed_path in (filepath, os.path.splitext(filepath)[0] + ".webp"):
            if os.path.exists(failed_path):
                os.remove(failed_path)
        raise
    return hero_url


@app.post("/api/admin/hero-media/upload")
async def admin_upload_hero_media(request: Request, file: UploadFile = File(...)):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")

    hero_url = await asyncio.to_thread(
        save_homepage_photo_upload,
        file,
        HERO_CONFIG_FILE,
        "Homepage Royal Photo",
        "hero"
    )
    append_history("hero photo upload", "Homepage Royal Photo", "image", "admin", hero_url)
    return RedirectResponse(url="/admin?hero=updated", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/admin/hero-companion-media")
async def admin_update_hero_companion_media(request: Request, photo_url: str = Form(...)):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")

    photo = select_homepage_photo(photo_url, HERO_COMPANION_CONFIG_FILE)
    append_history(
        "hero companion photo update",
        photo["title"],
        "image",
        "admin",
        photo_url
    )
    return RedirectResponse(url="/admin?hero=updated", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/admin/hero-companion-media/upload")
async def admin_upload_hero_companion_media(request: Request, file: UploadFile = File(...)):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")

    hero_url = await asyncio.to_thread(
        save_homepage_photo_upload,
        file,
        HERO_COMPANION_CONFIG_FILE,
        "Homepage Companion Photo",
        "hero_companion"
    )
    append_history(
        "hero companion photo upload",
        "Homepage Companion Photo",
        "image",
        "admin",
        hero_url
    )
    return RedirectResponse(url="/admin?hero=updated", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/admin/instagram-sync")
async def admin_sync_instagram(request: Request):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")
    await asyncio.to_thread(sync_instagram_media)
    return RedirectResponse(
        url="/admin",
        status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/api/admin/orders/create")
async def create_order(
    request: Request,
    customer_name: str = Form(...),
    phone: str = Form(...),
    branch: str = Form("Bhavnagar (Main Branch)"),
    event_date: str = Form(""),
    pickup_date: str = Form(""),
    return_date: str = Form(""),
    event_type: str = Form("Wedding"),
    product_name: List[str] = Form([]),
    quantity: List[str] = Form([]),
    item_price: List[str] = Form([]),
    total_amount: str = Form("0"),
    advance_paid: str = Form("0"),
    deposit_amount: str = Form("0"),
    payment_method: str = Form("Cash"),
    payment_status: str = Form("Unpaid"),
    notes: str = Form("")
):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")
    cleaned_phone = re.sub(r"\D", "", phone)
    if len(cleaned_phone) < 10:
        raise HTTPException(status_code=400, detail="Enter a valid customer phone number")
    def money(value: str) -> float:
        try:
            return max(0, float(value or 0))
        except ValueError:
            return 0

    products = []
    for index, name in enumerate(product_name):
        if name.strip():
            try:
                item_quantity = max(1, int(quantity[index] or 1)) if index < len(quantity) else 1
            except ValueError:
                item_quantity = 1
            products.append({
                "name": name.strip()[:120],
                "quantity": item_quantity,
                "price": money(item_price[index]) if index < len(item_price) else 0
            })
    orders = get_orders()
    order_id = f"ORD-{datetime.now().strftime('%Y%m%d')}-{len(orders) + 1:04d}"
    order = {
        "id": order_id,
        "customer_name": customer_name.strip()[:100],
        "phone": cleaned_phone,
        "branch": branch.strip()[:80],
        "event_date": event_date,
        "pickup_date": pickup_date,
        "return_date": return_date,
        "event_type": event_type,
        "products": products,
        "total_amount": money(total_amount),
        "advance_paid": money(advance_paid),
        "balance_amount": max(0, money(total_amount) - money(advance_paid)),
        "deposit_amount": money(deposit_amount),
        "payment_method": payment_method,
        "payment_status": payment_status,
        "status": "New",
        "notes": notes.strip()[:500],
        "created_at": datetime.now().strftime("%d-%m-%Y %I:%M %p")
    }
    orders.insert(0, order)
    with open(ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(orders, f, indent=4, ensure_ascii=False)
    append_history("order created", order["id"], "order", "admin", f"{order['customer_name']} | {len(products)} product(s)")
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/admin/orders/{order_id}/status")
async def update_order_status(order_id: str, request: Request, order_status: str = Form(...)):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")
    allowed = {"New", "Contacted", "Quotation Sent", "Advance Pending", "Confirmed", "Preparing",
               "Ready for Pickup", "Out for Delivery", "Delivered", "Returned", "Completed", "Cancelled"}
    if order_status not in allowed:
        raise HTTPException(status_code=400, detail="Invalid order status")
    orders = get_orders()
    order = next((entry for entry in orders if entry.get("id") == order_id), None)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    old_status = order.get("status", "New")
    order["status"] = order_status
    with open(ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(orders, f, indent=4, ensure_ascii=False)
    append_history("order status update", order_id, "order", "admin", f"{old_status} -> {order_status}")
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/login")
async def admin_login(request: Request, password: str = Form(...)):
    if not hmac.compare_digest(password, ADMIN_PASSWORD):
        append_history("login failed", "Admin portal", "auth", "admin", "Invalid password")
        return RedirectResponse(url="/admin?error=invalid", status_code=status.HTTP_303_SEE_OTHER)

    session_token = secrets.token_urlsafe(32)
    ACTIVE_ADMIN_SESSIONS.add(session_token)
    response = RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key="admin_session",
        value=session_token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=86400
    )
    append_history("login", "", "", "admin", "Admin logged in successfully")
    return response


@app.post("/api/admin/upload-logo")
async def admin_upload_logo(request: Request, file: UploadFile = File(...)):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")

    extension = os.path.splitext(file.filename or "")[1].lower()
    if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(status_code=400, detail="Logo must be JPG, PNG, or WEBP")

    old_logo_url = get_logo_url()
    filename = f"logo_{uuid.uuid4().hex}{extension}"
    filepath = os.path.join(UPLOAD_DIR, filename)
    save_upload(file, filepath, MAX_IMAGE_BYTES, "Logo image")

    logo_url = f"{MEDIA_URL_PREFIX}/{filename}"
    with open(LOGO_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"url": logo_url}, f, indent=4)

    if old_logo_url.startswith(f"{MEDIA_URL_PREFIX}/"):
        old_logo_path = os.path.join(UPLOAD_DIR, old_logo_url.split(f"{MEDIA_URL_PREFIX}/", 1)[1])
        if os.path.exists(old_logo_path) and old_logo_path != filepath:
            os.remove(old_logo_path)

    append_history("logo update", "Site logo", "image", "admin", f"Replaced {old_logo_url}")
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/admin/design")
async def admin_update_design(
    request: Request,
    primary_color: str = Form(...),
    accent_color: str = Form(...),
    background_color: str = Form(...)
):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")

    design = {
        "primary_color": primary_color.upper(),
        "accent_color": accent_color.upper(),
        "background_color": background_color.upper()
    }
    if not all(re.fullmatch(r"#[0-9A-F]{6}", value) for value in design.values()):
        raise HTTPException(status_code=400, detail="Colors must use six-digit HEX values")

    with open(DESIGN_FILE, "w", encoding="utf-8") as f:
        json.dump(design, f, indent=4)
    append_history("design update", "Live website theme", "settings", "admin", "Primary, accent, and background colors updated")
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/admin/label-colors")
async def admin_update_label_colors(
    request: Request,
    category_color: str = Form(...),
    badge_color: str = Form(...),
    product_title_color: str = Form(...),
    branch_color: str = Form(...)
):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")

    colors = {
        "category": category_color.upper(),
        "badge": badge_color.upper(),
        "product_title": product_title_color.upper(),
        "branch": branch_color.upper()
    }
    if not all(re.fullmatch(r"#[0-9A-F]{6}", value) for value in colors.values()):
        raise HTTPException(status_code=400, detail="Colors must use six-digit HEX values")
    colors["categories"] = get_admin_label_colors()["category_overrides"]

    temporary_path = f"{ADMIN_LABEL_COLORS_FILE}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary_path, "w", encoding="utf-8") as color_file:
            json.dump(colors, color_file, indent=4)
        os.replace(temporary_path, ADMIN_LABEL_COLORS_FILE)
    except OSError as error:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
        logger.exception("Could not save Admin label colors.")
        raise HTTPException(status_code=500, detail="Could not save Admin label colors.") from error

    append_history(
        "admin label colors update",
        "Admin labels",
        "settings",
        "admin",
        "Category, badge, product title, and branch colors updated"
    )
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/admin/category-label-colors")
async def admin_update_category_label_colors(request: Request):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")

    form = await request.form()
    categories = form.getlist("category")
    color_values = form.getlist("color")
    allowed_categories = set(get_categories())
    allowed_categories.update(
        item.get("category", "")
        for item in get_collection()
        if item.get("category")
    )
    if (
        not categories
        or len(categories) != len(color_values)
        or len(set(categories)) != len(categories)
        or any(category not in allowed_categories for category in categories)
    ):
        raise HTTPException(status_code=400, detail="Submit one color for each current category.")

    if not all(
        isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value)
        for value in color_values
    ):
        raise HTTPException(status_code=400, detail="Colors must use six-digit HEX values.")

    saved_colors = get_admin_label_colors()
    config = {
        "category": saved_colors["category"],
        "badge": saved_colors["badge"],
        "product_title": saved_colors["product_title"],
        "branch": saved_colors["branch"],
        "categories": {
            **saved_colors["category_overrides"],
            **{
                category: color.upper()
                for category, color in zip(categories, color_values)
            }
        }
    }
    temporary_path = f"{ADMIN_LABEL_COLORS_FILE}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary_path, "w", encoding="utf-8") as color_file:
            json.dump(config, color_file, indent=4)
        os.replace(temporary_path, ADMIN_LABEL_COLORS_FILE)
    except OSError as error:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
        logger.exception("Could not save Admin category label colors.")
        raise HTTPException(status_code=500, detail="Could not save category colors.") from error

    append_history(
        "category label colors update",
        "Media categories",
        "settings",
        "admin",
        f"Updated colors for {len(categories)} categories"
    )
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/admin/business")
async def update_business(
    request: Request,
    phone: str = Form(...),
    instagram: str = Form(...),
    headline: str = Form(...),
    announcement: str = Form(...),
    timings: str = Form(...)
):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")

    cleaned_phone = re.sub(r"\D", "", phone)
    if len(cleaned_phone) < 10:
        raise HTTPException(status_code=400, detail="Enter a valid phone number")
    handle = instagram.strip().lstrip("@").replace(" ", "")
    if not handle:
        raise HTTPException(status_code=400, detail="Instagram handle is required")

    business = {
        "phone": cleaned_phone,
        "instagram": handle,
        "headline": headline.strip()[:120],
        "announcement": announcement.strip()[:240],
        "timings": timings.strip()[:120]
    }
    with open(BUSINESS_FILE, "w", encoding="utf-8") as f:
        json.dump(business, f, indent=4, ensure_ascii=False)
    append_history("business update", "Business settings", "settings", "admin", "Phone, Instagram, headline and timings updated")
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/logout")
async def admin_logout(request: Request):
    session_token = request.cookies.get("admin_session")
    if session_token:
        ACTIVE_ADMIN_SESSIONS.discard(session_token)
        append_history("logout", "Admin portal", "auth", "admin", "Admin session ended")
    response = RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key="admin_session")
    return response


@app.post("/api/admin/upload")
async def admin_upload_media(
    request: Request,
    title: str = Form(...),
    category: str = Form(...),
    media_type: str = Form(...),
    badge: str = Form("New Arrival"),
    description: str = Form(""),
    colors: str = Form(""),
    occasions: str = Form(""),
    availability: str = Form("Available"),
    price_label: str = Form(""),
    featured: str = Form("false"),
    files: list[UploadFile] | None = File(None),
    item_images: list[UploadFile] | None = File(None)
):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")

    uploaded_files = [upload for upload in (files or []) if upload.filename]
    gallery_images = [upload for upload in (item_images or []) if upload.filename]
    if not uploaded_files and not gallery_images:
        raise HTTPException(status_code=400, detail="Select at least one media file")
    category = category.strip()
    if not category:
        raise HTTPException(status_code=400, detail="Category is required")
    if len(occasions) > 160:
        raise HTTPException(status_code=400, detail="Occasion tags must be 160 characters or fewer")
    remember_category(category)

    items = get_collection()

    if gallery_images:
        gallery = []
        for upload in gallery_images:
            extension = os.path.splitext(upload.filename or "")[1].lower()
            if not (upload.content_type or "").startswith("image/"):
                raise HTTPException(status_code=400, detail="Gallery files must be images")
            filename = f"{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:6]}{extension}"
            filepath = os.path.join(UPLOAD_DIR, filename)
            save_upload(upload, filepath, MAX_IMAGE_BYTES, "Gallery image")
            gallery.append({
                "url": f"{MEDIA_URL_PREFIX}/{filename}",
                "alt": f"{title} view {len(gallery) + 1}"
            })

        items.insert(0, {
            "id": f"item_{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:6]}",
            "title": title,
            "category": category,
            "type": "image",
            "url": gallery[0]["url"],
            "images": gallery,
            "badge": badge,
            "description": description.strip(),
            "colors": colors.strip().lower(),
            "occasions": occasions.strip() or "Wedding",
            "availability": availability.strip(),
            "price_label": price_label.strip(),
            "featured": featured == "true"
        })

    for upload in uploaded_files:
        extension = os.path.splitext(upload.filename or "")[1].lower()
        is_video = (upload.content_type or "").startswith("video/") or media_type == "video"
        max_bytes = MAX_VIDEO_BYTES if is_video else MAX_IMAGE_BYTES
        media_label = "Video" if is_video else "Image"
        filename = f"{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:6]}{extension}"
        filepath = os.path.join(UPLOAD_DIR, filename)
        save_upload(upload, filepath, max_bytes, media_label)

        new_media = {
            "id": f"item_{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:6]}",
            "title": title,
            "category": category,
            "type": media_type,
            "url": f"{MEDIA_URL_PREFIX}/{filename}",
            "badge": badge,
            "description": description.strip(),
            "colors": colors.strip().lower(),
            "occasions": occasions.strip() or "Wedding",
            "availability": availability.strip(),
            "price_label": price_label.strip(),
            "featured": featured == "true"
        }
        items.insert(0, new_media)

    with open(COLLECTION_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=4, ensure_ascii=False)

    append_history(
        "upload",
        title,
        "image gallery" if gallery_images else media_type,
        "admin",
        f"Category: {category} | Badge: {badge} | Files: {len(uploaded_files)} | Gallery images: {len(gallery_images)}"
    )

    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/admin/categories/add")
async def admin_add_category(request: Request, category: str = Form(...)):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")
    category = category.strip()
    if not category or len(category) > 60:
        raise HTTPException(status_code=400, detail="Enter a category between 1 and 60 characters")
    if category.casefold() in {item.casefold() for item in get_categories()}:
        raise HTTPException(status_code=400, detail="Category already exists")
    save_categories(get_categories() + [category])
    append_history("category added", category, "category", "admin", "Added from Admin category manager")
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/admin/categories/delete")
async def admin_delete_category(request: Request, category: str = Form(...)):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")
    category = category.strip()
    categories = get_categories()
    if category not in categories:
        raise HTTPException(status_code=404, detail="Category not found")
    if len(categories) <= 1:
        raise HTTPException(status_code=400, detail="At least one category must remain")
    save_categories([item for item in categories if item != category])
    append_history("category deleted", category, "category", "admin", "Deleted from Admin category manager")
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/admin/update-item")
async def admin_update_media(
    request: Request,
    item_id: str = Form(...),
    title: str = Form(...),
    category: str = Form(...),
    badge: str = Form("Royal Look"),
    description: str = Form(""),
    colors: str = Form(""),
    occasions: str = Form(""),
    availability: str = Form("Available"),
    price_label: str = Form(""),
    featured: str = Form("false")
):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")
    category = category.strip()
    if not category:
        raise HTTPException(status_code=400, detail="Category is required")
    if len(occasions) > 160:
        raise HTTPException(status_code=400, detail="Occasion tags must be 160 characters or fewer")
    remember_category(category)

    items = get_collection()
    item = next((entry for entry in items if entry.get("id") == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Media item not found")

    item.update({
        "title": title.strip(),
        "category": category,
        "badge": badge.strip(),
        "description": description.strip(),
        "colors": colors.strip().lower(),
        "occasions": occasions.strip() or "Wedding",
        "availability": availability.strip(),
        "price_label": price_label.strip(),
        "featured": featured == "true"
    })
    with open(COLLECTION_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=4, ensure_ascii=False)

    append_history("update", item["title"], item.get("type", "media"), "admin", f"Updated {item_id}")
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/add-item")
async def add_item(
    request: Request,
    title: str = Form(...),
    category: str = Form(...),
    item_images: list[UploadFile] = File(...)
):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")
    category = category.strip()
    if not category:
        raise HTTPException(status_code=400, detail="Category is required")
    remember_category(category)

    gallery = []
    for image in item_images:
        if not image.filename:
            continue
        if not (image.content_type or "").startswith("image/"):
            raise HTTPException(status_code=400, detail="Only image files are allowed")

        extension = os.path.splitext(image.filename or "")[1].lower()
        filename = f"{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:6]}{extension}"
        filepath = os.path.join(UPLOAD_DIR, filename)
        save_upload(image, filepath, MAX_IMAGE_BYTES, "Gallery image")
        gallery.append({
            "url": f"{MEDIA_URL_PREFIX}/{filename}",
            "alt": f"{title} view {len(gallery) + 1}"
        })

    if not gallery:
        raise HTTPException(status_code=400, detail="Select at least one image")

    items = get_collection()
    item = {
        "id": f"item_{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:6]}",
        "title": title.strip(),
        "category": category,
        "type": "image",
        "url": gallery[0]["url"],
        "images": gallery,
        "badge": "Royal Look"
    }
    items.insert(0, item)
    with open(COLLECTION_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=4, ensure_ascii=False)

    append_history("upload", item["title"], "image gallery", "admin", f"Category: {item['category']} | Images: {len(gallery)}")
    return JSONResponse(status_code=200, content={"status": "success", "images": [image["url"] for image in gallery]})


@app.post("/api/admin/delete-item")
async def admin_delete_media(request: Request, item_id: str = Form(...)):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")

    items = get_collection()
    item_to_remove = next((item for item in items if item["id"] == item_id), None)
    remaining_items = [item for item in items if item["id"] != item_id]
    if item_to_remove:
        file_urls = [item_to_remove.get("url", "")]
        file_urls.extend(
            image.get("url", "") for image in item_to_remove.get("images", [])
            if isinstance(image, dict)
        )
        locked_files = []
        for file_url in file_urls:
            if file_url.startswith("/static/"):
                file_path = os.path.join("static", file_url.split("/static/", 1)[1])
            elif file_url.startswith(f"{MEDIA_URL_PREFIX}/"):
                file_path = os.path.join(UPLOAD_DIR, file_url.split(f"{MEDIA_URL_PREFIX}/", 1)[1])
            else:
                continue
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except PermissionError:
                    locked_files.append(os.path.basename(file_path))
                except OSError as error:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Could not delete media file: {error}"
                    ) from error
        cleanup_note = (
            f"Removed item from {item_to_remove.get('category', 'catalog')}"
            if not locked_files else
            f"Removed from catalogue; file still open and could not be deleted: {', '.join(locked_files)}"
        )
        append_history("delete", item_to_remove.get("title", "Unknown item"), item_to_remove.get("type", "unknown"), "admin", cleanup_note)

    with open(COLLECTION_FILE, "w", encoding="utf-8") as f:
        json.dump(remaining_items, f, indent=4, ensure_ascii=False)

    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
