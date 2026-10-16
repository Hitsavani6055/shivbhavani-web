# Copilot instructions

## Build and run

- Install the pinned dependencies with `pip install -r requirements.txt`.
- Run locally with `uvicorn main:app --reload` (or `python main.py`); the local entry point listens on `127.0.0.1:8000`.
- Check the deployment health endpoint with `curl http://127.0.0.1:8000/health`.
- Render deploys with `pip install -r requirements.txt` and `uvicorn main:app --host 0.0.0.0 --port $PORT`, as configured in `render.yaml`.
- There is no checked-in automated test suite or lint configuration, so no single-test or lint command is currently available.

## Architecture

- `main.py` defines the FastAPI app, HTML routes, form/API endpoints, JSON-backed data access, upload handling, and admin authentication.
- `templates/index.html` is the public catalog and inquiry experience; `templates/admin.html` is the admin login/dashboard. Their forms submit to routes in `main.py`, so keep form actions, field names, and route parameters aligned. Most page styling and browser-side behavior live inline in these templates.
- Catalog entries, categories, business details, theme colors, inquiries, orders, logo settings, and admin history are stored in root-level JSON files. Their loaders supply defaults or normalize older catalog records; preserve compatibility with existing stored records when changing a shape.
- The admin-selected homepage hero image is persisted separately in `hero_config.json`; it can reference an active image item or a standalone validated hero upload, never a video or arbitrary filesystem path.
- The homepage's overlapping companion image is independently selected/uploaded and persisted in `hero_companion_config.json`; keep its admin controls and public template binding separate from the main hero photo.
- Uploads and JSON data use `PERSISTENT_DATA_DIR` when configured. Without it, uploads go to `static/uploads` and data files stay at the project root. Persistent uploads are served under `/media`; bundled static assets use `/static`.
- Upload routes for `/static/uploads/{filename}` and `/media/{filename}` must stay ahead of their static mounts so video `Range` requests can return partial content for reel seeking.
- Catalog reels retain native video controls plus a tap-to-play overlay. Keep the overlay synchronized with media `play`, `playing`, `pause`, and `error` events; call `video.play()` from the user's tap handler so mobile browsers receive a user gesture.
- Render's current configuration has no persistent disk; when no disk is mounted, an invalid `PERSISTENT_DATA_DIR` logs a warning and falls back to ephemeral storage. Uploaded media and JSON-backed catalog/settings may be lost when the instance restarts or redeploys. Never deploy local JSON or uploads to overwrite production data.
- When a Render disk is attached, mount it at `/opt/render/project/src/persistent`; an invalid `PERSISTENT_DATA_DIR` falls back to this mount if available.
- Set `SITE_URL` to the public production origin so canonical metadata and `sitemap.xml` use the real domain; without it, those URLs use the incoming request host.
- Instagram Graph API auto-import reads `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_ACCOUNT_ID`, and optional `INSTAGRAM_GRAPH_API_BASE_URL` from the environment; never check access tokens into the repository.

## Repository conventions

- Keep server-rendered page data in the context passed to `Jinja2Templates.TemplateResponse`; public and admin pages read shared site settings through the `get_*` helpers in `main.py`.
- Admin mutation handlers check `is_admin_authenticated(request)`. Preserve that check on admin-only endpoints, validate submitted values at the handler boundary, and use `save_upload` for bounded upload writes.
- Persist JSON as UTF-8 with indentation and `ensure_ascii=False`. Record admin-visible mutations through `append_history` to keep the dashboard history in sync.
- Use `requirements.txt` for Python runtime dependencies. The Render launch command imports the ASGI app as `main:app`.
