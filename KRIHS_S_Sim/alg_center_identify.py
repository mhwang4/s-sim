# -*- coding: utf-8 -*-
"""분석. 중심지 식별 — 거주·근무 배분결과 격자로 연도별 중심지 폴리곤·위계 산출.

충청남도 장래 중심지 분석(국토연구원, 2026) 2장 3절의 4단계 절차를 구현:
  1) 격자 분류: 집적지1(주거≥1500 AND 근무≥900) / 집적지2(주거≥300 AND 근무≥150)
     / 집적지3(주거≥300) / 주변
  2) 집적지1+2 묶어서, 집적지3 따로 100m 외부버퍼 → 유니온 → 100m 내부버퍼 병합
  3) 집적지3 폴리곤 중 집적지1/2 폴리곤과 1km 이내인 것 제거
  4) 폴리곤별 주거인구 밀도·수로 위계 구분 (UN DEGURBA):
     1차(밀도≥1500 AND 인구≥50000) / 2차(밀도≥300 AND 인구≥5000) / 3차(밀도≥300)
  (선택) 3)과 4) 사이에 중심지-생활권 분석 ① 후보 추출과 같은 후처리:
     · 읍면동별 중복 제거 — 교차 면적 최대 읍면동에 배정, 읍면동마다 주거인구 최대 1개 유지
     · 중심지 간 최소 거리 — 주거인구 큰 순으로 남기고, 남긴 폴리곤과 경계 간 직선거리가
       기준(기본 1,500m) 미만이면 제거
"""
import csv
import re
from collections import defaultdict

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
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterField,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsSpatialIndex,
)

BUF = 100.0        # 병합 버퍼 (m)
NEAR = 1000.0      # 집적지3 제거 거리 기준 (m)
YEAR_RE = re.compile(r'^pop_(\d{4})$')


