"""온톨로지 엔티티 스키마 정본 (XKRY 카탈로그, G1DD).

"Treat the ontology as code" — 스키마 변경은 PR + 골든 비교 + CHANGELOG 행.
SCHEMA_VERSION을 올리는 변경: 카탈로그 투영 필드 추가/제거, 엔티티 kind 추가,
정규화 규칙 변경. 그래프 소비자는 graph_compatible()로 호환을 확인한 뒤
사용하고, 불일치면 그래프를 쓰지 않고 규칙 기본 경로로 간다(fail-to-rules).

판단 로그(judge.py)의 schema_version도 이 상수에서 온다 — 로그가 어떤
스키마의 그래프를 전제한 결정인지 사후 추적이 가능하게 한다(G1DD 수용기준 3).
"""
SCHEMA_VERSION = 2


def graph_compatible(version):
    """그래프 산출물의 버전이 현재 스키마와 같은지. 불일치면 소비 금지."""
    return version == SCHEMA_VERSION
