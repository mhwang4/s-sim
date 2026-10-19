# -*- coding: utf-8 -*-
"""S-Sim 플러그인 화면을 헤드리스로 띄워, 사람이 메뉴 버튼을 누르는 순서 그대로 분석을 실행한다.

MCP 서버(server.py)가 별도 프로세스로 실행한다:  python pipeline_runner.py <작업설정.json>
플러그인 화면 코드(dialog.py)를 그대로 쓰므로 기본값·단계 연결·결과 폴더가 플러그인과 같다.
진행 상황은 stdout에 JSON 한 줄씩 낸다({"type": "step"|"done", ...}). 그 밖의 출력은 stderr.

작업설정 JSON:
  region, out_dir, datapack, plugin_parent,
  steps: ["population", "future_centers", "centers_zones", "kpi", "viz"] 중 일부(순서대로 실행)
  min_density(100·150·200·300), city_type(거점·강소·자립), use_work_criteria(bool),
  kpi_scope(all·basic·compact·network·life), fac_radius, fac_minpop, fac_radius_hub, fac_minpop_hub
"""
import json
import os
import sys
import tempfile
import time
import traceback
import warnings

warnings.simplefilter('ignore')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
_OUT = sys.stdout
sys.stdout = sys.stderr          # 진행 JSON 외의 출력은 모두 stderr


def emit(**kw):
    _OUT.write(json.dumps(kw, ensure_ascii=False) + '\n')
    _OUT.flush()


with open(sys.argv[1], encoding='utf-8') as f:
    CFG = json.load(f)

from qgis.core import QgsApplication  # noqa: E402
from PyQt5.QtCore import QSettings  # noqa: E402
from PyQt5.QtWidgets import QMessageBox  # noqa: E402

app = QgsApplication([], True)
QgsApplication.initQgis()
sys.path.append(os.path.join(QgsApplication.pkgDataPath(), 'python', 'plugins'))
from processing.core.Processing import Processing  # noqa: E402
Processing.initialize()

sys.path.insert(0, CFG['plugin_parent'])
from KRIHS_S_Sim.provider import PopAllocProvider  # noqa: E402
PROV = PopAllocProvider()  # 전역 참조 유지(GC 방지)
QgsApplication.processingRegistry().addProvider(PROV)
import KRIHS_S_Sim.dialog as DM  # noqa: E402

# 플러그인 저장 설정은 임시 ini로 — QGIS 화면에서 쓰는 설정(레지스트리)을 건드리지 않음
_INI = os.path.join(tempfile.mkdtemp(prefix='ssim_mcp_'), 'settings.ini')
DM.QSettings = lambda *a, **k: QSettings(_INI, QSettings.IniFormat)
MSG = []
for _kind in ('warning', 'critical', 'information'):
    setattr(QMessageBox, _kind,
            staticmethod(lambda *a, _k=_kind, **kw: MSG.append((_k, str(a[1]) if len(a) > 1 else '',
                                                                 str(a[2]) if len(a) > 2 else ''))
                         or QMessageBox.Ok))
QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.Yes)
import webbrowser  # noqa: E402
webbrowser.open = lambda *a, **k: True

d = DM.PopAllocDialog()
_seen = [0]
FAILED = []


def run_step(title, fn, nav=None):
    """한 단계 실행 — 새 로그 줄의 연결·오류와 메시지 상자를 모아 보고."""
    if nav:
        d.nav.setCurrentItem(d._nav[nav])
    n0 = len(MSG)
    t = time.time()
    emit(type='step', step=title, status='start')
    err = None
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        err = f'{type(e).__name__}: {e}'
        traceback.print_exc()
    lines = d._log.toPlainText().splitlines()
    new = lines[_seen[0]:]
    _seen[0] = len(lines)
    links = [l[:220] for l in new if l.startswith('[자동연결]') or l.startswith('[연결]')]
    errors = [l[:300] for l in new if '[오류]' in l or '실패' in l]
    msgs = [{'kind': k, 'title': a, 'text': b[:400]} for k, a, b in MSG[n0:]]
    bad = bool(err or errors or any(m['kind'] in ('warning', 'critical') for m in msgs))
    if bad:
        FAILED.append(title)
    emit(type='step', step=title, status='failed' if bad else 'done',
         elapsed_sec=round(time.time() - t, 1), links=links, errors=errors + ([err] if err else []),
         messages=msgs)
    return not bad


def by_value(combo, value):
    for i in range(combo.count()):
        if combo.itemData(i) == value or combo.itemText(i) == str(value):
            return i
    return -1


