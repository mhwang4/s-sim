"""
중심지 후보 추출 알고리즘.
국토공간거점지도 격자에서 버퍼·음의 버퍼·단일파트 분리를 거쳐
중심지 후보 폴리곤을 자동 생성한다.
"""

from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from PyQt5.QtCore import QVariant
from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
    QgsProcessingUtils,
    QgsProject,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsWkbTypes,
)

from ..utils import safe_float

# 버퍼 스타일 상수
# endCapStyle: 0=Round, 1=Flat, 2=Square
# joinStyle:   0=Round, 1=Miter, 2=Bevel
try:
    from qgis.core import Qgis
    _CAP_SQUARE = Qgis.EndCapStyle.Square
    _JOIN_MITER = Qgis.JoinStyle.Miter
except AttributeError:
    _CAP_SQUARE = 2  # Square
    _JOIN_MITER = 1  # Miter

_CENTER_TYPES = {"중심지Ⅰ", "중심지Ⅱ"}

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

def _subsection(title: str) -> str:
    """가는 구분선 + 제목 (세부 단계)."""
    return f"\n  {'─' * 52}\n  {title}\n  {'─' * 52}"

def _item(label: str, value: str, indent: int = 2) -> str:
    pad = " " * indent
    return f"{pad}  {label} : {value}"

def _fmt_n(n: int) -> str:
    return f"{n:,}"


