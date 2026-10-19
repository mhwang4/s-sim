from dataclasses import dataclass, field
from enum import Enum
from typing import List


class StatType(Enum):
    SUM = "sum"
    MAX = "max"
    MIN = "min"
    AVG = "avg"
    DENSITY = "density"  # 총계 / 폴리곤 면적(km²)


class InfraStatType(Enum):
    AVG = "avg"
    MAX = "max"
    MIN = "min"
    AVG_RATIO = "avg_ratio"
    MAX_RATIO = "max_ratio"
    MIN_RATIO = "min_ratio"


class Operator(Enum):
    GTE = ">="
    LTE = "<="
    EQ = "="
    GT = ">"
    LT = "<"


def _pop_stats():
    return [StatType.SUM, StatType.MAX, StatType.MIN, StatType.AVG, StatType.DENSITY]

def _centrality_stats():
    return [StatType.MAX, StatType.MIN, StatType.AVG]

def _infra_stats():
    return [
        InfraStatType.AVG, InfraStatType.MAX, InfraStatType.MIN,
        InfraStatType.AVG_RATIO, InfraStatType.MAX_RATIO, InfraStatType.MIN_RATIO,
    ]


@dataclass
class GeojeomConfig:
    field_resident_pop: str = ""
    field_work_pop: str = ""
    field_inflow: str = ""
    field_outflow: str = ""
    res_pop_stats: List[StatType] = field(default_factory=_pop_stats)
    wor_pop_stats: List[StatType] = field(default_factory=_pop_stats)
    inflow_stats: List[StatType] = field(default_factory=_centrality_stats)
    outflow_stats: List[StatType] = field(default_factory=_centrality_stats)


@dataclass
class InfraConfig:
    village_cols: List[str] = field(default_factory=list)
    base_cols: List[str] = field(default_factory=list)
    compute_total: bool = True
    compute_village: bool = True
    compute_base: bool = True
    stats: List[InfraStatType] = field(default_factory=_infra_stats)


@dataclass
class ClassifyConfig:
    # 생활중심지 조건
    living_field: str = "total_fac_avg_ratio"
    living_op: Operator = Operator.GTE
    living_threshold: float = 0.20
    # 지역중심지 추가 조건 (생활중심지 중 적용)
    regional_field: str = "base_fac_avg_ratio"
    regional_op: Operator = Operator.GTE
    regional_threshold: float = 0.50
    # 광역중심지 추가 조건 - AND 로직 (지역중심지 중 적용)
    metro_field1: str = "res_pop_sum"
    metro_op1: Operator = Operator.GTE
    metro_threshold1: float = 50000.0
    metro_field2: str = "wor_pop_sum"
    metro_op2: Operator = Operator.GTE
    metro_threshold2: float = 50000.0


@dataclass
class AnalysisConfig:
    # Tab 1: 파일 경로
    center_layer_path: str = ""
    geojeom_layer_path: str = ""
    infra_layer_path: str = ""
    emd_layer_path: str = ""   # Tab 3: 읍면동 경계 (선택 사항)
    sgg_layer_path: str = ""   # Tab 3: 시군구 경계 (선택 사항)
    # Tab 2: 분석 설정 + 출력 경로
    geojeom_cfg: GeojeomConfig = field(default_factory=GeojeomConfig)
    infra_cfg: InfraConfig = field(default_factory=InfraConfig)
    output_path: str = ""
    # Tab 3: 분류 설정
    classify_cfg: ClassifyConfig = field(default_factory=ClassifyConfig)
