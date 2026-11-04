# -*- coding: utf-8 -*-
"""1-1. 코호트 변화율 격자 배분 (거주) · 1-2. 코호트 연동 근무 격자 배분.

거주: 시군구 추계 총량은 그대로 두고, 읍면동 단위(그릇)마다 연령구조와 과거 변화율(SGIS 1km 성·연령,
2000~2020)로 감소 속도를 다르게 배분한다. 근무: 같은 단위의 15세 이상 거주인구 변화 배율로 근무 총량을 나눈다.
계산은 cohort.py.
"""
import csv
import os

from qgis.core import (
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterField,
    QgsProcessingParameterFile,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsSpatialIndex,
)

from . import core, cohort
from .layer_io import load_grid_groups, require_fields, validate_sgg_match, write_grid_sink


def _load_sgis(path, gids):
    """데이터팩 SGIS_성연령_1km.csv → {연도: {gid: {'tot','T','M','F'}}} (격자 레이어에 있는 gid만)."""
    out = {y: {} for y in cohort.SGIS_YEARS}
    with open(path, encoding='utf-8-sig') as f:
        rd = csv.reader(f)
        head = next(rd)
        need = ['year', 'gid', 'tot'] + ['%s%02d' % (k, i) for k in 'TMF' for i in range(cohort.NA)]
        if head[:len(need)] != need:
            raise QgsProcessingException(
                'SGIS 성·연령 표의 열이 맞지 않습니다. year, gid, tot, T00…T17, M00…M17, F00…F17 이어야 합니다.')
        for row in rd:
            y = int(row[0])
            if y not in out or row[1] not in gids:
                continue
            v = [float(x) for x in row[2:]]
            n = cohort.NA
            out[y][row[1]] = {'tot': v[0], 'T': v[1:1 + n], 'M': v[1 + n:1 + 2 * n], 'F': v[1 + 2 * n:1 + 3 * n]}
    miss = [y for y, d in out.items() if not d]
    if miss:
        raise QgsProcessingException(f'SGIS 성·연령 표에 이 격자의 {", ".join(map(str, miss))}년 자료가 없습니다.')
    return out


def _overlap(grid, gid_field, sgg_of, emd, code_field, name_field, feedback):
    """격자마다 자기 시군구 읍면동과 겹친 면적 [(이름, 면적)]. 겹치는 읍면동이 없으면 가장 가까운 것 하나."""
    xf = None
    if emd.crs() != grid.crs():
        xf = QgsCoordinateTransform(emd.crs(), grid.crs(), QgsProject.instance())
    by = {}
    for f in emd.getFeatures():
        geo = QgsGeometry(f.geometry())
        if xf is not None:
            geo.transform(xf)
        by.setdefault(str(f[code_field]).strip(), []).append((str(f[name_field]).strip(), geo))
    sggs = sorted({str(s) for s in sgg_of.values()})
    per_sgg = {}
    for s in sggs:
        items = [it for c, L in by.items() if c.startswith(s) for it in L]
        if not items:
            raise QgsProcessingException(f'읍면동 경계에 시군구 {s}에 속하는 읍면동(코드 앞자리 {s})이 없습니다.')
        idx = QgsSpatialIndex()
        for i, (_, geo) in enumerate(items):
            idx.addFeature(i, geo.boundingBox())
        per_sgg[s] = (items, idx)
    overlap, far = {}, 0
    feats = list(grid.getFeatures())
    for k, f in enumerate(feats):
        if feedback.isCanceled():
            return None, None
        if k % 500 == 0:
            feedback.setProgress(5 + int(k / max(1, len(feats)) * 25))
        g = str(f[gid_field]).strip()
        items, idx = per_sgg[str(sgg_of[g])]
        geo = f.geometry()
        L = []
        for i in idx.intersects(geo.boundingBox()):
            a = items[i][1].intersection(geo).area()
            if a > 0:
                L.append((items[i][0], a))
        if not L:
            far += 1
            c = geo.centroid()
            L = [(min(items, key=lambda it: it[1].distance(c))[0], 1.0)]
        overlap[g] = L
    if far:
        feedback.pushWarning(f'  자기 시군구 읍면동과 겹치지 않는 격자 {far}개 → 가장 가까운 읍면동에 연결')
    names = {int(s): {n for n, _ in items} for s, (items, _) in per_sgg.items()}
    return overlap, names


