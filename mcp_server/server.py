# -*- coding: utf-8 -*-
"""KRIHS S-Sim MCP 서버 — 헤드리스 QGIS로 S-Sim 플러그인 알고리즘 17종과 지역·KPI·가이드 기능을 노출.

실행(수동 시험):
    run_server.bat --selftest
MCP 클라이언트 등록은 설치_mcp.bat / README.md 참조.

위치 규약 (배포 묶음 S-Sim\\ 기준):
  - 플러그인:   S-Sim\\KRIHS_S_Sim  (없으면 QGIS 프로필의 설치본)
  - 데이터팩:   S-Sim\\S-Sim_데이터팩  (환경변수 S_SIM_DATAPACK 이 있으면 그것)
  - 결과 저장:  S-Sim\\mcp_server\\분석결과\\S-Sim_분석결과(지역명)\\...  (환경변수 S_SIM_RESULTS 가 있으면 그 아래)
  - 실행 이력:  결과 저장 위치의 MCP_실행결과\\run_log.jsonl (작업 기록은 MCP_실행결과\\작업기록\\)
결과 폴더 구성은 플러그인과 같다(장래인구 격자 배분 및 중심지 전망 / 중심지 및 생활권 분석 / KPI 지표 계산).
"""
import concurrent.futures
import contextlib
import functools
import gc
import glob
import json
import os
import queue
import re
import sys
import tempfile
import time
import traceback
from datetime import datetime

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

# MCP stdio 프로토콜은 stdout을 쓰므로, QGIS 초기화·알고리즘 로그는 stderr로 보낸다.
_protocol_stdout = sys.stdout
sys.stdout = sys.stderr

from qgis.core import (  # noqa: E402
    Qgis,
    QgsApplication,
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsProcessingParameterDefinition,
    QgsVectorLayer,
)

_qgis_prefix = os.path.dirname(os.path.dirname(os.path.dirname(
    __import__('qgis').__file__)))
QgsApplication.setPrefixPath(_qgis_prefix, True)
_app = QgsApplication([], False)
_app.initQgis()

sys.path.insert(0, os.path.join(_qgis_prefix, 'python', 'plugins'))
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402
Processing.initialize()

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.dirname(SERVER_DIR)                      # 배포 묶음 S-Sim\
GUIDE_DIR = os.path.join(SERVER_DIR, 'guide')
QGIS_PLUGIN_DIR = os.path.join(os.environ.get('APPDATA', ''), 'QGIS', 'QGIS3',
                               'profiles', 'default', 'python', 'plugins')
PLUGIN_NAME = 'KRIHS_S_Sim'
PROVIDER_ID = 'krihs_ssim'


def _plugin_parent():
    """플러그인 위치 — 배포 묶음 안 → QGIS 프로필 설치본 순."""
    for parent in (PKG_DIR, QGIS_PLUGIN_DIR):
        if os.path.isfile(os.path.join(parent, PLUGIN_NAME, 'metadata.txt')):
            return parent
    return None


PLUGIN_PARENT = _plugin_parent()
if PLUGIN_PARENT is None:
    raise SystemExit(f'[S-Sim MCP] 플러그인 {PLUGIN_NAME} 을 찾지 못했습니다. '
                     f'배포 묶음의 설치.bat 을 먼저 실행하세요.')
PLUGIN_DIR = os.path.join(PLUGIN_PARENT, PLUGIN_NAME)
sys.path.insert(0, PLUGIN_PARENT)
from KRIHS_S_Sim.provider import PopAllocProvider  # noqa: E402
_providers = [PopAllocProvider()]  # 전역 참조 유지 — GC되면 등록된 알고리즘이 무효화됨
QgsApplication.processingRegistry().addProvider(_providers[0])

DATAPACK_DIR = os.environ.get('S_SIM_DATAPACK') or os.path.join(PKG_DIR, 'S-Sim_데이터팩')
# MCP가 만드는 결과는 mcp_server\분석결과\ 한 곳에 모은다(처음 실행할 때 생김)
RESULTS_PARENT = os.environ.get('S_SIM_RESULTS') or os.path.join(SERVER_DIR, '분석결과')

# 결과 폴더 구성 — 플러그인(dialog.py)의 OUT_* 상수와 같다
OUT_FUT = '장래인구 격자 배분 및 중심지 전망'
OUT_SP = '중심지 및 생활권 분석'
OUT_SP_CTR = os.path.join(OUT_SP, '중심지 분석')
OUT_SP_ZONE = os.path.join(OUT_SP, '생활권 분석')
OUT_KPI = 'KPI 지표 계산'
SCEN_DIR = os.path.join(OUT_FUT, '거주인구 배분', '시나리오별_장래인구')

# ---- MCP 서버 객체 (mcp 패키지 판에 따라 이름이 다름) ----
try:
    from mcp.server.mcpserver import Context, MCPServer  # noqa: E402
    mcp = MCPServer('krihs-s-sim')
    _MCP_ERROR = None
except ImportError:
    try:
        from mcp.server.fastmcp import Context, FastMCP  # noqa: E402
        mcp = FastMCP('krihs-s-sim')
        _MCP_ERROR = None
    except ImportError as e:  # mcp 미설치 — 자체 시험(--selftest)만 가능
        _MCP_ERROR = f'{type(e).__name__}: {e}'
        Context = object

        class _NoMCP:
            def tool(self, *a, **k):
                return lambda f: f

            def run(self):
                raise SystemExit('[S-Sim MCP] mcp 패키지가 없습니다. 설치_mcp.bat 을 실행하세요.')
        mcp = _NoMCP()

# mcp 패키지는 ToolError 가 아닌 예외의 메시지를 가리고 'Error executing tool <이름>' 한 줄만 돌려준다.
# 그래서 tool 안의 모든 예외를 ToolError 로 바꿔 원인 메시지가 Claude 에게 그대로 가게 한다.
try:
    from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
