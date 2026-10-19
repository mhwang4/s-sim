from typing import Dict

from .models import AnalysisConfig, ClassifyConfig, Operator
from .utils import safe_float


def _compare(value: float, op: Operator, threshold: float) -> bool:
    if op == Operator.GTE:
        return value >= threshold
    elif op == Operator.LTE:
        return value <= threshold
    elif op == Operator.EQ:
        return value == threshold
    elif op == Operator.GT:
        return value > threshold
    elif op == Operator.LT:
        return value < threshold
    return False


class ConditionEvaluator:
    """중심지 분류 엔진.
    - 광역중심지: 생활중심지 O + 지역중심지 O + 광역중심지 AND조건 O
    - 지역중심지: 생활중심지 O + 지역중심지 O + 광역중심지 AND조건 X
    - 생활중심지: 생활중심지 O + 지역중심지 X
    - 이외: 생활중심지 X
    """

    def __init__(self, config: AnalysisConfig) -> None:
        self.cfg: ClassifyConfig = config.classify_cfg

    def classify(self, attributes: Dict) -> str:
        cfg = self.cfg

        # 1단계: 생활중심지 조건
        living_val = safe_float(attributes.get(cfg.living_field, 0.0))
        if not _compare(living_val, cfg.living_op, cfg.living_threshold):
            return "이외"

        # 2단계: 지역중심지 추가 조건
        regional_val = safe_float(attributes.get(cfg.regional_field, 0.0))
        if not _compare(regional_val, cfg.regional_op, cfg.regional_threshold):
            return "생활중심지"

        # 3단계: 광역중심지 AND 조건
        metro_val1 = safe_float(attributes.get(cfg.metro_field1, 0.0))
        metro_val2 = safe_float(attributes.get(cfg.metro_field2, 0.0))
        is_metro = (
            _compare(metro_val1, cfg.metro_op1, cfg.metro_threshold1) and
            _compare(metro_val2, cfg.metro_op2, cfg.metro_threshold2)
        )
        return "광역중심지" if is_metro else "지역중심지"
