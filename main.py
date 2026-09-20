from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException, status, Response
from typing import List
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(
    title="Shivbhavani Safa Paghdi",
    description="Developed by Hit Savani",
    version="2.0.0"
)

RENDER_DISK_DIR = "/opt/render/project/src/persistent"
configured_data_dir = os.getenv("PERSISTENT_DATA_DIR", "").strip()
PERSISTENT_DATA_DIR = configured_data_dir or (RENDER_DISK_DIR if os.path.isdir(RENDER_DISK_DIR) else ".")
UPLOAD_DIR = os.path.join(PERSISTENT_DATA_DIR, "uploads") if PERSISTENT_DATA_DIR != "." else os.path.join("static", "uploads")
MEDIA_URL_PREFIX = "/media" if PERSISTENT_DATA_DIR != "." else "/static/uploads"
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
        "admin_history.json", "inquiries.json", "orders.json", "logo_config.json"
    ):
        source = data_filename
        target = os.path.join(PERSISTENT_DATA_DIR, data_filename)
        if os.path.isfile(source) and not os.path.exists(target):
            shutil.copy2(source, target)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/media", StaticFiles(directory=UPLOAD_DIR), name="media")
templates = Jinja2Templates(directory="templates")

def persistent_file(filename):
    return os.path.join(PERSISTENT_DATA_DIR, filename) if PERSISTENT_DATA_DIR != "." else filename


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
BUSINESS_FILE = persistent_file("business.json")
DEFAULT_LOGO_URL = "/static/logo.svg"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "S@vani6055")
ACTIVE_ADMIN_SESSIONS = set()

DEFAULT_DESIGN = {
    "primary_color": "#5B0E1E",
    "accent_color": "#C5A059",
    "background_color": "#FBF8F1"
}

DEFAULT_BUSINESS = {
    "phone": "919925245246",
    "instagram": "shivbhavani_safa_bhavnagar",
    "headline": "Royal Elegance. Authentic Heritage.",
    "announcement": "Custom wedding turbans and royal groom accessories in Bhavnagar and Surat.",
    "timings": "Open daily by appointment"
}

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


def get_business():
    if os.path.exists(BUSINESS_FILE):
        try:
            with open(BUSINESS_FILE, "r", encoding="utf-8") as f:
                return {**DEFAULT_BUSINESS, **json.load(f)}
        except Exception:
            pass
    return DEFAULT_BUSINESS.copy()


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
        "logo_url": get_logo_url(),
        "design": get_design(),
        "business": get_business()
    })


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
    inquiries = get_inquiries() if is_authenticated else []
    orders = get_orders() if is_authenticated else []
    history = get_history() if is_authenticated else []
    logo_url = get_logo_url() if is_authenticated else DEFAULT_LOGO_URL
    design = get_design() if is_authenticated else DEFAULT_DESIGN.copy()
    business = get_business() if is_authenticated else DEFAULT_BUSINESS.copy()
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "is_authenticated": is_authenticated,
        "items": items,
        "inquiries": inquiries,
        "orders": orders,
        "history": history,
        "logo_url": logo_url,
        "design": design,
        "business": business,
        "error": error
    })


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


@app.post("/api/admin/update-item")
async def admin_update_media(
    request: Request,
    item_id: str = Form(...),
    title: str = Form(...),
    category: str = Form(...),
    badge: str = Form("Royal Look"),
    description: str = Form(""),
    colors: str = Form(""),
    availability: str = Form("Available"),
    price_label: str = Form(""),
    featured: str = Form("false")
):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")

    items = get_collection()
    item = next((entry for entry in items if entry.get("id") == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Media item not found")

    item.update({
        "title": title.strip(),
        "category": category.strip(),
        "badge": badge.strip(),
        "description": description.strip(),
        "colors": colors.strip().lower(),
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
        "category": category.strip(),
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
        file_url = item_to_remove.get("url", "")
        if file_url.startswith("/static/"):
            file_path = os.path.join("static", file_url.split("/static/", 1)[1])
            if os.path.exists(file_path):
                os.remove(file_path)
        append_history("delete", item_to_remove.get("title", "Unknown item"), item_to_remove.get("type", "unknown"), "admin", f"Removed item from {item_to_remove.get('category', 'catalog')}")

    with open(COLLECTION_FILE, "w", encoding="utf-8") as f:
        json.dump(remaining_items, f, indent=4, ensure_ascii=False)

    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
