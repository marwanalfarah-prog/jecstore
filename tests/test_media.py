"""Image upload (Part I §5.2, §5.4, §16).

Most of what is pinned here is defensive. An upload endpoint is the widest
attack surface a store has: it takes an arbitrary file from a browser and later
serves it back to other people's browsers.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi import UploadFile
from PIL import Image

from app.core.errors import ValidationFailed
from app.services import media


@pytest.fixture(autouse=True)
def media_root(tmp_path, monkeypatch):
    """Isolate storage per test."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "media_root", tmp_path)
    return tmp_path


def _image_bytes(
    *, size=(800, 600), fmt="JPEG", colour=(220, 40, 60), exif: bytes | None = None
) -> bytes:
    image = Image.new("RGB" if fmt == "JPEG" else "RGBA", size, colour)
    buffer = io.BytesIO()
    if exif is not None:
        image.save(buffer, fmt, exif=exif)
    else:
        image.save(buffer, fmt)
    return buffer.getvalue()


def _upload(payload: bytes, filename="photo.jpg") -> UploadFile:
    return UploadFile(filename=filename, file=io.BytesIO(payload))


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_stores_an_image_and_returns_a_relative_path(media_root: Path):
    stored = media.store(_upload(_image_bytes()), folder="products")

    assert stored.path.startswith("products/")
    assert (media_root / stored.path).is_file()
    assert stored.width == 800 and stored.height == 600


def test_stored_path_is_relative_so_media_can_move(media_root: Path):
    """The database holds a relative path, so re-hosting media needs no data
    change."""
    stored = media.store(_upload(_image_bytes()), folder="products")
    assert not Path(stored.path).is_absolute()
    assert stored.url.startswith("/media/")


def test_identical_uploads_deduplicate(media_root: Path):
    """A content hash means the same photo twice is one file."""
    payload = _image_bytes()
    first = media.store(_upload(payload), folder="products")
    second = media.store(_upload(payload), folder="products")
    assert first.path == second.path


def test_different_images_get_different_paths(media_root: Path):
    a = media.store(_upload(_image_bytes(colour=(10, 20, 30))), folder="products")
    b = media.store(_upload(_image_bytes(colour=(200, 100, 50))), folder="products")
    assert a.path != b.path


def test_png_with_transparency_stays_png(media_root: Path):
    stored = media.store(_upload(_image_bytes(fmt="PNG"), "logo.png"), folder="publishers")
    assert stored.path.endswith(".png")


# ---------------------------------------------------------------------------
# Rejection
# ---------------------------------------------------------------------------


def test_a_renamed_script_is_refused(media_root: Path):
    """An extension is a suggestion; the bytes are the fact."""
    payload = b"#!/bin/sh\nrm -rf /\n"
    with pytest.raises(ValidationFailed):
        media.store(_upload(payload, "innocent.jpg"), folder="products")


def test_html_disguised_as_an_image_is_refused(media_root: Path):
    """Serving this back would be stored XSS."""
    payload = b"<html><script>alert(document.cookie)</script></html>"
    with pytest.raises(ValidationFailed):
        media.store(_upload(payload, "x.png"), folder="products")


def test_svg_is_refused(media_root: Path):
    """SVG can carry script, and a logo is not worth an XSS vector."""
    payload = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    with pytest.raises(ValidationFailed):
        media.store(_upload(payload, "logo.svg"), folder="publishers")


def test_an_empty_file_is_refused(media_root: Path):
    with pytest.raises(ValidationFailed):
        media.store(_upload(b"", "empty.jpg"), folder="products")


def test_oversized_files_are_refused(media_root: Path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "max_upload_mb", 1)
    # Random-ish content so it does not compress away.
    payload = _image_bytes(size=(3000, 3000)) + (b"\x00\xff" * 700_000)

    with pytest.raises(ValidationFailed) as excinfo:
        media.store(_upload(payload), folder="products")
    assert "MB" in excinfo.value.message


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def test_large_images_are_downscaled_not_rejected(media_root: Path):
    """A shop manager photographing stock should not have to learn about
    resolution."""
    stored = media.store(_upload(_image_bytes(size=(4000, 3000))), folder="products")

    assert max(stored.width, stored.height) == media.MAX_DIMENSION
    # Aspect ratio preserved.
    assert round(stored.width / stored.height, 2) == round(4000 / 3000, 2)


def test_exif_is_stripped(media_root: Path):
    """A phone photo carries GPS — often a staff member's home address."""
    exif = Image.Exif()
    exif[271] = "SecretCamera"          # Make
    exif[34853] = {1: "N", 2: (31, 57, 0)}  # GPSInfo

    stored = media.store(
        _upload(_image_bytes(exif=exif.tobytes())), folder="products"
    )

    saved = Image.open(media_root / stored.path)
    assert not saved.getexif(), "no metadata may survive the re-encode"


def test_orientation_is_applied_before_exif_is_dropped(media_root: Path):
    """Otherwise portrait photos come out sideways."""
    exif = Image.Exif()
    exif[274] = 6  # rotate 90° CW

    stored = media.store(
        _upload(_image_bytes(size=(800, 400), exif=exif.tobytes())), folder="products"
    )
    # The rotation was applied, so the stored image is now portrait.
    assert stored.height > stored.width


def test_derivative_sizes_are_generated(media_root: Path):
    """Serving a 2000px original into a 160px card wastes a mobile shopper's
    data, and §17.5 makes mobile the primary case."""
    stored = media.store(_upload(_image_bytes(size=(1600, 1200))), folder="products")

    assert set(stored.variants) == set(media.THUMBNAIL_SIZES)
    for path in stored.variants.values():
        assert (media_root / path).is_file()


def test_small_images_get_no_pointless_derivatives(media_root: Path):
    stored = media.store(_upload(_image_bytes(size=(200, 150))), folder="products")
    assert stored.variants == {}


def test_variant_path_falls_back_to_the_original(media_root: Path):
    stored = media.store(_upload(_image_bytes(size=(200, 150))), folder="products")
    # No thumbnail exists at this size, so the original is served.
    assert media.variant_path(stored.path, "thumb") == stored.path


def test_variant_path_uses_a_derivative_when_present(media_root: Path):
    stored = media.store(_upload(_image_bytes(size=(1600, 1200))), folder="products")
    assert media.variant_path(stored.path, "thumb") != stored.path


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def test_folder_traversal_is_refused(media_root: Path):
    with pytest.raises(ValidationFailed):
        media.store(_upload(_image_bytes()), folder="../../etc")


def test_uploaded_filename_is_never_used(media_root: Path):
    """An uploaded name can traverse, collide, or be absurd. It is discarded."""
    stored = media.store(
        _upload(_image_bytes(), "../../../evil name!!.jpg"), folder="products"
    )
    assert "evil" not in stored.path
    assert ".." not in stored.path


def test_delete_refuses_to_escape_the_media_root(media_root: Path, tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("do not delete me")

    assert media.delete("../outside.txt") is False
    assert outside.is_file()


def test_delete_removes_the_file_and_its_derivatives(media_root: Path):
    stored = media.store(_upload(_image_bytes(size=(1600, 1200))), folder="products")
    derivative = media_root / stored.variants["thumb"]

    assert media.delete(stored.path) is True
    assert not (media_root / stored.path).exists()
    assert not derivative.exists()


def test_deleting_a_missing_file_is_harmless(media_root: Path):
    assert media.delete("products/nothing-here.jpg") is False
    assert media.delete(None) is False
