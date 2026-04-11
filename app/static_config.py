from pathlib import Path

from fastapi.staticfiles import StaticFiles

# If relative to project root, compute dynamically instead of hard-coding.
BASE_DIR = Path(__file__).resolve().parent.parent  # workspace root containing app/
DOC_STATIC_DIR = BASE_DIR / "doc"
DOC_MOUNT_PATH = "/doc"
UPLOADS_STATIC_DIR = BASE_DIR / "uploads"
UPLOADS_MOUNT_PATH = "/uploads"

# create directory if missing to avoid runtime errors
for static_dir in (DOC_STATIC_DIR, UPLOADS_STATIC_DIR):
	if not static_dir.exists():
		static_dir.mkdir(parents=True, exist_ok=True)


def setup_static_files(app):
    """
    配置静态文件服务，将项目中的 doc 目录映射到 /doc 路径，
    并将 uploads 目录映射到 /uploads 路径。
    """
    app.mount(DOC_MOUNT_PATH, StaticFiles(directory=str(DOC_STATIC_DIR)), name="doc")
    app.mount(UPLOADS_MOUNT_PATH, StaticFiles(directory=str(UPLOADS_STATIC_DIR)), name="uploads")
