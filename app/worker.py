"""Worker process entry points (must stay importable for multiprocessing)."""
from __future__ import annotations

import io
import os
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
    global _SOURCE, _ROOT
    if _SOURCE is None or _ROOT != root:
        init(root)
    return _SOURCE


def export_job(job: dict) -> dict:
    root = job["root"]
    name = job["name"]
    out_dir = job["out_dir"]
    started = time.time()
    try:
        src = get_source(root)
        saved = core.export_painting(
            src,
            name,
            out_dir,
            export_faces=job.get("faces", True),
            export_parts=job.get("parts", False),
            crop=job.get("crop", False),
            png_level=int(job.get("level", 6)),
            native_scale=job.get("native", True),
            face_names=job.get("face_names"),
            face_offset=tuple(job.get("face_offset") or (0, 0)),
            first_face_only=job.get("first_face_only", False),
            export_base=job.get("export_base", False),
            overwrite=job.get("overwrite", True),
        )
        return {
            "name": name,
            "ok": True,
            "saved": saved,
            "count": len(saved),
            "seconds": round(time.time() - started, 2),
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001 - reported to the UI
        return {
            "name": name,
            "ok": False,
            "saved": [],
            "count": 0,
            "seconds": round(time.time() - started, 2),
            "error": f"{type(exc).__name__}: {exc}",
        }


def preview_job(job: dict) -> dict:
    root = job["root"]
    name = job["name"]
    try:
        src = get_source(root)
        analysis = core.analyze(src, "painting/" + name)
        scale = core.auto_scale(analysis) if job.get("native", True) else 1.0
        img = core.compose_base(analysis, scale)
        if img is None:
            return {"name": name, "ok": False, "error": "empty painting"}

        buf = io.BytesIO()
        img.save(buf, format="PNG", compress_level=1)

        faces_payload = []
        rect = core.face_layout(analysis, scale)
        face_rect = None
        base_thumb = b""
        if rect is not None:
            face_rect = list(rect[:4])
            x, y, w, h = rect[:4]
            patch = img.crop((max(0, x), max(0, y), max(0, x + w), max(0, y + h)))
            patch.thumbnail((72, 72), Image.LANCZOS)
            tbuf = io.BytesIO()
            patch.save(tbuf, format="PNG", compress_level=1)
            base_thumb = tbuf.getvalue()
        if rect is not None:
            x, y, w, h, _, _ = rect
            for face_name, face_img in core.load_faces(src, name):
                placed = face_img if face_img.size == (w, h) else face_img.resize((w, h), Image.LANCZOS)
                fbuf = io.BytesIO()
                placed.save(fbuf, format="PNG", compress_level=1)
                thumb = placed.copy()
                thumb.thumbnail((72, 72), Image.LANCZOS)
                tbuf = io.BytesIO()
                thumb.save(tbuf, format="PNG", compress_level=1)
                faces_payload.append(
                    {
                        "name": face_name,
                        "png": fbuf.getvalue(),
                        "thumb": tbuf.getvalue(),
                        "rect": [x, y, w, h],
                    }
                )

        return {
            "name": name,
            "ok": True,
            "png": buf.getvalue(),
            "width": img.width,
            "height": img.height,
            "parts": [p.name for p in analysis.parts],
            "faces": faces_payload,
            "face_rect": face_rect,
            "base_thumb": base_thumb,
        }
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def probe_job(job: dict) -> dict:
    root = job["root"]
    name = job["name"]
    try:
        src = get_source(root)
        analysis = core.analyze(src, "painting/" + name)
        return {
            "name": name,
            "ok": True,
            "parts": len(analysis.parts),
            "faces": len(core.load_faces(src, name)),
            "canvas": analysis.canvas,
        }
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
