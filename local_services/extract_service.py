"""Local stand-in for the lab's PDF-extraction service.

Endpoint contract (reverse-engineered from paper2poster最终版.yml):

    POST /extract_pdf_from_upload/   multipart/form-data, field name: file
    -> JSON:
       {
         "markdown": "<full paper markdown>",
         "files": [
            {"type": "figure_image", "url": "http://<host>:44461/files/<name>.png",
             "name": "<name>.png", "caption": "...", "page": 3},
            ...
         ]
       }

The `files` list is consumed by the Dify workflow as:
  * ``$.files[*].url``  -> list of image URLs (downloaded one by one by an
    iteration node and described by a vision model)
  * ``$.files``        -> kept so ``type == figure_image`` entries can be
    indexed 1..N and matched against the figure ids chosen downstream.

Images are written to ./output and served under /files.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

HOST = os.environ.get("P2P_PUBLIC_HOST", "127.0.0.1")
PORT = int(os.environ.get("P2P_EXTRACT_PORT", "44461"))
PUBLIC_BASE = f"http://{HOST}:{PORT}"

MAX_FIGURES = 40

app = FastAPI(title="Paper2Poster PDF extraction (local)")
app.mount("/files", StaticFiles(directory=str(OUTPUT_DIR)), name="files")


def _slug(name: str) -> str:
    stem = Path(name or "paper").stem
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_") or "paper"
    return stem[:40]


def _load_api_key() -> str:
    for candidate in (BASE_DIR.parent / "bailian_key.txt", BASE_DIR / "bailian_key.txt"):
        if candidate.exists():
            return candidate.read_text(encoding="utf-8").strip()
    return os.environ.get("DASHSCOPE_API_KEY", "")


def _describe_figure(path: Path) -> str:
    """One-sentence description of a figure via qwen3-vl-plus (works on this account)."""
    key = _load_api_key()
    if not key:
        return ""
    try:
        import base64

        import requests

        b64 = base64.b64encode(path.read_bytes()).decode()
        resp = requests.post(
            "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": "qwen3-vl-plus",
                "input": {"messages": [{"role": "user", "content": [
                    {"image": f"data:image/png;base64,{b64}"},
                    {"text": "Describe this academic paper figure in 1-2 factual sentences: what it shows "
                             "(chart type, axes, or diagram) and the key takeaway. No preamble."},
                ]}]},
            },
            timeout=120,
        )
        resp.raise_for_status()
        content = resp.json()["output"]["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
        return str(content).strip()
    except Exception as exc:  # pragma: no cover - best effort
        print(f"[warn] figure description failed: {exc}", file=sys.stderr)
        return ""


def _clean_md(md: str) -> str:
    # drop docling's image placeholders and repeated blank lines
    md = re.sub(r"<!--\s*image\s*-->", "", md, flags=re.I)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def _docling_convert(pdf_path: Path):
    """Return (markdown, [(pil_image, caption, page_no), ...])."""
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions()
    opts.images_scale = 2.0
    opts.generate_picture_images = True
    opts.do_table_structure = True

    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )
    doc = converter.convert(str(pdf_path)).document

    md = doc.export_to_markdown()

    figures = []
    try:
        for item, _level in doc.iterate_items():
            if type(item).__name__ != "PictureItem":
                continue
            try:
                img = item.get_image(doc)
            except Exception:
                img = None
            if img is None:
                continue
            caption = " ".join(
                c.text for c in getattr(item, "captions", []) if getattr(c, "text", None)
            ).strip()
            page = None
            prov = getattr(item, "prov", None) or []
            if prov:
                page = getattr(prov[0], "page_no", None)
            figures.append((img, caption, page))
            if len(figures) >= MAX_FIGURES:
                break
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[warn] picture extraction failed: {exc}", file=sys.stderr)

    return md, figures


def _pymupdf_fallback(pdf_path: Path):
    """Very rough fallback: page text + embedded raster images."""
    import fitz  # PyMuPDF

    parts, figures = [], []
    with fitz.open(pdf_path) as doc:
        for pno in range(min(len(doc), 30)):
            page = doc[pno]
            parts.append(f"\n\n## Page {pno + 1}\n\n" + page.get_text("text").strip())
            for img in page.get_images(full=True):
                if len(figures) >= MAX_FIGURES:
                    break
                try:
                    pix = fitz.Pixmap(doc, img[0])
                    if pix.n - pix.alpha >= 4:
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    if pix.width < 120 or pix.height < 120:
                        continue
                    from PIL import Image

                    pil = Image.open(io.BytesIO(pix.tobytes("png")))
                    figures.append((pil, "", pno + 1))
                except Exception:
                    continue
    return "\n".join(parts), figures


@app.get("/health")
def health():
    return {"ok": True, "service": "extract", "public_base": PUBLIC_BASE}


@app.post("/extract_pdf_from_upload/")
async def extract_pdf_from_upload(file: UploadFile = File(...)):
    started = time.time()
    slug = _slug(file.filename)
    raw_bytes = await file.read()

    # cache by content hash so re-running the workflow on the same PDF is instant
    digest = hashlib.sha256(raw_bytes).hexdigest()[:16]
    cache_dir = OUTPUT_DIR / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{digest}.json"
    if cache_file.exists():
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        print(f"[extract] cache hit {digest}", file=sys.stderr)
        return JSONResponse(content=cached)

    job = f"{slug}_{digest[:6]}"
    pdf_path = OUTPUT_DIR / f"{job}.pdf"
    pdf_path.write_bytes(raw_bytes)

    try:
        md, figures = _docling_convert(pdf_path)
        engine = "docling"
    except Exception as exc:
        print(f"[warn] docling failed ({exc}); falling back to PyMuPDF", file=sys.stderr)
        md, figures = _pymupdf_fallback(pdf_path)
        engine = "pymupdf"

    files = []
    for idx, (img, caption, page) in enumerate(figures, start=1):
        name = f"{job}-figure-{idx}.png"
        try:
            img.save(OUTPUT_DIR / name, "PNG")
        except Exception:
            continue

        # The Dify tongyi plugin (0.0.27) does not know qwen3-vl-plus, which is the only
        # vision model this account can call. So we describe the figure here and hand the
        # workflow a plain-text "caption" URL; `image_url` keeps the real picture for the
        # layout/render step.
        desc = _describe_figure(OUTPUT_DIR / name) or caption or f"Figure {idx} from the paper."
        txt_name = f"{job}-figure-{idx}.txt"
        (OUTPUT_DIR / txt_name).write_text(desc, encoding="utf-8")

        files.append(
            {
                "type": "figure_image",
                "url": f"{PUBLIC_BASE}/files/{txt_name}",
                "image_url": f"{PUBLIC_BASE}/files/{name}",
                "name": name,
                "caption": desc,
                "page": page,
            }
        )

    (BASE_DIR / "output" / f"{job}.md").write_text(md, encoding="utf-8")

    body = {
        "markdown": _clean_md(md),
        "files": files,
        "engine": engine,
        "job": job,
        "elapsed_s": round(time.time() - started, 1),
    }
    cache_file.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    print(
        f"[extract] {job}: {len(md)} chars md, {len(files)} figures, {body['elapsed_s']}s",
        file=sys.stderr,
    )
    return JSONResponse(content=body)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
