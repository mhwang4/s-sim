import time
import numpy as np
from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingParameterVectorLayer, QgsProcessingParameterField,
    QgsProcessingParameterFileDestination,
    QgsCoordinateTransform, QgsProject, QgsGeometry, QgsPointXY, QgsVectorLayer,
    QgsFeature, QgsSpatialIndex,
)
from qgis.analysis import (
    QgsVectorLayerDirector, QgsNetworkDistanceStrategy, QgsGraphBuilder,
)


class GridCenterOdScipyGraph(QgsProcessingAlgorithm):
    """격자→중심지 OD 행렬 (도로거리, QGIS그래프+scipy).
    출발 격자 선별: 읍면동을 단일파트로 분해해 '도로와 교차하는 파트(육지)'에 겹치는 격자만 O로 사용
    (도로 없는 섬 파트 제외). 최단경로는 scipy 다중출발. 클리핑 없음(전체 도로망)."""

    GRID='GRID'; GRID_ID='GRID_ID'
    DONG='DONG'
    NETWORK='NETWORK'; ONEWAY_FIELD='ONEWAY_FIELD'
    CENTER='CENTER'; CENTER_ID='CENTER_ID'
    OUTPUT='OUTPUT'
    _CH = 64

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.GRID, '격자 레이어 (1km, 폴리곤)', [QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterField(
            self.GRID_ID, '  ↳ 격자 ID 필드 (기본: gid)', defaultValue='gid',
            parentLayerParameterName=self.GRID))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.DONG, '읍면동 경계 레이어 (선택 — 도로 있는 동 단일파트에 겹치는 격자만 사용)',
            [QgsProcessing.TypeVectorPolygon], optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.NETWORK, '도로망 레이어 (KTDB, EPSG:5179)', [QgsProcessing.TypeVectorLine]))
        self.addParameter(QgsProcessingParameterField(
            self.ONEWAY_FIELD, '  ↳ 일방통행 필드 (기본: ONEWAY)',
            defaultValue='ONEWAY', parentLayerParameterName=self.NETWORK, optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.CENTER, '중심지 중심점 레이어', [QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterField(
            self.CENTER_ID, '  ↳ 중심지 점 ID 필드 (기본: gid)', defaultValue='gid',
            parentLayerParameterName=self.CENTER))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT, 'OD 행렬 CSV', fileFilter='CSV 파일 (*.csv)'))

    def checkParameterValues(self, parameters, context):
        try:
            import scipy  # noqa: F401
        except ImportError:
            return False, ('scipy를 불러올 수 없습니다. 표준 QGIS(Windows)에는 번들되어 있습니다.')
        for key, label in ((self.GRID, '격자'), (self.CENTER, '중심지'), (self.NETWORK, '도로망')):
            lyr = self.parameterAsVectorLayer(parameters, key, context)
            if lyr is not None and lyr.sourceCrs().isGeographic():
                return False, (f'{label} 레이어가 지리좌표계({lyr.sourceCrs().authid()})입니다. '
                               'EPSG:5179 등 미터 투영좌표계를 사용하세요.')
        return super().checkParameterValues(parameters, context)

    def processAlgorithm(self, parameters, context, feedback):
        from scipy.sparse import csr_matrix
        from scipy.sparse.csgraph import dijkstra

        grid   = self.parameterAsVectorLayer(parameters, self.GRID, context)
        gidf   = self.parameterAsString(parameters, self.GRID_ID, context)
        dong   = self.parameterAsVectorLayer(parameters, self.DONG, context)
        net    = self.parameterAsVectorLayer(parameters, self.NETWORK, context)
        owf    = self.parameterAsString(parameters, self.ONEWAY_FIELD, context)
        center = self.parameterAsVectorLayer(parameters, self.CENTER, context)
        cidf   = self.parameterAsString(parameters, self.CENTER_ID, context)
        out    = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        if not out.lower().endswith('.csv'):
            out += '.csv'

        net_crs = net.sourceCrs(); grid_crs = grid.sourceCrs()
        t0 = time.time()

        # ── [1] 출발 격자 선별 (도로 있는 '읍면동 단일파트'에 겹치는 격자만) ──
        served_idx = None; served_geoms = None
        if dong is not None:
            feedback.pushInfo('[1] 읍면동 단일파트 분해 → 도로서비스 파트 추출...')
            r_tr = QgsCoordinateTransform(net_crs, grid_crs, QgsProject.instance()) \
                   if net_crs != grid_crs else None
            road_idx = QgsSpatialIndex(); road_geoms = {}
            for nf in net.getFeatures():
                gm = nf.geometry()
                if r_tr:
                    gm = QgsGeometry(gm); gm.transform(r_tr)
                road_geoms[nf.id()] = gm
                tf = QgsFeature(nf.id()); tf.setGeometry(gm); road_idx.insertFeature(tf)

            d_tr = QgsCoordinateTransform(dong.sourceCrs(), grid_crs, QgsProject.instance()) \
                   if dong.sourceCrs() != grid_crs else None
            served_idx = QgsSpatialIndex(); served_geoms = {}; k = 0; n_part = 0
            for df in dong.getFeatures():
                if feedback.isCanceled(): return {}
                dg = df.geometry()
                if dg is None or dg.isEmpty(): continue
                if d_tr:
                    dg = QgsGeometry(dg); dg.transform(d_tr)
                parts = dg.asGeometryCollection() if dg.isMultipart() else [dg]
                for part in parts:
                    n_part += 1
                    if part is None or part.isEmpty(): continue
                    if any(road_geoms[r].intersects(part)
                           for r in road_idx.intersects(part.boundingBox())):
                        served_geoms[k] = part
                        sf = QgsFeature(k); sf.setGeometry(part); served_idx.insertFeature(sf); k += 1
            feedback.pushInfo(f'    읍면동 파트 {n_part:,}개 중 도로서비스 파트 {k:,}개 유지')
            if k == 0:
                raise QgsProcessingException('도로와 교차하는 읍면동 파트가 없습니다.')
        else:
            feedback.pushInfo('[1] 읍면동 미입력 — 전체 격자 사용')

        kept_gid = []; kept_cen = []
        for f in grid.getFeatures():
            if feedback.isCanceled(): return {}
            g = f.geometry()
            if g is None or g.isEmpty(): continue
            if served_idx is not None:
                if not any(served_geoms[i].intersects(g)
                           for i in served_idx.intersects(g.boundingBox())):
                    continue
            kept_gid.append(str(f[gidf])); kept_cen.append(g.centroid())
        if not kept_gid:
            raise QgsProcessingException('선별 후 남은 격자가 없습니다.')
        feedback.pushInfo(f'    출발 격자 {len(kept_gid):,}개')

        # ── [2] 출발/도착점을 네트워크 CRS로 ──────────────────────
        g2n = QgsCoordinateTransform(grid_crs, net_crs, QgsProject.instance()) \
              if grid_crs != net_crs else None
        o_pts = []
        for cen in kept_cen:
            p = cen.asPoint()
            if g2n:
                gg = QgsGeometry(cen); gg.transform(g2n); p = gg.asPoint()
            o_pts.append(QgsPointXY(p.x(), p.y()))
        c2n = QgsCoordinateTransform(center.sourceCrs(), net_crs, QgsProject.instance()) \
              if center.sourceCrs() != net_crs else None
        c_ids = []; d_pts = []
        for f in center.getFeatures():
            gm = f.geometry()
            if gm is None or gm.isEmpty(): continue
            gg = gm.centroid()
            if c2n: gg.transform(c2n)
            p = gg.asPoint()
            c_ids.append(str(f[cidf])); d_pts.append(QgsPointXY(p.x(), p.y()))

        # ── [3] 전체 도로망 사용 (클리핑 없음) ────────────────────
        net_input = net
        feedback.pushInfo(f'[3] 전체 도로망 {net.featureCount():,} 링크 사용 (클리핑 없음)')

        # ── [4] 그래프 구축 (QGIS 엔진, 수직 스냅) ────────────────
        feedback.pushInfo('[4] 그래프 구축 (QgsGraphBuilder)...')
        tg = time.time()
        fidx = net_input.fields().indexOf(owf) if owf else -1
        director = QgsVectorLayerDirector(net_input, fidx, "1", "", "0",
                                          QgsVectorLayerDirector.DirectionBoth)
        director.addStrategy(QgsNetworkDistanceStrategy())
        builder = QgsGraphBuilder(net_input.sourceCrs())
        tied = director.makeGraph(builder, o_pts + d_pts)
        graph = builder.graph()
        feedback.pushInfo(f'    정점 {graph.vertexCount():,} · 엣지 {graph.edgeCount():,} '
                          f'({time.time()-tg:.1f}s)')

        n_o = len(o_pts)
        entry = np.array([o_pts[i].distance(tied[i]) for i in range(n_o)])
        exitc = np.array([d_pts[j].distance(tied[n_o + j]) for j in range(len(d_pts))])

        # ── [5] CSR 추출 + 좌표→정점 인덱스 + scipy 다중출발 ──────
        feedback.pushInfo('[5] CSR + scipy 최단경로...')
        te = time.time()
        V = graph.vertexCount()
        rows = []; cols = []; wts = []; coord2vid = {}
        for i in range(V):
            p = graph.vertex(i).point()
            coord2vid[(round(p.x(), 3), round(p.y(), 3))] = i
            for eid in graph.vertex(i).outgoingEdges():
                e = graph.edge(eid)
                rows.append(i); cols.append(e.toVertex()); wts.append(e.cost(0))

        def vid_of(pt):
            return coord2vid.get((round(pt.x(), 3), round(pt.y(), 3)), -1)
        o_vid = np.array([vid_of(tied[i]) for i in range(n_o)])
        d_vid = np.array([vid_of(tied[n_o + j]) for j in range(len(d_pts))])
        if (o_vid < 0).any() or (d_vid < 0).any():
            feedback.pushWarning('  일부 O/D 점이 그래프 정점에 매핑되지 않았습니다.')

        ra = np.asarray(rows); ca = np.asarray(cols); wa = np.asarray(wts)
        rcsr = csr_matrix((wa, (ra, ca)), shape=(V, V)).T.tocsr()   # 전치 = 격자→중심지
        # 무방향(양방향) 폴백용 대칭 그래프 — 단방향 도달불가 셀만 이걸로 재시도(직선거리 폴백 없음)
        usym = csr_matrix((np.concatenate([wa, wa]),
                           (np.concatenate([ra, ca]), np.concatenate([ca, ra]))),
                          shape=(V, V)).tocsr()
        feedback.pushInfo(f'    CSR 추출 {time.time()-te:.1f}s')

        uc, uc_inv = np.unique(d_vid, return_inverse=True)
        ts = time.time()
        netmat = np.full((len(uc), n_o), np.inf)
        n_recover = 0
        for s in range(0, len(uc), self._CH):
            if feedback.isCanceled(): return {}
            idx = uc[s:s + self._CH]
            dd = dijkstra(rcsr, directed=True, indices=idx, min_only=False)
            block = dd[:, o_vid]
            miss = ~np.isfinite(block)
            if miss.any():   # 단방향 도달불가 → 무방향으로만 재시도 (직선 없음)
                du = dijkstra(usym, directed=False, indices=idx, min_only=False)
                ublock = du[:, o_vid]
                fill = miss & np.isfinite(ublock)
                n_recover += int(fill.sum())
                block = np.where(fill, ublock, block)
            netmat[s:s + len(idx), :] = block
            feedback.setProgress(int(s / max(len(uc), 1) * 60))
        if n_recover:
            feedback.pushInfo(f'    단방향 도달불가 {n_recover:,}쌍 → 양방향 폴백으로 회복')
        feedback.pushInfo(f'    Dijkstra {len(uc):,}출발 완료 ({time.time()-ts:.1f}s)')

        # ── [6] 전체 OD 행렬 CSV 출력 ──────────────────────────────
        feedback.pushInfo('[6] CSV 기록...')
        nrow = 0; nskip = 0; nc = len(c_ids)
        with open(out, 'w', encoding='utf-8-sig', newline='') as w:
            w.write('origin_id,destination_id,entry_cost,network_cost,exit_cost,total_cost\n')
            for i, gid in enumerate(kept_gid):
                if feedback.isCanceled(): break
                ec = float(entry[i]); buf = []
                for j in range(nc):
                    ncst = netmat[uc_inv[j], i]
                    if not np.isfinite(ncst):
                        nskip += 1; continue
                    xc = float(exitc[j]); tot = ec + ncst + xc
                    buf.append(f'{gid},{c_ids[j]},{ec:.1f},{ncst:.1f},{xc:.1f},{tot:.1f}\n')
                if buf: w.write(''.join(buf)); nrow += len(buf)
                if i % 1000 == 0: feedback.setProgress(60 + int(i / max(len(kept_gid),1) * 40))
        feedback.pushInfo(f'[완료] {nrow:,}행 (도달불가 {nskip:,} 제외) · 전체 {time.time()-t0:.1f}s → {out}')
        self._out = out
        return {self.OUTPUT: out}

    def postProcessAlgorithm(self, context, feedback):
        path = getattr(self, '_out', None)
        if path:
            lyr = QgsVectorLayer(path, '격자-중심지 OD(scipy graph)', 'ogr')
            if lyr.isValid(): QgsProject.instance().addMapLayer(lyr)
        return {}

    def name(self): return 'grid_center_od_scipy_graph'
    def displayName(self): return '격자→중심지 OD 행렬 (QGIS그래프+scipy)'
    def group(self): return 'OD 산출 (도로거리)'
    def groupId(self): return 'od_road'
    def shortHelpString(self):
        return ('격자→중심지 도로거리 OD 행렬. 최단경로는 scipy 다중출발(그래프·수직스냅·ONEWAY는 QGIS 엔진).\n\n'
                '[출발 격자 선별] 읍면동을 단일파트로 분해해 "도로와 교차하는 파트(육지)"에 겹치는 격자만 O로 사용'
                '(도로 없는 섬 파트 제외). 미입력 시 전체 격자.\n'
                '[출력] origin_id · destination_id · entry_cost · network_cost · exit_cost · total_cost(m) CSV')
    def createInstance(self): return GridCenterOdScipyGraph()