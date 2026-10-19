# -*- coding: utf-8 -*-
"""0. 정책관심구역 산출 — 거점 유형 ∪ 생활인프라 충족도 기준으로 유도구역 격자 선정.

출력은 유도구역 격자만 담은 레이어(IN-3) — 그대로 '2. 거점유도 격자 배분'의
정책관심구역 입력으로 사용한다.
"""
from PyQt5.QtCore import QVariant
from qgis.core import (
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
)

from .layer_io import require_fields


class PolicyAreaAlgorithm(QgsProcessingAlgorithm):

    GRID = 'GRID'
    GID_FIELD = 'GID_FIELD'
    TYPE_FIELD = 'TYPE_FIELD'
    SELECTED_TYPES = 'SELECTED_TYPES'
    INFRA = 'INFRA'
    INFRA_GID_FIELD = 'INFRA_GID_FIELD'
    CI1_FIELD = 'CI1_FIELD'
    CI2_FIELD = 'CI2_FIELD'
    USE_CI1 = 'USE_CI1'
    USE_CI2 = 'USE_CI2'
    CI1_MIN = 'CI1_MIN'
    CI2_MIN = 'CI2_MIN'
    OUTPUT = 'OUTPUT'

    TYPE_OPTIONS = ['주변부', '중심지Ⅰ', '중심지Ⅱ', '중심지Ⅲ-1', '중심지Ⅲ-2']

    def name(self):
        return 'policy_interest_area'

    def displayName(self):
        return '0. 정책관심구역 산출'

    def group(self):
        return '3. 장래인구 배분모형'

    def groupId(self):
        return 'pop_allocation'

    def createInstance(self):
        return PolicyAreaAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.GRID,
            '국토공간거점지도 1km 격자',
            types=[QgsProcessing.TypeVectorPolygon],
        ))
        self.addParameter(QgsProcessingParameterField(
            self.GID_FIELD,
            '↳ 격자 ID 필드 (기본: gid)',
            defaultValue='gid',
            parentLayerParameterName=self.GRID,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.TYPE_FIELD,
            '↳ 거점 유형 필드 (기본: type)',
            defaultValue='type',
            parentLayerParameterName=self.GRID,
        ))
        self.addParameter(QgsProcessingParameterEnum(
            self.SELECTED_TYPES,
            '거점으로 볼 유형 (복수 선택)',
            options=self.TYPE_OPTIONS,
            allowMultiple=True,
            defaultValue=[1, 2, 3, 4],
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INFRA,
            '국토생활인프라 충족도 레이어 (선택)',
            types=[QgsProcessing.TypeVectorPolygon],
            optional=True,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.INFRA_GID_FIELD,
            '↳ 격자 ID 필드 (기본: GRID_1K_CD)',
            defaultValue='GRID_1K_CD',
            parentLayerParameterName=self.INFRA,
            optional=True,
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.USE_CI1, '마을시설 충족도 기준 사용', defaultValue=True))
        self.addParameter(QgsProcessingParameterField(
            self.CI1_FIELD,
            '↳ 마을시설 충족도 필드 (기본: Me_CI_1)',
            defaultValue='Me_CI_1',
            parentLayerParameterName=self.INFRA,
            type=QgsProcessingParameterField.Numeric,
            optional=True,
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.CI1_MIN,
            '↳ 마을시설 충족도 기준값 (이상)',
            type=QgsProcessingParameterNumber.Double,
            minValue=0.0, maxValue=10.0, defaultValue=3.0,
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.USE_CI2, '거점시설 충족도 기준 사용', defaultValue=True))
        self.addParameter(QgsProcessingParameterField(
            self.CI2_FIELD,
            '↳ 거점시설 충족도 필드 (기본: Me_CI_2)',
            defaultValue='Me_CI_2',
            parentLayerParameterName=self.INFRA,
            type=QgsProcessingParameterField.Numeric,
            optional=True,
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.CI2_MIN,
            '↳ 거점시설 충족도 기준값 (이상)',
            type=QgsProcessingParameterNumber.Double,
            minValue=0.0, maxValue=10.0, defaultValue=3.0,
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT,
            '정책관심구역 (유도구역 격자만)',
        ))

    def processAlgorithm(self, parameters, context, feedback):
        grid = self.parameterAsVectorLayer(parameters, self.GRID, context)
        gid_field = self.parameterAsString(parameters, self.GID_FIELD, context)
        type_field = self.parameterAsString(parameters, self.TYPE_FIELD, context)
        type_idx = self.parameterAsEnums(parameters, self.SELECTED_TYPES, context)
        infra = self.parameterAsVectorLayer(parameters, self.INFRA, context)
        use_ci1 = self.parameterAsBoolean(parameters, self.USE_CI1, context)
        use_ci2 = self.parameterAsBoolean(parameters, self.USE_CI2, context)

        selected = {self.TYPE_OPTIONS[i] for i in type_idx}
        total = grid.featureCount() or 1

        # ── [1단계] 거점 유형 필터 (0~30) ──────────────────
        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[1단계] 거점 유형 필터링')
        feedback.pushInfo(f'  선택 유형: {", ".join(sorted(selected))}')
        require_fields(grid, (gid_field, type_field), '격자')

        type_gids = set()
        for i, feat in enumerate(grid.getFeatures()):
            if feedback.isCanceled():
                return {}
            feedback.setProgress(int(i / total * 30))
            gid = str(feat[gid_field]).strip() if feat[gid_field] is not None else ''
            if gid and feat[type_field] in selected:
                type_gids.add(gid)
        feedback.pushInfo(f'  → 거점 유형 충족 격자: {len(type_gids):,}개')

        # ── [2단계] 생활인프라 충족도 필터 (30~60) ─────────
        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[2단계] 생활인프라 충족도 필터링')
        ci_gids = set()
        if infra is not None and (use_ci1 or use_ci2):
            infra_gid = self.parameterAsString(parameters, self.INFRA_GID_FIELD, context)
            ci1_field = self.parameterAsString(parameters, self.CI1_FIELD, context)
            ci2_field = self.parameterAsString(parameters, self.CI2_FIELD, context)
            t1 = self.parameterAsDouble(parameters, self.CI1_MIN, context)
            t2 = self.parameterAsDouble(parameters, self.CI2_MIN, context)
            need = [infra_gid]
            if use_ci1:
                need.append(ci1_field)
                feedback.pushInfo(f'  마을시설({ci1_field}) ≥ {t1}')
            if use_ci2:
                need.append(ci2_field)
                feedback.pushInfo(f'  거점시설({ci2_field}) ≥ {t2}')
            require_fields(infra, need, '생활인프라')

            n_infra = infra.featureCount() or 1
            for i, feat in enumerate(infra.getFeatures()):
                if feedback.isCanceled():
                    return {}
                feedback.setProgress(30 + int(i / n_infra * 30))
                gid = str(feat[infra_gid]).strip() if feat[infra_gid] is not None else ''
                if not gid:
                    continue
                ci1 = feat[ci1_field] if use_ci1 and feat[ci1_field] is not None else None
                ci2 = feat[ci2_field] if use_ci2 and feat[ci2_field] is not None else None
                if (use_ci1 and ci1 is not None and float(ci1) >= t1) or \
                   (use_ci2 and ci2 is not None and float(ci2) >= t2):
                    ci_gids.add(gid)
            feedback.pushInfo(f'  → 충족도 기준 충족 격자: {len(ci_gids):,}개')
        else:
            feedback.pushInfo('  생활인프라 레이어 미지정 또는 기준 미사용 → 유형 필터만 적용')

        # ── [3단계] 합집합 → 유도구역 격자 출력 (60~99) ────
        feedback.pushInfo('━' * 50)
        feedback.pushInfo('[3단계] 정책관심구역 산출 (합집합)')
        target = type_gids | ci_gids
        feedback.pushInfo(f'  유형 전용 {len(type_gids - ci_gids):,} / '
                          f'충족도 전용 {len(ci_gids - type_gids):,} / '
                          f'교집합 {len(type_gids & ci_gids):,}')
        feedback.pushInfo(f'  유도구역 {len(target):,}개 · 주변 {total - len(target):,}개')

        out_fields = QgsFields()
        for f in grid.fields():
            out_fields.append(f)
        out_fields.append(QgsField('src', QVariant.String, len=10))
        sink, dest_id = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            out_fields, grid.wkbType(), grid.sourceCrs())

        n_out = 0
        for i, feat in enumerate(grid.getFeatures()):
            if feedback.isCanceled():
                return {}
            feedback.setProgress(60 + int(i / total * 39))
            gid = str(feat[gid_field]).strip() if feat[gid_field] is not None else ''
            if gid not in target:
                continue
            new_feat = QgsFeature(out_fields)
            new_feat.setGeometry(feat.geometry())
            for f in grid.fields():
                new_feat[f.name()] = feat[f.name()]
            in_type, in_ci = gid in type_gids, gid in ci_gids
            new_feat['src'] = 'TYPE+CI' if (in_type and in_ci) else ('TYPE' if in_type else 'CI')
            sink.addFeature(new_feat, QgsFeatureSink.FastInsert)
            n_out += 1

        feedback.pushInfo('━' * 50)
        feedback.pushInfo(f'완료: 유도구역 격자 {n_out:,}개 출력')
        return {self.OUTPUT: dest_id}

    def shortHelpString(self):
        return (
            '국토공간거점지도의 거점 유형과 국토생활인프라 충족도를 기반으로\n'
            '정책관심구역(유도구역 격자)을 산출합니다.\n\n'
            '━ 입력\n'
            '  · 격자 레이어: gid + 거점 유형(type) 필드 — 격자_충남_2023.shp 사용 가능\n'
            '  · 생활인프라 레이어(선택): GRID_1K_CD + Me_CI_1/Me_CI_2 충족도\n'
            '    미지정 시 거점 유형 필터만 적용\n\n'
            '━ 처리 단계\n'
            '  1) 거점 유형 필터 2) 충족도 필터 3) 합집합 → 유도구역 격자만 출력\n\n'
            '━ 출력\n'
            '  · 유도구역 격자 레이어: 입력 필드 유지 + src (TYPE/CI/TYPE+CI 선정 근거)\n\n'
            '━ 연계\n'
            '  다음 도구: 2. 거점유도 격자 배분 — 이 출력을 정책관심구역 입력으로\n'
            '  그대로 사용 (별도 필터 불필요)'
        )
