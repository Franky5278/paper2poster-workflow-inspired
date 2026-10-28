"""Local stand-in for the lab's poster layout / rendering service (port 44462).

Endpoints (same contract as the lab deployment):

    POST /layout/            {panels, figures} -> {panel_arrangement, figure_arrangement, text_arrangement}
    POST /generate_poster/   {bullet_content, tree_split_result} -> {poster_url, pptx_url}

This follows the lab's route-function file closely; what changed:
  * project root / static dir point at this checkout instead of /root/...
  * /generate_poster actually renders the PPTX to PNG (soffice) and returns URLs,
    so the Dify workflow can show a real poster at the end.
"""

import json
import logging
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent / "repos" / "Paper2Poster"
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# LibreOffice ships here on Windows; make `soffice` resolvable for ppt_to_images()
LO_PROGRAM = Path(r"C:\Program Files\LibreOffice\program")
if LO_PROGRAM.exists():
    os.environ["PATH"] = str(LO_PROGRAM) + os.pathsep + os.environ.get("PATH", "")

sys.path.append(str(REPO_ROOT))

import contextlib  # noqa: E402


@contextlib.contextmanager
def repo_cwd():
    """main_train()/main_inference() read assets/... relative to cwd; the repo root also
    contains a vendored docling/ that must NOT be on sys.path, so borrow cwd only briefly."""
    prev = os.getcwd()
    os.chdir(REPO_ROOT)
    try:
        yield
    finally:
        os.chdir(prev)  # append: the vendored docling/ must not shadow site-packages

# Some Paper2Poster utils import camel.* / FlagEmbedding at module level but only use
# those symbols inside functions this service never calls. Stub them so the (very heavy)
# camel-ai / FlagEmbedding packages are not needed.
import importlib.abc  # noqa: E402
import importlib.machinery  # noqa: E402
import types as _pytypes  # noqa: E402

_STUBBED = {"datasets", "tiktoken", "pytesseract", "pytorch_fid", "pdf2image", "Levenshtein", "json_repair", "marker", "camel", "FlagEmbedding"}


class _DummyModule(_pytypes.ModuleType):
    __path__ = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        child = _DummyModule(f"{self.__name__}.{name}")
        setattr(self, name, child)
        return child


class _StubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in _STUBBED:
            return importlib.machinery.ModuleSpec(fullname, self, is_package=True)
        return None

    def create_module(self, spec):
        mod = _DummyModule(spec.name)
        mod.__path__ = []
        return mod

    def exec_module(self, module):
        return None


if not any(isinstance(f, _StubFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _StubFinder())

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from PosterAgent.gen_pptx_code import generate_poster_code  # noqa: E402
from PosterAgent.tree_split_layout import main_inference, main_train  # noqa: E402
from render_pptx import pptx_to_png  # noqa: E402
from utils.wei_utils import run_code, style_bullet_content, utils_functions  # noqa: E402

POSTER_WIDTH = 48 * 25  # 1200 px @ 25 dpi
POSTER_HEIGHT = 36 * 25  # 900 px

HOST = os.environ.get("P2P_PUBLIC_HOST", "127.0.0.1")
PUBLIC_BASE = f"http://{HOST}:{os.environ.get('P2P_POSTER_PORT', '44462')}"

app = FastAPI(title="Poster Layout Service (local)", version="1.0.0")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app.mount("/files", StaticFiles(directory=str(OUTPUT_DIR)), name="files")

_train_cache: dict = {}


async def _cleanup_temp_dir(path: Path):
    if path.exists() and path.is_dir():
        try:
            shutil.rmtree(path)
            logger.info("cleaned temp dir %s", path)
        except OSError as exc:
            logger.error("cleanup failed %s: %s", path, exc)


def _boxes_overlap(a, b, pad=0.02):
    """True when two inch-space rects (x, y, width, height) intersect."""
    ax, ay, aw, ah = float(a["x"]), float(a["y"]), float(a["width"]), float(a["height"])
    bx, by, bw, bh = float(b["x"]), float(b["y"]), float(b["width"]), float(b["height"])
    return not (
        ax + aw <= bx - pad
        or bx + bw <= ax - pad
        or ay + ah <= by - pad
        or by + bh <= ay - pad
    )


def _fit_font_sizes(bullet_content, text_boxes, fill=0.82, min_scale=0.62, max_scale=2.6):
    """Scale each textbox's font size so its copy fills roughly `fill` of the box height.

    Font sizes arrive from Dify as absolute points (48 body / 60 heading) with no knowledge of
    the box the layout produced, which is why short sections leave big empty areas and dense
    ones overflow. This estimates wrapped line counts and solves for a size that fits.
    """
    import math

    if not bullet_content or not text_boxes:
        return

    boxes = {b.get("textbox_name"): b for b in text_boxes}

    def est_height(paras, size, width_in):
        line_h = size / 72.0 * 1.28
        chars_per_line = max(8, int(width_in / (size / 72.0 * 0.50)))
        lines = 0
        for para in paras:
            text = " ".join(r.get("text", "") for r in para.get("runs", []))
            lines += max(1, math.ceil(len(text) / chars_per_line))
        return lines * line_h

    for index, section in enumerate(bullet_content):
        # section 0 is the poster title block; its size is already right, and the box is
        # deliberately short and wide so the fitter would blow it up
        if index == 0:
            continue
        for name, paras in section.items():
            if not isinstance(paras, list) or not paras:
                continue
            box = boxes.get(name)
            if not box:
                continue
            width_in, height_in = float(box["width"]), float(box["height"])
            sizes = [p.get("font_size", 48) for p in paras if isinstance(p.get("font_size"), (int, float))]
            base = max(sizes) if sizes else 48

            lo, hi = min_scale, max_scale
            for _ in range(20):
                mid = (lo + hi) / 2
                if est_height(paras, base * mid, width_in) <= height_in * fill:
                    lo = mid
                else:
                    hi = mid

            for para in paras:
                current = para.get("font_size", base)
                para["font_size"] = int(max(16, min(120, round(current * lo))))


def _models():
    if "params" not in _train_cache:
        logger.info("training layout models (cached after first call) ...")
        with repo_cwd():
            _train_cache["params"] = main_train()
        logger.info("layout models ready")
    return _train_cache["params"]


@app.get("/health")
def health():
    return {"ok": True, "service": "poster", "root": str(REPO_ROOT)}


@app.post("/layout/")
async def layout_endpoint(request: Request):
    try:
        raw = (await request.body()).decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, str):
            data = json.loads(data)
        panels, figures = data["panels"], data["figures"]

        panel_params, figure_params = _models()
        with repo_cwd():
            panel_arr, figure_arr, text_arr = main_inference(
                panels, panel_params, figure_params,
                int(POSTER_WIDTH), int(POSTER_HEIGHT), shrink_margin=3,
            )
        logger.info("layout ok: %d panels, %d figures", len(panel_arr), len(figure_arr))
        return JSONResponse(content={
            "panel_arrangement": panel_arr,
            "figure_arrangement": figure_arr,
            "text_arrangement": text_arr,
        })
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("layout failed", exc_info=True)
        raise HTTPException(status_code=500, detail=f"layout failed: {exc}")


