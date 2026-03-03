from fastapi.staticfiles import StaticFiles

# 静态文件目录
from pathlib import Path

# If relative to project root, compute dynamically instead of hard-coding.
BASE_DIR = Path(__file__).resolve().parent.parent  # workspace root containing app/
ESSAY_STATIC_DIR = BASE_DIR / "doc" / "essay"
ESSAY_MOUNT_PATH = "/essay"

# create directory if missing to avoid runtime errors
if not ESSAY_STATIC_DIR.exists():
    ESSAY_STATIC_DIR.mkdir(parents=True, exist_ok=True)

def setup_static_files(app):
    """
    配置静态文件服务，将项目中的 doc/essay 目录映射到 /essay 路径。
    """
    app.mount(ESSAY_MOUNT_PATH, StaticFiles(directory=str(ESSAY_STATIC_DIR)), name="essay")