# ---- 0. 분석 지역 설정 ----
def apply_region():
    d.rg_pack.setFilePath(CFG['datapack'])
    i = d.rg_region.findText(CFG['region'])
    if i < 0:
        raise ValueError(f"데이터팩에 지역 없음: {CFG['region']}")
    d.rg_region.setCurrentIndex(i)
    d.rg_out.setFilePath(CFG['out_dir'])
    d._apply_region(overwrite=True)


ok = run_step('분석 지역 설정', apply_region, 'region')
root = d._region_result_root() if ok else None
steps = CFG.get('steps') or []

if ok and 'population' in steps:
    run_step('거주 ① BAU 배분', lambda: d._run_bau(DM.RES), 'res_bau')
    run_step('거주 ② 정책관심구역 산출', d._run_policy, 'res_hub')
    run_step('거주 ② 거점유도 배분', d._run_hub, 'res_hub')
    run_step('거주 ③ 계획인구 반영 배분', lambda: d._run_plan(DM.RES), 'res_plan')
    run_step('근무 공통 데이터', lambda: None, 'work_common')
    run_step('근무 장래 근무인구 추계', d._run_wproj, 'work_proj')
    run_step('근무 ① BAU 배분', lambda: d._run_bau(DM.WORK), 'work_bau')
    run_step('근무 ② 계획인구 반영 배분', lambda: d._run_plan(DM.WORK), 'work_plan')

if ok and 'future_centers' in steps:
    run_step('장래 중심지 식별', d._run_center, 'center')

if ok and 'centers_zones' in steps:
    def preset():
        mind = int(CFG.get('min_density') or 150)
        city = CFG.get('city_type') or '거점'
        i = by_value(d._hier_min, mind)
        if i < 0:
            raise ValueError('min_density 는 100·150·200·300 중 하나')
        d._hier_min.setCurrentIndex(i)
        d._hier_city.setCurrentText(city)
        d._hier_work.setChecked(bool(CFG.get('use_work_criteria', True)))
        d._apply_hier_preset(d._center_flow(), d._flow_widgets[d._center_flow()['key']])
    run_step('위계 기준 적용 (표4·표5)', preset, 'wf_center')
    for si in range(3):
        run_step(f'① 중심지 추출 및 위계 분류 {si + 1}단계', lambda i=si: d._run_center_step(i), 'wf_center')
    for key in ('wf_odgen', 'wf_gridod', 'wf_basic', 'wf_upper'):
        fl = next(f for f in d.SPST_FLOWS if f['key'] == key)
        run_step(fl['menu'], lambda f=fl: d._run_flow(f), key)

if ok and 'kpi' in steps:
    k = d._kpi
    scope = CFG.get('kpi_scope') or 'all'

    def kpi():
        for wkey, ckey in (('fac_radius', 'fac_radius'), ('fac_minpop', 'fac_minpop'),
                           ('fac_radius_hub', 'fac_radius_hub'), ('fac_minpop_hub', 'fac_minpop_hub')):
            if CFG.get(ckey) is not None:
                k[wkey].setText(str(CFG[ckey]))
        d._run_kpi(scope)
    nav = {'all': 'kpi_batch', 'basic': 'kpi_basic', 'compact': 'kpi_compact',
           'network': 'kpi_net', 'life': 'kpi_life'}[scope]
    run_step('KPI 공통 데이터 연결', lambda: None, 'kpi_common')
    if run_step('KPI ' + ('일괄 산출' if scope == 'all' else scope + ' 산출'), kpi, nav) and scope == 'all':
        run_step('종합 리포트 생성', d._build_report, 'kpi_report')

if ok and 'viz' in steps:
    def viz():
        # 같은 실행에서 인구 배분을 돌리지 않았으면(steps=['viz']) 결과 폴더의 기존 배분·중심지 파일을 연결
        import glob
        fut = os.path.join(root, DM.OUT_FUT)
        names = {f'{DM.RES}_BAU': f'{DM.RES}배분_1_BAU.gpkg', f'{DM.RES}_거점유도': f'{DM.RES}배분_2_거점유도.gpkg',
                 f'{DM.RES}_계획인구': f'{DM.RES}배분_3_계획인구.gpkg', f'{DM.WORK}_BAU': f'{DM.WORK}배분_1_BAU.gpkg',
                 f'{DM.WORK}_계획반영': f'{DM.WORK}배분_2_계획반영.gpkg', '중심지_식별': '중심지_식별.gpkg'}
        for key, fn in names.items():
            fw = d.viz_files.get(key)
            if fw is not None and not fw.filePath().strip():
                hit = glob.glob(os.path.join(fut, '**', fn), recursive=True)
                if hit:
                    fw.setFilePath(hit[0])
                    d._log.appendPlainText(f'[자동연결] 3D 시각화 {key} ← {hit[0]}')
        d._run_viz()
    run_step('3D 시각화', viz, 'viz')


