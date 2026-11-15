# -*- coding: utf-8 -*-
"""校验 koolook.ths-sync-pack ZIP：本地契约 + 运行期 /api/import-center/inspect。"""
from __future__ import annotations

import ast
import io
import json
import sys
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(r"C:\Users\koolook\ashare-tsp")
ZIP = ROOT / "release" / "koolook.ths-sync-pack-1.0.0.zip"
INSPECT_URL = "http://127.0.0.1:3018/api/import-center/inspect"
EXPECTED_ID = "koolook.ths-sync-pack"
EXPECTED_PREFIX = "/api/extensions/" + EXPECTED_ID

ALLOWED = {".py", ".yaml", ".yml", ".json", ".txt", ".md", ".csv",
           ".js", ".mjs", ".css", ".svg", ".png", ".jpg", ".webp"}
REQUIRED = {
    "extension.yaml",
    "backend/__init__.py",
    "backend/extension.py",
    "backend/ths_sync/__init__.py",
    "backend/ths_sync/reader.py",
    "backend/ths_sync/routes.py",
    "backend/ths_sync/service.py",
    "backend/ths_sync/ths_agent.py",
    "backend/ths_sync/tzzb_source.py",
    "backend/ths_sync/writer.py",
    "backend/ths_sync/xd_screen.py",
    "frontend/extension.mjs",
}

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def multipart_upload(data: bytes, filename: str) -> tuple[int, bytes]:
    boundary = "----ths-sync-pack-" + uuid.uuid4().hex
    body = io.BytesIO()

    def w(part: bytes) -> None:
        body.write(part)

    w(("--" + boundary + "\r\n").encode())
    w(('Content-Disposition: form-data; name="files"; filename="%s"\r\n' % filename).encode())
    w(b"Content-Type: application/zip\r\n\r\n")
    w(data)
    w(("\r\n--" + boundary + "--\r\n").encode())
    req = urllib.request.Request(
        INSPECT_URL,
        data=body.getvalue(),
        method="POST",
        headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main() -> int:
    if not ZIP.exists():
        print("RESULT: FAIL — ZIP not found: %s" % ZIP)
        return 1
    data = ZIP.read_bytes()
    check(len(data) <= 20 * 1024 * 1024, "ZIP exceeds 20 MiB")

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        files = {name: zf.read(name) for name in names if not name.endswith("/")}

    check(len(infos) <= 1000, "ZIP has >1000 entries")
    for name in names:
        p = PurePosixPath(name)
        check(not name.startswith("/") and ".." not in p.parts, "bad path: %s" % name)
        if not name.endswith("/"):
            check(p.suffix.lower() in ALLOWED, "disallowed suffix: %s" % name)
        check("__pycache__" not in p.parts, "pycache in ZIP: %s" % name)
        check(not name.endswith(".pyc"), "pyc in ZIP: %s" % name)
    for banned in ("manifest.json", "plugin.yaml", "plugin.yml"):
        check(not any(PurePosixPath(n).name == banned for n in names), "banned file: %s" % banned)
    for name in REQUIRED:
        check(name in files, "missing: %s" % name)
    for name, payload in files.items():
        if name.endswith(".py"):
            try:
                ast.parse(payload.decode("utf-8"), filename=name)
            except Exception as exc:
                failures.append("python syntax error %s: %s" % (name, exc))

    yaml_text = files.get("extension.yaml", b"").decode("utf-8")
    for line in (
        "format: tick-extension-package",
        "format_version: 1",
        "id: " + EXPECTED_ID,
        "version: 1.0.0",
        "extension_api_version: 1",
        "backend: backend.extension",
        "frontend: frontend/extension.mjs",
        "requires_app: '>=0.3.25'",
        "requires_python: '>=3.11'",
        "platforms: [any]",
        "requires: []",
    ):
        check(line in yaml_text, "extension.yaml missing: %s" % line)
    check("TZZB MCP 主源" in yaml_text, "extension.yaml missing TZZB MCP primary source description")

    backend = files.get("backend/extension.py", b"").decode("utf-8")
    for snippet in (
        'from app.extensions import BACKEND_EXTENSION_API_VERSION, ExtensionContext',
        'EXTENSION_ID = "%s"' % EXPECTED_ID,
        'EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION',
        'def setup(registrar) -> None:',
        'registrar.include_router(build_router())',
        'def startup(context: ExtensionContext) -> None:',
        'service.start(context.data_dir)',
    ):
        check(snippet in backend, "backend/extension.py missing: %s" % snippet)

    routes = files.get("backend/ths_sync/routes.py", b"").decode("utf-8")
    check('router = APIRouter(prefix="%s", tags=["ths-sync-pack"])' % EXPECTED_PREFIX in routes,
          "routes prefix mismatch")
    check("/api/custom/ths-sync" not in routes, "stale /api/custom/ths-sync prefix")
    check('return service.run_once(with_positions=False)' in routes, "missing /sync handler")
    check('return service.run_once(with_positions=True)' in routes, "missing /sync/positions handler")
    check('return service.run_once(dry=True)' in routes, "missing /sync/dry handler")
    check('@router.get("/tzzb")' in routes, "missing /tzzb endpoint")
    check("service.tzzb_summary()" in routes, "/tzzb endpoint missing service.tzzb_summary()")
    check("service.read_positions_any(allow_screen=False)" in routes, "preview missing unified source chain")
    check("positions_channel" in routes, "preview missing positions_channel")

    service = files.get("backend/ths_sync/service.py", b"").decode("utf-8")
    check("STATE_VERSION = 3" in service, "service not v3")
    check('NOTE_BLOCK = "同花顺板块"' in service, "service missing NOTE_BLOCK")
    check("from . import tzzb_source" in service, "service missing tzzb_source import")
    check("def read_positions_any(*, allow_screen: bool = True)" in service, "read_positions_any guard missing")
    check("tzzb_source.read_positions" in service, "TZZB MCP primary read missing")
    check("read_positions_any(allow_screen=False)" in service, "status screen guard missing")
    check("def tzzb_summary()" in service, "tzzb_summary missing")
    check("tzzb_source.read_trade_summary()" in service, "trade summary delegation missing")
    check("query_positions_mcp" not in service, "query_positions_mcp remains")
    check("reader.to_tsp_symbol" in service, "canonical ingress missing")
    check("reader.to_ths_code" in service, "canonical egress missing")
    check('d["symbol"] = reader.to_tsp_symbol' in service, "position symbol missing")

    tzzb = files.get("backend/ths_sync/tzzb_source.py", b"").decode("utf-8")
    for marker in ("CREATE_NO_WINDOW", "STARTF_USESHOWWINDOW", "SW_HIDE", "shell=False"):
        check(marker in tzzb, "tzzb_source.py missing no-window marker: %s" % marker)
    for tool in (
        "TZZB.promptAccountSelection",
        "TZZB.queryStockPosition",
        "TZZB.queryPnlSummary",
        "TZZB.queryClearedPosition",
        "TZZB.queryMoneyHistory",
        "TZZB.queryAssetChangeSummary",
        "TZZB.queryDayProfitSummary",
    ):
        check(tool in tzzb, "tzzb_source.py missing tool: %s" % tool)
    for fn in ("call_tool", "positions_from_payload", "read_positions", "read_snapshot", "read_trade_summary"):
        check("def %s" % fn in tzzb, "tzzb_source.py missing function: %s" % fn)

    reader = files.get("backend/ths_sync/reader.py", b"").decode("utf-8")
    check("def to_tsp_symbol(code: str) -> str | None:" in reader, "to_tsp_symbol missing")
    check("def to_ths_code(code: str) -> str | None:" in reader, "to_ths_code missing")

    writer = files.get("backend/ths_sync/writer.py", b"").decode("utf-8")
    check("CreateToolhelp32Snapshot" in writer or "Process32FirstW" in writer,
          "writer ctypes process snapshot missing")
    check("subprocess.run" not in writer, "writer still has subprocess.run")
    check("powershell" not in writer, "writer still has powershell")

    frontend = files.get("frontend/extension.mjs", b"").decode("utf-8")
    check("export default extension" in frontend, "frontend default export missing")
    check("apiVersion: 1" in frontend, "frontend apiVersion missing")
    check("id: PACK_ID" in frontend, "frontend id binding missing")
    check("const PACK_ID = '%s'" % EXPECTED_ID in frontend, "frontend pack id mismatch")
    check("const API = '/api/extensions/__PACKAGE_ID__'" in frontend or "const API = '/api/extensions/%s'" % EXPECTED_ID in frontend,
          "frontend API base missing")
    check("17652" not in frontend, "frontend stale 17652 bridge")
    check("ths.sync" not in frontend, "frontend stale ths.sync id")
    check("openStockPreview" in frontend, "frontend native stock preview missing")
    check("path: '/ths-sync-pack'" in frontend, "frontend route path missing")
    check("icon: 'Database'" in frontend, "frontend navigation icon missing")

    status, raw = multipart_upload(data, ZIP.name)
    check(status == 200, "inspect HTTP %s body=%s" % (status, raw[:500].decode("utf-8", "replace")))
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        payload = {}
        failures.append("inspect JSON parse failed: %s" % exc)
    kind = payload.get("kind")
    preview = payload.get("preview") or {}
    manifest = preview.get("manifest") or {}
    errors = preview.get("errors") or []
    preview_files = preview.get("files") or []
    check(kind == "extension", "inspect kind=%r" % kind)
    check(manifest.get("id") == EXPECTED_ID, "inspect manifest.id=%r" % manifest.get("id"))
    check(manifest.get("format") == "tick-extension-package",
          "inspect manifest.format=%r" % manifest.get("format"))
    check(manifest.get("backend") == "backend.extension",
          "inspect manifest.backend=%r" % manifest.get("backend"))
    check(manifest.get("frontend") == "frontend/extension.mjs",
          "inspect manifest.frontend=%r" % manifest.get("frontend"))
    check(not errors, "inspect errors=%r" % errors)
    for name in REQUIRED:
        check(name in preview_files, "inspect files missing %s" % name)

    if failures:
        print("RESULT: FAIL — %d checks failed" % len(failures))
        for item in failures:
            print("  - " + item)
        return 1
    print("RESULT: VERIFY PASS zip=%s bytes=%d files=%d inspect=extension/%s errors=0" %
          (ZIP, len(data), len(files), EXPECTED_ID))
    return 0


if __name__ == "__main__":
    sys.exit(main())
