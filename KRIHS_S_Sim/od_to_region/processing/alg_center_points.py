from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterField,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsSpatialIndex,
    QgsWkbTypes,
)

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
    return f"\n{_bar()}\n  {title}\n{_bar()}"

def _item(label: str, value: str, indent: int = 2) -> str:
    pad = " " * indent
    return f"{pad}  {label} : {value}"


class CenterGridPointsAlgorithm(QgsProcessingAlgorithm):
    """중심지 폴리곤 → 소속 1km 격자 중심점 + cent_id/cent_nm 부여."""

    CENTER_LAYER  = 'CENTER_LAYER'
    CENT_ID_FIELD = 'CENT_ID_FIELD'
    CENT_NM_FIELD = 'CENT_NM_FIELD'
    GRID_LAYER    = 'GRID_LAYER'
    GRID_ID_FIELD = 'GRID_ID_FIELD'
    OUTPUT        = 'OUTPUT'

    # ------------------------------------------------------------------ #
    # 메타데이터                                                           #
    # ------------------------------------------------------------------ #

    def createInstance(self):
        return CenterGridPointsAlgorithm()

    def name(self):
        return 'center_grid_points'

    def displayName(self):
        return '1. 중심지 중심점 생성_update'

    def group(self):
        return '2. 중심지-생활권 경계 도출'

    def groupId(self):
        return 'region_boundary'

    def shortHelpString(self):
        return (
            "중심지 폴리곤에 속한 1km 격자의 중심점을 추출하고, 각 점에 소속 중심지의 "
            "ID·이름(cent_id·cent_nm)을 부여합니다.\n"
            "이 점들이 OD 행렬 산출(2)의 도착점이자 기초생활권 할당(3)의 중심지 중심점 입력입니다.\n\n"
            "【입력】\n"
            "  · 중심지 폴리곤 : '3. 중심지 및 위계 설정_update' 출력 (cand_id·cand_nm)\n"
            "  · 국토공간거점지도 : 1km 격자 폴리곤 (gid)\n\n"
            "【처리 단계】\n"
            "  1) 중심지 폴리곤과 겹치는 격자 탐색 (격자 중심점 기준, CRS 자동 변환)\n"
            "  2) 격자 중심점 생성 + cent_id(←중심지 ID)·cent_nm(←중심지 이름) 부여\n\n"
            "【출력 필드】\n"
            "  · gid : 격자 ID\n"
            "  · cent_id / cent_nm : 소속 중심지 ID·이름\n"
            "  · 한 격자가 여러 중심지에 걸치면 첫 번째 중심지에 배정 (경고 표시)\n\n"
            "이전 도구: 3. 중심지 및 위계 설정_update (그룹1)  /  "
            "다음 도구: 2. 격자→중심지 OD 행렬 산출_update · 3. 거리 행렬 기반 기초생활권 할당_update"
        )

    # ------------------------------------------------------------------ #
    # 파라미터 정의                                                        #
    # ------------------------------------------------------------------ #

    def initAlgorithm(self, config=None):
        if _sep: self.addParameter(_sep("SEP_INPUT", "입력 데이터"))

        self.addParameter(QgsProcessingParameterVectorLayer(
            self.CENTER_LAYER,
            "중심지 폴리곤 레이어 ('3. 중심지 및 위계 설정_update' 출력)",
            [QgsProcessing.TypeVectorPolygon],
        ))
        self.addParameter(QgsProcessingParameterField(
            self.CENT_ID_FIELD, '   ↳ 중심지 ID 필드 (기본: cand_id)',
            defaultValue='cand_id',
            parentLayerParameterName=self.CENTER_LAYER,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.CENT_NM_FIELD, '   ↳ 중심지 이름 필드 (기본: cand_nm)',
            defaultValue='cand_nm',
            parentLayerParameterName=self.CENTER_LAYER,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.GRID_LAYER,
            '국토공간거점지도 레이어 (1km 격자)',
            [QgsProcessing.TypeVectorPolygon],
        ))
        self.addParameter(QgsProcessingParameterField(
            self.GRID_ID_FIELD, '   ↳ 격자 ID 필드 (기본: gid)',
            defaultValue='gid',
            parentLayerParameterName=self.GRID_LAYER,
        ))

        if _sep: self.addParameter(_sep("SEP_OUTPUT", "출력"))

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, '중심지 중심점',
            type=QgsProcessing.TypeVectorPoint,
        ))

    # ------------------------------------------------------------------ #
    # 실행                                                                 #
    # ------------------------------------------------------------------ #

    def processAlgorithm(self, parameters, context, feedback):
        center_layer = self.parameterAsVectorLayer(parameters, self.CENTER_LAYER, context)
        f_cent_id    = self.parameterAsString(parameters, self.CENT_ID_FIELD, context)
        f_cent_nm    = self.parameterAsString(parameters, self.CENT_NM_FIELD, context)
        grid_layer   = self.parameterAsVectorLayer(parameters, self.GRID_LAYER, context)
        f_grid_id    = self.parameterAsString(parameters, self.GRID_ID_FIELD, context)

        # ── [1단계] 준비 ─────────────────────────────────────────────
        feedback.pushInfo(_section('[1단계] 공간 인덱스 구축'))
        feedback.pushInfo(_item('중심지', f'{center_layer.featureCount():,}개'))
        feedback.pushInfo(_item('격자', f'{grid_layer.featureCount():,}개'))

        grid_index  = QgsSpatialIndex(grid_layer.getFeatures())
        grid_by_fid = {f.id(): f for f in grid_layer.getFeatures()}

        transform = None
        if center_layer.crs() != grid_layer.crs():
            transform = QgsCoordinateTransform(
                center_layer.crs(), grid_layer.crs(), QgsProject.instance())

        out_fields = QgsFields()
        out_fields.append(QgsField('gid',     QVariant.String, len=80))
        out_fields.append(QgsField('cent_id', QVariant.String, len=80))
        out_fields.append(QgsField('cent_nm', QVariant.String, len=100))

        sink, dest_id = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            out_fields, QgsWkbTypes.Point, grid_layer.sourceCrs(),
        )

        # ── [2단계] 격자 중심점 추출 및 부여 ─────────────────────────
        feedback.pushInfo(_section('[2단계] 소속 격자 중심점 추출 (격자 중심점 기준)'))

        assigned = {}   # gid → (cent_id, cent_nm, point_geom)
        n_dup = 0
        total = center_layer.featureCount() or 1

        for i, cf in enumerate(center_layer.getFeatures()):
            if feedback.isCanceled():
                return {}
            feedback.setProgress(int(i / total * 90))

            cent_id = cf[f_cent_id]
            cent_nm = cf[f_cent_nm]
            if cent_id is None:
                feedback.pushWarning(f'  피처 {cf.id()}: 중심지 ID가 비어 있어 건너뜀')
                continue

            geom = cf.geometry()
            if transform:
                geom = QgsGeometry(geom)
                geom.transform(transform)

            for fid in grid_index.intersects(geom.boundingBox()):
                gf = grid_by_fid.get(fid)
                if gf is None:
                    continue
                centroid = gf.geometry().centroid()
                if not geom.contains(centroid):
                    continue

                gid = str(gf[f_grid_id])
                if gid in assigned:
                    n_dup += 1
                    continue   # 먼저 배정된 중심지 유지
                assigned[gid] = (str(cent_id), str(cent_nm) if cent_nm is not None else None, centroid)

        if not assigned:
            raise QgsProcessingException(
                '중심지에 속한 격자를 찾지 못했습니다. 두 레이어의 좌표계와 범위를 확인하세요.')
        if n_dup:
            feedback.pushWarning(f'  여러 중심지에 걸친 격자 {n_dup:,}건 → 먼저 배정된 중심지 유지')

        for gid, (cid, cnm, centroid) in assigned.items():
            out_feat = QgsFeature(out_fields)
            out_feat.setGeometry(centroid)
            out_feat.setAttributes([gid, cid, cnm])
            sink.addFeature(out_feat, QgsFeatureSink.FastInsert)

        n_centers = len({cid for cid, _, _ in assigned.values()})
        feedback.pushInfo(_item('중심점', f'{len(assigned):,}개 (중심지 {n_centers}개)'))
        feedback.pushInfo(_bar())
        feedback.pushInfo("완료: '2. 격자→중심지 OD 행렬 산출_update'의 중심지 중심점 입력으로 사용하세요.")
        feedback.setProgress(100)

        return {self.OUTPUT: dest_id}
