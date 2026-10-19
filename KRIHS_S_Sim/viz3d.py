# -*- coding: utf-8 -*-
"""배분 결과(pop_YYYY) → deck.gl 3D(2.5D) 시각화 HTML 생성.

단일 HTML 파일에 데이터(JSON)를 내장한다 — 서버 불필요.
deck.gl 스크립트와 OSM 배경 타일은 CDN/온라인 로드 (오프라인이면 배경·라이브러리 미표시).
"""
import json
import os
import re

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject,
    QgsVectorLayer,
)

YEAR_POP = re.compile(r'^pop_(\d{4})$')


def extract_series(path):
    """결과 레이어에서 (연도 목록, [[lon, lat, 연도별 인구...], ...]) 추출."""
    layer = QgsVectorLayer(path, os.path.basename(path), 'ogr')
    if not layer.isValid():
        raise ValueError(f'레이어를 열 수 없습니다: {path}')
    years = []
    for f in layer.fields():
        m = YEAR_POP.match(f.name())
        if m:
            years.append(int(m.group(1)))
    years.sort()
    if not years:
        raise ValueError(f'pop_YYYY 컬럼이 없습니다 (배분 결과 파일이 맞는지 확인): {path}')

    transform = QgsCoordinateTransform(
        layer.crs(), QgsCoordinateReferenceSystem('EPSG:4326'),
        QgsProject.instance())
    points = []
    for feat in layer.getFeatures():
        geom = feat.geometry()
        if geom.isEmpty():
            continue
        c = transform.transform(geom.centroid().asPoint())
        row = [round(c.x(), 5), round(c.y(), 5)]
        for y in years:
            try:
                row.append(round(float(feat[f'pop_{y}']), 1))
            except (TypeError, ValueError):
                row.append(0.0)
        points.append(row)
    if not points:
        raise ValueError(f'피처가 없습니다: {path}')
    return years, points


TIERS = ['1차', '2차', '3차', '위계외']


def extract_centers(path):
    """중심지 식별 결과(연도별 폴리곤) → (연도 목록, [[연도, 위계idx, 주거인구, [외곽링...], 근무인구], ...]).

    링은 EPSG:4326 [ [lon,lat], ... ]. 구멍(holes)은 무시 — 3D 기둥 표현 목적.
    근무인구(pop_w)는 아이템 끝에 붙인다 — 링 인덱스(it[3]) 유지 목적.
    """
    layer = QgsVectorLayer(path, os.path.basename(path), 'ogr')
    if not layer.isValid():
        raise ValueError(f'레이어를 열 수 없습니다: {path}')
    names = {f.name() for f in layer.fields()}
    missing = {'year', 'tier', 'pop_r'} - names
    if missing:
        raise ValueError(
            f'중심지 결과 필수 필드 부재 {sorted(missing)} '
            f'(분석 > 중심지 식별 출력 파일인지 확인): {path}')

    transform = QgsCoordinateTransform(
        layer.crs(), QgsCoordinateReferenceSystem('EPSG:4326'),
        QgsProject.instance())
    years, items = set(), []
    for feat in layer.getFeatures():
        geom = feat.geometry()
        if geom.isEmpty():
            continue
        polys = geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()]
        rings = []
        for p in polys:
            if not p:
                continue
            rings.append([[round(pt.x(), 5), round(pt.y(), 5)]
                          for pt in (transform.transform(v) for v in p[0])])
        if not rings:
            continue
        year = int(feat['year'])
        years.add(year)
        tier = str(feat['tier'])
        ti = TIERS.index(tier) if tier in TIERS else len(TIERS) - 1
        try:
            pop = round(float(feat['pop_r']), 1)
        except (TypeError, ValueError):
            pop = 0.0
        try:
            popw = round(float(feat['pop_w']), 1) if 'pop_w' in names else 0.0
        except (TypeError, ValueError):
            popw = 0.0
        items.append([year, ti, pop, rings, popw])
    if not items:
        raise ValueError(f'피처가 없습니다: {path}')
    return sorted(years), items


