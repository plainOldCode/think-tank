"""server/ontology — 엔티티 정본 카탈로그 패키지 (XKRY, G1DD).

스키마 버전은 schema.py의 SCHEMA_VERSION — 그래프 소비자는 graph_compatible()로
호환을 확인한 뒤 사용한다(fail-to-rules)."""
from .schema import SCHEMA_VERSION, graph_compatible

__all__ = ("SCHEMA_VERSION", "graph_compatible")
