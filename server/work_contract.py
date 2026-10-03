import hashlib
import json
import os
from pathlib import Path


def current_contract():
    raw = os.getenv("TT_CONTRACT_VERSION", "2.1")
    version = ("tt-tdd-v1" if raw == "1"
               else "tt-tdd-v2.1" if raw == "2.1" else "tt-tdd-v2")
    marker = ("tt-work-contract" if version == "tt-tdd-v1"
              else "tt-work-contract-v2.1" if version == "tt-tdd-v2.1" else "tt-work-contract-v2")
    document = (Path(__file__).parent / "static" / "api.md").read_text()
    instructions = document.split(f"<!-- {marker}:start -->", 1)[1].split(
        f"<!-- {marker}:end -->", 1
    )[0].strip()
    policy = {"instructions": instructions, "report_required": os.getenv("TT_REQUIRE_REPORT", "0") == "1"}
    digest = hashlib.sha256(json.dumps(policy, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"version": version + ":" + digest, **policy}
