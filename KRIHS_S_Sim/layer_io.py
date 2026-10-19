# -*- coding: utf-8 -*-
"""QGIS 레이어 입출력·검증 헬퍼 — 배분 알고리즘 3종 공유."""
from PyQt5.QtCore import QVariant
from qgis.core import (
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsProcessingException,
)

def require_fields(layer, names, layer_label):
    """필수 필드 존재 검사. 없으면 즉시 중단."""
    missing = [n for n in names if layer.fields().indexOf(n) < 0]
    if missing:
        raise QgsProcessingException(
            f'{layer_label} 레이어에 필수 필드가 없습니다: {", ".join(missing)}. '
            'INPUT_SPEC.md 표준 형식을 따르거나 도구의 필드 파라미터로 지정하세요.')


def load_grid_groups(layer, pop_field, feedback, gid_field='gid', sgg_field='sgg_cd_h'):
    """격자 레이어(IN-1) 로드 → {sgg: [{'gid','pop0','ratio'}]}.

    검증: 필수 필드, 격자 ID 중복, 시군구 코드 정수 변환, 기준인구 음수.
    NULL/비수치 기준인구는 0 처리 후 경고.
    """
    require_fields(layer, (gid_field, sgg_field, pop_field), '격자')
    groups = {}
    seen = set()
    n_null = 0
    for feat in layer.getFeatures():
        raw_gid = feat[gid_field]
        gid = str(raw_gid).strip() if raw_gid is not None else ''
        if not gid or gid.upper() == 'NULL':
            raise QgsProcessingException(
                f'격자 ID({gid_field}) 값이 비어 있습니다 (피처 id={feat.id()}). '
                '모든 격자에 채워져 있어야 합니다.')
        if gid in seen:
            raise QgsProcessingException(f'격자 ID({gid_field}) 중복: {gid}')
        seen.add(gid)
        try:
            sgg = int(str(feat[sgg_field]).strip())
        except (TypeError, ValueError):
            raise QgsProcessingException(
                f'격자 시군구 코드({sgg_field})를 정수로 변환할 수 없습니다 '
                f'(격자 ID={gid}): {feat[sgg_field]!r}')
        try:
            pop0 = float(feat[pop_field])
        except (TypeError, ValueError):
            pop0 = 0.0
            n_null += 1
        if pop0 < 0:
            raise QgsProcessingException(
                f'격자 기준인구({pop_field}) 음수는 허용되지 않습니다 (gid={gid}): {pop0}')
        groups.setdefault(sgg, []).append({'gid': gid, 'pop0': pop0, 'ratio': 0.0})
    if not groups:
        raise QgsProcessingException('격자 레이어에 피처가 없습니다.')
    if n_null:
        feedback.pushWarning(f'  기준인구 NULL/비수치 {n_null:,}개 격자 → 0 처리')
    return groups


def validate_sgg_match(grid_sggs, table_sggs, table_label, feedback):
    """격자-테이블 시군구 대조.

    격자에 있는데 테이블에 없으면 에러, 반대는 경고(해당 시군구 스킵).
    """
    missing = sorted(set(grid_sggs) - set(table_sggs))
    if missing:
        raise QgsProcessingException(
            f'격자에 있는 시군구가 {table_label}에 없습니다: '
            f'{", ".join(str(s) for s in missing)}. '
            '격자를 대상 지역으로 필터링했는지, 테이블 누락이 없는지 확인하세요.')
    extra = sorted(set(table_sggs) - set(grid_sggs))
    if extra:
        feedback.pushWarning(
            f'  {table_label}에만 있는 시군구 {len(extra)}개 스킵: '
            f'{", ".join(str(s) for s in extra)}')


def load_hub_set(layer, gid_field, grid_gids, feedback):
    """정책관심구역(IN-3) 로드 → 거점 gid 집합. 격자 밖 gid는 경고."""
    require_fields(layer, (gid_field,), '정책관심구역')
    hubs = set()
    for feat in layer.getFeatures():
        v = feat[gid_field]
        if v is None:
            continue
        gid = str(v).strip()
        if gid:
            hubs.add(gid)
    if not hubs:
        raise QgsProcessingException('정책관심구역 레이어에 유효한 격자 ID가 없습니다.')
    matched = hubs & grid_gids
    n_out = len(hubs) - len(matched)
    feedback.pushInfo(f'  거점 격자 {len(matched):,}개 매칭')
    if n_out:
        feedback.pushWarning(f'  격자에 없는 거점 ID {n_out:,}개 무시')
    return matched


def write_grid_sink(alg, parameters, output_key, context, layer, years, allocated,
                    feedback, prog_from=80, prog_to=99, gid_field='gid'):
    """입력 격자 필드 유지 + pop_YYYY 컬럼 추가하여 FeatureSink 출력."""
    out_fields = QgsFields()
    for f in layer.fields():
        out_fields.append(f)
    for yr in years:
        out_fields.append(QgsField(f'pop_{yr}', QVariant.Double))

    sink, dest_id = alg.parameterAsSink(
        parameters, output_key, context,
        out_fields, layer.wkbType(), layer.sourceCrs())

    total = layer.featureCount() or 1
    n = 0
    for i, feat in enumerate(layer.getFeatures()):
        if feedback.isCanceled():
            return dest_id, n
        feedback.setProgress(prog_from + int(i / total * (prog_to - prog_from)))
        gid = str(feat[gid_field]).strip()
        alloc = allocated.get(gid, {})
        new_feat = QgsFeature(out_fields)
        new_feat.setGeometry(feat.geometry())
        for f in layer.fields():
            new_feat[f.name()] = feat[f.name()]
        for yr in years:
            new_feat[f'pop_{yr}'] = round(alloc.get(yr, 0.0), 4)
        sink.addFeature(new_feat, QgsFeatureSink.FastInsert)
        n += 1
    return dest_id, n
