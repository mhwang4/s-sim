import csv
import os
import urllib.parse

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterField,
    QgsProcessingParameterString,
    QgsProcessingParameterFileDestination,
    QgsProcessing,
    QgsProcessingException,
    QgsSpatialIndex,
    QgsGeometry,
    QgsCoordinateTransform,
    QgsProject,
    QgsVectorLayer,
)


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


class MobilityMatrixAlgorithm(QgsProcessingAlgorithm):

    SUB_LAYER = "SUB_LAYER"
    SUB_ZONE_ID_FIELD = "SUB_ZONE_ID_FIELD"
    SUB_GRID_ID_FIELD = "SUB_GRID_ID_FIELD"
    UPPER_LAYER = "UPPER_LAYER"
    UPPER_TYPE_FIELD = "UPPER_TYPE_FIELD"
    UPPER_TYPES = "UPPER_TYPES"
    UPPER_ID_FIELD = "UPPER_ID_FIELD"
    CSV_LAYER = "CSV_LAYER"
    CSV_FROM_FIELD = "CSV_FROM_FIELD"
    CSV_TO_FIELD = "CSV_TO_FIELD"
    CSV_TRIP_FIELD = "CSV_TRIP_FIELD"
    OUTPUT = "OUTPUT"

    _TYPE_WIDGET = (
        "krihs_spst_sim_tools.cnt_classifier.processing."
        "field_value_widget.FieldValueWidgetWrapper"
    )

    def name(self) -> str:
        return "mobility_matrix"

    def displayName(self) -> str:
        return "7. 하위생활권-상위중심지 이동량 행렬 산출_update"

    def group(self) -> str:
        return "2. 중심지-생활권 경계 도출"

    def groupId(self) -> str:
        return "region_boundary"

    def createInstance(self):
        return MobilityMatrixAlgorithm()

    def shortHelpString(self) -> str:
        return (
            "하위생활권(스무딩된 격자 권역)에서 상위중심지로의 통행량을 집계해 "
            "생활권 간 연결 강도 행렬(CSV)을 만듭니다. "
            "'8. 매력도 기반 상위생활권 도출_update'의 이동량 근거 자료로 사용됩니다.\n\n"
            "【입력】\n"
            "  · 하위생활권 격자 : '4. 기초생활권 스무딩_update' 출력 (cent_id·gid)\n"
            "  · 상위중심지 폴리곤 : '3. 중심지 및 위계 설정_update' 출력 (cent_cls·cand_id, 그룹1)\n"
            "  · 모바일 통행 행렬 : 1km 격자 간 통행 CSV를 레이어로 추가 후 선택\n"
            "    (예: KT 통근 OD — o_GRID_1K_CD / d_GRID_1K_CD / VOL2)\n\n"
            "【처리 단계】\n"
            "  1) 상위중심지 유형 필터 후 포함 격자 추출 (격자 중심점 기준)\n"
            "  2) 통행 CSV 단일 패스 집계 — 하위→상위(out), 상위→하위(in) 양방향\n"
            "  3) CSV 저장\n\n"
            "【출력 CSV 헤더】 (영문 표준, 상위중심지 관점)\n"
            "  zone_id(하위생활권 ID) / upper_id(상위중심지 ID)\n"
            "  in_trips(하위→상위 = 상위중심지 유입) / out_trips(상위→하위 = 유출)\n\n"
            "이전 도구: 4. 기초생활권 스무딩_update  /  다음 도구: 8. 매력도 기반 상위생활권 도출_update"
        )

    def _make_type_param(self, name, label):
        """중심지 유형 필드의 고유값을 동적으로 로드하는 다중 선택 파라미터."""
        param = QgsProcessingParameterString(name, label, defaultValue="광역중심지")
        param.setMetadata({
            "widget_wrapper": {
                "class": self._TYPE_WIDGET,
                "layer_param": self.UPPER_LAYER,
                "field_param": self.UPPER_TYPE_FIELD,
            }
        })
        return param

    @staticmethod
    def _parse_types(parameters, key):
        """콤마로 join된 유형 문자열(또는 리스트)을 집합으로 파싱한다."""
        raw = parameters.get(key)
        if isinstance(raw, str):
            return {s.strip() for s in raw.split(",") if s.strip()}
        if raw:
            return {str(x).strip() for x in raw if str(x).strip()}
        return set()

    def initAlgorithm(self, config=None) -> None:
        # ── 하위생활권(격자) 레이어 ──────────────────────────────────────
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.SUB_LAYER,
                "하위생활권(격자) 레이어 ('4. 기초생활권 스무딩_update' 출력)",
                types=[QgsProcessing.TypeVectorPolygon],
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.SUB_ZONE_ID_FIELD,
                "   ↳ 생활권(중심지) ID 필드 (기본: cent_id)",
                defaultValue="cent_id",
                parentLayerParameterName=self.SUB_LAYER,
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.SUB_GRID_ID_FIELD,
                "   ↳ 격자 ID 필드 (기본: gid)",
                defaultValue="gid",
                parentLayerParameterName=self.SUB_LAYER,
            )
        )

        # ── 상위중심지 폴리곤 레이어 ─────────────────────────────────────
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.UPPER_LAYER,
                "상위중심지 폴리곤 레이어 ('3. 중심지 및 위계 설정_update' 출력)",
                types=[QgsProcessing.TypeVectorPolygon],
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.UPPER_TYPE_FIELD,
                "   ↳ 중심지 유형 필드 (기본: cent_cls)",
                defaultValue="cent_cls",
                parentLayerParameterName=self.UPPER_LAYER,
                type=QgsProcessingParameterField.String,
            )
        )
        self.addParameter(self._make_type_param(
            self.UPPER_TYPES, "   ↳ 상위중심지 유형 (필드 고유값에서 선택)"
        ))
        self.addParameter(
            QgsProcessingParameterField(
                self.UPPER_ID_FIELD,
                "   ↳ 상위중심지 ID 필드 (기본: cand_id)",
                defaultValue="cand_id",
                parentLayerParameterName=self.UPPER_LAYER,
            )
        )

        # ── 모바일 통행 행렬 CSV 레이어 ──────────────────────────────────
        # QGIS에서 CSV를 레이어로 불러온 뒤 선택 (구분자 텍스트 레이어, 도형 없음)
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.CSV_LAYER,
                "1km격자 모바일 통행 행렬 레이어 (CSV를 레이어로 추가)",
                types=[],  # 도형 없는 레이어 포함 모든 벡터 허용
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.CSV_FROM_FIELD,
                "   ↳ 출발 격자 ID 필드 (KT 통근 OD 기준: o_GRID_1K_CD)",
                defaultValue="o_GRID_1K_CD",
                parentLayerParameterName=self.CSV_LAYER,
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.CSV_TO_FIELD,
                "   ↳ 도착 격자 ID 필드 (KT 통근 OD 기준: d_GRID_1K_CD)",
                defaultValue="d_GRID_1K_CD",
                parentLayerParameterName=self.CSV_LAYER,
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.CSV_TRIP_FIELD,
                "   ↳ 통행량 필드 (KT 통근 OD 기준: VOL2)",
                defaultValue="VOL2",
                parentLayerParameterName=self.CSV_LAYER,
            )
        )

        # ── 출력 ─────────────────────────────────────────────────────────
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT,
                "출력 CSV",
                fileFilter="CSV 파일 (*.csv)",
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        sub_layer = self.parameterAsVectorLayer(parameters, self.SUB_LAYER, context)
        sub_zone_fld = self.parameterAsString(parameters, self.SUB_ZONE_ID_FIELD, context)
        sub_grid_fld = self.parameterAsString(parameters, self.SUB_GRID_ID_FIELD, context)

        upper_layer = self.parameterAsVectorLayer(parameters, self.UPPER_LAYER, context)
        upper_type_fld = self.parameterAsString(parameters, self.UPPER_TYPE_FIELD, context)
        upper_types = self._parse_types(parameters, self.UPPER_TYPES)
        if not upper_types:
            raise QgsProcessingException("상위중심지 유형을 1개 이상 선택하세요.")
        upper_id_fld = self.parameterAsString(parameters, self.UPPER_ID_FIELD, context)

        csv_layer = self.parameterAsVectorLayer(parameters, self.CSV_LAYER, context)
        csv_from = self.parameterAsString(parameters, self.CSV_FROM_FIELD, context)
        csv_to = self.parameterAsString(parameters, self.CSV_TO_FIELD, context)
        csv_trip = self.parameterAsString(parameters, self.CSV_TRIP_FIELD, context)
        csv_path = self._extract_csv_path(csv_layer)

        output_path = self.parameterAsFileOutput(parameters, self.OUTPUT, context)

        # ── [1단계] 하위생활권 격자 인덱스 및 매핑 구축 ──────────────────
        feedback.pushInfo(_section("[1단계] 하위생활권 격자 인덱스 구축"))
        feedback.pushInfo(_item("상위중심지 유형", ", ".join(sorted(upper_types))))
        sub_index = QgsSpatialIndex(sub_layer.getFeatures())
        sub_by_fid = {f.id(): f for f in sub_layer.getFeatures()}

        grid_to_zone = {}
        for f in sub_layer.getFeatures():
            grid_to_zone[str(f[sub_grid_fld])] = str(f[sub_zone_fld])

        feedback.pushInfo(_item("격자 수", f"{len(grid_to_zone):,}"))
        feedback.setProgress(10)

        # ── [2단계] 상위중심지별 포함 격자 집합 구축 ─────────────────────
        feedback.pushInfo(_section("[2단계] 상위중심지 공간 교차 분석 (격자 중심점 기준)"))
        transform = None
        if upper_layer.crs() != sub_layer.crs():
            transform = QgsCoordinateTransform(
                upper_layer.crs(), sub_layer.crs(), QgsProject.instance()
            )

        upper_center_grids = {}  # upper_id → set of grid_ids
        for uf in upper_layer.getFeatures():
            if feedback.isCanceled():
                return {}
            if str(uf[upper_type_fld]) not in upper_types:
                continue

            upper_id = str(uf[upper_id_fld])
            upper_geom = uf.geometry()
            if transform:
                upper_geom = QgsGeometry(upper_geom)
                upper_geom.transform(transform)

            grid_ids = set()
            for fid in sub_index.intersects(upper_geom.boundingBox()):
                sf = sub_by_fid.get(fid)
                if sf is None:
                    continue
                if upper_geom.contains(sf.geometry().centroid()):
                    grid_ids.add(str(sf[sub_grid_fld]))

            if grid_ids:
                upper_center_grids.setdefault(upper_id, set()).update(grid_ids)

        feedback.pushInfo(_item("상위중심지", f"{len(upper_center_grids)}개 (포함 격자 추출 완료)"))
        feedback.setProgress(25)

        if not upper_center_grids:
            raise QgsProcessingException(
                "선택한 유형에 해당하는 상위중심지가 없거나 교차 격자를 찾지 못했습니다."
            )

        # ── 3. 격자 → 상위중심지 역방향 매핑 ────────────────────────────
        grid_to_upper = {}  # grid_id → set of upper_ids
        for uid, gids in upper_center_grids.items():
            for gid in gids:
                grid_to_upper.setdefault(gid, set()).add(uid)

        # ── [3단계] CSV 단일 패스로 양방향 통행량 집계 ─────────────────────
        # result[(zone_id, upper_id)] = [하위→상위 통행량, 상위→하위 통행량]
        feedback.pushInfo(_section("[3단계] 통행 행렬 집계 (양방향)"))
        feedback.pushInfo(_item("CSV", csv_path))

        file_obj = self._open_csv(csv_path)
        result = {}
        row_count = 0
        matched_fwd = 0
        matched_rev = 0

        try:
            reader = csv.DictReader(file_obj)
            for row in reader:
                if feedback.isCanceled():
                    break

                row_count += 1
                if row_count % 500_000 == 0:
                    feedback.pushInfo(f"  처리 행: {row_count:,}")

                from_id = str(row[csv_from])
                to_id = str(row[csv_to])
                if from_id == to_id:
                    continue

                try:
                    mobility = float(row[csv_trip])
                except (ValueError, TypeError):
                    continue

                # 방향 1: 하위생활권 격자 → 상위중심지 격자
                zone_id = grid_to_zone.get(from_id)
                upper_ids = grid_to_upper.get(to_id)
                if zone_id is not None and upper_ids:
                    for uid in upper_ids:
                        entry = result.setdefault((zone_id, uid), [0.0, 0.0])
                        entry[0] += mobility
                    matched_fwd += 1

                # 방향 2: 상위중심지 격자 → 하위생활권 격자
                upper_ids_from = grid_to_upper.get(from_id)
                zone_id_to = grid_to_zone.get(to_id)
                if upper_ids_from and zone_id_to is not None:
                    for uid in upper_ids_from:
                        entry = result.setdefault((zone_id_to, uid), [0.0, 0.0])
                        entry[1] += mobility
                    matched_rev += 1

        finally:
            file_obj.close()

        feedback.pushInfo(
            f"  → 총 {row_count:,}행 처리 / "
            f"하위→상위 {matched_fwd:,}행, 상위→하위 {matched_rev:,}행 매칭"
        )
        feedback.setProgress(90)

        # ── [4단계] 결과 CSV 저장 (헤더: 영문 표준) ────────────────────────
        feedback.pushInfo(_section("[4단계] 결과 CSV 저장"))
        with open(output_path, "w", encoding="utf-8-sig", newline="") as out:
            writer = csv.writer(out)
            writer.writerow(["zone_id", "upper_id", "in_trips", "out_trips"])
            for (zone_id, uid), (fwd, rev) in sorted(result.items()):
                # 상위중심지 관점: in_trips = 하위→상위(유입), out_trips = 상위→하위(유출)
                writer.writerow([zone_id, uid, fwd, rev])

        feedback.pushInfo(_item("(하위생활권, 상위중심지) 쌍", f"{len(result)}개"))
        feedback.pushInfo(_item("저장 위치", output_path))
        feedback.pushInfo(_bar())
        feedback.pushInfo("완료: '8. 매력도 기반 상위생활권 도출_update'의 통행 근거 자료로 사용하세요.")
        feedback.setProgress(100)
        self._output_path = output_path
        return {self.OUTPUT: output_path}

    def postProcessAlgorithm(self, context, feedback) -> dict:
        output_path = getattr(self, "_output_path", None)
        if not output_path:
            return {}
        layer = QgsVectorLayer(output_path, "하위생활권-상위중심지 이동량 행렬", "ogr")
        if layer and layer.isValid():
            QgsProject.instance().addMapLayer(layer)
        return {}

    def _extract_csv_path(self, layer):
        """delimitedtext 레이어의 소스 URI에서 실제 파일 경로를 추출한다."""
        source = layer.source()
        # 쿼리 스트링 제거 (file.csv?type=csv&... → file.csv)
        path = source.split("?")[0]
        # file:/// 스킴 제거 및 URL 디코딩
        if path.lower().startswith("file:///"):
            path = urllib.parse.unquote(path[8:])
            # Windows: /C:/path → C:/path
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
