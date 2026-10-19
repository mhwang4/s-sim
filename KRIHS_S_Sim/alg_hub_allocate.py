# -*- coding: utf-8 -*-
"""2. 거점유도 격자 배분 — 정책관심구역(거점) 지수모형으로 연도 순차 배분."""
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterField,
    QgsProcessingParameterFile,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
)

from . import core
from .layer_io import load_grid_groups, load_hub_set, validate_sgg_match, write_grid_sink


class HubAllocateAlgorithm(QgsProcessingAlgorithm):

    INPUT = 'INPUT'
    GID_FIELD = 'GID_FIELD'
    SGG_FIELD = 'SGG_FIELD'
    POP_FIELD = 'POP_FIELD'
    TABLE = 'TABLE'
    HUB_LAYER = 'HUB_LAYER'
    HUB_GID_FIELD = 'HUB_GID_FIELD'
    PARAM_A = 'PARAM_A'
    PARAM_B = 'PARAM_B'
    PARAM_C = 'PARAM_C'
    PARAM_D = 'PARAM_D'
    OUTPUT = 'OUTPUT'

    def name(self):
        return 'hub_grid_allocate'

    def displayName(self):
        return '2. 거점유도 격자 배분'

    def group(self):
        return '3. 장래인구 배분모형'

    def groupId(self):
        return 'pop_allocation'

    def createInstance(self):
        return HubAllocateAlgorithm()

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
            '시군구 인구 추계 테이블 (sgg_cd_h + pop_YYYY)',
            fileFilter='추계 테이블 (*.csv *.xlsx)',
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.HUB_LAYER,
            '정책관심구역 (거점 격자 목록)',
            types=[QgsProcessing.TypeVector],
        ))
        self.addParameter(QgsProcessingParameterField(
            self.HUB_GID_FIELD,
            '↳ 격자 ID 필드 (기본: gid)',
            defaultValue='gid',
            parentLayerParameterName=self.HUB_LAYER,
        ))
        for key, label, default in (
            (self.PARAM_A, '지수 a — 거점 · ratio >= 1 (인구 다 거점 집중 강화)', 1.1),
            (self.PARAM_B, '지수 b — 거점 · ratio < 1 (인구 소 거점 감소 완화)', 0.9),
            (self.PARAM_C, '지수 c — 비거점 · ratio >= 1 (인구 다 비거점 집중 억제)', 0.9),
            (self.PARAM_D, '지수 d — 비거점 · ratio < 1 (인구 소 비거점 감소 강화)', 1.1),
        ):
            self.addParameter(QgsProcessingParameterNumber(
                key, label,
                type=QgsProcessingParameterNumber.Double,
                defaultValue=default, minValue=0.01, maxValue=10.0,
            ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT,
            '거점유도 배분 결과',
        ))

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        gid_field = self.parameterAsString(parameters, self.GID_FIELD, context)
        sgg_field = self.parameterAsString(parameters, self.SGG_FIELD, context)
        pop_field = self.parameterAsString(parameters, self.POP_FIELD, context)
        table_path = self.parameterAsString(parameters, self.TABLE, context)
        hub_layer = self.parameterAsVectorLayer(parameters, self.HUB_LAYER, context)
        hub_gid_field = self.parameterAsString(parameters, self.HUB_GID_FIELD, context)
        a = self.parameterAsDouble(parameters, self.PARAM_A, context)
        b = self.parameterAsDouble(parameters, self.PARAM_B, context)
        c = self.parameterAsDouble(parameters, self.PARAM_C, context)
        d = self.parameterAsDouble(parameters, self.PARAM_D, context)

        # ── [1단계] 추계 테이블 로드 (0~10) ────────────────
        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[1단계] 시군구 추계 테이블 로드')
        try:
            table, years, n_null = core.load_sgg_table(table_path)
        except ValueError as e:
            raise QgsProcessingException(str(e))
        feedback.pushInfo(f'  → {len(table):,}개 시군구, 연도: {", ".join(map(str, years))}')
        if n_null:
            feedback.pushWarning(f'  인구 NULL {n_null}건 → 0 처리')
        feedback.setProgress(10)

        # ── [2단계] 격자·거점 로드·검증 (10~30) ────────────
        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[2단계] 격자·정책관심구역 로드 및 검증')
        groups = load_grid_groups(layer, pop_field, feedback, gid_field, sgg_field)
        n_grid = sum(len(v) for v in groups.values())
        feedback.pushInfo(f'  → {n_grid:,}개 격자, {len(groups)}개 시군구')
        validate_sgg_match(groups.keys(), table.keys(), '추계 테이블', feedback)

        grid_gids = {g['gid'] for grids in groups.values() for g in grids}
        hub_set = load_hub_set(hub_layer, hub_gid_field, grid_gids, feedback)
        feedback.setProgress(30)

        # ── [3단계] 지수모형 연도 순차 배분 (30~80) ────────
        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[3단계] 거점유도 지수 배분')
        feedback.pushInfo(f'  지수: a={a}  b={b}  c={c}  d={d}')
        allocated = {}
        n_sgg = len(groups)
        for si, (sgg, grids) in enumerate(sorted(groups.items())):
            if feedback.isCanceled():
                return {}
            feedback.setProgress(30 + int(si / n_sgg * 50))

            core.init_ratio(grids)
            for yr in years:
                weights = {
                    g['gid']: core.exp_weight(g['ratio'], g['gid'] in hub_set, a, b, c, d)
                    for g in grids
                }
                result, equal = core.allocate_by_weight(weights, table[sgg][yr])
                if equal:
                    feedback.pushWarning(
                        f'  시군구 {sgg} {yr}년: 가중치 합 0 → 균등 배분')
                for gid, pop in result.items():
                    allocated.setdefault(gid, {})[yr] = pop
                if table[sgg][yr] > 0:
                    core.update_ratio(grids, result)

        # ── [4단계] 출력 (80~99) ───────────────────────────
        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[4단계] 결과 레이어 출력')
        dest_id, n_written = write_grid_sink(
            self, parameters, self.OUTPUT, context,
            layer, years, allocated, feedback, gid_field=gid_field)

        feedback.pushInfo('━' * 50)
        feedback.pushInfo(f'완료: 격자 {n_written:,}개 출력')
        return {self.OUTPUT: dest_id}

    def shortHelpString(self):
        return (
            '시군구 단위 장래인구 추계를 거점/비거점 지수모형으로 1km 격자에 배분합니다.\n'
            '연도 오름차순으로 순차 배분하며, 각 연도의 배분 결과가 다음 연도의\n'
            'ratio(격자인구/시군구평균)로 연쇄 반영됩니다.\n\n'
            '━ 입력\n'
            '  · 격자 레이어: gid, sgg_cd_h, 기준연도 인구 필드\n'
            '  · 추계 테이블(csv/xlsx): sgg_cd_h + pop_YYYY — 연도 자동 감지\n'
            '  · 정책관심구역: 거점 격자 ID 목록 (gid 매칭, 지오메트리 불필요)\n\n'
            '━ 가중치 (기본값)\n'
            '  a=1.1 거점·인구다 집중 강화 / b=0.9 거점·인구소 감소 완화\n'
            '  c=0.9 비거점·인구다 집중 억제 / d=1.1 비거점·인구소 감소 강화\n\n'
            '━ 출력\n'
            '  · 입력 격자 필드 유지 + pop_YYYY (연도별 배분 인구)\n\n'
            '━ 연계\n'
            '  비교 도구: 1. BAU 격자 배분 / 3. 계획인구 격자 배분'
        )
