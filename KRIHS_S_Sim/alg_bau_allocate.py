# -*- coding: utf-8 -*-
"""1. BAU 격자 배분 — 시군구 추계 인구를 기준연도 격자인구 단순 비례로 배분."""
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterField,
    QgsProcessingParameterFile,
    QgsProcessingParameterVectorLayer,
)

from . import core
from .layer_io import load_grid_groups, validate_sgg_match, write_grid_sink


class BauAllocateAlgorithm(QgsProcessingAlgorithm):

    INPUT = 'INPUT'
    GID_FIELD = 'GID_FIELD'
    SGG_FIELD = 'SGG_FIELD'
    POP_FIELD = 'POP_FIELD'
    TABLE = 'TABLE'
    OUTPUT = 'OUTPUT'

    def name(self):
        return 'bau_grid_allocate'

    def displayName(self):
        return '1. BAU 격자 배분'

    def group(self):
        return '3. 장래인구 배분모형'

    def groupId(self):
        return 'pop_allocation'

    def createInstance(self):
        return BauAllocateAlgorithm()

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
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT,
            'BAU 배분 결과',
        ))

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        gid_field = self.parameterAsString(parameters, self.GID_FIELD, context)
        sgg_field = self.parameterAsString(parameters, self.SGG_FIELD, context)
        pop_field = self.parameterAsString(parameters, self.POP_FIELD, context)
        table_path = self.parameterAsString(parameters, self.TABLE, context)

        # ── [1단계] 추계 테이블 로드 (진행률 0~10) ─────────
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

        # ── [2단계] 격자 로드·검증 (10~30) ─────────────────
        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[2단계] 격자 로드 및 검증')
        groups = load_grid_groups(layer, pop_field, feedback, gid_field, sgg_field)
        n_grid = sum(len(v) for v in groups.values())
        feedback.pushInfo(f'  → {n_grid:,}개 격자, {len(groups)}개 시군구')
        validate_sgg_match(groups.keys(), table.keys(), '추계 테이블', feedback)
        feedback.setProgress(30)

        # ── [3단계] 시군구별 단순 비례 배분 (30~80) ────────
        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[3단계] 기준인구 비례 배분')
        allocated = {}
        n_sgg = len(groups)
        for si, (sgg, grids) in enumerate(sorted(groups.items())):
            if feedback.isCanceled():
                return {}
            feedback.setProgress(30 + int(si / n_sgg * 50))

            weights = {g['gid']: g['pop0'] for g in grids}
            for yr in years:
                result, equal = core.allocate_by_weight(weights, table[sgg][yr])
                if equal:
                    feedback.pushWarning(
                        f'  시군구 {sgg} {yr}년: 기준인구 합 0 → 균등 배분')
                for gid, pop in result.items():
                    allocated.setdefault(gid, {})[yr] = pop

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
            '시군구 단위 장래인구 추계를 기준연도 격자인구 비례로 1km 격자에 배분합니다.\n'
            '가중치가 고정되어 모든 연도가 같은 분포 패턴을 가집니다 (BAU 시나리오).\n\n'
            '━ 입력\n'
            '  · 격자 레이어: gid(중복 금지), sgg_cd_h(시군구코드), 기준연도 인구 필드\n'
            '  · 추계 테이블(csv/xlsx): sgg_cd_h + pop_YYYY (예: pop_2025) — 연도 자동 감지\n'
            '  ※ 격자의 모든 시군구가 테이블에 있어야 합니다 (누락 시 에러)\n\n'
            '━ 처리 단계\n'
            '  1) 테이블 로드 2) 격자 검증 3) 시군구별 비례 배분 4) 출력\n\n'
            '━ 출력\n'
            '  · 입력 격자 필드 유지 + pop_YYYY (연도별 배분 인구)\n\n'
            '━ 연계\n'
            '  다음 도구: 2. 거점유도 격자 배분 / 3. 계획인구 격자 배분 (시나리오 비교)'
        )