@app.post("/generate_poster/")
async def generate_poster_endpoint(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        if not data or "bullet_content" not in data or "tree_split_result" not in data:
            raise HTTPException(400, "missing bullet_content / tree_split_result")

        bullet_content = data["bullet_content"]
        tree = data["tree_split_result"]
        poster_name = data.get("poster_name") or "Dify_poster"
        (OUTPUT_DIR / "last_generate_payload.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )

        panel_in = tree.get("panel_arrangement_inches")
        text_in = tree.get("text_arrangement_inches")
        figure_in = tree.get("figure_arrangement_inches")
        width_inch = tree.get("poster_width_inches")
        height_inch = tree.get("poster_height_inches")
        if not all([panel_in, text_in, width_inch, height_inch]):
            raise HTTPException(400, "tree_split_result lacks layout inches")

        theme_title_text_color = (255, 255, 255)
        theme_title_fill_color = (47, 85, 151)
        theme = {
            "panel_visible": True,
            "textbox_visible": False,
            "figure_visible": False,
            "panel_theme": {"color": theme_title_fill_color, "thickness": 5, "line_style": "solid"},
            "textbox_theme": None,
            "figure_theme": None,
        }

        if bullet_content:
            for _k, v in bullet_content[0].items():
                if isinstance(v, list):
                    style_bullet_content(v, theme_title_text_color, theme_title_fill_color)
            for i in range(1, len(bullet_content)):
                curr = bullet_content[i]
                if isinstance(curr.get("title"), list):
                    style_bullet_content(curr["title"], theme_title_text_color, theme_title_fill_color)

        tmp = Path(tempfile.mkdtemp(prefix="poster_tmp_"))
        background_tasks.add_task(_cleanup_temp_dir, tmp)

        # pull remote figures down to disk under names that are safe Python identifiers
        # (the generated rendering code derives variable names from the file name)
        if figure_in:
            # URLs use host.docker.internal so containers can reach us; on the host itself
            # that name does not resolve, so swap it back to loopback. HOST_ALIAS_REWRITE
            figdir = OUTPUT_DIR / "figures"
            figdir.mkdir(parents=True, exist_ok=True)
            for idx, fig in enumerate(figure_in, start=1):
                url = (fig.get("figure_path") or "").replace("host.docker.internal", "127.0.0.1")
                if url.startswith("http"):
                    try:
                        safe = figdir / f"fig_{idx}{Path(url).suffix or '.png'}"
                        urllib.request.urlretrieve(url, safe)
                        fig["figure_path"] = safe.as_posix()
                        fig["figure_name"] = safe.name
                    except Exception as exc:
                        logger.warning("figure download failed %s: %s", url, exc)
                        fig["figure_path"] = ""
                elif url:
                    # local path produced by the extract service: mirror it under a safe name
                    src = Path(url)
                    safe = figdir / f"fig_{idx}{src.suffix or '.png'}"
                    if not src.exists():
                        fig["figure_path"] = ""
                    elif src.resolve() == safe.resolve():
                        fig["figure_path"] = src.as_posix()
                        fig["figure_name"] = src.name
                    else:
                        shutil.copyfile(src, safe)
                        fig["figure_path"] = safe.as_posix()
                        fig["figure_name"] = safe.name

        # keep the title panel clean: figures there would cover the title
        if figure_in:
            titled = {
                p.get("panel_id")
                for p in (panel_in or [])
                if "title" in str(p.get("panel_name", "")).lower()
            }
            figure_in = [f for f in figure_in if f.get("panel_id") not in titled]

        # Decide the figure/text split BEFORE font fitting, so the fitter sees the final boxes.
        # A panel that also holds copy gives its top 45% to the figure and the rest to the text;
        # a panel that is figure-only keeps the whole area.
        try:
            panels_by_id = {p.get("panel_id"): p for p in (panel_in or [])}
            for fig in figure_in or []:
                panel = panels_by_id.get(fig.get("panel_id"))
                if not panel:
                    continue
                margin, header = 0.12, 0.45
                px, py = panel["x"], panel["y"]
                pw, ph = panel["width"], panel["height"]
                has_text = any(_boxes_overlap(t, panel) for t in (text_in or []))
                share = 0.45 if has_text else 1.0

                fig["width"] = min(fig["width"], pw - 2 * margin)
                fig["height"] = min(fig["height"], max(0.8, (ph - header - margin) * share))
                fig["x"] = max(px + margin, min(fig["x"], px + pw - fig["width"] - margin))
                fig["y"] = py + header

                if has_text:
                    boxes = sorted(
                        (t for t in (text_in or []) if _boxes_overlap(t, panel)),
                        key=lambda t: t["y"],
                    )
                    title_box = boxes[0] if len(boxes) > 1 else None
                    if title_box is not None:
                        title_box["y"] = py + 0.06
                        title_box["height"] = max(0.95, min(1.35, ph * 0.22))
                        fig["y"] = title_box["y"] + title_box["height"] + 0.06
                        body_boxes = boxes[1:]
                    else:
                        body_boxes = boxes
                    bottom = fig["y"] + fig["height"] + 0.12
                    for t in body_boxes:
                        t["y"] = bottom
                        t["height"] = max(0.8, (py + ph) - bottom - 0.12)
        except Exception as exc:
            logger.warning("figure inset failed: %s", exc)

        # the Dify side writes fixed font sizes (48pt body, 60pt headings) that know nothing
        # about how much room a panel actually got, which leaves large empty areas on short
        # sections. Re-derive each textbox's size from its own box so the copy fills it.
        try:
            _fit_font_sizes(bullet_content, text_in)
        except Exception as exc:
            logger.warning("font fit failed: %s", exc)

        pptx_path = tmp / "poster.pptx"
        code = generate_poster_code(
            panel_in, text_in, figure_in,
            presentation_object_name="poster_presentation",
            slide_object_name="poster_slide",
            utils_functions=utils_functions,
            slide_width=width_inch,
            slide_height=height_inch,
            img_path=None,
            save_path=pptx_path.as_posix(),
            visible=False,
            content=bullet_content,
            theme=theme,
            tmp_dir=tmp.as_posix(),
        )
        # slightly larger type fills the panels better on a 48x36in poster
        code = code.replace("font_size=40", "font_size=52")

        _out, err = run_code(code)
        if err is not None:
            logger.error("run_code failed: %s", err)
            (OUTPUT_DIR / "last_generate_payload.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            (OUTPUT_DIR / "last_generated_code.py").write_text(code, encoding="utf-8")
            raise HTTPException(500, f"pptx generation failed: {err}")

        job_dir = OUTPUT_DIR / poster_name
        job_dir.mkdir(parents=True, exist_ok=True)
        final_pptx = job_dir / f"{poster_name}.pptx"
        shutil.move(str(pptx_path), str(final_pptx))

        image_url = ""
        try:
            pngs = pptx_to_png(final_pptx, job_dir, dpi=110)
            if pngs:
                image_url = f"{PUBLIC_BASE}/files/{poster_name}/{pngs[0].name}"
        except Exception as exc:
            logger.error("pptx->png failed: %s", exc, exc_info=True)
        pptx_url = f"{PUBLIC_BASE}/files/{poster_name}/{final_pptx.name}"

        logger.info("poster ready: %s", image_url or pptx_url)
        return JSONResponse(content={
            "message": "ok",
            "poster_url": image_url,
            "pptx_url": pptx_url,
            "poster_name": poster_name,
        })
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("generate_poster failed", exc_info=True)
        raise HTTPException(500, f"generate_poster failed: {exc}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("P2P_POSTER_PORT", "44462")))









