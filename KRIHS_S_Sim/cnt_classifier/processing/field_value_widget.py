"""상위중심지 유형 파라미터용 커스텀 위젯 래퍼.

Processing 표준 대화상자에서 부모 레이어와 필드 선택에 반응해
선택한 필드의 고유값을 체크 콤보박스 항목으로 동적 로드한다.

이 모듈은 GUI에서 위젯을 만들 때에만 임포트된다(파라미터 metadata의
dotted-path 문자열 참조). headless/qgis_process 환경에서는 임포트되지 않으므로
processing.gui / qgis.gui 의존성이 알고리즘 로드를 깨뜨리지 않는다.
"""

from processing.gui.wrappers import WidgetWrapper
from processing.tools import dataobjects

from qgis.PyQt.QtCore import QEvent
from qgis.gui import QgsCheckableComboBox
from qgis.core import QgsVectorLayer, QgsProcessingUtils

# 이전 Enum 기본값(광역중심지)과 동일한 초기 선택을 유지하기 위한 값
_DEFAULT_CHECK = "광역중심지"


class FieldValueWidgetWrapper(WidgetWrapper):
    """선택한 레이어·필드의 고유값을 다중 선택 콤보박스로 노출하는 래퍼."""

    def createWidget(self, layer_param=None, field_param=None, **kwargs):
        self._layer_param_name = layer_param
        self._field_param_name = field_param
        self._layer_wrapper = None
        self._field_wrapper = None

        self.combo = QgsCheckableComboBox()
        self.combo.setToolTip("선택한 필드의 고유값 중 상위중심지로 사용할 유형을 선택하세요.")
        self.combo.checkedItemsChanged.connect(
            lambda items: self.widgetValueHasChanged.emit(self)
        )
        # 부모 위젯 신호를 놓쳤더라도 드롭다운을 여는 순간 최신 값으로 재적재
        self.combo.installEventFilter(self)
        return self.combo

    def eventFilter(self, obj, event):
        if obj is getattr(self, "combo", None) and event.type() == QEvent.MouseButtonPress:
            self._refresh()
        return False

    # ── 부모 위젯 연결 ──────────────────────────────────────────────
    def postInitialize(self, wrappers):
        for w in wrappers:
            name = self._wrapper_param_name(w)
            if name == self._layer_param_name:
                self._layer_wrapper = w
                self._connect_changed(w)
            elif name == self._field_param_name:
                self._field_wrapper = w
                self._connect_changed(w)
        self._refresh()

    def _connect_changed(self, w):
        if hasattr(w, "widgetValueHasChanged"):
            try:
                w.widgetValueHasChanged.connect(lambda *a: self._refresh())
            except Exception:
                pass

    # ── 고유값 로드 ─────────────────────────────────────────────────
    def _refresh(self):
        prev = set(self.combo.checkedItems())
        layer = self._resolve_layer()
        field = self._wrapper_value(self._field_wrapper)

        self.combo.blockSignals(True)
        self.combo.clear()
        if layer is not None and field:
            # 레이어 C++ 객체가 이미 삭제됐으면(RuntimeError) 목록만 비우고 넘어간다
            try:
                idx = layer.fields().lookupField(str(field))
                if idx >= 0:
                    values = sorted(
                        {"" if v is None else str(v) for v in layer.uniqueValues(idx)}
                    )
                    self.combo.addItems(values)

                    to_check = [v for v in values if v in prev]
                    if not to_check and not prev and _DEFAULT_CHECK in values:
                        to_check = [_DEFAULT_CHECK]
                    if to_check:
                        self.combo.setCheckedItems(to_check)
            except RuntimeError:
                pass
        self.combo.blockSignals(False)
        self.widgetValueHasChanged.emit(self)

    def _resolve_layer(self):
        val = self._wrapper_value(self._layer_wrapper)
        if val is None:
            return None
        if isinstance(val, QgsVectorLayer):
            try:
                val.id()  # C++ 객체 생존 확인 — 삭제됐으면 RuntimeError
            except RuntimeError:
                return None
            return val
        source = val.source if hasattr(val, "source") else val
        try:
            # 컨텍스트를 멤버로 유지해야 한다 — 지역 변수면 함수 종료 시
            # 컨텍스트가 파괴되면서 임시 로드된 레이어도 함께 삭제된다
            self._context = dataobjects.createContext()
            layer = QgsProcessingUtils.mapLayerFromString(str(source), self._context)
        except Exception:
            layer = None
        if isinstance(layer, QgsVectorLayer) and layer.isValid():
            return layer
        return None

    # ── 버전 호환 헬퍼 (modern/old 래퍼 모두 지원) ──────────────────
    @staticmethod
    def _wrapper_param_name(w):
        if hasattr(w, "parameterDefinition"):
            try:
                return w.parameterDefinition().name()
            except Exception:
                pass
        if hasattr(w, "param"):
            try:
                return w.param.name()
            except Exception:
                pass
        return None

    @staticmethod
    def _wrapper_value(w):
        if w is None:
            return None
        for attr in ("parameterValue", "value"):
            if hasattr(w, attr):
                try:
                    return getattr(w, attr)()
                except Exception:
                    continue
        return None

    # ── 값 입출력 (modern/old 호출 경로 모두 대응) ──────────────────
    def value(self):
        return ",".join(self.combo.checkedItems())

    def parameterValue(self):
        return self.value()

    def setValue(self, value):
        if value is None:
            return
        if isinstance(value, str):
            items = [s.strip() for s in value.split(",") if s.strip()]
        else:
            items = [str(v).strip() for v in value if str(v).strip()]
        self.combo.setCheckedItems(items)

    def setWidgetValue(self, value, *args, **kwargs):
        self.setValue(value)
