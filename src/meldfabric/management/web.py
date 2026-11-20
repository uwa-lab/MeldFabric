"""Serve the compiled Vue application; Python never assembles frontend source."""

from html import escape
from importlib.resources import files
from pathlib import PurePosixPath


def admin_html(management_path: str, root_path: str = "") -> str:
    root_path = root_path.rstrip("/")
    base = root_path + management_path
    return (
        files("meldfabric.management")
        .joinpath("static/index.html")
        .read_text(encoding="utf-8")
        .replace("__MELDFABRIC_ADMIN_BASE__", escape(base, quote=True))
        .replace("__MELDFABRIC_ROOT_PATH__", escape(root_path, quote=True))
        .replace("/__MELDFABRIC_ADMIN_STATIC__/", escape(base + "/static/", quote=True))
    )


def admin_asset(path: str) -> tuple[bytes, str] | None:
    # Serve Vite public assets only, never frontend source or build metadata.
    parts = PurePosixPath(path).parts
    if not parts or parts[0] != "assets" or any(part in {".", ".."} for part in parts):
        return None
    if "\\" in path or path.startswith("/"):
        return None
    asset = files("meldfabric.management").joinpath("static", *parts)
    if not asset.is_file():
        return None
    media = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}
    return asset.read_bytes(), media.get(PurePosixPath(path).suffix, "application/octet-stream")