def build_html(scenarios, out_path, emd_geojson=None, basemap_url='', summary=None):
    """scenarios: [{'name': str, 'years': [int], 'points': [[lon,lat,v...]]}] → HTML 저장.
    emd_geojson: 읍면동 경계 GeoJSON dict(선택). basemap_url: 배경 타일 URL 템플릿(선택,
    {z}/{x}/{y} 포함 — V-World·MapTiler 등 키 포함 URL). 비우면 배경지도 없음.
    summary: 장래 중심지 요약지표 dict(선택) — summarize_centers() 결과."""
    html = _TEMPLATE.replace(
        '__DATA__', json.dumps(scenarios, ensure_ascii=False, separators=(',', ':')))
    html = html.replace(
        '__EMD__', json.dumps(emd_geojson, ensure_ascii=False, separators=(',', ':'))
        if emd_geojson else 'null')
    html = html.replace('__BGURL__', json.dumps(basemap_url or ''))
    html = html.replace(
        '__SUMMARY__', json.dumps(summary, ensure_ascii=False, separators=(',', ':'))
        if summary else 'null')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    return out_path


# 장래 중심지 전망 요약지표 (그림 4-14: 중심지 수·면적/중심화율·인구 집중도·인구 밀도)
_BENCHMARKS = {'ratio': 10.2, 'conc_r': 93.7, 'conc_w': 84.8,
               'dens_r': 4241, 'dens_w': 2539, 'did': 4000}   # 전국(2023 거점지도)·일본 DID


def _region_pop(path, years, base_field):
    """배분결과 → {연도: 권역 총인구}. 장래 연도는 pop_YYYY, 기준연도는 base_field(pop_r/pop_w)."""
    lyr = QgsVectorLayer(path, 'reg', 'ogr')
    if not lyr.isValid():
        return None
    names = {f.name() for f in lyr.fields()}
    yf = {}
    for y in years:
        fld = 'pop_%d' % y
        yf[y] = fld if fld in names else (base_field if base_field in names else None)
    tot = {y: 0.0 for y in years}
    for f in lyr.getFeatures():
        for y, fld in yf.items():
            if fld:
                try:
                    tot[y] += float(f[fld] or 0)
                except (TypeError, ValueError):
                    pass
    return tot


def summarize_centers(center_path, res_path=None, work_path=None):
    """중심지 식별 결과(+거주·근무 배분결과) → 요약지표 dict.
    중심지 수·면적·밀도는 중심지 결과만으로, 중심화율·집중도는 배분결과(권역 총량)로 산출."""
    cl = QgsVectorLayer(center_path, 'cen', 'ogr')
    if not cl.isValid():
        return None
    agg = {}   # year -> [n, area, pop_r, pop_w]
    for f in cl.getFeatures():
        try:
            y = int(f['year'])
        except (TypeError, ValueError):
            continue
        a = agg.setdefault(y, [0, 0.0, 0.0, 0.0])
        a[0] += 1
        a[1] += float(f['area_km2'] or 0)
        a[2] += float(f['pop_r'] or 0)
        a[3] += float(f['pop_w'] or 0)
    if not agg:
        return None
    years = sorted(agg)
    out = {'years': years, 'base_year': min(years), 'benchmarks': _BENCHMARKS,
           'center_count': [agg[y][0] for y in years],
           'center_area': [round(agg[y][1], 1) for y in years],
           'dens_r': [round(agg[y][2] / agg[y][1]) if agg[y][1] > 0 else None for y in years],
           'dens_w': [round(agg[y][3] / agg[y][1]) if agg[y][1] > 0 else None for y in years],
           'center_ratio': None, 'conc_r': None, 'conc_w': None}
    # 권역 면적(격자 1㎢ 가정) + 총인구
    if res_path:
        rl = QgsVectorLayer(res_path, 'ra', 'ogr')
        reg_area = rl.featureCount() if rl.isValid() else 0
        if reg_area > 0:
            out['center_ratio'] = [round(agg[y][1] / reg_area * 100, 1) for y in years]
        reg_r = _region_pop(res_path, years, 'pop_r')
        if reg_r:
            out['conc_r'] = [round(agg[y][2] / reg_r[y] * 100, 1) if reg_r.get(y) else None
                             for y in years]
    if work_path:
        reg_w = _region_pop(work_path, years, 'pop_w')
        if reg_w:
            out['conc_w'] = [round(agg[y][3] / reg_w[y] * 100, 1) if reg_w.get(y) else None
                             for y in years]
    return out


def _round_coords(obj, nd=5):
    """GeoJSON geometry 좌표 자릿수 축소(용량·렉 감소)."""
    if isinstance(obj, list):
        if obj and isinstance(obj[0], (int, float)):
            return [round(obj[0], nd), round(obj[1], nd)]
        return [_round_coords(x, nd) for x in obj]
    return obj


