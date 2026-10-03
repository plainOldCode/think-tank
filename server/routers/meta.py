"""메타 라우터 — health, work-contract, install.sh."""
from fastapi import APIRouter, Request
from fastapi.responses import Response

import config
import service

router = APIRouter()


@router.get("/health")
def health():
    return {"status": "ok", "name": "think-tank"}


@router.get("/work-contract")
def work_contract(request: Request):
    return request.app.state.ctx.contract


@router.get("/install.sh", include_in_schema=False)
def install_script(request: Request):
    base = str(request.base_url).rstrip("/")
    src = config.CLI_PATH.read_text().replace("${TT_URL:-http://127.0.0.1:7800}", "${TT_URL:-" + base + "}")
    script = (
        "#!/bin/sh\n# think-tank tt CLI installer (served by the tt server itself)\n"
        "mkdir -p \"$HOME/.local/bin\"\n"
        "cat > \"$HOME/.local/bin/tt\" <<'TT_EOF'\n" + src + "\nTT_EOF\n"
        "chmod +x \"$HOME/.local/bin/tt\"\n"
        "echo \"installed: ~/.local/bin/tt (default TT_URL=" + base + "; env TT_URL overrides)\"\n"
        "case \":$PATH:\" in *\":$HOME/.local/bin:\"*) ;; *) echo 'add to PATH: export PATH=\"$HOME/.local/bin:$PATH\"' ;; esac\n"
    )
    return Response(script, media_type="text/x-shellscript")
