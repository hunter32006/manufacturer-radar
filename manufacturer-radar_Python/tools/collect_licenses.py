import importlib.metadata
import shutil
import sys
from pathlib import Path

target = Path(sys.argv[1]) / "licenses"
target.mkdir(parents=True, exist_ok=True)
for name in ["requests", "beautifulsoup4", "soupsieve", "openpyxl", "et-xmlfile", "urllib3", "certifi", "charset-normalizer", "idna", "typing-extensions", "Pillow", "pyinstaller", "pypdf", "playwright", "greenlet", "pypinyin"]:
    dist = importlib.metadata.distribution(name)
    folder = target / (name + "-" + dist.version)
    folder.mkdir(exist_ok=True)
    for entry in dist.files or []:
        if any(mark in entry.name.lower() for mark in ("license", "licence", "copying", "notice", "authors")):
            source = dist.locate_file(entry)
            if source.is_file():
                shutil.copy2(source, folder / entry.name)
license_file = Path(sys.base_prefix) / "LICENSE.txt"
if license_file.exists():
    shutil.copy2(license_file, target / "PYTHON_LICENSE.txt")
for archive in (Path(sys.base_prefix) / "tcl").glob("lib*.zip"):
    import zipfile
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.namelist():
            if "license" in entry.lower():
                (target / (archive.stem + "_" + Path(entry).name)).write_bytes(bundle.read(entry))
