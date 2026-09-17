from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException, status, Response
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

# Ensure required directories exist
os.makedirs("templates", exist_ok=True)
os.makedirs("static/uploads", exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

LEADS_FILE = "inquiries.json"
COLLECTION_FILE = "collection.json"
HISTORY_FILE = "admin_history.json"
LOGO_CONFIG_FILE = "logo_config.json"
DESIGN_FILE = "design.json"
DEFAULT_LOGO_URL = "/static/logo.svg"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "S@vani6055")
ACTIVE_ADMIN_SESSIONS = set()

DEFAULT_DESIGN = {
    "primary_color": "#54121E",
    "accent_color": "#C5A059",
    "background_color": "#FAF7F2"
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
        with open(COLLECTION_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_COLLECTION, f, indent=4, ensure_ascii=False)
        return DEFAULT_COLLECTION
    try:
        with open(COLLECTION_FILE, "r", encoding="utf-8") as f:
            collection = json.load(f)
            return [item for item in collection if media_file_exists(item)]
    except Exception:
        return DEFAULT_COLLECTION


def media_file_exists(item):
    url = item.get("url", "")
    if not url.startswith("/static/"):
        return True
    relative_path = url.split("/static/", 1)[1]
    return os.path.isfile(os.path.join("static", relative_path))


def get_inquiries():
    if os.path.exists(LEADS_FILE):
        try:
            with open(LEADS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def get_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def get_logo_url():
    if os.path.exists(LOGO_CONFIG_FILE):
        try:
            with open(LOGO_CONFIG_FILE, "r", encoding="utf-8") as f:
                logo_url = json.load(f).get("url", "")
            logo_path = logo_url.removeprefix("/static/")
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
        "design": get_design()
    })


@app.get("/health")
async def health_check():
    return {"status": "ok"}


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
        "submitted_at": datetime.now().strftime("%d-%m-%Y %I:%M %p")
    }
    inquiries = get_inquiries()
    inquiries.insert(0, record)
    with open(LEADS_FILE, "w", encoding="utf-8") as f:
        json.dump(inquiries, f, indent=4, ensure_ascii=False)
    return JSONResponse(status_code=200, content={"status": "success"})

# --- ADMIN PANEL ROUTES ---


@app.get("/admin", response_class=HTMLResponse)
async def serve_admin(request: Request, error: str = ""):
    is_authenticated = is_admin_authenticated(request)

    items = get_collection() if is_authenticated else []
    inquiries = get_inquiries() if is_authenticated else []
    history = get_history() if is_authenticated else []
    logo_url = get_logo_url() if is_authenticated else DEFAULT_LOGO_URL
    design = get_design() if is_authenticated else DEFAULT_DESIGN.copy()
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "is_authenticated": is_authenticated,
        "items": items,
        "inquiries": inquiries,
        "history": history,
        "logo_url": logo_url,
        "design": design,
        "error": error
    })


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
    filepath = os.path.join("static", filename)
    with open(filepath, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    logo_url = f"/static/{filename}"
    with open(LOGO_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"url": logo_url}, f, indent=4)

    if old_logo_url.startswith("/static/logo_"):
        old_logo_path = os.path.join("static", old_logo_url.split("/static/", 1)[1])
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
    featured: str = Form("false"),
    file: UploadFile = File(...)
):
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Unauthorized")

    filename = f"{int(datetime.now().timestamp())}_{file.filename.replace(' ', '_')}"
    filepath = os.path.join("static/uploads", filename)
    with open(filepath, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    new_media = {
        "id": f"item_{int(datetime.now().timestamp())}",
        "title": title,
        "category": category,
        "type": media_type,
        "url": f"/static/uploads/{filename}",
        "badge": badge,
        "description": description.strip(),
        "featured": featured == "true"
    }

    items = get_collection()
    items.insert(0, new_media)
    with open(COLLECTION_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=4, ensure_ascii=False)

    append_history("upload", title, media_type, "admin", f"Category: {category} | Badge: {badge}")

    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/admin/update-item")
async def admin_update_media(
    request: Request,
    item_id: str = Form(...),
    title: str = Form(...),
    category: str = Form(...),
    badge: str = Form("Royal Look"),
    description: str = Form(""),
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
        "featured": featured == "true"
    })
    with open(COLLECTION_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=4, ensure_ascii=False)

    append_history("update", item["title"], item.get("type", "media"), "admin", f"Updated {item_id}")
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


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
