"""File storage facade used by the rest of MY MONEY.

Routing (the only place that knows which vendor stores what):
    image/*                 -> Uploadcare   (logos, avatars, community and announcement images)
    everything else (docs)  -> ImageKit     (PDF, Word, Excel, TXT/CSV, receipts, statements)
Metadata for every file is stored in Turso (``stored_files``). Nothing is written to
the application filesystem. To replace a vendor, add a module implementing
``StorageProvider`` and change ``provider()`` below.
"""
import hashlib
import io
import logging

from flask import current_app
from PIL import Image, ImageOps, UnidentifiedImageError

from app.extensions import db
from app.models import StoredFile, utcnow
from app.services.audit import audit
from app.services.storage_providers.base import NotConfigured, StorageError
from app.services.storage_providers.imagekit import ImageKitProvider
from app.services.storage_providers.memory import MemoryProvider
from app.services.storage_providers.uploadcare import UploadcareProvider

log = logging.getLogger(__name__)

IMAGE_EXTS = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
PIL_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
DOC_EXTS = {
    "pdf": {"application/pdf"},
    "doc": {"application/msword"},
    "docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "xls": {"application/vnd.ms-excel"},
    "xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    "txt": {"text/plain"},
    "csv": {"text/csv", "application/csv", "text/plain", "application/vnd.ms-excel"},
}
DOCUMENT_KINDS = ("document", "receipt", "statement", "post_document", "announcement_document")


class UploadError(ValueError):
    """User-facing upload problem (validation, provider outage, missing config)."""


# ------------------------------------------------------------------- providers
def provider(name):
    if current_app.config.get("STORAGE_BACKEND") == "memory":
        if current_app.config.get("IS_PRODUCTION"):
            raise RuntimeError("The memory storage backend is for tests only.")
        pool = current_app.extensions.setdefault(
            "memory_storage", {"uploadcare": MemoryProvider("uploadcare"), "imagekit": MemoryProvider("imagekit")})
        return pool[name]
    return {"uploadcare": UploadcareProvider, "imagekit": ImageKitProvider}[name]()


def provider_name_for(content_type: str) -> str:
    return "uploadcare" if (content_type or "").startswith("image/") else "imagekit"


def storage_status() -> dict:
    """Which providers are configured (booleans only - never secrets)."""
    return {"uploadcare": provider("uploadcare").configured(), "imagekit": provider("imagekit").configured()}


# ------------------------------------------------------------------ validation
def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""


def _safe_name(filename: str) -> str:
    base = (filename or "file").replace("\\", "/").split("/")[-1]
    return "".join(c for c in base if c.isalnum() or c in "._- ")[:120] or "file"


def process_image(raw: bytes, filename: str, claimed_mime: str, max_bytes: int, max_side=1600, square=False):
    """Validate extension, MIME type, size and real image content, then re-encode
    (strips metadata and any embedded payload). Returns (bytes, mime, ext)."""
    ext = _ext(filename)
    if ext not in IMAGE_EXTS:
        raise UploadError("Only PNG, JPG, JPEG and WebP images are allowed.")
    if claimed_mime and claimed_mime.split(";")[0].lower() not in set(IMAGE_EXTS.values()):
        raise UploadError("Unsupported image type.")
    if not raw:
        raise UploadError("The file is empty.")
    if len(raw) > max_bytes:
        raise UploadError(f"Image is too large (max {max_bytes // (1024 * 1024)} MB).")
    try:
        probe = Image.open(io.BytesIO(raw))
        fmt = probe.format
        probe.verify()
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise UploadError("That file is not a valid image.")
    if fmt not in PIL_FORMATS:
        raise UploadError("Only PNG, JPG, JPEG and WebP images are allowed.")
    if square:
        img = ImageOps.fit(img, (min(img.size), min(img.size)), centering=(0.5, 0.5))
    img.thumbnail((max_side, max_side))
    out = io.BytesIO()
    if fmt == "JPEG" or (fmt == "WEBP" and img.mode not in ("RGBA", "LA")):
        img.convert("RGB").save(out, "JPEG", quality=88, optimize=True)
        return out.getvalue(), "image/jpeg", "jpg"
    img.convert("RGBA").save(out, "PNG", optimize=True)
    return out.getvalue(), "image/png", "png"


def validate_document(raw: bytes, filename: str, claimed_mime: str, max_bytes: int):
    ext = _ext(filename)
    if ext not in DOC_EXTS:
        raise UploadError("Allowed documents: PDF, Word, Excel, TXT, CSV.")
    if not raw:
        raise UploadError("The file is empty.")
    if len(raw) > max_bytes:
        raise UploadError(f"Document is too large (max {max_bytes // (1024 * 1024)} MB).")
    mime = (claimed_mime or "").split(";")[0].lower()
    if mime and mime != "application/octet-stream" and mime not in DOC_EXTS[ext]:
        raise UploadError("The file type does not match its extension.")
    head = raw[:8]
    ok = {
        "pdf": head.startswith(b"%PDF"), "docx": head.startswith(b"PK"), "xlsx": head.startswith(b"PK"),
        "doc": head.startswith(b"\xd0\xcf\x11\xe0"), "xls": head.startswith(b"\xd0\xcf\x11\xe0"),
    }.get(ext)
    if ok is None:  # txt / csv must be plain text
        try:
            raw[:4096].decode("utf-8")
            ok = b"\x00" not in raw[:4096]
        except UnicodeDecodeError:
            ok = False
    if not ok:
        raise UploadError("The file content does not match its type.")
    return ext, (mime if mime in DOC_EXTS[ext] else next(iter(sorted(DOC_EXTS[ext]))))


