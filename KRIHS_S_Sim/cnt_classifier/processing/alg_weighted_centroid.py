from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterField,
    QgsProcessingParameterFeatureSink,
    QgsProcessing,
    QgsProcessingUtils,
    QgsWkbTypes,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsSpatialIndex,
    QgsCoordinateTransform,
    QgsProject,
    QgsFeatureSink,
)

from ..utils import safe_float


# ──────────────────────────────────────────────────────────────────────────── #
# 로그 포맷 헬퍼                                                                #
# ──────────────────────────────────────────────────────────────────────────── #

def _bar(char: str = "━", width: int = 56) -> str:
    return char * width

def _section(title: str) -> str:
    return f"\n{_bar()}\n  {title}\n{_bar()}"

def _item(label: str, value: str, indent: int = 2) -> str:
    pad = " " * indent
    return f"{pad}  {label} : {value}"


class PopWeightedCentroidAlgorithm(QgsProcessingAlgorithm):

    INPUT = "INPUT"
    GEOJEOM_LAYER = "GEOJEOM_LAYER"
    POP_FIELD = "POP_FIELD"
    OUTPUT = "OUTPUT"

    def name(self) -> str:
        return "pop_weighted_centroid"

    def displayName(self) -> str:
        return "5. 인구가중 중심지 대표점 추출_update"

    def group(self) -> str:
        return "2. 중심지-생활권 경계 도출"

    def groupId(self) -> str:
        return "region_boundary"

    def createInstance(self):
        return PopWeightedCentroidAlgorithm()

    def shortHelpString(self) -> str:
        return (
            "중심지 폴리곤마다 내부 격자의 인구를 가중치로 한 무게중심 포인트를 계산합니다.\n"
            "기하학적 중심 대신 '사람이 많이 사는 쪽'으로 치우친 대표점을 얻어, "
            "도로 거리 행렬 산출(6)의 기·종점으로 사용합니다.\n\n"
            "【입력】\n"
            "  · 중심지 레이어 : '3. 중심지 및 위계 설정_update' 출력 폴리곤 (그룹1)\n"
            "  · 국토공간거점지도 : 1km 격자 (인구 필드 출처, 기본 pop_r)\n\n"
            "【처리 단계】\n"
            "  1) 중심지 폴리곤과 겹치는 격자 탐색 (공간 인덱스, CRS 자동 변환)\n"
            "  2) 격자 중심점을 인구로 가중 평균 → 대표점 좌표 계산\n"
            "     — 교차 격자가 없거나 인구 합이 0이면 폴리곤 기하 중심으로 대체(경고 표시)\n\n"
            "【출력】\n"
            "  · 인구가중 중심점 (포인트) — 중심지 레이어의 속성을 그대로 유지\n\n"
            "이전 도구: 3. 중심지 및 위계 설정_update (그룹1)  /  다음 도구: 6. 중심지-중심지 거리(도로) 행렬 산출_update"
        )

    def initAlgorithm(self, config=None) -> None:
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT,
                "중심지 레이어 ('3. 중심지 및 위계 설정_update' 출력)",
                types=[QgsProcessing.TypeVectorPolygon],
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.GEOJEOM_LAYER,
                "국토공간거점지도 레이어 (1km 격자 — 인구 출처)",
                types=[QgsProcessing.TypeVectorPolygon],
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.POP_FIELD,
                "인구 가중치 필드 (기본: pop_r)",
                defaultValue="pop_r",
                parentLayerParameterName=self.GEOJEOM_LAYER,
                type=QgsProcessingParameterField.Numeric,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                "인구가중 중심점",
                type=QgsProcessing.TypeVectorPoint,
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        center_layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        geojeom_layer = self.parameterAsVectorLayer(parameters, self.GEOJEOM_LAYER, context)
        pop_field = self.parameterAsString(parameters, self.POP_FIELD, context)

        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            center_layer.fields(),
            QgsWkbTypes.Point,
            center_layer.crs(),
        )
        self._dest_id = dest_id

        # ── [1단계] 준비 ─────────────────────────────────────────────
        feedback.pushInfo(_section("[1단계] 공간 인덱스 구축"))
        feedback.pushInfo(_item("중심지", f"{center_layer.featureCount():,}개"))
        feedback.pushInfo(_item("격자", f"{geojeom_layer.featureCount():,}개"))
        feedback.pushInfo(_item("가중치 필드", pop_field))
        geojeom_index = QgsSpatialIndex(geojeom_layer.getFeatures())
        geojeom_by_fid = {f.id(): f for f in geojeom_layer.getFeatures()}

        # center → geojeom 변환, geojeom → center 역변환
        to_geojeom = None
        to_center = None
        if center_layer.crs() != geojeom_layer.crs():
            to_geojeom = QgsCoordinateTransform(
                center_layer.crs(), geojeom_layer.crs(), QgsProject.instance()
            )
            to_center = QgsCoordinateTransform(
                geojeom_layer.crs(), center_layer.crs(), QgsProject.instance()
            )

        # ── [2단계] 인구가중 무게중심 계산 ───────────────────────────
        feedback.pushInfo(_section("[2단계] 인구가중 무게중심 계산"))

        n_fallback = 0
        total = center_layer.featureCount() or 1
        for i, center_feat in enumerate(center_layer.getFeatures()):
            if feedback.isCanceled():
                return {}
            feedback.setProgress(int(i / total * 100))

            center_geom = center_feat.geometry()

            if to_geojeom:
                geom_t = QgsGeometry(center_geom)
                geom_t.transform(to_geojeom)
            else:
                geom_t = center_geom

            weighted_x = 0.0
            weighted_y = 0.0
            total_weight = 0.0

            for fid in geojeom_index.intersects(geom_t.boundingBox()):
                gf = geojeom_by_fid.get(fid)
                if gf is None:
                    continue
                cell_centroid = gf.geometry().centroid()
                if not geom_t.contains(cell_centroid):
                    continue

                pop = safe_float(gf[pop_field])
                pt = cell_centroid.asPoint()
                weighted_x += pop * pt.x()
                weighted_y += pop * pt.y()
                total_weight += pop

            if total_weight > 0:
                wx = weighted_x / total_weight
                wy = weighted_y / total_weight
                if to_center:
                    result_pt = to_center.transform(QgsPointXY(wx, wy))
                else:
                    result_pt = QgsPointXY(wx, wy)
                point_geom = QgsGeometry.fromPointXY(result_pt)
            else:
                point_geom = center_geom.centroid()
                n_fallback += 1
                feedback.pushWarning(
                    f"  피처 {center_feat.id()}: 교차 격자 없음 또는 인구 합계 0 → 폴리곤 기하 중심 사용"
                )

            out_feat = QgsFeature(center_layer.fields())
            out_feat.setGeometry(point_geom)
            out_feat.setAttributes(center_feat.attributes())
            sink.addFeature(out_feat, QgsFeatureSink.FastInsert)

        feedback.pushInfo(_bar())
        feedback.pushInfo(f"완료: 대표점 {total:,}개 출력 (기하 중심 대체 {n_fallback}개)")
        feedback.pushInfo("다음 도구: '6. 중심지-중심지 거리(도로) 행렬 산출_update'의 기·종점으로 사용하세요.")
        return {self.OUTPUT: dest_id}

    def postProcessAlgorithm(self, context, feedback) -> dict:
        dest_id = getattr(self, "_dest_id", None)
        if dest_id:
            layer = QgsProcessingUtils.mapLayerFromString(dest_id, context)
            if layer and layer.isValid():
                QgsProject.instance().addMapLayer(layer)
        return {}
