import random

from qgis.PyQt.QtCore import QVariant, Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterField,
    QgsProcessingParameterVectorLayer,
    QgsProcessingUtils,
    QgsProject,
    QgsRendererCategory,
    QgsSymbol,
)

try:
    from qgis.core import QgsProcessingParameterSeparator as _QgsSep
    def _sep(name: str, label: str):
        return _QgsSep(name, label)
except ImportError:
    _sep = None


# ──────────────────────────────────────────────────────────────────────────── #
# 로그 포맷 헬퍼                                                                #
# ──────────────────────────────────────────────────────────────────────────── #

def _bar(char: str = "━", width: int = 56) -> str:
    return char * width

def _section(title: str) -> str:
    return f"\n{_bar()}\n  {title}\n{_bar()}"

def _item(label: str, value: str, indent: int = 2) -> str:
    pad = " " * indent
    return f"{pad}  {label} : {value}"


class ODMatrixRegionAssign(QgsProcessingAlgorithm):

    # OD Matrix 레이어 + 필드
    INPUT_OD         = 'INPUT_OD'
    FIELD_ORIGIN     = 'FIELD_ORIGIN'
    FIELD_DEST       = 'FIELD_DEST'
    FIELD_COST       = 'FIELD_COST'

    # 격자 레이어 + 필드
    INPUT_GRID       = 'INPUT_GRID'
    FIELD_GRID_GID   = 'FIELD_GRID_GID'

    # 중심지 레이어 + 필드
    INPUT_CENTER     = 'INPUT_CENTER'
    FIELD_CENTER_GID = 'FIELD_CENTER_GID'
    FIELD_CENT_ID    = 'FIELD_CENT_ID'
    FIELD_CENT_NM    = 'FIELD_CENT_NM'

    # 출력
    OUTPUT = 'OUTPUT'

    # 이 도구가 새로 추가하는 출력 필드 (기존 동명 필드는 교체)
    _OUT_FIELDS = ('dest_id', 'total_cost', 'cost_adj', 'na_count', 'cent_id', 'cent_nm')

    # ------------------------------------------------------------------ #
    # 메타데이터                                                           #
    # ------------------------------------------------------------------ #

    def createInstance(self):
        return ODMatrixRegionAssign()

    def name(self):
        return 'od_matrix_region_assign'

    def displayName(self):
        return '3. 거리 행렬 기반 기초생활권 할당_update'

    def group(self):
        return '2. 중심지-생활권 경계 도출'

    def groupId(self):
        return 'region_boundary'

    def shortHelpString(self):
        return (
            "격자→중심지 OD 행렬의 이동 비용을 비교해, 각 격자를 비용이 가장 낮은 "
            "중심지 권역에 배정합니다. 결과가 기초생활권의 원형이 되며, "
            "'4. 기초생활권 스무딩_update'으로 경계를 다듬습니다.\n\n"
            "【입력】\n"
            "  · OD 매트릭스 : '2. 격자→중심지 OD 행렬 산출_update' 출력 CSV를 레이어로 추가 후 선택\n"
            "    (레이어 → 레이어 추가 → 구분자로 분리된 텍스트 레이어)\n"
            "    필요 필드: origin_id(출발 격자) / destination_id(도착 중심지 격자) / total_cost(비용)\n"
            "  · 국토공간거점지도 : 권역을 배정할 1km 격자 (gid 필드) — 그룹1과 동일 원천\n"
            "  · 중심지 중심점 : '1. 중심지 중심점 생성_update' 출력 (gid, cent_id, cent_nm 필드)\n\n"
            "【처리 단계】\n"
            "  1) OD 행렬 로드 (선택한 필드명 그대로 사용)\n"
            "  2) 격자별 최소 비용 중심지 선정\n"
            "     — 출발지=도착지인 경우 비용을 0.1로 보정해 자기 권역 최우선 배정\n"
            "  3) 중심지 정보(cent_id·cent_nm) 결합\n"
            "  4) 격자에 조인해 출력 (cent_nm 기준 카테고리 심볼 자동 적용)\n\n"
            "【출력 필드】 (모두 10자 이내 — shp 호환)\n"
            "  · dest_id    : 배정된 중심지의 격자 ID\n"
            "  · total_cost : 해당 중심지까지의 원본 비용\n"
            "  · cost_adj   : 자기권역 보정(0.1) 반영 비용\n"
            "  · na_count   : 비용 결측이었던 후보 중심지 수\n"
            "  · cent_id / cent_nm : 배정된 중심지 ID·이름\n"
            "  · 도달 불가 격자는 위 필드가 비어 있음 → 지도에서 '(미할당)' 회색 표시\n\n"
            "이전 도구: 1. 중심지 중심점 생성_update · 2. 격자→중심지 OD 행렬 산출_update  /  "
            "다음 도구: 4. 기초생활권 스무딩_update"
        )

    # ------------------------------------------------------------------ #
    # 파라미터 정의                                                        #
    # ------------------------------------------------------------------ #

    def initAlgorithm(self, config=None):
        # ── 입력 데이터: OD 매트릭스 ─────────────────────────────────
        if _sep: self.addParameter(_sep("SEP_OD", "입력 데이터 — OD 매트릭스"))

        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT_OD,
            "OD 매트릭스 레이어 ('2. 격자→중심지 OD 행렬 산출_update' 출력 CSV를 레이어로 추가)",
            [QgsProcessing.TypeVector],
        ))
        self.addParameter(QgsProcessingParameterField(
            self.FIELD_ORIGIN, '   ↳ 출발 격자 ID 필드 (기본: origin_id)',
            defaultValue='origin_id',
            parentLayerParameterName=self.INPUT_OD,
            type=QgsProcessingParameterField.Any,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.FIELD_DEST, '   ↳ 도착 중심지 ID 필드 (기본: destination_id)',
            defaultValue='destination_id',
            parentLayerParameterName=self.INPUT_OD,
            type=QgsProcessingParameterField.Any,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.FIELD_COST, '   ↳ 비용 필드 (기본: total_cost)',
            defaultValue='total_cost',
            parentLayerParameterName=self.INPUT_OD,
            type=QgsProcessingParameterField.Any,
        ))

        # ── 입력 데이터: 격자·중심지 ─────────────────────────────────
        if _sep: self.addParameter(_sep("SEP_LAYERS", "입력 데이터 — 격자·중심지"))


        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT_GRID,
            '국토공간거점지도 레이어 (권역을 배정할 1km 격자 — 그룹1과 동일 원천)',
            [QgsProcessing.TypeVectorPolygon, QgsProcessing.TypeVectorPoint],
        ))
        self.addParameter(QgsProcessingParameterField(
            self.FIELD_GRID_GID, '   ↳ 격자 ID 필드 — 출발 격자 ID와 매칭 (기본: gid)',
            defaultValue='gid',
            parentLayerParameterName=self.INPUT_GRID,
            type=QgsProcessingParameterField.Any,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT_CENTER,
            "중심지 중심점 레이어 ('1. 중심지 중심점 생성_update' 출력)",
            [QgsProcessing.TypeVectorPoint],
        ))
        self.addParameter(QgsProcessingParameterField(
            self.FIELD_CENTER_GID, '   ↳ 격자 ID 필드 — 도착 중심지 ID와 매칭 (기본: gid)',
            defaultValue='gid',
            parentLayerParameterName=self.INPUT_CENTER,
            type=QgsProcessingParameterField.Any,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.FIELD_CENT_ID, '   ↳ 중심지 ID 필드 (기본: cent_id)',
            defaultValue='cent_id',
            parentLayerParameterName=self.INPUT_CENTER,
            type=QgsProcessingParameterField.Any,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.FIELD_CENT_NM, '   ↳ 중심지 이름 필드 — 심볼(라벨) 기준',
            defaultValue='cent_nm',
            parentLayerParameterName=self.INPUT_CENTER,
            type=QgsProcessingParameterField.Any,
        ))

        # ── 출력 ─────────────────────────────────────────────────────
        if _sep: self.addParameter(_sep("SEP_OUTPUT", "출력"))

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, '격자 권역할당 결과',
        ))

    # ------------------------------------------------------------------ #
    # 유틸리티                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_table_path(layer) -> str:
        """delimitedtext URI·ogr 소스 문자열에서 실제 파일 경로 추출."""
        src = layer.source()
        if src.lower().startswith('file:'):
            from urllib.parse import urlparse, unquote
            from urllib.request import url2pathname
            return url2pathname(unquote(urlparse(src).path))
        return src.split('|')[0].split('?')[0]

    @staticmethod
    def _read_od_csv(path, cols, feedback):
        """인코딩 폴백(utf-8-sig → cp949)으로 OD CSV 로드."""
        import pandas as pd
        last_err = None
        for enc in ('utf-8-sig', 'cp949'):
            try:
                return pd.read_csv(path, usecols=cols, encoding=enc)
            except UnicodeDecodeError as e:
                last_err = e
                feedback.pushWarning(f'  {enc} 인코딩 실패 → 다음 인코딩 시도')
            except ValueError as e:
                raise QgsProcessingException(
                    f'OD 매트릭스에서 선택한 필드를 찾을 수 없습니다: {cols}\n{e}'
                )
        raise QgsProcessingException(f'OD CSV 인코딩을 해석할 수 없습니다: {last_err}')

    @staticmethod
    def _safe_float(val):
        try:
            v = float(val)
            return None if v != v else v   # NaN 체크
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_int(val):
        try:
            v = float(val)
            return None if v != v else int(v)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ #
    # 실행                                                                 #
    # ------------------------------------------------------------------ #

    def processAlgorithm(self, parameters, context, feedback):
        od_layer     = self.parameterAsVectorLayer(parameters, self.INPUT_OD, context)
        f_origin     = self.parameterAsString(parameters, self.FIELD_ORIGIN, context)
        f_dest       = self.parameterAsString(parameters, self.FIELD_DEST, context)
        f_cost       = self.parameterAsString(parameters, self.FIELD_COST, context)

        grid_layer   = self.parameterAsVectorLayer(parameters, self.INPUT_GRID, context)
        f_grid_gid   = self.parameterAsString(parameters, self.FIELD_GRID_GID, context)

        center_layer = self.parameterAsVectorLayer(parameters, self.INPUT_CENTER, context)
        f_center_gid = self.parameterAsString(parameters, self.FIELD_CENTER_GID, context)
        f_cent_id    = self.parameterAsString(parameters, self.FIELD_CENT_ID, context)
        f_cent_nm    = self.parameterAsString(parameters, self.FIELD_CENT_NM, context)

        # ── [1단계] OD 행렬 로드 ─────────────────────────────────────
        feedback.pushInfo(_section('[1단계] OD 행렬 로드'))
        feedback.pushInfo('  파일 용량에 따라 시간이 걸릴 수 있습니다.')

        csv_path = self._extract_table_path(od_layer)
        feedback.pushInfo(_item('경로', csv_path))

        df = self._read_od_csv(csv_path, [f_origin, f_dest, f_cost], feedback)
        # 이후 처리는 선택한 필드명과 무관하게 내부 표준 컬럼명 사용
        df = df.rename(columns={f_origin: '_orig', f_dest: '_dest', f_cost: '_cost'})

        import pandas as pd
        df['_cost'] = pd.to_numeric(df['_cost'], errors='coerce')
        feedback.pushInfo(_item('행 수', f'{len(df):,}'))
        feedback.setProgress(20)
        if feedback.isCanceled():
            return {}

        # ── [2단계] 격자별 최소 비용 중심지 선정 ─────────────────────
        feedback.pushInfo(_section('[2단계] 격자별 최소 비용 중심지 선정'))
        feedback.pushInfo('  출발지 = 도착지인 조합은 비용 0.1로 보정 (자기 권역 최우선)')

        df['_adj'] = df['_cost'].where(df['_orig'] != df['_dest'], 0.1)

        df_clean = df.dropna(subset=['_adj'])
        if df_clean.empty:
            raise QgsProcessingException(
                '유효한 비용 값이 없습니다. 비용 필드 선택이 올바른지 확인하세요.'
            )
        min_idx = df_clean.groupby('_orig')['_adj'].idxmin()
        result  = df_clean.loc[min_idx].reset_index(drop=True)

        # 전체 출발 격자와 left join (도달 불가 → 결측 유지)
        all_origins = df[['_orig']].drop_duplicates()
        result      = all_origins.merge(result, on='_orig', how='left')

        na_count = df.groupby('_orig')['_adj'].apply(
            lambda x: x.isna().sum()
        ).reset_index()
        na_count.columns = ['_orig', '_na']
        result = result.merge(na_count, on='_orig', how='left')

        n_assigned = result['_dest'].notna().sum()
        feedback.pushInfo(_item('할당 격자', f'{n_assigned:,} / {len(result):,}'))
        if n_assigned < len(result):
            feedback.pushWarning(f'  도달 불가 격자 {len(result) - n_assigned:,}개 → 미할당 처리')
        feedback.setProgress(35)
        if feedback.isCanceled():
            return {}

        # ── [3단계] 중심지 정보 결합 ─────────────────────────────────
        feedback.pushInfo(_section('[3단계] 중심지 정보 결합'))

        center_dict = {}
        for feat in center_layer.getFeatures():
            center_dict[feat[f_center_gid]] = {
                'cent_id': feat[f_cent_id],
                'cent_nm': feat[f_cent_nm],
            }
        if not center_dict:
            raise QgsProcessingException('중심지 중심점 레이어에 피처가 없습니다.')
        feedback.pushInfo(_item('중심지 중심점', f'{len(center_dict):,}개'))

        result['cent_id'] = result['_dest'].map(
            lambda x: center_dict.get(x, {}).get('cent_id'))
        result['cent_nm'] = result['_dest'].map(
            lambda x: center_dict.get(x, {}).get('cent_nm'))

        n_unmatched = int((result['_dest'].notna() & result['cent_id'].isna()).sum())
        if n_unmatched:
            feedback.pushWarning(
                f'  중심지 레이어에서 찾지 못한 도착 ID {n_unmatched:,}건 '
                f'(격자 ID 필드 매칭을 확인하세요)'
            )

        result_dict = result.set_index('_orig').to_dict('index')
        feedback.setProgress(40)

        # ── [4단계] 바탕격자 조인 및 출력 ────────────────────────────
        feedback.pushInfo(_section('[4단계] 바탕격자 조인 및 출력'))

        # 기존 동명 필드는 교체 (재실행 호환)
        skip_idxs = {
            i for i, f in enumerate(grid_layer.fields())
            if f.name() in self._OUT_FIELDS
        }
        if skip_idxs:
            feedback.pushWarning(
                f'  바탕격자에 이미 있는 출력 필드 {len(skip_idxs)}개를 교체합니다.')

        out_fields = QgsFields()
        for i, f in enumerate(grid_layer.fields()):
            if i not in skip_idxs:
                out_fields.append(f)
        out_fields.append(QgsField('dest_id',    QVariant.String))
        out_fields.append(QgsField('total_cost', QVariant.Double))
        out_fields.append(QgsField('cost_adj',   QVariant.Double))
        out_fields.append(QgsField('na_count',   QVariant.Int))
        out_fields.append(QgsField('cent_id',    QVariant.String))
        out_fields.append(QgsField('cent_nm',    QVariant.String))

        sink, dest_id = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            out_fields, grid_layer.wkbType(), grid_layer.sourceCrs(),
        )

        cent_nms  = set()
        total     = grid_layer.featureCount()
        n_written = 0

        for i, feat in enumerate(grid_layer.getFeatures()):
            if feedback.isCanceled():
                return {}
            if i % 500 == 0:
                feedback.setProgress(40 + int(i / max(total, 1) * 55))

            row = result_dict.get(feat[f_grid_gid], {})

            new_feat = QgsFeature(out_fields)
            new_feat.setGeometry(feat.geometry())
            attrs = [v for j, v in enumerate(feat.attributes()) if j not in skip_idxs]
            attrs.extend([
                str(row['_dest']) if row.get('_dest') is not None and row['_dest'] == row['_dest'] else None,
                self._safe_float(row.get('_cost')),
                self._safe_float(row.get('_adj')),
                self._safe_int(row.get('_na')),
                str(row['cent_id']) if row.get('cent_id') is not None else None,
                str(row['cent_nm']) if row.get('cent_nm') is not None else None,
            ])
            new_feat.setAttributes(attrs)
            sink.addFeature(new_feat, QgsFeatureSink.FastInsert)
            n_written += 1

            nm = row.get('cent_nm')
            if nm is not None:
                cent_nms.add(str(nm))

        feedback.pushInfo(_item('출력 격자', f'{n_written:,}개'))
        feedback.pushInfo(_item('권역 수', f'{len(cent_nms)}개'))
        feedback.pushInfo(_bar())
        feedback.pushInfo(f'완료: 다음 도구 "4. 기초생활권 스무딩_update"에 이 출력을 입력하세요.')
        feedback.setProgress(100)

        self._dest_id  = dest_id
        self._cent_nms = sorted(cent_nms)
        return {self.OUTPUT: dest_id}

    # ── 후처리: cent_nm 기준 카테고리 심볼 적용 ───────────────────────── #

    def postProcessAlgorithm(
        self,
        context: QgsProcessingContext,
        feedback: QgsProcessingFeedback,
    ) -> dict:
        dest_id  = getattr(self, '_dest_id', None)
        cent_nms = getattr(self, '_cent_nms', [])
        if not dest_id:
            return {}

        layer = QgsProcessingUtils.mapLayerFromString(dest_id, context)
        if layer is None or not layer.isValid():
            return {}

        random.seed(42)
        categories = []
        for nm in cent_nms:
            symbol = QgsSymbol.defaultSymbol(layer.geometryType())
            symbol.setColor(QColor(
                random.randint(50, 220),
                random.randint(50, 220),
                random.randint(50, 220),
                210,
            ))
            symbol.symbolLayer(0).setStrokeStyle(Qt.NoPen)
            categories.append(QgsRendererCategory(nm, symbol, nm))

        # 미할당(NULL) 카테고리
        null_symbol = QgsSymbol.defaultSymbol(layer.geometryType())
        null_symbol.setColor(QColor(200, 200, 200, 150))
        null_symbol.symbolLayer(0).setStrokeStyle(Qt.NoPen)
        categories.append(QgsRendererCategory(None, null_symbol, '(미할당)'))

        layer.setRenderer(QgsCategorizedSymbolRenderer('cent_nm', categories))
        layer.triggerRepaint()

        if not QgsProject.instance().mapLayer(layer.id()):
            QgsProject.instance().addMapLayer(layer)

        return {}