class CohortAllocateAlgorithm(QgsProcessingAlgorithm):
    INPUT, GID_FIELD, SGG_FIELD, POP_FIELD = 'INPUT', 'GID_FIELD', 'SGG_FIELD', 'POP_FIELD'
    EMD, EMD_CODE, EMD_NAME = 'EMD', 'EMD_CODE', 'EMD_NAME'
    SGIS, TABLE = 'SGIS', 'TABLE'
    HALF_LIFE, POOL_SEX_POP, BORROW_POP, BASE_YEAR = 'HALF_LIFE', 'POOL_SEX_POP', 'BORROW_POP', 'BASE_YEAR'
    OUTPUT, OUT_UNITS, OUT_LINKS = 'OUTPUT', 'OUT_UNITS', 'OUT_LINKS'

    def name(self):
        return 'cohort_grid_allocate'

    def displayName(self):
        return '1-1. 코호트 변화율 격자 배분 (거주)'

    def group(self):
        return '3. 장래인구 배분모형'

    def groupId(self):
        return 'pop_allocation'

    def createInstance(self):
        return CohortAllocateAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT, '국토공간거점지도 1km 격자', types=[QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterField(
            self.GID_FIELD, '↳ 격자 ID 필드 (기본: gid)', defaultValue='gid', parentLayerParameterName=self.INPUT))
        self.addParameter(QgsProcessingParameterField(
            self.SGG_FIELD, '↳ 시군구 코드 필드 (기본: sgg_cd_h)', defaultValue='sgg_cd_h',
            parentLayerParameterName=self.INPUT))
        self.addParameter(QgsProcessingParameterField(
            self.POP_FIELD, '↳ 기준연도 격자인구 필드 (기본: pop_r)', defaultValue='pop_r',
            parentLayerParameterName=self.INPUT, type=QgsProcessingParameterField.Numeric))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.EMD, '읍면동 경계', types=[QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterField(
            self.EMD_CODE, '↳ 읍면동 코드 필드 (앞자리 = 시군구 코드, 기본: ADM_CD)', defaultValue='ADM_CD',
            parentLayerParameterName=self.EMD))
        self.addParameter(QgsProcessingParameterField(
            self.EMD_NAME, '↳ 읍면동 이름 필드 (끝 글자 읍·면·동, 기본: ADM_NM)', defaultValue='ADM_NM',
            parentLayerParameterName=self.EMD))
        self.addParameter(QgsProcessingParameterFile(
            self.SGIS, 'SGIS 1km 격자 성·연령 인구 표 (2000~2020)', fileFilter='CSV (*.csv)'))
        self.addParameter(QgsProcessingParameterFile(
            self.TABLE, '시군구 인구 추계 테이블 (sgg_cd_h + pop_YYYY)', fileFilter='추계 테이블 (*.csv *.xlsx)'))
        self.addParameter(QgsProcessingParameterNumber(
            self.HALF_LIFE, '반감기(년) — 단위 고유 변화율이 시군구 평균 쪽으로 절반 줄어드는 기간',
            type=QgsProcessingParameterNumber.Double, defaultValue=5.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.POOL_SEX_POP, '남녀 합쳐 계산하는 단위 인구 기준(명 미만)',
            type=QgsProcessingParameterNumber.Integer, defaultValue=5000, minValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.BORROW_POP, '이웃 단위와 묶어 변화율을 내는 단위 인구 기준(명 미만)',
            type=QgsProcessingParameterNumber.Integer, defaultValue=1000, minValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.BASE_YEAR, '기준연도 (격자인구 연도)', type=QgsProcessingParameterNumber.Integer,
            defaultValue=2023, minValue=2020, maxValue=2024))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, '코호트 배분 결과'))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUT_UNITS, '단위별 투영 CSV (근무 배분 입력)', fileFilter='CSV (*.csv)'))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUT_LINKS, '격자-단위 연결 CSV (근무 배분 입력)', fileFilter='CSV (*.csv)'))

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        gid_f = self.parameterAsString(parameters, self.GID_FIELD, context)
        sgg_f = self.parameterAsString(parameters, self.SGG_FIELD, context)
        pop_f = self.parameterAsString(parameters, self.POP_FIELD, context)
        emd = self.parameterAsVectorLayer(parameters, self.EMD, context)
        code_f = self.parameterAsString(parameters, self.EMD_CODE, context)
        name_f = self.parameterAsString(parameters, self.EMD_NAME, context)
        hl = self.parameterAsDouble(parameters, self.HALF_LIFE, context)
        pool = self.parameterAsInt(parameters, self.POOL_SEX_POP, context)
        borrow = self.parameterAsInt(parameters, self.BORROW_POP, context)
        base_year = self.parameterAsInt(parameters, self.BASE_YEAR, context)
        require_fields(emd, (code_f, name_f), '읍면동 경계')

        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[1단계] 추계 테이블·격자 로드')
        try:
            table, years, n_null = core.load_sgg_table(self.parameterAsString(parameters, self.TABLE, context))
        except ValueError as e:
            raise QgsProcessingException(str(e))
        years = [y for y in years if y > base_year]
        if not years:
            raise QgsProcessingException(f'추계 테이블에 기준연도({base_year}) 이후 연도가 없습니다.')
        groups = load_grid_groups(layer, pop_f, feedback, gid_f, sgg_f)
        validate_sgg_match(groups.keys(), table.keys(), '추계 테이블', feedback)
        sgg_of = {g['gid']: s for s, L in groups.items() for g in L}
        pop_r = {g['gid']: g['pop0'] for L in groups.values() for g in L}
        feedback.pushInfo(f'  → 격자 {len(pop_r):,}개, 시군구 {len(groups)}개, 연도 {", ".join(map(str, years))}')

        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[2단계] 단위 구성 (읍·면 각각 + 동지역 하나, 동만 있는 시군구는 동 각각)')
        overlap, names = _overlap(layer, gid_f, sgg_of, emd, code_f, name_f, feedback)
        if overlap is None:
            return {}
        links, only = cohort.build_links(overlap, sgg_of, names)
        if only:
            feedback.pushInfo('  동만 있는 시군구(동 각각, 격자는 겹친 면적 비율로 연결): '
                              + ', '.join(str(s) for s in sorted(only)))
        feedback.setProgress(30)

        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[3단계] SGIS 성·연령 집계 → 변화율 → 투영 → 기준연도 맞추기 → 시군구 총량')
        sgis = _load_sgis(self.parameterAsString(parameters, self.SGIS, context), set(pop_r))
        res = cohort.residential(links, sgg_of, pop_r, sgis, {s: table[s] for s in groups}, years, base_year,
                                 hl, pool, borrow)
        info = res['info']
        feedback.pushInfo(f'  → 단위 {len(res["units"]):,}개 (SGIS 인구 없는 단위 '
                          f'{sum(1 for u in info.values() if u["no_sgis"])}개는 시군구 평균 배율)')
        sc = [v for d in res['scale'].values() for v in d.values()]
        feedback.pushInfo(f'  → 시군구 총량 배율 {min(sc):.3f} ~ {max(sc):.3f}')
        for s in sorted(groups):
            for t in years:
                tot = sum(res['grid'][g][t] for g in pop_r if sgg_of[g] == s)
                if abs(tot - table[s][t]) > 0.5:
                    feedback.pushWarning(f'  시군구 {s} {t}년 합계 {tot:,.1f} ≠ 추계 {table[s][t]:,.1f}')
        feedback.setProgress(80)

        out_u = self.parameterAsFileOutput(parameters, self.OUT_UNITS, context)
        out_l = self.parameterAsFileOutput(parameters, self.OUT_LINKS, context)
        if out_u:
            with open(out_u, 'w', encoding='utf-8-sig', newline='') as fo:
                w = csv.writer(fo)
                w.writerow(['sgg_cd_h', 'unit', 'pop_base', 'sex_separate', 'rate_units', 'no_sgis']
                           + [f'pop_{t}' for t in years] + [f'r15_{t}' for t in years])
                for u in res['units']:
                    x = info[u]
                    w.writerow([u[0], u[1], round(x['base'], 4), int(x['sex']), '+'.join(v[1] for v in x['rate_units']),
                                int(x['no_sgis'])] + [round(x['final'][t], 4) for t in years]
                               + [(round(x['r15'][t], 10) if x['r15'] else '') for t in years])
        if out_l:
            with open(out_l, 'w', encoding='utf-8-sig', newline='') as fo:
                w = csv.writer(fo)
                w.writerow(['gid', 'sgg_cd_h', 'unit', 'weight'])
                for g in sorted(links):
                    for u, a in sorted(links[g].items()):
                        w.writerow([g, u[0], u[1], round(a, 8)])

        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[4단계] 결과 레이어 출력')
        dest, n = write_grid_sink(self, parameters, self.OUTPUT, context, layer, years, res['grid'], feedback,
                                  gid_field=gid_f)
        feedback.pushInfo(f'완료: 격자 {n:,}개 출력')
        return {self.OUTPUT: dest, self.OUT_UNITS: out_u, self.OUT_LINKS: out_l}

    def shortHelpString(self):
        return (
            '시군구 추계 총량은 그대로 두고, 읍면동 단위마다 연령구조와 과거 변화율에 따라 감소 속도를 다르게 '
            '1km 격자에 배분합니다 (BAU 옆 코호트 시나리오).\n\n'
            '━ 단위\n'
            '  · 읍·면이 있는 시군구: 읍·면 각각 + 동 전체를 묶은 동지역 하나 (격자는 겹친 면적이 가장 큰 읍면동의 단위)\n'
            '  · 동만 있는 시군구: 동 각각 (격자는 겹친 면적 비율로 여러 동에 나눠 연결)\n\n'
            '━ 계산\n'
            '  1) 단위별 SGIS 5세×성 인구(2000~2020) → 코호트 변화율·아동여성비\n'
            '  2) 2020 → 5년 단계 투영, 변화율은 반감기만큼 시군구 평균 쪽으로 수렴\n'
            '  3) 변화 배율만 쓰고 수준은 기준연도 격자인구, 시군구 합계 = 추계 테이블\n'
            '  4) 단위 안은 기준연도 격자인구 비중\n\n'
            '━ 출력\n'
            '  · 입력 격자 필드 + pop_YYYY, 단위별 투영 CSV·격자-단위 연결 CSV(근무 배분 입력)'
        )


