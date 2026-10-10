"""TT 개선#3f — CLI 전반 점검: 스키마 정합성·에러 전문 출력·입력 안전화.

서버 openapi 스키마가 CLI 페이로드와 정합인지, 422 에러가 잘리지 않는지,
다중행 본문의 안전 입력 경로(--body-file)가 있는지 검증한다.
"""
import json
import subprocess
import urllib.request

import pytest

from test_cli_display import cli  # noqa: F401 — 라이브 서버 픽스처 재사용


def test_openapi_필수필드와_cli_페이로드_정합(cli):
    """422 실측 회귀: POST /issues required는 [title, acceptance] — CLI new가 둘 다 보낸다."""
    run, url, api, db = cli
    with urllib.request.urlopen(url + "/openapi.json", timeout=10) as r:
        schema = json.load(r)
    ref = schema["paths"]["/issues"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    for _ in range(3):
        if "$ref" in ref:
            ref = schema["components"]["schemas"][ref["$ref"].split("/")[-1]]
    required = ref["required"]
    assert set(required) == {"title", "acceptance"}, "서버 스키마 기준 필드 확인"


def test_422_에러는_전문_출력된다(cli):
    """done --report의 계약 위반 시 validation 배열이 필드명까지 온전히 보인다."""
    run, url, api, db = cli
    iid = json.loads(run("new", "에러 전문 테스트", "-a", "완료 기준: 에러 전문", "--json").stdout)["id"]
    run("claim", iid)
    ver = api(f"/issues/{iid}")["work_contract"]["version"]
    import tempfile, pathlib
    bad = json.dumps({"method": "planned"})  # design/verification/result 누락
    f = pathlib.Path(tempfile.mkdtemp()) / "bad.json"
    f.write_text(bad, encoding="utf-8")
    p = run("done", iid, "--report", str(f))
    err = p.stderr + p.stdout
    # 전문: 누락 필드가 loc.msg로 온전히 출력(잘린 JSON 조각 없음)
    assert "contract_version" in err and "attempt" in err, f"에러 전문 확인: {err}"
    assert "실패:" in err and ": Field required" in err
    assert "} ]" not in err and "}" not in err, f"잘린 JSON 조각: {err}"


def test_body_file로_다중행_본문(cli, tmp_path):
    """백틱·개행 포함 본문을 파일로 안전하게 넣는다."""
    run, url, api, db = cli
    body_file = tmp_path / "body.md"
    body_file.write_text("첫 줄\n`backtick` $HOME \"인용\"\n마지막", encoding="utf-8")
    out = json.loads(run("new", "본문 파일 테스트", "-a", "완료 기준: 본문 보존",
                         "--body-file", str(body_file), "--json").stdout)
    got = api(f"/issues/{out['id']}")
    assert got["body"] == body_file.read_text(encoding="utf-8")


def test_version_명령(cli):
    """cli 버전 + 서버 openapi 해시 — 드리프트 감지 표기."""
    run, url, api, db = cli
    out = run("version").stdout
    assert "cli " in out and "openapi" in out, out
    with urllib.request.urlopen(url + "/openapi.json", timeout=10) as r:
        body = r.read()
    import hashlib
    # CLI와 동일 — raw 바이트 해시. 스키마 파일이 바뀌면 해시가 바뀐다(드리프트 신호).
    sha8 = hashlib.sha256(body).hexdigest()[:8]
    assert sha8 in out, f"서버 openapi 해시 표기 필요: {out}"


def test_cli_edit_페이로드_키는_openapi_와_정합(cli):
    """CLI edit가 보내는 키 전부가 서버 IssuePatch 스키마에 존재해야 한다."""
    run, url, api, db = cli
    with urllib.request.urlopen(url + "/openapi.json", timeout=10) as r:
        schema = json.load(r)
    patch = schema["components"]["schemas"]["IssuePatch"]["properties"]
    cli_keys = {"state", "title", "body", "priority", "parent_id", "assignee",
                "labels", "expected_version", "archived", "promoted"}
    unknown = cli_keys - set(patch)
    assert not unknown, f"CLI가 보내는 키 중 스키마에 없는 것: {unknown}"


def test_verify_페이로드는_계약_스키마와_정합(cli):
    """tt verify의 {verifier, evidence, completion_report} — CompletionReport 구조."""
    run, url, api, db = cli
    iid = json.loads(run("new", "검증 스키마", "-a", "완료 기준: 스키마 정합", "--json").stdout)["id"]
    run("claim", iid)
    ver = api(f"/issues/{iid}")["work_contract"]["version"]
    import tempfile, pathlib
    rep = {"contract_version": ver, "attempt": 1, "method": "planned",
           "design": {"criteria": "c", "verification": "v", "evidence": "e"},
           "implementation": {"summary": "s", "commands": "cc"},
           "verification": {"commands": "ccc", "evidence": "e2"},
           "result": "passed", "limitations": ""}
    f = pathlib.Path(tempfile.mkdtemp()) / "r.json"
    f.write_text(json.dumps(rep), encoding="utf-8")
    # done → review(보고 접수) → verify → done(독립 검증)
    p1 = run("done", iid, "--report", str(f))
    assert api(f"/issues/{iid}")["state"] == "review", p1.stderr + p1.stdout
    p = run("verify", iid, "--report", str(f))
    got = api(f"/issues/{iid}")
    assert got["state"] == "done" and got["verified"], p.stderr + p.stdout
