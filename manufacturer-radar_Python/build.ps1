param([string]$Python = "python", [string]$DistPath = "$PSScriptRoot\dist")
$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    $env:PYTHONIOENCODING = "utf-8"
    & $Python -m pip install --disable-pip-version-check -r requirements-dev.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }
    $Runtime = Join-Path $PSScriptRoot ".build-tcl"
    $env:RADAR_TCL_BUILD = $Runtime
    & $Python -c "import sys,zipfile,os; from pathlib import Path; p=Path(sys.base_prefix)/'tcl'; out=Path(os.environ['RADAR_TCL_BUILD']); out.mkdir(exist_ok=True); [zipfile.ZipFile(x).extractall(out) for x in p.glob('lib*.zip')]"
    $ExtraData = @()
    if (Test-Path "$Runtime\tcl_library\init.tcl") {
        $env:TCL_LIBRARY = "$Runtime\tcl_library"
        $env:TK_LIBRARY = "$Runtime\tk_library"
        $ExtraData = @("--add-data", "$Runtime\tcl_library;_tcl_data", "--add-data", "$Runtime\tk_library;_tk_data")
    }
    & $Python -m PyInstaller --noconfirm --clean --onedir --windowed --name ManufacturerRadar --icon assets\radar.ico --distpath $DistPath --workpath .build --specpath . @ExtraData main.py
    if ($LASTEXITCODE -ne 0) { throw "Build failed" }
    & $Python -c "from pathlib import Path; import shutil,sys; src=Path('\u4f7f\u7528\u8bf4\u660e.txt'); shutil.copy2(src, Path(sys.argv[1])/src.name)" "$DistPath\ManufacturerRadar"
    Copy-Item -LiteralPath "$PSScriptRoot\THIRD_PARTY_NOTICES.txt" -Destination "$DistPath\ManufacturerRadar\THIRD_PARTY_NOTICES.txt"
    Copy-Item -LiteralPath "$PSScriptRoot\MULTISOURCE.md" -Destination "$DistPath\ManufacturerRadar\MULTISOURCE.md"
    & $Python -c "from pathlib import Path; import sys; from manufacturer_finder.ingestion import write_import_template; write_import_template(Path(sys.argv[1])/'import-template.xlsx')" "$DistPath\ManufacturerRadar"
    if ($LASTEXITCODE -ne 0) { throw "Import template generation failed" }
    & $Python tools\collect_licenses.py "$DistPath\ManufacturerRadar"
    if ($LASTEXITCODE -ne 0) { throw "License collection failed" }
    Write-Output "Portable executable: $DistPath\ManufacturerRadar\ManufacturerRadar.exe"
} finally {
    Pop-Location
}
