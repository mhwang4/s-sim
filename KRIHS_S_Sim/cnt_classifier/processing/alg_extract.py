from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterEnum,
    QgsProcessingParameterField,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsVectorLayer,
)

from ..models import AnalysisConfig, GeojeomConfig, InfraConfig, InfraStatType, StatType
from ..processor import AnalysisCancelledError, SpatialProcessor
from ..utils import build_output_field_names

# ---- 열거형 옵션 정의 ---------------------------------------------------- #

_POP_STAT_OPTIONS = ["총계(SUM)", "최대(MAX)", "최소(MIN)", "평균(AVG)", "밀도(DENSITY)"]
_POP_STAT_TYPES   = [StatType.SUM, StatType.MAX, StatType.MIN, StatType.AVG, StatType.DENSITY]

_CENT_STAT_OPTIONS = ["최대(MAX)", "최소(MIN)", "평균(AVG)"]
_CENT_STAT_TYPES   = [StatType.MAX, StatType.MIN, StatType.AVG]

_INFRA_AGG_OPTIONS  = ["전체시설(tfac)", "마을시설(vfac)", "거점시설(bfac)"]
_INFRA_STAT_OPTIONS = [
    "평균(AVG)", "최대(MAX)", "최소(MIN)",
    "평균비율(AVG_RATIO)", "최대비율(MAX_RATIO)", "최소비율(MIN_RATIO)",
]
_INFRA_STAT_TYPES = [
    InfraStatType.AVG, InfraStatType.MAX, InfraStatType.MIN,
    InfraStatType.AVG_RATIO, InfraStatType.MAX_RATIO, InfraStatType.MIN_RATIO,
]

try:
    from qgis.core import QgsProcessingParameterSeparator as _QgsSep
    def _sep(name: str, label: str):
        return _QgsSep(name, label)
except ImportError:
    _sep = None


# ──────────────────────────────────────────────────────────────────────────── #
# 로그 포맷 헬퍼                                                                #
# ──────────────────────────────────────────────────────────────────────────── #

def _bar(char: str = "━", width: int = 56) -> str:
    return char * width

def _section(title: str) -> str:
    """굵은 구분선 + 제목 (주요 단계)."""
    return f"\n{_bar()}\n  {title}\n{_bar()}"

def _item(label: str, value: str, indent: int = 2) -> str:
    pad = " " * indent
    return f"{pad}  {label} : {value}"


