import csv
import os
import urllib.parse
from collections import defaultdict

from PyQt5.QtCore import QVariant

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterField,
    QgsProcessingParameterEnum,
    QgsProcessingParameterString,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFileDestination,
    QgsProcessing,
    QgsProcessingException,
    QgsProcessingUtils,
    QgsSpatialIndex,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsFeature,
    QgsFeatureSink,
    QgsFields,
    QgsField,
    QgsWkbTypes,
    QgsProject,
    QgsVectorLayer,
)

from ..utils import safe_float

_UPPER_ID_FIELD = "upper_id"
_ZONE_NM_FIELD = "zone_nm"


# ──────────────────────────────────────────────────────────────────────────── #
# 로그 포맷 헬퍼                                                                #
# ──────────────────────────────────────────────────────────────────────────── #

def _norm_id(v) -> str:
    """ID 값 문자열 정규화 — 필드 타입이 달라도 같은 번호는 같은 키가 되게.

    Double 필드의 12.0 → '12', 공백 제거. '다바4321' 같은 문자 ID는 그대로.
    """
    s = str(v).strip()
    try:
        f = float(s)
    except ValueError:
        return s
    return str(int(f)) if f.is_integer() else s


def _alpha(n: int) -> str:
    """0→A, 25→Z, 26→AA (엑셀 열 방식 임시 명칭)."""
    s = ""
    n += 1
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _city_name(sido: str, sgg: str) -> str:
    """시도·시군구명 → 생활권 명칭용 시·군 이름.

    광역시·특별시·특별자치시의 자치구는 시 이름(광주광역시 북구 → 광주),
    일반구는 시 이름(청주시 상당구 → 청주), 시·군은 접미사 제거(순천시 → 순천).
    """
    s = (sido or "").strip()
    for suf in ("특별자치시", "광역시", "특별시"):
        if s.endswith(suf) and len(s) > len(suf):
            return s[: -len(suf)]
    g = (sgg or "").strip().split()[0] if (sgg or "").strip() else ""
    if "시" in g and g.endswith("구") and not g.startswith("시"):
        g = g[: g.index("시") + 1]
    if len(g) > 2 and g[-1] in ("시", "군"):
        g = g[:-1]
    return g


def _sort_ids(ids):
    """ID 정렬 — 모두 숫자면 수치 순, 아니면 문자 순."""
    try:
        return sorted(ids, key=lambda x: float(x))
    except (TypeError, ValueError):
        return sorted(ids, key=str)


def _bar(char: str = "━", width: int = 56) -> str:
    return char * width

def _section(title: str) -> str:
    return f"\n{_bar()}\n  {title}\n{_bar()}"

def _item(label: str, value: str, indent: int = 2) -> str:
    pad = " " * indent
    return f"{pad}  {label} : {value}"


