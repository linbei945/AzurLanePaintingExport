"""Worker process entry points (must stay importable for multiprocessing)."""
from __future__ import annotations

import io
import time

from PIL import Image

import core

_SOURCE = None
_ROOT = None


def init(root: str) -> None:
    global _SOURCE, _ROOT
    _ROOT = root
    _SOURCE = core.AssetSource(root)


def get_source(root: str):
    if _SOURCE is None or _ROOT != root:
        init(root)
    return _SOURCE


def export_job(job: dict) -> dict:
    root = job["root"]
    name = job["name"]
    out_dir = job["out_dir"]
    started = time.time()
    try:
        saved, skipped = core.export_painting(
            get_source(root),
            name,
            out_dir,
            export_faces=job.get("faces", True),
            export_parts=job.get("parts", False),
            crop=job.get("crop", False),
            png_level=int(job.get("level", 0)),
            native=job.get("native", True),
            face_names=job.get("face_names"),
            face_offset=tuple(job.get("face_offset") or (0, 0)),
            export_base=job.get("export_base", False),
            overwrite=job.get("overwrite", True),
            keep_overlay=job.get("overlay", False),
        )
        return {
            "name": name,
            "ok": True,
            "saved": saved,
            "count": len(saved),
            "skipped": skipped,
            "seconds": round(time.time() - started, 2),
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001 - reported to the UI
        return {
            "name": name,
            "ok": False,
            "saved": [],
            "count": 0,
            "skipped": 0,
            "seconds": round(time.time() - started, 2),
            "error": f"{type(exc).__name__}: {exc}",
        }


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


def _thumb(img: Image.Image, size: int = 72) -> bytes:
    thumb = img.copy()
    thumb.thumbnail((size, size), Image.LANCZOS)
    return _png(thumb)


def preview_job(job: dict) -> dict:
    root = job["root"]
    name = job["name"]
    try:
        src = get_source(root)
        analysis = core.analyze(src, "painting/" + name)
        if not job.get("overlay", False):
            core.drop_overlays(analysis)
        native = job.get("native", True)
        img = core.compose_base(analysis, native)
        if img is None:
            return {"name": name, "ok": False, "error": "empty painting"}

        faces_payload = []
        face_rect = None
        base_thumb = b""
        rect = core.face_layout(analysis, native=native)
        _slot = core.slot_of(analysis)
        if rect is not None:
            x, y, w, h = rect[:4]
            face_rect = [x, y, w, h]
            patch = img.crop((max(0, x), max(0, y), max(0, x + w), max(0, y + h)))
            base_thumb = _thumb(patch)
            for face_name, face_img in core.load_faces(src, name, slot=_slot):
                placed = (
                    face_img if face_img.size == (w, h)
                    else face_img.resize((w, h), Image.LANCZOS)
                )
                faces_payload.append(
                    {
                        "name": face_name,
                        "png": _png(placed),
                        "thumb": _thumb(placed),
                        "rect": face_rect,
                    }
                )

        return {
            "name": name,
            "ok": True,
            "png": _png(img),
            "width": img.width,
            "height": img.height,
            "parts": [p.name for p in analysis.parts],
            "faces": faces_payload,
            "face_rect": face_rect,
            "base_thumb": base_thumb,
        }
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
