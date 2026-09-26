import hashlib
import json
import os
from pathlib import Path


def current_contract():
    document = (Path(__file__).parent / "static" / "api.md").read_text()
    instructions = document.split("<!-- tt-work-contract:start -->", 1)[1].split(
        "<!-- tt-work-contract:end -->", 1
    )[0].strip()
    policy = {"instructions": instructions, "report_required": os.getenv("TT_REQUIRE_REPORT", "0") == "1"}
    digest = hashlib.sha256(json.dumps(policy, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"version": "tt-tdd-v1:" + digest, **policy}