class CenterIdentifyAlgorithm(QgsProcessingAlgorithm):

    RES_LAYER = 'RES_LAYER'
    RES_GID = 'RES_GID'
    WORK_LAYER = 'WORK_LAYER'
    WORK_GID = 'WORK_GID'
    BASE_YEAR = 'BASE_YEAR'
    C1_RES = 'C1_RES'
    C1_WORK = 'C1_WORK'
    C2_RES = 'C2_RES'
    C2_WORK = 'C2_WORK'
    C3_RES = 'C3_RES'
    T1_DENS = 'T1_DENS'
    T1_POP = 'T1_POP'
    T2_DENS = 'T2_DENS'
    T2_POP = 'T2_POP'
    T3_DENS = 'T3_DENS'
    # 근무 조건 (위계를 주거밀도·주거수 + 근무밀도·근무수 = 4조건으로 확장, 0=무시)
    T1_WDENS = 'T1_WDENS'
    T1_WPOP = 'T1_WPOP'
    T2_WDENS = 'T2_WDENS'
    T2_WPOP = 'T2_WPOP'
    T3_WDENS = 'T3_WDENS'
    T3_WPOP = 'T3_WPOP'
    # 후처리 (선택) — 중심지-생활권 분석 ① 후보 추출과 같은 규칙
    EMD_LAYER = 'EMD_LAYER'
    DEDUP_BY_EMD = 'DEDUP_BY_EMD'
    MIN_DIST_APPLY = 'MIN_DIST_APPLY'
    MIN_DIST = 'MIN_DIST'
    OUTPUT = 'OUTPUT'
    OUTPUT_CSV = 'OUTPUT_CSV'

    def name(self):
        return 'center_identify'

    def displayName(self):
        return '분석. 중심지 식별'

    def group(self):
        return '3. 장래인구 배분모형'

    def groupId(self):
        return 'pop_allocation'

    def createInstance(self):
        return CenterIdentifyAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.RES_LAYER, '거주인구 배분결과 격자 (pop_YYYY 필드)',
            types=[QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterField(
            self.RES_GID, '↳ 격자 ID 필드 (기본: gid)',
            defaultValue='gid', parentLayerParameterName=self.RES_LAYER))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.WORK_LAYER, '근무인구 배분결과 격자 (pop_YYYY 필드)',
            types=[QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterField(
            self.WORK_GID, '↳ 격자 ID 필드 (기본: gid)',
            defaultValue='gid', parentLayerParameterName=self.WORK_LAYER))
        self.addParameter(QgsProcessingParameterNumber(
            self.BASE_YEAR,
            '기준연도 (pop_r/pop_w 필드 사용, 0 = 기준연도 제외)',
            type=QgsProcessingParameterNumber.Integer,
            minValue=0, maxValue=2100, defaultValue=2023))
        for key, label, default in (
                (self.C1_RES, '집적지1: 주거인구 ≥', 1500),
                (self.C1_WORK, '집적지1: 근무인구 ≥', 900),
                (self.C2_RES, '집적지2: 주거인구 ≥', 300),
                (self.C2_WORK, '집적지2: 근무인구 ≥', 150),
                (self.C3_RES, '집적지3: 주거인구 ≥', 300),
                (self.T1_DENS, '1차 중심지: 주거밀도(명/㎢) ≥', 4500),
                (self.T1_POP, '1차 중심지: 주거인구 ≥', 50000),
                (self.T1_WDENS, '1차 중심지: 근무밀도(명/㎢) ≥ (0=무시)', 0),
                (self.T1_WPOP, '1차 중심지: 근무인구 ≥ (0=무시)', 50000),
                (self.T2_DENS, '2차 중심지: 주거밀도(명/㎢) ≥', 300),
                (self.T2_POP, '2차 중심지: 주거인구 ≥', 5000),
                (self.T2_WDENS, '2차 중심지: 근무밀도(명/㎢) ≥ (0=무시)', 0),
                (self.T2_WPOP, '2차 중심지: 근무인구 ≥ (0=무시)', 5000),
                (self.T3_DENS, '3차 중심지: 주거밀도(명/㎢) ≥', 300),
                (self.T3_WDENS, '3차 중심지: 근무밀도(명/㎢) ≥ (0=무시)', 0),
                (self.T3_WPOP, '3차 중심지: 근무인구 ≥ (0=무시)', 0)):
            self.addParameter(QgsProcessingParameterNumber(
                key, label, type=QgsProcessingParameterNumber.Double,
                minValue=0.0, defaultValue=default))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.EMD_LAYER, '읍면동 경계 (읍면동별 중복 제거용, 선택)',
            types=[QgsProcessing.TypeVectorPolygon], optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.DEDUP_BY_EMD,
            '읍면동별 중복 제거 (읍면동 내 주거인구 최대 중심지 1개만 유지 — 읍면동 경계 필요)',
            defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.MIN_DIST_APPLY,
            '중심지 간 최소 거리 적용 (기준 거리 안의 인접 중심지 중 주거인구 최대 1개만 유지)',
            defaultValue=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.MIN_DIST, '  ↳ 중심지 간 최소 거리 m — 폴리곤 경계 간 직선거리',
            type=QgsProcessingParameterNumber.Integer, minValue=0, defaultValue=1500))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, '중심지 폴리곤 (연도×위계)'))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT_CSV, '요약 통계 CSV (연도×위계별 수/면적/인구)',
            fileFilter='CSV (*.csv)'))

    # ── 내부 도우미 ──────────────────────────────────────

    @staticmethod
    def _year_fields(layer):
        """{연도: 필드명} — pop_YYYY 패턴 자동 감지."""
        out = {}
        for f in layer.fields():
            m = YEAR_RE.match(f.name())
            if m:
                out[int(m.group(1))] = f.name()
        return out

    @staticmethod
    def _num(val):
        try:
            return float(val)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _merge(geoms):
        """+100m 버퍼 유니온 → −100m 버퍼 → 개별 폴리곤 리스트."""
        if not geoms:
            return []
        union = QgsGeometry.unaryUnion([g.buffer(BUF, 8) for g in geoms])
        merged = union.buffer(-BUF, 8)
        if merged.isEmpty():
            return []
        if merged.isMultipart():
            return merged.asGeometryCollection()
        return [merged]

    @staticmethod
    def _assign(parts, members):
        """격자(centroid)를 폴리곤에 배정 → 폴리곤별 (n, pop_r, pop_w)."""
        boxes = [p.boundingBox() for p in parts]
        stats = [[0, 0.0, 0.0] for _ in parts]
        for geom, r, w in members:
            pt = geom.centroid().asPoint()
            for i, p in enumerate(parts):
                if boxes[i].contains(pt) and p.contains(pt):
                    stats[i][0] += 1
                    stats[i][1] += r
                    stats[i][2] += w
                    break
        return stats

    @staticmethod
    def _dedup_emd(cands, emd_index, emd_geoms, emd_tr):
        """읍면동별 중복 제거 — 교차 면적 최대 읍면동에 배정, 읍면동마다 주거인구 최대 1개.
        cands: [(geom, src, n, pop_r, pop_w)] → (남은 cands, 제거 수). 읍면동에 안 걸치면 그대로 둠."""
        groups = defaultdict(list)
        keep = []
        for i, c in enumerate(cands):
            q = QgsGeometry(c[0])
            if emd_tr is not None:
                q.transform(emd_tr)
            best, best_area = None, -1.0
            for fid in emd_index.intersects(q.boundingBox()):
                inter = q.intersection(emd_geoms[fid])
                if inter and not inter.isNull() and inter.area() > best_area:
                    best, best_area = fid, inter.area()
            if best is None:
                keep.append(i)
            else:
                groups[best].append(i)
        for idx in groups.values():
            keep.append(max(idx, key=lambda i: (cands[i][3], -i)))
        keep = sorted(keep)
        return [cands[i] for i in keep], len(cands) - len(keep)

    @staticmethod
    def _min_distance(cands, min_dist):
        """주거인구 큰 순으로 남기고, 남긴 폴리곤과 경계 간 직선거리가 min_dist 미만이면 제거."""
        order = sorted(range(len(cands)), key=lambda i: (-cands[i][3], i))
        kept = []
        for i in order:
            if all(cands[i][0].distance(cands[k][0]) >= min_dist for k in kept):
                kept.append(i)
        kept = sorted(kept)
        return [cands[i] for i in kept], len(cands) - len(kept)

    # ── 본 처리 ──────────────────────────────────────────

    def processAlgorithm(self, parameters, context, feedback):
        res = self.parameterAsVectorLayer(parameters, self.RES_LAYER, context)
        work = self.parameterAsVectorLayer(parameters, self.WORK_LAYER, context)
        res_gid = self.parameterAsString(parameters, self.RES_GID, context)
        work_gid = self.parameterAsString(parameters, self.WORK_GID, context)
        base_year = self.parameterAsInt(parameters, self.BASE_YEAR, context)
        th = {k: self.parameterAsDouble(parameters, k, context)
              for k in (self.C1_RES, self.C1_WORK, self.C2_RES, self.C2_WORK,
                        self.C3_RES, self.T1_DENS, self.T1_POP, self.T2_DENS,
                        self.T2_POP, self.T3_DENS,
                        self.T1_WDENS, self.T1_WPOP, self.T2_WDENS, self.T2_WPOP,
                        self.T3_WDENS, self.T3_WPOP)}
        csv_path = self.parameterAsFileOutput(parameters, self.OUTPUT_CSV, context)
        emd = self.parameterAsVectorLayer(parameters, self.EMD_LAYER, context)
        dedup = self.parameterAsBoolean(parameters, self.DEDUP_BY_EMD, context)
        min_dist = (self.parameterAsDouble(parameters, self.MIN_DIST, context)
                    if self.parameterAsBoolean(parameters, self.MIN_DIST_APPLY, context) else 0.0)

        if res.sourceCrs().isGeographic():
            raise QgsProcessingException(
                '거주 배분결과 CRS가 지리좌표계입니다 — 미터 단위 투영좌표계'
                '(EPSG:5179 등)가 필요합니다 (버퍼·거리·면적 연산).')

        # 읍면동 중복 제거 준비 (경계가 없으면 건너뜀)
        emd_index, emd_geoms, emd_tr = None, {}, None
        if dedup and emd is None:
            feedback.pushWarning('읍면동별 중복 제거를 선택했지만 읍면동 경계가 없어 건너뜁니다.')
        elif dedup:
            emd_index = QgsSpatialIndex()
            for ef in emd.getFeatures():
                emd_geoms[ef.id()] = QgsGeometry(ef.geometry())
                emd_index.addFeature(ef)
            if emd.crs() != res.sourceCrs():
                emd_tr = QgsCoordinateTransform(res.sourceCrs(), emd.crs(), QgsProject.instance())
        feedback.pushInfo(f'후처리: 읍면동 중복 제거 {"사용" if emd_index is not None else "미사용"} / '
                          f'중심지 간 최소 거리 {f"사용 ({min_dist:,.0f} m)" if min_dist > 0 else "미사용"}')

        # 연도 결정: 두 레이어 공통 pop_YYYY (+ 기준연도 pop_r/pop_w)
        res_years = self._year_fields(res)
        work_years = self._year_fields(work)
        years = sorted(set(res_years) & set(work_years))
        skipped = sorted((set(res_years) | set(work_years)) - set(years))
        if skipped:
            feedback.pushWarning(
                f'한쪽에만 있는 연도 제외: {", ".join(map(str, skipped))}')
        plan = [(y, res_years[y], work_years[y]) for y in years]
        if base_year:
            if res.fields().indexOf('pop_r') < 0 or work.fields().indexOf('pop_w') < 0:
                feedback.pushWarning(
                    '기준연도 필드(pop_r/pop_w) 부재 — 기준연도 제외')
            elif base_year in years:
                feedback.pushWarning(
                    f'기준연도 {base_year}가 pop_{base_year}와 중복 — 기준연도 제외')
            else:
                plan.insert(0, (base_year, 'pop_r', 'pop_w'))
        if not plan:
            raise QgsProcessingException(
                '분석할 연도가 없습니다 — 두 레이어에 공통 pop_YYYY 필드 필요.')
        feedback.pushInfo('━' * 50)
        feedback.pushInfo(f'분석 연도: {", ".join(str(y) for y, _, _ in plan)}')

        # 근무인구 조인 테이블 {gid: {필드명: 값}}
        w_fields = sorted({wf for _, _, wf in plan})
        w_map = {}
        for feat in work.getFeatures():
            gid = str(feat[work_gid]).strip() if feat[work_gid] is not None else ''
            if gid:
                w_map[gid] = {f: self._num(feat[f]) for f in w_fields}

        # 거주 격자 캐시 (지오메트리 + 연도별 값)
        r_fields = sorted({rf for _, rf, _ in plan})
        grids = []
        n_miss = 0
        for feat in res.getFeatures():
            if feedback.isCanceled():
                return {}
            gid = str(feat[res_gid]).strip() if feat[res_gid] is not None else ''
            wvals = w_map.get(gid)
            if wvals is None:
                n_miss += 1
                wvals = {}
            grids.append((QgsGeometry(feat.geometry()),
                          {f: self._num(feat[f]) for f in r_fields}, wvals))
        if n_miss:
            feedback.pushWarning(
                f'근무 배분결과에 없는 격자 {n_miss:,}개 — 근무인구 0 처리')
        feedback.pushInfo(f'격자 {len(grids):,}개 로드')

        out_fields = QgsFields()
        for name, typ, length in (
                ('year', QVariant.Int, 0), ('tier', QVariant.String, 10),
                ('src', QVariant.String, 10), ('n_grid', QVariant.Int, 0),
                ('pop_r', QVariant.Double, 0), ('pop_w', QVariant.Double, 0),
                ('area_km2', QVariant.Double, 0), ('dens_r', QVariant.Double, 0)):
            out_fields.append(QgsField(name, typ, len=length))
        sink, dest_id = self.parameterAsSink(
            parameters, self.OUTPUT, context, out_fields,
            res.wkbType(), res.sourceCrs())

        summary = {}   # (year, tier) → [n, area, pop_r, pop_w]
        for step, (year, rf, wf) in enumerate(plan):
            if feedback.isCanceled():
                return {}
            feedback.setProgress(int(step / len(plan) * 100))
            feedback.pushInfo('━' * 50)
            feedback.pushInfo(f'[{year}년] 중심지 식별')

            # 1단계: 격자 분류
            c12, c3 = [], []
            for geom, rvals, wvals in grids:
                r, w = rvals.get(rf, 0.0), wvals.get(wf, 0.0)
                if r >= th[self.C1_RES] and w >= th[self.C1_WORK]:
                    c12.append((geom, r, w))
                elif r >= th[self.C2_RES] and w >= th[self.C2_WORK]:
                    c12.append((geom, r, w))
                elif r >= th[self.C3_RES]:
                    c3.append((geom, r, w))
            feedback.pushInfo(f'  집적지1·2 격자 {len(c12):,}개 / '
                              f'집적지3 격자 {len(c3):,}개')

            # 2단계: 버퍼 병합
            parts12 = self._merge([g for g, _, _ in c12])
            parts3 = self._merge([g for g, _, _ in c3])

            # 3단계: 집적지1/2 폴리곤과 1km 이내 집적지3 폴리곤 제거
            kept3 = [p for p in parts3
                     if all(p.distance(q) > NEAR for q in parts12)]
            feedback.pushInfo(f'  병합 폴리곤: 집적지1·2 {len(parts12)}개 / '
                              f'집적지3 {len(kept3)}개 '
                              f'(1km 이내 제거 {len(parts3) - len(kept3)}개)')

            # 폴리곤별 요약통계 (격자 centroid 배정)
            cands = []
            for parts, members, src in ((parts12, c12, '집적지1·2'),
                                        (kept3, c3, '집적지3')):
                for p, (n, pr, pw) in zip(parts, self._assign(parts, members)):
                    cands.append((p, src, n, pr, pw))

            # 후처리 (선택): 읍면동별 중복 제거 → 중심지 간 최소 거리
            if emd_index is not None:
                cands, nrm = self._dedup_emd(cands, emd_index, emd_geoms, emd_tr)
                feedback.pushInfo(f'  읍면동 중복 제거: {nrm}개 제거 → {len(cands)}개')
            if min_dist > 0:
                cands, nrm = self._min_distance(cands, min_dist)
                feedback.pushInfo(f'  중심지 간 최소 거리 {min_dist:,.0f} m: {nrm}개 제거 → {len(cands)}개')

            # 4단계: 위계 구분
            n_out = {'1차': 0, '2차': 0, '3차': 0, '위계외': 0}
            for p, src, n, pr, pw in cands:
                area = p.area() / 1e6
                dens = pr / area if area > 0 else 0.0
                wdens = pw / area if area > 0 else 0.0
                # 4조건 위계: 주거밀도·주거수 + 근무밀도·근무수 (0=무시)
                if (dens >= th[self.T1_DENS] and pr >= th[self.T1_POP]
                        and wdens >= th[self.T1_WDENS] and pw >= th[self.T1_WPOP]):
                    tier = '1차'
                elif (dens >= th[self.T2_DENS] and pr >= th[self.T2_POP]
                        and wdens >= th[self.T2_WDENS] and pw >= th[self.T2_WPOP]):
                    tier = '2차'
                elif (dens >= th[self.T3_DENS]
                        and wdens >= th[self.T3_WDENS] and pw >= th[self.T3_WPOP]):
                    tier = '3차'
                else:
                    tier = '위계외'
                n_out[tier] += 1
                feat = QgsFeature(out_fields)
                feat.setGeometry(p)
                feat['year'], feat['tier'], feat['src'] = year, tier, src
                feat['n_grid'], feat['pop_r'], feat['pop_w'] = n, round(pr, 1), round(pw, 1)
                feat['area_km2'], feat['dens_r'] = round(area, 4), round(dens, 1)
                sink.addFeature(feat, QgsFeatureSink.FastInsert)
                s = summary.setdefault((year, tier), [0, 0.0, 0.0, 0.0])
                s[0] += 1
                s[1] += area
                s[2] += pr
                s[3] += pw
            feedback.pushInfo(
                '  위계: ' + ' / '.join(f'{k} {v}개' for k, v in n_out.items() if v))
            if n_out['위계외']:
                feedback.pushWarning(
                    f'  위계 기준 미달 폴리곤 {n_out["위계외"]}개 — tier=위계외로 출력')

        # 요약 CSV
        with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
            wtr = csv.writer(f)
            wtr.writerow(['year', 'tier', 'n_center', 'area_km2',
                          'pop_r', 'pop_w', 'dens_r'])
            for (year, tier), (n, area, pr, pw) in sorted(summary.items()):
                wtr.writerow([year, tier, n, round(area, 2), round(pr, 1),
                              round(pw, 1), round(pr / area, 1) if area else 0])
        feedback.pushInfo('━' * 50)
        feedback.pushInfo(f'완료: 요약 CSV → {csv_path}')
        return {self.OUTPUT: dest_id, self.OUTPUT_CSV: csv_path}

    def shortHelpString(self):
        return (
            '거주·근무인구 배분결과 격자를 이용해 연도별 중심지 폴리곤과 위계를\n'
            '식별합니다. 충청남도 장래 중심지 분석(2026) 2장 3절 4단계 절차.\n\n'
            '━ 입력\n'
            '  · 거주/근무 배분결과 격자 (gid + pop_YYYY) — 공통 연도 자동 감지\n'
            '  · 기준연도 지정 시 pop_r/pop_w 필드로 기준연도도 분석\n\n'
            '━ 처리 단계 (연도별)\n'
            '  1) 격자 분류 — 집적지1/2/3/주변 (주거·근무인구 임계값)\n'
            '  2) 집적지1+2 묶음·집적지3 별도 100m 버퍼 병합\n'
            '  3) 집적지1/2 폴리곤 1km 이내의 집적지3 폴리곤 제거\n'
            '  (선택) 읍면동별 중복 제거 — 읍면동마다 주거인구 최대 1개 (읍면동 경계 필요)\n'
            '  (선택) 중심지 간 최소 거리 — 주거인구 큰 순으로 남기고, 경계 간 거리 기준 미만 제거\n'
            '  4) 폴리곤 주거인구 밀도·수로 1·2·3차 위계 구분 (UN DEGURBA)\n\n'
            '━ 출력\n'
            '  · 중심지 폴리곤: year·tier·src·n_grid·pop_r·pop_w·area_km2·dens_r\n'
            '  · 요약 CSV: 연도×위계별 수/면적/인구/밀도'
        )
