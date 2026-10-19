import math
from typing import Callable, Dict, List, Optional

from qgis.core import (
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsFields,
    QgsProject,
    QgsSpatialIndex,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from PyQt5.QtCore import QVariant

from .classifier import ConditionEvaluator
from .models import AnalysisConfig, GeojeomConfig, InfraConfig
from .utils import compute_infra_stats, compute_stats, safe_float

_POP_FIELDS = [
    ("field_resident_pop", "rpop", "res_pop_stats"),
    ("field_work_pop",     "wpop", "wor_pop_stats"),
]
_CENT_FIELDS = [
    ("field_inflow",  "pcin",  "inflow_stats"),
    ("field_outflow", "pcout", "outflow_stats"),
]


class AnalysisCancelledError(Exception):
    pass


class SpatialProcessor:
    def __init__(self, config: AnalysisConfig) -> None:
        self.config = config
        self.cancel_requested = False
        self.evaluator = ConditionEvaluator(config)

    # ------------------------------------------------------------------ #
    # Phase 1: 통계 산출 → GeoPackage 저장 (분류 필드 없음)                #
    # ------------------------------------------------------------------ #

    def execute(self, progress_callback: Optional[Callable] = None) -> None:
        def _p(pct, msg):
            if progress_callback:
                progress_callback(pct, msg)

        _p(0, "레이어 로드 중...")
        center_layer, geojeom_layer, infra_layer = self._load_layers()

        _p(5, "공간 인덱스 구축 중...")
        geojeom_index = QgsSpatialIndex(geojeom_layer.getFeatures())
        infra_index = QgsSpatialIndex(infra_layer.getFeatures())

        gcfg = self.config.geojeom_cfg
        icfg = self.config.infra_cfg

        geojeom_needed = [f for f in [
            gcfg.field_resident_pop, gcfg.field_work_pop,
            gcfg.field_inflow, gcfg.field_outflow,
        ] if f]
        infra_needed = list(set(icfg.village_cols + icfg.base_cols))

        geojeom_idx = [geojeom_layer.fields().indexOf(f) for f in geojeom_needed]
        infra_idx   = [infra_layer.fields().indexOf(f) for f in infra_needed]

        _p(10, "격자 데이터 캐시 구축 중...")
        geojeom_by_fid = {
            f.id(): f for f in geojeom_layer.getFeatures(
                QgsFeatureRequest().setSubsetOfAttributes(geojeom_idx)
            )
        }
        infra_by_fid = {
            f.id(): f for f in infra_layer.getFeatures(
                QgsFeatureRequest().setSubsetOfAttributes(infra_idx)
            )
        }

        geojeom_tr = self._make_transform(center_layer.crs(), geojeom_layer.crs())
        infra_tr   = self._make_transform(center_layer.crs(), infra_layer.crs())

        _p(15, "중심지별 분석 시작...")
        center_features = list(center_layer.getFeatures())
        total = len(center_features)
        output_records = []

        for i, feat in enumerate(center_features):
            if self.cancel_requested:
                raise AnalysisCancelledError("취소되었습니다.")

            geom = feat.geometry()
            area = geom.area()

            g_feats = self._get_intersecting(geom, geojeom_index, geojeom_by_fid, geojeom_tr)
            i_feats = self._get_intersecting(geom, infra_index, infra_by_fid, infra_tr)

            # 원본 중심지 속성 보존
            center_attrs = {f.name(): feat[f.name()] for f in center_layer.fields()}

            computed = {
                **self._compute_geojeom_stats(g_feats, gcfg, area),
                **self._compute_infra_stats(i_feats, icfg),
            }
            output_records.append({
                "geometry": geom,
                "center_attrs": center_attrs,
                "computed": computed,
            })

            _p(15 + int(80 * (i + 1) / max(total, 1)), f"처리 중: {i+1}/{total}")

        _p(95, "Shapefile 저장 중...")
        self._write_output(output_records, center_layer)
        _p(100, "완료")

    # ------------------------------------------------------------------ #
    # Phase 1.4: 분류 결과 중 '이외' 폴리곤 삭제                           #
    # ------------------------------------------------------------------ #

    def execute_delete_outside(self, progress_callback: Optional[Callable] = None) -> int:
        """'이외'로 분류된 피처 삭제. 삭제된 피처 수를 반환."""
        def _p(pct, msg):
            if progress_callback:
                progress_callback(pct, msg)

        _p(0, "'이외' 폴리곤 제거 중...")
        uri = f"{self.config.output_path}|layername=분류결과"
        layer = QgsVectorLayer(uri, "분류결과_이외제거", "ogr")
        if not layer.isValid():
            raise ValueError(f"결과 파일을 열 수 없습니다:\n{self.config.output_path}")

        cls_idx = layer.fields().indexOf("cent_cls")
        if cls_idx < 0:
            cls_idx = layer.fields().indexOf("분류")  # 구버전 산출물 호환
        if cls_idx < 0:
            return 0

        fids_to_delete = [
            f.id() for f in layer.getFeatures()
            if str(f[cls_idx]) == "이외"
        ]

        if fids_to_delete:
            layer.startEditing()
            layer.dataProvider().deleteFeatures(fids_to_delete)
            if not layer.commitChanges():
                layer.rollBack()
                raise RuntimeError("'이외' 폴리곤 삭제를 저장하지 못했습니다.")

        _p(100, f"'이외' 폴리곤 {len(fids_to_delete)}개 삭제 완료")
        return len(fids_to_delete)

    # ------------------------------------------------------------------ #
    # Phase 2: 기존 GeoPackage에 분류 필드 추가·업데이트                    #
    # ------------------------------------------------------------------ #

    def execute_phase2(self, progress_callback: Optional[Callable] = None) -> None:
        def _p(pct, msg):
            if progress_callback:
                progress_callback(pct, msg)

        _p(0, "결과 파일 로드 중...")
        uri = f"{self.config.output_path}|layername=분류결과"
        layer = QgsVectorLayer(uri, "분류결과_편집", "ogr")
        if not layer.isValid():
            raise ValueError(f"결과 파일을 열 수 없습니다:\n{self.config.output_path}")

        layer.startEditing()

        # 분류 필드가 없으면 추가
        if layer.fields().indexOf("cent_cls") < 0:
            layer.dataProvider().addAttributes([QgsField("cent_cls", QVariant.String, len=20)])
            layer.updateFields()

        cls_idx = layer.fields().indexOf("cent_cls")
        if cls_idx < 0:
            layer.rollBack()
            raise RuntimeError("'cent_cls' 필드를 추가할 수 없습니다.")

        _p(10, "분류 적용 중...")
        total = layer.featureCount()

        for i, feat in enumerate(layer.getFeatures()):
            if self.cancel_requested:
                layer.rollBack()
                raise AnalysisCancelledError("취소되었습니다.")

            attrs = {f.name(): feat[f.name()] for f in layer.fields()}
            category = self.evaluator.classify(attrs)
            layer.changeAttributeValue(feat.id(), cls_idx, category)

            _p(10 + int(85 * (i + 1) / max(total, 1)), f"분류 중: {i+1}/{total}")

        _p(95, "저장 중...")
        if not layer.commitChanges():
            raise RuntimeError("변경 사항을 저장하지 못했습니다.")
        _p(100, "완료")

    # ------------------------------------------------------------------ #
    # Phase 1.5: 읍면동 기반 중복 폴리곤 제거                              #
    # 같은 읍면동 내 중심점을 가진 폴리곤 중 rpop_sum 최대값만 보존       #
    # ------------------------------------------------------------------ #

    def execute_dedup(self, progress_callback: Optional[Callable] = None) -> int:
        """읍면동 기반 중복 제거. 삭제된 피처 수를 반환."""
        emd_path = self.config.emd_layer_path
        if not emd_path:
            return 0

        def _p(pct, msg):
            if progress_callback:
                progress_callback(pct, msg)

        _p(0, "읍면동 경계 로드 중...")
        emd_layer = QgsVectorLayer(emd_path, "읍면동", "ogr")
        if not emd_layer.isValid():
            raise ValueError(f"읍면동 경계 파일을 로드할 수 없습니다:\n{emd_path}")

        _p(5, "결과 파일 로드 중...")
        uri = f"{self.config.output_path}|layername=분류결과"
        layer = QgsVectorLayer(uri, "분류결과_중복제거", "ogr")
        if not layer.isValid():
            raise ValueError(f"결과 파일을 열 수 없습니다:\n{self.config.output_path}")

        _p(10, "공간 인덱스 구축 중...")
        emd_index = QgsSpatialIndex(emd_layer.getFeatures())
        emd_by_fid = {f.id(): f for f in emd_layer.getFeatures()}

        tr = self._make_transform(layer.crs(), emd_layer.crs())

        # rpop_sum 필드가 출력에 있는지 확인
        field_names = [f.name() for f in layer.fields()]
        pop_field = "rpop_sum" if "rpop_sum" in field_names else None

        _p(20, "읍면동별 그룹화 중...")
        total = layer.featureCount()
        emd_groups: Dict[int, List] = {}  # emd_fid -> [(feat_id, pop_sum)]

        for i, feat in enumerate(layer.getFeatures()):
            if self.cancel_requested:
                raise AnalysisCancelledError("취소되었습니다.")

            geom = feat.geometry()
            if tr:
                from qgis.core import QgsGeometry
                geom_t = QgsGeometry(geom)
                geom_t.transform(tr)
                centroid = geom_t.centroid()
            else:
                centroid = geom.centroid()

            for emd_fid in emd_index.intersects(centroid.boundingBox()):
                emd_feat = emd_by_fid.get(emd_fid)
                if emd_feat and emd_feat.geometry().contains(centroid):
                    pop_sum = safe_float(feat[pop_field]) if pop_field else 0.0
                    emd_groups.setdefault(emd_fid, []).append((feat.id(), pop_sum))
                    break

            if total > 0:
                _p(20 + int(40 * (i + 1) / total), f"그룹화 중: {i+1}/{total}")

        _p(60, "중복 피처 식별 중...")
        fids_to_delete = []
        for group in emd_groups.values():
            if len(group) > 1:
                sorted_group = sorted(group, key=lambda x: x[1], reverse=True)
                fids_to_delete.extend(fid for fid, _ in sorted_group[1:])

        if fids_to_delete:
            _p(70, f"중복 폴리곤 {len(fids_to_delete)}개 삭제 중...")
            layer.startEditing()
            layer.dataProvider().deleteFeatures(fids_to_delete)
            if not layer.commitChanges():
                layer.rollBack()
                raise RuntimeError("중복 폴리곤 삭제를 저장하지 못했습니다.")

        _p(100, f"중복 제거 완료 (삭제: {len(fids_to_delete)}개)")
        return len(fids_to_delete)

    # ------------------------------------------------------------------ #
    # 레이어 로드 (파일 경로 기반)                                          #
    # ------------------------------------------------------------------ #

    def _load_layers(self):
        def _get(path: str, label: str) -> QgsVectorLayer:
            if not path:
                raise ValueError(f"{label} 파일 경로가 지정되지 않았습니다.")

            # 파일 기반 레이어 시도
            layer = QgsVectorLayer(path, label, "ogr")
            if layer.isValid():
                return layer

            # 임시(메모리) 레이어: 프로젝트에 로드된 레이어 중 source가 일치하는 것을 반환
            for lyr in QgsProject.instance().mapLayers().values():
                if lyr.source() == path and lyr.isValid():
                    return lyr

            raise ValueError(f"{label} 파일을 로드할 수 없습니다:\n{path}")

        return (
            _get(self.config.center_layer_path,  "중심지"),
            _get(self.config.geojeom_layer_path, "국토공간거점지도"),
            _get(self.config.infra_layer_path,   "생활인프라충족도"),
        )

    # ------------------------------------------------------------------ #
    # 공간 교차                                                            #
    # ------------------------------------------------------------------ #

    def _make_transform(self, src_crs, dst_crs) -> Optional[QgsCoordinateTransform]:
        if src_crs == dst_crs:
            return None
        return QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())

    def _get_intersecting(self, center_geom, index, features_by_fid, transform):
        """격자 중심점이 중심지 폴리곤 내부에 포함되는 격자만 반환."""
        from qgis.core import QgsGeometry
        if transform:
            bbox = transform.transformBoundingBox(center_geom.boundingBox())
            geom_t = QgsGeometry(center_geom)
            geom_t.transform(transform)
        else:
            bbox = center_geom.boundingBox()
            geom_t = center_geom

        result = []
        for fid in index.intersects(bbox):
            feat = features_by_fid.get(fid)
            if feat is None:
                continue
            centroid = feat.geometry().centroid()
            if geom_t.contains(centroid):
                result.append(feat)
        return result

    # ------------------------------------------------------------------ #
    # 통계 계산                                                            #
    # ------------------------------------------------------------------ #

    def _compute_geojeom_stats(self, features, cfg: GeojeomConfig, area: float) -> Dict:
        result = {}
        for field_attr, base_key, stats_attr in _POP_FIELDS:
            fname = getattr(cfg, field_attr)
            if not fname:
                continue
            values = [safe_float(f[fname]) for f in features]
            for k, v in compute_stats(values, getattr(cfg, stats_attr), area=area).items():
                result[f"{base_key}_{k}"] = v

        for field_attr, base_key, stats_attr in _CENT_FIELDS:
            fname = getattr(cfg, field_attr)
            if not fname:
                continue
            values = [safe_float(f[fname]) for f in features]
            for k, v in compute_stats(values, getattr(cfg, stats_attr), area=area).items():
                result[f"{base_key}_{k}"] = v
        return result

    def _compute_infra_stats(self, features, cfg: InfraConfig) -> Dict:
        n_v, n_b = len(cfg.village_cols), len(cfg.base_cols)
        per_total, per_village, per_base = [], [], []

        for feat in features:
            v = sum(safe_float(feat[c]) for c in cfg.village_cols if c)
            b = sum(safe_float(feat[c]) for c in cfg.base_cols if c)
            per_village.append(v)
            per_base.append(b)
            per_total.append(v + b)

        result = {}
        items = []
        if cfg.compute_total:   items.append(("tfac", per_total,   n_v + n_b))
        if cfg.compute_village: items.append(("vfac", per_village, n_v))
        if cfg.compute_base:    items.append(("bfac", per_base,    n_b))

        for base_key, values, n_cols in items:
            for k, v in compute_infra_stats(values, cfg.stats, n_cols).items():
                result[f"{base_key}_{k}"] = v
        return result

    # ------------------------------------------------------------------ #
    # 출력 쓰기 (확장자에 따라 Shapefile / GeoPackage)                     #
    # ------------------------------------------------------------------ #

    def _build_fields(self, center_layer: QgsVectorLayer, sample_computed: Dict) -> QgsFields:
        fields = QgsFields()
        center_field_names = set()

        # 원본 중심지 필드를 먼저 추가 (원래 타입 유지)
        for f in center_layer.fields():
            fields.append(f)
            center_field_names.add(f.name())

        # 계산된 통계 필드 추가 (이름 중복 방지)
        for key in sample_computed:
            if key not in center_field_names:
                fields.append(QgsField(key, QVariant.Double))

        return fields

    def _write_output(self, records: List[Dict], center_layer: QgsVectorLayer) -> None:
        if not records:
            raise ValueError("출력할 데이터가 없습니다.")

        fields = self._build_fields(center_layer, records[0]["computed"])

        is_shp = self.config.output_path.lower().endswith(".shp")
        driver = "ESRI Shapefile" if is_shp else "GPKG"

        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = driver
        options.fileEncoding = "UTF-8"
        if not is_shp:
            options.layerName = "분류결과"

        ctx = QgsCoordinateTransformContext()

        # QGIS 버전 호환 (create() 우선, 구버전은 생성자 사용)
        writer = None
        try:
            result = QgsVectorFileWriter.create(
                self.config.output_path, fields,
                QgsWkbTypes.MultiPolygon, center_layer.crs(),
                ctx, options,
            )
            writer = result[0] if isinstance(result, tuple) else result
        except (AttributeError, TypeError):
            pass

        if writer is None:
            writer = QgsVectorFileWriter(
                self.config.output_path, "UTF-8", fields,
                QgsWkbTypes.MultiPolygon, center_layer.crs(),
                driver, [], [] if is_shp else ["LAYER_NAME=분류결과"],
            )

        if hasattr(writer, "hasError") and writer.hasError() != QgsVectorFileWriter.NoError:
            raise RuntimeError(f"출력 파일 생성 오류: {writer.errorMessage()}")

        for rec in records:
            feat = QgsFeature(fields)
            geom = rec["geometry"]
            if not geom.isMultipart():
                geom = geom.convertToMultiType()
            feat.setGeometry(geom)

            center_attrs = rec["center_attrs"]
            computed = rec["computed"]
            attrs = []
            for f in fields:
                name = f.name()
                if name in center_attrs:
                    attrs.append(center_attrs[name])
                elif name in computed:
                    attrs.append(computed[name])
                else:
                    attrs.append(None)
            feat.setAttributes(attrs)
            writer.addFeature(feat)

        del writer