# ---- 결과 정리 ----
def kpi_summary(kdir):
    """KPI JSON 4종을 합쳐 계획권역 핵심지표를 시나리오별로 추림 (종합 리포트와 같은 기준).

    - 시나리오마다 연도별 값(기준연도~목표연도) — 성과 평가(목표 대비 판정)에 그대로 쓸 수 있음
    - ③ 접근 형평성 대표값 = 1차 중심지까지 30분 내 도달 인구 비율(리포트·성과 평가 지원과 같음),
      1·2차·1·2·3차는 참고
    - ① 은 과거(내장 SGIS 격자) 값도 함께
    """
    from KRIHS_S_Sim.alg_kpi_indicators import merge_payloads, AREA_JSON_NAMES
    parts = []
    for key, fn in AREA_JSON_NAMES:
        p = os.path.join(kdir, fn)
        if os.path.isfile(p):
            with open(p, encoding='utf-8') as f:
                parts.append((key, json.load(f)))
    if not parts:
        return {}
    data = merge_payloads(parts)
    scs = data.get('scenarios') or []
    if not scs:
        return {}
    r1 = lambda v: None if v is None else round(v, 1)
    out = {'years': scs[0].get('labels') or scs[0].get('years'), 'scenarios': {}}
    past = data.get('past') or {}
    if past.get('years') and past.get('conc'):
        out['과거 중심지 주거인구 비율(%)'] = dict(zip(past['years'], [r1(v) for v in past['conc']]))
    for sc in scs:
        o = {}
        ct = sc.get('center_ts') or []
        if ct:
            o['① 중심지 주거인구 비율(%)'] = [r1(r.get('rconc')) for r in ct]
            o['② 중심지 근무인구 비율(%)'] = [r1(r.get('wconc')) for r in ct]
        conn = sc.get('conn_ts') or []
        if conn and conn[0]:
            eq = [((c or {}).get('equity') or [[None, None, None]])[0] for c in conn]
            o['③ 중심지 접근 형평성(%) — 1차 중심지까지(대표값)'] = [r1(e[0]) for e in eq]
            o['③ 참고 — 1·2차 / 1·2·3차 중심지까지(%)'] = [[r1(x) for x in e[1:]] for e in eq]
        fac = {}
        for gk, v in (sc.get('fac_ts_g') or {}).items():
            if gk.startswith('t:') and v:
                fac[gk[2:]] = {'향유도(%)': [r1(x.get('cover')) for x in v], '관리대상(곳)': [x.get('mgmt') for x in v]}
        if fac:
            o['④ 서비스권 향유도·관리대상 — 시설별'] = fac
        out['scenarios'][sc.get('name')] = o
    out['생활권'] = [z.get('name') for z in scs[0].get('zones', [])]
    out['note'] = ('리포트의 [성과 평가 지원] 창은 이 값으로 목표 대비 판정을 한다: 판정 = 평가 시점 실적 − 목표, '
                   '+구간(기본 0.5%p) 이상 초과 달성 · 0 이상 달성 · −구간 이상 소폭 미달 · 그 아래 미달. '
                   '④ 대표값은 근린·거점 시설 각 1종 향유도의 평균.')
    return out


result = {'result_root': root, 'failed_steps': FAILED,
          'completed_menus': [d.nav.item(i).text().strip() for i in range(d.nav.count())
                              if '✓' in d.nav.item(i).text()]}
if root:
    tree = {}
    for sub in (DM.OUT_FUT, DM.OUT_SP, DM.OUT_KPI):
        p = os.path.join(root, sub)
        if os.path.isdir(p):
            tree[sub] = sorted(os.path.relpath(os.path.join(dp, f), p)
                               for dp, _dn, fn in os.walk(p) for f in fn
                               if '시나리오별_장래인구' not in dp)
    result['files'] = tree
    kdir = os.path.join(root, DM.OUT_KPI)
    if 'kpi' in steps and os.path.isdir(kdir):
        try:
            result['kpi_summary'] = kpi_summary(kdir)
        except Exception as e:  # noqa: BLE001
            result['kpi_summary_error'] = str(e)
        rep = os.path.join(kdir, '공간구조_진단_대시보드.html')
        if os.path.isfile(rep):
            result['kpi_report'] = rep
emit(type='done', ok=bool(ok and not FAILED), result=result)
QgsApplication.exitQgis()
