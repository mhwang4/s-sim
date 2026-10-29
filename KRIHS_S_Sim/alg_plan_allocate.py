# -*- coding: utf-8 -*-
"""3. 계획인구 격자 배분 — 개발사업 계획인구·입주율 기반 개발/비개발 분리 배분.

  1단계  사업별 계획인구를 KOTI 입주율 규칙으로 준공연도부터 연도 분산,
         유입연도 y는 y보다 큰 최소 목표연도에 반영 → 시군·목표연도별 개발지구 인구
  2단계  비개발지구 인구(= 시군 추계 총량 − 개발지구 인구)를
         직전 시점 격자인구 비중에 단순 비례 배분 (최초 시점은 기준연도 격자인구)
  3단계  개발지구 인구는 사업지구와 중첩되는 격자에 균등 배분
  4단계  2+3 합산 결과를 다음 목표연도의 격자인구 비중으로 연쇄
         (이전 연도 개발지구는 거주지화되어 다음 연도 비개발 배분에 포함)
"""
import csv

from qgis.core import (
    QgsCoordinateTransform,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterField,
    QgsProcessingParameterFile,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsSpatialIndex,
)

from . import core
from .layer_io import load_grid_groups, require_fields, validate_sgg_match, write_grid_sink


class PlanAllocateAlgorithm(QgsProcessingAlgorithm):

    INPUT = 'INPUT'
    GID_FIELD = 'GID_FIELD'
    SGG_FIELD = 'SGG_FIELD'
    POP_FIELD = 'POP_FIELD'
    TABLE = 'TABLE'
    ZONE_LAYER = 'ZONE_LAYER'
    Z_POP_FIELD = 'Z_POP_FIELD'
    Z_YR_FIELD = 'Z_YR_FIELD'
    Z_PRJ_FIELD = 'Z_PRJ_FIELD'
    Z_SGG_FIELD = 'Z_SGG_FIELD'
    RATIO_FIELD = 'RATIO_FIELD'
    OUTPUT = 'OUTPUT'
    OUT_SPLIT_CSV = 'OUT_SPLIT_CSV'

    def name(self):
        return 'plan_grid_allocate'

    def displayName(self):
        return '3. 계획인구 반영 격자 배분'

    def group(self):
        return '3. 장래인구 배분모형'

    def groupId(self):
        return 'pop_allocation'

    def createInstance(self):
        return PlanAllocateAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT,
            '국토공간거점지도 1km 격자',
            types=[QgsProcessing.TypeVectorPolygon],
        ))
        self.addParameter(QgsProcessingParameterField(
            self.GID_FIELD,
            '↳ 격자 ID 필드 (기본: gid)',
            defaultValue='gid',
            parentLayerParameterName=self.INPUT,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.SGG_FIELD,
            '↳ 시군구 코드 필드 (기본: sgg_cd_h) — 추계 테이블 sgg_cd_h와 같은 코드 체계여야 함',
            defaultValue='sgg_cd_h',
            parentLayerParameterName=self.INPUT,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.POP_FIELD,
            '↳ 기준연도 격자인구 필드 (기본: pop_r)',
            defaultValue='pop_r',
            parentLayerParameterName=self.INPUT,
            type=QgsProcessingParameterField.Numeric,
        ))
        self.addParameter(QgsProcessingParameterFile(
            self.TABLE,
            '시군구 장래인구 추계 테이블 (sgg_cd_h + pop_YYYY)',
            fileFilter='추계 테이블 (*.csv *.xlsx)',
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.ZONE_LAYER,
            '개발사업 지구 경계',
            types=[QgsProcessing.TypeVectorPolygon],
        ))
        self.addParameter(QgsProcessingParameterField(
            self.Z_POP_FIELD,
            '↳ 계획인구 필드 (기본: plan_pop)',
            defaultValue='plan_pop',
            parentLayerParameterName=self.ZONE_LAYER,
            type=QgsProcessingParameterField.Numeric,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.Z_YR_FIELD,
            '↳ 준공연도 필드 (기본: yr_done)',
            defaultValue='yr_done',
            parentLayerParameterName=self.ZONE_LAYER,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.Z_PRJ_FIELD,
            '↳ 사업 전체 계획인구 합 필드 (선택 — 복수 시군 분할 사업의 입주율 구간 판정용)',
            defaultValue='prj_pop',
            parentLayerParameterName=self.ZONE_LAYER,
            type=QgsProcessingParameterField.Numeric,
            optional=True,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.Z_SGG_FIELD,
            '↳ 관할 시군구 코드 필드 (선택 — 있으면 해당 시군 격자로만 배분)',
            defaultValue='sgg_cd_h',
            parentLayerParameterName=self.ZONE_LAYER,
            optional=True,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.RATIO_FIELD,
            '원단위 분모 필드 (선택 — 근무인구 배분용: 개발 유입 = 계획인구 × 시군별 '
            '기준인구합/이 필드합. 예: 기준인구 pop_w, 분모 pop_r → 주거 1인당 근무인구)',
            parentLayerParameterName=self.INPUT,
            type=QgsProcessingParameterField.Numeric,
            optional=True,
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT,
            '계획인구 배분 결과',
        ))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUT_SPLIT_CSV,
            '시군구별 개발/비개발 인구 분배 CSV',
            fileFilter='CSV (*.csv)',
        ))

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        gid_field = self.parameterAsString(parameters, self.GID_FIELD, context)
        sgg_field = self.parameterAsString(parameters, self.SGG_FIELD, context)
        pop_field = self.parameterAsString(parameters, self.POP_FIELD, context)
        table_path = self.parameterAsString(parameters, self.TABLE, context)
        zone_layer = self.parameterAsVectorLayer(parameters, self.ZONE_LAYER, context)
        zone_fields = {
            'pop': self.parameterAsString(parameters, self.Z_POP_FIELD, context),
            'yr': self.parameterAsString(parameters, self.Z_YR_FIELD, context),
            'prj': self.parameterAsString(parameters, self.Z_PRJ_FIELD, context),
            'sgg': self.parameterAsString(parameters, self.Z_SGG_FIELD, context),
        }
        ratio_field = self.parameterAsString(parameters, self.RATIO_FIELD, context)
        split_csv = self.parameterAsFileOutput(parameters, self.OUT_SPLIT_CSV, context)

        # ── [1단계] 추계 테이블 로드 (0~10) ────────────────
        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[1단계] 시군구 추계 테이블 로드')
        try:
            table, years, n_null = core.load_sgg_table(table_path)
        except ValueError as e:
            raise QgsProcessingException(str(e))
        feedback.pushInfo(f'  → {len(table):,}개 시군구, 목표연도: {", ".join(map(str, years))}')
        if n_null:
            feedback.pushWarning(f'  인구 NULL {n_null}건 → 0 처리')
        feedback.setProgress(10)

        # ── [2단계] 격자 로드·검증 (10~25) ─────────────────
        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[2단계] 격자 로드 및 검증')
        groups = load_grid_groups(layer, pop_field, feedback, gid_field, sgg_field)
        n_grid = sum(len(v) for v in groups.values())
        feedback.pushInfo(f'  → {n_grid:,}개 격자, {len(groups)}개 시군구')
        validate_sgg_match(groups.keys(), table.keys(), '추계 테이블', feedback)
        feedback.setProgress(25)

        # ── [3단계] 개발지구 인구 산출 (25~45) ─────────────
        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[3단계] 개발사업 입주율 분산 및 개발격자 균등 배분')
        unit = None
        if ratio_field:
            unit = self._unit_ratio(layer, sgg_field, pop_field, ratio_field, feedback)
        dev_total, dev_alloc = self._process_zones(
            layer, zone_layer, years, feedback,
            gid_field, sgg_field, zone_fields, unit)
        for yr in years:
            tot = sum(v.get(yr, 0.0) for v in dev_total.values())
            feedback.pushInfo(f'  {yr}년: 개발지구 인구 {tot:,.0f}명')
        feedback.setProgress(45)

        # ── [4단계] 개발/비개발 분배 (45~50) ───────────────
        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[4단계] 시군구별 개발/비개발 인구 분배')
        split = {}   # {sgg: {yr: (dev, nondev)}}
        for sgg in sorted(groups):
            per = {}
            for yr in years:
                dev = dev_total.get(sgg, {}).get(yr, 0.0)
                nondev = table[sgg][yr] - dev
                if nondev < 0:
                    feedback.pushWarning(
                        f'  시군구 {sgg} {yr}년: 개발지구 인구({dev:,.0f})가 '
                        f'추계 총량({table[sgg][yr]:,.0f})을 초과 → 비개발 0 처리')
                    nondev = 0.0
                per[yr] = (dev, nondev)
            split[sgg] = per
        feedback.setProgress(50)

        # ── [5단계] 격자 배분 — 비례 배분 + 연쇄 (50~80) ───
        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[5단계] 격자 배분 (비개발 비례배분 + 개발 균등, 연도 연쇄)')
        allocated = {}
        n_sgg = len(groups)
        for si, (sgg, grids) in enumerate(sorted(groups.items())):
            if feedback.isCanceled():
                return {}
            feedback.setProgress(50 + int(si / n_sgg * 30))

            weights = {g['gid']: g['pop0'] for g in grids}
            for yr in years:
                dev, nondev = split[sgg][yr]
                result, equal = core.allocate_by_weight(weights, nondev)
                if equal:
                    feedback.pushWarning(
                        f'  시군구 {sgg} {yr}년: 격자인구 합 0 → 균등 배분')
                for gid in result:
                    result[gid] += dev_alloc.get(gid, {}).get(yr, 0.0)
                for gid, pop in result.items():
                    allocated.setdefault(gid, {})[yr] = pop
                # 다음 목표연도 가중치 = 이번 배분 결과 (개발지구 거주지화 포함)
                if sum(result.values()) > 0:
                    weights = dict(result)

        # ── [6단계] 출력 (80~99) ───────────────────────────
        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[6단계] 결과 출력')
        self._write_split_csv(split_csv, split, years)
        feedback.pushInfo(f'  → 분배 CSV: {split_csv}')

        dest_id, n_written = write_grid_sink(
            self, parameters, self.OUTPUT, context,
            layer, years, allocated, feedback, gid_field=gid_field)

        feedback.pushInfo('━' * 50)
        feedback.pushInfo(f'완료: 격자 {n_written:,}개 출력')
        return {
            self.OUTPUT: dest_id,
            self.OUT_SPLIT_CSV: split_csv,
        }

    # ── 원단위 (근무인구 배분용) ──────────────────────────
    @staticmethod
    def _unit_ratio(layer, sgg_field, pop_field, ratio_field, feedback):
        """시군별 원단위 = Σ기준인구(pop_field) / Σ분모(ratio_field).

        근무인구 배분 시 개발지구 유입 = 계획인구(거주) × 원단위(주거 1인당 근무인구).
        분모 합 0이면 1.0 처리 + 경고.
        """
        num, den = {}, {}
        for feat in layer.getFeatures():
            try:
                sgg = int(str(feat[sgg_field]).strip())
            except (TypeError, ValueError):
                continue
            try:
                num[sgg] = num.get(sgg, 0.0) + float(feat[pop_field])
            except (TypeError, ValueError):
                pass
            try:
                den[sgg] = den.get(sgg, 0.0) + float(feat[ratio_field])
            except (TypeError, ValueError):
                pass
        unit = {}
        for sgg in num:
            d = den.get(sgg, 0.0)
            if d > 0:
                unit[sgg] = num[sgg] / d
            else:
                unit[sgg] = 1.0
                feedback.pushWarning(f'  시군구 {sgg}: 원단위 분모 합 0 → 1.0 처리')
        feedback.pushInfo(
            '  원단위(기준인구/분모) 적용: '
            + ', '.join(f'{s}={unit[s]:.2f}' for s in sorted(unit)))
        return unit

    # ── 개발사업 처리 ─────────────────────────────────────
    @staticmethod
    def _process_zones(grid_layer, zone_layer, years, feedback,
                       gid_field, sgg_field, zone_fields, unit=None):
        """사업별 계획인구 → 입주율 분산 → 목표연도 매핑 → 격자 균등 배분.

        - zone_fields: {'pop': 계획인구, 'yr': 준공연도, 'prj': 사업전체합(선택),
          'sgg': 관할시군(선택)} 필드명. 선택 필드는 빈 문자열이면 미사용.
        - 입주율 규모 구간은 prj 필드가 있으면 그 값으로 판정
          (복수 시군 분할 사업 대응), 없으면 계획인구 필드.
        - 개발격자 판정: 사업 경계가 걸린(교차) 모든 격자 인정.
        - 관할 시군이 지정된 사업은 해당 시군 격자로만 배분,
          미지정 사업은 교차하는 시군에 1/n 균등 분할.

        Returns:
            dev_total: {sgg: {yr: 개발인구}}
            dev_alloc: {gid: {yr: 개발인구}}
        """
        f_pop, f_yr = zone_fields['pop'], zone_fields['yr']
        require_fields(zone_layer, (f_pop, f_yr), '개발사업')
        zfields = zone_layer.fields()
        f_sgg = zone_fields['sgg'] if (
            zone_fields['sgg'] and zfields.indexOf(zone_fields['sgg']) >= 0) else None
        f_prj = zone_fields['prj'] if (
            zone_fields['prj'] and zfields.indexOf(zone_fields['prj']) >= 0) else None

        idx = QgsSpatialIndex(grid_layer.getFeatures())
        fid_info = {}
        for feat in grid_layer.getFeatures():
            fid_info[feat.id()] = (
                str(feat[gid_field]).strip(),
                int(str(feat[sgg_field]).strip()),
                feat.geometry(),
            )

        transform = None
        if grid_layer.crs() != zone_layer.crs():
            feedback.pushInfo(
                f'  CRS 변환: {zone_layer.crs().authid()} → {grid_layer.crs().authid()}')
            transform = QgsCoordinateTransform(
                zone_layer.crs(), grid_layer.crs(), QgsProject.instance())

        dev_total = {}
        dev_alloc = {}
        n_zone = 0
        n_skip = 0
        dropped = 0.0
        for zf in zone_layer.getFeatures():
            try:
                plan_pop = float(zf[f_pop])
            except (TypeError, ValueError):
                plan_pop = 0.0
            if plan_pop < 0:
                raise QgsProcessingException(
                    f'개발사업 fid={zf.id()} 계획인구({f_pop}) 음수는 '
                    f'허용되지 않습니다: {plan_pop}')
            if plan_pop == 0:
                n_skip += 1
                continue
            try:
                yr_done = int(str(zf[f_yr]).strip())
            except (TypeError, ValueError):
                feedback.pushWarning(
                    f'  개발사업 fid={zf.id()}: 준공연도({f_yr}) 없음/비정수 → 제외')
                n_skip += 1
                continue

            # 입주율 구간 판정 인구 (사업 전체 합 우선)
            band_pop = None
            if f_prj:
                try:
                    band_pop = float(zf[f_prj])
                except (TypeError, ValueError):
                    band_pop = None

            # 입주율 분산 → 목표연도 매핑
            inflow = {}
            for y, v in core.entry_schedule(plan_pop, yr_done, band_pop).items():
                t = core.target_of(y, years)
                if t is None:
                    dropped += v
                else:
                    inflow[t] = inflow.get(t, 0.0) + v
            if not inflow:
                n_skip += 1
                continue

            # 격자 교차
            geom = zf.geometry()
            if transform:
                geom.transform(transform)
            hits = []   # (gid, sgg)
            for fid in idx.intersects(geom.boundingBox()):
                gid, sgg, ggeom = fid_info[fid]
                if ggeom.intersects(geom):
                    hits.append((gid, sgg))
            if not hits:
                feedback.pushWarning(f'  개발사업 fid={zf.id()}: 교차 격자 없음 → 제외')
                n_skip += 1
                continue

            # 관할 시군 지정 시 해당 시군 격자로만, 아니면 교차 시군 1/n
            zsgg = None
            if f_sgg and zf[f_sgg] is not None and str(zf[f_sgg]).strip():
                try:
                    zsgg = int(str(zf[f_sgg]).strip())
                except (TypeError, ValueError):
                    raise QgsProcessingException(
                        f'개발사업 fid={zf.id()} 관할 시군({f_sgg})을 정수로 '
                        f'변환할 수 없습니다: {zf[f_sgg]!r}')
            if zsgg is not None:
                gids = [gid for gid, s in hits if s == zsgg]
                if not gids:
                    feedback.pushWarning(
                        f'  개발사업 fid={zf.id()}: 관할 시군 {zsgg} 격자와 교차 없음 → 제외')
                    n_skip += 1
                    continue
                sgg_gids = {zsgg: gids}
            else:
                sgg_gids = {}
                for gid, s in hits:
                    sgg_gids.setdefault(s, []).append(gid)

            share = 1.0 / len(sgg_gids)
            for sgg, gids in sgg_gids.items():
                k = unit.get(sgg, 1.0) if unit else 1.0   # 원단위 (근무인구 배분)
                for t, v in inflow.items():
                    amt = v * share * k
                    dev_total.setdefault(sgg, {})[t] = \
                        dev_total.get(sgg, {}).get(t, 0.0) + amt
                    per = amt / len(gids)
                    for gid in gids:
                        dev_alloc.setdefault(gid, {})[t] = \
                            dev_alloc.get(gid, {}).get(t, 0.0) + per
            n_zone += 1

        feedback.pushInfo(f'  → 개발사업 {n_zone:,}건 반영, {n_skip}건 제외')
        if dropped > 0:
            feedback.pushWarning(
                f'  마지막 목표연도({years[-1]}) 이후 유입 인구 {dropped:,.0f}명 반영 불가')
        return dev_total, dev_alloc

    # ── 보조 CSV 출력 ─────────────────────────────────────
    @staticmethod
    def _write_split_csv(path, split, years):
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            header = ['sgg_cd_h']
            header += [f'dev_{yr}' for yr in years]
            header += [f'ndev_{yr}' for yr in years]
            w.writerow(header)
            for sgg in sorted(split):
                row = [sgg]
                row += [round(split[sgg][yr][0], 4) for yr in years]
                row += [round(split[sgg][yr][1], 4) for yr in years]
                w.writerow(row)

    def shortHelpString(self):
        return (
            '시군구 장래인구 추계를 개발사업(장래토지이용계획)을 반영하여 1km 격자에\n'
            '배분합니다.\n\n'
            '━ 입력\n'
            '  · 격자 레이어: gid, sgg_cd_h, 기준연도 인구 필드\n'
            '  · 추계 테이블(csv/xlsx): sgg_cd_h + pop_YYYY — 시군 총량은 이 값 유지\n'
            '  · 개발사업 지구 경계: 계획인구(명)·준공연도 필드 +\n'
            '    사업 전체 계획인구 합(선택 — 복수 시군 분할 사업의 입주율\n'
            '    구간 판정용), 관할 시군(선택 — 있으면 해당 시군 격자로만\n'
            '    배분, 없으면 교차 시군 1/n 분할)\n'
            '  ※ 필드명이 표준(plan_pop/yr_done 등)과 달라도 각 ↳ 필드\n'
            '    파라미터에서 선택하면 됨\n'
            '  ※ 개발격자 판정: 사업 경계가 걸린(교차) 모든 격자에 배분\n\n'
            '━ 처리 단계\n'
            '  1) 사업별 계획인구를 KOTI 입주율 규칙으로 준공연도부터 연도 분산\n'
            '     (10만명 이상: 30/20/20/20/10%, 5만~10만: 50/20/15/15%, 5만 미만: 75/15/10%)\n'
            '     연도별 값은 최대잔여법 정수 보정으로 합계 = 계획인구 유지\n'
            '     유입연도는 자신보다 큰 최소 목표연도에 반영 (예: 2028·2029 → 2030)\n'
            '  2) 비개발 인구(총량−개발)를 직전 시점 격자인구 비중에 비례 배분\n'
            '  3) 개발 인구를 사업지구 교차 격자에 균등 배분\n'
            '  4) 합산 결과가 다음 목표연도의 배분 가중치로 연쇄\n'
            '     (이전 연도 개발지구는 거주지화되어 비개발 배분에 포함)\n\n'
            '━ 출력\n'
            '  · 배분 결과 레이어: 입력 필드 + pop_YYYY\n'
            '  · 분배 CSV: sgg_cd_h, dev_YYYY(개발), ndev_YYYY(비개발)\n\n'
            '━ 연계\n'
            '  비교 도구: 1. BAU 격자 배분 / 2. 거점유도 격자 배분'
        )