class ExtractCenterAttributesAlgorithm(QgsProcessingAlgorithm):
    """중심지 후보(도형) + 격자 2종 → 통계 속성 GeoPackage 생성."""

    CENTER_LAYER  = "CENTER_LAYER"
    GEOJEOM_LAYER = "GEOJEOM_LAYER"
    INFRA_LAYER   = "INFRA_LAYER"

    FIELD_RES_POP = "FIELD_RES_POP"
    FIELD_WOR_POP = "FIELD_WOR_POP"
    FIELD_INFLOW  = "FIELD_INFLOW"
    FIELD_OUTFLOW = "FIELD_OUTFLOW"

    RES_POP_STATS = "RES_POP_STATS"
    WOR_POP_STATS = "WOR_POP_STATS"
    INFLOW_STATS  = "INFLOW_STATS"
    OUTFLOW_STATS = "OUTFLOW_STATS"

    VILLAGE_COLS = "VILLAGE_COLS"
    BASE_COLS    = "BASE_COLS"
    INFRA_AGG    = "INFRA_AGG"
    INFRA_STATS  = "INFRA_STATS"

    OUTPUT = "OUTPUT"

    # 다음 도구(3. 중심지 및 위계 설정)의 기본 분류식이 참조하는 필드
    _NEXT_TOOL_FIELDS = ("tfac_avg", "bfac_avg", "rpop_sum", "wpop_sum")

    # ------------------------------------------------------------------ #
    # 메타데이터                                                           #
    # ------------------------------------------------------------------ #

    def name(self) -> str:
        return "extract_center_attributes"

    def displayName(self) -> str:
        return "2. 중심지 후보 속성 추출_update"

    def group(self) -> str:
        return "1. 중심지 분석"

    def groupId(self) -> str:
        return "center_analysis"

    def shortHelpString(self) -> str:
        return (
            "'1. 중심지 후보 추출_update'이 만든 후보 폴리곤에는 도형만 있고 속성이 없습니다. "
            "이 도구는 각 후보 폴리곤과 공간적으로 겹치는 격자 셀들을 찾아 "
            "인구·중심성·인프라 통계를 계산해 속성으로 붙여줍니다.\n"
            "여기서 만든 통계 필드가 '3. 중심지 및 위계 설정_update'의 분류 조건 재료가 됩니다.\n\n"
            "【입력】\n"
            "  · 중심지 후보 폴리곤 : '1. 중심지 후보 추출_update'의 통합 출력\n"
            "  · 국토공간거점지도(1km 격자) : 인구·중심성 값의 출처\n"
            "    (거주인구 pop_r / 근무인구 pop_w / 유입 pc_in / 유출 pc_out)\n"
            "  · 생활인프라충족도(500m 격자) : 시설 충족여부 열(예: cli_ox 등 *_ox)의 출처\n\n"
            "【처리 단계】\n"
            "  1) 설정 확인 — 생성될 출력 필드 목록을 로그에 표시\n"
            "  2) 후보 폴리곤별 교차 격자 탐색(공간 인덱스) 및 통계 계산\n"
            "  3) Shapefile 저장\n\n"
            "【출력 필드 명명 규칙】  {대상}_{통계}  (모두 10자 이내 — shp 호환)\n"
            "  · 대상 : rpop(거주인구) wpop(근무인구) pcin(유입) pcout(유출)\n"
            "           tfac(마을+거점 전체시설) vfac(마을시설) bfac(거점시설)\n"
            "  · 통계 : sum(합계) max(최대) min(최소) avg(평균)\n"
            "           den(밀도 = 합계 ÷ 폴리곤 면적 km²)\n"
            "           avgr·maxr·minr(비율 = 통계값 ÷ 선택한 시설 열 개수)\n"
            "  · 예 : rpop_sum(거주인구 합계), tfac_avg(전체시설 충족 평균)\n\n"
            "【다음 도구와의 연계】\n"
            "  '3. 중심지 및 위계 설정_update'의 기본 분류식은 다음 4개 필드를 참조합니다:\n"
            "  rpop_den / rpop_sum / wpop_den / wpop_sum\n"
            "  → 기본 설정 그대로 실행하면 모두 생성됩니다.\n\n"
            "【참고】\n"
            "  · 필드를 비우거나 통계를 해제하면 해당 통계는 생략됩니다.\n\n"
            "이전 도구: 1. 중심지 후보 추출_update  /  다음 도구: 3. 중심지 및 위계 설정_update"
        )

    def createInstance(self):
        return ExtractCenterAttributesAlgorithm()

    # ------------------------------------------------------------------ #
    # 파라미터 정의                                                        #
    # ------------------------------------------------------------------ #

    def initAlgorithm(self, config=None) -> None:
        # ── 입력 데이터 ──────────────────────────────────────────────
        if _sep: self.addParameter(_sep("SEP_INPUT", "입력 데이터"))

        self.addParameter(QgsProcessingParameterVectorLayer(
            self.CENTER_LAYER, "중심지 후보 폴리곤 레이어 ('1. 중심지 후보 추출_update' 통합 출력)",
            types=[QgsProcessing.TypeVectorPolygon],
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.GEOJEOM_LAYER, "국토공간거점지도 레이어 (1km 격자 — 인구·중심성 출처)",
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INFRA_LAYER, "생활인프라충족도 레이어 (500m 격자 — 시설 충족여부 출처)",
        ))

        # ── 인구·중심성 통계 설정 ────────────────────────────────────
        if _sep: self.addParameter(_sep(
            "SEP_GEOJEOM", "인구·중심성 통계 설정 — 국토공간거점지도 기반 (필드를 비우면 해당 통계 생략)"))

        for field_key, stats_key, label, prefix, default, stat_options, stat_default in [
            (self.FIELD_RES_POP, self.RES_POP_STATS, "거주인구", "rpop", "pop_r",
             _POP_STAT_OPTIONS, list(range(len(_POP_STAT_OPTIONS)))),
            (self.FIELD_WOR_POP, self.WOR_POP_STATS, "근무인구", "wpop", "pop_w",
             _POP_STAT_OPTIONS, list(range(len(_POP_STAT_OPTIONS)))),
            (self.FIELD_INFLOW, self.INFLOW_STATS, "유입중심성", "pcin", "pc_in",
             _CENT_STAT_OPTIONS, list(range(len(_CENT_STAT_OPTIONS)))),
            (self.FIELD_OUTFLOW, self.OUTFLOW_STATS, "유출중심성", "pcout", "pc_out",
             _CENT_STAT_OPTIONS, list(range(len(_CENT_STAT_OPTIONS)))),
        ]:
            self.addParameter(QgsProcessingParameterField(
                field_key, f"{label} 필드 (기본: {default})",
                defaultValue=default,
                parentLayerParameterName=self.GEOJEOM_LAYER,
                type=QgsProcessingParameterField.Numeric,
                optional=True,
            ))
            self.addParameter(QgsProcessingParameterEnum(
                stats_key, f"   ↳ {label} 통계 → 출력 필드 {prefix}_*",
                options=stat_options,
                allowMultiple=True,
                defaultValue=stat_default,
            ))

        # ── 인프라 통계 설정 ─────────────────────────────────────────
        if _sep: self.addParameter(_sep(
            "SEP_INFRA", "인프라 통계 설정 — 생활인프라충족도 기반 (열을 비우면 해당 통계 생략)"))

        self.addParameter(QgsProcessingParameterField(
            self.VILLAGE_COLS, "마을시설 열 (복수 선택 — 예: 어린이집·약국 등 *_ox 열)",
            parentLayerParameterName=self.INFRA_LAYER,
            type=QgsProcessingParameterField.Any,
            allowMultiple=True,
            optional=True,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.BASE_COLS, "거점시설 열 (복수 선택 — 예: 병원·도서관 등 *_ox 열)",
            parentLayerParameterName=self.INFRA_LAYER,
            type=QgsProcessingParameterField.Any,
            allowMultiple=True,
            optional=True,
        ))
        self.addParameter(QgsProcessingParameterEnum(
            self.INFRA_AGG, "인프라 집계 항목 (전체시설 = 마을시설 + 거점시설 열 합산)",
            options=_INFRA_AGG_OPTIONS,
            allowMultiple=True,
            defaultValue=[0, 1, 2],
        ))
        self.addParameter(QgsProcessingParameterEnum(
            self.INFRA_STATS, "   ↳ 인프라 통계 (비율 = 통계값 ÷ 선택한 열 개수)",
            options=_INFRA_STAT_OPTIONS,
            allowMultiple=True,
            defaultValue=[0],
        ))

        # ── 출력 ─────────────────────────────────────────────────────
        if _sep: self.addParameter(_sep("SEP_OUTPUT", "출력"))

        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT, "출력 Shapefile",
            fileFilter="ESRI Shapefile (*.shp)",
        ))

    # ------------------------------------------------------------------ #
    # 실행                                                                 #
    # ------------------------------------------------------------------ #

    def processAlgorithm(self, parameters, context, feedback):
        center_layer  = self.parameterAsVectorLayer(parameters, self.CENTER_LAYER,  context)
        geojeom_layer = self.parameterAsVectorLayer(parameters, self.GEOJEOM_LAYER, context)
        infra_layer   = self.parameterAsVectorLayer(parameters, self.INFRA_LAYER,   context)

        output_path = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        if not output_path.lower().endswith(".shp"):
            output_path += ".shp"

        # GeojeomConfig 구성
        gcfg = GeojeomConfig(
            field_resident_pop=self.parameterAsString(parameters, self.FIELD_RES_POP, context) or "",
            field_work_pop    =self.parameterAsString(parameters, self.FIELD_WOR_POP, context) or "",
            field_inflow      =self.parameterAsString(parameters, self.FIELD_INFLOW,  context) or "",
            field_outflow     =self.parameterAsString(parameters, self.FIELD_OUTFLOW, context) or "",
            res_pop_stats =[_POP_STAT_TYPES[i]  for i in self.parameterAsEnums(parameters, self.RES_POP_STATS,  context)],
            wor_pop_stats =[_POP_STAT_TYPES[i]  for i in self.parameterAsEnums(parameters, self.WOR_POP_STATS,  context)],
            inflow_stats  =[_CENT_STAT_TYPES[i] for i in self.parameterAsEnums(parameters, self.INFLOW_STATS,   context)],
            outflow_stats =[_CENT_STAT_TYPES[i] for i in self.parameterAsEnums(parameters, self.OUTFLOW_STATS,  context)],
        )

        # InfraConfig 구성
        agg_indices  = self.parameterAsEnums(parameters, self.INFRA_AGG,   context)
        stat_indices = self.parameterAsEnums(parameters, self.INFRA_STATS, context)
        village_cols = self.parameterAsFields(parameters, self.VILLAGE_COLS, context)
        base_cols    = self.parameterAsFields(parameters, self.BASE_COLS,    context)
        icfg = InfraConfig(
            village_cols   =village_cols,
            base_cols      =base_cols,
            compute_total  =(0 in agg_indices),
            compute_village=(1 in agg_indices),
            compute_base   =(2 in agg_indices),
            stats          =[_INFRA_STAT_TYPES[i] for i in stat_indices],
        )

        config = AnalysisConfig(
            center_layer_path =center_layer.source(),
            geojeom_layer_path=geojeom_layer.source(),
            infra_layer_path  =infra_layer.source(),
            output_path       =output_path,
            geojeom_cfg       =gcfg,
            infra_cfg         =icfg,
        )

        # ── [1단계] 설정 확인 ────────────────────────────────────────
        feedback.pushInfo(_section("[1단계] 설정 확인"))
        feedback.pushInfo(_item("후보 폴리곤", f"{center_layer.name()} ({center_layer.featureCount():,}개)"))
        feedback.pushInfo(_item("거점지도 격자", f"{geojeom_layer.name()} ({geojeom_layer.featureCount():,}개)"))
        feedback.pushInfo(_item("인프라 격자", f"{infra_layer.name()} ({infra_layer.featureCount():,}개)"))
        feedback.pushInfo(_item("마을시설 열", f"{len(village_cols)}개 선택"))
        feedback.pushInfo(_item("거점시설 열", f"{len(base_cols)}개 선택"))

        # 생략 항목 안내
        skipped = [label for label, fname in [
            ("거주인구", gcfg.field_resident_pop), ("근무인구", gcfg.field_work_pop),
            ("유입중심성", gcfg.field_inflow), ("유출중심성", gcfg.field_outflow),
        ] if not fname]
        if skipped:
            feedback.pushWarning(f"  필드 미지정으로 생략: {', '.join(skipped)}")

        # 인프라 집계를 선택했으나 열이 없는 경우
        if (icfg.compute_total or icfg.compute_village or icfg.compute_base) \
                and not village_cols and not base_cols:
            feedback.pushWarning("  시설 열이 선택되지 않아 인프라 통계를 생략합니다.")
            icfg.compute_total = icfg.compute_village = icfg.compute_base = False
        if icfg.compute_village and not village_cols:
            feedback.pushWarning("  마을시설 열 미선택 → vfac 통계 생략")
            icfg.compute_village = False
        if icfg.compute_base and not base_cols:
            feedback.pushWarning("  거점시설 열 미선택 → bfac 통계 생략")
            icfg.compute_base = False

        # 생성될 출력 필드 목록
        planned = build_output_field_names(config)
        if not planned:
            raise QgsProcessingException(
                "생성할 통계가 없습니다.\n"
                "인구·중심성 필드를 지정하거나, 시설 열과 집계 항목을 선택하세요."
            )
        feedback.pushInfo("")
        feedback.pushInfo(f"  생성될 출력 필드 ({len(planned)}개):")
        for fname in planned:
            feedback.pushInfo(f"    · {fname}")

        # 다음 도구(위계 설정) 기본 분류식이 쓰는 필드 확인
        missing_next = [f for f in self._NEXT_TOOL_FIELDS if f not in planned]
        if missing_next:
            feedback.pushWarning(
                f"  주의: '3. 중심지 및 위계 설정_update' 기본 분류식이 참조하는 "
                f"{', '.join(missing_next)} 필드가 생성되지 않습니다.\n"
                f"  (해당 도구에서 분류식을 직접 수정할 경우에만 무시하세요)"
            )

        # ── [2단계] 공간 교차 분석 ───────────────────────────────────
        feedback.pushInfo(_section("[2단계] 공간 교차 분석 및 통계 계산"))

        processor = SpatialProcessor(config)

        def progress_cb(pct: int, msg: str) -> None:
            feedback.setProgress(pct)
            feedback.pushInfo(f"  {msg}")
            if feedback.isCanceled():
                processor.cancel_requested = True

        try:
            processor.execute(progress_callback=progress_cb)
        except AnalysisCancelledError:
            feedback.pushWarning("분석이 취소되었습니다.")
            return {}
        except ValueError as e:
            raise QgsProcessingException(str(e))

        # ── [3단계] 저장 완료 ────────────────────────────────────────
        feedback.pushInfo(_section("[3단계] 저장 완료"))
        feedback.pushInfo(_item("저장 위치", output_path))
        feedback.pushInfo("  다음 도구: '3. 중심지 및 위계 설정_update'에 이 출력을 입력하세요.")

        self._output_path = output_path
        return {self.OUTPUT: output_path}

    def postProcessAlgorithm(self, context, feedback):
        output_path = getattr(self, "_output_path", None)
        if not output_path:
            return {}

        layer = QgsVectorLayer(output_path, "중심지 후보 속성 추가", "ogr")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
        else:
            feedback.pushWarning("출력 레이어를 지도 뷰에 추가하지 못했습니다.")

        return {}
