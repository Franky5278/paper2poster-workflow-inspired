"""Convert the generated PPTX to PNG with LibreOffice + PyMuPDF (robust on Windows)."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

SOFFICE = r"C:\Program Files\LibreOffice\program\soffice.exe"


def pptx_to_png(pptx: Path, out_dir: Path, dpi: int = 110) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    # NOTE: LibreOffice must write somewhere the sandbox allows; the user's %TEMP% is
    # not always writable here, so keep everything inside the workspace.
    tmp = (out_dir / "_lo_tmp").resolve()
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    profile = tmp / "profile"
    env = dict(os.environ, TEMP=str(tmp), TMP=str(tmp))
    cmd = [
        SOFFICE,
        "--headless",
        "--norestore",
        "--nolockcheck",
        "--nodefault",
        f"-env:UserInstallation=file:///{profile.as_posix()}",
        "--convert-to",
        "pdf",
        "--outdir",
        tmp.as_posix(),
        pptx.as_posix(),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
    print("soffice rc:", proc.returncode, proc.stdout[-300:], proc.stderr[-300:])

    pdfs = list(tmp.glob("*.pdf"))
    if not pdfs:
        raise RuntimeError(f"soffice produced no pdf: {proc.stdout} {proc.stderr}")

    import fitz

    images = []
    with fitz.open(pdfs[0]) as doc:
        for i, page in enumerate(doc):
            pix = page.get_pixmap(dpi=dpi)
            target = out_dir / f"poster_page_{i + 1}.png"
            pix.save(target)
            images.append(target)
    return images


if __name__ == "__main__":
    src = Path(sys.argv[1])
    dest = Path(sys.argv[2])
    for p in pptx_to_png(src, dest):
        print(p, p.stat().st_size // 1024, "KB")