def emd_to_geojson(path, name_field='', clip_bbox=None, simplify_deg=0.0008):
    """읍면동 폴리곤 → EPSG:4326 GeoJSON. 분석영역 bbox로 클립 + 지오메트리 단순화 + 좌표 축소.
    clip_bbox: (minlon, minlat, maxlon, maxlat) — 겹치는 읍면동만 사용(성능)."""
    from qgis.core import (QgsVectorLayer, QgsGeometry, QgsProject, QgsRectangle,
                           QgsCoordinateTransform, QgsCoordinateReferenceSystem)
    lyr = QgsVectorLayer(path, 'emd', 'ogr')
    if not lyr.isValid():
        raise ValueError(f'읍면동 경계 레이어를 열 수 없습니다: {path}')
    wgs = QgsCoordinateReferenceSystem('EPSG:4326')
    ct = (QgsCoordinateTransform(lyr.crs(), wgs, QgsProject.instance())
          if lyr.crs() != wgs else None)
    fi = lyr.fields().indexOf(name_field) if name_field else -1
    rect = None
    if clip_bbox:
        mnx, mny, mxx, mxy = clip_bbox
        pad = 0.05   # 여유
        rect = QgsRectangle(mnx - pad, mny - pad, mxx + pad, mxy + pad)
    feats = []
    for f in lyr.getFeatures():
        g = f.geometry()
        if g is None or g.isEmpty():
            continue
        if ct:
            g = QgsGeometry(g)
            g.transform(ct)
        if rect is not None and not g.boundingBox().intersects(rect):
            continue   # 분석영역 밖 읍면동 제외
        if simplify_deg > 0:
            gs = g.simplify(simplify_deg)
            if gs is not None and not gs.isEmpty():
                g = gs
        nm = str(f[fi]) if fi >= 0 and f[fi] is not None else ''
        gj = json.loads(g.asJson())
        geom = _round_coords(gj.get('coordinates'))
        feats.append({'type': 'Feature',
                      'properties': {'name': nm, 'idx': len(feats)},
                      'geometry': {'type': gj['type'], 'coordinates': geom}})
    return {'type': 'FeatureCollection', 'features': feats}