except ImportError:
    try:
        from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402
    except ImportError:
        class ToolError(Exception):
            pass


# QGIS 작업 전용 스레드 — mcp 패키지는 동기 tool을 anyio 워커 스레드에서 돌리는데, 이 스레드는 쉬면
# 사라지고 다음 호출은 새 스레드가 받는다. QGIS 타원체 거리·면적 계산(QgsDistanceArea)은 처음 계산한
# 스레드가 사라진 뒤 다른 스레드에서 NaN을 내므로(도로 그래프 링크 길이 전부 NaN → OD가 자기 격자만 남음),
# QGIS를 쓰는 tool은 서버가 꺼질 때까지 유지되는 이 스레드 하나에서만 실행한다.
_QGIS_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='qgis')
_PROGRESS = queue.Queue()   # (ctx, 진행률, 메시지) — QGIS 스레드가 넣고 tool 호출 스레드가 클라이언트로 보냄


def _relay_progress():
    while True:
        try:
            ctx, progress, msg = _PROGRESS.get_nowait()
        except queue.Empty:
            return
        try:  # tool 호출 스레드(anyio 워커)에서 이벤트루프에 위임
            anyio.from_thread.run(ctx.report_progress, progress, 100.0, msg)
        except Exception:
            pass


def _on_qgis_thread(fn, *args, **kwargs):
    fut = _QGIS_POOL.submit(fn, *args, **kwargs)
    while True:
        try:
            result = fut.result(timeout=0.5)
            break
        except concurrent.futures.TimeoutError:
            _relay_progress()
    _relay_progress()
    return result


def _tool(qgis=False):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                if qgis:
                    return _on_qgis_thread(fn, *args, **kwargs)
                return fn(*args, **kwargs)
            except ToolError:
                raise
            except (ValueError, FileNotFoundError, RuntimeError) as e:   # 예상한 실패 — 안내 메시지 그대로
                raise ToolError(str(e)) from e
            except Exception as e:                                        # 예상 못 한 오류 — 종류와 위치까지
                tb = traceback.format_exception(type(e), e, e.__traceback__)
                raise ToolError(f'{type(e).__name__}: {e}\n' + ''.join(tb[-3:]).rstrip()) from e
        return mcp.tool()(wrapper)
    return deco


try:
    import anyio  # noqa: E402
except ImportError:
    anyio = None

# 파일 경로를 받는 입력/출력 파라미터 타입 (QgsProcessingParameter*.type() 값)
_INPUT_TYPES = {'vector', 'source', 'raster', 'file', 'layer', 'multilayer'}
_VECTOR_TYPES = {'vector', 'source'}
_DEST_TYPES = {'sink', 'vectorDestination', 'rasterDestination',
               'fileDestination', 'folderDestination'}
_VECTOR_EXT = ('.gpkg', '.shp', '.geojson')


