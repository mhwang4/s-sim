"""중심지 및 위계 설정 알고리즘."""

from PyQt5.QtCore import Qt, QVariant
from PyQt5.QtGui import QColor
from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterExpression,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
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

_N_TYPES = 3
_DEFAULT_NAMES = ["생활중심지", "지역중심지", "광역중심지"]
# 기본식 = 가이드라인 표4(최소 주거밀도 150명/㎢·거점도시) + 표5(거점도시, 1·2차에만)
_DEFAULT_EXPRS = [
    '"rpop_den" >= 150 AND "rpop_sum" >= 150',
    '"rpop_den" >= 750 AND "rpop_sum" >= 5000 AND "wpop_den" >= 250 AND "wpop_sum" >= 2000',
    '"rpop_den" >= 4000 AND "rpop_sum" >= 100000 AND "wpop_den" >= 1750 AND "wpop_sum" >= 50000',
]
_DEFAULT_RANKS = [1, 2, 3]

# 위계 낮은 순(index 0) → 높은 순(index 2) 색상
_COLORS = [
    QColor("#33a02c"),  # 생활중심지
    QColor("#ff7f00"),  # 지역중심지
    QColor("#8c0000"),  # 광역중심지
]


class ClassifyCentersAlgorithm(QgsProcessingAlgorithm):
    """속성 추출 완료된 중심지 후보 레이어에 위계 분류 적용."""

    INPUT  = "INPUT"
    OUTPUT = "OUTPUT"

    # ── 메타데이터 ─────────────────────────────────────────────────────── #

    def name(self) -> str:
        return "classify_centers"

    def displayName(self) -> str:
        return "3. 중심지 및 위계 설정_update"

    def group(self) -> str:
        return "1. 중심지 분석"

    def groupId(self) -> str:
        return "center_analysis"

    def shortHelpString(self) -> str:
        return (
            "속성이 추출된 중심지 후보 레이어에 분류 기준을 적용하여 위계를 설정합니다.\n\n"
            "【Cascade 규칙】\n"
            "  위계값이 낮은 유형의 조건을 먼저 충족해야 다음 유형으로 진급합니다.\n"
            "  위계1 조건 통과              → 유형1\n"
            "  위계1 AND 위계2 조건 통과   → 유형2\n"
            "  위계1 AND 2 AND 3 조건 통과 → 유형3\n\n"
            "【이외 처리】\n"
            "  위계1 조건도 충족하지 못한 폴리곤은 최종 결과에서 삭제됩니다.\n\n"
            "입력: '2. 중심지 후보 속성 추출_update' 알고리즘의 출력 레이어\n"
            "출력: 위계 필드 cent_cls(값: 생활중심지·지역중심지·광역중심지)가 추가된 새 레이어\n"
            "      (입력 레이어를 수정하지 않음)\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "【참고 : UN DEGURBA 분류체계】\n"
            "  - Urban Center    : 인구밀도 최소 1,500명/km², 인구규모 최소 5만명\n"
            "  - Urban Cluster   : 인구밀도 최소 300명/km², 인구규모 최소 5천명\n"
            "  - Rural Grid Cells: 인구밀도 최소 300명/km²\n\n"
            "【참고 : 황명화 외(2025)】\n"
            "  - 생활중심지: 마을+거점시설 충족도 20% 이상\n"
            "                (20개 시설 중 4개 이상 접근 가능)\n"
            "  - 지역중심지: 거점시설 충족도 50% 이상\n"
            "                (10개 거점시설 중 5개 이상 접근 가능)"
        )

    def createInstance(self):
        return ClassifyCentersAlgorithm()

    # ── 파라미터 키 헬퍼 ───────────────────────────────────────────────── #

    @staticmethod
    def _k(n: int, suffix: str) -> str:
        return f"TYPE_{n}_{suffix}"

    # ── 파라미터 정의 ──────────────────────────────────────────────────── #

    def initAlgorithm(self, config=None) -> None:
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT, "중심지 후보(속성 포함) 레이어 ('2. 중심지 후보 속성 추출_update' 출력)",
            types=[QgsProcessing.TypeVectorPolygon],
        ))

        for n in range(1, _N_TYPES + 1):
            if _sep:
                self.addParameter(_sep(f"SEP_{n}", f"── 유형 {n} ──────────────────────────────"))

            self.addParameter(QgsProcessingParameterBoolean(
                self._k(n, "ENABLED"), f"유형 {n} 활성화",
                defaultValue=True,
            ))
            self.addParameter(QgsProcessingParameterString(
                self._k(n, "NAME"), f"유형 {n} 이름",
                defaultValue=_DEFAULT_NAMES[n - 1],
            ))
            self.addParameter(QgsProcessingParameterExpression(
                self._k(n, "EXPR"), f"유형 {n} 조건 (QGIS 표현식)",
                defaultValue=_DEFAULT_EXPRS[n - 1],
                parentLayerParameterName=self.INPUT,
                optional=True,
            ))
            self.addParameter(QgsProcessingParameterNumber(
                self._k(n, "RANK"), f"유형 {n} 위계값 (낮을수록 하위 등급)",
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=_DEFAULT_RANKS[n - 1],
                minValue=1,
            ))

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, "분류 결과",
            type=QgsProcessing.TypeVectorPolygon,
        ))

    # ── 실행 ───────────────────────────────────────────────────────────── #

    def processAlgorithm(
        self,
        parameters: dict,
        context: QgsProcessingContext,
        feedback: QgsProcessingFeedback,
    ) -> dict:
        input_layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)

        # ── 활성 유형 수집 및 정렬 ─────────────────────────────────────
        active_types = []
        for n in range(1, _N_TYPES + 1):
            if not self.parameterAsBoolean(parameters, self._k(n, "ENABLED"), context):
                continue
            name     = self.parameterAsString(parameters, self._k(n, "NAME"), context).strip()
            expr_str = self.parameterAsExpression(parameters, self._k(n, "EXPR"), context).strip()
            rank     = self.parameterAsInt(parameters, self._k(n, "RANK"), context)
            if not name or not expr_str:
                feedback.pushWarning(f"유형 {n}: 이름 또는 조건이 비어 있어 건너뜁니다.")
                continue
            active_types.append({"name": name, "expr_str": expr_str, "rank": rank})

        if not active_types:
            raise QgsProcessingException("활성화된 유형이 없습니다. 최소 1개를 설정하세요.")

        active_types.sort(key=lambda t: t["rank"])

        # ── 시작 요약 ──────────────────────────────────────────────────
        feedback.pushInfo("━" * 56)
        feedback.pushInfo("  중심지 및 위계 설정 시작")
        feedback.pushInfo("━" * 56)
        feedback.pushInfo(f"  입력 레이어: {input_layer.name()}")
        feedback.pushInfo(f"\n  활성 유형 ({len(active_types)}개) — 위계 오름차순:")
        for t in active_types:
            feedback.pushInfo(f"    위계{t['rank']}  [{t['name']}]")
            feedback.pushInfo(f"          조건: {t['expr_str']}")
        feedback.pushInfo(f"\n  Cascade: {' → '.join(t['name'] for t in active_types)}")
        feedback.pushInfo("  위계1 미충족 폴리곤: 출력에서 제외")

        # ── 출력 필드 구성 (기존 분류 필드 교체, 없으면 추가) ──────────
        # 신버전 출력(cent_cls)과 구버전 출력(분류) 모두 교체 대상
        skip_idxs = {
            idx for idx in (
                input_layer.fields().indexOf("cent_cls"),
                input_layer.fields().indexOf("tier"),
                input_layer.fields().indexOf("분류"),
            ) if idx >= 0
        }
        out_fields = QgsFields()
        for i, field in enumerate(input_layer.fields()):
            if i not in skip_idxs:
                out_fields.append(field)
        out_fields.append(QgsField("cent_cls", QVariant.String, len=50))
        out_fields.append(QgsField("tier", QVariant.String, len=12))   # 1차중심지=상위 — 원본 cent_cls와 병기
        # 유형명 → 차수 중심지: active_types는 위계 오름차순(하위→상위) → 상위일수록 1차중심지
        tier_map = {t["name"]: f"{len(active_types) - i}차중심지"
                    for i, t in enumerate(active_types)}

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            out_fields, input_layer.wkbType(), input_layer.crs(),
        )

        # ── 표현식 컴파일 ──────────────────────────────────────────────
        feedback.pushInfo("\n" + "─" * 56)
        feedback.pushInfo("  [1단계] 표현식 컴파일")
        feedback.pushInfo("─" * 56)
        ctx = QgsExpressionContext()
        ctx.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(input_layer))
        ctx.setFields(input_layer.fields())

        compiled = []
        for t in active_types:
            expr = QgsExpression(t["expr_str"])
            if expr.hasParserError():
                raise QgsProcessingException(
                    f"표현식 파싱 오류 [{t['name']}]: {expr.parserErrorString()}"
                )
            expr.prepare(ctx)
            compiled.append({"name": t["name"], "expr": expr})
            feedback.pushInfo(f"  ✔ [{t['name']}]  {t['expr_str']}")

        # ── 피처별 cascade 평가 ────────────────────────────────────────
        feedback.pushInfo("\n" + "─" * 56)
        feedback.pushInfo("  [2단계] 피처별 cascade 평가")
        feedback.pushInfo("─" * 56)

        counts = {t["name"]: 0 for t in active_types}
        counts["이외(제외)"] = 0

        total    = input_layer.featureCount()
        log_step = max(1, total // 10)

        for i, feat in enumerate(input_layer.getFeatures()):
            if feedback.isCanceled():
                feedback.pushWarning("취소되었습니다.")
                return {}

            ctx.setFeature(feat)
            assigned = None

            for entry in compiled:
                result = entry["expr"].evaluate(ctx)
                if entry["expr"].hasEvalError():
                    feedback.pushWarning(
                        f"  피처 {feat.id()} 평가 오류 [{entry['name']}]: "
                        f"{entry['expr'].evalErrorString()}"
                    )
                    break
                # 첫 번째 피처에 대해 평가값 진단 로그
                if i == 0:
                    feedback.pushInfo(
                        f"  [진단] 피처#{feat.id()} [{entry['name']}] "
                        f"결과={result!r}  bool={bool(result)}"
                    )
                if bool(result):
                    assigned = entry["name"]
                else:
                    break

            if assigned is None:
                counts["이외(제외)"] += 1
            else:
                # 입력 속성 복사 (기존 분류 필드 제외) + 새 분류값
                in_attrs = feat.attributes()
                out_attrs = [v for j, v in enumerate(in_attrs) if j not in skip_idxs]
                out_attrs.append(assigned)
                out_attrs.append(tier_map[assigned])   # tier(차수)

                out_feat = QgsFeature(out_fields)
                out_feat.setGeometry(feat.geometry())
                out_feat.setAttributes(out_attrs)
                sink.addFeature(out_feat, QgsFeatureSink.FastInsert)
                counts[assigned] += 1

            if i % log_step == 0 and total > 0:
                feedback.setProgress(int(i / total * 90))
                feedback.pushInfo(f"  {i:,} / {total:,} 처리 중...")

        # ── 결과 요약 ──────────────────────────────────────────────────
        feedback.setProgress(100)
        feedback.pushInfo("\n" + "━" * 56)
        feedback.pushInfo("  분류 결과 요약")
        feedback.pushInfo("━" * 56)
        for name, cnt in counts.items():
            bar = "▓" * min(cnt, 30)
            feedback.pushInfo(f"  {name:<16}: {cnt:>5,}개  {bar}")
        feedback.pushInfo(f"  {'합계':<16}: {sum(counts.values()):>5,}개")
        feedback.pushInfo("\n  완료")

        # postProcessAlgorithm에서 심볼 적용을 위해 저장
        self._dest_id     = dest_id
        self._active_types = active_types

        return {self.OUTPUT: dest_id}

    # ── 후처리: 심볼 적용 및 지도 추가 ───────────────────────────────── #

    def postProcessAlgorithm(
        self,
        context: QgsProcessingContext,
        feedback: QgsProcessingFeedback,
    ) -> dict:
        dest_id      = getattr(self, "_dest_id", None)
        active_types = getattr(self, "_active_types", [])
        if not dest_id:
            return {}

        layer = QgsProcessingUtils.mapLayerFromString(dest_id, context)
        if layer is None or not layer.isValid():
            return {}

        # 유형 수에 따라 색상 배정 (낮은 위계 → 밝음, 높은 위계 → 어두움)
        n = len(active_types)
        categories = []
        for i, t in enumerate(active_types):
            color = _COLORS[min(i, len(_COLORS) - 1)]
            symbol = QgsSymbol.defaultSymbol(layer.geometryType())
            symbol.setColor(color)
            symbol.symbolLayer(0).setStrokeStyle(Qt.NoPen)
            tval = f"{n - i}차중심지"   # tier 값으로 분류 (cent_cls 원본은 병기 유지)
            categories.append(QgsRendererCategory(tval, symbol, tval))

        layer.setRenderer(QgsCategorizedSymbolRenderer("tier", categories))
        layer.triggerRepaint()

        # 프로젝트에 추가 (Processing 프레임워크가 이미 추가했을 수 있으므로 중복 확인)
        if not QgsProject.instance().mapLayer(layer.id()):
            QgsProject.instance().addMapLayer(layer)

        return {}
