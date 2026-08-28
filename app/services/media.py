"""Image upload and storage (Part I §5.2, §5.4, §4, §16).

Every image the store shows comes through here: product main images and
galleries, variant photos, category tiles, publisher logos and homepage banners.

Four things this module refuses to leave to chance:

**1. The file is what it claims to be.** An extension is a suggestion; the
bytes are the fact. Every upload is decoded by Pillow before it is stored, so a
script renamed to ``.jpg`` fails at the door rather than being served back to a
browser later.

**2. EXIF is stripped.** A product photo taken on a phone carries the GPS
coordinates of wherever it was shot — frequently a staff member's home. Saving
that and serving it publicly would leak it. Re-encoding drops all metadata while
honouring the orientation tag first, so portrait photos do not come out
sideways.

**3. Sizes are bounded.** Both bytes and pixel dimensions, because a 20-megapixel
photo is a decompression bomb waiting to exhaust memory, and no storefront needs
one. Large images are downscaled rather than rejected — a shop manager
photographing stock should not have to learn about resolution.

**4. Filenames are generated, never accepted.** An uploaded name can contain
path traversal, be absurdly long, or collide with an existing file. A content
hash sidesteps all three and deduplicates identical uploads for free.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import settings
from app.core.errors import ValidationFailed
from app.core.logging import get_logger

log = get_logger(__name__)

#: Formats accepted on upload. Anything else is refused, including SVG — it can
#: carry script, and a logo is not worth an XSS vector.
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}

#: Pillow format → (extension, save format).
_OUTPUT = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}

#: Largest stored dimension. Bigger uploads are downscaled, preserving aspect.
MAX_DIMENSION = 2000

#: Guards against a decompression bomb: a small file that expands enormously.
MAX_PIXELS = 40_000_000

#: Derivative sizes generated alongside the original.
THUMBNAIL_SIZES: dict[str, int] = {"thumb": 320, "card": 640}


@dataclass(frozen=True, slots=True)
class StoredImage:
    """Where an upload ended up, relative to ``MEDIA_ROOT``."""

    path: str
    width: int
    height: int
    bytes_written: int
    variants: dict[str, str]

    @property
    def url(self) -> str:
        return f"{settings.media_url.rstrip('/')}/{self.path}"


def _target_dir(folder: str) -> Path:
    """Resolve a storage folder, refusing anything outside MEDIA_ROOT."""
    root = settings.media_root.resolve()
    # A folder name arriving from a route must not be able to escape the media
    # directory via "../".
    candidate = (root / folder).resolve()
    if not candidate.is_relative_to(root):
        raise ValidationFailed("Invalid upload destination.")
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def _read_upload(upload: UploadFile) -> bytes:
    """Read the upload, enforcing the byte limit as we go.

    Read in chunks with a running total rather than trusting a declared
    ``Content-Length``, which a client controls.
    """
    limit = settings.max_upload_mb * 1024 * 1024
    buffer = io.BytesIO()
    total = 0

    upload.file.seek(0)
    while chunk := upload.file.read(64 * 1024):
        total += len(chunk)
        if total > limit:
            raise ValidationFailed(
                f"That image is larger than {settings.max_upload_mb} MB.",
                details={"max_mb": settings.max_upload_mb},
            )
        buffer.write(chunk)

    if total == 0:
        raise ValidationFailed("That file is empty.")

    buffer.seek(0)
    return buffer.getvalue()


def _open_image(raw: bytes) -> Image.Image:
    """Decode and validate. The bytes decide, not the filename."""
    try:
        probe = Image.open(io.BytesIO(raw))
        probe.verify()  # structural check; consumes the file object
    except (UnidentifiedImageError, OSError) as exc:
        raise ValidationFailed(
            "That file is not a readable image."
        ) from exc

    image = Image.open(io.BytesIO(raw))  # reopen: verify() leaves it unusable

    if image.format not in ALLOWED_FORMATS:
        raise ValidationFailed(
            "Images must be JPEG, PNG or WebP.",
            details={"detected": image.format or "unknown"},
        )

    if image.width * image.height > MAX_PIXELS:
        raise ValidationFailed("That image has too many pixels to process.")

    return image


def _prepare(image: Image.Image) -> tuple[Image.Image, str]:
    """Normalise orientation, strip metadata, and pick an output format."""
    # Applies the EXIF orientation tag, then discards EXIF entirely — so the
    # photo is the right way up and carries no location data.
    image = ImageOps.exif_transpose(image)

    keep_alpha = image.mode in ("RGBA", "LA", "P") and image.format != "JPEG"
    if keep_alpha:
        image = image.convert("RGBA")
        out_format = "PNG"
    else:
        image = image.convert("RGB")
        out_format = "JPEG"

    if max(image.size) > MAX_DIMENSION:
        image.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)

    return image, out_format


def _encode(image: Image.Image, out_format: str) -> bytes:
    buffer = io.BytesIO()
    if out_format == "JPEG":
        image.save(buffer, "JPEG", quality=85, optimize=True, progressive=True)
    else:
        image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def store(upload: UploadFile, *, folder: str = "products") -> StoredImage:
    """Validate, normalise and save one uploaded image.

    Returns the path relative to ``MEDIA_ROOT`` — which is what goes in the
    database, so moving or re-hosting the media directory needs no data change.
    """
    raw = _read_upload(upload)
    image = _open_image(raw)
    prepared, out_format = _prepare(image)
    payload = _encode(prepared, out_format)

    # Content hash: no traversal, no collisions, and identical uploads
    # deduplicate to the same file for free.
    digest = hashlib.sha256(payload).hexdigest()[:24]
    extension = _OUTPUT[out_format]
    directory = _target_dir(folder)
    filename = f"{digest}{extension}"
    (directory / filename).write_bytes(payload)

    variants = _write_derivatives(prepared, out_format, directory, digest)

    stored = StoredImage(
        path=f"{folder}/{filename}",
        width=prepared.width,
        height=prepared.height,
        bytes_written=len(payload),
        variants=variants,
    )
    log.info(
        "image_stored",
        extra={
            "path": stored.path,
            "width": stored.width,
            "height": stored.height,
            "kb": round(len(payload) / 1024, 1),
        },
    )
    return stored


def _write_derivatives(
    image: Image.Image, out_format: str, directory: Path, digest: str
) -> dict[str, str]:
    """Generate the listing and card sizes.

    Serving a 2000px original into a 160px product card wastes most of a mobile
    shopper's data — and §17.5 makes mobile the primary case.
    """
    extension = _OUTPUT[out_format]
    variants: dict[str, str] = {}

    for name, size in THUMBNAIL_SIZES.items():
        if max(image.size) <= size:
            continue
        derivative = image.copy()
        derivative.thumbnail((size, size), Image.LANCZOS)
        filename = f"{digest}-{name}{extension}"
        (directory / filename).write_bytes(_encode(derivative, out_format))
        variants[name] = f"{directory.name}/{filename}"

    return variants


def delete(path: str | None) -> bool:
    """Remove a stored file and its derivatives.

    Used when an image is *replaced*. The database row that referenced it is
    still closed rather than deleted (Part II §6) — this only reclaims disk.
    Refuses any path that escapes ``MEDIA_ROOT``.
    """
    if not path:
        return False

    root = settings.media_root.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return False

    stem, extension = target.stem, target.suffix
    for name in THUMBNAIL_SIZES:
        derivative = target.with_name(f"{stem}-{name}{extension}")
        if derivative.is_file():
            derivative.unlink()

    target.unlink()
    log.info("image_deleted", extra={"path": path})
    return True


def variant_path(path: str | None, size: str) -> str | None:
    """The path of a derivative, falling back to the original.

    Templates call this rather than constructing names, so a missing derivative
    degrades to the full-size image instead of a broken picture.
    """
    if not path:
        return None
    if size not in THUMBNAIL_SIZES:
        return path

    candidate = Path(path)
    derived = f"{candidate.parent}/{candidate.stem}-{size}{candidate.suffix}".lstrip("./")
    return derived if (settings.media_root / derived).is_file() else path