class _Feedback(QgsProcessingFeedback):
    """알고리즘 로그 수집 + 진행률을 MCP 클라이언트로 중계."""

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.log = []
        self._last_step = -1
        self.progressChanged.connect(self._on_progress)

    def _notify(self, progress):
        if self.ctx is None or anyio is None:
            return
        msg = self.log[-1] if self.log else None
        _PROGRESS.put((self.ctx, float(progress), msg))   # QGIS 스레드 → tool 호출 스레드가 전달(_relay_progress)

    def _on_progress(self, p):
        step = int(p // 10)  # 10% 단위로만 전송
        if step != self._last_step:
            self._last_step = step
            self._notify(p)

    def _push(self, kind, text):
        self.log.append(f'[{kind}] {text}')

    def warnings(self):
        """경고 줄 전부 (개수 제한 없음) — 결과 해석에 영향을 줄 수 있어 진행 로그와 따로 돌려준다."""
        return [s[len('[warning] '):] for s in self.log if s.startswith('[warning] ')]

    def pushInfo(self, text):
        self._push('info', text)
        super().pushInfo(text)

    def pushWarning(self, text):
        self._push('warning', text)
        super().pushWarning(text)

    def reportError(self, text, fatalError=False):
        self._push('error', text)
        super().reportError(text, fatalError)


# ---------------------------------------------------------------- 지역(데이터팩)
def _regions():
    """데이터팩 지역\\*\\region.json → {지역명: (지역 폴더, json)}."""
    out = {}
    for js_path in sorted(glob.glob(os.path.join(DATAPACK_DIR, '지역', '*', 'region.json'))):
        try:
            with open(js_path, encoding='utf-8') as f:
                js = json.load(f)
        except (OSError, ValueError):
            continue
        folder = os.path.dirname(js_path)
        out[js.get('name') or os.path.basename(folder)] = (folder, js)
    return out


def _region(name: str):
    regs = _regions()
    if not regs:
        raise ValueError(f'데이터팩에 지역이 없습니다: {DATAPACK_DIR}\n'
                         f'- 데이터팩 폴더를 배포 묶음에 두거나 환경변수 S_SIM_DATAPACK 으로 지정하세요.')
    if not name:
        if len(regs) == 1:
            return next(iter(regs.items()))
        raise ValueError('지역을 지정하세요: ' + ', '.join(regs))
    if name in regs:
        return name, regs[name]
    hits = [k for k in regs if name in k]
    if len(hits) == 1:
        return hits[0], regs[hits[0]]
    raise ValueError(f'지역 없음: {name} — 데이터팩 지역: ' + ', '.join(regs))


def _region_paths(folder: str, js: dict) -> dict:
    """region.json의 자료 경로를 절대경로로 (gpkg 레이어는 '경로|layername=…')."""
    data, user = js.get('data', {}), js.get('user_inputs', {})

    def p(rel, layer=None):
        if not rel:
            return None
        ap = os.path.join(folder, rel.replace('/', os.sep))
        return ap + (f'|layername={layer}' if layer else '')
    b = data.get('boundary', {})
    g = data.get('grid', {})
    return {
        'region_boundary': p(b.get('path'), b.get('region_layer')),
        'sigungu': p(b.get('path'), b.get('sgg_layer')),
        'emd': p(b.get('path'), b.get('emd_layer')),
        'grid': p(g.get('path'), g.get('layer')),
        'infra_500m': p(data.get('infra_500m', {}).get('path'), data.get('infra_500m', {}).get('layer')),
        'infra_1km': p(data.get('infra_1km', {}).get('path')),
        'resid_projection': p(data.get('resid_proj', {}).get('path')),
        'pop15_projection': p(data.get('pop15_proj', {}).get('path')),
        'employment_rate': p(data.get('emp_rate', {}).get('path')),
        'work_observed': p(data.get('work_obs', {}).get('path')),
        'dev_projects': p(data.get('dev_projects', {}).get('path')),
        'facility_neighborhood_dir': p(data.get('fac_neigh_dir')),
        'facility_hub_dir': p(data.get('fac_hub_dir')),
        'carbon': p(data.get('carbon', {}).get('path')),
        'road_network': p(user.get('network', {}).get('path')),
        'trip_matrix': p(user.get('trip', {}).get('path')),
    }


def _result_root(name: str, out_dir: str = '') -> str:
    return os.path.join(out_dir or RESULTS_PARENT, f'S-Sim_분석결과({name})')


def _exists(path):
    return bool(path) and os.path.exists(path.split('|')[0])


# ---------------------------------------------------------------- 입출력 보조
def _alg(alg_id: str):
    full = alg_id if ':' in alg_id else f'{PROVIDER_ID}:{alg_id}'
    alg = QgsApplication.processingRegistry().algorithmById(full)
    if alg is None:
        raise ValueError(f'알고리즘 없음: {alg_id} — list_algorithms 로 id를 확인하세요.')
    return alg


def _search_roots():
    roots = [DATAPACK_DIR]
    roots += glob.glob(os.path.join(RESULTS_PARENT, 'S-Sim_분석결과(*)'))
    return [r for r in roots if os.path.isdir(r)]


def _resolve_input(value: str) -> str:
    """입력 경로 해석: 절대경로 그대로, 파일명·상대경로는 데이터팩과 결과 폴더에서 탐색."""
    path, _, layer = value.partition('|')
    if os.path.isabs(path):
        if os.path.exists(path):
            return value
    else:
        base = os.path.basename(path)
        for root in _search_roots():
            cand = os.path.join(root, path)
            if os.path.exists(cand):
                return cand + ('|' + layer if layer else '')
            for dirpath, _dirs, files in os.walk(root):
                if base in files:
                    return os.path.join(dirpath, base) + ('|' + layer if layer else '')
    raise FileNotFoundError(
        f'입력 데이터 없음: {value}\n'
        f'- 절대경로로 지정하거나, 데이터팩·결과 폴더 안의 파일명을 쓰세요.\n'
        f'- 지역별 입력 파일 경로는 list_regions 로 확인할 수 있습니다.')


def _check_vector(path: str) -> None:
    """벡터 입력 사전 검사: 열리는지, 투영좌표계인지 (알고리즘이 m 단위 버퍼·거리 사용)."""
    layer = QgsVectorLayer(path, 'chk', 'ogr')
    if not layer.isValid():
        raise ValueError(f'파일을 열 수 없음(손상 또는 미지원 형식): {path}')
    crs = layer.crs()
    if crs.isValid() and crs.isGeographic():
        raise ValueError(
            f'좌표계 오류: {os.path.basename(path)} 가 위경도 좌표계({crs.authid()})입니다.\n'
            f'- 투영좌표계(EPSG:5179 권장)로 변환한 파일을 사용하세요.')


def _auto_output(alg, param) -> str:
    """출력 파라미터 생략 시 결과 저장 위치의 MCP_실행결과\\ 에 자동 경로 부여."""
    d = os.path.join(RESULTS_PARENT, MCP_DIR)
    os.makedirs(d, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    if param.type() == 'folderDestination':
        path = os.path.join(d, f'{alg.name()}_{param.name()}_{stamp}')
        os.makedirs(path, exist_ok=True)
        return path
    ext = (param.defaultFileExtension()
           if hasattr(param, 'defaultFileExtension') else 'gpkg')
    return os.path.join(d, f'{alg.name()}_{param.name()}_{stamp}.{ext}')


def _summarize_output(path):
    """벡터 출력이면 피처 수·필드·좌표계 요약 (LLM이 결과를 바로 해석하도록)."""
    if not isinstance(path, str) or not path.lower().endswith(_VECTOR_EXT):
        return None
    layer = QgsVectorLayer(path, 'out', 'ogr')
    if not layer.isValid():
        return None
    return {'features': layer.featureCount(),
            'fields': [f.name() for f in layer.fields()],
            'crs': layer.crs().authid()}


MCP_DIR = 'MCP_실행결과'   # 결과 저장 위치 아래: 개별 알고리즘 출력, 실행 이력, 작업 기록


def _append_run_log(entry: dict, parent: str = '') -> None:
    try:
        d = os.path.join(parent or RESULTS_PARENT, MCP_DIR)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'run_log.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except OSError:
        pass


def _jsonable(v):
    if isinstance(v, (str, int, float, bool, type(None))):
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return str(v)


def _plugin_version():
    try:
        with open(os.path.join(PLUGIN_DIR, 'metadata.txt'), encoding='utf-8') as f:
            for line in f:
                if line.startswith('version='):
                    return line.split('=', 1)[1].strip()
    except OSError:
        pass
    return None


def _run(alg, resolved: dict, ctx) -> dict:
    feedback = _Feedback(ctx)
    started = time.time()
    entry = {'time': datetime.now().isoformat(timespec='seconds'), 'algorithm': alg.id(),
             'params': {k: _jsonable(v) for k, v in resolved.items()}}
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = processing.run(alg.id(), resolved, feedback=feedback)
    except QgsProcessingException as e:
        warnings = feedback.warnings()
        entry.update(status='failed', error=str(e), warnings=warnings,
                     elapsed_sec=round(time.time() - started, 1))
        _append_run_log(entry)
        cause = '\n'.join(str(e).strip().splitlines()[-6:])   # traceback 이면 끝부분(오류 종류·위치)만
        tail = '\n'.join(feedback.log[-8:])
        warn = ''.join(f'\n  {w}' for w in warnings)
        raise ToolError(
            f'알고리즘 실행 실패: {alg.displayName()}\n'
            f'- 원인: {cause}\n'
            + (f'- 경고:{warn}\n' if warnings else '') +
            f'- 알고리즘 로그:\n{tail}\n'
            f'- 확인할 것: 입력 파일의 필수 컬럼, 좌표계, 파라미터 값 범위 (guide 의 "입력자료" 참조).')
    # 안전장치: 실행 방식에 따라(예: 서버 없이 call_tool 직접 호출) 출력 싱크가 가비지 수집 전까지
    # 닫히지 않아 반환 직후 결과 파일이 빈 경우가 있었다. 요약·다음 단계 입력 전에 확실히 닫는다.
    result = dict(result)
    gc.collect()
    outputs = {k: _jsonable(v) for k, v in result.items()}
    elapsed = round(time.time() - started, 1)
    warnings = feedback.warnings()
    entry.update(status='ok', outputs=outputs, warnings=warnings, elapsed_sec=elapsed)
    _append_run_log(entry)
    return {'outputs': outputs, 'elapsed_sec': elapsed, 'log': feedback.log, 'warnings': warnings}


# ---------------------------------------------------------------- MCP tools
@_tool(qgis=True)
def check_environment() -> dict:
    """실행 환경 점검 — QGIS·S-Sim 플러그인 버전, 알고리즘 수, 데이터팩 위치와 지역, 결과 저장 위치.

    문제가 있으면 각 항목에 조치 방법을 담아 돌려준다. 분석 전에 한 번 호출 권장.
    """
    provider = QgsApplication.processingRegistry().providerById(PROVIDER_ID)
    regs = _regions()
    return {
        'qgis_version': Qgis.QGIS_VERSION,
        'plugin': {'path': PLUGIN_DIR, 'version': _plugin_version(),
                   'bundled': PLUGIN_PARENT == PKG_DIR},
        'algorithms': len(provider.algorithms()) if provider else 0,
        'datapack': (DATAPACK_DIR if os.path.isdir(DATAPACK_DIR) else
                     f'없음: {DATAPACK_DIR} — 배포 묶음에 S-Sim_데이터팩 폴더를 두거나 '
                     f'환경변수 S_SIM_DATAPACK 으로 지정'),
        'regions': list(regs),
        'results_parent': RESULTS_PARENT,
        'guide_topics': guide()['topics'],
        'mcp_package': 'OK' if _MCP_ERROR is None else f'미설치 — 설치_mcp.bat 실행 ({_MCP_ERROR})',
    }


@_tool()
def list_regions(region: str = '') -> list[dict]:
    """데이터팩의 분석 지역 목록과 지역별 입력 파일 경로·존재 여부, 이미 만들어진 분석 결과.

    플러그인의 [분석 지역 설정]과 같은 정보다. run_algorithm 의 입력 경로를 채울 때 쓴다.

    Args:
        region: 지역명(일부만 써도 됨). 비우면 전체 지역.
    """
    regs = _regions()
    names = [_region(region)[0]] if region else list(regs)
    out = []
    for name in names:
        folder, js = regs[name]
        paths = _region_paths(folder, js)
        rr = _result_root(name)
        results = {
            'result_root': rr,
            'centers': os.path.join(rr, OUT_SP_CTR, '중심지_위계.gpkg'),
            'center_points': os.path.join(rr, OUT_SP_ZONE, '중심지_중심점.gpkg'),
            'grid_center_od': os.path.join(rr, OUT_SP_ZONE, '격자_중심지_OD.csv'),
            'zones_top': os.path.join(rr, OUT_SP_ZONE, '상위생활권_1차중심지권역.gpkg'),
            'scenario_folder': os.path.join(rr, SCEN_DIR),
            'kpi_report': os.path.join(rr, OUT_KPI, '공간구조_진단_대시보드.html'),
        }
        out.append({
            'name': name, 'unit': js.get('unit'), 'base_year': js.get('base_year'),
            'city_type': js.get('city_type'), 'trend_type': js.get('trend_type'),
            'sgg_count': len(js.get('sgg_codes', [])),
            'inputs': {k: {'path': v, 'exists': _exists(v)} for k, v in paths.items() if v},
            'grid_fields': js.get('data', {}).get('grid', {}).get('fields', {}),
            'results': {k: {'path': v, 'exists': _exists(v)} for k, v in results.items()},
        })
    return out


@_tool(qgis=True)
def list_algorithms() -> list[dict]:
    """S-Sim 알고리즘 17종 목록 (id, 이름, 그룹).

    실행 절차: list_algorithms → describe_algorithm(id) 로 파라미터 확인
    → run_algorithm(id, params) 로 실행. 플러그인 흐름 전체는 run_pipeline, KPI 는 run_kpi 가
    플러그인과 같은 기본값·연결로 실행한다(백그라운드 작업, job_status 로 확인).
    """
    provider = QgsApplication.processingRegistry().providerById(PROVIDER_ID)
    return [{'id': a.id(), 'name': a.displayName(), 'group': a.group()}
            for a in provider.algorithms()]


@_tool(qgis=True)
def describe_algorithm(alg_id: str) -> dict:
    """알고리즘 1개의 파라미터 명세 — run_algorithm 호출 전에 확인.

    Args:
        alg_id: 알고리즘 id ('krihs_ssim:' 접두는 생략 가능, 예: 'bau_grid_allocate')
    """
    alg = _alg(alg_id)
    params = []
    for p in alg.parameterDefinitions():
        params.append({
            'name': p.name(),
            'label': p.description(),
            'type': p.type(),
            'optional': bool(p.flags() & QgsProcessingParameterDefinition.FlagOptional),
            'is_output': p.type() in _DEST_TYPES,
            'default': _jsonable(p.defaultValue()),
        })
    return {'id': alg.id(), 'name': alg.displayName(), 'group': alg.group(),
            'help': (alg.shortHelpString() or '')[:1500],
            'parameters': params,
            'note': '입력 파일은 절대경로 또는 데이터팩·결과 폴더 안의 파일명(자동 탐색). '
                    '출력 파라미터는 생략하면 MCP_실행결과\\ 에 자동 저장.'}


@_tool(qgis=True)
def run_algorithm(alg_id: str, params: dict, ctx: Context | None = None) -> dict:
    """알고리즘 실행 (17종 전부). describe_algorithm 으로 파라미터 확인 후 호출.

    입력 파일이 없거나 좌표계가 잘못됐으면 실행 전에 안내 메시지로 알려준다.
    실행 중 진행률을 보고하고, 결과에는 출력 파일별 피처 수·필드 요약이 붙는다.
    결과의 warnings(알고리즘 경고 전부, 예: 지정한 필드가 없어 대체 필드를 씀)는 사용자에게 전한다.
    OD 알고리즘은 od_pairs(전체·성공·실패 쌍 수)를 함께 돌려준다. 실패(도달불가)가 있으면 사용자에게 알린다.

    Args:
        alg_id: 알고리즘 id ('krihs_ssim:' 접두는 생략 가능)
        params: 파라미터 이름→값. 입력 파일은 절대경로 또는 파일명,
            출력(OUTPUT 등)은 생략 시 자동 저장.
    """
    alg = _alg(alg_id)
    resolved = dict(params)
    problems = []
    for p in alg.parameterDefinitions():
        val = resolved.get(p.name())
        if p.type() in _INPUT_TYPES and isinstance(val, str) and val:
            try:
                path = _resolve_input(val)
                if p.type() in _VECTOR_TYPES:
                    _check_vector(path)
                resolved[p.name()] = path
            except (FileNotFoundError, ValueError) as e:
                problems.append(f'[{p.name()}: {p.description()}] {e}')
    if problems:
        raise ValueError('\n'.join(problems))
    for p in alg.parameterDefinitions():   # 입력 검사를 통과한 뒤에만 출력 경로(폴더) 생성
        if p.type() in _DEST_TYPES and not resolved.get(p.name()):
            resolved[p.name()] = _auto_output(alg, p)
    r = _run(alg, resolved, ctx)
    summary = {k: s for k, v in r['outputs'].items() if (s := _summarize_output(v))}
    out = {'outputs': r['outputs'], 'summary': summary, 'elapsed_sec': r['elapsed_sec'],
           'warnings': r['warnings'], 'log': r['log'][-5:]}
    od = _od_counts(r['log'])
    if od:
        out['od_pairs'] = od
        if od['실패']:
            out['warnings'] = out['warnings'] + [
                f"OD {od['전체']:,}쌍 중 {od['실패']:,}쌍은 거리가 계산되지 않아(도달불가) 결과에서 빠짐 — "
                f"성공 {od['성공']:,}쌍만 저장됨"]
    return out


_OD_DONE = re.compile(r'([\d,]+)행 \(도달불가 ([\d,]+) 제외\)')


def _od_counts(log):
    """OD 알고리즘(격자→중심지·중심지 간) 완료 로그 → 성공·실패 쌍 수. OD가 아니면 None."""
    for line in reversed(log):
        m = _OD_DONE.search(line)
        if m:
            ok, ng = (int(g.replace(',', '')) for g in m.groups())
            return {'전체': ok + ng, '성공': ok, '실패': ng}
    return None


# ---------------------------------------------------------------- 플러그인 흐름 실행 (백그라운드 작업)
# 플러그인 화면 코드를 그대로 쓰는 pipeline_runner.py 를 별도 프로세스로 실행한다.
# 전체 흐름은 15분 안팎이라 MCP 응답을 기다리지 않고 작업을 시작한 뒤 job_status 로 확인한다.
import subprocess  # noqa: E402
import threading  # noqa: E402
import uuid  # noqa: E402

RUNNER = os.path.join(SERVER_DIR, 'pipeline_runner.py')
_STEPS = ['population', 'future_centers', 'centers_zones', 'kpi', 'viz']
_STEP_LABEL = {'population': '장래인구 격자 배분(거주 BAU·거점유도·계획인구, 근무 추계·BAU·계획반영)',
               'future_centers': '장래 중심지 식별',
               'centers_zones': '중심지 및 생활권 분석 ①~⑤',
               'kpi': 'KPI 지표 계산(일괄 산출 + 종합 리포트)',
               'viz': '3D 시각화'}
_STEP_MIN = {'population': 2, 'future_centers': 1, 'centers_zones': 6, 'kpi': 7, 'viz': 1}
_JOBS = {}
_JOB_LOCK = threading.Lock()


def _start_job(kind: str, cfg: dict) -> dict:
    with _JOB_LOCK:
        busy = [j for j in _JOBS.values() if j['status'] == 'running']
        if busy:
            raise RuntimeError(f"이미 실행 중인 작업이 있습니다: {busy[0]['id']} ({busy[0]['kind']}). "
                               f'job_status 로 확인하거나 cancel_job 으로 멈춘 뒤 다시 시작하세요.')
        jid = datetime.now().strftime('%m%d_%H%M%S_') + uuid.uuid4().hex[:4]
        os.makedirs(cfg['out_dir'], exist_ok=True)
        cfg_path = os.path.join(tempfile.gettempdir(), f'ssim_job_{jid}.json')
        with open(cfg_path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False)
        log_dir = os.path.join(cfg['out_dir'], MCP_DIR, '작업기록')
        os.makedirs(log_dir, exist_ok=True)
        job = {'id': jid, 'kind': kind, 'status': 'running', 'started': time.time(),
               'config': cfg, 'events': [], 'result': None,
               'log_file': os.path.join(log_dir, f'{jid}.jsonl')}
        proc = subprocess.Popen([sys.executable, RUNNER, cfg_path], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        job['proc'] = proc
        _JOBS[jid] = job

    def _reader():
        with open(job['log_file'], 'a', encoding='utf-8') as lf:
            for raw in proc.stdout:
                line = raw.decode('utf-8', 'replace').strip()
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                lf.write(line + '\n')
                lf.flush()
                job['events'].append(ev)
                if ev.get('type') == 'done':
                    job['result'] = ev.get('result')
                    job['ok'] = ev.get('ok')
        proc.wait()
        if job['status'] == 'running':
            job['status'] = ('done' if job.get('ok') else 'failed') if job['result'] is not None else 'crashed'
        job['elapsed_sec'] = round(time.time() - job['started'])
        _append_run_log({'time': datetime.now().isoformat(timespec='seconds'), 'job': jid, 'kind': kind,
                         'status': job['status'], 'elapsed_sec': job['elapsed_sec']}, cfg['out_dir'])
    threading.Thread(target=_reader, daemon=True).start()
    return job


def _job_brief(job, detail=False):
    steps = [e for e in job['events'] if e.get('type') == 'step']
    done = [e for e in steps if e.get('status') in ('done', 'failed')]
    current = steps[-1]['step'] if steps and steps[-1].get('status') == 'start' else None
    out = {'job_id': job['id'], 'kind': job['kind'], 'status': job['status'],
           'elapsed_sec': (round(time.time() - job['started']) if job['status'] == 'running'
                           else job.get('elapsed_sec')),
           'current_step': current,
           'completed_steps': [{'step': e['step'], 'status': e['status'], 'sec': e.get('elapsed_sec')}
                               for e in done],
           'problems': [{'step': e['step'], 'errors': e.get('errors'), 'messages': e.get('messages')}
                        for e in done if e['status'] == 'failed'],
           'log_file': job['log_file']}
    if detail:
        out['links'] = [ln for e in done for ln in e.get('links', [])]
    if job['result'] is not None:
        out['result'] = job['result']
    return out


def _job_config(region, out_dir, steps, **opts):
    name, _ = _region(region)
    cfg = {'region': name, 'out_dir': out_dir or RESULTS_PARENT, 'datapack': DATAPACK_DIR,
           'plugin_parent': PLUGIN_PARENT, 'steps': steps}
    cfg.update({k: v for k, v in opts.items() if v is not None})
    return cfg


@_tool()
def run_pipeline(region: str = '', steps: list[str] | None = None, out_dir: str = '',
                 min_density: int = 150, city_type: str = '거점', use_work_criteria: bool = True,
                 fac_radius: float | None = None, fac_minpop: float | None = None,
                 fac_radius_hub: float | None = None, fac_minpop_hub: float | None = None,
                 dedup_by_emd: bool = True, min_center_dist: float = 1500) -> dict:
    """플러그인 전체 흐름을 메뉴 순서 그대로 실행 (백그라운드 작업). 플러그인 화면에서 버튼을 차례로
    누른 것과 같은 결과·결과 폴더를 만든다. 바로 job_id 를 돌려주므로 job_status 로 진행을 확인한다.

    steps (생략하면 전체, 실행 순서는 항상 아래 순서):
      'population'     장래인구 격자 배분 — 거주 BAU·정책관심구역·거점유도·계획인구, 근무 추계·BAU·계획반영,
                       KPI 장래 전망용 시나리오별 장래인구 파일 (약 2분)
      'future_centers' 장래 중심지 식별 (약 1분)
      'centers_zones'  중심지 및 생활권 분석 ①~⑤ — 후보 추출·속성·위계(표4·표5 기준), 중심점,
                       격자-중심지 OD, 3차중심지 권역, 상위생활권 (약 6분)
      'kpi'            KPI 일괄 산출 + 종합 리포트 (약 7분, centers_zones 결과 필요)
      'viz'            3D 시각화 HTML

    Args:
        region: 지역명(비우면 데이터팩 지역이 하나일 때 그 지역)
        steps: 실행할 단계 목록. 생략하면 전체
        out_dir: 결과 저장 폴더(그 아래 S-Sim_분석결과(지역명)\\). 비우면 기본 위치
        min_density: 중심지 후보 최소 주거인구 밀도 100·150·200·300 (기본 150)
        city_type: 위계 기준 도시위상 '거점'·'강소'·'자립' (기본 '거점')
        use_work_criteria: 1·2차 위계에 근무인구 기준(표5)도 적용 (기본 True)
        fac_radius, fac_minpop, fac_radius_hub, fac_minpop_hub: KPI 시설 기준(생략하면 플러그인 기본값
            근린 1500m·500명, 거점 3000m·3000명)
        dedup_by_emd: 중심지 읍면동별 중복 제거 — 읍면동마다 주거인구 최대 1개 유지 (기본 True,
            중심지 후보 추출·장래 중심지 식별 공통)
        min_center_dist: 중심지 간 최소 거리(m, 폴리곤 경계 간 직선거리). 주거인구 큰 순으로 남기고
            남긴 중심지와 이 거리 미만인 중심지는 상위 중심지의 영향권으로 보고 제거. 0이면 적용 안 함
            (기본 1500, 중심지 후보 추출·장래 중심지 식별 공통)
    """
    bad = [s for s in (steps or []) if s not in _STEPS]
    if bad:
        raise ValueError('steps 는 ' + ', '.join(_STEPS) + ' 중에서 고르세요: ' + ', '.join(bad))
    steps = [s for s in _STEPS if s in (steps or _STEPS)]
    if city_type not in ('거점', '강소', '자립'):
        raise ValueError("city_type 은 '거점'·'강소'·'자립' 중 하나")
    if int(min_density) not in (100, 150, 200, 300):
        raise ValueError('min_density 는 100·150·200·300 중 하나')
    cfg = _job_config(region, out_dir, steps, min_density=int(min_density), city_type=city_type,
                      use_work_criteria=bool(use_work_criteria), kpi_scope='all',
                      fac_radius=fac_radius, fac_minpop=fac_minpop,
                      fac_radius_hub=fac_radius_hub, fac_minpop_hub=fac_minpop_hub,
                      dedup_by_emd=bool(dedup_by_emd), min_center_dist=float(min_center_dist or 0))
    job = _start_job('pipeline', cfg)
    return {'job_id': job['id'], 'region': cfg['region'], 'steps': [_STEP_LABEL[s] for s in steps],
            'result_root': _result_root(cfg['region'], cfg['out_dir']),
            'expected_minutes': sum(_STEP_MIN[s] for s in steps),
            'note': '백그라운드에서 실행 중. 1~2분 간격으로 job_status(job_id) 로 확인하세요. '
                    'QGIS 화면에서 같은 결과 파일을 열어 두면 덮어쓰기가 실패할 수 있습니다.'}


@_tool()
def run_kpi(region: str = '', scope: str = 'all', out_dir: str = '',
            fac_radius: float | None = None, fac_minpop: float | None = None,
            fac_radius_hub: float | None = None, fac_minpop_hub: float | None = None) -> dict:
    """공간구조 KPI 산출만 실행 (백그라운드 작업) — 플러그인 [KPI 지표 계산]과 같다.

    이미 만든 중심지·생활권 분석 결과(중심지 위계, 최상위 생활권, 격자-중심지 OD)와
    시나리오별 장래인구를 플러그인과 같은 방식으로 자동 연결한다. scope='all'이면 단계별 CSV·JSON
    4종과 종합 리포트(공간구조_진단_대시보드.html)를 만든다(약 7분). job_status 로 확인한다.
    리포트 머리의 [성과 평가 지원] 창에서 목표치를 정해 핵심지표 달성 여부를 판정할 수 있다(guide('kpi') §6).

    Args:
        region: 지역명(비우면 데이터팩 지역이 하나일 때 그 지역)
        scope: 'all'(일괄 산출, 기본) 또는 'basic'·'compact'·'network'·'life'(단계 산출)
        out_dir: 결과 저장 폴더(그 아래 S-Sim_분석결과(지역명)\\). 비우면 기본 위치
        fac_radius, fac_minpop: 근린시설 서비스권 반경(m)·관리대상 최소 배후인구(명) — 생략 시 1500·500
        fac_radius_hub, fac_minpop_hub: 거점시설 반경(m)·최소 배후인구(명) — 생략 시 3000·3000
            (이 기본값은 예시 산출용으로 근거 확정 전. 1차년도 보고서는 5000·10000. guide('kpi') 참조)
    """
    if scope not in ('all', 'basic', 'compact', 'network', 'life'):
        raise ValueError("scope 는 'all'·'basic'·'compact'·'network'·'life' 중 하나")
    name, _ = _region(region)
    rr = _result_root(name, out_dir)
    need = {'중심지 위계(① 결과)': os.path.join(rr, OUT_SP_CTR, '중심지_위계.gpkg'),
            '최상위 생활권(⑤ 결과)': os.path.join(rr, OUT_SP_ZONE, '상위생활권_1차중심지권역.gpkg')}
    missing = [f'{k}: {v}' for k, v in need.items() if not os.path.exists(v)]
    if missing:
        raise ValueError('KPI 산출에 필요한 결과가 없습니다.\n' + '\n'.join('- ' + m for m in missing) +
                         "\n- run_pipeline(steps=['centers_zones']) 로 중심지·생활권 분석을 먼저 실행하세요.")
    cfg = _job_config(name, out_dir, ['kpi'], kpi_scope=scope, fac_radius=fac_radius, fac_minpop=fac_minpop,
                      fac_radius_hub=fac_radius_hub, fac_minpop_hub=fac_minpop_hub)
    job = _start_job('kpi', cfg)
    return {'job_id': job['id'], 'region': name, 'scope': scope, 'kpi_dir': os.path.join(rr, OUT_KPI),
            'linked': {'od': os.path.exists(os.path.join(rr, OUT_SP_ZONE, '격자_중심지_OD.csv')),
                       'scenario_folder': os.path.isdir(os.path.join(rr, SCEN_DIR))},
            'expected_minutes': 7 if scope == 'all' else 2,
            'note': '백그라운드에서 실행 중. job_status(job_id) 로 확인하세요.'}


@_tool()
def job_status(job_id: str = '', detail: bool = False) -> dict:
    """백그라운드 작업(run_pipeline·run_kpi)의 진행 상황과 결과.

    status: running(실행 중) · done(완료) · failed(일부 단계 실패) · crashed(비정상 종료) · cancelled.
    완료되면 result 에 결과 폴더, 단계별 파일 목록, KPI 요약, 리포트 경로가 붙는다.
    KPI 요약(kpi_summary)은 계획권역 핵심지표 ①~④의 시나리오별·연도별 값이다(③ 대표값 = 1차 중심지까지).
    성과 평가(목표 대비 판정)는 이 값으로 계산한다 — 판정 규칙은 kpi_summary.note·guide('kpi') §6.

    Args:
        job_id: 작업 id (비우면 가장 최근 작업)
        detail: True 면 단계 사이 자동 연결 기록도 포함
    """
    if not _JOBS:
        return {'note': '이 서버 세션에서 시작한 작업이 없습니다. 이전 작업 기록은 결과 저장 위치의 MCP_실행결과\\작업기록\\ 에 있습니다.'}
    job = _JOBS.get(job_id) if job_id else max(_JOBS.values(), key=lambda j: j['started'])
    if job is None:
        raise ValueError('작업 없음: ' + job_id + ' — 작업: ' + ', '.join(_JOBS))
    return _job_brief(job, detail)


@_tool()
def cancel_job(job_id: str = '') -> dict:
    """실행 중인 백그라운드 작업을 멈춘다. 멈춘 단계의 결과 파일은 불완전할 수 있다.

    Args:
        job_id: 작업 id (비우면 실행 중인 작업)
    """
    running = [j for j in _JOBS.values() if j['status'] == 'running']
    job = _JOBS.get(job_id) if job_id else (running[0] if running else None)
    if job is None or job['status'] != 'running':
        return {'note': '실행 중인 작업이 없습니다.'}
    job['status'] = 'cancelled'
    job['proc'].kill()
    return {'job_id': job['id'], 'status': 'cancelled'}


@_tool()
def guide(topic: str = '') -> dict:
    """S-Sim 도움말 — 가이드라인 요약(분석 흐름, 중심지·생활권 기준, KPI 지표와 해석, 입력자료, 결과 폴더).

    Args:
        topic: 주제(파일 이름의 일부, 예: '흐름', '중심지', 'kpi', '입력', '결과'). 비우면 목록만.
    """
    files = sorted(glob.glob(os.path.join(GUIDE_DIR, '*.md')))
    topics = [os.path.splitext(os.path.basename(f))[0] for f in files]
    if not topic:
        return {'topics': topics, 'note': 'guide(topic) 으로 내용을 본다.'}
    hits = [f for f, t in zip(files, topics) if topic.lower() in t.lower()]
    if not hits:
        raise ValueError(f'주제 없음: {topic} — 주제: ' + ', '.join(topics))
    out = {}
    for f in hits:
        with open(f, encoding='utf-8') as fp:
            out[os.path.splitext(os.path.basename(f))[0]] = fp.read()
    return {'topics': topics, 'content': out}


# ---------------------------------------------------------------- 자체 시험
def _selftest(full=False):
    env = check_environment()
    print('env:', env['qgis_version'], '| plugin', env['plugin']['version'], env['plugin']['path'],
          '| algorithms', env['algorithms'], '| regions', env['regions'], '| mcp', env['mcp_package'][:30],
          file=sys.stderr)
    assert env['algorithms'] == 17, f"알고리즘 수 {env['algorithms']}"
    assert env['regions'], '데이터팩 지역 없음'
    regs = list_regions()
    r0 = regs[0]
    miss = [k for k, v in r0['inputs'].items() if not v['exists']]
    print('region:', r0['name'], '| 입력', len(r0['inputs']), '개, 없음', miss,
          '| 결과', {k: v['exists'] for k, v in r0['results'].items()}, file=sys.stderr)
    algs = list_algorithms()
    assert len(algs) == 17
    d = describe_algorithm('extract_candidates')
    assert d['parameters'], '파라미터 명세 비어 있음'
    g = guide()
    assert g['topics'], 'guide 주제 없음'
    assert guide('kpi')['content'], 'guide(kpi) 비어 있음'
    try:
        run_algorithm('bau_grid_allocate', {'INPUT': '없는파일.gpkg'})
        raise AssertionError('데이터 없음 예외 미발생')
    except ToolError as e:
        assert '입력 데이터 없음' in str(e), '안내 문구 누락'
    # 실제 MCP 응답 단계를 거쳐도 원인 메시지가 클라이언트까지 가는지 (가려지면 'Error executing tool' 한 줄만 옴)
    if _MCP_ERROR is None and hasattr(mcp, 'call_tool') and anyio is not None:
        for name, args, expect in [('run_algorithm', {'alg_id': 'bau_grid_allocate',
                                                      'params': {'INPUT': '없는파일.gpkg'}}, '입력 데이터 없음'),
                                   ('run_algorithm', {'alg_id': '없는알고리즘', 'params': {}}, '알고리즘 없음'),
                                   ('run_pipeline', {'steps': ['없는단계']}, 'steps 는'),
                                   ('guide', {'topic': '없는주제'}, '주제 없음')]:
            try:
                anyio.run(mcp.call_tool, name, args)
                raise AssertionError(f'{name}: 오류 미발생')
            except ToolError as e:
                assert expect in str(e), f'{name}: MCP 응답에 원인 메시지 없음 — {e}'
        print('MCP 오류 전달 OK — 원인 메시지가 응답에 포함됨', file=sys.stderr)
    print('selftest OK —', len(algs), 'algorithms,', len(g['topics']), 'guide topics', file=sys.stderr)
    if full:
        st = run_pipeline(r0['name'])
        print('run_pipeline 시작:', st['job_id'], '예상', st['expected_minutes'], '분', file=sys.stderr)
        while True:
            time.sleep(15)
            js = job_status(st['job_id'])
            print(' ', js['status'], js['current_step'], len(js['completed_steps']), '단계 완료', file=sys.stderr)
            if js['status'] != 'running':
                break
        print(json.dumps(js, ensure_ascii=False)[:3000], file=sys.stderr)

if __name__ == '__main__':
    if '--selftest' in sys.argv:
        _selftest(full='--full' in sys.argv)
    else:
        sys.stdout = _protocol_stdout
        mcp.run()
