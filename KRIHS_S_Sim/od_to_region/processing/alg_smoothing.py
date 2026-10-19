import random
from collections import Counter, defaultdict

from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
    QgsProcessingUtils,
    QgsProject,
    QgsRendererCategory,
    QgsSpatialIndex,
    QgsSymbol,
    QgsWkbTypes,
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


class RegionSmoothing(QgsProcessingAlgorithm):

    INPUT_LAYER     = 'INPUT_LAYER'
    FIELD_CENT_ID   = 'FIELD_CENT_ID'
    FIELD_CENT_NM   = 'FIELD_CENT_NM'
    N_ITERATIONS    = 'N_ITERATIONS'
    RANDOM_SEED     = 'RANDOM_SEED'
    OUTPUT          = 'OUTPUT'
    OUTPUT_DISSOLVE = 'OUTPUT_DISSOLVE'

    # ------------------------------------------------------------------ #
    # 메타데이터                                                           #
    # ------------------------------------------------------------------ #

    def createInstance(self):
        return RegionSmoothing()

    def name(self):
        return 'region_smoothing'

    def displayName(self):
        return '4. 기초생활권 스무딩_update'

    def group(self):
        return '2. 중심지-생활권 경계 도출'

    def groupId(self):
        return 'region_boundary'

    def shortHelpString(self):
        return (
            "'3. 거리 행렬 기반 기초생활권 할당_update' 결과의 들쭉날쭉한 권역 경계를 "
            "이웃 격자 최빈값 투표로 다듬습니다.\n\n"
            "【입력】\n"
            "  · 격자 권역할당 레이어 : 앞 도구의 출력 (cent_id·cent_nm 필드)\n\n"
            "【처리 단계】\n"
            "  1) 격자 간 이웃(변 접촉) 관계 계산\n"
            "  2) 반복 스무딩 — 각 격자를 이웃 다수 권역으로 재배정,\n"
            "     동률이면 랜덤 선택(시드 고정으로 재현 가능), 변화 없으면 조기 종료\n"
            "  3) 최종 권역에 맞춰 cent_id·cent_nm 값 갱신 후 출력\n\n"
            "【출력 필드】\n"
            "  · 입력과 동일한 구조 — cent_id·cent_nm 값만 스무딩 결과로 갱신\n"
            "    (중간 반복 열은 출력하지 않음. 원본 배정은 앞 도구 출력에 보존)\n"
            "  · 심볼: 입력 레이어의 권역 색상을 그대로 이어받아 cent_nm 기준 적용\n\n"
            "【파라미터】\n"
            "  · 반복 횟수 : 클수록 경계가 매끈해지나 작은 권역이 사라질 수 있음 (기본 8)\n"
            "  · 랜덤 시드 : 같은 시드 → 같은 결과 (기본 4226)\n\n"
            "이전 도구: 3. 거리 행렬 기반 기초생활권 할당_update  /  "
            "다음 도구: 7. 하위생활권-상위중심지 이동량 행렬 산출_update · 8. 매력도 기반 상위생활권 도출_update"
        )

    # ------------------------------------------------------------------ #
    # 파라미터 정의                                                        #
    # ------------------------------------------------------------------ #

    def initAlgorithm(self, config=None):
        if _sep: self.addParameter(_sep("SEP_INPUT", "입력 데이터"))

        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT_LAYER,
            "격자 권역할당 레이어 ('3. 거리 행렬 기반 기초생활권 할당_update' 출력)",
            [QgsProcessing.TypeVectorPolygon],
        ))
        self.addParameter(QgsProcessingParameterField(
            self.FIELD_CENT_ID, '   ↳ 권역 ID 필드 (기본: cent_id)',
            defaultValue='cent_id',
            parentLayerParameterName=self.INPUT_LAYER,
            type=QgsProcessingParameterField.Any,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.FIELD_CENT_NM, '   ↳ 권역 이름 필드 — 심볼 기준 (기본: cent_nm)',
            defaultValue='cent_nm',
            parentLayerParameterName=self.INPUT_LAYER,
            type=QgsProcessingParameterField.Any,
        ))

        if _sep: self.addParameter(_sep("SEP_PARAM", "기준 설정"))

        self.addParameter(QgsProcessingParameterNumber(
            self.N_ITERATIONS, '반복 횟수 (클수록 경계 매끈, 기본 8)',
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=8, minValue=1, maxValue=50,
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.RANDOM_SEED, '랜덤 시드 (같은 시드 → 같은 결과)',
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=4226, minValue=0,
        ))

        if _sep: self.addParameter(_sep("SEP_OUTPUT", "출력"))

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, '스무딩된 권역 결과 (격자)',
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_DISSOLVE, '스무딩된 권역 결과 (권역 폴리곤·합역)',
            type=QgsProcessing.TypeVectorPolygon,
        ))

    # ------------------------------------------------------------------ #
    # 실행                                                                 #
    # ------------------------------------------------------------------ #

    def processAlgorithm(self, parameters, context, feedback):
        input_layer  = self.parameterAsVectorLayer(parameters, self.INPUT_LAYER, context)
        f_cent_id    = self.parameterAsString(parameters, self.FIELD_CENT_ID, context)
        f_cent_nm    = self.parameterAsString(parameters, self.FIELD_CENT_NM, context)
        n_iterations = self.parameterAsInt(parameters, self.N_ITERATIONS, context)
        random_seed  = self.parameterAsInt(parameters, self.RANDOM_SEED, context)

        rng = random.Random(random_seed)

        # ── [1단계] 로드 및 이웃 관계 계산 ───────────────────────────
        feedback.pushInfo(_section('[1단계] 격자 로드 및 이웃 관계 계산'))

        geoms, assign, id_to_nm = {}, {}, {}
        for feat in input_layer.getFeatures():
            fid = feat.id()
            geoms[fid] = feat.geometry()
            val = feat[f_cent_id]
            assign[fid] = str(val) if val is not None and str(val) not in ('NULL', 'None', '') else None
            nm = feat[f_cent_nm]
            if assign[fid] is not None and assign[fid] not in id_to_nm and nm is not None:
                id_to_nm[assign[fid]] = str(nm)

        total = len(geoms)
        if total == 0:
            raise QgsProcessingException('입력 레이어에 피처가 없습니다.')
        n_null = sum(1 for v in assign.values() if v is None)
        feedback.pushInfo(_item('격자 수', f'{total:,}개 (미할당 {n_null:,}개)'))
        feedback.pushInfo(_item('권역 수', f'{len(id_to_nm)}개'))

        index = QgsSpatialIndex(input_layer.getFeatures())
        neighbors = {}
        for j, (fid, geom) in enumerate(geoms.items()):
            if feedback.isCanceled():
                return {}
            cands = index.intersects(geom.boundingBox())
            nbrs = [c for c in cands if c != fid and geoms[c].touches(geom)]
            neighbors[fid] = nbrs + [fid]   # 자기 자신 포함 (자기 값에 가중)
            if j % 2000 == 0 and j > 0:
                feedback.pushInfo(f'  이웃 계산 {j:,} / {total:,}...')
                feedback.setProgress(int(j / total * 15))

        feedback.setProgress(15)

        # ── [2단계] 반복 스무딩 ──────────────────────────────────────
        feedback.pushInfo(_section('[2단계] 반복 스무딩 (이웃 최빈값 투표)'))
        feedback.pushInfo(_item('반복 횟수', str(n_iterations)))
        feedback.pushInfo(_item('랜덤 시드', str(random_seed)))

        fids = list(geoms.keys())
        # 원래 미할당(OD에 없던 섬·도로망 밖 격자)은 스무딩 대상에서 제외 — 채우지 않음
        unassigned0 = {fid for fid in fids if assign[fid] is None}
        if unassigned0:
            feedback.pushInfo(_item('스무딩 제외(미할당 유지)', f'{len(unassigned0):,}개'))
        for i in range(n_iterations):
            if feedback.isCanceled():
                return {}

            new_assign = dict(assign)
            rng.shuffle(fids)
            for fid in fids:
                if fid in unassigned0:   # 미할당 격자는 이웃값으로 채우지 않음(원래 상태 유지)
                    continue
                values = [assign[n] for n in neighbors[fid] if assign[n] is not None]
                if not values:
                    continue
                count = Counter(values)
                max_count = count.most_common(1)[0][1]
                candidates = sorted(k for k, v in count.items() if v == max_count)
                new_assign[fid] = rng.choice(candidates)

            changed = sum(1 for fid in geoms if assign[fid] != new_assign[fid])
            assign = new_assign
            feedback.pushInfo(
                f'  [{i + 1}/{n_iterations}회차] 변경 {changed:,} / {total:,} '
                f'({changed / total:.2%})'
            )
            feedback.setProgress(15 + int((i + 1) / n_iterations * 70))

            if changed == 0:
                feedback.pushInfo('  → 변화 없음, 조기 종료')
                break

        # ── [3단계] cent_id·cent_nm 갱신 및 출력 ─────────────────────
        feedback.pushInfo(_section('[3단계] cent_id·cent_nm 갱신 및 출력'))

        idx_cent_id = input_layer.fields().indexOf(f_cent_id)
        idx_cent_nm = input_layer.fields().indexOf(f_cent_nm)

        sink, dest_id = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            input_layer.fields(), input_layer.wkbType(), input_layer.sourceCrs(),
        )

        n_written = 0
        dissolve_geoms = defaultdict(list)   # cent_id → [격자 geometry]
        for feat in input_layer.getFeatures():
            if feedback.isCanceled():
                return {}
            new_id = assign.get(feat.id())
            attrs = feat.attributes()
            attrs[idx_cent_id] = new_id
            attrs[idx_cent_nm] = id_to_nm.get(new_id) if new_id is not None else None

            out_feat = QgsFeature(input_layer.fields())
            out_feat.setGeometry(feat.geometry())
            out_feat.setAttributes(attrs)
            sink.addFeature(out_feat, QgsFeatureSink.FastInsert)
            n_written += 1
            if new_id is not None:
                dissolve_geoms[new_id].append(feat.geometry())

        # ── 권역 폴리곤(합역) 출력 — 상위생활권 산출물과 형태 통일 ──────
        # (격자는 상위생활권의 입력으로 그대로 쓰이고, 이 폴리곤은 표시·비교용)
        dissolve_fields = QgsFields()
        dissolve_fields.append(QgsField(f_cent_id, QVariant.String, len=100))
        dissolve_fields.append(QgsField(f_cent_nm, QVariant.String, len=200))
        dissolve_fields.append(QgsField('n_grid', QVariant.Int))
        dsink, dissolve_id = self.parameterAsSink(
            parameters, self.OUTPUT_DISSOLVE, context,
            dissolve_fields, QgsWkbTypes.MultiPolygon, input_layer.sourceCrs(),
        )
        for cid in sorted(dissolve_geoms):
            if feedback.isCanceled():
                return {}
            merged = QgsGeometry.unaryUnion(dissolve_geoms[cid])
            if merged is None or merged.isNull():
                continue
            df = QgsFeature(dissolve_fields)
            df.setGeometry(merged)
            df.setAttributes([cid, id_to_nm.get(cid), len(dissolve_geoms[cid])])
            dsink.addFeature(df, QgsFeatureSink.FastInsert)

        n_regions = len({v for v in assign.values() if v is not None})
        feedback.pushInfo(_item('출력 격자', f'{n_written:,}개'))
        feedback.pushInfo(_item('최종 권역 수', f'{n_regions}개 (권역 폴리곤 합역 완료)'))
        feedback.pushInfo(_bar())
        feedback.pushInfo('완료: 이동량 행렬(7)·매력도 상위생활권(8)의 하위생활권 입력으로 사용하세요.')
        feedback.setProgress(100)

        self._dissolve_id = dissolve_id
        self._dest_id     = dest_id
        self._f_cent_nm   = f_cent_nm
        self._input_layer = input_layer
        self._seed        = random_seed
        self._final_nms   = sorted({
            id_to_nm.get(v) for v in assign.values()
            if v is not None and id_to_nm.get(v) is not None
        })
        return {self.OUTPUT: dest_id, self.OUTPUT_DISSOLVE: dissolve_id}

    # ── 후처리: 입력 심볼 색상 계승 + cent_nm 기준 적용 ───────────────── #

    def postProcessAlgorithm(
        self,
        context: QgsProcessingContext,
        feedback: QgsProcessingFeedback,
    ) -> dict:
        dest_id = getattr(self, '_dest_id', None)
        if not dest_id:
            return {}

        layer = QgsProcessingUtils.mapLayerFromString(dest_id, context)
        if layer is None or not layer.isValid():
            return {}

        f_cent_nm   = getattr(self, '_f_cent_nm', 'cent_nm')
        input_layer = getattr(self, '_input_layer', None)
        final_nms   = getattr(self, '_final_nms', [])

        # 입력 레이어가 분류형 심볼이면 색상 계승
        color_map = {}
        null_symbol = None
        if input_layer is not None:
            src = input_layer.renderer()
            if isinstance(src, QgsCategorizedSymbolRenderer):
                for cat in src.categories():
                    sym = cat.symbol().clone()
                    sym.symbolLayer(0).setStrokeStyle(Qt.NoPen)
                    if cat.value() is None or cat.value() == '':
                        null_symbol = sym
                    else:
                        color_map[cat.value()] = sym

        rng = random.Random(getattr(self, '_seed', 4226))
        categories = []
        for nm in final_nms:
            if nm in color_map:
                symbol = color_map[nm]
            else:
                symbol = QgsSymbol.defaultSymbol(layer.geometryType())
                symbol.setColor(QColor(
                    rng.randint(50, 220), rng.randint(50, 220), rng.randint(50, 220), 210))
                symbol.symbolLayer(0).setStrokeStyle(Qt.NoPen)
            categories.append(QgsRendererCategory(nm, symbol, nm))

        if null_symbol is None:
            null_symbol = QgsSymbol.defaultSymbol(layer.geometryType())
            null_symbol.setColor(QColor(200, 200, 200, 150))
            null_symbol.symbolLayer(0).setStrokeStyle(Qt.NoPen)
        categories.append(QgsRendererCategory(None, null_symbol, '(미할당)'))

        layer.setRenderer(QgsCategorizedSymbolRenderer(f_cent_nm, categories))
        layer.triggerRepaint()

        if not QgsProject.instance().mapLayer(layer.id()):
            QgsProject.instance().addMapLayer(layer)

        return {}
