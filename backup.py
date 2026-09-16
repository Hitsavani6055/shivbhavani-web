from datetime import datetime
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parent
BACKUP_DIR = ROOT / "backups"
INCLUDE_FILES = [
    "main.py",
    "requirements.txt",
    "collection.json",
    "inquiries.json",
    "admin_history.json",
    "logo_config.json",
    "design.json",
]


def create_backup():
    BACKUP_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = BACKUP_DIR / f"shivbhavani_backup_{timestamp}.zip"

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative_path in INCLUDE_FILES:
            source = ROOT / relative_path
            if source.is_file():
                archive.write(source, relative_path)

        uploads_dir = ROOT / "static" / "uploads"
        if uploads_dir.is_dir():
            for source in uploads_dir.rglob("*"):
                if source.is_file():
                    archive.write(source, source.relative_to(ROOT).as_posix())

        for template in (ROOT / "templates").glob("*"):
            if template.is_file():
                archive.write(template, template.relative_to(ROOT).as_posix())

    print(f"Backup created: {archive_path}")


if __name__ == "__main__":
    create_backup()