class CohortWorkAllocateAlgorithm(QgsProcessingAlgorithm):
    INPUT, GID_FIELD, SGG_FIELD, POP_FIELD = 'INPUT', 'GID_FIELD', 'SGG_FIELD', 'POP_FIELD'
    TABLE, UNITS, LINKS, OUTPUT = 'TABLE', 'UNITS', 'LINKS', 'OUTPUT'

    def name(self):
        return 'cohort_work_allocate'

    def displayName(self):
        return '1-2. 코호트 연동 근무 격자 배분'

    def group(self):
        return '3. 장래인구 배분모형'

    def groupId(self):
        return 'pop_allocation'

    def createInstance(self):
        return CohortWorkAllocateAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT, '국토공간거점지도 1km 격자', types=[QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterField(
            self.GID_FIELD, '↳ 격자 ID 필드 (기본: gid)', defaultValue='gid', parentLayerParameterName=self.INPUT))
        self.addParameter(QgsProcessingParameterField(
            self.SGG_FIELD, '↳ 시군구 코드 필드 (기본: sgg_cd_h)', defaultValue='sgg_cd_h',
            parentLayerParameterName=self.INPUT))
        self.addParameter(QgsProcessingParameterField(
            self.POP_FIELD, '↳ 기준연도 근무인구 필드 (기본: pop_w)', defaultValue='pop_w',
            parentLayerParameterName=self.INPUT, type=QgsProcessingParameterField.Numeric))
        self.addParameter(QgsProcessingParameterFile(
            self.TABLE, '시군구 근무인구 추계 테이블 (sgg_cd_h + pop_YYYY)', fileFilter='추계 테이블 (*.csv *.xlsx)'))
        self.addParameter(QgsProcessingParameterFile(
            self.UNITS, '단위별 투영 CSV (코호트 거주 배분 결과)', fileFilter='CSV (*.csv)'))
        self.addParameter(QgsProcessingParameterFile(
            self.LINKS, '격자-단위 연결 CSV (코호트 거주 배분 결과)', fileFilter='CSV (*.csv)'))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, '코호트 연동 근무 배분 결과'))

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        gid_f = self.parameterAsString(parameters, self.GID_FIELD, context)
        sgg_f = self.parameterAsString(parameters, self.SGG_FIELD, context)
        pop_f = self.parameterAsString(parameters, self.POP_FIELD, context)
        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[1단계] 근무 추계·격자·거주 코호트 결과 로드')
        try:
            table, years, _ = core.load_sgg_table(self.parameterAsString(parameters, self.TABLE, context))
        except ValueError as e:
            raise QgsProcessingException(str(e))
        groups = load_grid_groups(layer, pop_f, feedback, gid_f, sgg_f)
        validate_sgg_match(groups.keys(), table.keys(), '근무 추계 테이블', feedback)
        sgg_of = {g['gid']: s for s, L in groups.items() for g in L}
        pop_w = {g['gid']: g['pop0'] for L in groups.values() for g in L}
        r15 = {}
        with open(self.parameterAsString(parameters, self.UNITS, context), encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))
        ys = [int(c[4:]) for c in (rows[0].keys() if rows else []) if c.startswith('r15_')]
        years = [y for y in years if y in ys]
        if not years:
            raise QgsProcessingException('근무 추계 테이블과 단위별 투영 CSV에 공통 연도가 없습니다.')
        for r in rows:
            u = (int(r['sgg_cd_h']), r['unit'])
            r15[u] = {y: float(r[f'r15_{y}']) for y in years} if r.get(f'r15_{years[0]}') else None
        links = {}
        with open(self.parameterAsString(parameters, self.LINKS, context), encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                links.setdefault(r['gid'], {})[(int(r['sgg_cd_h']), r['unit'])] = float(r['weight'])
        miss = set(pop_w) - set(links)
        if miss:
            raise QgsProcessingException(
                f'격자-단위 연결 CSV에 없는 격자가 {len(miss)}개 있습니다. 거주 코호트 배분을 같은 격자로 다시 실행하세요.')
        links = {g: links[g] for g in pop_w}
        feedback.pushInfo(f'  → 격자 {len(pop_w):,}개, 단위 {len(r15):,}개, 연도 {", ".join(map(str, years))}')
        feedback.setProgress(30)

        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[2단계] 단위 근무 = 기준연도 근무 × 15세 이상 변화 배율 → 시군구 근무 총량')
        res = cohort.work(links, sgg_of, pop_w, r15, {s: table[s] for s in groups}, years)
        sc = [v for d in res['scale'].values() for v in d.values()]
        feedback.pushInfo(f'  → 시군구 총량 배율 {min(sc):.3f} ~ {max(sc):.3f}')
        feedback.setProgress(70)
        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[3단계] 결과 레이어 출력')
        dest, n = write_grid_sink(self, parameters, self.OUTPUT, context, layer, years, res['grid'], feedback,
                                  gid_field=gid_f)
        feedback.pushInfo(f'완료: 격자 {n:,}개 출력')
        return {self.OUTPUT: dest}

    def shortHelpString(self):
        return (
            '코호트 거주 배분과 같은 단위에서, 단위의 15세 이상 거주인구 변화 배율로 시군구 근무 총량을 나눕니다.\n\n'
            '  단위 근무(t) = 기준연도 단위 근무 × 15세 이상 배율(t) → 시군구 근무 총량에 맞춤\n'
            '  격자 근무(t) = 기준연도 격자 근무 × 연결된 단위 배율(동만 있는 시군구는 면적 비율로 섞음)\n\n'
            '━ 입력: 격자(pop_w), 근무 추계 테이블(장래 근무인구 추계 결과), 코호트 거주 배분의 단위별 투영·격자-단위 연결 CSV\n'
            '━ 출력: 입력 격자 필드 + pop_YYYY'
        )