class ExtractCandidatesAlgorithm(QgsProcessingAlgorithm):
    """국토공간거점지도 격자에서 중심지 후보 폴리곤 추출."""

    GEOJEOM_LAYER   = "GEOJEOM_LAYER"
    TYPE_FIELD      = "TYPE_FIELD"
    POP_FIELD       = "POP_FIELD"
    BUFFER_DISTANCE = "BUFFER_DISTANCE"
    MERGE_TOUCHING  = "MERGE_TOUCHING"
    POP_THRESHOLD   = "POP_THRESHOLD"
    EMD_LAYER       = "EMD_LAYER"
    EMD_NAME_FIELD  = "EMD_NAME_FIELD"
    DEDUP_BY_EMD    = "DEDUP_BY_EMD"
    OUTPUT_GROUP1   = "OUTPUT_GROUP1"
    OUTPUT_GROUP2   = "OUTPUT_GROUP2"
    OUTPUT          = "OUTPUT"

    # ------------------------------------------------------------------ #
    # 메타데이터                                                           #
    # ------------------------------------------------------------------ #

    def name(self) -> str:
        return "extract_candidates"

    def displayName(self) -> str:
        return "1. 중심지 후보 추출_update"

    def group(self) -> str:
        return "1. 중심지 분석"

    def groupId(self) -> str:
        return "center_analysis"

    def shortHelpString(self) -> str:
        return (
            "국토공간거점지도 격자에서 중심지 후보 폴리곤을 추출합니다.\n\n"
            "【후보그룹1】 중심지 유형 필드가 '중심지Ⅰ' 또는 '중심지Ⅱ'인 격자\n"
            "【후보그룹2】 나머지 격자 중 거주인구밀도 임계값 이상이며 "
            "후보그룹1 결과물의 1km 버퍼 밖에 있는 격자\n\n"
            "처리 파이프라인 (두 후보그룹 공통):\n"
            "  ① 버퍼 (정사각형 끝, 마이터 이음새, 디졸브)\n"
            "  ② 음의 버퍼 (동일 스타일)\n"
            "  ③ 다중파트 → 단일파트 분리\n"
            "  ④ 점 접촉 폴리곤 병합 (옵션)\n\n"
            "출력: 후보그룹1·후보그룹2·통합 레이어 3종\n\n"
            "중심지 후보 이름(cand_nm) = 후보 안 주거인구가 가장 많은 읍면동. "
            "필요하면 속성 테이블에서 직접 수정하세요.\n\n"
            "출력 필드: cand_id(후보 ID) / cand_nm(후보 이름) / cand_grp(후보 구분)"
        )

    def createInstance(self):
        return ExtractCandidatesAlgorithm()

    # ------------------------------------------------------------------ #
    # 파라미터 정의                                                        #
    # ------------------------------------------------------------------ #

    def initAlgorithm(self, config=None) -> None:
        # ── 입력 레이어 ───────────────────────────────────────────────
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.GEOJEOM_LAYER, "국토공간거점지도 레이어 (1km 격자 — 중심지 유형·거주인구밀도 출처)",
        ))

        # ── 후보그룹1 설정 ────────────────────────────────────────────────
        if _sep: self.addParameter(_sep("SEP_GROUP1", "후보그룹1 설정 — 중심지 유형(중심지Ⅰ·중심지Ⅱ) 기반"))
        self.addParameter(QgsProcessingParameterField(
            self.TYPE_FIELD, "중심지 유형 필드 (값: '중심지Ⅰ', '중심지Ⅱ')",
            defaultValue="type",
            parentLayerParameterName=self.GEOJEOM_LAYER,
            type=QgsProcessingParameterField.Any,
        ))

        # ── 후보그룹2 설정 ────────────────────────────────────────────────
        if _sep: self.addParameter(_sep("SEP_GROUP2", "후보그룹2 설정 — 거주인구밀도 기반 (후보그룹1 1km 밖 격자)"))
        self.addParameter(QgsProcessingParameterField(
            self.POP_FIELD, "거주인구밀도 필드",
            defaultValue="pop_r",
            parentLayerParameterName=self.GEOJEOM_LAYER,
            type=QgsProcessingParameterField.Numeric,
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.POP_THRESHOLD, "거주인구밀도 임계값 (이상인 격자만 포함)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=150.0,
            minValue=0.0,
        ))

        # ── 버퍼 처리 옵션 (후보그룹1·후보그룹2 공통) ───────────────────────
        if _sep: self.addParameter(_sep("SEP_BUFFER", "버퍼 처리 옵션 (후보그룹1·후보그룹2 공통)"))
        self.addParameter(QgsProcessingParameterNumber(
            self.BUFFER_DISTANCE, "버퍼 거리 (m)",
            type=QgsProcessingParameterNumber.Double,
            defaultValue=100.0,
            minValue=0.0,
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.MERGE_TOUCHING, "점 접촉 폴리곤 병합",
            defaultValue=True,
        ))

        # ── 읍면동 설정 (선택) ────────────────────────────────────────
        if _sep: self.addParameter(_sep("SEP_EMD", "읍면동 설정 (선택 — 이름 부여·중복 제거)"))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.EMD_LAYER, "읍면동 경계 레이어 (통계청 읍면동 경계 폴리곤 — 예: bnd_dong)",
            types=[QgsProcessing.TypeVectorPolygon],
            optional=True,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.EMD_NAME_FIELD, "읍면동 명칭 필드",
            defaultValue="emd_nm",
            parentLayerParameterName=self.EMD_LAYER,
            type=QgsProcessingParameterField.Any,
            optional=True,
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.DEDUP_BY_EMD,
            "읍면동별 중복 제거 (읍면동 내 거주인구 합계 최대 폴리곤 1개만 유지)",
            defaultValue=True,
        ))

        # ── 출력 ──────────────────────────────────────────────────────
        if _sep: self.addParameter(_sep("SEP_OUTPUT", "출력"))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_GROUP1, "후보그룹1 출력 (중심지Ⅰ·중심지Ⅱ 기반)",
            type=QgsProcessing.TypeVectorPolygon,
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_GROUP2, "후보그룹2 출력 (거주인구밀도 기반)",
            type=QgsProcessing.TypeVectorPolygon,
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, "통합 출력 (후보그룹1+후보그룹2)",
            type=QgsProcessing.TypeVectorPolygon,
        ))

    # ------------------------------------------------------------------ #
    # 실행                                                                 #
    # ------------------------------------------------------------------ #

    def processAlgorithm(
        self,
        parameters: dict,
        context: QgsProcessingContext,
        feedback: QgsProcessingFeedback,
    ) -> dict:
        # ── 파라미터 읽기 ──────────────────────────────────────────────
        geojeom_layer  = self.parameterAsVectorLayer(parameters, self.GEOJEOM_LAYER, context)
        type_field     = self.parameterAsString(parameters, self.TYPE_FIELD, context)
        pop_field      = self.parameterAsString(parameters, self.POP_FIELD, context)
        buffer_dist    = self.parameterAsDouble(parameters, self.BUFFER_DISTANCE, context)
        merge_touching = self.parameterAsBoolean(parameters, self.MERGE_TOUCHING, context)
        pop_threshold  = self.parameterAsDouble(parameters, self.POP_THRESHOLD, context)
        emd_layer      = self.parameterAsVectorLayer(parameters, self.EMD_LAYER, context)
        emd_name_field = self.parameterAsString(parameters, self.EMD_NAME_FIELD, context)
        dedup_by_emd   = self.parameterAsBoolean(parameters, self.DEDUP_BY_EMD, context)

        # ── 검증 ──────────────────────────────────────────────────────
        if dedup_by_emd and emd_layer is None:
            raise QgsProcessingException(
                "읍면동별 중복 제거를 선택했지만 읍면동 경계 레이어가 지정되지 않았습니다."
            )
        # 명칭 필드가 레이어에 없으면 흔한 대체 이름으로 (예: 통계청 읍면동 경계 ADM_NM)
        if emd_layer is not None and emd_name_field:
            names = emd_layer.fields().names()
            if emd_name_field not in names:
                alt = next((a for a in ('emd_nm', 'ADM_NM', 'ADM_DR_NM', 'EMD_KOR_NM', 'emd_kor_nm')
                            if a in names), None)
                if alt is None:
                    raise QgsProcessingException(
                        f"읍면동 명칭 필드 '{emd_name_field}'가 읍면동 레이어에 없습니다. "
                        f"레이어 필드: {', '.join(names)}"
                    )
                feedback.pushWarning(f"읍면동 명칭 필드 '{emd_name_field}'가 읍면동 레이어에 없어 '{alt}'를 사용합니다.")
                emd_name_field = alt

        # ── 시작 요약 ──────────────────────────────────────────────────
        feedback.pushInfo(_section("중심지 후보 추출 시작"))
        feedback.pushInfo(_item("입력 레이어", geojeom_layer.name()))
        feedback.pushInfo(_item("유형 필드", f"'{type_field}'  (대상 값: 중심지Ⅰ, 중심지Ⅱ)"))
        feedback.pushInfo(_item("인구밀도 필드", f"'{pop_field}'  (임계값 ≥ {pop_threshold:,.1f})"))
        feedback.pushInfo(_item("버퍼 거리", f"{buffer_dist:,.1f} m"))
        feedback.pushInfo(_item("점접촉 병합", "사용" if merge_touching else "미사용"))
        feedback.pushInfo(_item("읍면동 레이어", emd_layer.name() if emd_layer else "미설정"))
        feedback.pushInfo(_item("읍면동별 중복 제거", "사용" if dedup_by_emd else "미사용"))

        # ── 출력 싱크 생성 ────────────────────────────────────────────
        group_fields = self._make_group_fields()
        final_fields = self._make_final_fields()

        (sink1, dest1) = self.parameterAsSink(
            parameters, self.OUTPUT_GROUP1, context,
            group_fields, QgsWkbTypes.MultiPolygon, geojeom_layer.crs(),
        )
        (sink2, dest2) = self.parameterAsSink(
            parameters, self.OUTPUT_GROUP2, context,
            group_fields, QgsWkbTypes.MultiPolygon, geojeom_layer.crs(),
        )
        (sink_final, dest_final) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            final_fields, QgsWkbTypes.MultiPolygon, geojeom_layer.crs(),
        )

        self._dest_ids = {
            self.OUTPUT_GROUP1: dest1,
            self.OUTPUT_GROUP2: dest2,
            self.OUTPUT: dest_final,
        }

        # ── [1단계] 격자 필터링 (진행률 0~17%) ──────────────────────
        feedback.pushInfo(_section("[1단계] 격자 필터링"))
        feedback.setProgress(0)
        g1_geoms, g2_candidates = self._filter_source_grids(
            geojeom_layer, type_field, pop_field, pop_threshold, feedback
        )
        if feedback.isCanceled():
            return {}

        # ── [2단계] 후보그룹1 파이프라인 (진행률 17~42%) ─────────────
        feedback.pushInfo(_section("[2단계] 후보그룹1 버퍼 파이프라인"))
        feedback.pushInfo(_item("소스 격자", f"{_fmt_n(len(g1_geoms))}개"))
        feedback.setProgress(17)
        g1_polys = self._apply_pipeline(g1_geoms, buffer_dist, merge_touching, feedback)
        if feedback.isCanceled():
            return {}
        feedback.pushInfo(_item("최종 후보그룹1 폴리곤", f"{_fmt_n(len(g1_polys))}개"))

        # ── [3단계] 후보그룹2 거리 필터 (진행률 42~50%) ──────────────
        feedback.pushInfo(_section("[3단계] 후보그룹2 거리 필터 (후보그룹1 1km 버퍼 밖)"))
        feedback.pushInfo(_item("1km 필터 전", f"{_fmt_n(len(g2_candidates))}개 격자"))
        feedback.setProgress(42)
        g2_geoms = self._filter_g2_outside_buffer(g2_candidates, g1_polys, feedback)
        if feedback.isCanceled():
            return {}
        feedback.pushInfo(_item("1km 필터 후", f"{_fmt_n(len(g2_geoms))}개 격자"))

        # ── [4단계] 후보그룹2 파이프라인 (진행률 50~67%) ─────────────
        feedback.pushInfo(_section("[4단계] 후보그룹2 버퍼 파이프라인"))
        feedback.pushInfo(_item("소스 격자", f"{_fmt_n(len(g2_geoms))}개"))
        feedback.setProgress(50)
        g2_polys = self._apply_pipeline(g2_geoms, buffer_dist, merge_touching, feedback)
        if feedback.isCanceled():
            return {}
        feedback.pushInfo(_item("최종 후보그룹2 폴리곤", f"{_fmt_n(len(g2_polys))}개"))

        # ── [5단계] 속성 할당 및 중복 제거 (진행률 67~90%) ──────────
        feedback.pushInfo(_section("[5단계] 속성 할당 및 읍면동 중복 제거"))
        feedback.setProgress(67)
        records = self._assign_attributes(
            g1_polys, g2_polys,
            geojeom_layer, pop_field,
            emd_layer, emd_name_field,
            dedup_by_emd, feedback,
        )
        if feedback.isCanceled():
            return {}

        # ── [6단계] 결과 저장 (진행률 90~100%) ───────────────────────
        feedback.pushInfo(_section("[6단계] 결과 저장"))
        feedback.setProgress(90)
        self._write_to_sinks(records, sink1, sink2, sink_final, group_fields, final_fields)

        g1_count = sum(1 for r in records if r["group"] == "중심지후보그룹1")
        g2_count = sum(1 for r in records if r["group"] == "중심지후보그룹2")
        feedback.setProgress(100)

        feedback.pushInfo(_section("완료"))
        feedback.pushInfo(_item("후보그룹1", f"{_fmt_n(g1_count)}개"))
        feedback.pushInfo(_item("후보그룹2", f"{_fmt_n(g2_count)}개"))
        feedback.pushInfo(_item("전체", f"{_fmt_n(len(records))}개"))
        feedback.pushInfo("\n  중심지 후보 이름(cand_nm)은 레이어 속성 테이블에서 직접 수정하세요.")

        return {
            self.OUTPUT_GROUP1: dest1,
            self.OUTPUT_GROUP2: dest2,
            self.OUTPUT: dest_final,
        }

    def postProcessAlgorithm(
        self,
        context: QgsProcessingContext,
        feedback: QgsProcessingFeedback,
    ) -> dict:
        """레이어 목록에 객체 수 표시."""
        dest_ids = getattr(self, "_dest_ids", {})
        root = QgsProject.instance().layerTreeRoot()
        for dest_id in dest_ids.values():
            layer = QgsProcessingUtils.mapLayerFromString(dest_id, context)
            if layer is None:
                continue
            node = root.findLayer(layer.id())
            if node:
                node.setCustomProperty("showFeatureCount", True)
        return {}

    # ------------------------------------------------------------------ #
    # 필드 스키마                                                          #
    # ------------------------------------------------------------------ #

    def _make_group_fields(self) -> QgsFields:
        fields = QgsFields()
        fields.append(QgsField("cand_id", QVariant.Int))
        fields.append(QgsField("cand_grp", QVariant.String, len=30))
        return fields

    def _make_final_fields(self) -> QgsFields:
        fields = QgsFields()
        fields.append(QgsField("cand_id", QVariant.Int))
        fields.append(QgsField("cand_nm", QVariant.String, len=100))
        fields.append(QgsField("cand_grp", QVariant.String, len=30))
        return fields

    # ------------------------------------------------------------------ #
    # 격자 분류                                                            #
    # ------------------------------------------------------------------ #

    def _filter_source_grids(
        self,
        layer: QgsVectorLayer,
        type_field: str,
        pop_field: str,
        pop_threshold: float,
        feedback: QgsProcessingFeedback,
    ) -> Tuple[List[QgsGeometry], List[QgsGeometry]]:
        total = layer.featureCount()
        feedback.pushInfo(_item("전체 피처 수", f"{_fmt_n(total)}개"))

        # 유형 필드 값 분포 샘플 (최대 200개)
        sample_vals: Dict[str, int] = {}
        for i, feat in enumerate(layer.getFeatures()):
            if i >= 200:
                break
            v = feat[type_field]
            k = str(v) if v is not None else "NULL"
            sample_vals[k] = sample_vals.get(k, 0) + 1

        feedback.pushInfo(_subsection("유형 필드 값 분포 (최대 200개 샘플)"))
        for val, cnt in sorted(sample_vals.items(), key=lambda x: -x[1]):
            mark = " ✔" if val in _CENTER_TYPES else ""
            feedback.pushInfo(f"    '{val}' : {_fmt_n(cnt)}개{mark}")

        g1_geoms: List[QgsGeometry] = []
        g2_candidates: List[QgsGeometry] = []

        for i, feat in enumerate(layer.getFeatures()):
            if feedback.isCanceled():
                return [], []
            t_val = str(feat[type_field]) if feat[type_field] is not None else ""
            geom = feat.geometry()
            if t_val in _CENTER_TYPES:
                g1_geoms.append(QgsGeometry(geom))
            elif safe_float(feat[pop_field]) >= pop_threshold:
                g2_candidates.append(QgsGeometry(geom))
            if i % 500 == 0:
                feedback.setProgress(int(17 * i / max(total, 1)))

        feedback.pushInfo(_subsection("필터링 결과"))
        feedback.pushInfo(_item("후보그룹1 격자 (중심지Ⅰ+Ⅱ)", f"{_fmt_n(len(g1_geoms))}개"))
        feedback.pushInfo(_item("후보그룹2 격자 (인구 조건, 1km 필터 전)", f"{_fmt_n(len(g2_candidates))}개"))
        return g1_geoms, g2_candidates

    def _filter_g2_outside_buffer(
        self,
        g2_candidates: List[QgsGeometry],
        g1_polys: List[QgsGeometry],
        feedback: QgsProcessingFeedback,
    ) -> List[QgsGeometry]:
        if not g1_polys:
            feedback.pushInfo("  후보그룹1 없음 → 1km 필터 미적용, 전체 포함")
            return list(g2_candidates)

        feedback.pushInfo(_item("후보그룹1 폴리곤 수", f"{_fmt_n(len(g1_polys))}개 → 1km 버퍼 생성 중"))

        buffered = [self._do_buffer(poly, 1000.0) for poly in g1_polys]
        buffered = [b for b in buffered if b is not None and not b.isNull()]
        g1_buffer_union = QgsGeometry.unaryUnion(buffered)

        if g1_buffer_union is None or g1_buffer_union.isNull():
            feedback.pushInfo("  경고: 버퍼 생성 실패 → 전체 포함")
            return list(g2_candidates)

        buffer_parts: List[QgsGeometry] = list(g1_buffer_union.asGeometryCollection())
        if not buffer_parts:
            buffer_parts = [g1_buffer_union]

        buf_index = QgsSpatialIndex()
        for i, part in enumerate(buffer_parts):
            f = QgsFeature(i)
            f.setGeometry(part)
            buf_index.addFeature(f)

        g2_geoms: List[QgsGeometry] = []
        for geom in g2_candidates:
            bbox = geom.boundingBox()
            intersects = any(
                buffer_parts[ci].intersects(geom)
                for ci in buf_index.intersects(bbox)
            )
            if not intersects:
                g2_geoms.append(geom)

        removed = len(g2_candidates) - len(g2_geoms)
        feedback.pushInfo(_item("1km 버퍼 내 제외", f"{_fmt_n(removed)}개 격자"))
        feedback.pushInfo(_item("1km 버퍼 밖 유지", f"{_fmt_n(len(g2_geoms))}개 격자"))
        return g2_geoms

    # ------------------------------------------------------------------ #
    # 버퍼 파이프라인                                                      #
    # ------------------------------------------------------------------ #

    def _do_buffer(self, geom: QgsGeometry, dist: float) -> QgsGeometry:
        """버퍼 실행. 5인수(스타일 지정) → 실패 시 2인수(기본 스타일) 폴백."""
        try:
            result = geom.buffer(dist, 5, _CAP_SQUARE, _JOIN_MITER, 5.0)
            if result is not None and not result.isNull():
                return result
        except (TypeError, Exception):
            pass
        return geom.buffer(dist, 5)

    def _apply_pipeline(
        self,
        source_geoms: List[QgsGeometry],
        buffer_dist: float,
        merge_touching: bool,
        feedback: QgsProcessingFeedback,
    ) -> List[QgsGeometry]:
        """버퍼 → 디졸브 → 음의 버퍼 → 단일파트 분리 → (점 접촉 병합)"""
        if not source_geoms:
            feedback.pushInfo("  소스 도형 없음 → 빈 결과 반환")
            return []

        # 1. 양의 버퍼
        buffered = [self._do_buffer(g, buffer_dist) for g in source_geoms if not g.isNull()]
        buffered = [b for b in buffered if b is not None and not b.isNull()]
        feedback.pushInfo(_item("  ① 양의 버퍼", f"{_fmt_n(len(buffered))}개 유효 도형"))
        if not buffered:
            feedback.pushInfo("  경고: 모든 버퍼 결과가 null")
            return []

        # 2. 디졸브
        dissolved = QgsGeometry.unaryUnion(buffered)
        if dissolved is None or dissolved.isNull():
            feedback.pushInfo("  경고: 디졸브(unaryUnion) 결과가 null")
            return []
        feedback.pushInfo(_item(
            "  ② 디졸브",
            f"완료  (면적: {dissolved.area():,.1f} m²)"
        ))

        # 3. 음의 버퍼
        dissolved_neg = self._do_buffer(dissolved, -buffer_dist)
        if dissolved_neg is None or dissolved_neg.isNull():
            feedback.pushInfo(
                f"  경고: 음의 버퍼 결과가 null (버퍼 거리 {buffer_dist}m가 폴리곤 크기보다 클 수 있음)"
            )
            return []
        feedback.pushInfo(_item(
            "  ③ 음의 버퍼",
            f"완료  (면적: {dissolved_neg.area():,.1f} m²)"
        ))

        # 4. 단일파트 분리
        parts = [
            p for p in dissolved_neg.asGeometryCollection()
            if not p.isNull() and p.area() > 0
        ]
        feedback.pushInfo(_item("  ④ 단일파트 분리", f"{_fmt_n(len(parts))}개 폴리곤"))
        if not parts:
            return []
        if not merge_touching:
            return parts

        # 5. 점 접촉 병합
        merged = self._merge_touching_polygons(parts)
        feedback.pushInfo(_item(
            "  ⑤ 점접촉 병합",
            f"{_fmt_n(len(parts))}개 → {_fmt_n(len(merged))}개"
            + (f"  ({_fmt_n(len(parts) - len(merged))}개 병합)" if len(parts) != len(merged) else "")
        ))
        return merged

    def _merge_touching_polygons(
        self,
        parts: List[QgsGeometry],
    ) -> List[QgsGeometry]:
        """Union-Find로 점 접촉(corner-to-corner)하는 폴리곤을 같은 그룹으로 병합한다."""
        n = len(parts)
        if n <= 1:
            return parts

        index = QgsSpatialIndex()
        for i, geom in enumerate(parts):
            f = QgsFeature(i)
            f.setGeometry(geom)
            index.addFeature(f)

        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x: int, y: int) -> None:
            px, py = find(x), find(y)
            if px != py:
                parent[px] = py

        for i, geom_a in enumerate(parts):
            bbox = geom_a.boundingBox()
            bbox.grow(1e-6)
            for j in index.intersects(bbox):
                if j <= i:
                    continue
                geom_b = parts[j]
                if geom_a.touches(geom_b):
                    inter = geom_a.intersection(geom_b)
                    if inter and not inter.isNull():
                        geom_type = QgsWkbTypes.geometryType(inter.wkbType())
                        if geom_type == QgsWkbTypes.PointGeometry:
                            union(i, j)

        groups: Dict[int, List[QgsGeometry]] = defaultdict(list)
        for i in range(n):
            groups[find(i)].append(parts[i])

        result: List[QgsGeometry] = []
        for group_geoms in groups.values():
            if len(group_geoms) == 1:
                result.append(group_geoms[0])
            else:
                merged = QgsGeometry.unaryUnion(group_geoms)
                if merged and not merged.isNull():
                    result.append(merged)
        return result

    # ------------------------------------------------------------------ #
    # 속성 할당                                                            #
    # ------------------------------------------------------------------ #

    def _assign_attributes(
        self,
        g1_polys: List[QgsGeometry],
        g2_polys: List[QgsGeometry],
        geojeom_layer: QgsVectorLayer,
        pop_field: str,
        emd_layer: Optional[QgsVectorLayer],
        emd_name_field: str,
        dedup_by_emd: bool,
        feedback: QgsProcessingFeedback,
    ) -> List[dict]:
        """group·name·id를 부여한 records 리스트 반환."""

        feedback.pushInfo(_subsection("공간 인덱스 구축"))
        geojeom_index = QgsSpatialIndex(geojeom_layer.getFeatures())
        geojeom_by_fid = {f.id(): f for f in geojeom_layer.getFeatures()}
        feedback.pushInfo(_item("국토공간거점지도", f"{_fmt_n(geojeom_layer.featureCount())}개 피처 인덱싱 완료"))

        emd_index: Optional[QgsSpatialIndex] = None
        emd_by_fid: dict = {}
        emd_tr: Optional[QgsCoordinateTransform] = None
        if emd_layer is not None:
            emd_index = QgsSpatialIndex(emd_layer.getFeatures())
            emd_by_fid = {f.id(): f for f in emd_layer.getFeatures()}
            feedback.pushInfo(_item("읍면동 경계", f"{_fmt_n(emd_layer.featureCount())}개 피처 인덱싱 완료"))
            if emd_layer.crs() != geojeom_layer.crs():
                emd_tr = QgsCoordinateTransform(
                    geojeom_layer.crs(), emd_layer.crs(), QgsProject.instance()
                )
                feedback.pushInfo("  좌표계 변환 활성화: 국토공간거점지도 → 읍면동")

        tagged: List[Tuple[QgsGeometry, str]] = (
            [(g, "중심지후보그룹1") for g in g1_polys] +
            [(g, "중심지후보그룹2") for g in g2_polys]
        )
        feedback.pushInfo(_item("중복 제거 전 전체 후보", f"{_fmt_n(len(tagged))}개"))

        # ── 읍면동 중복 제거 ──────────────────────────────────────────
        if dedup_by_emd and emd_index is not None:
            feedback.pushInfo(_subsection("읍면동별 중복 제거"))
            tagged = self._dedup_by_emd(
                tagged, geojeom_index, geojeom_by_fid, pop_field,
                emd_index, emd_by_fid, emd_name_field, emd_tr, feedback,
            )
        else:
            if dedup_by_emd:
                feedback.pushInfo("  중복 제거 건너뜀 (읍면동 레이어 미설정)")
            else:
                feedback.pushInfo("  중복 제거 옵션 미사용")

        # ── 이름 할당 ─────────────────────────────────────────────────
        feedback.pushInfo(_subsection(f"이름 할당  ({_fmt_n(len(tagged))}개 후보)"))
        used_names: Dict[str, int] = {}
        records = []
        duplicated_names = []

        for geom, group_label in tagged:
            seq = len(records) + 1
            raw = self._resolve_name(
                geom, emd_index, emd_by_fid, emd_name_field, emd_tr, seq,
                geojeom_index, geojeom_by_fid, pop_field,
            )
            if raw not in used_names:
                used_names[raw] = 1
                name = raw
            else:
                used_names[raw] += 1
                name = f"{raw}_{used_names[raw]}"
                used_names.setdefault(name, 0)
                duplicated_names.append(name)

            short_group = "그룹1" if group_label == "중심지후보그룹1" else "그룹2"
            suffix_note = f"  ← 중복 suffix 부여" if name != raw else ""
            feedback.pushInfo(f"    #{seq:>3}  [{short_group}]  {name}{suffix_note}")

            records.append({"geom": geom, "group": group_label, "name": name})

        for seq, rec in enumerate(records, start=1):
            rec["id"] = seq

        if duplicated_names:
            feedback.pushInfo(_subsection("중복 suffix 부여 목록"))
            for dn in duplicated_names:
                feedback.pushInfo(f"    {dn}")
            feedback.pushInfo(f"\n  총 {_fmt_n(len(duplicated_names))}개에 suffix 부여됨")
        else:
            feedback.pushInfo("\n  이름 중복 없음 — suffix 부여 없음")

        return records

    def _dedup_by_emd(
        self,
        tagged: List[Tuple[QgsGeometry, str]],
        geojeom_index: QgsSpatialIndex,
        geojeom_by_fid: dict,
        pop_field: str,
        emd_index: QgsSpatialIndex,
        emd_by_fid: dict,
        emd_name_field: str,
        emd_tr: Optional[QgsCoordinateTransform],
        feedback: QgsProcessingFeedback,
    ) -> List[Tuple[QgsGeometry, str]]:
        """읍면동 내 모든 그룹 통합 — 교차 면적 최대 EMD 기준, 인구 최대 1개 유지.

        수정 이력:
        - EMD 결정: centroid 포함 여부 → 교차 면적 최대 (이름 할당 방식과 동일)
        - 그룹 구분 제거: 그룹1/그룹2 통합 dedup (동일 EMD 내 1개만 유지)
        """
        # emd_fid → [(poly_idx, pop_sum, group_label)]
        emd_map: Dict[int, List[Tuple[int, float, str]]] = defaultdict(list)
        unassigned_indices: List[int] = []

        feedback.pushInfo(f"  EMD 매칭 중 (교차 면적 최대 방식) ...")

        for poly_idx, (geom, group_label) in enumerate(tagged):
            query_geom = QgsGeometry(geom)
            if emd_tr:
                query_geom.transform(emd_tr)

            # 교차 면적이 가장 큰 EMD를 이 폴리곤의 소속 EMD로 결정
            best_area = -1.0
            best_emd_fid = None
            for emd_fid in emd_index.intersects(query_geom.boundingBox()):
                emd_feat = emd_by_fid.get(emd_fid)
                if emd_feat is None:
                    continue
                inter = query_geom.intersection(emd_feat.geometry())
                if inter and not inter.isNull() and inter.area() > best_area:
                    best_area = inter.area()
                    best_emd_fid = emd_fid

            if best_emd_fid is not None:
                pop_sum = self._sum_pop_in_polygon(
                    geom, geojeom_index, geojeom_by_fid, pop_field
                )
                emd_map[best_emd_fid].append((poly_idx, pop_sum, group_label))
            else:
                unassigned_indices.append(poly_idx)

        # EMD 미매칭 현황
        if unassigned_indices:
            feedback.pushInfo(
                f"  EMD 미매칭 (중복 제거 대상 제외): {_fmt_n(len(unassigned_indices))}개 폴리곤"
            )

        # 각 EMD에서 인구 최대 1개만 유지
        keep_indices = set(range(len(tagged)))
        total_removed = 0

        feedback.pushInfo(_subsection("EMD별 중복 제거 결과"))
        has_dup = False

        for emd_fid, entries in sorted(emd_map.items()):
            if len(entries) <= 1:
                continue
            has_dup = True
            entries.sort(key=lambda x: x[1], reverse=True)

            emd_feat = emd_by_fid.get(emd_fid)
            emd_name = ""
            if emd_feat and emd_name_field:
                raw = emd_feat[emd_name_field]
                emd_name = str(raw) if raw is not None else ""

            feedback.pushInfo(
                f"\n  [{emd_name}]  총 {len(entries)}개 후보 → 1개 유지"
            )
            for rank, (pidx, pop, grp) in enumerate(entries):
                short_grp = "그룹1" if grp == "중심지후보그룹1" else "그룹2"
                if rank == 0:
                    feedback.pushInfo(f"    ✔ 유지  #{pidx + 1:>3} [{short_grp}]  인구합계: {pop:>12,.1f}")
                else:
                    feedback.pushInfo(f"    ✗ 제거  #{pidx + 1:>3} [{short_grp}]  인구합계: {pop:>12,.1f}")
                    keep_indices.discard(pidx)
                    total_removed += 1

        if not has_dup:
            feedback.pushInfo("  중복 없음 — 모든 EMD에 후보가 1개씩만 존재")

        before = len(tagged)
        after = len(keep_indices)
        feedback.pushInfo(_subsection("중복 제거 요약"))
        feedback.pushInfo(_item("제거 전", f"{_fmt_n(before)}개"))
        feedback.pushInfo(_item("제거 후", f"{_fmt_n(after)}개"))
        feedback.pushInfo(_item("제거 수", f"{_fmt_n(total_removed)}개"))

        return [tagged[i] for i in sorted(keep_indices)]

    def _sum_pop_in_polygon(
        self,
        poly_geom: QgsGeometry,
        geojeom_index: QgsSpatialIndex,
        geojeom_by_fid: dict,
        pop_field: str,
    ) -> float:
        """폴리곤 내 격자 셀 pop_field 합계. centroid-in-polygon 방식."""
        total = 0.0
        for fid in geojeom_index.intersects(poly_geom.boundingBox()):
            feat = geojeom_by_fid.get(fid)
            if feat is None:
                continue
            if poly_geom.contains(feat.geometry().centroid()):
                total += safe_float(feat[pop_field])
        return total

    def _resolve_name(
        self,
        geom: QgsGeometry,
        emd_index: Optional[QgsSpatialIndex],
        emd_by_fid: dict,
        emd_name_field: str,
        emd_tr: Optional[QgsCoordinateTransform],
        seq: int,
        geojeom_index: Optional[QgsSpatialIndex] = None,
        geojeom_by_fid: Optional[dict] = None,
        pop_field: str = "",
    ) -> str:
        """후보 안 주거인구가 가장 많은 읍면동 이름 반환. EMD 없으면 '중심지후보_N'.

        격자 중심점이 후보 안에 있는 격자의 인구를 읍면동별로 합산해 최대 읍면동을
        고른다(여러 시·군에 걸친 후보가 면적만 큰 이웃 읍면동 이름을 받는 문제 방지).
        인구가 없으면 교차 면적 최대 읍면동으로 대신한다.
        읍면동 이름 끝이 '읍'·'면'·'동'이면 해당 글자를 제거한다.
        """
        if emd_index is None or not emd_name_field:
            return f"중심지후보_{seq}"

        def _short(val):
            name_str = str(val)
            if name_str and name_str[-1] in ("읍", "면", "동"):
                name_str = name_str[:-1]
            return name_str

        if geojeom_index is not None and geojeom_by_fid and pop_field:
            pop_by_emd: Dict[int, float] = defaultdict(float)
            for fid in geojeom_index.intersects(geom.boundingBox()):
                feat = geojeom_by_fid.get(fid)
                if feat is None:
                    continue
                c = feat.geometry().centroid()
                if not geom.contains(c):
                    continue
                cq = QgsGeometry(c)
                if emd_tr:
                    cq.transform(emd_tr)
                for emd_fid in emd_index.intersects(cq.boundingBox()):
                    emd_feat = emd_by_fid.get(emd_fid)
                    if emd_feat is not None and emd_feat.geometry().contains(cq):
                        pop_by_emd[emd_fid] += safe_float(feat[pop_field])
                        break
            if pop_by_emd:
                top = max(pop_by_emd, key=pop_by_emd.get)
                if pop_by_emd[top] > 0:
                    val = emd_by_fid[top][emd_name_field]
                    if val is not None:
                        return _short(val)

        query_geom = QgsGeometry(geom)
        if emd_tr:
            query_geom.transform(emd_tr)

        best_area = -1.0
        best_name = f"중심지후보_{seq}"

        for emd_fid in emd_index.intersects(query_geom.boundingBox()):
            emd_feat = emd_by_fid.get(emd_fid)
            if emd_feat is None:
                continue
            inter = query_geom.intersection(emd_feat.geometry())
            if inter and not inter.isNull() and inter.area() > best_area:
                best_area = inter.area()
                val = emd_feat[emd_name_field]
                if val is not None:
                    name_str = str(val)
                    if name_str and name_str[-1] in ("읍", "면", "동"):
                        name_str = name_str[:-1]
                    best_name = name_str
                else:
                    best_name = f"중심지후보_{seq}"

        return best_name

    # ------------------------------------------------------------------ #
    # 싱크 기록                                                            #
    # ------------------------------------------------------------------ #

    def _write_to_sinks(
        self,
        records: List[dict],
        sink1: QgsFeatureSink,
        sink2: QgsFeatureSink,
        sink_final: QgsFeatureSink,
        group_fields: QgsFields,
        final_fields: QgsFields,
    ) -> None:
        for rec in records:
            geom  = QgsGeometry(rec["geom"])
            group = rec["group"]
            cid   = rec["id"]
            name  = rec["name"]

            if not geom.isMultipart():
                geom.convertToMultiType()

            group_feat = QgsFeature(group_fields)
            group_feat.setGeometry(geom)
            group_feat.setAttributes([cid, group])
            if group == "중심지후보그룹1":
                sink1.addFeature(group_feat, QgsFeatureSink.FastInsert)
            else:
                sink2.addFeature(group_feat, QgsFeatureSink.FastInsert)

            final_feat = QgsFeature(final_fields)
            final_feat.setGeometry(geom)
            final_feat.setAttributes([cid, name, group])
            sink_final.addFeature(final_feat, QgsFeatureSink.FastInsert)
