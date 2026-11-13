"""Portable paths, Tcl setup, and logging without a console window."""
import logging
import os
import sys
from pathlib import Path


def configure_tcl():
    if getattr(sys, "frozen", False):
        return  # PyInstaller's runtime hook sets bundled Tcl paths.
    project = Path(__file__).resolve().parents[1]
    candidates = [project / ".runtime-tcl", project.parent / "tcl_runtime"]
    for root in candidates:
        if (root / "tcl_library" / "init.tcl").exists():
            os.environ.setdefault("TCL_LIBRARY", str(root / "tcl_library"))
            os.environ.setdefault("TK_LIBRARY", str(root / "tk_library"))
            return
    # Python 3.14's Windows install can provide Tcl script libraries as ZIPs.
    import zipfile
    archives = list((Path(sys.base_prefix) / "tcl").glob("lib*.zip"))
    if archives:
        root = project / ".runtime-tcl"
        root.mkdir(exist_ok=True)
        for archive in archives:
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(root)
        if (root / "tcl_library" / "init.tcl").exists():
            os.environ.setdefault("TCL_LIBRARY", str(root / "tcl_library"))
            os.environ.setdefault("TK_LIBRARY", str(root / "tk_library"))


def app_directory() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]


def data_directory() -> Path:
    folder = app_directory() / "data"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write-check"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        folder = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "ManufacturerRadar" / "data"
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def configure_logging():
    from logging.handlers import RotatingFileHandler
    handler = RotatingFileHandler(data_directory() / "application.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(handlers=[handler], level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
