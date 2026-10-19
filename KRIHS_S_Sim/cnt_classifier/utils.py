import math
from typing import Dict, List, Optional

from .models import AnalysisConfig, InfraStatType, StatType

# 통계 타입 → 출력 필드 접미사 (shp 필드명 10자 제한에 맞춘 축약형)
STAT_SUFFIX: Dict[StatType, str] = {
    StatType.SUM: "sum",
    StatType.MAX: "max",
    StatType.MIN: "min",
    StatType.AVG: "avg",
    StatType.DENSITY: "den",
}

INFRA_STAT_SUFFIX: Dict[InfraStatType, str] = {
    InfraStatType.AVG: "avg",
    InfraStatType.MAX: "max",
    InfraStatType.MIN: "min",
    InfraStatType.AVG_RATIO: "avgr",
    InfraStatType.MAX_RATIO: "maxr",
    InfraStatType.MIN_RATIO: "minr",
}

STAT_LABEL: Dict[StatType, str] = {
    StatType.SUM: "총계",
    StatType.MAX: "최대",
    StatType.MIN: "최소",
    StatType.AVG: "평균",
    StatType.DENSITY: "밀도",
}

INFRA_STAT_LABEL: Dict[InfraStatType, str] = {
    InfraStatType.AVG: "평균",
    InfraStatType.MAX: "최대",
    InfraStatType.MIN: "최소",
    InfraStatType.AVG_RATIO: "평균 비율",
    InfraStatType.MAX_RATIO: "최대 비율",
    InfraStatType.MIN_RATIO: "최소 비율",
}


def build_output_field_names(config: AnalysisConfig) -> List[str]:
    """Tab 3 필드 드롭다운 및 출력 스키마의 단일 소스."""
    names: List[str] = []
    gcfg = config.geojeom_cfg

    pop_fields = [
        ("field_resident_pop", "rpop", "res_pop_stats"),
        ("field_work_pop", "wpop", "wor_pop_stats"),
    ]
    centrality_fields = [
        ("field_inflow", "pcin", "inflow_stats"),
        ("field_outflow", "pcout", "outflow_stats"),
    ]

    for field_attr, base_key, stats_attr in pop_fields:
        if getattr(gcfg, field_attr):
            for stat in getattr(gcfg, stats_attr):
                names.append(f"{base_key}_{STAT_SUFFIX[stat]}")

    for field_attr, base_key, stats_attr in centrality_fields:
        if getattr(gcfg, field_attr):
            for stat in getattr(gcfg, stats_attr):
                names.append(f"{base_key}_{STAT_SUFFIX[stat]}")

    icfg = config.infra_cfg
    agg_items = []
    if icfg.compute_total:
        agg_items.append("tfac")
    if icfg.compute_village:
        agg_items.append("vfac")
    if icfg.compute_base:
        agg_items.append("bfac")

    for base_key in agg_items:
        for stat in icfg.stats:
            names.append(f"{base_key}_{INFRA_STAT_SUFFIX[stat]}")

    return names


def compute_stats(values: List[float], stats: List[StatType], area: float = 1.0) -> Dict[str, float]:
    """값 목록에서 요청된 통계를 계산. DENSITY는 sum/area 소수점 3자리 라운드업."""
    if not values:
        return {STAT_SUFFIX[s]: 0.0 for s in stats}

    result: Dict[str, float] = {}
    total = sum(values)

    for s in stats:
        if s == StatType.SUM:
            result["sum"] = total
        elif s == StatType.MAX:
            result["max"] = max(values)
        elif s == StatType.MIN:
            result["min"] = min(values)
        elif s == StatType.AVG:
            result["avg"] = total / len(values)
        elif s == StatType.DENSITY:
            # 면적: m² → km² 변환 후 총계/면적, 소수점 2자리 반올림
            area_km2 = area / 1_000_000 if area > 0 else 0.0
            raw = total / area_km2 if area_km2 > 0 else 0.0
            result["den"] = round(raw, 2)

    return result


def compute_infra_stats(
    values: List[float],
    stats: List[InfraStatType],
    n_cols: int,
) -> Dict[str, float]:
    """인프라 격자별 합계 목록에서 요청된 통계를 계산. ratio = stat / n_cols."""
    if not values:
        return {INFRA_STAT_SUFFIX[s]: 0.0 for s in stats}

    avg_val = sum(values) / len(values)
    max_val = max(values)
    min_val = min(values)
    denom = max(n_cols, 1)

    result: Dict[str, float] = {}
    for s in stats:
        if s == InfraStatType.AVG:
            result["avg"] = avg_val
        elif s == InfraStatType.MAX:
            result["max"] = max_val
        elif s == InfraStatType.MIN:
            result["min"] = min_val
        elif s == InfraStatType.AVG_RATIO:
            result["avgr"] = avg_val / denom
        elif s == InfraStatType.MAX_RATIO:
            result["maxr"] = max_val / denom
        elif s == InfraStatType.MIN_RATIO:
            result["minr"] = min_val / denom
    return result


def safe_float(value) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