class AttractivenessUpperZoneAlgorithm(QgsProcessingAlgorithm):

    UPPER_LAYER      = "UPPER_LAYER"
    UPPER_TYPE_FIELD = "UPPER_TYPE_FIELD"
    UPPER_TYPES      = "UPPER_TYPES"
    UPPER_ID_FIELD   = "UPPER_ID_FIELD"

    SUB_LAYER        = "SUB_LAYER"
    SUB_ZONE_ID_FIELD = "SUB_ZONE_ID_FIELD"
    SUB_GRID_ID_FIELD = "SUB_GRID_ID_FIELD"

    CSV_LAYER      = "CSV_LAYER"
    CSV_FROM_FIELD = "CSV_FROM_FIELD"
    CSV_TO_FIELD   = "CSV_TO_FIELD"
    CSV_TRIP_FIELD = "CSV_TRIP_FIELD"

    DIST_LAYER      = "DIST_LAYER"
    DIST_FROM_FIELD = "DIST_FROM_FIELD"
    DIST_TO_FIELD   = "DIST_TO_FIELD"
    DIST_VALUE_FIELD = "DIST_VALUE_FIELD"
    DIST_UNIT       = "DIST_UNIT"

    ZONE_LABEL_PREFIX = "ZONE_LABEL_PREFIX"
    NAME_BY_CITY      = "NAME_BY_CITY"

    OUTPUT_DISSOLVE     = "OUTPUT_DISSOLVE"
    OUTPUT_GRID         = "OUTPUT_GRID"
    OUTPUT_SCORE_CSV    = "OUTPUT_SCORE_CSV"
    OUTPUT_MOBILITY_CSV = "OUTPUT_MOBILITY_CSV"

    _DIST_UNITS   = ["미터 (m)", "킬로미터 (km)"]

    def name(self) -> str:
        return "attractiveness_upper_zone"

    def displayName(self) -> str:
        return "8. 매력도 기반 상위생활권 도출_update"

    def group(self) -> str:
        return "2. 중심지-생활권 경계 도출"

    def groupId(self) -> str:
        return "region_boundary"

    def createInstance(self):
        return AttractivenessUpperZoneAlgorithm()

    def shortHelpString(self) -> str:
        return (
            "하위생활권이 어느 상위중심지 생활권에 속하는지를 매력도 점수로 판정해 "
            "상위생활권 경계를 도출합니다.\n"
            "매력도 = (in_trips + out_trips) ÷ 거리(km)² — 통행이 많고 가까운 중심지에 배정.\n"
            "하위생활권 자신이 상위중심지이면 자기 권역으로 자동 배정합니다.\n\n"
            "【입력】\n"
            "  · 상위중심지 폴리곤 : '3. 중심지 및 위계 설정_update' 출력 (cent_cls·cand_id, 그룹1)\n"
            "  · 하위생활권 격자 : '4. 기초생활권 스무딩_update' 출력 (cent_id·gid)\n"
            "  · 모바일 통행 행렬 : 1km 격자 간 통행 CSV (예: KT 통근 OD)\n"
            "  · 거리 행렬 : '6. 중심지-중심지 거리(도로) 행렬 산출_update' 출력\n\n"
            "【출력】\n"
            "  · 상위생활권 권역 레이어 : upper_id·n_grid 필드 (합역 폴리곤)\n"
            "  · 상위중심지 할당 격자 레이어 : 격자 + upper_id 필드\n"
            "  · 매력도 점수 CSV : zone_id / upper_id / in_trips / out_trips / dist_km / score\n"
            "  · 이동량 행렬 CSV (선택) : zone_id / upper_id / in_trips / out_trips\n"
            "    (in_trips = 하위→상위 = 상위중심지 유입, out_trips = 상위→하위)\n\n"
            "이전 도구: 4. 기초생활권 스무딩_update · 6. 거리(도로) 행렬_update · 7. 이동량 행렬_update  /  파이프라인 최종 단계"
        )

    def initAlgorithm(self, config=None) -> None:
        # ── 상위중심지 폴리곤 레이어 ─────────────────────────────────────
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.UPPER_LAYER, "상위중심지 폴리곤 레이어 ('3. 중심지 및 위계 설정_update' 출력)",
            types=[QgsProcessing.TypeVectorPolygon],
        ))
        self.addParameter(QgsProcessingParameterField(
            self.UPPER_TYPE_FIELD, "   ↳ 중심지 유형 필드 (기본: cent_cls)",
            defaultValue="cent_cls",
            parentLayerParameterName=self.UPPER_LAYER,
            type=QgsProcessingParameterField.String,
        ))
        # '중심지 유형 필드'의 고유값을 동적으로 불러와 다중 선택하게 한다.
        # (표준 Enum은 옵션이 고정되므로 커스텀 위젯 래퍼로 처리)
        upper_types_param = QgsProcessingParameterString(
            self.UPPER_TYPES, "   ↳ 상위중심지 유형 (필드 고유값에서 선택)",
            defaultValue="광역중심지",
        )
        upper_types_param.setMetadata({
            "widget_wrapper": {
                "class": "krihs_spst_sim_tools.cnt_classifier.processing."
                         "field_value_widget.FieldValueWidgetWrapper",
                "layer_param": self.UPPER_LAYER,
                "field_param": self.UPPER_TYPE_FIELD,
            }
        })
        self.addParameter(upper_types_param)
        self.addParameter(QgsProcessingParameterField(
            self.UPPER_ID_FIELD, "   ↳ 상위중심지 ID 필드 (기본: cand_id)",
            defaultValue="cand_id",
            parentLayerParameterName=self.UPPER_LAYER,
        ))

        # ── 하위생활권(격자) 레이어 ──────────────────────────────────────
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.SUB_LAYER, "하위생활권(격자) 레이어 ('4. 기초생활권 스무딩_update' 출력)",
            types=[QgsProcessing.TypeVectorPolygon],
        ))
        self.addParameter(QgsProcessingParameterField(
            self.SUB_ZONE_ID_FIELD, "   ↳ 생활권(중심지) ID 필드 (기본: cent_id)",
            defaultValue="cent_id",
            parentLayerParameterName=self.SUB_LAYER,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.SUB_GRID_ID_FIELD, "   ↳ 격자 ID 필드 (기본: gid)",
            defaultValue="gid",
            parentLayerParameterName=self.SUB_LAYER,
        ))

        # ── 모바일 통행 행렬 ─────────────────────────────────────────────
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.CSV_LAYER, "1km격자 모바일 통행 행렬 레이어 (CSV를 레이어로 추가)",
            types=[],
        ))
        self.addParameter(QgsProcessingParameterField(
            self.CSV_FROM_FIELD, "   ↳ 출발 격자 ID 필드 (KT 통근 OD 기준: o_GRID_1K_CD)",
            defaultValue="o_GRID_1K_CD",
            parentLayerParameterName=self.CSV_LAYER,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.CSV_TO_FIELD, "   ↳ 도착 격자 ID 필드 (KT 통근 OD 기준: d_GRID_1K_CD)",
            defaultValue="d_GRID_1K_CD",
            parentLayerParameterName=self.CSV_LAYER,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.CSV_TRIP_FIELD, "   ↳ 통행량 필드 (KT 통근 OD 기준: VOL2)",
            defaultValue="VOL2",
            parentLayerParameterName=self.CSV_LAYER,
        ))

        # ── 중심지-중심지 간 거리 행렬 ──────────────────────────────────
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.DIST_LAYER, "중심지-중심지 간 거리 행렬 레이어 ('6. 중심지-중심지 거리(도로) 행렬 산출_update' 출력)",
            types=[],
        ))
        self.addParameter(QgsProcessingParameterField(
            self.DIST_FROM_FIELD, "   ↳ 출발 ID 필드 (기본: origin_id)",
            defaultValue="origin_id",
            parentLayerParameterName=self.DIST_LAYER,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.DIST_TO_FIELD, "   ↳ 도착 ID 필드 (기본: destination_id, shp 저장본은 destinatio)",
            defaultValue="destination_id",
            parentLayerParameterName=self.DIST_LAYER,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.DIST_VALUE_FIELD, "   ↳ 거리 필드 (기본: total_cost)",
            defaultValue="total_cost",
            parentLayerParameterName=self.DIST_LAYER,
        ))
        self.addParameter(QgsProcessingParameterEnum(
            self.DIST_UNIT, "거리 단위",
            options=self._DIST_UNITS,
            defaultValue=0,  # 미터
        ))

        # ── 생활권 명칭 ─────────────────────────────────────────────────
        self.addParameter(QgsProcessingParameterString(
            self.ZONE_LABEL_PREFIX, "생활권 임시 명칭 접두어 (예: 생활권 → 생활권 A·B·C)",
            defaultValue="생활권", optional=True,
        ))
        name_param = QgsProcessingParameterBoolean(
            self.NAME_BY_CITY, "생활권 명칭을 1차 중심지 소재 시·군명으로 표기",
            defaultValue=True,     # 기본: 시·군명 (끄면 생활권 A·B·C…)
        )
        name_param.setHelp(
            "상위중심지 영역 안 주거인구가 가장 많은 시·군의 이름으로 생활권 명칭을 붙입니다"
            "(광역시·특별시의 자치구는 시 이름으로 통합, 같은 이름이 겹치면 번호 부여). "
            "끄면 접두어 + A·B·C 임시 명칭을 씁니다.")
        self.addParameter(name_param)

        # ── 출력 ─────────────────────────────────────────────────────────
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_DISSOLVE, "상위생활권 권역 레이어",
            type=QgsProcessing.TypeVectorPolygon,
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_GRID, "상위중심지 ID 할당 격자 레이어",
            type=QgsProcessing.TypeVectorPolygon,
        ))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT_SCORE_CSV, "매력도 점수 CSV",
            fileFilter="CSV 파일 (*.csv)",
        ))
        mobility_csv_param = QgsProcessingParameterFileDestination(
            self.OUTPUT_MOBILITY_CSV, "이동량 행렬 CSV (선택)",
            fileFilter="CSV 파일 (*.csv)",
            optional=True,
            createByDefault=False,
        )
        self.addParameter(mobility_csv_param)

    def processAlgorithm(self, parameters, context, feedback):
        upper_layer    = self.parameterAsVectorLayer(parameters, self.UPPER_LAYER, context)
        upper_type_fld = self.parameterAsString(parameters, self.UPPER_TYPE_FIELD, context)
        raw_types      = parameters.get(self.UPPER_TYPES)
        if isinstance(raw_types, str):
            upper_types = {s.strip() for s in raw_types.split(",") if s.strip()}
        elif raw_types:
            upper_types = {str(x).strip() for x in raw_types if str(x).strip()}
        else:
            upper_types = set()
        if not upper_types:
            raise QgsProcessingException("상위중심지 유형을 1개 이상 선택하세요.")
        upper_id_fld   = self.parameterAsString(parameters, self.UPPER_ID_FIELD, context)

        sub_layer     = self.parameterAsVectorLayer(parameters, self.SUB_LAYER, context)
        sub_zone_fld  = self.parameterAsString(parameters, self.SUB_ZONE_ID_FIELD, context)
        sub_grid_fld  = self.parameterAsString(parameters, self.SUB_GRID_ID_FIELD, context)

        csv_layer     = self.parameterAsVectorLayer(parameters, self.CSV_LAYER, context)
        csv_from      = self.parameterAsString(parameters, self.CSV_FROM_FIELD, context)
        csv_to        = self.parameterAsString(parameters, self.CSV_TO_FIELD, context)
        csv_trip      = self.parameterAsString(parameters, self.CSV_TRIP_FIELD, context)
        csv_path      = self._extract_csv_path(csv_layer)

        dist_layer     = self.parameterAsVectorLayer(parameters, self.DIST_LAYER, context)
        dist_from_fld  = self.parameterAsString(parameters, self.DIST_FROM_FIELD, context)
        dist_to_fld    = self.parameterAsString(parameters, self.DIST_TO_FIELD, context)
        dist_value_fld = self.parameterAsString(parameters, self.DIST_VALUE_FIELD, context)
        dist_in_meters = (self.parameterAsEnum(parameters, self.DIST_UNIT, context) == 0)

        score_csv_path    = self.parameterAsFileOutput(parameters, self.OUTPUT_SCORE_CSV, context)
        label_prefix = (self.parameterAsString(parameters, self.ZONE_LABEL_PREFIX, context) or "").strip()
        name_by_city = self.parameterAsBoolean(parameters, self.NAME_BY_CITY, context)
        mobility_csv_path = self.parameterAsFileOutput(parameters, self.OUTPUT_MOBILITY_CSV, context)

        # ── [1단계] 하위생활권 격자 인덱스 구축 ──────────────────────────
        feedback.pushInfo(_section("[1단계] 하위생활권 격자 인덱스 구축"))
        feedback.pushInfo(_item("상위중심지 유형", ", ".join(sorted(upper_types))))
        sub_index  = QgsSpatialIndex(sub_layer.getFeatures())
        sub_by_fid = {f.id(): f for f in sub_layer.getFeatures()}
        grid_to_zone = {}
        # 생활권 명칭(시·군명)용 — 격자별 (시·군 이름, 주거인구)
        sub_names = sub_layer.fields().names()
        has_city = all(n in sub_names for n in ("sido_nm", "sgg_nm", "pop_r"))
        grid_city = {}

        def _txt(v):
            return "" if v is None or str(v) == "NULL" else str(v)

        for f in sub_layer.getFeatures():
            gid = _norm_id(f[sub_grid_fld])
            grid_to_zone[gid] = _norm_id(f[sub_zone_fld])
            if has_city:
                grid_city[gid] = (_city_name(_txt(f["sido_nm"]), _txt(f["sgg_nm"])),
                                  safe_float(f["pop_r"]) or 0.0)
        feedback.pushInfo(_item("격자 수", f"{len(grid_to_zone):,}"))

        # ── [2단계] 상위중심지별 포함 격자 추출 ──────────────────────────
        feedback.pushInfo(_section("[2단계] 상위중심지 공간 교차 분석 (격자 중심점 기준)"))
        transform = None
        if upper_layer.crs() != sub_layer.crs():
            transform = QgsCoordinateTransform(
                upper_layer.crs(), sub_layer.crs(), QgsProject.instance()
            )

        upper_center_grids = {}  # upper_id → set(grid_id)
        upper_center_ids   = set()

        for uf in upper_layer.getFeatures():
            if feedback.isCanceled():
                return {}
            if str(uf[upper_type_fld]) not in upper_types:
                continue

            upper_id   = _norm_id(uf[upper_id_fld])
            upper_geom = uf.geometry()
            if transform:
                upper_geom = QgsGeometry(upper_geom)
                upper_geom.transform(transform)

            grid_ids = set()
            for fid in sub_index.intersects(upper_geom.boundingBox()):
                sf = sub_by_fid.get(fid)
                if sf and upper_geom.contains(sf.geometry().centroid()):
                    grid_ids.add(_norm_id(sf[sub_grid_fld]))

            if grid_ids:
                upper_center_grids.setdefault(upper_id, set()).update(grid_ids)
                upper_center_ids.add(upper_id)

        grid_to_upper = {}  # grid_id → set(upper_id)
        for uid, gids in upper_center_grids.items():
            for gid in gids:
                grid_to_upper.setdefault(gid, set()).add(uid)

        feedback.pushInfo(_item("상위중심지", f"{len(upper_center_ids)}개 (포함 격자 추출 완료)"))

        if not upper_center_ids:
            raise QgsProcessingException(
                "선택한 유형에 해당하는 상위중심지가 없거나 교차 격자를 찾지 못했습니다."
            )

        # ── [3단계] CSV 단일 패스, 양방향 집계 ───────────────────────────
        feedback.pushInfo(_section("[3단계] 통행 행렬 집계 (양방향)"))
        feedback.pushInfo(_item("CSV", csv_path))
        file_obj    = self._open_csv(csv_path)
        mobility    = {}  # (zone_id, upper_id) → [inflow, outflow]
        row_count   = 0
        matched_fwd = matched_rev = 0

        try:
            reader = csv.DictReader(file_obj)
            for row in reader:
                if feedback.isCanceled():
                    break
                row_count += 1
                if row_count % 500_000 == 0:
                    feedback.pushInfo(f"  처리 행: {row_count:,}")

                from_id = _norm_id(row[csv_from])
                to_id   = _norm_id(row[csv_to])
                if from_id == to_id:
                    continue

                try:
                    trip = float(row[csv_trip])
                except (ValueError, TypeError):
                    continue

                # 방향 1: 하위생활권 격자 → 상위중심지 격자
                zone_id   = grid_to_zone.get(from_id)
                upper_ids = grid_to_upper.get(to_id)
                if zone_id is not None and upper_ids:
                    for uid in upper_ids:
                        mobility.setdefault((zone_id, uid), [0.0, 0.0])[0] += trip
                    matched_fwd += 1

                # 방향 2: 상위중심지 격자 → 하위생활권 격자
                upper_ids_from = grid_to_upper.get(from_id)
                zone_id_to     = grid_to_zone.get(to_id)
                if upper_ids_from and zone_id_to is not None:
                    for uid in upper_ids_from:
                        mobility.setdefault((zone_id_to, uid), [0.0, 0.0])[1] += trip
                    matched_rev += 1
        finally:
            file_obj.close()

        feedback.pushInfo(
            f"  총 {row_count:,}행 / 하위→상위 {matched_fwd:,}행, 상위→하위 {matched_rev:,}행 매칭"
        )

        # ── [4단계] (선택) 이동량 행렬 CSV 저장 ──────────────────────────
        if mobility_csv_path:
            with open(mobility_csv_path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["zone_id", "upper_id", "in_trips", "out_trips"])
                for (z, u), (inf, out) in sorted(mobility.items()):
                    w.writerow([z, u, inf, out])
            feedback.pushInfo(f"  이동량 행렬 CSV 저장: {mobility_csv_path}")

        # ── [5단계] 거리 행렬 로드 ───────────────────────────────────────
        feedback.pushInfo(_section("[5단계] 거리 행렬 로드"))
        distances = {}
        for feat in dist_layer.getFeatures():
            if feedback.isCanceled():
                return {}
            raw = safe_float(feat[dist_value_fld])
            distances[(_norm_id(feat[dist_from_fld]), _norm_id(feat[dist_to_fld]))] = \
                raw / 1000.0 if dist_in_meters else raw

        unit_label = "m→km 변환 적용" if dist_in_meters else "km 그대로 사용"
        feedback.pushInfo(_item("거리 레코드", f"{len(distances):,}개 ({unit_label})"))

        # ── ID 체계 일치 검증 — 불일치면 결과가 전부 NULL이 되므로 즉시 중단 ──
        mob_zone_ids = {z for (z, _u) in mobility}
        dist_from_ids = {a for (a, _b) in distances}
        if mobility and distances and not (mob_zone_ids & dist_from_ids):
            raise QgsProcessingException(
                "거리행렬의 출발 ID와 기초생활권 ID(cent_id)가 하나도 일치하지 않습니다.\n"
                f"  · 기초생활권 ID 예: {sorted(mob_zone_ids)[:5]}\n"
                f"  · 거리행렬 출발 ID 예: {sorted(dist_from_ids)[:5]}\n"
                "중심지 위계(cand_id) → 중심지 중심점(cent_id) → 기초생활권(cent_id)이 "
                "같은 ID 체계여야 합니다.\n"
                "위계 분석을 새로 실행했다면 그 결과와 짝이 맞는 중심점·OD로 "
                "기초생활권을 다시 도출하세요."
            )
        if not mobility:
            feedback.pushWarning(
                "  통행 행렬에서 매칭된 행이 0건 — 통행 CSV의 격자 ID가 "
                "격자(gid)·기초생활권과 같은 체계인지 확인하세요. 결과가 비게 됩니다."
            )

        # ── [6단계] 매력도 점수 산출 → zone_to_upper ─────────────────────
        # zone_id 자체가 상위중심지이면 자기 ID를 직접 할당
        # 그 외: score = (in_trips + out_trips) / dist_km² 최대값
        feedback.pushInfo(_section("[6단계] 매력도 점수 산출"))
        zone_uppers = defaultdict(list)
        for (z, u) in mobility:
            zone_uppers[z].append(u)

        zone_to_upper = {}
        score_rows    = []
        no_dist_count = 0
        self_assigned = 0

        for zone_id, upper_ids in zone_uppers.items():
            if zone_id in upper_center_ids:
                zone_to_upper[zone_id] = zone_id
                self_assigned += 1
                continue

            best_upper = None
            best_score = -1.0

            for upper_id in upper_ids:
                inflow, outflow = mobility[(zone_id, upper_id)]
                dist = distances.get((zone_id, upper_id))

                if dist is None or dist <= 0:
                    no_dist_count += 1
                    score = None
                else:
                    score = (inflow + outflow) / (dist ** 2)
                    if score > best_score:
                        best_score = score
                        best_upper = upper_id

                score_rows.append((zone_id, upper_id, inflow, outflow, dist, score))

            if best_upper is not None:
                zone_to_upper[zone_id] = best_upper

        if no_dist_count:
            feedback.pushWarning(
                f"  거리 미존재 또는 0인 쌍 {no_dist_count}개 — 해당 쌍은 매력도 계산에서 제외"
            )
        feedback.pushInfo(
            f"  상위중심지 할당: {len(zone_to_upper)}개 하위생활권 (자기할당 {self_assigned}개 포함)"
        )

        # ── [7단계] 매력도 점수 CSV 저장 ─────────────────────────────────
        with open(score_csv_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["zone_id", "upper_id", "in_trips", "out_trips", "dist_km", "score"])
            for (z, u, inf, out, dist, score) in sorted(score_rows, key=lambda r: (r[0], r[1])):
                w.writerow([z, u, inf, out,
                             "" if dist is None else dist,
                             "" if score is None else score])

        # ── 생활권 명칭 — 기본 접두어+A·B·C, 선택 시 상위중심지 소재 시·군명 ──
        assigned_uppers = _sort_ids({u for u in zone_to_upper.values()})
        zone_labels = {uid: f"{label_prefix} {_alpha(i)}".strip()
                       for i, uid in enumerate(assigned_uppers)}
        if name_by_city:
            if not has_city:
                feedback.pushWarning(
                    "  sido_nm·sgg_nm·pop_r 필드가 없어 임시 명칭(A·B·C)을 사용합니다.")
            else:
                city_of = {}
                for uid in assigned_uppers:
                    acc = defaultdict(float)
                    for gid in upper_center_grids.get(uid, ()):
                        c = grid_city.get(gid)
                        if c and c[0]:
                            acc[c[0]] += c[1]
                    if acc:
                        city_of[uid] = max(acc.items(), key=lambda kv: kv[1])[0]
                n_same = defaultdict(int)
                for c in city_of.values():
                    n_same[c] += 1
                seq = defaultdict(int)
                for uid in assigned_uppers:
                    c = city_of.get(uid)
                    if not c:
                        continue
                    if n_same[c] > 1:
                        seq[c] += 1
                        zone_labels[uid] = f"{c} 생활권 {seq[c]}"
                    else:
                        zone_labels[uid] = f"{c} 생활권"
        feedback.pushInfo(_item("생활권 명칭", ", ".join(
            f"{u}→{zone_labels[u]}" for u in assigned_uppers)))

        # ── [8단계] 격자 출력 레이어 ─────────────────────────────────────
        feedback.pushInfo(_section("[8단계] 격자 레이어에 upper_id 할당"))
        grid_fields  = QgsFields(sub_layer.fields())
        for fname in (_UPPER_ID_FIELD, _ZONE_NM_FIELD):
            if grid_fields.indexOf(fname) < 0:
                grid_fields.append(QgsField(fname, QVariant.String, len=100))
        idx_up = grid_fields.indexOf(_UPPER_ID_FIELD)
        idx_nm = grid_fields.indexOf(_ZONE_NM_FIELD)
        n_sub = len(sub_layer.fields())

        (grid_sink, grid_dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT_GRID, context,
            grid_fields, sub_layer.wkbType(), sub_layer.crs(),
        )

        upper_geoms = defaultdict(list)
        total = sub_layer.featureCount() or 1

        for i, feat in enumerate(sub_layer.getFeatures()):
            if feedback.isCanceled():
                break
            feedback.setProgress(int(i / total * 75))

            zone_id  = _norm_id(feat[sub_zone_fld])
            assigned = zone_to_upper.get(zone_id)

            out_feat = QgsFeature(grid_fields)
            out_feat.setGeometry(feat.geometry())

            attrs = list(feat.attributes()) + [None] * (len(grid_fields) - n_sub)
            attrs[idx_up] = assigned
            attrs[idx_nm] = zone_labels.get(assigned) if assigned else None
            out_feat.setAttributes(attrs)

            grid_sink.addFeature(out_feat, QgsFeatureSink.FastInsert)

            if assigned:
                upper_geoms[assigned].append(feat.geometry())

        # ── [9단계] 상위생활권 권역 Dissolve ─────────────────────────────
        feedback.pushInfo(_section("[9단계] 상위생활권 권역 합역 (Dissolve)"))
        dissolve_fields = QgsFields()
        dissolve_fields.append(QgsField(_UPPER_ID_FIELD, QVariant.String, len=100))
        dissolve_fields.append(QgsField(_ZONE_NM_FIELD, QVariant.String, len=100))
        dissolve_fields.append(QgsField("n_grid", QVariant.Int))

        (dissolve_sink, dissolve_dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT_DISSOLVE, context,
            dissolve_fields, QgsWkbTypes.MultiPolygon, sub_layer.crs(),
        )

        n_upper = len(upper_geoms)
        for j, (upper_id, geoms) in enumerate(sorted(upper_geoms.items())):
            if feedback.isCanceled():
                break
            feedback.setProgress(75 + int(j / max(n_upper, 1) * 25))

            merged = QgsGeometry.unaryUnion(geoms)
            if merged is None or merged.isNull():
                continue

            d_feat = QgsFeature(dissolve_fields)
            d_feat.setGeometry(merged)
            d_feat.setAttributes([upper_id, zone_labels.get(upper_id), len(geoms)])
            dissolve_sink.addFeature(d_feat, QgsFeatureSink.FastInsert)

        feedback.setProgress(100)
        feedback.pushInfo(_bar())
        feedback.pushInfo(
            f"완료: 상위생활권 {n_upper}개, 미할당 하위생활권 "
            f"{len(zone_uppers) - len(zone_to_upper)}개"
        )

        self._dissolve_dest_id  = dissolve_dest_id
        self._grid_dest_id      = grid_dest_id
        self._score_csv_path    = score_csv_path
        self._mobility_csv_path = mobility_csv_path

        return {
            self.OUTPUT_DISSOLVE:     dissolve_dest_id,
            self.OUTPUT_GRID:         grid_dest_id,
            self.OUTPUT_SCORE_CSV:    score_csv_path,
            self.OUTPUT_MOBILITY_CSV: mobility_csv_path or "",
        }

    def postProcessAlgorithm(self, context, feedback) -> dict:
        for dest_id, name in [
            (getattr(self, "_dissolve_dest_id", None), "상위생활권 권역"),
            (getattr(self, "_grid_dest_id", None),     "상위중심지 ID 할당 격자"),
        ]:
            if not dest_id:
                continue
            layer = QgsProcessingUtils.mapLayerFromString(dest_id, context)
            if layer and layer.isValid():
                QgsProject.instance().addMapLayer(layer)

        for path, lyr_name in [
            (getattr(self, "_score_csv_path", None),    "매력도 점수"),
            (getattr(self, "_mobility_csv_path", None), "이동량 행렬"),
        ]:
            if path:
                lyr = QgsVectorLayer(path, lyr_name, "ogr")
                if lyr and lyr.isValid():
                    QgsProject.instance().addMapLayer(lyr)

        return {}

    def _extract_csv_path(self, layer):
        """delimitedtext 레이어의 소스 URI에서 실제 파일 경로를 추출한다."""
        source = layer.source()
        path = source.split("?")[0]
        if path.lower().startswith("file:///"):
            path = urllib.parse.unquote(path[8:])
            if not os.path.isabs(path):
                path = "/" + path
        if not os.path.isfile(path):
            raise QgsProcessingException(
                f"CSV 파일 경로를 확인할 수 없습니다: {path}\n"
                f"(레이어 소스: {layer.source()})\n"
                "QGIS에서 CSV를 '구분자로 구분된 텍스트 레이어'로 추가한 뒤 선택하세요."
            )
        return path

    def _open_csv(self, path):
        """UTF-8-SIG → UTF-8 → CP949 순으로 인코딩을 시도해 파일 객체 반환."""
        for enc in ("utf-8-sig", "utf-8", "cp949"):
            try:
                f = open(path, "r", encoding=enc, newline="")
                f.readline()
                f.seek(0)
                return f
            except (UnicodeDecodeError, Exception):
                try:
                    f.close()
                except Exception:
                    pass
        raise QgsProcessingException(
            f"CSV 파일 인코딩을 읽을 수 없습니다 (UTF-8/CP949 시도 실패): {path}"
        )
