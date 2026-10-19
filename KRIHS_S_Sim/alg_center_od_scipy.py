import time
import numpy as np
from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterField,
    QgsProcessingParameterString,
    QgsProcessingParameterFeatureSink,
    QgsProcessing,
    QgsProcessingException,
    QgsVectorLayer,
    QgsProject,
    QgsFeatureSink,
    QgsFields, QgsField, QgsFeature, QgsGeometry, QgsPointXY,
    QgsCoordinateTransform, QgsWkbTypes,
)
from qgis.analysis import (
    QgsVectorLayerDirector, QgsNetworkDistanceStrategy, QgsGraphBuilder,
)


class OdMatrixRoadScipyAlgorithm(QgsProcessingAlgorithm):
    """기존 6번 툴(od_matrix_road_distance)과 형식·내용 동일.
    내부 경로탐색 엔진만 QNeat3 → scipy로 교체."""

    INPUT = "INPUT"
    NETWORK_LAYER = "NETWORK_LAYER"
    TYPE_FIELD = "TYPE_FIELD"
    FROM_TYPE = "FROM_TYPE"
    FROM_ID_FIELD = "FROM_ID_FIELD"
    TO_TYPE = "TO_TYPE"
    TO_ID_FIELD = "TO_ID_FIELD"
    OUTPUT = "OUTPUT"

    _ONEWAY = "ONEWAY"
    _TYPE_WIDGET = (
        "krihs_spst_sim_tools.cnt_classifier.processing."
        "field_value_widget.FieldValueWidgetWrapper"
    )
    _CH = 64

    def name(self) -> str:
        return "od_matrix_road_distance_scipy"

    def displayName(self) -> str:
        return "6. 중심지-중심지 거리(도로) 행렬 산출 (scipy)"

    def group(self) -> str:
        return "2. 중심지-생활권 경계 도출"

    def groupId(self) -> str:
        return "region_boundary"

    def createInstance(self):
        return OdMatrixRoadScipyAlgorithm()

    def shortHelpString(self) -> str:
        return (
            "중심지 대표점 간 도로 네트워크 최단거리 행렬을 산출합니다. "
            "기존 6번 툴과 형식·입출력이 동일하며, 경로탐색 엔진만 QNeat3 대신 scipy를 씁니다.\n\n"
            "【입력】\n"
            "  · 중심지 대표점 : '5. 인구가중 중심지 대표점 추출_update' 출력 (cent_cls·cand_id)\n"
            "  · 도로망 : KTDB GIS 도로망도, EPSG:5179 — ONEWAY 필드 필요\n"
            "  · 기점·종점 유형 : 대표점 유형 필드 고유값에서 다중 선택\n\n"
            "【출력 필드】 origin_id / destination_id / entry_cost / network_cost / exit_cost / total_cost(m)"
        )

    def initAlgorithm(self, config=None) -> None:
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT,
            "중심지 대표점 레이어 ('5. 인구가중 중심지 대표점 추출_update' 출력)",
            types=[QgsProcessing.TypeVectorPoint],
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.NETWORK_LAYER,
            "도로망 레이어 (KTDB GIS 도로망도, EPSG:5179 — ONEWAY 필드 필요)",
            types=[QgsProcessing.TypeVectorLine],
        ))
        self.addParameter(QgsProcessingParameterField(
            self.TYPE_FIELD,
            "   ↳ 중심지 유형 필드 (기본: cent_cls)",
            defaultValue="cent_cls",
            parentLayerParameterName=self.INPUT,
            type=QgsProcessingParameterField.String,
        ))
        self.addParameter(self._make_type_param(
            self.FROM_TYPE, "   ↳ 기점 중심지 유형 (필드 고유값에서 선택)"))
        self.addParameter(QgsProcessingParameterField(
            self.FROM_ID_FIELD, "   ↳ 기점 고유 ID 필드 (기본: cand_id)",
            defaultValue="cand_id", parentLayerParameterName=self.INPUT,
        ))
        self.addParameter(self._make_type_param(
            self.TO_TYPE, "   ↳ 종점 중심지 유형 (필드 고유값에서 선택)"))
        self.addParameter(QgsProcessingParameterField(
            self.TO_ID_FIELD, "   ↳ 종점 고유 ID 필드 (기본: cand_id)",
            defaultValue="cand_id", parentLayerParameterName=self.INPUT,
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, "중심지-중심지 OD 행렬", type=QgsProcessing.TypeVectorLine,
        ))

    def _make_type_param(self, name, label):
        """'중심지 유형 필드'의 고유값을 체크 콤보로 채우는 커스텀 위젯(원본과 동일)."""
        param = QgsProcessingParameterString(name, label, defaultValue="")
        param.setMetadata({
            "widget_wrapper": {
                "class": self._TYPE_WIDGET,
                "layer_param": self.INPUT,
                "field_param": self.TYPE_FIELD,
            }
        })
        return param

    @staticmethod
    def _parse_types(parameters, key):
        raw = parameters.get(key)
        if isinstance(raw, str):
            return {s.strip() for s in raw.split(",") if s.strip()}
        if raw:
            return {str(x).strip() for x in raw if str(x).strip()}
        return set()

    def _make_filtered_layer(self, layer, type_field, type_values, label):
        """유형 값 목록으로 필터링한 임시 메모리 포인트 레이어 반환 (원본과 동일)."""
        value_set = {str(v) for v in type_values}
        tmp = QgsVectorLayer(f"Point?crs={layer.crs().authid()}", label, "memory")
        dp = tmp.dataProvider()
        dp.addAttributes(layer.fields().toList())
        tmp.updateFields()
        feats = [f for f in layer.getFeatures() if str(f[type_field]) in value_set]
        dp.addFeatures(feats)
        return tmp

    def checkParameterValues(self, parameters, context):
        try:
            import scipy  # noqa: F401
        except ImportError:
            return False, "scipy를 불러올 수 없습니다. 표준 QGIS(Windows)에는 번들되어 있습니다."
        return super().checkParameterValues(parameters, context)

    def processAlgorithm(self, parameters, context, feedback):
        from scipy.sparse import csr_matrix
        from scipy.sparse.csgraph import dijkstra

        center_layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        network_layer = self.parameterAsVectorLayer(parameters, self.NETWORK_LAYER, context)
        type_field = self.parameterAsString(parameters, self.TYPE_FIELD, context)
        from_types = self._parse_types(parameters, self.FROM_TYPE)
        from_id = self.parameterAsString(parameters, self.FROM_ID_FIELD, context)
        to_types = self._parse_types(parameters, self.TO_TYPE)
        to_id = self.parameterAsString(parameters, self.TO_ID_FIELD, context)

        if not from_types:
            raise QgsProcessingException("기점 중심지 유형을 1개 이상 선택하세요.")
        if not to_types:
            raise QgsProcessingException("종점 중심지 유형을 1개 이상 선택하세요.")

        net_fields = [f.name() for f in network_layer.fields()]
        if self._ONEWAY not in net_fields:
            raise QgsProcessingException(
                f"네트워크 레이어에 '{self._ONEWAY}' 필드가 없습니다. "
                f"현재 필드 목록: {', '.join(net_fields[:30])}")

        from_layer = self._make_filtered_layer(center_layer, type_field, from_types, "기점")
        to_layer = self._make_filtered_layer(center_layer, type_field, to_types, "종점")
        if from_layer.featureCount() == 0:
            raise QgsProcessingException(f"기점 피처 없음: 유형 필드='{type_field}', 값={from_types}")
        if to_layer.featureCount() == 0:
            raise QgsProcessingException(f"종점 피처 없음: 유형 필드='{type_field}', 값={to_types}")

        feedback.pushInfo(f"기점 피처 수: {from_layer.featureCount()}")
        feedback.pushInfo(f"종점 피처 수: {to_layer.featureCount()}")

        net_crs = network_layer.sourceCrs()
        t0 = time.time()

        # 기점/종점 점 읽기 (네트워크 CRS)
        def read(layer, idf):
            tr = QgsCoordinateTransform(layer.crs(), net_crs, QgsProject.instance()) \
                 if layer.crs() != net_crs else None
            ids = []; pts = []
            for f in layer.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                p = g.centroid()
                if tr:
                    p.transform(tr)
                q = p.asPoint()
                ids.append(str(f[idf])); pts.append(QgsPointXY(q.x(), q.y()))
            return ids, pts
        f_id, f_xy = read(from_layer, from_id)
        t_id, t_xy = read(to_layer, to_id)

        # 그래프 구축 (QGIS 엔진, 수직 스냅, ONEWAY) — QNeat3와 동일
        feedback.pushInfo("그래프 구축 (QgsGraphBuilder)...")
        tg = time.time()
        fidx = network_layer.fields().indexOf(self._ONEWAY)
        director = QgsVectorLayerDirector(network_layer, fidx, "1", "", "0",
                                          QgsVectorLayerDirector.DirectionBoth)
        director.addStrategy(QgsNetworkDistanceStrategy())
        builder = QgsGraphBuilder(net_crs)
        tied = director.makeGraph(builder, f_xy + t_xy)
        graph = builder.graph()
        feedback.pushInfo(f"  정점 {graph.vertexCount():,} · 엣지 {graph.edgeCount():,} ({time.time()-tg:.1f}s)")

        nF = len(f_xy)
        entry = np.array([f_xy[i].distance(tied[i]) for i in range(nF)])
        exitc = np.array([t_xy[j].distance(tied[nF + j]) for j in range(len(t_xy))])

        # CSR 추출 + 좌표→정점 인덱스 + scipy 다중출발 (기점→종점, 정방향)
        feedback.pushInfo("scipy 최단경로...")
        V = graph.vertexCount()
        rows = []; cols = []; wts = []; coord2vid = {}
        for i in range(V):
            p = graph.vertex(i).point()
            coord2vid[(round(p.x(), 3), round(p.y(), 3))] = i
            for eid in graph.vertex(i).outgoingEdges():
                e = graph.edge(eid); rows.append(i); cols.append(e.toVertex()); wts.append(e.cost(0))
        csr = csr_matrix((np.asarray(wts), (np.asarray(rows), np.asarray(cols))), shape=(V, V))
        def vid_of(pt): return coord2vid.get((round(pt.x(), 3), round(pt.y(), 3)), -1)
        f_vid = np.array([vid_of(tied[i]) for i in range(nF)])
        t_vid = np.array([vid_of(tied[nF + j]) for j in range(len(t_xy))])
        uf, uf_inv = np.unique(f_vid, return_inverse=True)
        ts = time.time()
        netmat = np.full((len(uf), len(t_id)), np.inf)   # [기점노드, 종점] = 기점→종점
        for s in range(0, len(uf), self._CH):
            if feedback.isCanceled():
                return {}
            idx = uf[s:s + self._CH]
            dd = dijkstra(csr, directed=True, indices=idx, min_only=False)
            netmat[s:s + len(idx), :] = dd[:, t_vid]
        feedback.pushInfo(f"  Dijkstra {len(uf):,}출발 완료 ({time.time()-ts:.1f}s)")

        # 출력 (직선 라인 + QNeat3 동일 스키마)
        fields = QgsFields()
        for nm in ("origin_id", "destination_id"):
            fields.append(QgsField(nm, QVariant.String))
        for nm in ("entry_cost", "network_cost", "exit_cost", "total_cost"):
            fields.append(QgsField(nm, QVariant.Double))
        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context, fields, QgsWkbTypes.LineString, net_crs)

        nrow = 0; nskip = 0
        for i in range(nF):
            if feedback.isCanceled():
                break
            ec = float(entry[i]); r = uf_inv[i]
            for j in range(len(t_id)):
                ncst = netmat[r, j]
                if not np.isfinite(ncst):
                    nskip += 1; continue
                xc = float(exitc[j])
                feat = QgsFeature(fields)
                feat.setGeometry(QgsGeometry.fromPolylineXY([f_xy[i], t_xy[j]]))
                feat.setAttributes([f_id[i], t_id[j], ec, float(ncst), xc, ec + float(ncst) + xc])
                sink.addFeature(feat, QgsFeatureSink.FastInsert)
                nrow += 1

        feedback.pushInfo(f"완료: OD 행렬 {nrow:,}행 (도달불가 {nskip:,} 제외) · 전체 {time.time()-t0:.1f}s")
        return {self.OUTPUT: dest_id}