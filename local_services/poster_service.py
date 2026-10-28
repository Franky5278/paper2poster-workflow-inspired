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
            figdir = OUTPUT_DIR / "figures"
            figdir.mkdir(parents=True, exist_ok=True)
            for idx, fig in enumerate(figure_in, start=1):
                url = fig.get("figure_path") or ""
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

        # inset figures inside their panel so they never cover the section header or the border
        try:
            panels_by_id = {p.get("panel_id"): p for p in (panel_in or [])}
            for fig in figure_in or []:
                panel = panels_by_id.get(fig.get("panel_id"))
                if not panel:
                    continue
                margin = 0.12
                header = 0.45
                px, py = panel["x"], panel["y"]
                pw, ph = panel["width"], panel["height"]
                fig["width"] = min(fig["width"], pw - 2 * margin)
                fig["height"] = min(fig["height"], ph - header - margin)
                fig["x"] = max(px + margin, min(fig["x"], px + pw - fig["width"] - margin))
                fig["y"] = max(py + header, min(fig["y"], py + ph - fig["height"] - margin))
        except Exception as exc:
            logger.warning("figure inset failed: %s", exc)

        # keep the title panel clean: figures assigned to panel 0 would cover the title
        if figure_in:
            figure_in = [f for f in figure_in if f.get("panel_id") != 0]

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