_TEMPLATE = r'''<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>격자 인구배분 3D</title>
<script src="https://unpkg.com/deck.gl@8.9.35/dist.min.js"></script>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  @font-face {
    font-family: 'KoPubWorldDotum';
    src: url('https://cdn.jsdelivr.net/gh/projectnoonnu/noonfonts_2105_2@1.0/KoPubWorldDotumMedium.woff') format('woff');
    font-weight: normal; font-display: swap;
  }
  @font-face {
    font-family: 'KoPubWorldDotum';
    src: url('https://cdn.jsdelivr.net/gh/projectnoonnu/noonfonts_2105_2@1.0/KoPubWorldDotumBold.woff') format('woff');
    font-weight: bold; font-display: swap;
  }
  html, body { margin:0; height:100%; background:#101418;
    font-family:'KoPubWorldDotum','KoPub Dotum','Malgun Gothic',sans-serif; }
  #maps { position:absolute; inset:0; display:flex; }
  .map { position:relative; flex:1; }
  .cap { position:absolute; top:10px; left:50%; transform:translateX(-50%); z-index:1;
    background:rgba(16,20,24,.75); color:#fff; padding:4px 14px; border-radius:14px;
    font-size:15px; font-weight:bold; pointer-events:none; }
  #panel {
    position:absolute; top:16px; left:16px; z-index:2;
    background:rgba(16,20,24,.85); color:#eee; padding:18px 22px;
    border-radius:10px; min-width:270px; font-size:13px; line-height:1.6;
  }
  #panel h1 { font-size:15px; margin:0 0 14px; }
  #panel label { display:block; margin:8px 0; cursor:pointer; }
  #panel label.inl { display:inline; margin-right:10px; }
  #panel select { width:100%; padding:4px; margin:4px 0 10px; background:#222;
    color:#eee; border:1px solid #555; border-radius:4px; }
  .side { margin-top:14px; padding-top:12px; border-top:1px solid #444; }
  .side .t { font-weight:bold; color:#9cf; }
  .dots { margin:8px 0 4px; }
  .dot { display:inline-block; padding:3px 9px; margin-right:5px; border-radius:12px;
    background:#2c3238; border:1px solid #5a626a; cursor:pointer; font-size:12px;
    position:relative; }
  .dot:not(:last-child)::after { content:''; position:absolute; right:-6px; top:50%;
    width:6px; height:1px; background:#5a626a; }
  .dot.on { background:#fff; color:#000; font-weight:bold; border-color:#fff; }
  #legend { margin-top:14px; }
  #legend .bar { height:10px; border-radius:5px;
    background:linear-gradient(to right,#2166ac,#67a9cf,#f7d708,#ef8a62,#b2182b); }
  #legend .lab { display:flex; justify-content:space-between; color:#aaa; font-size:11px; }
  #legend .white { display:flex; align-items:center; gap:6px; margin-top:5px;
    color:#ccc; font-size:11px; }
  #legend .chip { width:14px; height:14px; background:#fff; border:1px solid #888;
    border-radius:3px; }
  #sumBtn { margin-left:8px; font-size:11px; padding:3px 9px; background:#2166ac; color:#fff;
    border:none; border-radius:6px; cursor:pointer; vertical-align:middle; }
  #sumBtn:hover { background:#3b82c4; }
  #sumWrap { position:fixed; inset:0; z-index:20; background:rgba(0,0,0,.55);
    display:none; align-items:center; justify-content:center; }
  #sumBox { background:#fff; color:#222; width:90%; max-width:1120px; height:84%;
    border-radius:12px; padding:16px 22px 20px; overflow:auto; position:relative; }
  #sumBox h2 { margin:0 0 2px; font-size:18px; color:#1e293b; }
  #sumBox .sub { color:#666; font-size:12px; margin-bottom:12px; }
  #sumClose { position:absolute; top:8px; right:14px; font-size:26px; line-height:1;
    border:none; background:none; cursor:pointer; color:#666; }
  #sumGrid { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  #sumGrid .cell { border:1px solid #e3e6ea; border-radius:10px; padding:10px 12px; }
  #sumGrid .cell .t { font-weight:bold; font-size:14px; color:#1e293b; }
  #sumGrid .cell .d { color:#666; font-size:11.5px; margin:1px 0 4px; }
  #sumGrid .chart { width:100%; height:250px; }
  @media(max-width:820px){ #sumGrid{grid-template-columns:1fr;} }
</style>
</head>
<body>
<div id="maps">
  <div class="map" id="mapA"><div class="cap" id="capA"></div></div>
  <div class="map" id="mapB" style="display:none"><div class="cap" id="capB"></div></div>
</div>
<div id="panel">
  <h1>격자 인구배분 (1km) <button id="sumBtn" style="display:none">장래 중심지 요약지표</button></h1>
  <div style="margin-bottom:8px">
    배경:
    <label class="inl"><input type="radio" name="bg" value="dark" checked> 검정</label>
    <label class="inl"><input type="radio" name="bg" value="light"> 회색</label>
  </div>
  <label><input type="checkbox" id="cmp"> 좌/우 비교 모드</label>
  <label id="emdRow" style="display:none"><input type="checkbox" id="emdChk"> 읍면동 경계 (마우스 올리면 지역명)</label>
  <div class="side" id="secA">
    <div class="t" id="titleA">지도</div>
    <select id="scenA"></select>
    <div class="dots" id="dotsA"></div>
  </div>
  <div class="side" id="secB" style="display:none">
    <div class="t">오른쪽 지도</div>
    <select id="scenB"></select>
    <div class="dots" id="dotsB"></div>
  </div>
  <div id="legend">
    <div class="bar" style="background:linear-gradient(90deg,rgb(33,102,172),rgb(103,169,207),rgb(247,215,8),rgb(239,138,98),rgb(178,24,43))"></div>
    <div class="lab"><span>0명</span><span id="legMax"></span></div>
  </div>
  <div id="tierLegend" style="display:none;margin-top:8px;font-size:11px;color:#ccc">
    <span style="color:#d73027">■</span> 1차
    <span style="color:#ff7f00">■</span> 2차
    <span style="color:#1a9850">■</span> 3차 중심지
    <span style="color:#969696">■</span> 위계외 · 높이 = √주거인구
  </div>
  <div style="margin-top:14px;color:#888;font-size:11px">
    인구 = 색·높이(√인구) 연속형 · 두 화면 시점 동기화<br>
    회전: 마우스 휠(가운데 버튼) 드래그 · 우클릭 드래그도 가능</div>
</div>
<div id="sumWrap">
  <div id="sumBox">
    <button id="sumClose">&times;</button>
    <h2>장래 중심지 전망 요약지표</h2>
    <div class="sub" id="sumSub"></div>
    <div id="sumGrid">
      <div class="cell"><div class="t">중심지 수</div><div class="d">연도별 중심지 개수(개)</div><div id="scCount" class="chart"></div></div>
      <div class="cell"><div class="t">중심지 면적 &amp; 중심화율</div><div class="d">면적(좌 ㎢) · 중심화율(우 %)</div><div id="scArea" class="chart"></div></div>
      <div class="cell"><div class="t">인구 집중도 추이</div><div class="d">중심지 내 인구 / 권역 총인구 (%)</div><div id="scConc" class="chart"></div></div>
      <div class="cell"><div class="t">인구 밀도 추이</div><div class="d">중심지 내 주거·근무 인구밀도 (명/㎢)</div><div id="scDens" class="chart"></div></div>
    </div>
  </div>
</div>
<script>
const DATA = __DATA__;
const EMD = __EMD__;   // 읍면동 경계 GeoJSON (선택) 또는 null
const SUMMARY = __SUMMARY__;   // 요약지표 데이터 또는 null (중심지 식별 있을 때만)
let compare = false, bg = 'dark', emdOn = false, emdSel = -1;
const S = {A: {scen: 0, yi: 0}, B: {scen: 0, yi: 0}};
const grids = DATA.filter(s => s.type !== 'center');
const maxVal = Math.max(1, ...grids.map(s => Math.max(...s.points.map(p => Math.max(...p.slice(2))))));
const elevScale = 6000 / Math.sqrt(maxVal);   // 높이 = sqrt(인구) — 작은 값도 식별 가능
{ const _lm = document.getElementById('legMax');
  if (_lm) _lm.textContent = Math.round(maxVal).toLocaleString() + '명'; }
const centers = DATA.filter(s => s.type === 'center');
const maxCtr = Math.max(1, ...centers.map(s => Math.max(...s.items.map(it => it[2]))));
const ctrScale = 8000 / Math.sqrt(maxCtr);
const TIER_NAMES = ['1차 중심지', '2차 중심지', '3차 중심지', '위계외'];
// 1차 빨강 · 2차 야광오렌지 · 3차 초록 · 위계외 회색 (대비 강화)
const TIER_COLORS = [[215,48,39],[255,127,0],[26,152,80],[150,150,150]];
const RAMP = [[33,102,172],[103,169,207],[247,215,8],[239,138,98],[178,24,43]];

function color(v) {   // 연속형 min-max(0~maxVal) 컬러맵
  const t = Math.max(0, Math.min(1, v / maxVal));
  const x = t * (RAMP.length - 1), i = Math.min(RAMP.length - 2, Math.floor(x)), f = x - i;
  return [0,1,2].map(k => Math.round(RAMP[i][k] + (RAMP[i+1][k] - RAMP[i][k]) * f));
}

const all = DATA[0].type === 'center'
  ? DATA[0].items.flatMap(it => it[3].flatMap(r => r))
  : DATA[0].points;
const cx = all.reduce((a,p)=>a+p[0],0)/all.length;
const cy = all.reduce((a,p)=>a+p[1],0)/all.length;
let viewState = {longitude: cx, latitude: cy, zoom: 8.3, pitch: 50, bearing: 0};

const BG_URL = __BGURL__;   // 배경 타일 URL 템플릿(키 포함, {z}/{x}/{y}) 또는 ''
function tileLayer() {
  if (!BG_URL) return null;   // URL 없으면 배경지도 없음 — 읍면동 오버레이로만 방향감
  return new deck.TileLayer({
    id: 'basemap', data: BG_URL,
    minZoom: 0, maxZoom: 19, tileSize: 256,
    loadOptions: {image: {crossOrigin: 'anonymous'}},   // WebGL 텍스처 CORS 대비
    renderSubLayers: props => {
      const {west, south, east, north} = props.tile.bbox;
      return new deck.BitmapLayer(props, {
        data: null, image: props.data, bounds: [west, south, east, north]});
    },
  });
}

function emdLayer() {   // 읍면동 오버레이 — 반투명 채움 · 면 hover 지역명 · 클릭 토글 강조
  if (!EMD || !emdOn) return null;
  return new deck.GeoJsonLayer({
    id: 'emd', data: EMD, pickable: true,
    stroked: true, filled: true,
    getFillColor: f => f.properties.idx === emdSel ? [255, 190, 60, 120] : [150, 150, 150, 35],
    getLineColor: f => f.properties.idx === emdSel ? [255, 140, 0, 255] : [90, 90, 90, 170],
    getLineWidth: f => f.properties.idx === emdSel ? 3 : 1,
    lineWidthUnits: 'pixels', lineWidthMinPixels: 1,
    updateTriggers: {getFillColor: emdSel, getLineColor: emdSel, getLineWidth: emdSel},
  });
}

function colLayer(side) {
  const st = S[side], s = DATA[st.scen];
  const y = Math.min(st.yi, s.years.length - 1);
  if (s.type === 'center') {
    const yr = s.years[y];
    const data = s.items.filter(it => it[0] === yr)
      .flatMap(it => it[3].map(r => ({ring: r, ti: it[1], pop: it[2], popw: it[4]})));
    return new deck.PolygonLayer({
      // 연도·시나리오별 id — 전환 시 레이어 재생성 → 바닥에서 올라오는 애니메이션
      id: 'centers-' + st.scen + '-' + yr, data, extruded: true, pickable: true,
      elevationScale: ctrScale, opacity: 0.85,
      getPolygon: d => d.ring,
      getElevation: d => Math.sqrt(d.pop),
      getFillColor: d => TIER_COLORS[d.ti],
      getLineColor: [255, 255, 255, 120],
      transitions: {getElevation: {duration: 800, enter: () => [0]}, getFillColor: 600},
    });
  }
  return new deck.ColumnLayer({
    id: 'cols-' + st.scen + '-' + s.years[y],
    data: s.points.filter(p => p[2 + y] > 0),   // 빈 격자만 제외(인구 조건 없음)
    diskResolution: 4, radius: 660, angle: 45,
    extruded: true, pickable: true, elevationScale: elevScale,
    getPosition: d => [d[0], d[1]],
    getElevation: d => Math.sqrt(d[2 + y]),
    getFillColor: d => color(d[2 + y]),
    transitions: {getElevation: {duration: 800, enter: () => [0]}, getFillColor: 600},
  });
}

function onView({viewState: vs}) {
  viewState = vs;
  render();
}

// 마우스 휠(가운데 버튼) 드래그 = 회전 (좌우 bearing / 상하 pitch)
(() => {
  const maps = document.getElementById('maps');
  let drag = null;
  maps.addEventListener('mousedown', e => {
    if (e.button !== 1) return;
    e.preventDefault();                       // 휠 클릭 자동 스크롤 방지
    drag = {x: e.clientX, y: e.clientY,
            bearing: viewState.bearing, pitch: viewState.pitch};
  });
  window.addEventListener('mousemove', e => {
    if (!drag) return;
    viewState = {...viewState,
      bearing: drag.bearing + (e.clientX - drag.x) * 0.4,
      pitch: Math.max(0, Math.min(85, drag.pitch - (e.clientY - drag.y) * 0.4))};
    render();
  });
  window.addEventListener('mouseup', () => { drag = null; });
})();
function makeDeck(container, side) {
  return new deck.DeckGL({
    container, viewState, controller: true, onViewStateChange: onView,
    onClick: info => {   // 읍면동 클릭 → 강조 토글(같은 동 다시 클릭 시 해제)
      if (info && info.layer && info.layer.id === 'emd' && info.object) {
        const ix = info.object.properties.idx;
        emdSel = (emdSel === ix) ? -1 : ix;
        render();
      }
    },
    getTooltip: ({object}) => {
      if (!object) return null;
      if (object.properties && object.properties.name)   // 읍면동 오버레이
        return object.properties.name;
      const st = S[side], s = DATA[st.scen];
      if (s.type === 'center')
        return {html: '<b>' + TIER_NAMES[object.ti] + '</b>'
          + '<br>주거인구 ' + Math.round(object.pop).toLocaleString() + '명'
          + '<br>근무인구 ' + Math.round(object.popw || 0).toLocaleString() + '명'};
      const y = Math.min(st.yi, s.years.length - 1);
      return '인구 ' + Math.round(object[2 + y]).toLocaleString() + '명';
    },
  });
}
const decks = {A: makeDeck('mapA', 'A'), B: makeDeck('mapB', 'B')};

function cap(side) {
  const st = S[side], s = DATA[st.scen];
  return s.name + ' · ' + s.years[Math.min(st.yi, s.years.length - 1)] + '년';
}
function render() {
  document.getElementById('capA').textContent = cap('A');
  document.getElementById('capB').textContent = cap('B');
  const anyCtr = DATA[S.A.scen].type === 'center'
    || (compare && DATA[S.B.scen].type === 'center');
  document.getElementById('tierLegend').style.display = anyCtr ? 'block' : 'none';
  // 요약지표 버튼: '중심지 식별' 지도를 볼 때 + 요약이 산출된 경우에만 노출
  const _sb = document.getElementById('sumBtn');
  if (_sb) _sb.style.display =
    (SUMMARY && SUMMARY.years && SUMMARY.years.length && anyCtr) ? '' : 'none';
  decks.A.setProps({viewState, layers: [tileLayer(), emdLayer(), colLayer('A')].filter(Boolean)});
  if (compare) decks.B.setProps({viewState, layers: [tileLayer(), emdLayer(), colLayer('B')].filter(Boolean)});
}

function buildDots(side) {
  const el = document.getElementById('dots' + side);
  el.innerHTML = '';
  const st = S[side], yrs = DATA[st.scen].years;
  if (st.yi >= yrs.length) st.yi = yrs.length - 1;
  yrs.forEach((y, i) => {
    const d = document.createElement('span');
    d.className = 'dot' + (i === st.yi ? ' on' : '');
    d.textContent = y;
    d.onclick = () => { st.yi = i; buildDots(side); render(); };
    el.appendChild(d);
  });
}
function buildScenSelect(side) {
  const sel = document.getElementById('scen' + side);
  sel.innerHTML = '';
  DATA.forEach((s, i) => {
    const o = document.createElement('option');
    o.value = i; o.textContent = s.name;
    sel.appendChild(o);
  });
  sel.value = S[side].scen;
  sel.onchange = () => { S[side].scen = +sel.value; buildDots(side); render(); };
}

document.querySelectorAll('input[name=bg]').forEach(rb => {
  rb.onchange = () => {
    bg = rb.value;
    document.body.style.background = (bg === 'dark') ? '#101418' : '#c8c8c8';
    render();
  };
});
document.getElementById('cmp').onchange = e => {
  compare = e.target.checked;
  document.getElementById('mapB').style.display = compare ? 'block' : 'none';
  document.getElementById('secB').style.display = compare ? 'block' : 'none';
  document.getElementById('titleA').textContent = compare ? '왼쪽 지도' : '지도';
  render();
};

if (EMD) {
  document.getElementById('emdRow').style.display = '';
  document.getElementById('emdChk').onchange = e => { emdOn = e.target.checked; render(); };
}
buildScenSelect('A');
buildScenSelect('B');
S.B.yi = DATA[0].years.length - 1;   // 비교 기본: 왼쪽 첫 연도 / 오른쪽 마지막 연도
buildDots('A');
buildDots('B');
render();

// ===== 장래 중심지 전망 요약지표 (그림 4-14) =====
(function(){
  var btn=document.getElementById('sumBtn'), wrap=document.getElementById('sumWrap');
  if(!SUMMARY||!SUMMARY.years||!SUMMARY.years.length){ if(btn) btn.style.display='none'; return; }
  var Y=SUMMARY.years.map(String), B=SUMMARY.benchmarks||{}, drawn=false;
  var CFG={displayModeBar:false,responsive:true};
  var sub=document.getElementById('sumSub');
  if(sub) sub.textContent='기준연도·장래 추계 '+SUMMARY.years.join(' · ')+' · 점선 = 전국 기준';
  function mx(a){ return Math.max.apply(null, a.filter(function(v){return v!=null;})); }
  function bench(name,val,color,axis){ var t={type:'scatter',mode:'lines',name:name,
    x:Y,y:Y.map(function(){return val;}),line:{color:color,dash:'dash',width:1.3},hoverinfo:'skip'};
    if(axis) t.yaxis=axis; return t; }
  // 천단위 콤마
  function fmt(n){ return (n==null)?'':Math.round(n).toLocaleString('en-US'); }
  // 기준선 숫자를 선 옆(차트 안)에 색 라벨로 — side:'right'|'left', dy=세로 오프셋
  function annot(val,txt,color,axis,side,dy){ var left=(side==='left');
    return {xref:'paper',x:left?0.01:0.99,xanchor:left?'left':'right',
      y:val,yref:axis||'y',yshift:(dy==null?8:dy),text:txt,showarrow:false,
      font:{color:color,size:11},bgcolor:'rgba(255,255,255,0.65)'}; }
  function lay(extra){ return Object.assign({margin:{l:46,r:20,t:34,b:24},
    legend:{orientation:'h',y:1.1,yanchor:'bottom',x:0.5,xanchor:'center',font:{size:11}},
    font:{size:11}, xaxis:{type:'category'}},extra||{}); }
  function draw(){
    if(drawn) return; drawn=true;
    var base=SUMMARY.base_year;
    // 1. 중심지 수 — 기준연도 주황, 상단 여백으로 숫자 안 잘리게
    Plotly.newPlot('scCount',[{type:'bar',x:Y,y:SUMMARY.center_count,
      text:SUMMARY.center_count,textposition:'outside',cliponaxis:false,
      marker:{color:SUMMARY.years.map(function(y){return y===base?'#ff7f0e':'#5a7796';})}}],
      lay({yaxis:{range:[0,mx(SUMMARY.center_count)*1.18]},showlegend:false}),CFG);
    // 2. 면적 & 중심화율 — 이중축, y2 범위 보정
    var t2=[{type:'scatter',name:'중심지 면적',x:Y,y:SUMMARY.center_area,mode:'lines+markers',line:{color:'#5a7796'}}];
    var l2=lay({yaxis:{range:[0,mx(SUMMARY.center_area)*1.18]},
      yaxis2:{overlaying:'y',side:'right'}});
    if(SUMMARY.center_ratio){
      t2.push({type:'scatter',name:'중심화율',x:Y,y:SUMMARY.center_ratio,mode:'lines+markers',yaxis:'y2',line:{color:'#e8703a'}});
      t2.push(bench('전국',B.ratio,'#e8703a','y2'));
      l2.yaxis2.range=[0, Math.max(mx(SUMMARY.center_ratio),B.ratio)*1.4];
      l2.annotations=[annot(B.ratio,String(B.ratio),'#e8703a','y2','right')];
    }
    Plotly.newPlot('scArea',t2,l2,CFG);
    // 3. 인구 집중도 — 기준선 숫자 주석
    var t3=[], a3=[];
    if(SUMMARY.conc_r){ t3.push({type:'scatter',name:'주거 집중도',x:Y,y:SUMMARY.conc_r,mode:'lines+markers',line:{color:'#2166ac'}});
      t3.push(bench('전국 주거',B.conc_r,'#2166ac')); a3.push(annot(B.conc_r,String(B.conc_r),'#2166ac','y','right')); }
    if(SUMMARY.conc_w){ t3.push({type:'scatter',name:'근무 집중도',x:Y,y:SUMMARY.conc_w,mode:'lines+markers',line:{color:'#e8703a'}});
      t3.push(bench('전국 근무',B.conc_w,'#e8703a')); a3.push(annot(B.conc_w,String(B.conc_w),'#e8703a','y','right')); }
    if(t3.length) Plotly.newPlot('scConc',t3,lay({yaxis:{range:[0,100]},annotations:a3}),CFG);
    else document.getElementById('scConc').innerHTML='<p style="color:#888;font-size:12px;padding:24px">거주·근무 배분결과를 함께 로드하면 표시됩니다</p>';
    // 4. 인구 밀도 — 기준선 3종(주거·근무·DID) 숫자 주석(가까운 4241/4000은 좌우로 분리)
    var dmax=Math.max(mx(SUMMARY.dens_r),mx(SUMMARY.dens_w),B.dens_r,B.dens_w,B.did);
    var t4=[{type:'scatter',name:'주거밀도',x:Y,y:SUMMARY.dens_r,mode:'lines+markers',line:{color:'#2166ac'}},
            {type:'scatter',name:'근무밀도',x:Y,y:SUMMARY.dens_w,mode:'lines+markers',line:{color:'#e8703a'}},
            bench('전국 주거',B.dens_r,'#2166ac'),
            bench('전국 근무',B.dens_w,'#e8703a'),
            bench('일본 DID',B.did,'#2ca02c')];
    Plotly.newPlot('scDens',t4,lay({yaxis:{range:[0,dmax*1.12]},
      annotations:[annot(B.dens_r,fmt(B.dens_r),'#2166ac','y','right',10),
                   annot(B.dens_w,fmt(B.dens_w),'#e8703a','y','right',8),
                   annot(B.did,fmt(B.did),'#2ca02c','y','right',-10)]}),CFG);
  }
  btn.addEventListener('click',function(){ wrap.style.display='flex'; draw();
    setTimeout(function(){['scCount','scArea','scConc','scDens'].forEach(function(id){
      var e=document.getElementById(id); if(e&&e.data) Plotly.Plots.resize(e);});},80); });
  document.getElementById('sumClose').addEventListener('click',function(){ wrap.style.display='none'; });
  wrap.addEventListener('click',function(e){ if(e.target===wrap) wrap.style.display='none'; });
})();
</script>
</body>
</html>
'''