# --------------------------------------------------------------------- storing
def store_bytes(raw, filename, content_type, *, group_id, user_id, kind, related=(None, None)) -> StoredFile:
    """Upload already-validated bytes to the right provider and record metadata in Turso."""
    prov = provider(provider_name_for(content_type))
    if not prov.configured():
        raise UploadError("File storage is not configured yet. Please contact the platform administrator.")
    folder = f"my-money/{'group-%s' % group_id if group_id else 'users'}/{kind}"
    try:
        res = prov.upload(raw, _safe_name(filename), content_type, folder=folder, tags=[kind])
    except NotConfigured as e:
        raise UploadError(str(e))
    except StorageError as e:
        raise UploadError(str(e))
    row = StoredFile(
        group_id=group_id, kind=kind, provider=prov.name, provider_file_id=res.provider_file_id,
        file_url=res.file_url, filename=_safe_name(filename), extension=_ext(filename) or None,
        content_type=content_type, size=len(raw), checksum=hashlib.sha256(raw).hexdigest(),
        related_type=related[0], related_id=related[1], created_by=user_id,
    )
    db.session.add(row)
    db.session.flush()
    audit("file.uploaded", "stored_file", row.id,
          {"kind": kind, "provider": prov.name, "filename": row.filename, "size": row.size},
          group_id=group_id, user_id=user_id)
    return row


def store_upload(file_storage, *, group_id, user_id, kind, images_only=False, related=(None, None), square=False,
                 max_side=1600, max_bytes=None) -> StoredFile:
    """Validate an uploaded form file and store it: images -> Uploadcare, documents -> ImageKit."""
    cfg = current_app.config
    filename = file_storage.filename or ""
    raw = file_storage.read()
    ext = _ext(filename)
    if ext in IMAGE_EXTS:
        data, mime, new_ext = process_image(raw, filename, file_storage.mimetype,
                                            max_bytes or cfg["ATTACHMENT_MAX_BYTES"], max_side, square)
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        return store_bytes(data, f"{stem}.{new_ext}", mime, group_id=group_id, user_id=user_id, kind=kind,
                           related=related)
    if images_only:
        raise UploadError("Only PNG, JPG, JPEG and WebP images are allowed.")
    _, mime = validate_document(raw, filename, file_storage.mimetype, max_bytes or cfg["ATTACHMENT_MAX_BYTES"])
    return store_bytes(raw, filename, mime, group_id=group_id, user_id=user_id, kind=kind, related=related)


def archive_generated(raw, filename, content_type, *, group_id, user_id, kind, related):
    """Best-effort archive of a generated PDF (receipt / statement) to ImageKit.
    De-duplicated by checksum, never blocks the download, returns None on failure."""
    checksum = hashlib.sha256(raw).hexdigest()
    existing = StoredFile.query.filter_by(group_id=group_id, checksum=checksum, kind=kind, deleted_at=None).first()
    if existing:
        return existing
    try:
        return store_bytes(raw, filename, content_type, group_id=group_id, user_id=user_id, kind=kind,
                           related=related)
    except UploadError as e:
        log.warning("Could not archive %s: %s", filename, e)
        return None


# ---------------------------------------------------------------- reading / URLs
def read_bytes(file: StoredFile) -> bytes:
    try:
        return provider(file.provider).read(file.provider_file_id, file.file_url, content_type=file.content_type)
    except StorageError as e:
        raise UploadError(str(e))


def signed_url(file: StoredFile, seconds=None):
    """Short-lived private delivery URL (ImageKit documents). None when the provider
    has no signed URLs - callers then stream the bytes through Flask."""
    secs = seconds or current_app.config["SIGNED_URL_SECONDS"]
    try:
        return provider(file.provider).signed_url(file.provider_file_id, file.file_url, expires_in=secs)
    except StorageError as e:
        raise UploadError(str(e))


# ---------------------------------------------------------------------- deleting
def delete_file(file: StoredFile, user_id=None, reason="deleted"):
    """Soft-delete the metadata row (history preserved) and remove the remote object."""
    if file is None or file.deleted_at:
        return
    file.deleted_at, file.deleted_by = utcnow(), user_id
    try:
        provider(file.provider).delete(file.provider_file_id)
        audit("file.deleted", "stored_file", file.id, {"reason": reason, "provider": file.provider},
              group_id=file.group_id, user_id=user_id)
    except (StorageError, NotConfigured) as e:
        log.warning("Remote delete failed for file %s: %s", file.id, e)
        audit("file.delete_failed", "stored_file", file.id, {"error": str(e)}, group_id=file.group_id, user_id=user_id)


def replace_file(old: StoredFile, file_storage, *, user_id, **kwargs) -> StoredFile:
    """Upload the replacement FIRST, then retire the old file - a failed upload
    never leaves the record without a file."""
    new = store_upload(file_storage, group_id=old.group_id, user_id=user_id, kind=old.kind,
                       related=(old.related_type, old.related_id), **kwargs)
    audit("file.replaced", "stored_file", new.id, {"replaces": old.id}, group_id=old.group_id, user_id=user_id)
    delete_file(old, user_id, reason="replaced")
    return new
