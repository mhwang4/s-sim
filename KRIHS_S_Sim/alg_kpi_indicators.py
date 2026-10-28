# -*- coding: utf-8 -*-
"""
공간구조 진단 지표 계산 — 장래 추계 버전 (QGIS Processing Script Algorithm)

기준년도(현재 실측) + 장래 인구추계 시나리오(다중)로 지역/생활권/중심지 지표를 계산한다.
과거 버전(spatial_structure_indicators.py)의 집계 엔진·연결성 로직을 재사용하되,
시간축에 '시나리오' 차원을 추가하고, 초등학교 시설 생존/서비스권 지표 2개를 신설했다.

입력:
  - 시군구 경계 / 생활권 경계 / 중심지 폴리곤
  - 기준년도 격자 인구 (레이어 + 연도 + 주거/근무/ID 필드)
  - 장래 시나리오 상위폴더 (고급): 하위폴더=시나리오, 그 안 파일명에 연도 포함 → 자동 로딩
  - OD Matrix (선택): 연결성/접근 형평성 (도로망 고정, 인구만 변동)
  - 초등학교 시설 포인트 + 서비스반경 + 최소 배후인구 (선택): 시설 생존/향유도 지표

출력:
  - 생활권 지표 레이어 / 중심지 지표 레이어 / 시설 지표 레이어(포인트)
  - 지역 시계열 CSV(시나리오×시점) / 시설 요약 CSV
  - HTML 대시보드 (시나리오·목표시점 드롭다운)

작성: Claude (Anthropic).
"""

import csv
import json
import math
import os
import webbrowser
from collections import defaultdict

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterDefinition,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFile,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFileDestination,
    QgsProcessingException,
    QgsVectorLayer,
    QgsFields,
    QgsField,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsSpatialIndex,
    QgsDistanceArea,
    QgsCoordinateTransform,
    QgsCoordinateReferenceSystem,
    QgsProject,
    QgsWkbTypes,
    QgsProcessingParameterEnum,
)

# ---------------------------------------------------------------------------
# 보조 함수 (QGIS 비의존 — 단독 테스트 가능)
# ---------------------------------------------------------------------------
# 단계별 결과 파일 이름 (메뉴 이름 그대로) — 결과 CSV와 종합 리포트용 JSON
AREA_NAMES = (("basic", "기초통계"), ("compact", "콤팩트성"), ("network", "네트워크성"), ("life", "생활편리성"))
AREA_CSV_NAMES = tuple((k, n + "_결과.csv") for k, n in AREA_NAMES)
AREA_JSON_NAMES = tuple((k, n + "_결과.json") for k, n in AREA_NAMES)


# 단계별 결과를 합칠 때 각 단계가 맡는 항목 — 나머지(인구·밀도·중심지·지도 등)는 ① 기초통계 값
_AREA_OWN = {
    "compact": {"top": ("carbon",)},
    "network": {"top": ("conn_enabled", "conn_meta"), "scen": ("conn_ts",), "zone": ("conn",),
                "center": ("name", "tier", "tier_name", "mean_up", "mean_eq", "mean_dn", "conn_bands",
                           "tot_up", "tot_eq", "conn_all", "conn_up", "conn_eq", "conn_dn", "cnt_up", "cnt_eq")},
    "life": {"top": ("fac_enabled", "fac_type", "fac_groups", "fac_meta", "kpi_enabled", "life_table",
                     "kpi_region", "kpi_zones"),
             "scen": ("fac_ts", "fac_ts_g", "fac_items"), "zone": ("fac", "fac_g"), "center": ("suff_present",)},
}


def merge_payloads(parts):
    """①~④ 단계 결과 [(scope, dashboard dict), …] → 종합 리포트 한 개.

    바탕은 ① 기초통계(없으면 첫 결과), 그 위에 ②·③·④가 맡은 항목만 덮어쓴다.
    """
    parts = list(parts)
    base = dict(next((d for sc, d in parts if sc == "basic"), parts[0][1]))
    base["scenarios"] = [dict(sc_) for sc_ in base.get("scenarios", [])]
    for sc_ in base["scenarios"]:
        sc_["zones"] = [dict(z) for z in sc_.get("zones", [])]
    base["kpi_centers"] = [dict(c) for c in base.get("kpi_centers", [])]
    for sc, d in parts:
        own = _AREA_OWN.get(sc)
        if not own:
            continue
        for k in own.get("top", ()):
            if k in d:
                base[k] = d[k]
        for bs, ds in zip(base["scenarios"], d.get("scenarios", [])):
            for k in own.get("scen", ()):
                if k in ds:
                    bs[k] = ds[k]
            for bz, dz in zip(bs["zones"], ds.get("zones", [])):
                for k in own.get("zone", ()):
                    if k in dz:
                        bz[k] = dz[k]
        for bc, dc in zip(base["kpi_centers"], d.get("kpi_centers", [])):
            for k in own.get("center", ()):
                if k in dc:
                    bc[k] = dc[k]
    return base


def _num(value):
    """필드 값을 float 으로. NULL/빈값/문자는 None."""
    if value is None:
        return None
    try:
        if isinstance(value, str) and value.strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _change_metrics(series):
    """series: [(year_num, value), ...] (value=None 제외). 변화 지표 dict 또는 None."""
    pts = [(y, v) for (y, v) in series if v is not None]
    if len(pts) < 2:
        return None
    (y0, v0), (y1, v1) = pts[0], pts[-1]
    total = ((v1 - v0) / v0 * 100.0) if (v0 not in (0, None)) else None
    span = y1 - y0
    cagr = None
    if v0 and v0 > 0 and v1 > 0 and span > 0:
        cagr = ((v1 / v0) ** (1.0 / span) - 1.0) * 100.0
    return {"first": v0, "last": v1, "total_pct": total, "cagr_pct": cagr}


def _r(value, ndigits=2):
    """둥글기 + None 안전."""
    if value is None:
        return None
    try:
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return None


def _idkey(value):
    """OD ID / 격자 ID 매칭용 정규화 키 (정수/실수/문자 통일)."""
    if value is None:
        return None
    if isinstance(value, float):
        return str(int(value)) if value == int(value) else str(value)
    if isinstance(value, int):
        return str(value)
    s = str(value).strip()
    return s if s else None


def _parse_tier(value):
    """중심지 위계 값에서 차수(1/2/3) 추출. 예: '1차 중심지' → 1."""
    if value is None:
        return None
    for ch in str(value):
        if ch in "123":
            return int(ch)
    return None


# ---------------------------------------------------------------------------
# 연결성/접근 형평성 — 거리(도로망 고정)와 인구평가를 분리
# ---------------------------------------------------------------------------
def _od_min_distances(base_cell_zone, dests, od_iter):
    """OD 거리로 origin(거주격자 후보)별 위계별 최소거리 산출. 인구와 무관(도로망 고정).

    base_cell_zone: {gid: zone|None}  전체 기준년도 격자(=origin 후보)
    dests:          {gid: {"tier":1|2|3, "zone":z}}  목적지(중심지 내 격자)
    반환: reg_min{gid:[t1,t2,t3]}, zon_min{gid:[...]}  (없으면 INF)
    """
    INF = float("inf")
    reg_min = {g: [INF, INF, INF] for g in base_cell_zone}
    zon_min = {g: [INF, INF, INF] for g in base_cell_zone}
    for origin, dest, dist in od_iter:
        if origin not in reg_min or dist is None:
            continue
        d = dests.get(dest)
        if not d:
            continue
        ti = d["tier"] - 1
        if dist < reg_min[origin][ti]:
            reg_min[origin][ti] = dist
        oz = base_cell_zone[origin]
        if oz is not None and oz == d["zone"] and dist < zon_min[origin][ti]:
            zon_min[origin][ti] = dist
    return reg_min, zon_min


def _levels_of(present):
    """present: 오름차순 위계 리스트 → [(label, [tier_index,...]) ...] 누적."""
    out, inc = [], []
    for t in present:
        inc = inc + [t]
        out.append(("+".join("%d차" % x for x in inc), [x - 1 for x in inc]))
    return out


def _scope_eval(gids, minmap, present, cellpop, thr_m_list):
    """gids(거주격자, pop>0)에 대해 임계·누적위계별 연결성/형평성."""
    levels = _levels_of(present)
    n = len(gids)
    tot = sum(cellpop[g] for g in gids)
    conn_all, eq_all = [], []
    for thr in thr_m_list:
        crow, erow = [], []
        for _label, tidx in levels:
            nconn, unpop = 0, 0.0
            for g in gids:
                dmin = min(minmap[g][i] for i in tidx)
                if dmin <= thr:
                    nconn += 1
                else:
                    unpop += cellpop[g]
            crow.append(nconn / n * 100.0 if n > 0 else None)
            erow.append(100.0 - unpop / tot * 100.0 if tot > 0 else None)
        conn_all.append(crow)
        eq_all.append(erow)
    return {"levels": [l for l, _ in levels], "conn": conn_all, "equity": eq_all, "n": n}


def _eval_conn_snapshot(cellpop, cell_zone, reg_min, zon_min,
                        region_present, zone_present, thr_m_list):
    """한 시점(시나리오×연도) 인구 스냅샷으로 연결성/형평성 평가.

    cellpop: {gid: pop}  (해당 시점 격자 주거인구)
    반환: {"region": scope|None, "zones": {zid: scope|None}}
    """
    residents = [g for g, p in cellpop.items() if p and p > 0 and g in reg_min]
    region = (_scope_eval(residents, reg_min, region_present, cellpop, thr_m_list)
              if (region_present and residents) else None)
    zg = defaultdict(list)
    for g in residents:
        z = cell_zone.get(g)
        if z is not None:
            zg[z].append(g)
    zones = {}
    for z, gids in zg.items():
        present = sorted(zone_present.get(z, []))
        zones[z] = (_scope_eval(gids, zon_min, present, cellpop, thr_m_list)
                    if present else None)
    return {"region": region, "zones": zones}


def _eval_facility_snapshot(cellpop, cell_zone, fac_catch, fac_zone, min_pop):
    """한 시점 인구 스냅샷으로 시설 지표 평가. (직선반경 서비스권, 생존 시설 기준 향유도)

    fac_catch: {fid: set(cell_id)}  시설 반경 내 격자집합 (기하 고정 → 1회 계산)
    fac_zone:  {fid: zid|None}      시설 소재 생활권
    반환:
      {"per_fac": {fid: {"bp":배후인구, "alive":bool}},
       "region": {"cover":%, "mgmt":관리대상수, "alive":생존수, "total":시설수},
       "zones": {zid: {동일}}}
    """
    per_fac = {}
    served = set()          # 생존 시설 서비스권에 포함된 격자
    served_by_zone = defaultdict(set)
    for fid, cells in fac_catch.items():
        bp = sum(cellpop.get(c, 0.0) for c in cells)
        alive = bp >= min_pop
        per_fac[fid] = {"bp": bp, "alive": alive}
        if alive:
            served |= cells

    tot_pop = sum(cellpop.values())
    served_pop = sum(cellpop.get(c, 0.0) for c in served)
    region = {
        "cover": (served_pop / tot_pop * 100.0) if tot_pop > 0 else None,
        "mgmt": sum(1 for v in per_fac.values() if not v["alive"]),
        "alive": sum(1 for v in per_fac.values() if v["alive"]),
        "total": len(per_fac),
    }

    # 생활권별: 그 생활권 소재 시설 수/관리대상, 향유도는 그 생활권 격자 기준
    zpop = defaultdict(float)
    zserved = defaultdict(float)
    for c, p in cellpop.items():
        z = cell_zone.get(c)
        if z is None:
            continue
        zpop[z] += p
        if c in served:
            zserved[z] += p
    ztotal = defaultdict(int)
    zmgmt = defaultdict(int)
    zalive = defaultdict(int)
    for fid, info in per_fac.items():
        z = fac_zone.get(fid)
        if z is None:
            continue
        ztotal[z] += 1
        if info["alive"]:
            zalive[z] += 1
        else:
            zmgmt[z] += 1
    zones = {}
    for z in set(list(zpop.keys()) + list(ztotal.keys())):
        tp = zpop.get(z, 0.0)
        zones[z] = {
            "cover": (zserved.get(z, 0.0) / tp * 100.0) if tp > 0 else None,
            "mgmt": zmgmt.get(z, 0),
            "alive": zalive.get(z, 0),
            "total": ztotal.get(z, 0),
        }
    return {"per_fac": per_fac, "region": region, "zones": zones}


def _kde_mix(cell_xy, facs, radius, eps=1e-6, adj=1e-4, use_exp=True):
    """서비스 복합 집적도(지표1) — 격자점별 c2 = S × exp(H').
    cell_xy: (M,2) 셀 중심 좌표. facs: [(유형명, coords(N,2), weights(N,)), ...] 한 세트.
    quartic 커널 K(d)=(1-(d/r)^2)^2. 반환: Di, S, div, c2.
    """
    import numpy as np
    from scipy.spatial import cKDTree
    M = len(cell_xy); T = len(facs); r2 = float(radius) * float(radius)
    Di = np.full((M, max(T, 1)), adj, dtype=float)
    if M == 0:
        S = Di.sum(axis=1); return Di, S, np.zeros(0), np.zeros(0)
    ctree = cKDTree(cell_xy)
    for ti, (tn, coords, w) in enumerate(facs):
        if coords is None or len(coords) == 0:
            continue
        ftree = cKDTree(coords)
        for fi, cidx in enumerate(ftree.query_ball_tree(ctree, radius)):
            if not cidx:
                continue
            cidx = np.asarray(cidx, dtype=int)
            d2 = np.sum((cell_xy[cidx] - coords[fi]) ** 2, axis=1)
            k = 1.0 - d2 / r2
            k[k < 0] = 0.0
            k *= k
            Di[cidx, ti] += float(w[fi]) * k
    S = Di.sum(axis=1)
    P = Di / S[:, None]
    H = -np.sum(P * np.log(P + eps), axis=1)
    div = np.exp(H) if use_exp else (H / max(T, 1))
    return Di, S, div, S * div


def _road_node_engine(net, from_field, to_field, oneway_field):
    """검증본(지표2·4) 도로엔진: UP_FROM/UP_TO 노드 그래프 + 본토(최대성분).
    링크 시작/끝 노드 ID로 방향(Gd)·무방향(Gu) csr 구성. ONEWAY='0'이면 양방향.
    반환 dict{Gd,Gu,main_idx,tree,Nn} 또는 None.
    """
    try:
        import numpy as np
        from scipy.sparse import csr_matrix
        from scipy.sparse.csgraph import connected_components
        from scipy.spatial import cKDTree
        has_ow = bool(oneway_field) and net.fields().indexOf(oneway_field) >= 0
        node_id = {}; ncoords = []
        def _nid(k, xy):
            if k not in node_id:
                node_id[k] = len(ncoords); ncoords.append(xy)
            return node_id[k]
        er = []; ec = []; ewd = []; ur = []; uc = []; uw = []
        for f in net.getFeatures():
            gm = f.geometry()
            if gm is None or gm.isEmpty():
                continue
            if gm.isMultipart():
                mp = gm.asMultiPolyline()
                if not mp or not mp[0] or not mp[-1]:
                    continue
                s = mp[0][0]; e = mp[-1][-1]
            else:
                pl = gm.asPolyline()
                if len(pl) < 2:
                    continue
                s = pl[0]; e = pl[-1]
            L = gm.length()
            iu = _nid(str(f[from_field]), (s.x(), s.y())); iv = _nid(str(f[to_field]), (e.x(), e.y()))
            ow = str(f[oneway_field]) if has_ow else '0'
            er.append(iu); ec.append(iv); ewd.append(L)
            if ow == '0':
                er.append(iv); ec.append(iu); ewd.append(L)
            ur += [iu, iv]; uc += [iv, iu]; uw += [L, L]
        Nn = len(ncoords)
        if Nn == 0:
            return None
        carr = np.array(ncoords)
        Gd = csr_matrix((np.asarray(ewd), (np.asarray(er), np.asarray(ec))), shape=(Nn, Nn))
        Gu = csr_matrix((np.asarray(uw), (np.asarray(ur), np.asarray(uc))), shape=(Nn, Nn))
        _n, lab = connected_components(Gu, directed=False)
        big = int(np.argmax(np.bincount(lab)))
        main_idx = np.where(lab == big)[0]
        tree = cKDTree(carr[main_idx])
        return {"Gd": Gd, "Gu": Gu, "main_idx": main_idx, "tree": tree, "Nn": Nn}
    except Exception:
        return None


def _road_snap(engine, xy_array):
    """점(N,2)을 본토 노드에 스냅. 반환 (vid(N,), snap거리(N,))."""
    import numpy as np
    d0, i0 = engine["tree"].query(xy_array)
    return engine["main_idx"][np.asarray(i0)], np.asarray(d0, dtype=float)


# =====================================================================
# KPI 지표 설명 (단일 출처) — 플러그인 간략 안내 + HTML 상세표 공용
#   n=지표명 · d=정의 · m=산출 방법 · i=해석 관점 · u=단위
# =====================================================================
KPI_INDICATOR_DESC = [
    {"key": "basic", "label": "기초통계", "items": [
        {"n": "면적", "d": "공간단위의 면적", "m": "폴리곤 면적 합", "i": "규모 파악", "u": "km²"},
        {"n": "주거인구 수", "d": "공간단위 내 주거인구", "m": "격자 주거인구(pop_r) 합", "i": "인구 규모", "u": "명"},
        {"n": "근무인구 수", "d": "공간단위 내 근무(종사자)인구", "m": "격자 근무인구(pop_w) 합", "i": "일자리 규모", "u": "명"},
        {"n": "주거인구 밀도", "d": "단위면적당 주거인구", "m": "주거인구 ÷ 면적", "i": "거주 밀집도", "u": "인/km²"},
        {"n": "근무인구 밀도", "d": "단위면적당 근무인구", "m": "근무인구 ÷ 면적", "i": "일자리 밀집도", "u": "인/km²"},
    ]},
    {"key": "compact", "label": "콤팩트성", "items": [
        {"n": "중심지 수", "d": "공간단위 내 중심지 개수(위계별)", "m": "중심지 폴리곤 수", "i": "다핵 정도", "u": "개"},
        {"n": "중심지 면적·비율", "d": "중심지 면적과 권역 대비 비율", "m": "중심지 면적 ÷ 권역 면적", "i": "압축 정도", "u": "km²·%"},
        {"n": "중심지 주거·근무 인구 수·비율", "d": "중심지 내 인구와 권역 대비 비율", "m": "중심지 인구 ÷ 권역 인구", "i": "인구 집중", "u": "명·%"},
        {"n": "중심지 주거·근무 밀도", "d": "중심지 단위면적당 인구", "m": "중심지 인구 ÷ 중심지 면적", "i": "중심 밀집", "u": "인/km²"},
        {"n": "인구과소지역 비율", "d": "저밀도 격자의 비율", "m": "주거밀도 기준 이하 격자 ÷ 전체 격자", "i": "확산·과소화", "u": "%"},
        {"n": "1인당 탄소 총배출", "d": "인구 1인당 총배출(활동·주거·근무 분모)", "m": "총배출 ÷ 인구(탄소공간지도 격자코드 조인)", "i": "저탄소 효율", "u": "tCO₂eq/인"},
    ]},
    {"key": "network", "label": "네트워크성", "items": [
        {"n": "N분내 도달 상위 중심지 수·비율", "d": "임계시간 내 도달 가능한 상위 위계 중심지", "m": "도로 최단시간(40km/h) 상위 도달 수 ÷ 상위 총수", "i": "상위 접근성", "u": "개·%"},
        {"n": "N분내 도달 동급 중심지 수·비율", "d": "임계시간 내 도달 가능한 동급 위계 중심지", "m": "도로 최단시간 동급 도달 수 ÷ 동급 총수", "i": "동급 연계", "u": "개·%"},
        {"n": "중심지-접근 형평성", "d": "거주지→중심지 접근의 형평(격자→중심지 OD 필요)", "m": "임계시간 내 도달 거주인구 비율", "i": "접근 형평", "u": "%"},
    ]},
    {"key": "life", "label": "생활편리성", "items": [
        {"n": "천인당 시설수", "d": "인구 천 명당 시설 수", "m": "시설 수 ÷ 인구 × 1000", "i": "공급 수준", "u": "개/천인"},
        {"n": "중심지 내 시설 비율", "d": "해당 유형 시설 중 중심지 내 위치 비율", "m": "중심지 내 시설 ÷ 전체 시설 × 100", "i": "시설 집적", "u": "%"},
        {"n": "서비스권 향유도", "d": "서비스반경 내 배후인구 비율", "m": "서비스권 포함 인구 ÷ 인구 × 100", "i": "접근 보장", "u": "%"},
        {"n": "관리대상 시설 비율", "d": "배후인구 기준 미달 시설 비율", "m": "배후인구<기준 시설 ÷ 전체 × 100", "i": "취약 시설", "u": "%"},
        {"n": "평균 접근성", "d": "최근린 시설까지 평균 거리", "m": "격자별 최근린 시설 도로거리 단순평균", "i": "근접성", "u": "km"},
    ]},
]


class KpiSpatialStructureIndicators(QgsProcessingAlgorithm):

    # --- 파라미터 키 ---
    REGION_NAME = "REGION_NAME"
    CITY_TYPE = "CITY_TYPE"          # 도시위상(가이드라인 참조값용)
    TREND_TYPE = "TREND_TYPE"        # 인구추세 유형(가이드라인 참조값용)
    DENS_REF = "DENS_REF"            # 중심지 밀도 참고치(명/㎢)
    _CITY_TYPES = ["미지정", "거점도시", "강소도시", "자립도시"]
    _TREND_TYPES = ["미지정", "성장형", "성숙·안정형", "감소형"]
    SIGUNGU = "SIGUNGU"
    ZONES = "ZONES"
    ZONE_NAME = "ZONE_NAME"
    CENTERS = "CENTERS"
    CENTER_NAME = "CENTER_NAME"
    CENTER_HIER = "CENTER_HIER"
    CENTER_HIER_ORDER = "CENTER_HIER_ORDER"
    GRID_ID = "GRID_ID"
    OD_MATRIX = "OD_MATRIX"
    OD_ORIGIN = "OD_ORIGIN"
    OD_DEST = "OD_DEST"
    OD_DIST = "OD_DIST"
    FAC_NEIGH = "FAC_NEIGH"          # 근린시설(멀티)
    FAC_HUB = "FAC_HUB"              # 거점시설(멀티)
    NET = "NET"                     # 도로망(선택; 근접성 내부계산·향후)
    FAC_RADIUS = "FAC_RADIUS"        # 향유도 서비스반경(근린, 직선 m)
    FAC_RADIUS_HUB = "FAC_RADIUS_HUB"  # 향유도 서비스반경(거점, m)
    FAC_MINPOP = "FAC_MINPOP"        # 관리대상 최소 배후인구(근린)
    FAC_MINPOP_HUB = "FAC_MINPOP_HUB"  # 관리대상 최소 배후인구(거점)
    FAC_MINPOP_BY_TYPE = "FAC_MINPOP_BY_TYPE"  # 유형별 최소배후 override
    KDE_RADIUS = "KDE_RADIUS"        # 복합집적도 커널 반경
    SUFF_RADIUS = "SUFF_RADIUS"      # 충족도 도보권 반경
    BASE_GRID = "BASE_GRID"          # 기초현황 격자 레이어1(선택)
    BASE_FIELD = "BASE_FIELD"        # 기초현황 값 필드1(선택)
    BASE_LABEL = "BASE_LABEL"        # 기초현황 지표명1(라벨)
    BASE_UNIT = "BASE_UNIT"          # 기초현황 단위1
    BASE_GRID2 = "BASE_GRID2"
    BASE_FIELD2 = "BASE_FIELD2"
    BASE_LABEL2 = "BASE_LABEL2"
    BASE_UNIT2 = "BASE_UNIT2"
    BASE_GRID3 = "BASE_GRID3"
    BASE_FIELD3 = "BASE_FIELD3"
    BASE_LABEL3 = "BASE_LABEL3"
    BASE_UNIT3 = "BASE_UNIT3"
    # 탄소 배출 (원천 1개 — 격자코드 조인, 부문 컬럼 자동 감지)
    CO2_SRC = "CO2_SRC"              # 탄소 총배출 원천(격자; 선택)
    CO2_JOIN = "CO2_JOIN"            # 격자코드 필드명
    CO2_VALUE = "CO2_VALUE"          # 총배출량 필드명(예: exhst_vl)
    OPEN_HTML = "OPEN_HTML"
    # 고급
    SCEN_FOLDER = "SCEN_FOLDER"
    FUT_YEARS = "FUT_YEARS"
    FUT_RESID_FIELD = "FUT_RESID_FIELD"
    FUT_WORK_FIELD = "FUT_WORK_FIELD"
    SPARSE_THRESHOLD = "SPARSE_THRESHOLD"
    THRESHOLD_MIN = "THRESHOLD_MIN"

    SCOPE = "SCOPE"                 # 산출 범위: basic/compact/network/life/all
    OUT_AREA_CSV = "OUT_AREA_CSV"   # 영역별 결과 CSV(단계 산출)
    OUT_ZONES = "OUT_ZONES"
    OUT_CENTERS = "OUT_CENTERS"
    OUT_FACILITY = "OUT_FACILITY"
    OUT_REGION_CSV = "OUT_REGION_CSV"
    OUT_FAC_CSV = "OUT_FAC_CSV"
    OUT_HTML = "OUT_HTML"
    OUT_JSON = "OUT_JSON"           # 종합 리포트용 결과(JSON) — 단계 산출
    OUT_AREA_DIR = "OUT_AREA_DIR"   # 일괄 산출: 단계별 결과 CSV·JSON 4종씩 쓸 폴더
    RUN_TAG = "RUN_TAG"             # 실행 입력 기록(JSON 문자열) — 단계별 결과를 합칠 때 입력 일치 확인

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return KpiSpatialStructureIndicators()

    def name(self):
        return "spatial_structure_indicators_kpi"

    def displayName(self):
        return self.tr("공간구조 진단 지표 계산 (KPI·장래)")

    def group(self):
        return self.tr("4. KPI 산출")

    def groupId(self):
        return "spatial_structure"

    def shortHelpString(self):
        return self.tr(
            "기준년도(현재 실측) + 장래 인구추계 시나리오(다중)로 공간구조 진단 지표를 계산합니다.\n\n"
            "장래 시나리오 입력(고급): '시나리오 상위폴더'를 지정하면, 그 안의 각 하위폴더를 "
            "하나의 시나리오로 인식하고(폴더명=시나리오명), 하위폴더 안에서 파일명에 각 연도가 "
            "포함된 벡터 파일을 찾아 자동 로딩합니다. 필드명은 '장래 주거인구 필드명'(기본 pop_tot)을 공유.\n\n"
            "시설 지표(선택, 예: 도서관): 시설 포인트 + 종류명 + 서비스반경(직선, m) + 최소 배후인구를 주면, "
            "시나리오×시점별로 반경 내 배후인구가 최소규모 미만인 시설을 '관리대상'으로 표시하고, "
            "생존 시설 서비스권 내 인구 비중(향유도)과 관리대상 시설 수를 지역/생활권별로 집계합니다.\n\n"
            "연결성/접근 형평성은 현재 도로망(OD Matrix) 고정 가정하에 장래 인구 변화만 반영합니다."
        )

    # -----------------------------------------------------------------
    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterString(
            self.REGION_NAME, self.tr("지역명 (대시보드 제목)"),
            defaultValue="지역", optional=True))
        # 1. 시군구
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.SIGUNGU, self.tr("1. 시군구 경계 (지역 전체)"),
            [QgsProcessing.TypeVectorPolygon]))
        # 2. 생활권
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.ZONES, self.tr("2. 생활권 경계"), [QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterField(
            self.ZONE_NAME, self.tr("생활권 이름 필드 (병합 기준, 권장)"),
            parentLayerParameterName=self.ZONES, optional=True))
        # 3. 중심지
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.CENTERS, self.tr("3. 중심지 폴리곤"), [QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterField(
            self.CENTER_NAME, self.tr("중심지 이름 필드 (선택)"),
            parentLayerParameterName=self.CENTERS, optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.CENTER_HIER, self.tr("중심지 위계 필드 (예: 1차/2차/3차 중심지)"),
            parentLayerParameterName=self.CENTERS, optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.CENTER_HIER_ORDER,
            self.tr("중심지 위계 순서(상위→하위, 선택; 비우면 값에 적힌 차수, 차수가 없으면 수량 적은 위계가 고차)"),
            optional=True))
        # 4. 기준년도 격자
        self.addParameter(QgsProcessingParameterFeatureSource(
            "GRID1", self.tr("4. 기준년도 격자 인구 레이어 (현재 실측)"),
            [QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterNumber(
            "YEAR1", self.tr("기준년도 연도"),
            type=QgsProcessingParameterNumber.Integer, optional=True, defaultValue=None))
        self.addParameter(QgsProcessingParameterEnum(
            self.CITY_TYPE, self.tr("도시위상 (가이드라인 참조값 표시용)"),
            options=self._CITY_TYPES, defaultValue=0, optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.TREND_TYPE, self.tr("인구추세 유형 (가이드라인 참조값 표시용)"),
            options=self._TREND_TYPES, defaultValue=0, optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.DENS_REF, self.tr("중심지 밀도 참고치 (명/㎢, 참고치 미달 중심지 수 산출용)"),
            type=QgsProcessingParameterNumber.Double, defaultValue=2000.0, optional=True))
        self.addParameter(QgsProcessingParameterField(
            "RESID1", self.tr("기준년도 주거인구 필드"),
            parentLayerParameterName="GRID1",
            type=QgsProcessingParameterField.Numeric, optional=True))
        self.addParameter(QgsProcessingParameterField(
            "WORK1", self.tr("기준년도 근무인구 필드 (선택)"),
            parentLayerParameterName="GRID1",
            type=QgsProcessingParameterField.Numeric, optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.GRID_ID, self.tr("기준년도 격자 ID 필드 (장래/OD/시설 매칭용, 권장)"),
            parentLayerParameterName="GRID1", optional=True))
        # 5. OD Matrix
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.OD_MATRIX, self.tr("5. OD Matrix (격자중심점 간 거리, 선택)"),
            [QgsProcessing.TypeVector], optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.OD_ORIGIN, self.tr("OD Origin ID 필드"),
            parentLayerParameterName=self.OD_MATRIX, optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.OD_DEST, self.tr("OD Destination ID 필드"),
            parentLayerParameterName=self.OD_MATRIX, optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.OD_DIST, self.tr("OD 거리 필드 (m, 예: total_cost)"),
            parentLayerParameterName=self.OD_MATRIX, optional=True))
        # 6. 시설 (근린5·거점5 멀티 — 향유도류 + 우리 4지표 공통 입력)
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.FAC_NEIGH, self.tr("6. 저차시설 레이어들 (레이어명=유형, 선택)"),
            QgsProcessing.TypeVectorPoint, optional=True))
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.FAC_HUB, self.tr("   고차시설 레이어들 (레이어명=유형, 선택)"),
            QgsProcessing.TypeVectorPoint, optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.NET, self.tr("   도로망 (선택; 근접성 내부계산용)"),
            [QgsProcessing.TypeVectorLine], optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.KDE_RADIUS, self.tr("   복합집적도 커널 반경 (m)"),
            type=QgsProcessingParameterNumber.Double, defaultValue=2000.0, optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.SUFF_RADIUS, self.tr("   충족도 도보권 반경 (m)"),
            type=QgsProcessingParameterNumber.Double, defaultValue=1000.0, optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.FAC_RADIUS, self.tr("   향유도 서비스반경 — 근린 (직선, m)"),
            type=QgsProcessingParameterNumber.Double, defaultValue=1500.0, optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.FAC_RADIUS_HUB, self.tr("   향유도 서비스반경 — 거점 (직선, m)"),
            type=QgsProcessingParameterNumber.Double, defaultValue=3000.0, optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.FAC_MINPOP, self.tr("   관리대상 최소 배후인구 — 근린 (미만이면 관리대상)"),
            type=QgsProcessingParameterNumber.Double, defaultValue=500.0, optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.FAC_MINPOP_HUB, self.tr("   관리대상 최소 배후인구 — 거점"),
            type=QgsProcessingParameterNumber.Double, defaultValue=3000.0, optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.FAC_MINPOP_BY_TYPE, self.tr("   유형별 최소배후 override (예: 대학=30000, 병원=1000)"),
            optional=True))

        # 7. 기초현황 (선택, 최대 3개) — 격자 레이어의 값 필드를 생활권·계획권역으로 집계
        base_slots = [
            (self.BASE_GRID, self.BASE_FIELD, self.BASE_LABEL, self.BASE_UNIT,
             "7. 기초현황 격자 ① (선택; 예: 노후주택)", "노후주택 비율", "%"),
            (self.BASE_GRID2, self.BASE_FIELD2, self.BASE_LABEL2, self.BASE_UNIT2,
             "   기초현황 격자 ② (선택; 예: 토지이용 압축도)", "토지이용 압축도", "지수"),
            (self.BASE_GRID3, self.BASE_FIELD3, self.BASE_LABEL3, self.BASE_UNIT3,
             "   기초현황 격자 ③ (선택; 예: 토지이용 복합도)", "토지이용 복합도", "지수"),
        ]
        for gk, fk, lk, uk, glabel, dlabel, dunit in base_slots:
            self.addParameter(QgsProcessingParameterVectorLayer(
                gk, self.tr(glabel), optional=True))
            self.addParameter(QgsProcessingParameterField(
                fk, self.tr("   └ 값 필드 (선택)"),
                parentLayerParameterName=gk,
                type=QgsProcessingParameterField.Numeric, optional=True))
            self.addParameter(QgsProcessingParameterString(
                lk, self.tr("   └ 지표명 (라벨)"), defaultValue=dlabel, optional=True))
            self.addParameter(QgsProcessingParameterString(
                uk, self.tr("   └ 단위"), defaultValue=dunit, optional=True))

        # 8. 탄소 총배출 (탄소공간지도 원천 — 격자코드로 기준년도 격자에 조인)
        self.addParameter(QgsProcessingParameterFile(
            self.CO2_SRC, self.tr("8. 탄소 총배출 원천 (탄소공간지도 격자; 선택)"),
            extension="", optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.CO2_JOIN, self.tr("   └ 격자코드 필드명"),
            defaultValue="grid_cd", optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.CO2_VALUE, self.tr("   └ 총배출량 필드명 (예: exhst_vl)"),
            defaultValue="exhst_vl", optional=True))

        self.addParameter(QgsProcessingParameterBoolean(
            self.OPEN_HTML, self.tr("완료 후 HTML 대시보드 자동 열기"), defaultValue=True))

        # --- 고급: 장래 시나리오 폴더 + 임계 등 ---
        advanced_params = [
            QgsProcessingParameterFile(
                self.SCEN_FOLDER, self.tr("장래 시나리오 상위폴더 (하위폴더=시나리오)"),
                behavior=QgsProcessingParameterFile.Folder, optional=True),
            QgsProcessingParameterString(
                self.FUT_YEARS, self.tr("장래 연도 리스트 (예: 2030, 2035, 2040, 2045)"),
                optional=True),
            QgsProcessingParameterString(
                self.FUT_RESID_FIELD, self.tr("장래 주거인구 필드명"),
                defaultValue="pop_tot", optional=True),
            QgsProcessingParameterString(
                self.FUT_WORK_FIELD, self.tr("장래 근무인구 필드명 (선택)"),
                defaultValue="", optional=True),
            QgsProcessingParameterNumber(
                self.SPARSE_THRESHOLD, self.tr("인구과소 격자 기준 (주거인구 인/km² 이하)"),
                type=QgsProcessingParameterNumber.Double, defaultValue=20.0, optional=True),
            QgsProcessingParameterString(
                self.THRESHOLD_MIN,
                self.tr("연결성/접근 형평성 임계시간 리스트 (분, 최대 3개, 예: 20, 30, 40)"),
                defaultValue="30", optional=True),
        ]
        for p in advanced_params:
            p.setFlags(p.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
            self.addParameter(p)

        # 출력
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_ZONES, self.tr("생활권 지표 레이어"), QgsProcessing.TypeVectorPolygon))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_CENTERS, self.tr("중심지 지표 레이어"), QgsProcessing.TypeVectorPolygon))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_FACILITY, self.tr("시설 지표 레이어 (포인트)"),
            QgsProcessing.TypeVectorPoint, optional=True, createByDefault=False))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUT_REGION_CSV, self.tr("지역 시계열 CSV (시나리오×시점)"),
            fileFilter="CSV files (*.csv)", optional=True, createByDefault=False))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUT_FAC_CSV, self.tr("시설 요약 CSV"),
            fileFilter="CSV files (*.csv)", optional=True, createByDefault=False))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUT_AREA_CSV, self.tr("영역별 결과 CSV (단계 산출)"),
            fileFilter="CSV files (*.csv)", optional=True, createByDefault=False))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUT_HTML, self.tr("HTML 대시보드"),
            fileFilter="HTML files (*.html)", optional=True, createByDefault=False))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUT_JSON, self.tr("종합 리포트용 결과 (JSON)"),
            fileFilter="JSON files (*.json)", optional=True, createByDefault=False))
        ad = QgsProcessingParameterString(
            self.OUT_AREA_DIR, self.tr("일괄 산출 결과 폴더 (단계별 CSV·JSON)"), optional=True)
        ad.setFlags(ad.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(ad)
        rt = QgsProcessingParameterString(self.RUN_TAG, self.tr("실행 입력 기록"), optional=True)
        rt.setFlags(rt.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(rt)
        sp = QgsProcessingParameterString(
            self.SCOPE, self.tr("산출 범위 (basic/compact/network/life/all)"),
            defaultValue="all", optional=True)
        sp.setFlags(sp.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(sp)

    # -----------------------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):
        region_name = self.parameterAsString(parameters, self.REGION_NAME, context) or "대상 지역"

        sigungu = self.parameterAsSource(parameters, self.SIGUNGU, context)
        zones_src = self.parameterAsSource(parameters, self.ZONES, context)
        centers_src = self.parameterAsSource(parameters, self.CENTERS, context)
        if sigungu is None or zones_src is None or centers_src is None:
            raise QgsProcessingException("시군구/생활권/중심지 레이어는 필수입니다.")

        zone_name_field = self.parameterAsString(parameters, self.ZONE_NAME, context) or None
        center_name_field = self.parameterAsString(parameters, self.CENTER_NAME, context) or None
        center_hier_field = self.parameterAsString(parameters, self.CENTER_HIER, context) or None

        sparse_threshold = self.parameterAsDouble(parameters, self.SPARSE_THRESHOLD, context)
        if not sparse_threshold or sparse_threshold <= 0:
            sparse_threshold = 20.0

        region_crs = sigungu.sourceCrs()
        tctx = QgsProject.instance().transformContext()

        da = QgsDistanceArea()
        da.setSourceCrs(region_crs, tctx)
        ellip = QgsProject.instance().ellipsoid()
        da.setEllipsoid(ellip if ellip and ellip != "NONE" else "WGS84")

        def area_km2(geom):
            try:
                return da.measureArea(geom) / 1_000_000.0
            except Exception:
                return geom.area() / 1_000_000.0

        def transformer(src):
            crs = src.sourceCrs()
            if crs == region_crs:
                return None
            return QgsCoordinateTransform(crs, region_crs, tctx)

        # --- 시군구 전체 도형 (병합) ---
        feedback.pushInfo("시군구 경계 읽는 중…")
        ct = transformer(sigungu)
        region_geom = None
        for f in sigungu.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            if ct:
                g = QgsGeometry(g)
                g.transform(ct)
            region_geom = g if region_geom is None else region_geom.combine(g)
        if region_geom is None or region_geom.isEmpty():
            raise QgsProcessingException("시군구 경계 도형이 비어 있습니다.")
        region_area = area_km2(region_geom)

        # --- 생활권 (이름 병합) ---
        feedback.pushInfo("생활권 경계 읽는 중…")
        ct = transformer(zones_src)
        zone_geoms, zone_meta = {}, {}
        zone_index = QgsSpatialIndex()
        if zone_name_field:
            grouped, order = {}, []
            for f in zones_src.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                if ct:
                    g = QgsGeometry(g)
                    g.transform(ct)
                nm = f[zone_name_field]
                nm = str(nm) if nm not in (None, "") else "생활권"
                if nm in grouped:
                    grouped[nm] = grouped[nm].combine(g)
                else:
                    grouped[nm] = g
                    order.append(nm)
            for k, nm in enumerate(order):
                zone_geoms[k] = grouped[nm]
                zone_meta[k] = {"name": nm, "area": area_km2(grouped[nm])}
                ix = QgsFeature(k)
                ix.setGeometry(grouped[nm])
                zone_index.addFeature(ix)
            feedback.pushInfo("생활권 %d개 (이름 기준 병합)" % len(order))
        else:
            for k, f in enumerate(zones_src.getFeatures()):
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                if ct:
                    g = QgsGeometry(g)
                    g.transform(ct)
                zone_geoms[k] = g
                zone_meta[k] = {"name": "생활권 %d" % (k + 1), "area": area_km2(g)}
                ix = QgsFeature(k)
                ix.setGeometry(g)
                zone_index.addFeature(ix)
            feedback.pushWarning("생활권 이름 필드 미지정 → 병합 안 함. 중복 폴리곤 주의.")

        # 생활권 이름이 숫자(upper_id 등)뿐이면 A/B/C… 로 재명명 (지역 무관 일관 지칭)
        def _excel_col(n):
            s = ""; n += 1
            while n > 0:
                n, r = divmod(n - 1, 26); s = chr(65 + r) + s
            return s

        def _isnum(x):
            try:
                float(x); return True
            except (TypeError, ValueError):
                return False
        _znames = [zone_meta[k]["name"] for k in zone_geoms]
        if _znames and all(_isnum(n) for n in _znames):
            _order = sorted(zone_geoms, key=lambda k: float(zone_meta[k]["name"]))
            for _i, _k in enumerate(_order):
                zone_meta[_k]["name"] = "생활권 " + _excel_col(_i)
            feedback.pushInfo("생활권 이름이 숫자 → A/B/C… 로 재명명(%d개)." % len(_order))

        # --- 중심지 ---
        feedback.pushInfo("중심지 폴리곤 읽는 중…")
        ct = transformer(centers_src)
        center_geoms, center_meta = {}, {}
        center_index = QgsSpatialIndex()
        # 중심지 레이어에 'year' 필드가 있으면(장래 중심지 식별 결과) 여러 연도 폴리곤이
        # 섞여 있으므로, KPI는 '고정 경계' 한 벌만 써야 함 → 가장 최신(장래) 연도만 사용.
        _cfields = [fld.name() for fld in centers_src.fields()]
        _center_year = None
        if 'year' in _cfields:
            _yrs = []
            for f in centers_src.getFeatures():
                try:
                    _yrs.append(int(f['year']))
                except (TypeError, ValueError):
                    pass
            if _yrs:
                _center_year = max(_yrs)
                feedback.pushInfo("중심지 레이어에 연도 다수 — KPI는 최신 연도(%d) 경계만 사용(고정 경계)." % _center_year)
        k = 0
        for f in centers_src.getFeatures():
            if _center_year is not None:
                try:
                    if int(f['year']) != _center_year:
                        continue
                except (TypeError, ValueError):
                    continue
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            if ct:
                g = QgsGeometry(g)
                g.transform(ct)
            center_geoms[k] = g
            name = f[center_name_field] if (center_name_field and center_name_field in f.fields().names()) else None
            hraw = f[center_hier_field] if (center_hier_field and center_hier_field in f.fields().names()) else None
            hraw = str(hraw) if hraw not in (None, "") else None
            center_meta[k] = {
                "name": str(name) if name not in (None, "") else "중심지 %d" % (k + 1),
                "area": area_km2(g), "tier": None, "hier_raw": hraw,
            }
            ix = QgsFeature(k)
            ix.setGeometry(g)
            center_index.addFeature(ix)
            k += 1

        # --- 위계 인식: 위계 필드의 서로 다른 값 = 각각 하나의 위계.
        #     (1) 사용자가 '위계 순서'를 적었으면 그 순서대로 1·2·3차,
        #     (2) 값마다 차수(1/2/3)가 적혀 있으면 그 차수 그대로 — 중심지 위계 분류 결과('1차중심지' 등).
        #         없는 위계는 비워 둠(예: 2차가 없으면 1차·3차),
        #     (3) 그 밖에는 수량 적은 위계를 고차로. ---
        order_raw = self.parameterAsString(parameters, self.CENTER_HIER_ORDER, context) or ""
        manual_order = [t.strip() for t in order_raw.replace(";", ",").split(",") if t.strip()]
        hier_count = {}
        for m in center_meta.values():
            hv = m.get("hier_raw")
            if hv not in (None, "", "None"):
                hier_count[hv] = hier_count.get(hv, 0) + 1
        if hier_count:
            parsed = {h: _parse_tier(h) for h in hier_count}
            if manual_order:
                ordered = [h for h in manual_order if h in hier_count]
                ordered += sorted([h for h in hier_count if h not in ordered], key=lambda h: hier_count[h])
                how = "사용자 지정"
                tier_map = {h: i + 1 for i, h in enumerate(ordered)}
            elif all(parsed.values()) and len(set(parsed.values())) == len(parsed):
                ordered = sorted(hier_count, key=lambda h: parsed[h])
                how = "위계 값의 차수 그대로"
                tier_map = parsed
            else:
                ordered = sorted(hier_count.keys(), key=lambda h: hier_count[h])  # 적을수록 고차
                how = "수량순(적을수록 고차)"
                tier_map = {h: i + 1 for i, h in enumerate(ordered)}
            for m in center_meta.values():
                m["tier"] = tier_map.get(m.get("hier_raw"))
            feedback.pushInfo("중심지 위계 %d종 인식(%s, 상위→하위): %s"
                              % (len(ordered), how,
                                 " > ".join("%s(%d차·%d개)" % (h, tier_map[h], hier_count[h]) for h in ordered)))
        else:
            feedback.pushInfo("중심지 위계 값이 없어 위계 미구분(연결성은 단일 위계로).")

        center_count = len(center_geoms)
        center_area_total = sum(m["area"] for m in center_meta.values())
        center_ratio = (center_area_total / region_area * 100.0) if region_area > 0 else None

        def locate(point_geom, index, geoms):
            for cand in index.intersects(point_geom.boundingBox()):
                if geoms[cand].contains(point_geom):
                    return cand
            return None

        # 중심지 → 소속 생활권 (내부점 기준, 없으면 가장 많이 겹치는 생활권)
        center_zone = {}
        for cid, cg in center_geoms.items():
            zid = locate(cg.pointOnSurface(), zone_index, zone_geoms)
            if zid is None:
                best, best_a = None, 0.0
                for cand in zone_index.intersects(cg.boundingBox()):
                    inter = zone_geoms[cand].intersection(cg)
                    a = inter.area() if inter is not None and not inter.isEmpty() else 0.0
                    if a > best_a:
                        best, best_a = cand, a
                zid = best
            center_zone[cid] = zid

        # =================================================================
        # 기준년도 격자에서 격자 기하/중심점/생활권/중심지/ID 캐시 구축 (1회)
        # =================================================================
        grid1 = self.parameterAsSource(parameters, "GRID1", context)
        if grid1 is None:
            raise QgsProcessingException("기준년도 격자 인구 레이어는 필수입니다.")
        resid1 = self.parameterAsString(parameters, "RESID1", context) or None
        if not resid1:
            raise QgsProcessingException("기준년도 주거인구 필드가 지정되지 않았습니다.")
        work1 = self.parameterAsString(parameters, "WORK1", context) or None
        year1 = self.parameterAsInt(parameters, "YEAR1", context)
        base_year_num = year1 if (year1 is not None and year1 > 0) else 0
        base_label = str(year1) if base_year_num else "기준년도"
        base_year_label = base_label   # 탄소 연도 표기용 (기초현황 루프의 base_label 재사용과 분리)
        grid_id_field = self.parameterAsString(parameters, self.GRID_ID, context) or None

        feedback.pushInfo("기준년도 격자 캐시 구축 중…")
        gt = transformer(grid1)
        cell_zone = {}       # {cell_id: zid|None}
        cell_center = {}     # {cell_id: cid|None}
        cell_area = {}       # {cell_id: km2}
        cell_centroid = {}   # {cell_id: QgsPointXY (region_crs)}
        cell_pop_r = {}      # {cell_id: 기준년도 주거인구} (탄소 1인당용)
        cell_pop_w = {}      # {cell_id: 기준년도 근무인구} (탄소 1인당용)
        base_cells = []      # 순서 보존
        has_id = bool(grid_id_field and grid_id_field in grid1.fields().names())
        if not has_id:
            feedback.pushWarning("격자 ID 필드가 없어 장래/OD/시설 매칭이 제한됩니다. (권장: ID 필드 지정)")
        # centroid 공간 인덱스 (시설 반경 계산용)
        cell_pt_index = QgsSpatialIndex()
        idx_to_cell = {}
        auto = 0
        for f in grid1.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            if gt:
                g = QgsGeometry(g)
                g.transform(gt)
            cid_key = _idkey(f[grid_id_field]) if has_id else None
            if cid_key is None:
                cid_key = "_auto%d" % auto
                auto += 1
            c = g.centroid().asPoint()
            pt = QgsGeometry.fromPointXY(QgsPointXY(c))
            cell_centroid[cid_key] = QgsPointXY(c)
            cell_area[cid_key] = area_km2(g)
            cell_zone[cid_key] = locate(pt, zone_index, zone_geoms)
            cell_center[cid_key] = locate(pt, center_index, center_geoms)
            cell_pop_r[cid_key] = _num(f[resid1]) if resid1 in f.fields().names() else 0.0
            cell_pop_w[cid_key] = _num(f[work1]) if (work1 and work1 in f.fields().names()) else 0.0
            base_cells.append(cid_key)
            ixf = QgsFeature(len(idx_to_cell))
            ixf.setGeometry(pt)
            idx_to_cell[len(idx_to_cell)] = cid_key
            cell_pt_index.addFeature(ixf)
        base_id_set = set(base_cells)
        feedback.pushInfo("기준년도 격자 %d개 캐시 완료." % len(base_cells))

        # =================================================================
        # 시나리오 수집: baseline(기준년도만) + 시나리오별 [기준년도 + 장래연도]
        # =================================================================
        scen_folder = self.parameterAsString(parameters, self.SCEN_FOLDER, context) or None
        fut_years_raw = self.parameterAsString(parameters, self.FUT_YEARS, context) or ""
        fut_resid = self.parameterAsString(parameters, self.FUT_RESID_FIELD, context) or "pop_tot"
        fut_work = self.parameterAsString(parameters, self.FUT_WORK_FIELD, context) or None
        fut_years = [t.strip() for t in fut_years_raw.replace(";", ",").replace(" ", ",").split(",") if t.strip()]

        self._layer_refs = []  # GC 방지
        vector_ext = (".shp", ".gpkg", ".geojson", ".json")

        # scenarios: [{"name":.., "slots":[{grid_src, resid, work, year_num, label, is_base}]}]
        scenarios = []

        def base_slot():
            return {"grid": grid1, "resid": resid1, "work": work1,
                    "year_num": base_year_num if base_year_num else -1,
                    "label": base_label, "is_base": True}

        if scen_folder and fut_years:
            if not os.path.isdir(scen_folder):
                raise QgsProcessingException("시나리오 상위폴더 경로가 올바르지 않습니다: %s" % scen_folder)
            subdirs = [d for d in sorted(os.listdir(scen_folder))
                       if os.path.isdir(os.path.join(scen_folder, d))]
            if not subdirs:
                feedback.pushWarning("상위폴더에 시나리오 하위폴더가 없습니다: %s" % scen_folder)
            for sd in subdirs:
                sdir = os.path.join(scen_folder, sd)
                files = [f for f in os.listdir(sdir) if f.lower().endswith(vector_ext)]
                slots = [base_slot()]
                for yv in fut_years:
                    matches = [f for f in files if yv in f]
                    if not matches:
                        feedback.pushWarning("[%s/%s] 파일명에 '%s' 포함 벡터 없음 → 건너뜀." % (sd, yv, yv))
                        continue
                    if len(matches) > 1:
                        feedback.pushWarning("[%s/%s] 후보 여러 개, 첫 번째 사용: %s" % (sd, yv, matches[0]))
                    fpath = os.path.join(sdir, matches[0])
                    layer = QgsVectorLayer(fpath, matches[0], "ogr")
                    if not layer.isValid():
                        feedback.pushWarning("[%s/%s] 레이어 열기 실패 → 건너뜀: %s" % (sd, yv, matches[0]))
                        continue
                    if layer.fields().indexOf(fut_resid) < 0:
                        feedback.pushWarning("[%s/%s] 주거인구 필드 '%s' 없음 → 건너뜀." % (sd, yv, fut_resid))
                        continue
                    wf = fut_work if (fut_work and layer.fields().indexOf(fut_work) >= 0) else None
                    self._layer_refs.append(layer)
                    slots.append({"grid": layer, "resid": fut_resid, "work": wf,
                                  "year_num": int(yv) if yv.isdigit() else (9000 + len(slots)),
                                  "label": yv, "is_base": False})
                    feedback.pushInfo("[%s/%s] 로딩: %s (주거=%s, 근무=%s)" % (sd, yv, matches[0], fut_resid, wf or "-"))
                if len(slots) >= 2:
                    scenarios.append({"name": sd, "slots": slots})
                else:
                    feedback.pushWarning("[%s] 유효한 장래 시점이 없어 시나리오에서 제외." % sd)

        if not scenarios:
            # 시나리오 없으면 기준년도 단일 시나리오라도 산출
            feedback.pushWarning("장래 시나리오가 없어 기준년도 단일 시점으로만 산출합니다.")
            scenarios.append({"name": "기준", "slots": [base_slot()]})

        # OD/시설 매칭에 ID 필수 안내
        if not base_id_set:
            feedback.pushWarning("격자 ID가 없어 장래 데이터 ID 추출/OD/시설 지표가 제한됩니다.")

        # =================================================================
        # OD 거리(도로망 고정) 1회 계산 → 시나리오×시점 재평가
        # =================================================================
        od_src = self.parameterAsSource(parameters, self.OD_MATRIX, context)
        od_origin_f = self.parameterAsString(parameters, self.OD_ORIGIN, context) or None
        od_dest_f = self.parameterAsString(parameters, self.OD_DEST, context) or None
        od_dist_f = self.parameterAsString(parameters, self.OD_DIST, context) or None
        thr_raw = self.parameterAsString(parameters, self.THRESHOLD_MIN, context) or "30"
        threshold_mins = []
        for t in thr_raw.replace(";", ",").replace(" ", ",").split(","):
            t = t.strip()
            if t:
                try:
                    v = float(t)
                    if v > 0:
                        threshold_mins.append(v)
                except ValueError:
                    pass
        threshold_mins = threshold_mins[:3] or [30.0]
        thr_m_list = [m / 10.0 * 5.0 * 1000.0 for m in threshold_mins]  # 분→km(10분당5km)→m

        reg_min = zon_min = None
        region_present = []
        zone_present = {}
        conn_enabled = False
        have_tier = any(center_meta[c].get("tier") for c in center_geoms)
        if od_src is not None and has_id and center_hier_field and have_tier \
                and od_origin_f and od_dest_f and od_dist_f:
            feedback.pushInfo("OD 거리 계산 중 (도로망 고정, 임계 %s분)…"
                              % ",".join("%g" % m for m in threshold_mins))
            dests = {}
            for cell_id, cid in cell_center.items():
                if cid is not None:
                    t = center_meta[cid].get("tier")
                    if t:
                        dests[cell_id] = {"tier": t, "zone": cell_zone.get(cell_id)}

            def _od_rows():
                for f in od_src.getFeatures():
                    yield (_idkey(f[od_origin_f]), _idkey(f[od_dest_f]), _num(f[od_dist_f]))

            # [진단] ID 매칭 점검 (연결성/형평성 값이 안 나올 때 원인 파악용)
            grid_ids = list(cell_zone.keys())
            dest_ids = list(dests.keys())
            feedback.pushInfo("[진단] 거주격자 후보 %d개 · 목적지(중심지 내 격자) %d개" % (len(grid_ids), len(dest_ids)))
            feedback.pushInfo("[진단] 격자 ID 예: %s" % grid_ids[:5])
            feedback.pushInfo("[진단] 목적지 ID 예: %s" % dest_ids[:5])
            _tot = _oin = _din = _match = _dist_ok = 0
            _samp = []
            for _o, _d, _dist in _od_rows():
                _tot += 1
                _o_ok = _o in cell_zone
                _d_ok = _d in dests
                if _o_ok:
                    _oin += 1
                if _d_ok:
                    _din += 1
                if _dist is not None:
                    _dist_ok += 1
                if _o_ok and _d_ok and _dist is not None:
                    _match += 1
                if len(_samp) < 5:
                    _samp.append((_o, _d, _dist))
            feedback.pushInfo("[진단] OD 행 %d개 · origin 격자매칭 %d · dest 목적지매칭 %d · 거리값 있음 %d · 최종매칭 %d"
                              % (_tot, _oin, _din, _dist_ok, _match))
            feedback.pushInfo("[진단] OD 예(origin, dest, 거리): %s" % _samp)
            if _match == 0:
                if _tot == 0:
                    feedback.pushWarning("[진단] OD 레이어에 행이 없음 → OD Matrix 입력/CSV 로딩 확인.")
                if len(dest_ids) == 0:
                    feedback.pushWarning("[진단] 목적지 격자 0개 → 중심지 폴리곤 안에 격자 중심점이 없거나 '중심지 위계' 값에서 1/2/3차가 안 읽힘.")
                if _tot and _oin == 0:
                    feedback.pushWarning("[진단] OD origin ID가 격자 ID와 전혀 안 맞음 → OD Origin 필드 선택/인코딩(CSV는 UTF-8)/gid 형식 확인.")
                if _tot and _din == 0 and len(dest_ids) > 0:
                    feedback.pushWarning("[진단] OD destination ID가 '중심지 내 격자 ID'와 안 맞음 → OD dest가 격자ID가 아니거나 중심지 밖을 가리킴.")
                if _tot and _oin > 0 and _din > 0 and _dist_ok == 0:
                    feedback.pushWarning("[진단] ID는 맞는데 거리값이 전부 비어있음 → OD 거리 필드 선택 확인(예: total_cost).")

            reg_min, zon_min = _od_min_distances(cell_zone, dests, _od_rows())
            region_present = sorted({d["tier"] for d in dests.values()})
            zp = defaultdict(set)
            for d in dests.values():
                if d["zone"] is not None:
                    zp[d["zone"]].add(d["tier"])
            zone_present = dict(zp)
            conn_enabled = bool(region_present)
            feedback.pushInfo("OD 거리 계산 완료 (목적지 격자 %d개)." % len(dests))
        elif od_src is not None:
            feedback.pushWarning("연결성/형평성 건너뜀 — OD/격자 ID/중심지 위계/OD 필드가 모두 필요합니다.")

        # =================================================================
        # 시설(초등학교) 반경 격자집합(기하 고정) 1회 계산
        # =================================================================
        neigh_layers = self.parameterAsLayerList(parameters, self.FAC_NEIGH, context) or []
        hub_layers = self.parameterAsLayerList(parameters, self.FAC_HUB, context) or []
        fac_radius = self.parameterAsDouble(parameters, self.FAC_RADIUS, context)
        fac_radius_hub = self.parameterAsDouble(parameters, self.FAC_RADIUS_HUB, context)
        fac_minpop = self.parameterAsDouble(parameters, self.FAC_MINPOP, context)
        fac_minpop_hub = self.parameterAsDouble(parameters, self.FAC_MINPOP_HUB, context)
        if not fac_radius or fac_radius <= 0:
            fac_radius = 1500.0
        if not fac_radius_hub or fac_radius_hub <= 0:
            fac_radius_hub = fac_radius
        if not fac_minpop or fac_minpop < 0:
            fac_minpop = 0.0
        if not fac_minpop_hub or fac_minpop_hub < 0:
            fac_minpop_hub = fac_minpop
        fac_radius_by_cat = {"근린": fac_radius, "거점": fac_radius_hub}
        fac_minpop_by_cat = {"근린": fac_minpop, "거점": fac_minpop_hub}

        def _clean_type(nm):
            return str(nm).split(" — ")[0].replace("_전남광주_5179", "").strip()

        fac_catch = {}     # {fid: set(cell_id)}  (전체 풀; 향유도 전체용)
        fac_meta = {}      # {fid: {"name":.., "zone":zid, "point":.., "type":.., "cat":..}}
        fac_zone = {}
        fac_pts_by_type = {}   # {유형: {"cat":근린/거점, "pts":[(QgsPointXY,w)]}}  (우리 4지표용)
        fac_enabled = False
        fid = 0
        if (neigh_layers or hub_layers) and not has_id:
            feedback.pushWarning(
                "시설 지표 건너뜀 — 격자 ID 필드가 있어야 시나리오별 격자 인구를 시설 서비스권에 매칭할 수 있습니다.")
        if (neigh_layers or hub_layers) and has_id:
            feedback.pushInfo("시설 서비스권(근린 %g m · 거점 %g m) 격자집합 계산 중…"
                              % (fac_radius, fac_radius_hub))
            for cat, layers in [("근린", neigh_layers), ("거점", hub_layers)]:
                rad = fac_radius_by_cat.get(cat, fac_radius)
                for lyr in layers:
                    tname = _clean_type(lyr.name())
                    fct = transformer(lyr)
                    entry = fac_pts_by_type.setdefault(tname, {"cat": cat, "pts": []})
                    for f in lyr.getFeatures():
                        g = f.geometry()
                        if g is None or g.isEmpty():
                            continue
                        if fct:
                            g = QgsGeometry(g)
                            g.transform(fct)
                        p = g.centroid().asPoint()
                        pxy = QgsPointXY(p)
                        entry["pts"].append((pxy, 1.0))   # 가중치 옵션 제거 — 균일 1
                        rect = QgsGeometry.fromPointXY(pxy).boundingBox()
                        rect.grow(rad)
                        cells = set()
                        for cand in cell_pt_index.intersects(rect):
                            ck = idx_to_cell[cand]
                            cp = cell_centroid[ck]
                            if math.hypot(cp.x() - p.x(), cp.y() - p.y()) <= rad:
                                cells.add(ck)
                        zid = locate(QgsGeometry.fromPointXY(pxy), zone_index, zone_geoms)
                        fac_catch[fid] = cells
                        fac_meta[fid] = {"name": "%s %d" % (tname, fid), "zone": zid,
                                         "point": pxy, "type": tname, "cat": cat}
                        fac_zone[fid] = zid
                        fid += 1
                    feedback.pushInfo("  [%s] %s: %d개" % (cat, tname, len(entry["pts"])))
            fac_enabled = len(fac_catch) > 0
            feedback.pushInfo("시설 %d유형 · 총 %d개 · 반경 내 격자집합 계산 완료."
                              % (len(fac_pts_by_type), len(fac_catch)))
        fac_type = "생활서비스 시설"   # 전체 라벨

        # 시설 유형/구분별 그룹 (향유도·배후인구 드롭박스용)
        #   그룹 키:  전체 / 근린 / 거점 / t:<유형명>
        fac_groups = {}
        fac_group_list = []
        if fac_enabled:
            fac_groups["전체"] = list(fac_catch.keys())
            fac_group_list.append({"key": "전체", "label": "전체 시설", "cat": "전체"})
            for _cat in ("근린", "거점"):
                _fids = [fid for fid in fac_catch if fac_meta[fid]["cat"] == _cat]
                if _fids:
                    fac_groups[_cat] = _fids
                    fac_group_list.append({"key": _cat, "label": {"근린": "저차", "거점": "고차"}.get(_cat, _cat) + " 전체", "cat": _cat})
            for _tn, _dtt in fac_pts_by_type.items():
                _fids = [fid for fid in fac_catch if fac_meta[fid]["type"] == _tn]
                if _fids:
                    _gk = "t:" + _tn
                    fac_groups[_gk] = _fids
                    fac_group_list.append({"key": _gk, "label": _tn, "cat": _dtt["cat"]})

        # =================================================================
        # KPI 4지표 (기준년도 스냅샷; 시나리오 불변) — 생활권/중심지 부착
        #   집적도(KDE)·근접성(직선 인구가중)·충족도(도보권 존재)·위계연결성(직선 시간권)
        #   ※ 근접성·위계연결성은 우선 직선거리(도로거리는 추후 scipy 내부계산으로 교체)
        # =================================================================
        kde_radius = self.parameterAsDouble(parameters, self.KDE_RADIUS, context) or 2000.0
        suff_radius = self.parameterAsDouble(parameters, self.SUFF_RADIUS, context) or 1000.0
        kpi_zones = {zid: {} for zid in zone_geoms}
        kpi_centers = {}
        kpi_region = {}
        life_table = None   # 생활편리성 표(Sheet2): 시설유형 × 공간단위
        kpi_enabled = bool(fac_pts_by_type)   # 시설 관련 지표 가용 여부(HTML/payload용)
        # 연결성(네트워크성)은 도로망만 있어도 산출 → 시설 없이도 블록 실행
        _net_present = self.parameterAsVectorLayer(parameters, self.NET, context) is not None
        if kpi_enabled or _net_present:
            import numpy as _np
            from scipy.spatial import cKDTree as _KD
            feedback.pushInfo("=== KPI 4지표 계산 (집적도·근접성·충족도·위계연결성) ===")
            cell_pop_base = {}
            for f in grid1.getFeatures():
                cidk = _idkey(f[grid_id_field]) if has_id else None
                if cidk is None:
                    continue
                try:
                    cell_pop_base[cidk] = float(f[resid1]) if f[resid1] not in (None, "") else 0.0
                except (TypeError, ValueError):
                    cell_pop_base[cidk] = 0.0
            cids = list(base_cells)
            cxy = _np.array([[cell_centroid[c].x(), cell_centroid[c].y()] for c in cids], dtype=float)
            zof = _np.array([cell_zone.get(c) for c in cids], dtype=object)
            popv = _np.array([cell_pop_base.get(c, 0.0) for c in cids], dtype=float)

            def _cat_sets(cat):
                out = []
                for tn, d in fac_pts_by_type.items():
                    if d["cat"] == cat and d["pts"]:
                        out.append((tn, _np.array([[p.x(), p.y()] for p, w in d["pts"]], dtype=float),
                                    _np.array([w for p, w in d["pts"]], dtype=float)))
                return out
            neigh_sets = _cat_sets("근린")
            hub_sets = _cat_sets("거점")

            # 집적도(지표1 검증본): 1km 셀을 500m로 4분할 → KDE → 생활권 c2 최대
            sub_xy = []; sub_zone = []
            for i in range(len(cids)):
                cx0, cy0 = cxy[i]; zc = zof[i]
                for dx in (-250.0, 250.0):
                    for dy in (-250.0, 250.0):
                        sub_xy.append([cx0 + dx, cy0 + dy]); sub_zone.append(zc)
            sub_xy = _np.array(sub_xy, dtype=float); sub_zone = _np.array(sub_zone, dtype=object)
            c2map = {}
            for key, sets in [("ne", neigh_sets), ("hu", hub_sets)]:
                if sets:
                    _, _, _, cc = _kde_mix(sub_xy, sets, kde_radius)
                    c2map[key] = cc
            for zid in zone_geoms:
                sm = (sub_zone == zid)
                for key in ("ne", "hu"):
                    if key in c2map and sm.any():
                        kpi_zones[zid]["c2_" + key] = round(float(c2map[key][sm].max()), 3)
            # --- 도로망 엔진(검증본: UP_FROM/UP_TO 노드+본토): 근접성·위계연결성 도로거리 ---
            net_lyr = self.parameterAsVectorLayer(parameters, self.NET, context)
            cpos = {cid: (cg.centroid().asPoint().x(), cg.centroid().asPoint().y())
                    for cid, cg in center_geoms.items()}
            cids_c = list(center_geoms.keys())
            road = None
            road_grid_vid = road_grid_snap = None
            road_cen_vid = road_cen_snap = None
            fac_snap_by_type = {}   # {유형: (vid, snap거리)}
            if net_lyr is not None:
                feedback.pushInfo("도로망 그래프 구축 중 (검증본 엔진: UP_FROM/UP_TO+본토)…")
                road = _road_node_engine(net_lyr, "UP_FROM_NO", "UP_TO_NODE", "ONEWAY")
                if road is not None:
                    feedback.pushInfo("  노드 %d · 본토 %d (%.1f%%)"
                                      % (road["Nn"], len(road["main_idx"]), 100.0 * len(road["main_idx"]) / road["Nn"]))
                    road_grid_vid, road_grid_snap = _road_snap(road, cxy)
                    cen_xy = _np.array([list(cpos[c]) for c in cids_c], dtype=float)
                    road_cen_vid, road_cen_snap = _road_snap(road, cen_xy)
                    for tn, d in fac_pts_by_type.items():
                        if d["pts"]:
                            fxy = _np.array([[p.x(), p.y()] for p, w in d["pts"]], dtype=float)
                            fac_snap_by_type[tn] = _road_snap(road, fxy)
                else:
                    feedback.pushWarning("도로망 그래프 실패 → 근접성·위계연결성 직선거리로 대체.")

            from scipy.sparse.csgraph import dijkstra as _dij
            # 근접성(지표2 검증본): 셀→유형별 최근린 시설 도로거리(방향+무방향폴백+진입스냅), 직선 폴백
            prox_by_type = {}    # {유형: 셀별 거리(m)}
            type_cat = {}        # {유형: "ne"/"hu"}
            for cat_key, sets in [("ne", neigh_sets), ("hu", hub_sets)]:
                for tn, xy, w in sets:
                    if len(xy) == 0:
                        continue
                    type_cat[tn] = cat_key
                    straight = _KD(xy).query(cxy)[0]
                    dist = straight
                    if road is not None and tn in fac_snap_by_type:
                        fvid = _np.unique(fac_snap_by_type[tn][0])
                        dd = _dij(road["Gd"], directed=True, indices=fvid, min_only=True)
                        g = dd[road_grid_vid].astype(float)
                        if (~_np.isfinite(g)).any():
                            ddu = _dij(road["Gu"], directed=True, indices=fvid, min_only=True)
                            g = _np.where(_np.isfinite(g), g, ddu[road_grid_vid].astype(float))
                        g = g + road_grid_snap
                        miss = ~_np.isfinite(g); g[miss] = straight[miss]
                        dist = g
                    prox_by_type[tn] = dist

            for zid in zone_geoms:
                m = (zof == zid); z = kpi_zones[zid]; mm = m & (popv > 0)
                # 근접성: 유형별 평균 거리 D_zk(km) — 단순평균(인구가중 폐기, 2026-09) + 종합
                dzk = {}
                if mm.any():
                    for tn, dist in prox_by_type.items():
                        dzk[tn] = round(float(dist[mm].mean()) / 1000.0, 3)
                z["prox_by_type"] = dzk
                ne_vals = [dzk[t] for t in dzk if type_cat.get(t) == "ne"]
                hu_vals = [dzk[t] for t in dzk if type_cat.get(t) == "hu"]
                if ne_vals:
                    z["prox_ne"] = round(sum(ne_vals) / len(ne_vals), 3)
                if hu_vals:
                    z["prox_hu"] = round(sum(hu_vals) / len(hu_vals), 3)

            # 충족도(지표3, 검증본): 중심지 폴리곤 도보권(폴리곤+반경) 내 시설유형 존재 → 생활권 union 비율(%)
            ne_types = [tn for tn, d in fac_pts_by_type.items() if d["cat"] == "근린"]
            hu_types = [tn for tn, d in fac_pts_by_type.items() if d["cat"] == "거점"]
            fac_idx_by_type = {}; fac_geo_by_type = {}
            for tn, d in fac_pts_by_type.items():
                idx = QgsSpatialIndex(); geos = {}
                for i, (p, w) in enumerate(d["pts"]):
                    fe = QgsFeature(i); gg = QgsGeometry.fromPointXY(p); fe.setGeometry(gg)
                    idx.insertFeature(fe); geos[i] = QgsPointXY(p)
                fac_idx_by_type[tn] = idx; fac_geo_by_type[tn] = geos
            zexist = {zid: {"ne": set(), "hu": set()} for zid in zone_geoms}
            region_ne = set(); region_hu = set()   # 계획권역 union(전체 중심지)
            for cid, cg in center_geoms.items():
                present = set()
                bb = QgsGeometry(cg).boundingBox(); bb.grow(suff_radius)
                for tn in fac_pts_by_type:
                    geos = fac_geo_by_type[tn]
                    for cand in fac_idx_by_type[tn].intersects(bb):
                        if cg.distance(QgsGeometry.fromPointXY(geos[cand])) <= suff_radius:
                            present.add(tn); break
                for t in present:
                    (region_ne if t in ne_types else region_hu).add(t)
                zpc = locate(QgsGeometry.fromPointXY(QgsPointXY(cpos[cid][0], cpos[cid][1])), zone_index, zone_geoms)
                kpi_centers.setdefault(cid, {})["suff_present"] = len(present)
                if zpc is not None:
                    for t in present:
                        (zexist[zpc]["ne"] if t in ne_types else zexist[zpc]["hu"]).add(t)
            for zid in zone_geoms:
                if ne_types:
                    kpi_zones[zid]["suff_ne"] = round(len(zexist[zid]["ne"]) / len(ne_types) * 100.0, 1)
                if hu_types:
                    kpi_zones[zid]["suff_hu"] = round(len(zexist[zid]["hu"]) / len(hu_types) * 100.0, 1)

            # 계획권역(전체) 근접성·충족도 (전체요약 박스용)
            kpi_region = {}
            allmm = popv > 0
            if allmm.any():
                rdzk = {tn: round(float(dist[allmm].mean()) / 1000.0, 3)
                        for tn, dist in prox_by_type.items()}
                kpi_region["prox_by_type"] = rdzk
                ne_r = [rdzk[t] for t in rdzk if type_cat.get(t) == "ne"]
                hu_r = [rdzk[t] for t in rdzk if type_cat.get(t) == "hu"]
                if ne_r:
                    kpi_region["prox_ne"] = round(sum(ne_r) / len(ne_r), 3)
                if hu_r:
                    kpi_region["prox_hu"] = round(sum(hu_r) / len(hu_r), 3)
            if ne_types:
                kpi_region["suff_ne"] = round(len(region_ne) / len(ne_types) * 100.0, 1)
            if hu_types:
                kpi_region["suff_hu"] = round(len(region_hu) / len(hu_types) * 100.0, 1)
            kpi_region["type_cat"] = dict(type_cat)
            kpi_region["exist_by_type"] = {t: (1 if (t in region_ne or t in region_hu) else 0)
                                           for t in fac_pts_by_type}

            # 위계연결성(지표4 검증본): 중심지↔중심지 도로거리(방향+무방향폴백+스냅)→시간권
            #   결과는 중심지 단위(네트워크성). 생활권 레이더에는 안 넣음(콤팩트성=집적·근접·충족).
            bands = [30, 60, 90, 120]
            ctier = {c: (center_meta[c].get("tier") or 99) for c in cids_c}
            nC = len(cids_c)
            M = _np.full((nC, nC), _np.inf)
            if road is not None and road_cen_vid is not None:
                cvid = road_cen_vid; csnap = road_cen_snap
                uniq, inv = _np.unique(cvid, return_inverse=True)
                Dd = _dij(road["Gd"], directed=True, indices=uniq, min_only=False)
                Mn = Dd[inv][:, cvid].astype(float)
                mss = ~_np.isfinite(Mn)
                if mss.any():
                    Du = _dij(road["Gu"], directed=True, indices=uniq, min_only=False)
                    Mn = _np.where(mss, Du[inv][:, cvid].astype(float), Mn)
                M = Mn + csnap[:, None] + csnap[None, :]
                _np.fill_diagonal(M, 0.0)
            for a in range(nC):
                xa, ya = cpos[cids_c[a]]
                for b in range(nC):
                    if not _np.isfinite(M[a, b]):
                        xb, yb = cpos[cids_c[b]]
                        M[a, b] = ((xa - xb) ** 2 + (ya - yb) ** 2) ** 0.5
            _mpm = 40.0 * 1000.0 / 60.0   # 40km/h → m/분 (속도 파라미터화 예정)
            Tm = M / _mpm
            ranks = _np.array([ctier[c] for c in cids_c])
            for ai, c in enumerate(cids_c):
                t0 = ctier[c]
                others = _np.arange(nC) != ai
                up_m = (ranks < t0) & others
                eq_m = (ranks == t0) & others
                dn_m = (ranks > t0) & others
                tot_up = int(up_m.sum()); tot_eq = int(eq_m.sum()); tot_dn = int(dn_m.sum())
                all_r = []; up_r = []; eq_r = []; dn_r = []
                up_c = []; eq_c = []   # 시간권별 도달 '개수'(상위·동급) — xlsx: 수/비율
                for b in bands:
                    wi = (Tm[ai] <= b) & others
                    nu = int((wi & up_m).sum()); ne_ = int((wi & eq_m).sum())
                    up_c.append(nu); eq_c.append(ne_)
                    all_r.append(round(100.0 * int(wi.sum()) / (nC - 1), 1) if nC > 1 else None)
                    up_r.append(round(100.0 * nu / tot_up, 1) if tot_up > 0 else None)
                    eq_r.append(round(100.0 * ne_ / tot_eq, 1) if tot_eq > 0 else None)
                    dn_r.append(round(100.0 * int((wi & dn_m).sum()) / tot_dn, 1) if tot_dn > 0 else None)
                def _meankm(mask, _ai=ai):
                    v = M[_ai, mask]; v = v[_np.isfinite(v)]
                    return round(float(v.mean()) / 1000.0, 2) if len(v) else None
                kpi_centers.setdefault(c, {}).update({
                    "name": center_meta[c]["name"],
                    "tier": center_meta[c].get("tier"),
                    "tier_name": center_meta[c].get("hier_raw"),
                    "mean_up": _meankm(up_m), "mean_eq": _meankm(eq_m), "mean_dn": _meankm(dn_m),
                    "conn_bands": bands, "tot_up": tot_up, "tot_eq": tot_eq,
                    "conn_all": all_r, "conn_up": up_r, "conn_eq": eq_r, "conn_dn": dn_r,
                    "cnt_up": up_c, "cnt_eq": eq_c})

            # =========================================================
            # 생활편리성 표(Sheet2): 시설유형 × 공간단위(계획권역·생활권·중심지)
            #   천인당 시설수 · 중심지 내 시설 비율(%) · 서비스권 향유도(%)
            #   · 관리대상 시설 비율(%) · 평균 접근성(km)
            #   중심지 단위는 2·3·4열 산출 불가 → None
            # =========================================================
            cen_idx = QgsSpatialIndex(); cen_glist = {}; cen_ids = {}
            for _i, (_cid, _cg) in enumerate(center_geoms.items()):
                _fe = QgsFeature(_i); _fe.setGeometry(_cg)
                cen_idx.insertFeature(_fe); cen_glist[_i] = _cg; cen_ids[_i] = _cid

            def _center_tier_at(x, y):
                """점이 속한 중심지의 위계(tier) — 없으면 None."""
                gp = QgsGeometry.fromPointXY(QgsPointXY(x, y))
                for cand in cen_idx.intersects(gp.boundingBox()):
                    if cen_glist[cand].contains(gp):
                        return center_meta[cen_ids[cand]].get("tier")
                return None

            # 셀·시설의 소속 중심지 위계
            cell_ctier = _np.array([_center_tier_at(cxy[i, 0], cxy[i, 1]) or 0
                                    for i in range(len(cids))], dtype=int)
            cen_cell_mask = cell_ctier > 0
            fac_ctier = {fid: (_center_tier_at(m["point"].x(), m["point"].y()) or 0)
                         for fid, m in fac_meta.items()}
            fac_in_center = {fid: (fac_ctier[fid] > 0) for fid in fac_ctier}
            fids_by_type = {}
            for fid in fac_catch:
                fids_by_type.setdefault(fac_meta[fid]["type"], []).append(fid)

            def _unit_table(cell_mask, fid_filter, centers_only=False):
                pop = float(popv[cell_mask].sum())
                unit_cellset = set(cids[i] for i in range(len(cids)) if cell_mask[i])
                rows = {}
                for tn, d in fac_pts_by_type.items():
                    fids = [fid for fid in fids_by_type.get(tn, []) if fid_filter(fid)]
                    n = len(fids)
                    per1000 = round(n / pop * 1000.0, 3) if pop > 0 else None
                    dist = prox_by_type.get(tn)
                    acc = (round(float(dist[cell_mask].mean()) / 1000.0, 3)
                           if dist is not None and cell_mask.any() else None)
                    if centers_only:   # 중심지: 천인당·평균접근성만
                        rows[tn] = {"cat": d["cat"], "per1000": per1000, "ctr_ratio": None,
                                    "cover": None, "mgmt_ratio": None, "access": acc}
                        continue
                    in_c = sum(1 for fid in fids if fac_in_center[fid])
                    ctr_ratio = round(100.0 * in_c / n, 1) if n > 0 else None
                    covered = set()
                    for fid in fids:
                        covered |= fac_catch[fid]
                    cov_pop = sum(cell_pop_base.get(c, 0.0) for c in (covered & unit_cellset))
                    cover = round(100.0 * cov_pop / pop, 1) if pop > 0 else None
                    _mp = fac_minpop_by_cat.get(d["cat"], fac_minpop)
                    mgmt = 0
                    for fid in fids:
                        bp = sum(cell_pop_base.get(c, 0.0) for c in fac_catch[fid])
                        if bp < _mp:
                            mgmt += 1
                    mgmt_ratio = round(100.0 * mgmt / n, 1) if n > 0 else None
                    rows[tn] = {"cat": d["cat"], "per1000": per1000, "ctr_ratio": ctr_ratio,
                                "cover": cover, "mgmt_ratio": mgmt_ratio, "access": acc}
                return rows

            reg_rows = _unit_table(_np.ones(len(cids), dtype=bool), lambda fid: True)
            zrows = []
            for zid in zone_geoms:
                zmask = (zof == zid)
                zrows.append({"name": zone_meta[zid]["name"],
                              "rows": _unit_table(zmask, lambda fid, _z=zid: fac_meta[fid]["zone"] == _z)})
            # 중심지: 위계별(1차/2차/3차) 그릇
            crows = []
            _ctiers = sorted({center_meta[c].get("tier") for c in center_geoms
                              if center_meta[c].get("tier")})
            _clabel = {}
            for c in center_geoms:
                _t = center_meta[c].get("tier")
                if _t and _t not in _clabel:
                    _clabel[_t] = center_meta[c].get("hier_raw") or ("%d차 중심지" % _t)
            for _t in _ctiers:
                tmask = (cell_ctier == _t)
                crows.append({"name": _clabel[_t], "tier": _t,
                              "rows": _unit_table(tmask, lambda fid, _tt=_t: fac_ctier[fid] == _tt,
                                                  centers_only=True)})
            life_table = {"region": reg_rows, "zones": zrows, "centers": crows,
                          "minpop": fac_minpop, "minpop_hub": fac_minpop_hub,
                          "radius": fac_radius, "radius_hub": fac_radius_hub}
            feedback.pushInfo("생활편리성 표 산출: 시설 %d유형 × (계획권역·생활권 %d·중심지 위계별)."
                              % (len(fac_pts_by_type), len(zone_geoms)))

            feedback.pushInfo("KPI 4지표 계산 완료 (생활권 %d · 중심지 %d)." % (len(kpi_zones), len(kpi_centers)))

        # =================================================================
        # 지역 과거 현황 (전국 과거 1km 격자 주거 총인구 내장 → 격자코드 매칭)
        #   2000~2020(5년) + 기준연도 → 계획권역·생활권·중심지 총인구·집중도 추이
        # =================================================================
        past = None
        past_csv = os.path.join(os.path.dirname(__file__), "data", "past_pop_1km.csv")
        if os.path.exists(past_csv) and has_id:
            pmap = {}
            with open(past_csv, encoding="utf-8") as pf:
                prd = csv.reader(pf)
                phdr = next(prd)
                pyears = [int(h[1:]) for h in phdr[1:]]
                for prow in prd:
                    d = {}
                    for j, yv in enumerate(prow[1:]):
                        if yv != "":
                            try:
                                d[pyears[j]] = float(yv)
                            except ValueError:
                                pass
                    if d:
                        pmap[prow[0].strip()] = d
            # 중심지 소속 판정 인덱스 (위계 포함)
            pcidx = QgsSpatialIndex(); pcgl = {}; pcid = {}
            for _i, (_cid, _cg) in enumerate(center_geoms.items()):
                _fe = QgsFeature(_i); _fe.setGeometry(_cg)
                pcidx.insertFeature(_fe); pcgl[_i] = _cg; pcid[_i] = _cid

            def _p_tier(pt):
                gp = QgsGeometry.fromPointXY(pt)
                for cand in pcidx.intersects(gp.boundingBox()):
                    if pcgl[cand].contains(gp):
                        return center_meta[pcid[cand]].get("tier") or 0
                return 0
            p_tiers = sorted({center_meta[c].get("tier") for c in center_geoms
                              if center_meta[c].get("tier")})
            p_tlabel = {}
            for c in center_geoms:
                _t = center_meta[c].get("tier")
                if _t and _t not in p_tlabel:
                    p_tlabel[_t] = center_meta[c].get("hier_raw") or ("%d차 중심지" % _t)

            reg_ts = {y: 0.0 for y in pyears}
            zn_ts = {zid: {y: 0.0 for y in pyears} for zid in zone_geoms}
            cn_ts = {y: 0.0 for y in pyears}
            cnt_ts = {t: {y: 0.0 for y in pyears} for t in p_tiers}   # 위계별 중심지 인구
            base_reg = 0.0; base_zn = {zid: 0.0 for zid in zone_geoms}; base_cn = 0.0
            base_cnt = {t: 0.0 for t in p_tiers}
            cen_area = 0.0
            for _cg in center_geoms.values():
                cen_area += area_km2(_cg)
            matched = 0
            for f in grid1.getFeatures():
                raw = f[grid_id_field]
                if raw in (None, ""):
                    continue
                gid_s = str(raw).strip()
                cid = _idkey(raw)
                ctr = cell_centroid.get(cid)
                zid = cell_zone.get(cid)
                ctier = _p_tier(ctr) if ctr is not None else 0
                inc = ctier > 0
                try:
                    pr = float(f[resid1]) if f[resid1] not in (None, "") else 0.0
                except (TypeError, ValueError):
                    pr = 0.0
                base_reg += pr
                if zid is not None:
                    base_zn[zid] += pr
                if inc:
                    base_cn += pr
                    if ctier in base_cnt:
                        base_cnt[ctier] += pr
                d = pmap.get(gid_s)
                if d:
                    matched += 1
                    for y, v in d.items():
                        reg_ts[y] += v
                        if zid is not None:
                            zn_ts[zid][y] += v
                        if inc:
                            cn_ts[y] += v
                            if ctier in cnt_ts:
                                cnt_ts[ctier][y] += v
            if matched > 0:
                ylab = [str(y) for y in pyears] + ["기준연도"]
                reg_pop = [round(reg_ts[y], 0) for y in pyears] + [round(base_reg, 0)]
                cen_pop = [round(cn_ts[y], 0) for y in pyears] + [round(base_cn, 0)]
                conc = [round(100.0 * cn_ts[y] / reg_ts[y], 1) if reg_ts[y] > 0 else None
                        for y in pyears] + [round(100.0 * base_cn / base_reg, 1) if base_reg > 0 else None]
                zn_list = [{"name": zone_meta[zid]["name"],
                            "pop": [round(zn_ts[zid][y], 0) for y in pyears] + [round(base_zn[zid], 0)]}
                           for zid in zone_geoms]
                conc_tiers = [{"name": p_tlabel[t], "tier": t,
                               "conc": [round(100.0 * cnt_ts[t][y] / reg_ts[y], 1) if reg_ts[y] > 0 else None
                                        for y in pyears]
                               + [round(100.0 * base_cnt[t] / base_reg, 1) if base_reg > 0 else None]}
                              for t in p_tiers]
                past = {"years": ylab, "region_pop": reg_pop, "center_pop": cen_pop,
                        "conc": conc, "conc_tiers": conc_tiers, "zones": zn_list,
                        "region_dens": [round(v / region_area, 1) if region_area > 0 else None for v in reg_pop],
                        "center_dens": [round(v / cen_area, 1) if cen_area > 0 else None for v in cen_pop]}
                feedback.pushInfo("지역 과거 현황: 격자 %d개 매칭(전국 내장 %d) · 연도 %s→기준연도."
                                  % (matched, len(pmap), pyears))
            else:
                feedback.pushWarning("지역 과거 현황: 격자코드가 전국 과거 데이터와 매칭되지 않음(과거 현황 생략).")

        # =================================================================
        # 시나리오별 집계
        # =================================================================
        def y0():
            return {"resid": 0.0, "work": 0.0, "has_work": False, "cells": 0, "sparse": 0}

        scen_results = []   # 각 시나리오의 계산 결과 dict (대시보드 payload용)

        for scen in scenarios:
            sname = scen["name"]
            slots = scen["slots"]
            slots.sort(key=lambda s: s["year_num"])
            years = [s["year_num"] for s in slots]
            labels = {s["year_num"]: s["label"] for s in slots}
            feedback.pushInfo("=== 시나리오 '%s' 집계 (%d시점) ===" % (sname, len(years)))

            region = {y: y0() for y in years}
            zone_pop = {zid: {y: y0() for y in years} for zid in zone_geoms}
            zone_ctr = {zid: {y: {"resid": 0.0, "work": 0.0, "area": 0.0} for y in years} for zid in zone_geoms}
            center_pop = {cid: {y: y0() for y in years} for cid in center_geoms}
            cell_pop_year = {y: {} for y in years}   # {year: {cell_id: resid}} (연결성/시설용)

            for s in slots:
                yr = s["year_num"]
                has_work = s["work"] is not None
                grid = s["grid"]
                gtl = transformer(grid)
                total = grid.featureCount() or 0
                use_idfilter = (not s["is_base"]) and bool(base_id_set) and has_id
                idf = None
                if use_idfilter:
                    idf = grid_id_field if grid_id_field in grid.fields().names() else None
                    if idf is None:
                        feedback.pushWarning("[%s/%s] 격자 ID 필드 없음 → 공간필터만 적용." % (sname, s["label"]))
                feedback.pushInfo("[%s/%s] 격자 %s개 처리…%s"
                                  % (sname, s["label"], total, " (기준년도 ID 추출)" if idf else ""))
                kept = 0
                for n, f in enumerate(grid.getFeatures()):
                    if feedback.isCanceled():
                        break
                    ck = _idkey(f[idf]) if idf else (_idkey(f[grid_id_field]) if has_id and grid_id_field in grid.fields().names() else None)
                    if idf is not None:
                        if ck not in base_id_set:
                            continue
                        kept += 1
                    g = f.geometry()
                    if g is None or g.isEmpty():
                        continue
                    if gtl:
                        g = QgsGeometry(g)
                        g.transform(gtl)
                    c = g.centroid().asPoint()
                    pt = QgsGeometry.fromPointXY(QgsPointXY(c))

                    rv = _num(f[s["resid"]]) or 0.0
                    wv = (_num(f[s["work"]]) or 0.0) if has_work else 0.0
                    carea = area_km2(g)
                    is_sparse = carea > 0 and (rv / carea) <= sparse_threshold

                    region[yr]["resid"] += rv
                    region[yr]["work"] += wv
                    if has_work:
                        region[yr]["has_work"] = True
                    region[yr]["cells"] += 1
                    if is_sparse:
                        region[yr]["sparse"] += 1

                    if ck is not None:
                        cell_pop_year[yr][ck] = rv

                    zid = locate(pt, zone_index, zone_geoms)
                    if zid is not None:
                        zone_pop[zid][yr]["resid"] += rv
                        zone_pop[zid][yr]["work"] += wv
                        if has_work:
                            zone_pop[zid][yr]["has_work"] = True
                        zone_pop[zid][yr]["cells"] += 1
                        if is_sparse:
                            zone_pop[zid][yr]["sparse"] += 1

                    cid = locate(pt, center_index, center_geoms)
                    if cid is not None:
                        center_pop[cid][yr]["resid"] += rv
                        center_pop[cid][yr]["work"] += wv
                        if has_work:
                            center_pop[cid][yr]["has_work"] = True
                    if zid is not None and cid is not None:
                        zone_ctr[zid][yr]["resid"] += rv
                        zone_ctr[zid][yr]["work"] += wv
                        zone_ctr[zid][yr]["area"] += carea

                    if total and n % 5000 == 0:
                        feedback.setProgress(int(n / total * 100))
                feedback.setProgress(0)
                if idf is not None:
                    feedback.pushInfo("[%s/%s] 전체 %s개 중 대상 %d개 · 인구합 %.0f명"
                                      % (sname, s["label"], total, kept, region[yr]["resid"]))

            multi_year = len(years) >= 2

            # 지역 시계열
            region_ts = []
            for y in years:
                resid = region[y]["resid"]
                work = region[y]["work"]
                hw = region[y]["has_work"]
                region_ts.append({
                    "year": labels[y], "pop": resid,
                    "work": (work if hw else None),
                    "rdens": (resid / region_area) if region_area > 0 else None,
                    "wdens": (work / region_area) if (hw and region_area > 0) else None,
                    "sparse_ratio": (region[y]["sparse"] / region[y]["cells"] * 100.0) if region[y]["cells"] > 0 else None,
                })
            region_change = {
                "pop": _change_metrics([(y, region[y]["resid"]) for y in years]),
                "work": _change_metrics([(y, region[y]["work"] if region[y]["has_work"] else None) for y in years]),
                "rdens": _change_metrics([(y, (region[y]["resid"] / region_area) if region_area > 0 else None) for y in years]),
                "wdens": _change_metrics([(y, (region[y]["work"] / region_area) if (region[y]["has_work"] and region_area > 0) else None) for y in years]),
            }

            # 중심지 요약 시계열
            center_ts = []
            for y in years:
                cresid = sum(center_pop[cid][y]["resid"] for cid in center_geoms)
                cwork = sum(center_pop[cid][y]["work"] for cid in center_geoms)
                chw = any(center_pop[cid][y]["has_work"] for cid in center_geoms)
                reg_resid = region[y]["resid"]
                reg_work = region[y]["work"]
                center_ts.append({
                    "year": labels[y], "pop": cresid,
                    "wpop": (cwork if chw else None),
                    "density": (cresid / center_area_total) if center_area_total > 0 else None,
                    "rconc": (cresid / reg_resid * 100.0) if reg_resid > 0 else None,
                    "wconc": (cwork / reg_work * 100.0) if (chw and reg_work > 0) else None,
                })

            # 생활권 시계열
            zones_data = []
            for zid in sorted(zone_geoms):
                meta = zone_meta[zid]
                zarea = meta["area"]
                per_year = []
                for y in years:
                    zresid = zone_pop[zid][y]["resid"]
                    zwork = zone_pop[zid][y]["work"]
                    zhw = zone_pop[zid][y]["has_work"]
                    ctr_r = zone_ctr[zid][y]["resid"]
                    ctr_w = zone_ctr[zid][y]["work"]
                    ctr_a = zone_ctr[zid][y]["area"]
                    zcells = zone_pop[zid][y]["cells"]
                    per_year.append({
                        "year": labels[y], "pop": zresid,
                        "rdens": (zresid / zarea) if zarea > 0 else None,
                        "wdens": (zwork / zarea) if (zhw and zarea > 0) else None,
                        "crconc": (ctr_r / zresid * 100.0) if zresid > 0 else None,
                        "cwconc": (ctr_w / zwork * 100.0) if (zhw and zwork > 0) else None,
                        "cpop": ctr_r,   # 생활권 내 중심지 인구
                        "cdens": (ctr_r / ctr_a) if ctr_a > 0 else None,   # 생활권 내 중심지 인구밀도
                        "sparse_ratio": (zone_pop[zid][y]["sparse"] / zcells * 100.0) if zcells > 0 else None,
                    })
                chg = {
                    "pop": _change_metrics([(y, zone_pop[zid][y]["resid"]) for y in years]),
                    "rdens": _change_metrics([(y, (zone_pop[zid][y]["resid"] / zarea) if zarea > 0 else None) for y in years]),
                }
                zones_data.append({"zid": zid, "name": meta["name"], "area": zarea,
                                   "per_year": per_year, "change": chg})

            # 중심지 시계열
            centers_data = []
            for cid in sorted(center_geoms):
                meta = center_meta[cid]
                carea = meta["area"]
                per_year = []
                for y in years:
                    cresid = center_pop[cid][y]["resid"]
                    cwork = center_pop[cid][y]["work"]
                    chw = center_pop[cid][y]["has_work"]
                    reg_resid = region[y]["resid"]
                    per_year.append({
                        "year": labels[y], "pop": cresid,
                        "rdens": (cresid / carea) if carea > 0 else None,
                        "wdens": (cwork / carea) if (chw and carea > 0) else None,
                        "pshare": (cresid / reg_resid * 100.0) if reg_resid > 0 else None,
                    })
                chg = {"pop": _change_metrics([(y, center_pop[cid][y]["resid"]) for y in years])}
                zc = center_zone.get(cid)
                centers_data.append({"cid": cid, "name": meta["name"], "area": carea,
                                     "tier": meta.get("tier"),
                                     "zone": (zone_meta[zc]["name"] if zc is not None else None),
                                     "per_year": per_year, "change": chg})

            # 중심지 위계별 집계 (1차/2차/3차) — '중심지' 집계 그릇 = 위계별
            tiers_present = sorted({center_meta[c].get("tier") for c in center_geoms
                                    if center_meta[c].get("tier")})
            tier_label = {}
            for c in center_geoms:
                t = center_meta[c].get("tier")
                if t and t not in tier_label:
                    tier_label[t] = center_meta[c].get("hier_raw") or ("%d차 중심지" % t)
            center_tiers = []
            for t in tiers_present:
                cids_t = [c for c in center_geoms if center_meta[c].get("tier") == t]
                area_t = sum(center_meta[c]["area"] for c in cids_t)
                per_year = []
                for y in years:
                    rr = sum(center_pop[c][y]["resid"] for c in cids_t)
                    ww = sum(center_pop[c][y]["work"] for c in cids_t)
                    hw = any(center_pop[c][y]["has_work"] for c in cids_t)
                    reg_r = region[y]["resid"]
                    per_year.append({
                        "year": labels[y], "pop": rr, "wpop": (ww if hw else None),
                        "rdens": (rr / area_t) if area_t > 0 else None,
                        "wdens": (ww / area_t) if (hw and area_t > 0) else None,
                        "pshare": (rr / reg_r * 100.0) if reg_r > 0 else None,
                    })
                chg = {"pop": _change_metrics([(y, sum(center_pop[c][y]["resid"] for c in cids_t))
                                               for y in years])}
                center_tiers.append({"tier": t, "name": tier_label[t], "count": len(cids_t),
                                     "area": area_t, "per_year": per_year, "change": chg})

            # 연결성/형평성 (시점별 재평가)
            conn_ts = None
            zone_conn_ts = None
            if conn_enabled:
                conn_ts = []
                zone_conn_ts = {zid: [] for zid in zone_geoms}
                for y in years:
                    snap = _eval_conn_snapshot(cell_pop_year[y], cell_zone, reg_min, zon_min,
                                               region_present, zone_present, thr_m_list)
                    reg = snap["region"]
                    conn_ts.append({
                        "levels": reg["levels"], "conn": reg["conn"],
                        "equity": reg["equity"], "n": reg["n"],
                    } if reg else None)
                    for zid in zone_geoms:
                        zsc = snap["zones"].get(zid)
                        zone_conn_ts[zid].append({
                            "levels": zsc["levels"], "conn": zsc["conn"],
                            "equity": zsc["equity"], "n": zsc["n"],
                        } if zsc else None)

            # 시설 지표 (시점별 재평가) — 그룹(전체/근린/거점/유형)별 시계열
            fac_ts = None
            zone_fac_ts = None
            fac_items = None
            fac_ts_g = None
            zone_fac_ts_g = None
            if fac_enabled:
                fac_ts_g = {g: [] for g in fac_groups}
                zone_fac_ts_g = {g: {zid: [] for zid in zone_geoms} for g in fac_groups}
                bp_by_fac = {fid: [] for fid in fac_catch}
                for y in years:
                    cp = cell_pop_year[y]
                    tot_pop = sum(cp.values())
                    zpop = defaultdict(float)
                    for c, p in cp.items():
                        z = cell_zone.get(c)
                        if z is not None:
                            zpop[z] += p
                    bpv = {}
                    for fid in fac_catch:
                        b = sum(cp.get(c, 0.0) for c in fac_catch[fid])
                        bpv[fid] = b
                        bp_by_fac[fid].append(b)
                    for g, fids in fac_groups.items():
                        served = set()
                        alive_n = 0
                        mgmt_n = 0
                        zt = defaultdict(int)
                        za = defaultdict(int)
                        zm = defaultdict(int)
                        for fid in fids:
                            al = bpv[fid] >= fac_minpop_by_cat.get(fac_meta[fid]["cat"], fac_minpop)
                            z = fac_zone.get(fid)
                            if al:
                                served |= fac_catch[fid]
                                alive_n += 1
                            else:
                                mgmt_n += 1
                            if z is not None:
                                zt[z] += 1
                                if al:
                                    za[z] += 1
                                else:
                                    zm[z] += 1
                        served_pop = sum(cp.get(c, 0.0) for c in served)
                        zserved = defaultdict(float)
                        for c in served:
                            z = cell_zone.get(c)
                            if z is not None:
                                zserved[z] += cp.get(c, 0.0)
                        fac_ts_g[g].append({
                            "cover": (served_pop / tot_pop * 100.0) if tot_pop > 0 else None,
                            "mgmt": mgmt_n, "alive": alive_n, "total": len(fids)})
                        for zid in zone_geoms:
                            tp = zpop.get(zid, 0.0)
                            zone_fac_ts_g[g][zid].append({
                                "cover": (zserved.get(zid, 0.0) / tp * 100.0) if tp > 0 else None,
                                "mgmt": zm.get(zid, 0), "alive": za.get(zid, 0), "total": zt.get(zid, 0)})
                # 전체 그룹 = 레거시 fac_ts/zone_fac_ts (기존 카드·표 호환)
                fac_ts = fac_ts_g["전체"]
                zone_fac_ts = zone_fac_ts_g["전체"]
                # 시설별 항목: 폐지시점 = 첫 배후인구 < 최소규모 연도
                fac_items = []
                for fid in sorted(fac_catch):
                    bps = bp_by_fac[fid]
                    _mp = fac_minpop_by_cat.get(fac_meta[fid]["cat"], fac_minpop)   # 근린·거점 최소 배후인구 구분
                    close_year = None
                    for i, y in enumerate(years):
                        if bps[i] < _mp:
                            close_year = labels[y]
                            break
                    fac_items.append({
                        "name": fac_meta[fid]["name"], "zone": fac_zone[fid],
                        "type": fac_meta[fid]["type"], "cat": fac_meta[fid]["cat"],
                        "bp": bps, "close_year": close_year,
                        "alive_last": bps[-1] >= _mp,
                    })

            scen_results.append({
                "name": sname, "years": years, "labels": labels, "multi_year": multi_year,
                "region_ts": region_ts, "region_change": region_change,
                "center_ts": center_ts, "zones": zones_data, "centers": centers_data,
                "center_tiers": center_tiers,
                "conn_ts": conn_ts, "zone_conn_ts": zone_conn_ts,
                "fac_ts": fac_ts, "zone_fac_ts": zone_fac_ts, "fac_items": fac_items,
                "fac_ts_g": fac_ts_g, "zone_fac_ts_g": zone_fac_ts_g,
            })

        # =================================================================
        # 출력 레이어: 생활권 / 중심지 / 시설  (목표시점=각 시나리오 마지막)
        # =================================================================
        scen_names = [s["name"] for s in scen_results]
        n_scen = len(scen_results)

        def _rel(a, b):
            return ((b - a) / a * 100.0) if (a not in (None, 0) and b is not None) else None

        # --- 생활권 지표 레이어 ---
        zfields = QgsFields()
        zfields.append(QgsField("zone_id", QVariant.Int))
        zfields.append(QgsField("zone_name", QVariant.String))
        zfields.append(QgsField("area_km2", QVariant.Double))
        zfields.append(QgsField("base_pop", QVariant.Double))
        for i in range(n_scen):
            zfields.append(QgsField("pop_s%d" % (i + 1), QVariant.Double))       # 목표시점 인구
            zfields.append(QgsField("popchg_s%d" % (i + 1), QVariant.Double))    # 기준→목표 %
            zfields.append(QgsField("sparse_s%d" % (i + 1), QVariant.Double))    # 목표 인구과소비율%
            if fac_enabled:
                zfields.append(QgsField("cover_s%d" % (i + 1), QVariant.Double)) # 향유도%
                zfields.append(QgsField("mgmt_s%d" % (i + 1), QVariant.Int))     # 관리대상 수
        (zsink, zsink_id) = self.parameterAsSink(
            parameters, self.OUT_ZONES, context, zfields, QgsWkbTypes.MultiPolygon, region_crs)

        for zid in sorted(zone_geoms):
            feat = QgsFeature(zfields)
            feat.setGeometry(zone_geoms[zid])
            base_pop = None
            for zd in scen_results[0]["zones"]:
                if zd["zid"] == zid:
                    base_pop = zd["per_year"][0]["pop"]
                    break
            attrs = [zid, zone_meta[zid]["name"], _r(zone_meta[zid]["area"], 4), _r(base_pop)]
            for i, sr in enumerate(scen_results):
                zd = next((z for z in sr["zones"] if z["zid"] == zid), None)
                if zd:
                    tgt = zd["per_year"][-1]
                    frst = zd["per_year"][0]
                    attrs += [_r(tgt["pop"]), _r(_rel(frst["pop"], tgt["pop"])), _r(tgt["sparse_ratio"])]
                    if fac_enabled:
                        zf = sr["zone_fac_ts"][zid][-1] if sr["zone_fac_ts"] else None
                        attrs += [_r(zf["cover"]) if zf else None, (zf["mgmt"] if zf else None)]
                else:
                    attrs += [None, None, None] + ([None, None] if fac_enabled else [])
            feat.setAttributes(attrs)
            zsink.addFeature(feat)

        # --- 중심지 지표 레이어 ---
        cfields = QgsFields()
        cfields.append(QgsField("center_id", QVariant.Int))
        cfields.append(QgsField("center_nm", QVariant.String))
        cfields.append(QgsField("area_km2", QVariant.Double))
        cfields.append(QgsField("base_pop", QVariant.Double))
        for i in range(n_scen):
            cfields.append(QgsField("pop_s%d" % (i + 1), QVariant.Double))
            cfields.append(QgsField("popchg_s%d" % (i + 1), QVariant.Double))
        (csink, csink_id) = self.parameterAsSink(
            parameters, self.OUT_CENTERS, context, cfields, QgsWkbTypes.MultiPolygon, region_crs)

        for cid in sorted(center_geoms):
            feat = QgsFeature(cfields)
            feat.setGeometry(center_geoms[cid])
            base_pop = None
            cd0 = next((c for c in scen_results[0]["centers"] if c["cid"] == cid), None)
            if cd0:
                base_pop = cd0["per_year"][0]["pop"]
            attrs = [cid, center_meta[cid]["name"], _r(center_meta[cid]["area"], 4), _r(base_pop)]
            for sr in scen_results:
                cd = next((c for c in sr["centers"] if c["cid"] == cid), None)
                if cd:
                    tgt = cd["per_year"][-1]
                    frst = cd["per_year"][0]
                    attrs += [_r(tgt["pop"]), _r(_rel(frst["pop"], tgt["pop"]))]
                else:
                    attrs += [None, None]
            feat.setAttributes(attrs)
            csink.addFeature(feat)

        # --- 시설 지표 레이어 (포인트) ---
        fsink_id = None
        if fac_enabled:
            ffields = QgsFields()
            ffields.append(QgsField("fac_id", QVariant.Int))
            ffields.append(QgsField("fac_name", QVariant.String))
            ffields.append(QgsField("zone_name", QVariant.String))
            ffields.append(QgsField("base_bp", QVariant.Double))
            for i in range(n_scen):
                ffields.append(QgsField("bp_s%d" % (i + 1), QVariant.Double))     # 목표 배후인구
                ffields.append(QgsField("close_s%d" % (i + 1), QVariant.String))  # 폐지시점(없으면 생존)
                ffields.append(QgsField("alive_s%d" % (i + 1), QVariant.Int))     # 목표시점 생존 1/0
            (fsink, fsink_id) = self.parameterAsSink(
                parameters, self.OUT_FACILITY, context, ffields, QgsWkbTypes.Point, region_crs)
            if fsink is not None:
                for fid in sorted(fac_catch):
                    feat = QgsFeature(ffields)
                    feat.setGeometry(QgsGeometry.fromPointXY(fac_meta[fid]["point"]))
                    znm = zone_meta[fac_zone[fid]]["name"] if fac_zone[fid] is not None else ""
                    base_bp = scen_results[0]["fac_items"][
                        [it["name"] for it in scen_results[0]["fac_items"]].index(fac_meta[fid]["name"])
                    ]["bp"][0] if scen_results[0]["fac_items"] else None
                    attrs = [fid, fac_meta[fid]["name"], znm, _r(base_bp)]
                    for sr in scen_results:
                        it = next((x for x in (sr["fac_items"] or []) if x["name"] == fac_meta[fid]["name"]), None)
                        if it:
                            attrs += [_r(it["bp"][-1]),
                                      (it["close_year"] if it["close_year"] else "생존"),
                                      (1 if it["alive_last"] else 0)]
                        else:
                            attrs += [None, None, None]
                    feat.setAttributes(attrs)
                    fsink.addFeature(feat)

        # =================================================================
        # 출력 CSV: 지역 시계열(시나리오×시점) / 시설 요약
        # =================================================================
        region_csv = self.parameterAsFileOutput(parameters, self.OUT_REGION_CSV, context)
        if region_csv:
            with open(region_csv, "w", newline="", encoding="utf-8-sig") as fp:
                w = csv.writer(fp)
                w.writerow(["시나리오", "연도", "인구(주거)", "주거밀도(인/km2)", "근무밀도(인/km2)",
                            "인구과소비율(%)", "중심지 주거집중도(%)", "중심지 인구밀도(인/km2)"])
                for sr in scen_results:
                    for i, r in enumerate(sr["region_ts"]):
                        c = sr["center_ts"][i]
                        w.writerow([sr["name"], r["year"], _r(r["pop"]), _r(r["rdens"]), _r(r["wdens"]),
                                    _r(r["sparse_ratio"]), _r(c["rconc"]), _r(c["density"])])
                w.writerow([])
                w.writerow(["[시나리오별 기준→목표시점 인구 변화]"])
                w.writerow(["시나리오", "기준시점", "목표시점", "총변화율(%)", "연평균변화율(%)"])
                for sr in scen_results:
                    ch = sr["region_change"]["pop"]
                    if ch:
                        w.writerow([sr["name"], _r(ch["first"]), _r(ch["last"]),
                                    _r(ch["total_pct"]), _r(ch["cagr_pct"])])

        # -----------------------------------------------------------------
        # 영역별 결과 CSV (단계 산출) — SCOPE=basic/compact/network/life
        # -----------------------------------------------------------------
        scope = (self.parameterAsString(parameters, self.SCOPE, context) or "all").strip().lower()
        area_csv = self.parameterAsFileOutput(parameters, self.OUT_AREA_CSV, context)
        if area_csv:
            self._write_area_csv(area_csv, scope, scen_results, kpi_centers,
                                 center_geoms, life_table, feedback)
        area_dir = (self.parameterAsString(parameters, self.OUT_AREA_DIR, context) or "").strip()
        if area_dir:   # 일괄 산출: 단계별 CSV 4종을 한 번에
            os.makedirs(area_dir, exist_ok=True)
            for sc_key, fname in AREA_CSV_NAMES:
                self._write_area_csv(os.path.join(area_dir, fname), sc_key, scen_results, kpi_centers,
                                     center_geoms, life_table, feedback)

        fac_csv_path = None
        if fac_enabled:
            fac_csv_path = self.parameterAsFileOutput(parameters, self.OUT_FAC_CSV, context)
            if fac_csv_path:
                with open(fac_csv_path, "w", newline="", encoding="utf-8-sig") as fp:
                    w = csv.writer(fp)
                    w.writerow(["[시설 개요]"])
                    w.writerow(["시설 종류", fac_type])
                    w.writerow(["서비스반경(m) 근린/거점", "%s / %s" % (_r(fac_radius, 0), _r(fac_radius_hub, 0))])
                    w.writerow(["최소 배후인구 근린/거점", "%s / %s" % (_r(fac_minpop, 0), _r(fac_minpop_hub, 0))])
                    w.writerow([])
                    w.writerow(["[시나리오별 목표시점 요약]"])
                    w.writerow(["시나리오", "목표시점", "총 시설수", "생존", "관리대상", "서비스권 향유도(%)"])
                    for sr in scen_results:
                        if not sr["fac_ts"]:
                            continue
                        t = sr["fac_ts"][-1]
                        w.writerow([sr["name"], sr["region_ts"][-1]["year"],
                                    t["total"], t["alive"], t["mgmt"], _r(t["cover"])])
                    w.writerow([])
                    w.writerow(["[시설별 배후인구 · 폐지시점]"])
                    header = ["시설명", "생활권"]
                    for sr in scen_results:
                        header += ["%s 기준" % sr["name"], "%s 목표" % sr["name"], "%s 폐지시점" % sr["name"]]
                    w.writerow(header)
                    # 시설명 기준 정렬 (첫 시나리오 순서)
                    base_items = scen_results[0]["fac_items"] or []
                    for it0 in base_items:
                        znm = zone_meta[it0["zone"]]["name"] if it0["zone"] is not None else "-"
                        row = [it0["name"], znm]
                        for sr in scen_results:
                            it = next((x for x in (sr["fac_items"] or []) if x["name"] == it0["name"]), None)
                            if it:
                                row += [_r(it["bp"][0], 0), _r(it["bp"][-1], 0),
                                        it["close_year"] if it["close_year"] else "생존"]
                            else:
                                row += ["-", "-", "-"]
                        w.writerow(row)

        # =================================================================
        # HTML 대시보드
        # =================================================================
        def _sc_payload(sr):
            years = sr["years"]
            return {
                "name": sr["name"],
                "years": [sr["labels"][y] for y in years],
                "multi_year": sr["multi_year"],
                "region_ts": [{"year": r["year"], "pop": _r(r["pop"]), "work": _r(r["work"]),
                               "rdens": _r(r["rdens"]), "wdens": _r(r["wdens"]),
                               "sparse_ratio": _r(r["sparse_ratio"])} for r in sr["region_ts"]],
                "region_change": sr["region_change"],
                "center_ts": [{"year": r["year"], "pop": _r(r["pop"]), "wpop": _r(r["wpop"]),
                               "density": _r(r["density"]), "rconc": _r(r["rconc"]),
                               "wconc": _r(r["wconc"])} for r in sr["center_ts"]],
                "zones": [{
                    "zid": z["zid"], "name": z["name"], "area": _r(z["area"], 3),
                    "per_year": [{"year": p["year"], "pop": _r(p["pop"]), "rdens": _r(p["rdens"]),
                                  "wdens": _r(p["wdens"]), "crconc": _r(p["crconc"]),
                                  "cwconc": _r(p["cwconc"]), "cpop": _r(p["cpop"]), "cdens": _r(p["cdens"]),
                                  "sparse_ratio": _r(p["sparse_ratio"])}
                                 for p in z["per_year"]],
                    "change": z["change"],
                    "conn": ([_conn_payload(c) for c in sr["zone_conn_ts"][z["zid"]]]
                             if sr["zone_conn_ts"] else None),
                    "fac": ([{"cover": _r(x["cover"]), "mgmt": x["mgmt"], "alive": x["alive"],
                              "total": x["total"]} for x in sr["zone_fac_ts"][z["zid"]]]
                            if sr["zone_fac_ts"] else None),
                    "fac_g": ({g: [{"cover": _r(x["cover"]), "mgmt": x["mgmt"], "alive": x["alive"],
                                    "total": x["total"]} for x in sr["zone_fac_ts_g"][g][z["zid"]]]
                               for g in sr["zone_fac_ts_g"]} if sr["zone_fac_ts_g"] else None),
                } for z in sr["zones"]],
                "centers": [{
                    "name": c["name"], "area": _r(c["area"], 3), "tier": c.get("tier"),
                    "zone": c.get("zone"),
                    "per_year": [{"year": p["year"], "pop": _r(p["pop"]), "rdens": _r(p["rdens"]),
                                  "wdens": _r(p["wdens"]), "pshare": _r(p["pshare"])}
                                 for p in c["per_year"]],
                    "change": c["change"],
                } for c in sr["centers"]],
                "center_tiers": [{
                    "name": t["name"], "tier": t["tier"], "count": t["count"],
                    "area": _r(t["area"], 3),
                    "per_year": [{"year": p["year"], "pop": _r(p["pop"]), "wpop": _r(p["wpop"]),
                                  "rdens": _r(p["rdens"]), "wdens": _r(p["wdens"]),
                                  "pshare": _r(p["pshare"])}
                                 for p in t["per_year"]],
                    "change": t["change"],
                } for t in sr["center_tiers"]],
                "conn_ts": ([_conn_payload(c) for c in sr["conn_ts"]] if sr["conn_ts"] else None),
                "fac_ts": ([{"cover": _r(x["cover"]), "mgmt": x["mgmt"], "alive": x["alive"],
                             "total": x["total"]} for x in sr["fac_ts"]] if sr["fac_ts"] else None),
                "fac_ts_g": ({g: [{"cover": _r(x["cover"]), "mgmt": x["mgmt"], "alive": x["alive"],
                                   "total": x["total"]} for x in sr["fac_ts_g"][g]]
                              for g in sr["fac_ts_g"]} if sr["fac_ts_g"] else None),
                "fac_items": ([{"name": it["name"], "bp": [_r(v, 0) for v in it["bp"]],
                                "zone": (zone_meta[it["zone"]]["name"] if it["zone"] is not None else None),
                                "type": it["type"], "cat": it["cat"],
                                "close_year": it["close_year"], "alive_last": it["alive_last"]}
                               for it in sr["fac_items"]] if sr["fac_items"] else None),
            }

        def _conn_payload(c):
            if not c:
                return None
            return {"levels": c["levels"],
                    "conn": [[_r(v, 1) for v in row] for row in c["conn"]],
                    "equity": [[_r(v, 1) for v in row] for row in c["equity"]],
                    "n": c["n"]}

        # 기초현황 (선택, 최대 3개) — 격자 값 필드를 생활권/계획권역으로 집계 (단순평균; 프레임용)
        base_defs = [
            (self.BASE_GRID, self.BASE_FIELD, self.BASE_LABEL, self.BASE_UNIT),
            (self.BASE_GRID2, self.BASE_FIELD2, self.BASE_LABEL2, self.BASE_UNIT2),
            (self.BASE_GRID3, self.BASE_FIELD3, self.BASE_LABEL3, self.BASE_UNIT3),
        ]
        basestat_items = []
        for gk, fk, lk, uk in base_defs:
            base_layer = self.parameterAsVectorLayer(parameters, gk, context)
            base_field = self.parameterAsString(parameters, fk, context)
            base_label = self.parameterAsString(parameters, lk, context) or "기초현황 값"
            base_unit = self.parameterAsString(parameters, uk, context) or ""
            if base_layer is None or not base_field:
                continue
            fidx = base_layer.fields().indexOf(base_field)
            if fidx < 0:
                feedback.pushWarning("기초현황 값 필드 '%s' 없음 — 건너뜀." % base_field)
                continue
            bfct = transformer(base_layer)
            zsum = {zid: 0.0 for zid in zone_geoms}
            zcnt = {zid: 0 for zid in zone_geoms}
            rsum = 0.0
            rcnt = 0
            for f in base_layer.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                v = f[fidx]
                if v in (None, ""):
                    continue
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
                if bfct:
                    g = QgsGeometry(g)
                    g.transform(bfct)
                pt = g.centroid().asPoint()
                rsum += v
                rcnt += 1
                zid = locate(QgsGeometry.fromPointXY(QgsPointXY(pt)), zone_index, zone_geoms)
                if zid is not None:
                    zsum[zid] += v
                    zcnt[zid] += 1
            basestat_items.append({
                "label": base_label, "unit": base_unit,
                "region": (round(rsum / rcnt, 3) if rcnt else None),
                "zones": [{"name": zone_meta[zid]["name"],
                           "value": (round(zsum[zid] / zcnt[zid], 3) if zcnt[zid] else None)}
                          for zid in zone_geoms],
            })
            feedback.pushInfo("기초현황 '%s' 집계: 유효격자 %d개 → 생활권 %d." % (base_label, rcnt, len(zone_geoms)))
        basestat = ({"enabled": True, "items": basestat_items} if basestat_items else None)

        # =================================================================
        # 탄소 부하 (탄소공간지도 원천 → 격자코드로 기준년도 격자에 조인)
        #   부문: 전체·건물·수송 / 1인당 분모: 활동(주거+근무)·주거·근무
        #   집계 그릇: 계획권역·생활권·중심지 / 기준연도(현재 실측)
        # =================================================================
        carbon = None
        co2_src_p = self.parameterAsString(parameters, self.CO2_SRC, context) or None
        if co2_src_p:
            co2_join = self.parameterAsString(parameters, self.CO2_JOIN, context) or "grid_cd"
            co2_value = self.parameterAsString(parameters, self.CO2_VALUE, context) or "exhst_vl"

            tot_map = None
            av_total = av_bldg = av_trans = False      # 총배출 전용 (건물·수송 부문 미사용)
            probe = QgsVectorLayer(co2_src_p, "co2_src", "ogr")
            if not probe.isValid():
                feedback.pushWarning("탄소 배출 원천 열기 실패 → 탄소 지표 건너뜀: %s" % co2_src_p)
            else:
                names = probe.fields().names()
                jf = co2_join if co2_join in names else None
                vf = co2_value if co2_value in names else None
                if jf is None or vf is None:
                    feedback.pushWarning(
                        "탄소 원천: 필드('%s'/'%s') 없음. 보유필드=%s → 건너뜀."
                        % (co2_join, co2_value, names))
                else:
                    # 한글 격자코드 인코딩(탄소공간지도 CSV=CP949)을 매칭될 때까지 자동 시도
                    best = None
                    for enc in (None, "CP949", "EUC-KR", "UTF-8"):
                        lyr = QgsVectorLayer(co2_src_p, "co2_src", "ogr")
                        if enc:
                            lyr.setProviderEncoding(enc)
                        tm = {}
                        for f in lyr.getFeatures():
                            k = _idkey(f[jf])
                            if k is None:
                                continue
                            tm[k] = tm.get(k, 0.0) + _num(f[vf])
                        hit = len(set(tm) & base_id_set) if base_id_set else len(tm)
                        if best is None or hit > best[0]:
                            best = (hit, tm, (enc or "기본"))
                            self._layer_refs.append(lyr)
                        if base_id_set and hit > 0:
                            break
                    _, tot_map, best_enc = best
                    av_total = True
                    feedback.pushInfo("탄소 총배출 원천 로딩(인코딩=%s, 기준격자 매칭 %d)."
                                      % (best_enc, best[0]))

            if not av_total:
                feedback.pushWarning("탄소 배출을 산출할 수 없어 탄소 지표를 건너뜁니다.")
            else:
                def _cell_emis(cid):
                    # 총배출만 사용 (건물·수송 부문 미산출)
                    return (tot_map.get(cid, 0.0) if tot_map else 0.0), 0.0, 0.0

                def _acc():
                    return {"pop_r": 0.0, "pop_w": 0.0, "pop_a": 0.0,
                            "e_total": 0.0, "e_bldg": 0.0, "e_trans": 0.0,
                            "pop_a_center": 0.0}

                reg_acc = _acc()
                zon_acc = {zid: _acc() for zid in zone_geoms}
                cen_acc = {cid: _acc() for cid in center_geoms}
                matched = 0
                for cid in base_cells:
                    pr = cell_pop_r.get(cid, 0.0)
                    pw = cell_pop_w.get(cid, 0.0)
                    pa = pr + pw
                    et, eb, er = _cell_emis(cid)
                    in_center = cell_center.get(cid) is not None
                    if tot_map and cid in tot_map:
                        matched += 1
                    for acc in (reg_acc, zon_acc.get(cell_zone.get(cid))):
                        if acc is None:
                            continue
                        acc["pop_r"] += pr
                        acc["pop_w"] += pw
                        acc["pop_a"] += pa
                        acc["e_total"] += et
                        acc["e_bldg"] += eb
                        acc["e_trans"] += er
                        if in_center:
                            acc["pop_a_center"] += pa
                    cid_c = cell_center.get(cid)
                    if cid_c is not None:
                        ca = cen_acc[cid_c]
                        ca["pop_r"] += pr
                        ca["pop_w"] += pw
                        ca["pop_a"] += pa
                        ca["e_total"] += et
                        ca["e_bldg"] += eb
                        ca["e_trans"] += er

                def _percap(acc):
                    """부문×분모 1인당 (tCO2eq/인). 미가용 부문·0분모는 None."""
                    def _one(e, av):
                        if not av:
                            return {"활동인구": None, "주거인구": None, "근무인구": None}
                        return {
                            "활동인구": _r(e / acc["pop_a"], 3) if acc["pop_a"] > 0 else None,
                            "주거인구": _r(e / acc["pop_r"], 3) if acc["pop_r"] > 0 else None,
                            "근무인구": _r(e / acc["pop_w"], 3) if acc["pop_w"] > 0 else None,
                        }
                    return {"전체": _one(acc["e_total"], av_total),
                            "건물": _one(acc["e_bldg"], av_bldg),
                            "수송": _one(acc["e_trans"], av_trans)}

                def _emis(acc):
                    return {"전체": _r(acc["e_total"], 1) if av_total else None,
                            "건물": _r(acc["e_bldg"], 1) if av_bldg else None,
                            "수송": _r(acc["e_trans"], 1) if av_trans else None}

                def _pack(acc, name, extra=None):
                    d = {"name": name,
                         "pop_r": _r(acc["pop_r"], 0), "pop_w": _r(acc["pop_w"], 0),
                         "pop_a": _r(acc["pop_a"], 0),
                         "emis": _emis(acc), "per": _percap(acc),
                         "conc": (_r(acc["pop_a_center"] / acc["pop_a"] * 100.0, 1)
                                  if acc["pop_a"] > 0 else None)}
                    if extra:
                        d.update(extra)
                    return d

                carbon = {
                    "enabled": True,
                    "year": base_year_label,
                    "sectors_avail": {"전체": av_total, "건물": av_bldg, "수송": av_trans},
                    "region": _pack(reg_acc, region_name),
                    "zones": [_pack(zon_acc[zid], zone_meta[zid]["name"]) for zid in zone_geoms],
                    "centers": [_pack(cen_acc[cid], center_meta[cid]["name"],
                                      {"tier": center_meta[cid].get("hier_raw")})
                                for cid in center_geoms],
                }
                feedback.pushInfo(
                    "탄소 집계 완료: 격자 %d/%d 매칭 · 부문(전체=%s·건물=%s·수송=%s) · 생활권 %d · 중심지 %d"
                    % (matched, len(base_cells), av_total, av_bldg, av_trans,
                       len(zone_geoms), len(center_geoms)))

        # 지도 도형(계획권역 바탕·생활권·위계별 중심지·시군구)과 가이드라인 참조값 메타
        geo = None
        try:
            geo = _geo_payload(region_geom, region_crs, zone_geoms, zone_meta, center_geoms,
                               center_meta, center_zone, sigungu)
            feedback.pushInfo("지도 도형: 생활권 %d · 시군구 %d · 위치도 %s"
                              % (len(geo["zones"]), len(geo["sgg"]), "포함" if geo.get("inset") else "없음"))
        except Exception as e:
            feedback.pushWarning("지도 도형 생성 실패(지도 생략): %s" % e)
        _ct = self.parameterAsEnum(parameters, self.CITY_TYPE, context)
        _tt = self.parameterAsEnum(parameters, self.TREND_TYPE, context)
        _dr = self.parameterAsDouble(parameters, self.DENS_REF, context)
        meta_info = {
            "base_year": base_year_num or None,
            "city_type": (self._CITY_TYPES[_ct] if 0 < _ct < len(self._CITY_TYPES) else None),
            "trend_type": (self._TREND_TYPES[_tt] if 0 < _tt < len(self._TREND_TYPES) else None),
            "dens_ref": _dr if (_dr and _dr > 0) else 2000.0,
        }

        dashboard = {
            "meta": meta_info,
            "geo": geo,
            "region_name": region_name,
            "region_area": _r(region_area, 3),
            "sparse_threshold": _r(sparse_threshold, 1),
            "center_count": center_count,
            "center_area_total": _r(center_area_total, 3),
            "center_ratio": _r(center_ratio),
            "have_center_tier": have_tier,
            "conn_enabled": conn_enabled,
            "conn_meta": ({"thresholds": threshold_mins,
                           "thr_km": [_r(x / 1000.0, 1) for x in thr_m_list]} if conn_enabled else None),
            "fac_enabled": fac_enabled,
            "fac_type": fac_type,
            "fac_groups": (fac_group_list if fac_enabled else None),
            "fac_meta": ({"radius_m": _r(fac_radius, 0), "min_pop": _r(fac_minpop, 0),
                          "radius_hub_m": _r(fac_radius_hub, 0), "min_pop_hub": _r(fac_minpop_hub, 0)}
                         if fac_enabled else None),
            "scenarios": [_sc_payload(sr) for sr in scen_results],
            "kpi_enabled": kpi_enabled,
            "indicator_desc": KPI_INDICATOR_DESC,
            "life_table": life_table,
            "past": past,
            "kpi_region": kpi_region,
            "kpi_zones": [dict({"name": zone_meta[zid]["name"]}, **kpi_zones[zid]) for zid in zone_geoms],
            "kpi_centers": [dict({"cid": str(cid)}, **kpi_centers.get(cid, {})) for cid in center_geoms],
            "basestat": basestat,
            "carbon": carbon,
        }

        html_path = self.parameterAsFileOutput(parameters, self.OUT_HTML, context)
        if html_path:   # 종합 리포트 단계(SCOPE=all)에서만 HTML 생성
            with open(html_path, "w", encoding="utf-8") as fp:
                fp.write(_build_html(dashboard))
            feedback.pushInfo("HTML 대시보드 작성: %s" % html_path)
            if self.parameterAsBool(parameters, self.OPEN_HTML, context):
                try:
                    webbrowser.open("file:///" + html_path.replace("\\", "/"))
                except Exception as e:
                    feedback.pushInfo("브라우저 자동 열기 실패(무시): %s" % e)

        dashboard["run_tag"] = self.parameterAsString(parameters, self.RUN_TAG, context) or ""
        json_path = self.parameterAsFileOutput(parameters, self.OUT_JSON, context)
        json_paths = [json_path] if json_path else []
        if area_dir:   # 일괄 산출: 단계별 JSON 4종 (내용은 같은 전체 결과)
            json_paths += [os.path.join(area_dir, fname) for _, fname in AREA_JSON_NAMES]
        if json_paths:   # 종합 리포트가 재계산 없이 읽는 결과
            payload = json.dumps(dashboard, ensure_ascii=False)
            for jp in json_paths:
                with open(jp, "w", encoding="utf-8") as fp:
                    fp.write(payload)
                feedback.pushInfo("종합 리포트용 결과(JSON) 저장: %s" % jp)

        result = {
            self.OUT_ZONES: zsink_id,
            self.OUT_CENTERS: csink_id,
        }
        if json_path:
            result[self.OUT_JSON] = json_path
        if region_csv:
            result[self.OUT_REGION_CSV] = region_csv
        if html_path:
            result[self.OUT_HTML] = html_path
        if area_csv:
            result[self.OUT_AREA_CSV] = area_csv
        if fsink_id is not None:
            result[self.OUT_FACILITY] = fsink_id
        if fac_csv_path:
            result[self.OUT_FAC_CSV] = fac_csv_path
        return result

    def _write_area_csv(self, path, scope, scen_results, kpi_centers,
                        center_geoms, life_table, feedback):
        """단계 산출: SCOPE(basic/compact/network/life) 영역 결과를 CSV로."""
        sr = scen_results[0] if scen_results else None

        def _v(x, n=1):
            return _r(x, n) if x is not None else ""
        rows = []
        if scope == "basic" and sr is not None:
            rows.append(["[기초통계] 공간단위", "이름", "주거인구", "근무인구",
                         "주거밀도(인/km2)", "근무밀도(인/km2)", "인구과소비율(%)"])
            r0 = sr["region_ts"][0]
            rows.append(["계획권역", sr["name"], _v(r0["pop"], 0), _v(r0["work"], 0),
                         _v(r0["rdens"]), _v(r0["wdens"]), _v(r0["sparse_ratio"])])
            for z in sr["zones"]:
                p = z["per_year"][0]
                rows.append(["생활권", z["name"], _v(p["pop"], 0), "",
                             _v(p["rdens"]), _v(p["wdens"]), _v(p["sparse_ratio"])])
            for c in sr["centers"]:
                p = c["per_year"][0]
                rows.append(["중심지", c["name"], _v(p["pop"], 0), "",
                             _v(p["rdens"]), _v(p["wdens"]), ""])
        elif scope == "compact" and sr is not None:
            rows.append(["[콤팩트성] 공간단위", "이름", "중심지 주거집중도(%)",
                         "중심지 근무집중도(%)", "중심지 주거밀도(인/km2)", "중심지 수"])
            c0 = sr["center_ts"][0]
            rows.append(["계획권역", sr["name"], _v(c0["rconc"]), _v(c0["wconc"]),
                         _v(c0["density"]), len(center_geoms)])
            for z in sr["zones"]:
                p = z["per_year"][0]
                rows.append(["생활권", z["name"], _v(p["crconc"]), _v(p["cwconc"]),
                             _v(p["cdens"]), ""])
        elif scope == "network":
            rows.append(["[네트워크성] 중심지", "위계", "상위 중심지 수", "동급 중심지 수",
                         "상위 평균거리(km)", "동급 평균거리(km)"])
            for cid, c in kpi_centers.items():
                if "tot_up" not in c:
                    continue
                rows.append([c.get("name", str(cid)),
                             c.get("tier_name", c.get("tier", "")),
                             c.get("tot_up", ""), c.get("tot_eq", ""),
                             _v(c.get("mean_up"), 2), _v(c.get("mean_eq"), 2)])
        elif scope == "life" and life_table:
            rows.append(["[생활편리성] 공간단위", "시설", "구분", "천인당 시설수(개/천인)",
                         "중심지 내 시설 비율(%)", "서비스권 향유도(%)",
                         "관리대상 시설 비율(%)", "평균 접근성(km)"])

            def _life_rows(unit_label, rowsobj):
                for tn, r in rowsobj.items():
                    rows.append([unit_label, tn, ("근린" if r["cat"] == "근린" else "거점"),
                                 _v(r["per1000"], 3),
                                 _v(r["ctr_ratio"]) if r["ctr_ratio"] is not None else "-",
                                 _v(r["cover"]) if r["cover"] is not None else "-",
                                 _v(r["mgmt_ratio"]) if r["mgmt_ratio"] is not None else "-",
                                 _v(r["access"], 3)])
            _life_rows("계획권역", life_table["region"])
            for z in life_table["zones"]:
                _life_rows("생활권:" + z["name"], z["rows"])
            for ct in life_table["centers"]:
                _life_rows("중심지:" + ct["name"], ct["rows"])
        if not rows:
            feedback.pushWarning("영역별 CSV: SCOPE '%s' 결과가 비어 있음(입력 확인)." % scope)
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as fp:
            wr = csv.writer(fp)
            for r in rows:
                wr.writerow(r)
        feedback.pushInfo("영역별 결과 CSV(%s) 저장: %s (%d행)" % (scope, path, len(rows) - 1))


# ---------------------------------------------------------------------------
# HTML 대시보드 템플릿
# ---------------------------------------------------------------------------
_MAP_CRS = "EPSG:5179"
_SIDO_SHORT = {"11": "서울", "21": "부산", "22": "대구", "23": "인천", "24": "광주",
               "25": "대전", "26": "울산", "29": "세종"}


def _map_parts(g, tol, min_area):
    """단순화 후 폴리곤 부분 목록 — 면적 min_area(m²) 미만 조각은 제외(최대 조각은 유지)."""
    if g is None or g.isEmpty():
        return []
    s = g.simplify(tol) if tol else g
    if s is None or s.isEmpty():
        s = g
    parts = [p for p in (s.asGeometryCollection() if s.isMultipart() else [s])
             if p is not None and not p.isEmpty() and p.asPolygon()]
    if not parts:
        return []
    keep = [p for p in parts if p.area() >= min_area]
    return keep or [max(parts, key=lambda p: p.area())]


def _svg_d(parts, r):
    """폴리곤 부분들 → SVG path d (y축 반전, r m 단위 반올림)."""
    out, ri = [], int(r)
    for p in parts:
        for ring in p.asPolygon():
            pts, last = [], None
            for pt in ring:
                q = (int(round(pt.x() / r)) * ri, -int(round(pt.y() / r)) * ri)
                if q != last:
                    pts.append(q)
                    last = q
            if len(pts) >= 3:
                out.append("M" + "L".join("%d,%d" % q for q in pts) + "Z")
    return "".join(out)


def _geo_payload(region_geom, region_crs, zone_geoms, zone_meta, center_geoms,
                 center_meta, center_zone, sigungu_src):
    """대시보드 지도용 도형 — EPSG:5179 기준 SVG path."""
    target = QgsCoordinateReferenceSystem(_MAP_CRS)
    prj = QgsProject.instance()

    def tf(src_crs):
        return None if src_crs == target else QgsCoordinateTransform(src_crs, target, prj)

    def to_map(g, ct):
        g2 = QgsGeometry(g)
        if ct is not None:
            g2.transform(ct)
        return g2

    ct = tf(region_crs)
    rg = to_map(region_geom, ct)
    bb = rg.boundingBox()
    pad = 5000
    vb = [round(bb.xMinimum() - pad), round(-bb.yMaximum() - pad),
          round(bb.width() + 2 * pad), round(bb.height() + 2 * pad)]
    base = _svg_d(_map_parts(rg, 150, 0.05e6), 100)                  # 섬 포함 계획권역 바탕
    zones = [{"zid": zid, "name": zone_meta[zid]["name"],
              "d": _svg_d(_map_parts(to_map(zone_geoms[zid], ct), 400, 0.3e6), 100)}
             for zid in zone_geoms]
    tiers = {}
    for cid, g in center_geoms.items():
        t = str(center_meta[cid].get("tier") or 0)
        tiers.setdefault(t, []).append(_svg_d(_map_parts(to_map(g, ct), 150, 0), 100))
    tiers = {t: "".join(v) for t, v in tiers.items()}
    labels = []   # 생활권 이름표 — 생활권 안 최상위 위계 중심지(가장 큰 것) 위, 없으면 생활권 내부점
    for zid, g in zone_geoms.items():
        cands = [c for c, z in center_zone.items() if z == zid and center_meta[c].get("tier")]
        if cands:
            top = min(center_meta[c]["tier"] for c in cands)
            best = max((c for c in cands if center_meta[c]["tier"] == top),
                       key=lambda c: center_meta[c]["area"])
            pt = to_map(center_geoms[best], ct).pointOnSurface().asPoint()
        else:
            pt = to_map(g, ct).pointOnSurface().asPoint()
        labels.append({"zid": zid, "x": round(pt.x()), "y": round(-pt.y())})
    sgg = []
    if sigungu_src is not None:
        names = [f.name() for f in sigungu_src.fields()]
        nf = next((n for n in ("SIGUNGU_NM", "SGG_NM", "sgg_nm", "SIG_KOR_NM", "sig_kor_nm", "name", "NAME")
                   if n in names), None)
        cf = next((n for n in ("SIGUNGU_CD", "SGG_CD", "sgg_cd", "SIG_CD", "sig_cd") if n in names), None)
        if nf:
            sct = tf(sigungu_src.sourceCrs())
            for f in sigungu_src.getFeatures():
                if f.geometry() is None or f.geometry().isEmpty():
                    continue
                nm = str(f[nf]) if f[nf] not in (None, "") else ""
                if cf and nm.endswith("구") and "시" not in nm:
                    pre = _SIDO_SHORT.get(str(f[cf])[:2])
                    if pre:
                        nm = pre + " " + nm
                sgg.append({"name": nm,
                            "d": _svg_d(_map_parts(to_map(f.geometry(), sct), 300, 0.3e6), 100)})
    inset = None
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                               "sido_inset_5179.json"), encoding="utf-8") as fp:
            nat = json.load(fp)
        inset = {"vb": nat["vb"], "sido": [x["d"] for x in nat["sido"]],
                 "region": _svg_d(_map_parts(rg, 2500, 30e6), 500)}
    except Exception:
        inset = None
    return {"vb": vb, "base": base, "zones": zones, "tiers": tiers, "labels": labels,
            "sgg": sgg, "inset": inset}


def _build_html(data):
    payload = json.dumps(data, ensure_ascii=False)
    return _HTML_TEMPLATE.replace("__DATA_JSON__", payload)


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>공간구조 진단 대시보드 (장래 추계)</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
<link rel="stylesheet" href="https://cdn.datatables.net/2.1.8/css/dataTables.dataTables.min.css">
<script src="https://cdn.datatables.net/2.1.8/js/dataTables.min.js"></script>
<style>
  :root{--bg:#f1f5f9;--card:#ffffff;--accent:#2563eb;--accent2:#059669;--muted:#64748b;--text:#1e293b;--border:#e2e8f0;}
  *{box-sizing:border-box;}
  body{margin:0;background:linear-gradient(160deg,#f8fafc,#eef2f7);color:var(--text);
       font-family:'Malgun Gothic','맑은 고딕',system-ui,sans-serif;}
  .wrap{max-width:1200px;margin:0 auto;padding:28px 20px 60px;}
  header h1{margin:0 0 4px;font-size:26px;}
  header .sub{color:var(--muted);font-size:14px;}
  .tabs{display:flex;gap:6px;margin:18px 0 4px;border-bottom:2px solid var(--border);flex-wrap:wrap;}
  .tab{background:none;border:none;padding:10px 18px;font-size:15px;font-weight:600;color:var(--muted);
       cursor:pointer;border-bottom:3px solid transparent;margin-bottom:-2px;font-family:inherit;}
  .tab:hover{color:var(--text);}
  .tab.active{color:var(--accent);border-bottom-color:var(--accent);}
  .panel{display:none;}
  .panel.active{display:block;}
  /* 하위탭(영역: 기초통계·콤팩트성·네트워크성·생활편리성) */
  .subtabs{display:flex;gap:4px;margin:14px 0 2px;flex-wrap:wrap;}
  .subtab{background:#eef2f7;border:1px solid var(--border);border-radius:999px;padding:6px 16px;
       font-size:13.5px;font-weight:600;color:var(--muted);cursor:pointer;font-family:inherit;}
  .subtab:hover{color:var(--text);}
  .subtab.active{background:var(--accent);border-color:var(--accent);color:#fff;}
  .subpanel{display:none;}
  .subpanel.active{display:block;}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin:8px 0 4px;}
  .kpi{background:var(--card);border-radius:14px;padding:18px 20px;border:1px solid var(--border);
       box-shadow:0 1px 3px rgba(15,23,42,.06);}
  .kpi .label{color:var(--muted);font-size:13px;margin-bottom:6px;}
  .kpi .value{font-size:26px;font-weight:700;}
  .kpi .value small{font-size:14px;color:var(--muted);font-weight:400;}
  .kpi .delta{font-size:13px;margin-top:6px;}
  .kpi .sub2{font-size:14px;margin-top:8px;padding-top:8px;border-top:1px dashed var(--border);}
  .kpi .sub2 b{font-weight:700;}
  .tag{color:var(--muted);font-size:12.5px;font-weight:400;}
  .muted{color:var(--muted);font-weight:400;font-size:15px;}
  .up{color:#dc2626;} .down{color:#2563eb;}
  section{background:var(--card);border-radius:14px;padding:18px 20px;margin:18px 0;border:1px solid var(--border);
          box-shadow:0 1px 3px rgba(15,23,42,.06);}
  section h2{margin:0 0 4px;font-size:17px;border-left:4px solid var(--accent);padding-left:10px;}
  .bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:2px 0 12px;color:var(--muted);font-size:13px;}
  .bar select{background:#fff;color:var(--text);border:1px solid var(--border);
       border-radius:6px;padding:5px 9px;font-size:13px;font-family:inherit;}
  .bar .cmp{background:#eff6ff;color:var(--accent);border-radius:10px;padding:2px 9px;font-weight:600;font-size:12px;}
  .ch3{font-size:14px;margin:2px 0 4px;color:var(--text);font-weight:600;}
  .cap{color:var(--muted);font-size:12.5px;margin-top:8px;}
  .grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;}
  .grid2>div{min-width:0;}
  @media(max-width:820px){.grid2{grid-template-columns:minmax(0,1fr);}}
  .grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px;}
  .grid3>div{min-width:0;}
  @media(max-width:900px){.grid3{grid-template-columns:minmax(0,1fr);}}
  .chart{width:100%;height:320px;}
  table.dataTable{color:var(--text);font-size:13px;}
  table.dataTable thead th{color:var(--accent);}
  .dataTables_wrapper .dataTables_filter input,.dataTables_wrapper .dataTables_length select{
     background:#fff;color:var(--text);border:1px solid var(--border);border-radius:6px;}
  .dataTables_wrapper{color:var(--muted);}
  .note{color:var(--muted);font-size:12px;margin-top:8px;}
  .pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;font-weight:600;}
  .pill.ok{background:#dcfce7;color:#166534;} .pill.bad{background:#fee2e2;color:#991b1b;}
  /* 설계 프레임(데이터 미연결) placeholder */
  .frametag{display:inline-block;background:#fef3c7;color:#92400e;border:1px solid #fde68a;border-radius:8px;
     padding:2px 9px;font-size:12px;font-weight:600;margin-left:8px;vertical-align:middle;}
  .ph{border:1.5px dashed #cbd5e1;border-radius:12px;padding:20px;color:var(--muted);
     background:repeating-linear-gradient(45deg,#f8fafc,#f8fafc 10px,#f1f5f9 10px,#f1f5f9 20px);}
  .ph .big{font-size:14px;font-weight:600;color:#475569;margin-bottom:4px;}
  .kpi.ph2{background:repeating-linear-gradient(45deg,#f8fafc,#f8fafc 8px,#f1f5f9 8px,#f1f5f9 16px);
     border-style:dashed;}
  .quad{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:10px 0;}
  .quad .q{border:1px solid var(--border);border-radius:12px;padding:14px 16px;min-height:96px;background:#fff;}
  .quad .q .qh{font-weight:700;font-size:13px;margin-bottom:6px;}
  .quad .q .qd{font-size:12.5px;color:var(--muted);line-height:1.5;}
  .q.good{background:#f0fdf4;border-color:#bbf7d0;} .q.good .qh{color:#166534;}
  .q.warn{background:#fffbeb;border-color:#fde68a;} .q.warn .qh{color:#92400e;}
  .q.risk{background:#fef2f2;border-color:#fecaca;} .q.risk .qh{color:#991b1b;}
  .q.info2{background:#eff6ff;border-color:#bfdbfe;} .q.info2 .qh{color:#1e40af;}
  .axcap{color:var(--muted);font-size:12px;text-align:center;margin:2px 0;}
  /* 중심지 연결성 상세표: 가로 스크롤 + 가운데 정렬 (복합헤더 정렬 안정) */
  .tblscroll{overflow-x:auto;}
  #kpiCenterTable{min-width:1180px;}
  #kpiCenterTable thead th{text-align:center;vertical-align:middle;white-space:nowrap;}
  #kpiCenterTable tbody td{text-align:center;white-space:nowrap;}
  #kpiCenterTable tbody td:first-child, #kpiCenterTable tbody td:nth-child(2){text-align:left;}
  /* 생활권별 기초현황 표: 행열 정렬 */
  #baseStatTable{width:100%;border-collapse:collapse;}
  #baseStatTable th, #baseStatTable td{text-align:center;padding:8px 12px;border-bottom:1px solid var(--border);}
  #baseStatTable th:first-child, #baseStatTable td:first-child{text-align:left;}
  /* 탄소 부하 표 */
  #carbonZoneTable, #carbonCenterTable{width:100%;min-width:760px;border-collapse:collapse;font-size:13px;}
  #carbonZoneTable th, #carbonCenterTable th{color:var(--accent);text-align:center;padding:7px 10px;
     border-bottom:1px solid var(--border);white-space:nowrap;vertical-align:middle;}
  #carbonZoneTable td, #carbonCenterTable td{text-align:right;padding:6px 10px;border-bottom:1px solid var(--border);white-space:nowrap;}
  #carbonZoneTable td:first-child, #carbonCenterTable td:first-child,
  #carbonCenterTable td:nth-child(2){text-align:left;}
  /* 지역 과거 현황 버튼·오버레이 */
  #pastBtn{margin-top:10px;font-size:13px;padding:7px 16px;background:#0f766e;color:#fff;border:none;
     border-radius:9px;cursor:pointer;font-family:inherit;font-weight:600;}
  #pastBtn:hover{background:#115e59;}
  #descBtn{margin-top:10px;margin-right:8px;font-size:13px;padding:7px 16px;background:#475569;color:#fff;
     border:none;border-radius:9px;cursor:pointer;font-family:inherit;font-weight:600;}
  #descBtn:hover{background:#334155;}
  #descWrap{display:none;position:fixed;inset:0;background:rgba(15,23,42,.5);z-index:60;
     align-items:flex-start;justify-content:center;overflow:auto;padding:30px 16px;}
  #descBox{background:#fff;border-radius:16px;padding:22px 26px;max-width:1000px;width:100%;
     box-shadow:0 20px 60px rgba(0,0,0,.3);position:relative;}
  #descClose{position:absolute;top:12px;right:14px;border:none;background:none;font-size:26px;
     color:#64748b;cursor:pointer;line-height:1;}
  #descBox h3{margin:18px 0 6px;font-size:15px;color:var(--accent);}
  table.desc{width:100%;border-collapse:collapse;font-size:12.5px;margin-bottom:6px;}
  table.desc th{background:#f1f5f9;color:var(--text);text-align:left;padding:6px 9px;border-bottom:2px solid var(--border);white-space:nowrap;}
  table.desc td{padding:6px 9px;border-bottom:1px solid var(--border);vertical-align:top;}
  table.desc td:first-child{font-weight:600;white-space:nowrap;}
  table.desc td:last-child{white-space:nowrap;color:var(--muted);}
  #pastWrap{display:none;position:fixed;inset:0;background:rgba(15,23,42,.5);z-index:60;
     align-items:flex-start;justify-content:center;overflow:auto;padding:30px 16px;}
  #pastBox{background:#fff;border-radius:16px;padding:22px 24px;max-width:1100px;width:100%;
     box-shadow:0 20px 60px rgba(0,0,0,.3);position:relative;}
  #pastClose{position:absolute;top:12px;right:14px;border:none;background:none;font-size:26px;
     color:#64748b;cursor:pointer;line-height:1;}
  #pastBox h2{border-left:none;padding-left:0;}
  /* ===== 진단 요약·핵심지표 카드·지도 (가이드라인 표6·표9·표10 형식) ===== */
  .ovctl{display:flex;flex-wrap:wrap;gap:14px;margin-top:12px;font-size:13px;color:var(--muted);align-items:center;}
  .ovctl select,.ovfac select{background:#fff;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:4px 8px;font-size:13px;font-family:inherit;}
  .ovctl b{color:var(--text);}
  .ovh{font-size:16px;margin:24px 0 10px;}
  .ovrow{display:grid;grid-template-columns:1.5fr 1fr;gap:14px;align-items:start;margin-top:14px;}
  @media(max-width:860px){.ovrow{grid-template-columns:minmax(0,1fr);}}
  .ovrow>div{min-width:0;}
  .ovmapbox{position:relative;background:#fff;border:1px solid var(--border);border-radius:12px;padding:8px;}
  .ovkey{width:100%;height:auto;max-height:560px;display:block;}
  .ovbtns{position:absolute;top:12px;left:12px;display:flex;gap:6px;}
  .ovbtns button{font:inherit;font-size:12px;padding:3px 10px;border:1px solid var(--border);border-radius:6px;background:#fff;color:var(--text);cursor:pointer;}
  .ovbtns button.on{background:#eef2f7;font-weight:700;}
  .ovinset{position:absolute;top:42px;left:12px;width:120px;background:#fff;border:1px solid var(--border);border-radius:6px;padding:4px;}
  .ovinset svg{display:block;width:100%;height:auto;}
  .ovleg{display:flex;flex-wrap:wrap;gap:4px 10px;font-size:12px;color:var(--muted);padding:6px 6px 0;align-items:center;}
  .ovleg .lh{font-weight:700;color:var(--text);margin-right:2px;}
  .ovleg i,.sw{display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:4px;vertical-align:-1px;}
  .ovli{border:0;background:none;font:inherit;font-size:12px;color:var(--muted);cursor:pointer;padding:1px 4px;border-radius:4px;}
  .ovli:hover{background:#eef2f7;} .ovli.off{opacity:.4;text-decoration:line-through;}
  .ovhint{font-size:12px;color:var(--muted);padding:4px 6px 2px;}
  .ovzp{cursor:pointer;transition:fill-opacity .15s;} .ovzp:hover{fill-opacity:.5;}
  #ovZone .ovmapbox [data-lyr="sgg"] path{cursor:pointer;}
  #ovTip{position:fixed;display:none;pointer-events:none;background:#1e293b;color:#fff;font-size:12px;padding:3px 8px;border-radius:4px;z-index:80;}
  .ovbasic{background:#fff;border:1px solid var(--border);border-radius:12px;padding:12px 16px;}
  .ovbasic h3{margin:2px 0 10px;font-size:16px;}
  .ovbasic dl{display:grid;grid-template-columns:110px 1fr;row-gap:8px;margin:0;font-size:13.5px;}
  .ovbasic dt{color:var(--muted);} .ovbasic dd{margin:0;font-weight:600;}
  .ovfac{font-size:13px;color:var(--muted);margin:0 0 10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;}
  .ovtiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;}
  .ovtile{border-radius:12px;padding:13px 15px;border:1px solid var(--border);}
  .ovtile .ct{font-size:12px;font-weight:700;} .ovtile .nm{font-size:14px;font-weight:700;margin:2px 0 6px;}
  .ovtile .v{font-size:27px;font-weight:800;letter-spacing:-.5px;}
  .ovtile .row{display:flex;justify-content:space-between;gap:8px;font-size:12.5px;color:var(--muted);margin-top:3px;}
  .ovtile .row span:last-child{color:var(--text);text-align:right;}
  .ov-cmp{background:#e8f0fb;} .ov-cmp .ct{color:#1d4f7c;}
  .ov-net{background:#e4f3ea;} .ov-net .ct{color:#1e6a44;}
  .ov-life{background:#fdf1dc;} .ov-life .ct{color:#7a4f00;}
  .ovcap{font-weight:700;margin:24px 0 8px;font-size:15px;}
  .ovtw{overflow-x:auto;}
  table.ovtbl{border-collapse:collapse;width:100%;background:#fff;font-size:13px;}
  table.ovtbl th,table.ovtbl td{border:1px solid var(--border);padding:6px 8px;text-align:center;}
  table.ovtbl th{background:#eef2f7;font-weight:700;}
  table.ovtbl td.l{text-align:left;}
  table.ovtbl a{color:inherit;}
  .ovnote{font-size:12px;color:var(--muted);margin-top:6px;line-height:1.6;}
  .ovcard{background:#fff;border:1px solid var(--border);border-radius:12px;margin:14px 0;overflow:hidden;}
  .ovcard .hd{padding:10px 16px;display:flex;gap:10px;align-items:center;}
  .ovcard .hd .tg{margin-left:auto;font-size:12px;}
  .ovcard .bd{display:grid;grid-template-columns:1.15fr 1fr;gap:18px;padding:14px 16px;}
  @media(max-width:860px){.ovcard .bd{grid-template-columns:minmax(0,1fr);}}
  .ovkv{display:grid;grid-template-columns:130px 1fr;row-gap:6px;font-size:13.5px;margin:0;}
  .ovkv dt{color:var(--muted);} .ovkv dd{margin:0;}
  .ovformula{background:#f3f6f9;border-radius:6px;padding:6px 10px;font-size:13px;margin-bottom:8px;}
  .ovspark{width:100%;height:auto;background:#f8fafc;border-radius:6px;}
  .ovrefbar{position:relative;height:12px;background:#eef2f7;border-radius:6px;margin-top:10px;}
  .ovrefbar i{position:absolute;left:0;top:0;bottom:0;background:#9fbad6;border-radius:6px;}
  .ovrefbar em{position:absolute;top:-4px;bottom:-4px;width:2px;background:#1e293b;}
  .ovsmall{font-size:11.5px;color:var(--muted);margin-top:4px;}
  .ovcard details{font-size:13px;color:#3e4c59;margin-top:8px;} .ovcard summary{cursor:pointer;color:var(--muted);}
  .ovlinks{border-top:1px dashed var(--border);padding:8px 16px;font-size:12.5px;color:var(--muted);}
  .ovzdet{background:#fff;border:1px solid var(--border);border-radius:12px;margin:14px 0;transition:box-shadow .3s;}
  .ovzdet.flash{box-shadow:0 0 0 3px #9fbad6;}
  .ovzdet .zh{display:flex;align-items:center;gap:8px;padding:10px 16px;border-bottom:1px solid var(--border);}
  .ovzdet .zh i{width:14px;height:14px;border-radius:3px;display:inline-block;}
  .ovzdet .zb{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;padding:12px 16px;}
  .ovbar{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;font-size:13px;color:var(--muted);background:#fff;border:1px solid var(--border);border-radius:8px;padding:7px 12px;margin:10px 0;}
  .ovbar select{background:#fff;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:3px 8px;font-size:13px;font-family:inherit;}
  .ovbar b{color:var(--text);}
  .ovsleg{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:11.5px;color:var(--muted);margin-top:4px;}
  .ovsleg i{display:inline-block;width:14px;height:3px;border-radius:2px;margin-right:4px;vertical-align:3px;}
  h2.ovdetail{font-size:15px;color:var(--muted);border-left:none;padding-left:0;margin:30px 0 0;padding-top:14px;border-top:1px solid var(--border);}
  #ovZone .ovrow{grid-template-columns:1.2fr 1fr;}
  #ovZoneQuick table.ovtbl{font-size:12.5px;}
  #ovZoneQuick th,#ovZoneQuick td{white-space:nowrap;}
  /* 성과 평가 지원 버튼·오버레이 (목표치 설정 → 평가 실행) */
  #evalBtn{margin-top:10px;margin-left:8px;font-size:13px;padding:7px 16px;background:#c2410c;color:#fff;border:none;
     border-radius:9px;cursor:pointer;font-family:inherit;font-weight:600;}
  #evalBtn:hover{background:#9a3412;}
  #evalWrap{display:none;position:fixed;inset:0;background:rgba(15,23,42,.5);z-index:60;
     align-items:flex-start;justify-content:center;overflow:auto;padding:30px 16px;}
  #evalBox{background:#fff;border-radius:16px;padding:22px 24px;max-width:1180px;width:100%;
     box-shadow:0 20px 60px rgba(0,0,0,.3);position:relative;}
  #evalClose{position:absolute;top:12px;right:14px;border:none;background:none;font-size:26px;
     color:#64748b;cursor:pointer;line-height:1;}
  #evalBox h2{border-left:none;padding-left:0;}
  .evset{border:1px solid var(--border);border-radius:12px;padding:12px 16px;background:#f8fafc;margin-top:10px;}
  .evset .evh{font-weight:700;font-size:14px;margin:0 0 8px;color:var(--text);}
  .evrow{display:flex;flex-wrap:wrap;gap:8px 18px;align-items:center;font-size:13px;color:var(--muted);margin-bottom:8px;}
  .evrow b{color:var(--text);}
  .evset select,.evset input{background:#fff;color:var(--text);border:1px solid var(--border);border-radius:6px;
     padding:4px 8px;font-size:13px;font-family:inherit;}
  .evset input[type=number]{width:76px;text-align:right;}
  .evset input[readonly]{background:#f1f5f9;color:#475569;}
  table.evtg{width:100%;border-collapse:collapse;background:#fff;font-size:13px;margin-top:4px;}
  table.evtg th,table.evtg td{border:1px solid var(--border);padding:6px 9px;text-align:center;}
  table.evtg th{background:#eef2f7;font-weight:700;}
  table.evtg td.l{text-align:left;font-weight:600;}
  table.evtg td.rl{text-align:left;color:var(--muted);font-size:12.5px;}
  .evrun{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:10px;}
  #evRun{font-size:14px;padding:8px 22px;background:#c2410c;color:#fff;border:none;border-radius:9px;cursor:pointer;
     font-family:inherit;font-weight:700;}
  #evRun:hover{background:#9a3412;}
  .evhint{font-size:12px;color:var(--muted);}
  #evalResult{margin-top:16px;}
  .evempty{border:1.5px dashed #cbd5e1;border-radius:12px;padding:26px;text-align:center;color:var(--muted);font-size:13.5px;}
  /* 평가 결과 */
  .evres{color:#1f2937;}
  .evres .evchips span{display:inline-block;border:1px solid #d6dde6;border-radius:6px;padding:4px 10px;margin:0 6px 6px 0;
     font-size:13px;color:#475569;background:#f8fafc;}
  .evres .evleg{font-size:13px;color:#475569;margin:6px 0 14px;}
  .evres .evleg i{display:inline-block;width:26px;height:0;border-top:3px solid;vertical-align:middle;margin:0 6px 0 14px;}
  .evres .evleg i.vt{width:0;height:14px;border-top:0;border-left:2px dotted #64748b;}
  .evres .evgrid{display:grid;grid-template-columns:1fr 1fr;gap:16px;}
  @media(max-width:860px){.evres .evgrid,.evres .evsum{grid-template-columns:minmax(0,1fr);}}
  .evres .evcard{border:1px solid #dfe5ec;border-radius:12px;padding:14px 16px;min-width:0;}
  .evres .evch{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;}
  .evres .evtag{font-size:12px;color:#64748b;}
  .evres .evtt{font-size:18px;font-weight:700;margin-top:2px;}
  .evres .evun{font-size:12px;color:#64748b;}
  .evres .evbd{border-radius:8px;padding:6px 10px;font-weight:700;font-size:15px;text-align:right;white-space:nowrap;}
  .evres .evbd span{display:block;font-size:11px;font-weight:500;}
  .evres svg .ax{font-size:11px;fill:#7b8794;}
  .evres table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:2px;}
  .evres th{color:#64748b;font-weight:600;text-align:right;padding:2px 4px;}
  .evres td{text-align:right;padding:2px 4px;border-top:1px solid #f1f4f8;}
  .evres td.rl{text-align:left;font-weight:600;}
  .evres td.b{font-weight:800;}
  .evres .evsup{font-size:12px;color:#334155;margin-top:7px;line-height:1.45;}
  .evres .evsup,.evres .evnote{display:flex;align-items:baseline;}   /* 라벨 뒤 내용이 넘어가면 내용 칸 안에서 줄바꿈 */
  .evres .evsup b,.evres .evnote b{margin-right:8px;flex:none;}
  .evres .evnote .evmemo{flex:1;}
  .evres .evnote{font-size:12px;background:#fff7ed;border-radius:6px;padding:7px 9px;margin-top:7px;line-height:1.45;}
  .evres .evref{font-size:11.5px;color:#64748b;margin-top:6px;}
  .evres .evsum{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px;}
  .evres .evsum>div{border:1px solid #dfe5ec;border-radius:12px;padding:12px 16px;font-size:13px;line-height:1.55;}
  .evres .evsum b{display:block;margin-bottom:4px;}
  .evres .evauto{color:#334155;}
  .evres .evfn{font-size:11.5px;color:#64748b;margin-top:10px;}
  /* 사용자 메모(해석) 칸 — 화면에서 직접 입력 */
  .evmemo{outline:none;border-radius:4px;min-height:1.4em;}
  .evmemo:focus{background:#fff;box-shadow:0 0 0 2px #fdba74;}
  .evmemo:empty:before{content:attr(data-ph);color:#94a3b8;}
  .evsum .evmemo{display:block;margin-top:4px;padding:2px 0;}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1 id="title">공간구조 진단 대시보드 (장래 추계)</h1>
    <div class="sub" id="subtitle"></div>
    <button id="descBtn">지표 설명 보기</button>
    <button id="pastBtn" style="display:none">지역 과거 현황 보기</button>
    <button id="evalBtn">성과 평가 지원</button>
    <div class="ovctl" style="display:none">
      <label>시나리오 <select id="ovScen"></select></label>
      <label>과거 비교연도 <select id="ovPast"></select></label>
      <span>기준연도 <b id="ovBase"></b></span>
      <label>목표연도 <select id="ovTarget"></select></label>
    </div>
  </header>

  <div class="tabs">
    <button class="tab active" data-tab="overview">진단 요약</button>
    <button class="tab" data-tab="summary">계획권역</button>
    <button class="tab" data-tab="centers">중심부(위계별)</button>
    <button class="tab" data-tab="zones">생활권별</button>
  </div>

  <!-- ================= 진단 요약 ================= -->
  <div class="panel active" id="panel-overview">
    <div class="ovrow">
      <div id="ovMapSum"></div>
      <div class="ovbasic"><h3>기초정보</h3><dl id="ovBasic"></dl></div>
    </div>
    <h3 class="ovh">핵심지표</h3>
    <div class="ovbar" data-ov="spbtnh"></div>
    <div class="ovfac" id="ovFacWrap" style="display:none">서비스권 향유도·관리 대상 표시 시설 — 근린 <select id="ovFacN"></select> 거점 <select id="ovFacH"></select></div>
    <div class="ovtiles" id="ovTiles"></div>
    <div class="ovcap" id="ovT9cap"></div>
    <div class="ovbar" data-ov="spbtnh"></div>
    <div class="ovtw"><table class="ovtbl" id="ovT9"></table></div>
    <div class="ovnote" id="ovT9note"></div>
    <div class="ovcap" id="ovT10cap"></div>
    <div class="ovbar" data-ov="sbtnh"></div>
    <div class="ovtw"><table class="ovtbl" id="ovT10"></table></div>
    <div class="ovnote" id="ovT10note"></div>
  </div>

  <!-- ================= 계획권역 ================= -->
  <div class="panel" id="panel-summary">
    <div class="ovbar" data-ov="spbtnh"></div>
    <div id="ovReg"></div>
    <h2 class="ovdetail">상세 분석</h2>
    <div class="subtabs">
      <button class="subtab active" data-sub="basic">기초통계</button>
      <button class="subtab" data-sub="compact">콤팩트성</button>
      <button class="subtab" data-sub="network">네트워크성</button>
      <button class="subtab" data-sub="life">생활편리성</button>
    </div>

    <!-- 기초통계 -->
    <div class="subpanel active" data-sub="basic">
      <section>
        <h2>핵심 지표 <span class="tag">시나리오·목표시점 선택</span></h2>
        <div class="bar">
          <label>시나리오</label><select id="kpiScen"></select>
          <label>목표시점</label><select id="kpiYear"></select>
          <span class="note">이 선택은 아래 핵심 지표 박스에만 적용됩니다</span>
        </div>
        <div class="kpis" id="kpis"></div>
      </section>
      <section>
        <h2>기초통계 추이 <span class="tag">면적·인구·밀도</span></h2>
        <div class="bar"><label>보기</label><select id="sumTrendScen"></select>
          <span class="cmp" id="sumTrendTag">시나리오 비교</span></div>
        <div class="grid2">
          <div><div class="ch3">총인구 추이</div><div id="chartPop" class="chart"></div><div class="cap" id="capPop"></div></div>
          <div><div class="ch3">인구밀도 추이</div><div id="chartDens" class="chart"></div><div class="cap" id="capDens"></div></div>
        </div>
      </section>
    </div>

    <!-- 콤팩트성 -->
    <div class="subpanel" data-sub="compact">
      <section>
        <h2>콤팩트성 추이 <span class="tag">중심지 집적·인구과소</span></h2>
        <div class="note" style="margin-bottom:8px">시나리오·비교 선택은 기초통계 탭의 '보기'와 연동됩니다.</div>
        <div class="grid2">
          <div><div class="ch3">중심지 인구 수 추이</div><div id="chartCtrPop" class="chart"></div><div class="cap" id="capCtrPop"></div></div>
          <div><div class="ch3">중심지 인구밀도 추이</div><div id="chartCtrDens" class="chart"></div><div class="cap" id="capCtrDens"></div></div>
        </div>
        <div class="grid2" style="margin-top:22px">
          <div><div class="ch3">중심지 인구 집중도 추이</div><div id="chartConc" class="chart"></div><div class="cap" id="capConc"></div></div>
          <div><div class="ch3">인구과소지역 비율 추이</div><div id="chartSparse" class="chart"></div>
            <div class="note" id="sparseNote"></div><div class="cap" id="capSparse"></div></div>
        </div>
      </section>

      <section id="carbonRegionSec">
        <h2>탄소 부하 <span class="tag">계획권역</span><span class="tag" id="carbonYearTag"></span></h2>
        <div class="kpis" id="cbRegCards"></div>
        <div class="ch3">총배출 1인당 (분모 비교)</div>
        <div id="cbRegChart" class="chart"></div>
        <div class="note">막대 = 총배출, 범례 = 분모(총인구·거주인구·근무인구). 기준연도 실측(탄소공간지도). 단위 tCO₂eq/인.</div>
      </section>
    </div>

    <!-- 네트워크성 -->
    <div class="subpanel" data-sub="network">
      <section>
        <h2>네트워크성 <span class="tag">중심지 단위 지표</span></h2>
        <div class="note">네트워크성 지표(중심지 간 연결성·접근 형평성·N분내 상위·동급 중심지 수/비율)는
          <b>중심지별 탭</b>에서 확인합니다. 계획권역·생활권 단위의 격자→중심지 접근성 차트는
          격자→중심지 OD 입력이 있을 때만 아래에 표시됩니다(현재 미입력 시 숨김).</div>
      </section>
      <section id="secConnWrap">
        <h2>중심지-주변 연결성 / 접근 형평성 <span class="tag">격자→중심지 OD 필요</span></h2>
        <div class="bar"><label>보기</label><select id="connScen"></select>
          <label>목표시점</label><select id="connYear"></select>
          <span id="connThrWrap"><label>임계치</label><select id="connThr"></select></span>
          <span class="cmp" id="connTag">시나리오 비교</span></div>
        <div class="grid2">
          <div><div class="ch3">중심지-주변 연결성</div><div id="chartConn" class="chart"></div><div class="note" id="connNote"></div></div>
          <div><div class="ch3">중심지 접근 형평성</div><div id="chartEqty" class="chart"></div>
            <div class="note">누적 위계별 · 도달 가능 거주인구 비율(100−미도달) · 도로망 고정 가정</div></div>
        </div>
      </section>
    </div>

    <!-- 생활편리성 -->
    <div class="subpanel" data-sub="life">
      <section>
        <h2>생활편리성 <span class="tag">계획권역</span></h2>
        <div class="tblscroll"><table id="lifeRegionTable" class="display" style="width:100%"></table></div>
        <div class="note">천인당 시설수(개/천인) · 중심지 내 시설 비율(%) · 서비스권 향유도(%) · 관리대상 시설 비율(%) · 평균 접근성(km). 시설 서비스반경 기준.</div>
      </section>
      <section>
        <h2>시설 추이</h2>
        <div class="bar" id="facTrendBar">
          <label>구분</label><select id="sumFacCat"></select>
          <label>시설</label><select id="sumFacType"></select></div>
        <div class="grid3" id="facTrendRow">
          <div><div class="ch3" id="lblSumFacCover"></div><div id="chartFacCover" class="chart"></div><div class="cap" id="capFacCover"></div></div>
          <div><div class="ch3" id="lblSumFacAlive"></div><div id="chartFacAlive" class="chart"></div><div class="cap" id="capFacAlive"></div></div>
          <div><div class="ch3" id="lblSumFacMgmt"></div><div id="chartFacMgmt" class="chart"></div><div class="cap" id="capFacMgmt"></div></div>
        </div>
      </section>
    </div>
  </div>

  <!-- ================= 생활권별 ================= -->
  <div class="panel" id="panel-zones">
    <div id="ovZone">
      <div class="ovbar" data-ov="sbtnh"></div>
      <div class="ovrow"><div id="ovZoneMap"></div><div class="ovbasic" id="ovZoneQuick"></div></div>
      <div id="ovZoneSecs"></div>
    </div>
    <h2 class="ovdetail">상세 분석</h2>
    <div class="subtabs">
      <button class="subtab active" data-sub="basic">기초통계</button>
      <button class="subtab" data-sub="compact">콤팩트성</button>
      <button class="subtab" data-sub="network">네트워크성</button>
      <button class="subtab" data-sub="life">생활편리성</button>
    </div>

    <!-- 기초통계 -->
    <div class="subpanel active" data-sub="basic">
      <section>
        <h2>연도별 생활권 인구 비중 변화</h2>
        <div class="bar"><label>시나리오</label><select id="zShareScen"></select></div>
        <div id="chartZoneShare" class="chart"></div>
        <div class="note">각 생활권 인구 ÷ 전체 생활권 인구 합 (%) — 연도별 누적</div>
      </section>
      <section>
        <h2>생활권별 지표</h2>
        <div class="bar"><label>시나리오</label><select id="zTableScen"></select>
          <label>목표시점</label><select id="zTableYear"></select></div>
        <table id="zoneTable" class="display" style="width:100%"></table>
        <div class="note">밀도 단위: 인/km² · 집중도: 생활권 인구 중 중심지 인구 비중(%)</div>
      </section>
      <section>
        <h2>생활권 선택 추이 <span class="tag">기초통계</span></h2>
        <div class="bar"><label>생활권</label><select id="zTrendZone"></select>
          <label>보기</label><select id="zTrendScen"></select>
          <span class="cmp" id="zTrendTag">시나리오 비교</span></div>
        <div class="grid2">
          <div><div class="ch3">총인구 추이</div><div id="chartZPop" class="chart"></div><div class="cap" id="capZPop"></div></div>
          <div><div class="ch3">인구밀도 추이</div><div id="chartZDens" class="chart"></div><div class="cap" id="capZDens"></div></div>
        </div>
      </section>
    </div>

    <!-- 콤팩트성 -->
    <div class="subpanel" data-sub="compact">
      <section>
        <h2>생활권 선택 추이 <span class="tag">콤팩트성</span></h2>
        <div class="note" style="margin-bottom:8px">생활권·보기 선택은 기초통계 탭과 연동됩니다.</div>
        <div class="grid2">
          <div><div class="ch3">중심지 인구 수 추이</div><div id="chartZCpop" class="chart"></div><div class="cap" id="capZCpop"></div></div>
          <div><div class="ch3">중심지 인구밀도 추이</div><div id="chartZCdens" class="chart"></div><div class="cap" id="capZCdens"></div></div>
        </div>
        <div class="grid2" style="margin-top:22px">
          <div><div class="ch3">중심지 인구 집중도 추이</div><div id="chartZConc" class="chart"></div><div class="cap" id="capZConc"></div></div>
          <div><div class="ch3">인구과소지역 비율 추이</div><div id="chartZSparse" class="chart"></div><div class="cap" id="capZSparse"></div></div>
        </div>
      </section>

      <section id="carbonZoneSec">
        <h2>탄소 부하 <span class="tag">생활권별</span></h2>
        <div class="grid3">
          <div><div class="ch3">총배출 1인당</div><div id="cbZoneTotal" class="chart"></div></div>
          <div><div class="ch3">건물부문 1인당</div><div id="cbZoneBldg" class="chart"></div></div>
          <div><div class="ch3">수송부문 1인당</div><div id="cbZoneTrans" class="chart"></div></div>
        </div>
        <div class="ch3" style="margin-top:16px">중심지 총인구 집중도 (%)</div>
        <div id="cbZoneConc" class="chart" style="height:260px"></div>
        <div class="ch3" style="margin-top:16px">생활권별 값 (표)</div>
        <div class="tblscroll"><table id="carbonZoneTable" class="display" style="width:100%"></table></div>
        <div class="note">각 부문 차트: x=생활권, 범례=분모 3종(총인구·거주·근무). 집중도 = 생활권 총인구 중 중심지 총인구 비중(%). 단위 tCO₂eq/인.</div>
      </section>
    </div>

    <!-- 네트워크성 -->
    <div class="subpanel" data-sub="network">
      <section>
        <h2>네트워크성 <span class="tag">생활권별</span></h2>
        <div class="note">중심지 간 연결성·접근 형평성은 중심지별 탭에서 확인합니다. (생활권 단위 네트워크 지표는 준비 중)</div>
      </section>
    </div>

    <!-- 생활편리성 -->
    <div class="subpanel" data-sub="life">
      <section>
        <h2>생활권 생활편리성 <span class="tag">생활권별</span></h2>
        <div class="bar"><label>생활권</label><select id="lifeZoneSel"></select></div>
        <div class="tblscroll"><table id="lifeZoneTable" class="display" style="width:100%"></table></div>
        <div class="note">천인당 시설수(개/천인) · 중심지 내 시설 비율(%) · 서비스권 향유도(%) · 관리대상 시설 비율(%) · 평균 접근성(km).</div>
      </section>
      <section>
        <h2>생활권 시설 추이</h2>
        <div class="bar" id="facZTrendBar">
          <label>생활권·보기는 기초통계 탭과 연동 · 구분</label><select id="zFacCat"></select>
          <label>시설</label><select id="zFacType"></select></div>
        <div class="grid3" id="facZTrendRow">
          <div><div class="ch3" id="lblZCover"></div><div id="chartZCover" class="chart"></div><div class="cap" id="capZCover"></div></div>
          <div><div class="ch3" id="lblZAlive"></div><div id="chartZAlive" class="chart"></div><div class="cap" id="capZAlive"></div></div>
          <div><div class="ch3" id="lblZMgmt"></div><div id="chartZMgmt" class="chart"></div><div class="cap" id="capZMgmt"></div></div>
        </div>
      </section>

      <section id="facTableWrap">
        <h2>시설별 배후인구 · 폐지시점</h2>
        <div class="bar"><label>시나리오</label><select id="facTableScen"></select>
          <label>목표시점</label><select id="facTableYear"></select>
          <label>생활권</label><select id="facTableZone"></select>
          <label>구분</label><select id="facTableCat"></select>
          <label>시설</label><select id="facTableType"></select></div>
        <table id="facTable" class="display" style="width:100%"></table>
        <div class="note" id="facTableNote"></div>
      </section>
    </div>
  </div>

  <!-- ================= 중심지별 ================= -->
  <div class="panel" id="panel-centers">
    <div id="ovCtr">
      <div class="ovbar" data-ov="sbt"></div>
      <div class="ovrow"><div id="ovCtrMap"></div><div class="ovbasic" id="ovCtrCum"></div></div>
      <div id="ovCtrRef"></div>
    </div>
    <h2 class="ovdetail">상세 분석</h2>
    <div class="subtabs">
      <button class="subtab active" data-sub="basic">기초통계</button>
      <button class="subtab" data-sub="compact">콤팩트성</button>
      <button class="subtab" data-sub="network">네트워크성</button>
      <button class="subtab" data-sub="life">생활편리성</button>
    </div>

    <!-- 기초통계 -->
    <div class="subpanel active" data-sub="basic">
      <section>
        <h2>중심지 위계별 지표 <span class="tag">1차 / 2차 / 3차</span></h2>
        <div class="bar"><label>시나리오</label><select id="ctScen"></select>
          <label>목표시점</label><select id="ctYear"></select></div>
        <table id="ctTable" class="display" style="width:100%"></table>
        <div class="note">위계별 집계 — 중심지 개수·면적·주거/근무 인구·밀도·인구 비중·연평균 변화율</div>
      </section>
      <section>
        <h2>위계별 인구 수 / 밀도</h2>
        <div id="ctBar" class="chart"></div>
      </section>
      <section>
        <h2>위계별 추이 <span class="tag">기초통계</span></h2>
        <div class="bar"><label>보기</label><select id="ctTrendScen"></select>
          <span class="cmp" id="ctTrendTag">시나리오 비교</span></div>
        <div class="grid2">
          <div><div class="ch3">위계별 총인구 추이</div><div id="ctPop" class="chart"></div></div>
          <div><div class="ch3">위계별 인구밀도 추이</div><div id="ctDens" class="chart"></div></div>
        </div>
      </section>
    </div>

    <!-- 콤팩트성 -->
    <div class="subpanel" data-sub="compact">
      <section>
        <h2>위계별 중심지 인구 비중 추이</h2>
        <div class="bar"><label>시나리오</label><select id="ctShareScen"></select></div>
        <div id="ctShare" class="chart"></div>
        <div class="note">각 위계 중심지 인구 ÷ 계획권역 인구 (%) — 연도별</div>
      </section>

      <section id="carbonCenterSec">
        <h2>탄소 부하 <span class="tag">중심지별 · 1인당 상위 15개(총인구)</span></h2>
        <div class="grid3">
          <div><div class="ch3">총배출 1인당</div><div id="cbCenTotal" class="chart"></div></div>
          <div><div class="ch3">건물부문 1인당</div><div id="cbCenBldg" class="chart"></div></div>
          <div><div class="ch3">수송부문 1인당</div><div id="cbCenTrans" class="chart"></div></div>
        </div>
        <div class="ch3" style="margin-top:16px">중심지별 값 (표 · 전체)</div>
        <div class="tblscroll"><table id="carbonCenterTable" class="display" style="width:100%"></table></div>
        <div class="note">막대 = 총인구 1인당 배출 상위 15개 중심지 · 범례 = 분모 3종. 표는 전체 중심지. 단위 tCO₂eq/인.</div>
      </section>
    </div>

    <!-- 네트워크성 -->
    <div class="subpanel" data-sub="network">
      <section id="kpiCenterConn">
        <h2>중심지 간 연결성 <span class="tag">위계별 · 시간권</span></h2>
        <div class="bar"><label>중심지</label><select id="connCenterSel"></select>
          <label>시각화</label><select id="connVizMode">
            <option value="basic">막대 · 꺾은선 (기본)</option>
            <option value="radar">레이더 (삼각 · 사각)</option></select></div>
        <div class="grid2" id="connRadarWrap">
          <div><div class="ch3">시간권별 도달 중심지 수</div><div id="chartConnRadarDist" class="chart"></div>
            <div class="note">30·60·90·120분 내 도달 가능한 상위·동급 중심지 '수'. 선택 중심지 기준(전체 평균 선택 시 전체 중심지 평균).</div></div>
          <div><div class="ch3">시간권별 도달률</div><div id="chartConnRadarReach" class="chart"></div>
            <div class="note">30·60·90·120분 내 도달 가능 비율(%) = 도달 수 ÷ 상위(동급) 총 수. 선택 중심지 기준.</div></div>
        </div>
        <div class="ch3" style="margin-top:18px">중심지별 연결성 상세</div>
        <div class="tblscroll"><table id="kpiCenterTable" class="display" style="width:100%"></table></div>
        <div class="note">※ 상위·동급 중심지 총 수 + 시간권(30·60·90·120분) 내 도달 상위·동급 중심지 수·비율(%). 도로거리·40km/h.</div>
      </section>
    </div>

    <!-- 생활편리성 -->
    <div class="subpanel" data-sub="life">
      <section>
        <h2>중심지 생활편리성 <span class="tag">위계별</span></h2>
        <div class="bar"><label>중심지 위계</label><select id="lifeCenterSel"></select></div>
        <div class="tblscroll"><table id="lifeCenterTable" class="display" style="width:100%"></table></div>
        <div class="note">중심지 단위는 천인당 시설수·평균 접근성만 산출 — 중심지 내 시설 비율·서비스권 향유도·관리대상 비율은 '-'(산출 불가).</div>
      </section>
    </div>
  </div>

  <!-- ================= 탄소 부하 ================= -->
</div>

<!-- 지표 설명 오버레이 -->
<div id="descWrap">
  <div id="descBox">
    <button id="descClose">&times;</button>
    <h2>KPI 지표 설명 <span class="tag">영역별 산출 목록</span></h2>
    <div id="descBody"></div>
    <div class="note">각 지표의 정의·산출 방법·해석 관점. 네트워크성의 접근 형평성은 격자→중심지 OD 입력이 있을 때만 산출됩니다.</div>
  </div>
</div>

<!-- 지역 과거 현황 오버레이 -->
<div id="pastWrap">
  <div id="pastBox">
    <button id="pastClose">&times;</button>
    <h2>지역 과거 현황 <span class="tag">전국 과거 격자(주거) · 2000~기준연도</span></h2>
    <div class="sub" id="pastSub"></div>
    <div class="grid2" style="margin-top:10px">
      <div><div class="ch3">총인구 추이 (계획권역·중심지)</div><div id="pastPop" class="chart"></div></div>
      <div><div class="ch3">중심지 인구 집중도 추이</div><div id="pastConc" class="chart"></div></div>
    </div>
    <div class="grid2" style="margin-top:16px">
      <div><div class="ch3">생활권별 인구 추이</div><div id="pastZone" class="chart"></div></div>
      <div><div class="ch3">인구밀도 추이 (계획권역·중심지)</div><div id="pastDens" class="chart"></div></div>
    </div>
    <div class="note">전국 과거 1km 격자 주거 총인구(SGIS)를 격자코드로 매칭·집계. 근무 과거 시계열은 없어 주거 기준. 마지막 점 = 기준연도(현재 배분 주거인구).</div>
  </div>
</div>

<!-- 성과 평가 지원 오버레이 (목표치 설정 → 평가 실행) -->
<div id="evalWrap">
  <div id="evalBox">
    <button id="evalClose">&times;</button>
    <h2>성과 평가 지원 <span class="tag">목표치 설정 → 평가 실행 · 목표연도 실적과 목표 비교</span></h2>
    <div class="evset" id="evSet">
      <div class="evh">평가 기준 설정</div>
      <div class="evrow" id="evRow1"></div>
      <div class="evrow" id="evRow2"></div>
      <table class="evtg"><thead><tr><th>핵심지표</th><th>산출 기준</th><th>기준연도 값(%)</th><th>목표 설정 방식</th><th>목표치(평가 시점, %)</th></tr></thead>
        <tbody id="evTgBody"></tbody></table>
      <div class="evrun"><button id="evRun">평가 실행</button>
        <span class="evhint" id="evHint"></span></div>
    </div>
    <div id="evalResult"><div class="evempty">평가 기준을 확인하고 <b>평가 실행</b>을 누르면 핵심지표별 달성 여부가 표시됩니다.</div></div>
  </div>
</div>

<div id="ovTip"></div>
<script>
const D = __DATA_JSON__;
const SC = D.scenarios;
const fmt = (v,d=0)=> (v===null||v===undefined||Number.isNaN(v))? "-" : Number(v).toLocaleString("ko-KR",{maximumFractionDigits:d});
document.getElementById("title").textContent = D.region_name + " 공간구조 진단";
document.title = D.region_name + " 공간구조 진단";
document.getElementById("subtitle").textContent =
  "기준연도 " + ((D.meta&&D.meta.base_year)||SC[0].years[0]) + " · 시점 " + SC[0].years.join(", ") + " · 지역 면적 " + fmt(D.region_area,1) + " km²";
document.getElementById("sparseNote").textContent =
  "1km²당 주거인구 " + fmt(D.sparse_threshold,1) + "인 이하 격자 비율";
const FT = D.fac_type || "시설";   // 시설 종류명 (하드코딩 대신 파라미터)

// ---- 시설 그룹 드롭박스 (구분 전체/근린/거점 · 시설 전체/각시설) ----
const FGROUPS = D.fac_groups || [];
const FGBYKEY = {}; FGROUPS.forEach(g=>FGBYKEY[g.key]=g);
function fgLabel(key){ return (FGBYKEY[key]&&FGBYKEY[key].label) || FT; }
function facFillCat(catSel){
  catSel.innerHTML="";
  catSel.appendChild(new Option("전체","전체"));
  if(FGROUPS.some(g=>g.cat==="근린")) catSel.appendChild(new Option("저차","근린"));
  if(FGROUPS.some(g=>g.cat==="거점")) catSel.appendChild(new Option("고차","거점"));
}
function facFillType(catSel,typeSel){
  const cat=catSel.value; typeSel.innerHTML="";
  const catLbl=c=>({"근린":"저차","거점":"고차"}[c]||c);
  if(cat==="전체"){ typeSel.appendChild(new Option("전체 시설","전체")); typeSel.value="전체"; return; }
  typeSel.appendChild(new Option(catLbl(cat)+" 전체",cat));
  FGROUPS.filter(g=>g.key.indexOf("t:")===0 && g.cat===cat).forEach(g=>typeSel.appendChild(new Option(g.label,g.key)));
  typeSel.value=cat;
}
function facGroupKey(catSel,typeSel){
  if(!catSel || catSel.value==="전체") return "전체";
  return (typeSel && typeSel.value) || catSel.value;
}
// fac 시계열(그룹) 안전 접근
function facSeries(s,gk){ return (s.fac_ts_g && s.fac_ts_g[gk]) ? s.fac_ts_g[gk] : s.fac_ts; }
function zfacSeries(z,gk){ return (z.fac_g && z.fac_g[gk]) ? z.fac_g[gk] : z.fac; }

const PAL=["#2563eb","#059669","#f59e0b","#dc2626","#7c3aed","#0891b2"];       // 시나리오 색
const EPAL=["#2563eb","#059669","#f59e0b","#dc2626","#7c3aed","#0891b2",       // 엔티티 색(고정)
            "#db2777","#65a30d","#0284c7","#ca8a04","#9333ea","#e11d48"];
const CMP="__cmp__";
const baseLayout=(extra)=>Object.assign({
  paper_bgcolor:"rgba(0,0,0,0)", plot_bgcolor:"rgba(0,0,0,0)",
  font:{color:"#334155"}, margin:{t:16,r:20,b:40,l:64},
  legend:{orientation:"h",y:-0.22}, xaxis:{gridcolor:"#e5e7eb"}, yaxis:{gridcolor:"#e5e7eb"},
}, extra||{});
const cfg={displayModeBar:false, responsive:true};
// 계열(라인) 수가 많으면 범례가 커져 플롯이 눌리므로 컨테이너 높이를 늘려 최소 플롯 높이 확보
function chartH(nSeries){ return nSeries<=3 ? 320 : Math.min(360 + Math.ceil(nSeries/3)*28, 1400); }
// 연도축 라벨: 첫 시점(기준년도)에 "(기준)" 표기 → 모든 차트에서 기준년도 명시
function bx(arr){ return arr.map((v,i)=> i===0 ? v+" (기준)" : v); }

// ---------- 셀렉트 채우기 ----------
function fillScen(el, withCompare){
  el.innerHTML="";
  if(withCompare) el.appendChild(new Option("시나리오 비교","__cmp__"));
  SC.forEach((s,i)=>el.appendChild(new Option(s.name,i)));
  el.value = withCompare? "__cmp__" : "0";
}
function fillYears(el, sIdx){
  el.innerHTML="";
  SC[sIdx].years.forEach((y,i)=>el.appendChild(new Option(y+(i===0?" (기준)":""),i)));
  el.value = SC[sIdx].years.length-1;
}
function scenOf(el){ const v=el.value; return v===CMP? CMP : (+v); }

// ---------- 추이 라인 + 캡션 ----------
function drawLines(divId, series, yTitle){
  Plotly.react(divId, series.map(s=>({x:bx(s.x), y:s.y, name:s.name, type:"scatter",
     mode:"lines+markers", line:{color:s.color, dash:s.dash||"solid"}})),
     baseLayout({xaxis:{type:"category",gridcolor:"#e5e7eb"}, yaxis:{title:yTitle,gridcolor:"#e5e7eb"}}), cfg);
}
const DASH=["solid","dot","dash","dashdot","longdash","longdashdot"];
function trendCap(capId, series, isPct){
  const el=document.getElementById(capId); if(!el) return;
  el.innerHTML = "기준시점 대비 목표시점 증감 — " + series.map(s=>{
    const y=s.y, v0=y[0], vb=y[y.length-1];
    if(v0===null||v0===undefined||vb===null||vb===undefined) return `${s.name} <b>-</b>`;
    let val,unit;
    if(isPct==="count"){ val=vb-v0; unit="개"; }
    else if(isPct){ val=vb-v0; unit="%p"; }
    else { if(v0===0) return `${s.name} <b>-</b>`; val=(vb-v0)/v0*100; unit="%"; }
    const cls=val>=0?"up":"down", sg=val>=0?"+":"";
    return `${s.name} <b class="${cls}">${sg}${fmt(val,1)}${unit}</b>`;
  }).join("   ·   ");
}

// ================================================================
// 전체 요약
// ================================================================
const kpiScen=document.getElementById("kpiScen"), kpiYear=document.getElementById("kpiYear");
fillScen(kpiScen,false); fillYears(kpiYear,0);
function deltaHtml(ch){
  if(!ch || ch.cagr_pct===null || ch.cagr_pct===undefined) return "";
  const v=ch.cagr_pct, cls=v>=0?"up":"down", arrow=v>=0?"▲":"▼";
  return `<div class="delta ${cls}">연평균 ${arrow} ${fmt(Math.abs(v),2)}% <span class="muted">(전체기간)</span></div>`;
}
function renderKPIs(){
  const si=+kpiScen.value, s=SC[si], yi=Math.min(+kpiYear.value, s.years.length-1);
  const r=s.region_ts[yi], c=s.center_ts[yi], yL=s.years[yi];
  const cards=[
    {label:"중심지 수", value:fmt(D.center_count)+'<small> 개</small>'},
    {label:"중심지 면적 비율", value:fmt(D.center_ratio,1)+'<small> %</small>'},
    {label:`주거인구 총계 (${yL})`, value:fmt(r.pop)+'<small> 명</small>', delta:deltaHtml(s.region_change.pop)},
    {label:`주거밀도 (${yL})`, value:fmt(r.rdens,1)+'<small> 인/km²</small>'},
    {label:`주거인구 집중도 (${yL})`, value:fmt(c.rconc,1)+'<small> %</small>'},
    {label:`중심지 인구 (${yL})`, value:fmt(c.pop)+'<small> 명</small>'},
    {label:`중심지 인구밀도 (${yL})`, value:fmt(c.density,1)+'<small> 인/km²</small>'},
    {label:`인구과소지역 비율 (${yL})`, value:fmt(r.sparse_ratio,1)+'<small> %</small>'},
  ];
  if(D.conn_enabled && s.conn_ts && s.conn_ts[yi]){
    const cc=s.conn_ts[yi], mn=D.conn_meta.thresholds[0];
    cards.push({label:"중심지-주변 연결성", value:fmt(cc.conn[0][0],1)+'<small> %</small>',
                sub:`<span class="tag">최상위(${cc.levels[0]}) · ${mn}분</span>`});
    cards.push({label:"중심지 접근 형평성", value:fmt(cc.equity[0][0],1)+'<small> %</small>',
                sub:`<span class="tag">최상위(${cc.levels[0]}) · ${mn}분</span>`});
  }
  if(D.fac_enabled && s.fac_ts && s.fac_ts[yi]){
    const t=s.fac_ts[yi];
    cards.push({label:`${FT} 서비스권 향유도 (${yL})`, value:fmt(t.cover,1)+'<small> %</small>'});
    cards.push({label:`관리대상 ${FT} (${yL})`, value:fmt(t.mgmt)+'<small> / '+fmt(t.total)+' 개</small>'});
  }
  document.getElementById("kpis").innerHTML = cards.map(k=>
    `<div class="kpi"><div class="label">${k.label}</div><div class="value">${k.value}</div>`+
    `${k.delta||""}${k.sub?`<div class="sub2">${k.sub}</div>`:""}</div>`).join("");
}
kpiScen.addEventListener("change", ()=>{ fillYears(kpiYear,+kpiScen.value); renderKPIs(); });
kpiYear.addEventListener("change", renderKPIs);

// ---- 지역 지표 추이 (기본 시나리오 비교) ----
const sumTrendScen=document.getElementById("sumTrendScen");
fillScen(sumTrendScen,true);
if(!D.fac_enabled) document.getElementById("facTrendRow").style.display="none";
function renderSummaryTrends(){
  const mode=scenOf(sumTrendScen);
  document.getElementById("sumTrendTag").style.display = (mode===CMP)?"":"none";
  let SFGK="전체";
  if(D.fac_enabled){
    SFGK=facGroupKey(document.getElementById("sumFacCat"),document.getElementById("sumFacType"));
    const gl=fgLabel(SFGK), setT=(id,t)=>{const e=document.getElementById(id); if(e)e.textContent=t;};
    setT("lblSumFacCover", gl+" 서비스권 향유도 추이");
    setT("lblSumFacAlive", "지속가능 "+gl+" 수 추이");
    setT("lblSumFacMgmt", "관리대상(폐지후보) "+gl+" 수 추이");
  }
  if(mode===CMP){
    drawLines("chartPop", SC.map((s,i)=>({x:s.years,y:s.region_ts.map(r=>r.pop),name:s.name,color:PAL[i%PAL.length]})),"인구(명)");
    drawLines("chartDens", SC.map((s,i)=>({x:s.years,y:s.region_ts.map(r=>r.rdens),name:s.name,color:PAL[i%PAL.length]})),"밀도(인/km²)");
    drawLines("chartConc", SC.map((s,i)=>({x:s.years,y:s.center_ts.map(r=>r.rconc),name:s.name,color:PAL[i%PAL.length]})),"%");
    drawLines("chartSparse", SC.map((s,i)=>({x:s.years,y:s.region_ts.map(r=>r.sparse_ratio),name:s.name,color:PAL[i%PAL.length]})),"%");
    trendCap("capPop",SC.map((s,i)=>({name:s.name,y:s.region_ts.map(r=>r.pop)})),false);
    trendCap("capDens",SC.map((s,i)=>({name:s.name,y:s.region_ts.map(r=>r.rdens)})),false);
    trendCap("capConc",SC.map((s,i)=>({name:s.name,y:s.center_ts.map(r=>r.rconc)})),true);
    trendCap("capSparse",SC.map((s,i)=>({name:s.name,y:s.region_ts.map(r=>r.sparse_ratio)})),true);
    drawLines("chartCtrPop", SC.map((s,i)=>({x:s.years,y:s.center_ts.map(r=>r.pop),name:s.name,color:PAL[i%PAL.length]})),"인구(명)");
    drawLines("chartCtrDens", SC.map((s,i)=>({x:s.years,y:s.center_ts.map(r=>r.density),name:s.name,color:PAL[i%PAL.length]})),"밀도(인/km²)");
    trendCap("capCtrPop",SC.map(s=>({name:s.name,y:s.center_ts.map(r=>r.pop)})),false);
    trendCap("capCtrDens",SC.map(s=>({name:s.name,y:s.center_ts.map(r=>r.density)})),false);
    if(D.fac_enabled){
      const ff=SC.filter(s=>s.fac_ts);
      drawLines("chartFacCover", ff.map((s,i)=>({x:s.years,y:facSeries(s,SFGK).map(x=>x.cover),name:s.name,color:PAL[i%PAL.length]})),"%");
      drawLines("chartFacAlive", ff.map((s,i)=>({x:s.years,y:facSeries(s,SFGK).map(x=>x.alive),name:s.name,color:PAL[i%PAL.length]})),"시설 수(개)");
      drawLines("chartFacMgmt", ff.map((s,i)=>({x:s.years,y:facSeries(s,SFGK).map(x=>x.mgmt),name:s.name,color:PAL[i%PAL.length]})),"시설 수(개)");
      trendCap("capFacCover",ff.map(s=>({name:s.name,y:facSeries(s,SFGK).map(x=>x.cover)})),true);
      trendCap("capFacAlive",ff.map(s=>({name:s.name,y:facSeries(s,SFGK).map(x=>x.alive)})),"count");
      trendCap("capFacMgmt",ff.map(s=>({name:s.name,y:facSeries(s,SFGK).map(x=>x.mgmt)})),"count");
    }
  }else{
    const s=SC[mode];
    drawLines("chartPop",[{x:s.years,y:s.region_ts.map(r=>r.pop),name:"계획권역",color:"#2563eb"}],"인구(명)");
    drawLines("chartDens",[{x:s.years,y:s.region_ts.map(r=>r.rdens),name:"계획권역",color:"#2563eb"}],"밀도(인/km²)");
    drawLines("chartConc",[{x:s.years,y:s.center_ts.map(r=>r.rconc),name:"주거인구 집중도",color:"#059669"}],"%");
    drawLines("chartSparse",[{x:s.years,y:s.region_ts.map(r=>r.sparse_ratio),name:"인구과소지역 비율",color:"#dc2626"}],"%");
    trendCap("capPop",[{name:"계획권역",y:s.region_ts.map(r=>r.pop)}],false);
    trendCap("capDens",[{name:"계획권역",y:s.region_ts.map(r=>r.rdens)}],false);
    trendCap("capConc",[{name:"집중도",y:s.center_ts.map(r=>r.rconc)}],true);
    trendCap("capSparse",[{name:"과소비율",y:s.region_ts.map(r=>r.sparse_ratio)}],true);
    drawLines("chartCtrPop",[{x:s.years,y:s.center_ts.map(r=>r.pop),name:"중심지 인구",color:"#7c3aed"}],"인구(명)");
    drawLines("chartCtrDens",[{x:s.years,y:s.center_ts.map(r=>r.density),name:"중심지 인구밀도",color:"#7c3aed"}],"밀도(인/km²)");
    trendCap("capCtrPop",[{name:"중심지 인구",y:s.center_ts.map(r=>r.pop)}],false);
    trendCap("capCtrDens",[{name:"중심지 인구밀도",y:s.center_ts.map(r=>r.density)}],false);
    if(D.fac_enabled && s.fac_ts){
      const fs=facSeries(s,SFGK);
      drawLines("chartFacCover",[{x:s.years,y:fs.map(x=>x.cover),name:"향유도",color:"#059669"}],"%");
      drawLines("chartFacAlive",[{x:s.years,y:fs.map(x=>x.alive),name:"지속가능 시설 수",color:"#059669"}],"시설 수(개)");
      drawLines("chartFacMgmt",[{x:s.years,y:fs.map(x=>x.mgmt),name:"관리대상 시설 수",color:"#dc2626"}],"시설 수(개)");
      trendCap("capFacCover",[{name:"향유도",y:fs.map(x=>x.cover)}],true);
      trendCap("capFacAlive",[{name:"지속가능 수",y:fs.map(x=>x.alive)}],"count");
      trendCap("capFacMgmt",[{name:"관리대상 수",y:fs.map(x=>x.mgmt)}],"count");
    }
  }
}
sumTrendScen.addEventListener("change", renderSummaryTrends);
if(D.fac_enabled){
  const _sc=document.getElementById("sumFacCat"), _st=document.getElementById("sumFacType");
  facFillCat(_sc); facFillType(_sc,_st);
  _sc.addEventListener("change",()=>{ facFillType(_sc,_st); renderSummaryTrends(); });
  _st.addEventListener("change", renderSummaryTrends);
}else{ const b=document.getElementById("facTrendBar"); if(b) b.style.display="none"; }

// ---- 연결성 / 형평성 (기본 시나리오 비교, 목표시점 내장) ----
const connScen=document.getElementById("connScen"), connYear=document.getElementById("connYear"),
      connThr=document.getElementById("connThr");
const palC=["#93c5fd","#3b82f6","#1d4ed8"], palE=["#fca5a5","#ef4444","#b91c1c"];
if(!D.conn_enabled){ document.getElementById("secConnWrap").style.display="none"; }
else{ fillScen(connScen,true); fillYears(connYear,0);
  D.conn_meta.thresholds.forEach((mn,i)=>connThr.appendChild(new Option(mn+"분",i)));
  connThr.value="0"; }
function connGrp(extra){ return baseLayout(Object.assign({barmode:"group",
  xaxis:{type:"category",gridcolor:"#e5e7eb"}}, extra||{})); }
function renderConn(){
  if(!D.conn_enabled) return;
  const mode=scenOf(connScen), yi=+connYear.value, THR=D.conn_meta.thresholds, ti=+connThr.value;
  document.getElementById("connTag").style.display=(mode===CMP)?"":"none";
  document.getElementById("connThrWrap").style.display=(mode===CMP)?"":"none";  // 임계치는 비교모드에서만
  if(mode===CMP){
    // x=누적위계, 계열=시나리오, 선택 임계치(ti) 기준
    const base=SC.find(s=>s.conn_ts && s.conn_ts[Math.min(yi,s.years.length-1)]);
    const lv = base? base.conn_ts[Math.min(yi,base.years.length-1)].levels : [];
    const at=(s,arr)=>{ const j=Math.min(yi,s.years.length-1); const c=s.conn_ts&&s.conn_ts[j]; return c? c[arr][ti] : null; };
    Plotly.react("chartConn", SC.map((s,i)=>({x:lv, y:(at(s,"conn")||[]), name:s.name, type:"bar", marker:{color:PAL[i%PAL.length]}})),
      connGrp({yaxis:{title:"연결성(%)",gridcolor:"#e5e7eb",rangemode:"tozero"}}), cfg);
    Plotly.react("chartEqty", SC.map((s,i)=>({x:lv, y:(at(s,"equity")||[]), name:s.name, type:"bar", marker:{color:PAL[i%PAL.length]}})),
      connGrp({yaxis:{title:"접근 형평성(%)",gridcolor:"#e5e7eb",rangemode:"tozero"}}), cfg);
    document.getElementById("connNote").textContent =
      `목표시점 ${SC[0].years[Math.min(yi,SC[0].years.length-1)]} · ${THR[ti]}분 기준 · 시나리오 비교 · 도로망 고정`;
  }else{
    const s=SC[mode], j=Math.min(yi,s.years.length-1), cc=s.conn_ts&&s.conn_ts[j];
    if(!cc){ Plotly.react("chartConn",[],baseLayout()); Plotly.react("chartEqty",[],baseLayout()); return; }
    Plotly.react("chartConn", THR.map((mn,ti)=>({x:cc.levels,y:cc.conn[ti],name:mn+"분",type:"bar",marker:{color:palC[ti%3]}})),
      connGrp({yaxis:{title:"연결성(%)",gridcolor:"#e5e7eb",rangemode:"tozero"}}), cfg);
    Plotly.react("chartEqty", THR.map((mn,ti)=>({x:cc.levels,y:cc.equity[ti],name:mn+"분",type:"bar",marker:{color:palE[ti%3]}})),
      connGrp({yaxis:{title:"접근 형평성(%)",gridcolor:"#e5e7eb",rangemode:"tozero"}}), cfg);
    document.getElementById("connNote").textContent =
      `${s.name} · ${s.years[j]} · 임계시간별(차량 10분당 5km) 최근린 중심지 도달 거주격자 비율 · 도로망 고정`;
  }
}
connScen.addEventListener("change", renderConn);
connYear.addEventListener("change", renderConn);
if(connThr) connThr.addEventListener("change", renderConn);

// ================================================================
// 생활권별
// ================================================================
const DTL={search:"검색:", lengthMenu:"_MENU_ 개씩", info:"_TOTAL_건 중 _START_–_END_",
  paginate:{next:"다음",previous:"이전"}, zeroRecords:"데이터 없음", infoEmpty:"0건"};
// 중심지 위계 순 정렬 (tier 오름차순=상위 위계 먼저, null 뒤, 동위계는 이름순)
function connTierSort(a,b){
  var ta=(a.tier==null)?9999:a.tier, tb=(b.tier==null)?9999:b.tier;
  if(ta!==tb) return ta-tb;
  return (a.name||"").localeCompare(b.name||"");
}

// ---- 생활권 인구 비중 (시나리오 선택) ----
const zShareScen=document.getElementById("zShareScen");
fillScen(zShareScen,false);
function renderZoneShare(){
  const s=SC[+zShareScen.value];
  const totals=s.years.map((y,i)=> s.zones.reduce((a,z)=>a+(z.per_year[i].pop||0),0));
  Plotly.react("chartZoneShare",
    s.zones.map((z,zi)=>({x:bx(s.years),name:z.name,type:"bar",marker:{color:EPAL[zi%EPAL.length]},
      y:z.per_year.map((p,i)=> totals[i]? p.pop/totals[i]*100 : null)})),
    baseLayout({barmode:"stack",xaxis:{type:"category",gridcolor:"#e5e7eb"},
                yaxis:{title:"인구 비중(%)",gridcolor:"#e5e7eb"}}), cfg);
}
zShareScen.addEventListener("change", renderZoneShare);

// ---- 생활권 지표 표 (시나리오/목표시점) ----
const zTableScen=document.getElementById("zTableScen"), zTableYear=document.getElementById("zTableYear");
fillScen(zTableScen,false); fillYears(zTableYear,0);
let zoneDT=null;
function buildZoneTable(){
  const s=SC[+zTableScen.value], yi=Math.min(+zTableYear.value,s.years.length-1), yL=s.years[yi];
  if(zoneDT){zoneDT.destroy(); $("#zoneTable").empty();}
  const facOn = D.fac_enabled && s.zones.some(z=>z.fac);
  const cols=["생활권","면적(km²)",`인구(${yL})`,`주거밀도(${yL})`,`중심지 주거집중도%(${yL})`,`인구과소비율%(${yL})`,"인구 연평균변화율%"];
  if(facOn) cols.push(`향유도%(${yL})`,`관리대상수(${yL})`);
  $("#zoneTable").html("<thead><tr>"+cols.map(c=>`<th>${c}</th>`).join("")+"</tr></thead>");
  zoneDT=new DataTable("#zoneTable",{pageLength:10,language:DTL,
    data:s.zones.map(z=>{const p=z.per_year[yi],ch=z.change.pop;
      const row=[z.name,fmt(z.area,2),fmt(p.pop),fmt(p.rdens,1),fmt(p.crconc,1),fmt(p.sparse_ratio,1),ch?fmt(ch.cagr_pct,2):"-"];
      if(facOn){const zf=z.fac?z.fac[yi]:null; row.push(zf?fmt(zf.cover,1):"-", zf?fmt(zf.mgmt):"-");}
      return row;}),
    columns:cols.map(()=>({}))});
}
zTableScen.addEventListener("change", ()=>{ fillYears(zTableYear,+zTableScen.value); buildZoneTable(); });
zTableYear.addEventListener("change", buildZoneTable);

// ---- 생활권 선택 추이 (기본 시나리오 비교) ----
const zTrendZone=document.getElementById("zTrendZone"), zTrendScen=document.getElementById("zTrendScen");
zTrendZone.appendChild(new Option("전체 (모든 생활권)","__all__"));
SC[0].zones.forEach((z,i)=>zTrendZone.appendChild(new Option(z.name,i)));
zTrendZone.value = SC[0].zones.length? "0" : "__all__";
fillScen(zTrendScen,true);
// 생활권 추이 지표: per_year 기반 + (시설) fac 기반
const ZMETRICS=[
  {div:"chartZPop",  cap:"capZPop",  y:"인구(명)",    pct:false,   get:(s,zi)=>s.zones[zi].per_year.map(p=>p.pop)},
  {div:"chartZDens", cap:"capZDens", y:"밀도(인/km²)",pct:false,   get:(s,zi)=>s.zones[zi].per_year.map(p=>p.rdens)},
  {div:"chartZCpop", cap:"capZCpop", y:"인구(명)",    pct:false,   get:(s,zi)=>s.zones[zi].per_year.map(p=>p.cpop)},
  {div:"chartZCdens",cap:"capZCdens",y:"밀도(인/km²)",pct:false,   get:(s,zi)=>s.zones[zi].per_year.map(p=>p.cdens)},
  {div:"chartZConc", cap:"capZConc", y:"%",           pct:true,    get:(s,zi)=>s.zones[zi].per_year.map(p=>p.crconc)},
  {div:"chartZSparse",cap:"capZSparse",y:"%",         pct:true,    get:(s,zi)=>s.zones[zi].per_year.map(p=>p.sparse_ratio)},
];
let ZFGK="전체";
if(D.fac_enabled) ZMETRICS.push(
  {div:"chartZCover",cap:"capZCover",y:"%",           pct:true,    get:(s,zi)=>{const f=zfacSeries(s.zones[zi],ZFGK);return f?f.map(x=>x.cover):[];}},
  {div:"chartZAlive",cap:"capZAlive",y:"시설 수(개)", pct:"count", get:(s,zi)=>{const f=zfacSeries(s.zones[zi],ZFGK);return f?f.map(x=>x.alive):[];}},
  {div:"chartZMgmt", cap:"capZMgmt", y:"시설 수(개)", pct:"count", get:(s,zi)=>{const f=zfacSeries(s.zones[zi],ZFGK);return f?f.map(x=>x.mgmt):[];}},
);
else{ if(document.getElementById("facZTrendRow")) document.getElementById("facZTrendRow").style.display="none";
      if(document.getElementById("facZTrendBar")) document.getElementById("facZTrendBar").style.display="none"; }
// 모드별 series 빌더: 개별생활권→시나리오선 / 전체+비교→생활권×시나리오(색=생활권,점선=시나리오) / 전체+개별→생활권선
function zoneSeries(get){
  const zsel=zTrendZone.value, mode=scenOf(zTrendScen), cmp=(mode===CMP);
  if(zsel==="__all__"){
    if(cmp){
      const out=[];
      SC[0].zones.forEach((z,zi)=>SC.forEach((s,si)=>out.push(
        {x:s.years, y:get(s,zi), name:`${s.zones[zi].name} · ${s.name}`,
         color:EPAL[zi%EPAL.length], dash:DASH[si%DASH.length]})));
      return out;
    }
    const s=SC[mode];
    return s.zones.map((z,zi)=>({x:s.years, y:get(s,zi), name:z.name, color:EPAL[zi%EPAL.length]}));
  }
  const zi=+zsel;
  if(cmp) return SC.map((s,si)=>({x:s.years, y:get(s,zi), name:s.name, color:PAL[si%PAL.length]}));
  const s=SC[mode];
  return [{x:s.years, y:get(s,zi), name:s.zones[zi].name, color:"#2563eb"}];
}
function renderZoneTrend(){
  const cmp=(scenOf(zTrendScen)===CMP);
  document.getElementById("zTrendTag").style.display = cmp?"":"none";
  if(D.fac_enabled){
    ZFGK=facGroupKey(document.getElementById("zFacCat"),document.getElementById("zFacType"));
    const gl=fgLabel(ZFGK), setT=(id,t)=>{const e=document.getElementById(id); if(e)e.textContent=t;};
    setT("lblZCover", gl+" 서비스권 향유도 추이");
    setT("lblZAlive", "지속가능 "+gl+" 수 추이");
    setT("lblZMgmt", "관리대상(폐지후보) "+gl+" 수 추이");
  }
  ZMETRICS.forEach(m=>{ const ser=zoneSeries(m.get);
    const el=document.getElementById(m.div); if(el) el.style.height=chartH(ser.length)+"px";   // 라인 수만큼 높이 확대
    drawLines(m.div,ser,m.y); trendCap(m.cap,ser,m.pct); });
}
zTrendZone.addEventListener("change", renderZoneTrend);
zTrendScen.addEventListener("change", renderZoneTrend);
if(D.fac_enabled){
  const _zc=document.getElementById("zFacCat"), _zt=document.getElementById("zFacType");
  facFillCat(_zc); facFillType(_zc,_zt);
  _zc.addEventListener("change",()=>{ facFillType(_zc,_zt); renderZoneTrend(); });
  _zt.addEventListener("change", renderZoneTrend);
}

// ---- 시설별 표 (시나리오/목표시점) ----
const facTableScen=document.getElementById("facTableScen"), facTableYear=document.getElementById("facTableYear"),
      facTableZone=document.getElementById("facTableZone"),
      facTableCat=document.getElementById("facTableCat"), facTableType=document.getElementById("facTableType");
let facDT=null;
if(!D.fac_enabled){ document.getElementById("facTableWrap").style.display="none"; }
else{
  fillScen(facTableScen,false); fillYears(facTableYear,0);
  facTableZone.appendChild(new Option("전체 생활권","__all__"));
  SC[0].zones.forEach(z=>facTableZone.appendChild(new Option(z.name, z.name)));
  facTableZone.value="__all__";
  facFillCat(facTableCat); facFillType(facTableCat,facTableType);
  facTableCat.addEventListener("change",()=>{ facFillType(facTableCat,facTableType); buildFacTable(); });
  facTableType.addEventListener("change", buildFacTable);
  document.getElementById("facTableWrap").querySelector("h2").textContent = FT+"별 배후인구 · 폐지시점";
  document.getElementById("facTableNote").textContent =
    "배후인구 = 서비스반경(직선) 근린 "+fmt(D.fac_meta.radius_m,0)+"m · 거점 "+fmt(facRad("거점"),0)+
    "m 내 격자 주거인구 합 · 최소 근린 "+fmt(D.fac_meta.min_pop,0)+"명 · 거점 "+fmt(facMinPop("거점"),0)+
    "명 미만 시점부터 관리대상(폐지후보)";
}
function facMinPop(cat){   // 근린·거점 최소 배후인구 (옛 결과는 근린 값 하나)
  const m=D.fac_meta||{};
  return (cat==="거점" && m.min_pop_hub!=null)? m.min_pop_hub : m.min_pop;
}
function facRad(cat){
  const m=D.fac_meta||{};
  return (cat==="거점" && m.radius_hub_m!=null)? m.radius_hub_m : m.radius_m;
}
function facItemInGroup(it,gk){
  if(gk==="전체") return true;
  if(gk==="근린"||gk==="거점") return it.cat===gk;
  if(gk.indexOf("t:")===0) return it.type===gk.slice(2);
  return true;
}
function buildFacTable(){
  if(!D.fac_enabled) return;
  const s=SC[+facTableScen.value], yi=Math.min(+facTableYear.value,s.years.length-1), yL=s.years[yi];
  if(!s.fac_items) return;
  const zf=facTableZone.value, gk=facGroupKey(facTableCat,facTableType);
  const rows=s.fac_items.filter(it=> (zf==="__all__" || it.zone===zf) && facItemInGroup(it,gk));
  if(facDT){facDT.destroy(); $("#facTable").empty();}
  const cols=["시설명","유형","구분","생활권","기준 배후인구",`목표 배후인구(${yL})`,"폐지시점",`상태(${yL})`];
  $("#facTable").html("<thead><tr>"+cols.map(c=>`<th>${c}</th>`).join("")+"</tr></thead>");
  facDT=new DataTable("#facTable",{pageLength:10,language:DTL,
    data:rows.map(it=>{
      const alive = it.bp[yi] >= facMinPop(it.cat);
      const status = alive? '<span class="pill ok">생존</span>' : '<span class="pill bad">관리대상</span>';
      return [it.name, it.type||"-", it.cat||"-", it.zone||"-", fmt(it.bp[0]), fmt(it.bp[yi]), it.close_year||"생존", status];}),
    columns:cols.map(()=>({}))});
}
facTableScen.addEventListener("change", ()=>{ fillYears(facTableYear,+facTableScen.value); buildFacTable(); });
facTableYear.addEventListener("change", buildFacTable);
facTableZone.addEventListener("change", buildFacTable);

// ================================================================
// 중심지별
// ================================================================
const TIERLBL={1:"1차중심지",2:"2차중심지",3:"3차중심지"};
const presentTiers=(()=>{const s=new Set(); SC[0].centers.forEach(c=>{if(c.tier)s.add(c.tier);}); return [...s].sort();})();
function centerFilterByType(centers, tsel){
  if(tsel==="__all__") return centers.map((c,i)=>({c,i}));
  const t=+tsel; return centers.map((c,i)=>({c,i})).filter(o=>o.c.tier===t);
}
function top10(list, yi){   // list of {c,i} → 상위10 by pop at yi
  return list.slice().sort((a,b)=>(b.c.per_year[yi].pop||0)-(a.c.per_year[yi].pop||0)).slice(0,10);
}

// ================= 중심지 위계별(1차/2차/3차) =================
const ctScen=document.getElementById("ctScen"), ctYear=document.getElementById("ctYear");
if(ctScen){ fillScen(ctScen,false); fillYears(ctYear,0); }
let ctDT=null;
function buildCtTable(){
  if(!ctScen) return;
  const s=SC[+ctScen.value], yi=Math.min(+ctYear.value,s.years.length-1), yL=s.years[yi], T=s.center_tiers||[];
  if(ctDT){ ctDT.destroy(); $("#ctTable").empty(); }
  const cols=["위계","중심지 수","면적(km²)",`주거인구(${yL})`,`근무인구(${yL})`,`주거밀도(${yL})`,`근무밀도(${yL})`,`인구비중%(${yL})`,"인구 연평균변화율%"];
  $("#ctTable").html("<thead><tr>"+cols.map(c=>`<th>${c}</th>`).join("")+"</tr></thead>");
  ctDT=new DataTable("#ctTable",{paging:false,searching:false,info:false,ordering:false,language:DTL,
    data:T.map(t=>{const p=t.per_year[yi],ch=t.change.pop;
      return [t.name,t.count,fmt(t.area,2),fmt(p.pop),fmt(p.wpop),fmt(p.rdens,1),fmt(p.wdens,1),fmt(p.pshare,1),ch?fmt(ch.cagr_pct,2):"-"];}),
    columns:cols.map(()=>({}))});
}
function drawCtBar(){
  if(!document.getElementById("ctBar")||!ctScen) return;
  const s=SC[+ctScen.value], yi=Math.min(+ctYear.value,s.years.length-1), T=s.center_tiers||[];
  Plotly.react("ctBar",[
    {x:T.map(t=>t.name),y:T.map(t=>t.per_year[yi].pop),name:"주거인구",type:"bar",marker:{color:"#2563eb"}},
    {x:T.map(t=>t.name),y:T.map(t=>t.per_year[yi].rdens),name:"주거밀도(인/km²)",type:"scatter",mode:"lines+markers",yaxis:"y2",line:{color:"#f59e0b"},marker:{color:"#f59e0b"}},
  ], baseLayout({margin:{t:16,r:76,b:44,l:66},xaxis:{type:"category"},
      yaxis:{title:"인구(명)",gridcolor:"#e5e7eb",automargin:true},
      yaxis2:{title:{text:"밀도(인/km²)",standoff:12},overlaying:"y",side:"right",gridcolor:"transparent",rangemode:"tozero"}}), cfg);
}
function renderCtBasic(){ buildCtTable(); drawCtBar(); }
if(ctScen){ ctScen.addEventListener("change",()=>{ fillYears(ctYear,+ctScen.value); renderCtBasic(); });
  ctYear.addEventListener("change",renderCtBasic); }

// 위계별 인구 비중 추이
const ctShareScen=document.getElementById("ctShareScen");
if(ctShareScen) fillScen(ctShareScen,false);
function renderCtShare(){
  if(!ctShareScen) return;
  const s=SC[+ctShareScen.value], T=s.center_tiers||[];
  Plotly.react("ctShare", T.map((t,ti)=>({x:bx(s.years),name:t.name,type:"bar",marker:{color:EPAL[ti%EPAL.length]},
    y:t.per_year.map(p=>p.pshare)})),
    baseLayout({barmode:"stack",xaxis:{type:"category",gridcolor:"#e5e7eb"},yaxis:{title:"인구 비중(%)",gridcolor:"#e5e7eb"}}), cfg);
}
if(ctShareScen) ctShareScen.addEventListener("change",renderCtShare);

// 위계별 추이 (총인구·밀도)
const ctTrendScen=document.getElementById("ctTrendScen");
if(ctTrendScen) fillScen(ctTrendScen,true);
function renderCtTrend(){
  if(!ctTrendScen) return;
  const mode=scenOf(ctTrendScen), cmp=(mode===CMP);
  const tag=document.getElementById("ctTrendTag"); if(tag) tag.style.display=cmp?"":"none";
  function series(get){
    const refT=SC[0].center_tiers||[];
    if(cmp){ const out=[]; refT.forEach((t,ti)=>SC.forEach((s,si)=>out.push(
      {x:s.years,y:get((s.center_tiers||[])[ti]),name:`${t.name} · ${s.name}`,color:EPAL[ti%EPAL.length],dash:DASH[si%DASH.length]}))); return out; }
    const s=SC[mode]; return (s.center_tiers||[]).map((t,ti)=>({x:s.years,y:get(t),name:t.name,color:EPAL[ti%EPAL.length]}));
  }
  drawLines("ctPop",series(t=>t?t.per_year.map(p=>p.pop):[]),"인구(명)");
  drawLines("ctDens",series(t=>t?t.per_year.map(p=>p.rdens):[]),"밀도(인/km²)");
}
if(ctTrendScen) ctTrendScen.addEventListener("change",renderCtTrend);

// ================================================================
// 초기 렌더 + 탭
// ================================================================
function renderCarbon(){
  var C = D.carbon;
  var secs = ["carbonRegionSec","carbonZoneSec","carbonCenterSec"].map(function(id){return document.getElementById(id);});
  if(!C || !C.enabled){ secs.forEach(function(s){ if(s) s.style.display="none"; }); return; }
  secs.forEach(function(s){ if(s) s.style.display=""; });
  var yt=document.getElementById("carbonYearTag"); if(yt) yt.textContent=(C.year||"기준연도")+" 실측";
  var av = C.sectors_avail || {};
  var SECT_ALL = [["전체","총배출"],["건물","건물부문"],["수송","수송부문"]];
  var SECT = SECT_ALL.filter(function(s){return av[s[0]];});   // 가용 부문만 (표·카드용)
  var DEN = [["활동인구","총인구","#2563eb"],["주거인구","거주인구","#059669"],["근무인구","근무인구","#f59e0b"]];
  function lay(h){ return {margin:{l:52,r:12,t:10,b:44}, barmode:"group",
      legend:{orientation:"h",y:-0.18}, yaxis:{title:"tCO₂eq/인"}, height:(h||320)}; }
  function noData(id,msg){ var el=document.getElementById(id);
    if(el) el.innerHTML='<div class="ph" style="height:280px;display:flex;align-items:center;justify-content:center">'
      +(msg||"부문 데이터 없음 (원천 파일 미입력)")+'</div>'; }
  var cfg={responsive:true,displayModeBar:false};

  // ---- 계획권역: 카드 + 부문×분모 막대 ----
  var R = C.region;
  var cards="";
  SECT.forEach(function(s){
    var per=R.per[s[0]], em=R.emis[s[0]], v=per?per["활동인구"]:null;
    cards += '<div class="kpi"><div class="label">'+s[1]+' 1인당</div><div class="value">'
      +(v==null?"-":fmt(v,3))+'<span class="tag"> tCO₂eq/인</span></div>'
      +'<div class="sub2">총배출 '+(em==null?"-":fmt(em,0))+' tCO₂eq · 총인구 기준</div></div>';
  });
  document.getElementById("cbRegCards").innerHTML=cards;
  var sx=SECT.filter(function(s){return av[s[0]];});
  if(sx.length){
    Plotly.newPlot("cbRegChart", DEN.map(function(d){
      return {type:"bar",name:d[1],marker:{color:d[2]},
        x:sx.map(function(s){return s[1];}),
        y:sx.map(function(s){var p=R.per[s[0]];return p?p[d[0]]:null;})};
    }), lay(), cfg);
  } else noData("cbRegChart");

  // ---- 생활권: 부문별 차트 3 + 집중도 + 표 ----
  var Z=C.zones||[], znames=Z.map(function(z){return z.name;});
  [["전체","cbZoneTotal"],["건물","cbZoneBldg"],["수송","cbZoneTrans"]].forEach(function(p){
    var el=document.getElementById(p[1]), wrap=el?el.parentElement:null;
    if(!av[p[0]]){ if(wrap) wrap.style.display="none"; return; }
    if(wrap) wrap.style.display="";
    Plotly.newPlot(p[1], DEN.map(function(d){
      return {type:"bar",name:d[1],marker:{color:d[2]},x:znames,
        y:Z.map(function(z){var pp=z.per[p[0]];return pp?pp[d[0]]:null;})};
    }), lay(), cfg);
  });
  Plotly.newPlot("cbZoneConc",
    [{type:"bar",marker:{color:"#7c3aed"},x:znames,y:Z.map(function(z){return z.conc;})}],
    {margin:{l:52,r:12,t:10,b:40},yaxis:{title:"%"},height:260}, cfg);
  var zt='<thead><tr><th>생활권</th><th>총인구</th><th>집중도(%)</th>';
  SECT.forEach(function(s){ zt+='<th>'+s[1]+' 총배출<br>(tCO₂eq)</th><th>'+s[1]+' 1인당<br>(활동)</th>'; });
  zt+='</tr></thead><tbody>';
  Z.forEach(function(z){
    zt+='<tr><td>'+z.name+'</td><td>'+fmt(z.pop_a,0)+'</td><td>'+(z.conc==null?"-":fmt(z.conc,1))+'</td>';
    SECT.forEach(function(s){ var em=z.emis[s[0]],pr=z.per[s[0]],v=pr?pr["활동인구"]:null;
      zt+='<td>'+(em==null?"-":fmt(em,0))+'</td><td>'+(v==null?"-":fmt(v,3))+'</td>'; });
    zt+='</tr>';
  });
  document.getElementById("carbonZoneTable").innerHTML=zt+'</tbody>';

  // ---- 중심지: 상위 15 차트 3 + 전체 표 ----
  var CE=(C.centers||[]).slice();
  var top=CE.slice().sort(function(a,b){return (b.pop_a||0)-(a.pop_a||0);}).slice(0,15);
  var cnames=top.map(function(c){return c.name;});
  [["전체","cbCenTotal"],["건물","cbCenBldg"],["수송","cbCenTrans"]].forEach(function(p){
    var el=document.getElementById(p[1]), wrap=el?el.parentElement:null;
    if(!av[p[0]]){ if(wrap) wrap.style.display="none"; return; }
    if(wrap) wrap.style.display="";
    Plotly.newPlot(p[1], DEN.map(function(d){
      return {type:"bar",name:d[1],marker:{color:d[2]},x:cnames,
        y:top.map(function(c){var pp=c.per[p[0]];return pp?pp[d[0]]:null;})};
    }), lay(), cfg);
  });
  var ct='<thead><tr><th>중심지</th><th>위계</th><th>총인구</th>';
  SECT.forEach(function(s){ ct+='<th>'+s[1]+' 총배출<br>(tCO₂eq)</th><th>'+s[1]+' 1인당<br>(활동)</th>'; });
  ct+='</tr></thead><tbody>';
  CE.forEach(function(c){
    ct+='<tr><td>'+c.name+'</td><td>'+(c.tier||"-")+'</td><td>'+fmt(c.pop_a,0)+'</td>';
    SECT.forEach(function(s){ var em=c.emis[s[0]],pr=c.per[s[0]],v=pr?pr["활동인구"]:null;
      ct+='<td>'+(em==null?"-":fmt(em,0))+'</td><td>'+(v==null?"-":fmt(v,3))+'</td>'; });
    ct+='</tr>';
  });
  document.getElementById("carbonCenterTable").innerHTML=ct+'</tbody>';
}
function renderAll(){
  renderKPIs(); renderSummaryTrends(); renderConn();
  renderZoneShare(); buildZoneTable(); renderZoneTrend(); buildFacTable();
  renderCtBasic(); renderCtShare(); renderCtTrend();
  renderCarbon();
}
document.querySelectorAll(".tab").forEach(btn=>{
  btn.addEventListener("click",()=>{
    document.querySelectorAll(".tab").forEach(b=>b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach(p=>p.classList.remove("active"));
    btn.classList.add("active");
    const panel=document.getElementById("panel-"+btn.dataset.tab);
    panel.classList.add("active");
    if(zoneDT) zoneDT.columns.adjust();
    if(typeof ctDT!=="undefined" && ctDT) ctDT.columns.adjust();
    if(facDT) facDT.columns.adjust();
    panel.querySelectorAll(".js-plotly-plot").forEach(el=>Plotly.Plots.resize(el));
  });
});
// 하위탭(영역) 전환 — 같은 패널 안 .subpanel 토글, 차트 리사이즈
document.querySelectorAll(".subtabs").forEach(bar=>{
  bar.querySelectorAll(".subtab").forEach(btn=>{
    btn.addEventListener("click",()=>{
      bar.querySelectorAll(".subtab").forEach(b=>b.classList.remove("active"));
      btn.classList.add("active");
      const panel=bar.closest(".panel");
      panel.querySelectorAll(":scope > .subpanel").forEach(p=>p.classList.remove("active"));
      const sp=panel.querySelector(':scope > .subpanel[data-sub="'+btn.dataset.sub+'"]');
      if(sp){ sp.classList.add("active");
        sp.querySelectorAll(".js-plotly-plot").forEach(el=>Plotly.Plots.resize(el));
        if(window.jQuery&&$.fn.dataTable){ $.fn.dataTable.tables({visible:true,api:true}).columns.adjust(); }
      }
    });
  });
});
renderAll();

// ===== KPI 4지표 (생활권 min-max 레이더 + 원값 표) =====
(function(){
  // [삭제 예정] 복합집적도·충족도 진단(생활권 레이더/원값표) — HTML 섹션 제거됨, JS 무력화
  if(!document.getElementById("kpiRadar")) return;
  if(!D.kpi_enabled || !D.kpi_zones || !D.kpi_zones.length){
    var s=document.getElementById("kpiSection"); if(s) s.style.display="none"; return;
  }
  var Z=D.kpi_zones;
  var AX=[["c2_ne","복합집적도",false],["prox_ne","근접성",true],["suff_ne","충족도",false]];
  function norm(key,invert,mode){
    var vs=Z.map(function(z){return z[key];}).filter(function(v){return v!=null&&!isNaN(v)&&v>0;});
    if(!vs.length) return Z.map(function(){return 0;});
    var mx=Math.max.apply(null,vs), mn=Math.min.apply(null,vs);
    return Z.map(function(z){var v=z[key]; if(v==null||isNaN(v)||v<=0) return 0;
      if(mode==="minmax"){ var t=(mx>mn)?(v-mn)/(mx-mn):0.5; return invert?1-t:t; }
      return invert ? (mn/v) : (mx>0 ? v/mx : 0);
    });
  }
  var labels=AX.map(function(a){return a[1];});
  function drawRadar(){
    var el=document.getElementById("kpiNorm"); var mode=el?el.value:"max";
    var N=AX.map(function(a){return norm(a[0],a[2],mode);});
    var traces=Z.map(function(z,zi){
      return {type:"scatterpolar", fill:"toself", name:z.name,
        r:AX.map(function(a,ai){return N[ai][zi];}).concat([N[0][zi]]),
        theta:labels.concat([labels[0]])};
    });
    Plotly.react("kpiRadar", traces,
      {polar:{radialaxis:{range:[0,1],tickfont:{size:10}}},
       legend:{orientation:"h"}, margin:{t:20,r:20,b:20,l:20}, paper_bgcolor:"transparent"},
      {displayModeBar:false,responsive:true});
  }
  drawRadar();
  var sel=document.getElementById("kpiNorm"); if(sel) sel.addEventListener("change",drawRadar);
  // 시설유형별 근접성 막대 (구분 근린/거점 · 참조선 근린1km·거점5km)
  var TC=(D.kpi_region&&D.kpi_region.type_cat)||{};
  function drawProxBar(){
    var pcEl=document.getElementById("proxCat"); var cat=pcEl?pcEl.value:"ne";
    var types=Object.keys(TC).filter(function(t){return TC[t]===cat;});
    var traces=Z.map(function(z){var pbt=z.prox_by_type||{};
      return {type:"bar", name:z.name, x:types, y:types.map(function(t){return pbt[t]!=null?pbt[t]:null;})};});
    var ref=(cat==="ne")?1:5;
    // 참조선: 좌우 끝까지(paper) 그리는 shape + 범례용 더미 트레이스(글씨 없음)
    traces.push({type:"scatter", mode:"lines", name:"참조 거리 ("+ref+"km)",
      x:[null], y:[null], line:{color:"#e0561e", width:1.5, dash:"dash"}, hoverinfo:"skip"});
    Plotly.react("kpiProxBar", traces,
      {barmode:"group", margin:{t:34,r:10,b:100,l:44},
       legend:{orientation:"h", x:1, xanchor:"right", y:1.14, yanchor:"bottom"},
       yaxis:{title:"km", rangemode:"tozero"}, xaxis:{tickangle:-30, automargin:true}, paper_bgcolor:"transparent",
       shapes:[{type:"line", xref:"paper", x0:0, x1:1, yref:"y", y0:ref, y1:ref,
         line:{color:"#e0561e", width:1.5, dash:"dash"}}]},
      {displayModeBar:false, responsive:true});
  }
  drawProxBar();
  var pcEl=document.getElementById("proxCat"); if(pcEl) pcEl.addEventListener("change",drawProxBar);
  var cols=[{title:"생활권"},{title:"저차시설 집적도<br>(지수)"},{title:"고차시설 집적도<br>(지수)"},
    {title:"저차시설 근접성<br>(km)"},{title:"고차시설 근접성<br>(km)"},{title:"저차시설 충족도<br>(%)"},{title:"고차시설 충족도<br>(%)"}];
  function g(v){return (v==null)?"-":v;}
  var rows=Z.map(function(z){return [z.name,g(z.c2_ne),g(z.c2_hu),g(z.prox_ne),g(z.prox_hu),g(z.suff_ne),g(z.suff_hu)];});
  if(window.jQuery && $.fn.dataTable){
    $("#kpiTable").DataTable({data:rows,columns:cols,paging:false,searching:false,info:false});
  }
})();

// ===== 네트워크성 — 중심지 위계별 연결성 표 =====
(function(){
  if(!D.kpi_enabled || !D.kpi_centers || !D.kpi_centers.length){
    var s=document.getElementById("kpiCenterConn"); if(s) s.style.display="none"; return;
  }
  var C=D.kpi_centers.slice().sort(connTierSort);   // 위계 순 정렬
  var bands=(C[0] && C[0].conn_bands) ? C[0].conn_bands : [30,60,90,120];
  function g(v){return (v==null)?"-":v;}
  // 상위·동급만 · 수/비율 (2026-09): [중심지·위계] [상위계·동급계] [임계시간별 상위 수/% · 동급 수/%]
  var h1='<tr><th rowspan="3">중심지</th><th rowspan="3">위계</th>'+
         '<th rowspan="3">상위<br>중심지 수</th><th rowspan="3">동급<br>중심지 수</th>'+
         '<th colspan="'+(bands.length*4)+'">임계시간 내 도달 (수 · 비율%)</th></tr>';
  var h2='<tr>'+bands.map(function(b){return '<th colspan="4">'+b+'분</th>';}).join('')+'</tr>';
  var h3='<tr>'+bands.map(function(){return '<th>상위 수</th><th>상위 %</th><th>동급 수</th><th>동급 %</th>';}).join('')+'</tr>';
  $("#kpiCenterTable").html('<thead>'+h1+h2+h3+'</thead>');
  var ncol=2+2+bands.length*4;
  var rows=C.map(function(c){
    var r=[c.name,(c.tier_name!=null?c.tier_name:g(c.tier)),g(c.tot_up),g(c.tot_eq)];
    bands.forEach(function(b,bi){
      r.push(g((c.cnt_up||[])[bi]),g((c.conn_up||[])[bi]),g((c.cnt_eq||[])[bi]),g((c.conn_eq||[])[bi]));
    });
    return r;
  });
  if(window.jQuery && $.fn.dataTable){
    $("#kpiCenterTable").DataTable({data:rows,columns:Array.apply(null,{length:ncol}).map(function(){return {};}),
      paging:true,pageLength:10,searching:true,info:true,autoWidth:false,ordering:true,order:[],
      language:DTL, lengthMenu:[[5,10,25,-1],[5,10,25,"전체"]]});
  }
})();

// ===== 중심지별 연결성 레이더/막대 (시각화 전환) =====
(function(){
  if(!D.kpi_enabled || !D.kpi_centers || !D.kpi_centers.length){
    var s=document.getElementById("connRadarWrap"); if(s) s.style.display="none";
    var b=document.getElementById("connCenterSel"); if(b&&b.parentNode) b.parentNode.style.display="none"; return;
  }
  var C=D.kpi_centers.slice().sort(connTierSort);   // 위계 순
  var bands=(C[0]&&C[0].conn_bands)?C[0].conn_bands:[30,60,90,120];
  var bandLbl=bands.map(function(b){return b+"분";});
  var sel=document.getElementById("connCenterSel");
  if(sel){
    sel.appendChild(new Option("전체 평균("+C.length+"개)","__avg__"));
    C.forEach(function(c,i){
      var tn=(c.tier_name!=null?c.tier_name:c.tier);
      sel.appendChild(new Option(c.name+(tn!=null?" ["+tn+"]":""), String(i)));
    });
    sel.value="__avg__";
  }
  function avgOf(fn){   // 전체 평균(널 제외)
    var vs=C.map(fn).filter(function(v){return v!=null&&!isNaN(v);});
    if(!vs.length) return null;
    return Math.round(vs.reduce(function(a,b){return a+b;},0)/vs.length*10)/10;
  }
  // 밴드별 값(상위/동급 · 수 또는 비율) — 선택 중심지 or 전체평균
  function bandVals(v,key){
    if(v==="__avg__") return bands.map(function(_,bi){return avgOf(function(c){return (c[key]||[])[bi];});});
    var c=C[+v]; return bands.map(function(_,bi){return (c[key]||[])[bi];});
  }
  function close(a){ return a.concat([a[0]]); }
  // [라벨, 비율키, 수키, 색]
  var DEFS=[["상위","conn_up","cnt_up","#2563eb"],["동급","conn_eq","cnt_eq","#059669"]];
  // --- 도달 수: 꺾은선 ---
  function cntLine(v){
    var traces=DEFS.map(function(d){
      return {type:"scatter",mode:"lines+markers",name:d[0],x:bandLbl,y:bandVals(v,d[2]),
        line:{color:d[3],width:2},marker:{color:d[3],size:7},connectgaps:false};
    });
    Plotly.react("chartConnRadarDist",traces,
      {margin:{t:34,r:14,b:40,l:50},legend:{orientation:"h",x:1,xanchor:"right",y:1.16,yanchor:"bottom"},
       yaxis:{title:"도달 중심지 수",rangemode:"tozero"},xaxis:{type:"category",title:"임계시간"},paper_bgcolor:"transparent"},
      {displayModeBar:false,responsive:true});
  }
  // --- 도달 수: 사각 레이더 ---
  function cntRadar(v){
    var th=close(bandLbl);
    var traces=DEFS.map(function(d){
      return {type:"scatterpolar",name:d[0],r:close(bandVals(v,d[2])),theta:th,
        line:{color:d[3]},fill:(d[0]==="상위"?"toself":"none"),connectgaps:false};
    });
    Plotly.react("chartConnRadarDist",traces,
      {polar:{radialaxis:{rangemode:"tozero",tickfont:{size:10}}},
       legend:{orientation:"h"},margin:{t:34,r:30,b:30,l:30},paper_bgcolor:"transparent"},
      {displayModeBar:false,responsive:true});
  }
  // --- 도달률: 꺾은선 ---
  function reachLine(v){
    var traces=DEFS.map(function(d){
      return {type:"scatter",mode:"lines+markers",name:d[0],x:bandLbl,y:bandVals(v,d[1]),
        line:{color:d[3],width:2},marker:{color:d[3],size:7},connectgaps:false};
    });
    Plotly.react("chartConnRadarReach",traces,
      {margin:{t:34,r:14,b:40,l:50},
       legend:{orientation:"h",x:1,xanchor:"right",y:1.16,yanchor:"bottom"},
       yaxis:{title:"도달률(%)",range:[0,100]},xaxis:{type:"category",title:"임계시간"},paper_bgcolor:"transparent"},
      {displayModeBar:false,responsive:true});
  }
  // --- 도달률: 사각 레이더 ---
  function reachRadar(v){
    var th=close(bandLbl);
    var traces=DEFS.map(function(d){
      return {type:"scatterpolar",name:d[0],r:close(bandVals(v,d[1])),theta:th,
        line:{color:d[3]},fill:(d[0]==="상위"?"toself":"none"),connectgaps:false};
    });
    Plotly.react("chartConnRadarReach",traces,
      {polar:{radialaxis:{range:[0,100],ticksuffix:"%",tickfont:{size:10}}},
       legend:{orientation:"h",x:1.05,xanchor:"right",y:1.12,yanchor:"bottom"},
       margin:{t:34,r:30,b:30,l:30},paper_bgcolor:"transparent"},
      {displayModeBar:false,responsive:true});
  }
  var modeEl=document.getElementById("connVizMode");
  function drawAll(){
    var v=sel?sel.value:"__avg__", mode=modeEl?modeEl.value:"basic";
    if(mode==="radar"){ cntRadar(v); reachRadar(v); }
    else { cntLine(v); reachLine(v); }
  }
  drawAll();
  if(sel) sel.addEventListener("change",drawAll);
  if(modeEl) modeEl.addEventListener("change",drawAll);
})();

// ===== 생활편리성 표(Sheet2): 계획권역·생활권·중심지 =====
(function(){
  var L=D.life_table;
  if(!L) return;
  function g(v){return (v==null)?"-":v;}
  function buildTable(sel, rowsObj, centersOnly){
    var el=document.getElementById(sel); if(!el||!rowsObj) return;
    var cols=centersOnly
      ? [{title:"시설"},{title:"구분"},{title:"천인당 시설수<br>(개/천인)"},{title:"평균 접근성<br>(km)"}]
      : [{title:"시설"},{title:"구분"},{title:"천인당 시설수<br>(개/천인)"},{title:"중심지 내<br>시설 비율(%)"},
         {title:"서비스권<br>향유도(%)"},{title:"관리대상<br>시설 비율(%)"},{title:"평균 접근성<br>(km)"}];
    var order=["근린","거점"];
    var tns=Object.keys(rowsObj).sort(function(a,b){
      var ca=order.indexOf(rowsObj[a].cat), cb=order.indexOf(rowsObj[b].cat);
      return ca!==cb?ca-cb:a.localeCompare(b,"ko");});
    var rows=tns.map(function(tn){
      var r=rowsObj[tn], catl=(r.cat==="근린"?"근린":"거점");
      return centersOnly
        ? [tn,catl,g(r.per1000),g(r.access)]
        : [tn,catl,g(r.per1000),g(r.ctr_ratio),g(r.cover),g(r.mgmt_ratio),g(r.access)];
    });
    if(window.jQuery&&$.fn.dataTable){
      if($.fn.dataTable.isDataTable("#"+sel)) $("#"+sel).DataTable().destroy();
      $("#"+sel).empty();
      $("#"+sel).DataTable({data:rows,columns:cols,paging:false,searching:false,info:false,
        ordering:false,autoWidth:false});
    }
  }
  buildTable("lifeRegionTable", L.region, false);
  var zsel=document.getElementById("lifeZoneSel");
  if(zsel && L.zones && L.zones.length){
    L.zones.forEach(function(z,i){ zsel.appendChild(new Option(z.name,String(i))); });
    function drawZone(){ var z=L.zones[(+zsel.value)||0]; buildTable("lifeZoneTable", z.rows, false); }
    zsel.addEventListener("change",drawZone); drawZone();
  }
  var csel=document.getElementById("lifeCenterSel");
  if(csel && L.centers && L.centers.length){
    L.centers.forEach(function(c,i){ csel.appendChild(new Option(c.name,String(i))); });
    function drawC(){ var c=L.centers[(+csel.value)||0]; buildTable("lifeCenterTable", c.rows, true); }
    csel.addEventListener("change",drawC); drawC();
  } else if(L.centers && L.centers.length){
    buildTable("lifeCenterTable", L.centers[0].rows, true);
  }
})();

// ===== KPI 지표 설명 (영역별 상세표) =====
(function(){
  var DD=D.indicator_desc, btn=document.getElementById("descBtn"), wrap=document.getElementById("descWrap");
  if(!DD||!DD.length){ if(btn) btn.style.display="none"; return; }
  function esc(s){ return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;"); }
  function build(){
    var body=document.getElementById("descBody"); if(!body||body.dataset.done) return;
    var html="";
    DD.forEach(function(g){
      html+='<h3>'+esc(g.label)+'</h3><table class="desc"><thead><tr>'
        +'<th>지표</th><th>정의</th><th>산출 방법</th><th>해석 관점</th><th>단위</th></tr></thead><tbody>';
      (g.items||[]).forEach(function(it){
        html+='<tr><td>'+esc(it.n)+'</td><td>'+esc(it.d)+'</td><td>'+esc(it.m)
          +'</td><td>'+esc(it.i)+'</td><td>'+esc(it.u)+'</td></tr>';
      });
      html+='</tbody></table>';
    });
    body.innerHTML=html; body.dataset.done="1";
  }
  if(btn) btn.addEventListener("click",function(){ build(); wrap.style.display="flex"; });
  var dc=document.getElementById("descClose");
  if(dc) dc.addEventListener("click",function(){ wrap.style.display="none"; });
  if(wrap) wrap.addEventListener("click",function(e){ if(e.target===wrap) wrap.style.display="none"; });
})();

// ===== 지역 과거 현황 (전국 과거격자 주거 · 2000~기준연도) =====
(function(){
  var P=D.past, btn=document.getElementById("pastBtn"), wrap=document.getElementById("pastWrap");
  if(!P||!P.years||!P.years.length){ if(btn) btn.style.display="none"; return; }
  if(btn) btn.style.display="";
  var Y=P.years, drawn=false;
  var sub=document.getElementById("pastSub");
  if(sub) sub.textContent="전국 과거 격자 주거 총인구 매칭·집계 · "+Y.join(" · ");
  function lay(extra){return Object.assign({paper_bgcolor:"rgba(0,0,0,0)",plot_bgcolor:"rgba(0,0,0,0)",
    font:{color:"#334155",size:11},margin:{t:16,r:16,b:36,l:64},legend:{orientation:"h",y:-0.22},
    xaxis:{type:"category",gridcolor:"#e5e7eb"},yaxis:{gridcolor:"#e5e7eb"}},extra||{});}
  var CFG={displayModeBar:false,responsive:true};
  function draw(){
    if(drawn) return; drawn=true;
    Plotly.newPlot("pastPop",[
      {type:"scatter",mode:"lines+markers",name:"계획권역",x:Y,y:P.region_pop,line:{color:"#2563eb"}},
      {type:"scatter",mode:"lines+markers",name:"중심지",x:Y,y:P.center_pop,line:{color:"#dc2626"}}],
      lay({yaxis:{title:"인구(명)",rangemode:"tozero"}}),CFG);
    var concTr=[{type:"scatter",mode:"lines+markers",name:"전체 중심지",x:Y,y:P.conc,line:{color:"#059669",width:2.5}}];
    (P.conc_tiers||[]).forEach(function(t,ti){
      concTr.push({type:"scatter",mode:"lines+markers",name:t.name,x:Y,y:t.conc,
        line:{color:["#2563eb","#f59e0b","#dc2626","#7c3aed"][ti%4],dash:"dot"}});
    });
    Plotly.newPlot("pastConc",concTr,lay({yaxis:{title:"집중도(%)",rangemode:"tozero"}}),CFG);
    Plotly.newPlot("pastZone",(P.zones||[]).map(function(z){
      return {type:"scatter",mode:"lines+markers",name:z.name,x:Y,y:z.pop};}),
      lay({yaxis:{title:"인구(명)",rangemode:"tozero"}}),CFG);
    Plotly.newPlot("pastDens",[
      {type:"scatter",mode:"lines+markers",name:"계획권역",x:Y,y:P.region_dens,line:{color:"#2563eb"}},
      {type:"scatter",mode:"lines+markers",name:"중심지",x:Y,y:P.center_dens,line:{color:"#dc2626"}}],
      lay({yaxis:{title:"밀도(인/km²)",rangemode:"tozero"}}),CFG);
  }
  if(btn) btn.addEventListener("click",function(){ wrap.style.display="flex"; draw();
    setTimeout(function(){["pastPop","pastConc","pastZone","pastDens"].forEach(function(id){
      var e=document.getElementById(id); if(e&&e.data) Plotly.Plots.resize(e);});},60); });
  var pc=document.getElementById("pastClose");
  if(pc) pc.addEventListener("click",function(){ wrap.style.display="none"; });
  if(wrap) wrap.addEventListener("click",function(e){ if(e.target===wrap) wrap.style.display="none"; });
})();

// ===== 기초현황 탭 (선택 입력값 표시, 없으면 프레임 유지) =====
(function(){
  var B=D.basestat;
  if(!document.getElementById("baseStatFrameTag")) return;   // [삭제됨] 기초현황 탭 제거 — JS 무력화
  if(!B || !B.enabled || !B.items || !B.items.length) return;   // 미연결 → HTML 프레임 그대로
  var items=B.items;
  document.getElementById("baseStatFrameTag").style.display="none";
  document.getElementById("baseStatZoneTag").style.display="none";
  var desc=document.getElementById("baseStatDesc");
  if(desc) desc.textContent="입력된 기초현황 "+items.length+"개를 격자→생활권·계획권역으로 집계한 결과입니다.";
  // 계획권역 카드 (지표당 1개)
  var cards=document.getElementById("baseStatCards");
  if(cards) cards.innerHTML=items.map(function(it){
    var rv=(it.region==null)?'<span class="muted">-</span>':(fmt(it.region,2)+'<small> '+(it.unit||"")+'</small>');
    return '<div class="kpi"><div class="label">'+it.label+' (계획권역)</div><div class="value">'+rv+'</div></div>';
  }).join("");
  // 생활권별 막대 (grid3, 지표당 1개)
  var wrap=document.getElementById("baseStatCharts");
  if(wrap){
    wrap.innerHTML=items.map(function(it,i){
      return '<div><div class="ch3">'+it.label+'</div><div id="baseStatChart'+i+'" class="chart"></div></div>';
    }).join("");
    var PALB=["#2563eb","#059669","#f59e0b"];
    items.forEach(function(it,i){
      var zs=it.zones||[];
      if(window.Plotly) Plotly.react("baseStatChart"+i,[{type:"bar",
        x:zs.map(function(z){return z.name;}), y:zs.map(function(z){return z.value;}),
        marker:{color:PALB[i%PALB.length]},
        text:zs.map(function(z){return z.value==null?"":fmt(z.value,1);}),textposition:"outside",cliponaxis:false}],
        {margin:{t:16,r:10,b:40,l:48},yaxis:{title:it.unit||"",rangemode:"tozero"},
         xaxis:{type:"category"},paper_bgcolor:"transparent"},
        {displayModeBar:false,responsive:true});
    });
  }
  // 통합 표: 생활권 × 지표
  var zoneNames=(items[0].zones||[]).map(function(z){return z.name;});
  var head='<thead><tr><th>생활권</th>'+items.map(function(it){
    return '<th>'+it.label+(it.unit?('<br>('+it.unit+')'):'')+'</th>';}).join("")+'</tr></thead>';
  var body='<tbody>'+zoneNames.map(function(nm,zi){
    return '<tr><td>'+nm+'</td>'+items.map(function(it){
      var v=(it.zones[zi]||{}).value; return '<td>'+(v==null?'-':fmt(v,2))+'</td>';}).join("")+'</tr>';
  }).join("")+'</tbody>';
  var tbl=document.getElementById("baseStatTable");
  if(tbl) tbl.innerHTML=head+body;
})();

// ================= 진단 요약 · 핵심지표 카드 · 지도 (가이드라인 표6·표9·표10 형식, 수치만 표시) =================
(function(){
  const G=D.geo, M=D.meta||{}, P=D.past;
  const TIERC={1:"#d73027",2:"#ff7f00",3:"#1a9850"};          // 플러그인 위계 고정색
  const ZPAL=(typeof EPAL!=="undefined")?EPAL:["#2563eb","#059669","#f59e0b","#dc2626","#7c3aed","#0891b2"];
  // 가이드라인 표7(주거)·표8(근무) — [평균, 중위값] (2023년, 2025 국토모니터링 내부자료)
  const REF={r:{"거점도시":[95.4,98.2],"강소도시":[87.8,90.7],"자립도시":[58.0,57.2],
                "성장형":[84.7,91.8],"성숙·안정형":[74.0,77.1],"감소형":[65.3,59.9]},
             w:{"거점도시":[84.9,84.5],"강소도시":[73.2,72.6],"자립도시":[60.8,60.5],
                "성장형":[69.7,73.2],"성숙·안정형":[69.1,68.6],"감소형":[67.5,65.3]}};
  const $id=id=>document.getElementById(id);
  const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  const nz=v=>v===null||v===undefined||Number.isNaN(v);
  const N=(v,d)=>nz(v)?"–":Number(v).toLocaleString("ko-KR",{minimumFractionDigits:d||0,maximumFractionDigits:d||0});
  const PCT=v=>nz(v)?"–":N(v,1)+"%";
  const arrow=(a,b)=>(nz(a)||nz(b))?"":(b>a?" ▲":(b<a?" ▼":""));
  const yNum=lab=>{const n=parseInt(lab,10); return Number.isNaN(n)?null:n;};
  // 변화: 부호로 증가·감소, 연평균 변화량(비율=%p, 수량=%)
  function chg(a,b,ya,yb,kind){
    if(nz(a)||nz(b)) return "–";
    const w=b>a?"증가":(b<a?"감소":"변화 없음");
    if(nz(ya)||nz(yb)||yb<=ya) return w;
    let r,s;
    if(kind==="pp"){ r=(b-a)/(yb-ya); s=(r>=0?"+":"−")+Math.abs(r).toFixed(2)+"%p"; }
    else { if(a<=0) return w; r=(Math.pow(b/a,1/(yb-ya))-1)*100; s=(r>=0?"+":"−")+Math.abs(r).toFixed(2)+"%"; }
    return w+" (연 "+s+")";
  }
  const baseLabel=M.base_year?String(M.base_year):(SC[0].years[0]||"기준연도");
  const baseNum=M.base_year||yNum(SC[0].years[0]);

  // ---- 선택 상자 ----
  const selS=$id("ovScen"), selP=$id("ovPast"), selT=$id("ovTarget"), selN=$id("ovFacN"), selH=$id("ovFacH");
  SC.forEach((s,i)=>selS.add(new Option(s.name,String(i))));
  if(P&&P.years&&P.years.length>1){
    P.years.slice(0,-1).forEach((y,i)=>selP.add(new Option(y,String(i))));
    const i15=P.years.indexOf("2015"); selP.value=String(i15>=0?i15:P.years.length-2);
  } else { selP.add(new Option("자료 없음","-1")); selP.disabled=true; }
  function fillTarget(){
    const s=SC[+selS.value]||SC[0], cur=selT.value; selT.innerHTML="";
    if(s.years.length<2){ selT.add(new Option("–","0")); selT.disabled=true; return; }
    selT.disabled=false; s.years.forEach((y,i)=>{ if(i>0) selT.add(new Option(y,String(i))); });
    selT.value=(cur&&+cur>0&&+cur<s.years.length)?cur:String(s.years.length-1);
  }
  fillTarget();
  $id("ovBase").textContent=baseLabel;
  const facAll=Object.keys((SC[0]&&SC[0].fac_ts_g)||{}).filter(k=>k.indexOf("t:")===0);
  const FK={ne:facAll.filter(k=>/^t:근린/.test(k)), hu:facAll.filter(k=>/^t:거점/.test(k))};
  const fnm=k=>String(k).replace(/^t:(근린|거점)_?/,"");
  function fillFac(sel,list,prefer){
    sel.innerHTML=""; list.forEach(k=>sel.add(new Option(fnm(k),k)));
    const p=list.find(k=>k.indexOf(prefer)>=0); if(p) sel.value=p;
    if(!list.length){ sel.add(new Option("없음","")); sel.disabled=true; }
  }
  fillFac(selN,FK.ne,"약국"); fillFac(selH,FK.hu,"도서관");
  if(!facAll.length){ const w=$id("ovFacWrap"); if(w) w.style.display="none"; }

  // ---- 구역별 선택 막대: 같은 선택을 필요한 자리마다 두고 서로 연동 ----
  const MSEL={s:[selS,"시나리오"],p:[selP,"과거 비교연도"],t:[selT,"목표연도"],n:[selN,"근린 시설"],h:[selH,"거점 시설"]};
  const ovBars=Array.prototype.slice.call(document.querySelectorAll(".ovbar"));
  ovBars.forEach(b=>{ (b.dataset.ov||"").split("").forEach(k=>{
    if(k==="b"){ const sp=document.createElement("span"); sp.innerHTML='기준연도 <b>'+esc(baseLabel)+'</b>'; b.appendChild(sp); return; }
    const m=MSEL[k]; if(!m||((k==="n"||k==="h")&&!facAll.length)) return;
    const lab=document.createElement("label"); lab.textContent=m[1]+" ";
    const c=document.createElement("select"); c.dataset.k=k; lab.appendChild(c); b.appendChild(lab);
    c.addEventListener("change",()=>{ m[0].value=c.value; m[0].dispatchEvent(new Event("change")); }); }); });
  function syncBars(){ ovBars.forEach(b=>b.querySelectorAll("select").forEach(c=>{ const m=MSEL[c.dataset.k][0];
    c.innerHTML=m.innerHTML; c.value=m.value; c.disabled=m.disabled; })); }

  function st(){
    const si=+selS.value||0, s=SC[si];
    const ti=s.years.length>1?Math.min(Math.max(1,+selT.value||1),s.years.length-1):0;
    const pi=(P&&!selP.disabled)?+selP.value:-1;
    return {s,si,ti,pi,yb:baseNum,yt:yNum(s.years[ti]),yp:(pi>=0?yNum(P.years[pi]):null),
            lb:baseLabel,lt:s.years[ti],lp:(pi>=0?P.years[pi]:null),fn:selN.value,fh:selH.value};
  }
  // ---- 데이터 헬퍼 ----
  const tiersOf=s=>(s.center_tiers||[]).slice().sort((a,b)=>a.tier-b.tier);
  function cumList(s,yi,get){ let acc=0; const lab=[];
    return tiersOf(s).map(t=>{ const v=get(t,yi); acc=(acc===null||nz(v))?null:acc+v; lab.push(t.tier+"차");
      return {tier:t.tier,lab:lab.join("+"),v:acc}; }); }
  // 위계별 인구 합 ÷ 권역 인구 (위계별 비율을 반올림한 뒤 더하면 오차가 쌓임)
  const rShareFn=s=>(t,yi)=>{ const R=(s.region_ts[yi]||{}).pop, p=(t.per_year[yi]||{}).pop;
    return (nz(R)||nz(p)||R<=0)?null:p/R*100; };
  const wShareFn=s=>(t,yi)=>{ const W=(s.region_ts[yi]||{}).work, w=(t.per_year[yi]||{}).wpop;
    return (nz(W)||nz(w)||W<=0)?null:w/W*100; };
  function pastCum(s,pi){
    if(!P||pi<0||!P.conc_tiers||!P.conc_tiers.length) return null;
    const byT={}; P.conc_tiers.forEach(t=>{ if(t.tier!=null) byT[t.tier]=t.conc[pi]; });
    let acc=0; return tiersOf(s).map(t=>{ const v=byT[t.tier]; acc=(acc===null||nz(v))?null:acc+v; return acc; });
  }
  function eqAt(conn,yi){
    if(!conn||!conn[yi]) return null; const c=conn[yi];
    const th=(D.conn_meta&&D.conn_meta.thresholds)||[]; let k=th.indexOf(30); if(k<0) k=0;
    const row=(c.equity||[])[k]; return row?{levels:c.levels||[],vals:row,thr:(th[k]||30)}:null;
  }
  const lvLab=l=>String(l).split("+").join(" 또는 ")+" 중심지까지";
  const EQK=0;   // ③ 접근 형평성 대표값 = 위계 누적 첫 단계(1차 중심지까지) — 성과 평가 지원과 같은 기준
  const facAt=(g,key,yi)=>{ const a=g&&g[key]; return a&&a[yi]?a[yi]:null; };
  function missCnt(s,yi,tier,zname){ const ref=M.dens_ref||2000;
    return (s.centers||[]).filter(c=>c.tier===tier&&(zname==null||c.zone===zname))
      .filter(c=>{ const d=(c.per_year[yi]||{}).rdens; return !nz(d)&&d<ref; }).length; }
  function refRows(kind){ const out=[];
    [M.city_type,M.trend_type].forEach(k=>{ if(k&&REF[kind][k]) out.push(N(REF[kind][k][1],1)+"% ("+k+" 중위값)"); });
    return out; }
  const refMain=kind=>{ const k=M.city_type||M.trend_type; return (k&&REF[kind][k])?REF[kind][k][1]:null; };

  // ---- 지도 (EPSG:5179 경로를 그대로 사용) ----
  function mapSVG(kind){
    const vb=G.vb, w=vb[2], fs=Math.round(w*0.026), sw=Math.max(1,Math.round(w*0.0011));
    let h='<svg viewBox="'+vb.join(" ")+'" class="ovkey" preserveAspectRatio="xMidYMid meet">';
    h+='<path d="'+G.base+'" fill="#e9edf1"/>';
    if(kind!=="ctr") G.zones.forEach((z,i)=>{
      h+='<g data-lyr="z'+z.zid+'"><path'+(kind==="zone"?' class="ovzp"':'')+' data-zone="'+z.zid+'" data-name="'+esc(z.name)+
         '" d="'+z.d+'" fill="'+ZPAL[i%ZPAL.length]+'" fill-opacity=".25"/></g>'; });
    let tk=Object.keys(G.tiers).map(Number).sort((a,b)=>b-a);
    const pos=tk.filter(t=>t>0);
    if(kind==="zone"&&pos.length) tk=[Math.min.apply(null,pos)];
    tk.forEach(t=>{ h+='<g data-lyr="t'+t+'" fill="'+(TIERC[t]||"#8a94a3")+'" pointer-events="none"><path d="'+G.tiers[t]+'"/></g>'; });
    if(kind!=="ctr"){
      h+='<g data-lyr="lbl" pointer-events="none">';
      G.labels.forEach(l=>{ const z=G.zones.find(q=>q.zid===l.zid);
        h+='<text x="'+l.x+'" y="'+Math.round(l.y-fs*1.1)+'" text-anchor="middle" style="font-size:'+fs+'px;font-weight:700;fill:#1f2933;'+
           'paint-order:stroke;stroke:#fff;stroke-width:'+Math.round(fs*0.26)+'px">'+esc(z?z.name:"")+'</text>'; });
      h+='</g>';
    }
    if(G.sgg&&G.sgg.length){
      h+='<g data-lyr="sgg" fill="transparent" stroke="#52606d" stroke-width="'+sw+'" style="display:none">';
      G.sgg.forEach(g=>{ h+='<path data-name="'+esc(g.name)+'" d="'+g.d+'"/>'; }); h+='</g>';
    }
    return h+'</svg>';
  }
  function insetSVG(){
    if(!G.inset) return "";
    const I=G.inset;
    return '<svg viewBox="'+I.vb.join(" ")+'" preserveAspectRatio="xMidYMid meet">'+
      I.sido.map(d=>'<path d="'+d+'" fill="#e4e7eb" stroke="#9aa5b1" stroke-width="1200"/>').join("")+
      '<path d="'+I.region+'" fill="#2f5d8a" stroke="#1d3f5f" stroke-width="1600"/></svg>';
  }
  function mapBox(kind,legend,hint){
    return '<div class="ovmapbox">'+mapSVG(kind)+'<div class="ovbtns">'+
      (G.inset?'<button type="button" class="ovsite">위치도</button>':'')+
      (G.sgg&&G.sgg.length?'<button type="button" class="ovsgg">시군구 경계</button>':'')+'</div>'+
      '<div class="ovinset" hidden>'+insetSVG()+'</div>'+legend+(hint?'<div class="ovhint">'+hint+'</div>':'')+'</div>';
  }
  if(G&&G.zones){
    const tcount={}; tiersOf(SC[0]).forEach(t=>{ tcount[t.tier]=t.count; });
    const tkeys=Object.keys(G.tiers).map(Number).sort((a,b)=>a-b);
    const tname=t=>t>0?t+"차 중심지":"중심지";
    const zleg='<div class="ovleg"><span class="lh">생활권</span>'+G.zones.map((z,i)=>
      '<button type="button" class="ovli" data-lyr="z'+z.zid+'"><i style="background:'+ZPAL[i%ZPAL.length]+';opacity:.35"></i>'+esc(z.name)+'</button>').join("")+'</div>';
    const tleg=tkeys.length?'<div class="ovleg"><span class="lh">중심지</span>'+tkeys.map(t=>
      '<button type="button" class="ovli" data-lyr="t'+t+'"><i style="background:'+(TIERC[t]||"#8a94a3")+'"></i>'+tname(t)+
      (tcount[t]!=null?' ('+tcount[t]+')':'')+'</button>').join("")+'</div>':'';
    const top=tkeys.filter(t=>t>0)[0];
    const zlegS='<div class="ovleg">'+G.zones.map((z,i)=>'<span><i style="background:'+ZPAL[i%ZPAL.length]+';opacity:.35"></i>'+esc(z.name)+'</span>').join("")+
      (top?'<span><i style="background:'+(TIERC[top]||"#8a94a3")+'"></i>'+tname(top)+'</span>':'')+'</div>';
    $id("ovMapSum").innerHTML=mapBox("sum",zleg+tleg,"");
    $id("ovCtrMap").innerHTML=mapBox("ctr",tleg,"");
    $id("ovZoneMap").innerHTML=mapBox("zone",zlegS,"생활권을 누르면 아래 해당 생활권으로 이동합니다"+
      (G.sgg&&G.sgg.length?" (시군구 경계를 켠 상태에서도 가능)":""));
  } else {
    ["ovMapSum","ovCtrMap","ovZoneMap"].forEach(id=>{ const e=$id(id); if(e) e.innerHTML='<div class="ovnote">지도 도형이 없어 지도를 생략합니다.</div>'; });
  }

  // ---- 진단 요약 ----
  function tile(cls,cat,nm,val,rows){
    return '<div class="ovtile '+cls+'"><div class="ct">'+cat+'</div><div class="nm">'+nm+'</div>'+
      (val!==undefined?'<div class="v">'+val+'</div>':'')+
      rows.map(r=>'<div class="row"><span>'+r[0]+'</span><span>'+r[1]+'</span></div>').join("")+'</div>';
  }
  function renderBasic(x){
    const s=x.s, r0=s.region_ts[0], rt=s.region_ts[x.ti], T=tiersOf(s), zn=(s.zones||[]).map(z=>z.name);
    $id("ovBasic").innerHTML=
      '<dt>총인구</dt><dd>'+N(r0.pop)+'명 ('+esc(x.lb)+')'+(x.ti>0?'<br><span style="font-weight:400">→ '+N(rt.pop)+'명 ('+esc(x.lt)+', '+esc(s.name)+')</span>':'')+'</dd>'+
      '<dt>근무인구</dt><dd>'+(nz(r0.work)?"–":N(r0.work)+'명 ('+esc(x.lb)+')')+'</dd>'+
      '<dt>면적 · 밀도</dt><dd>'+N(D.region_area,0)+'㎢ · '+N(r0.rdens,0)+'명/㎢</dd>'+
      '<dt>도시위상</dt><dd>'+esc(M.city_type||"미지정")+'</dd>'+
      '<dt>인구추세 유형</dt><dd>'+esc(M.trend_type||"미지정")+'</dd>'+
      '<dt>중심지</dt><dd>'+N(D.center_count)+'곳'+(T.length?' ('+T.map(t=>t.tier+'차 '+t.count).join(' · ')+')':'')+'</dd>'+
      '<dt>생활권</dt><dd>'+zn.length+'개'+(zn.length?' ('+zn.map(n=>'<span style="white-space:nowrap">'+esc(n)+'</span>').join(' · ')+')':'')+'</dd>';
  }
  function renderTiles(x){
    const s=x.s, c0=s.center_ts[0], ct=s.center_ts[x.ti], pc=(P&&x.pi>=0)?P.conc[x.pi]:null;
    const rm=refMain("r"), wm=refMain("w"), rl="참조값 ("+(M.city_type||M.trend_type||"")+" 중위)";
    const sn=esc(x.lb)+" → "+esc(x.lt);
    // 굵은 값 = 목표연도(선택 시나리오) 값, 아래 줄 = 과거 → 기준 · 기준 → 목표
    let h=tile("ov-cmp","콤팩트성","① 중심지 주거인구 비율",PCT(x.ti>0?ct.rconc:c0.rconc),[
      [esc(x.lp||"과거")+" → "+esc(x.lb), nz(pc)?"–":PCT(pc)+" → "+PCT(c0.rconc)+arrow(pc,c0.rconc)],
      [sn, x.ti>0?PCT(c0.rconc)+" → "+PCT(ct.rconc)+arrow(c0.rconc,ct.rconc):"–"],
      [rm!=null?rl:"참조값", rm!=null?N(rm,1)+"%":"도시위상 지정 시"]]);
    h+=tile("ov-cmp","콤팩트성","② 중심지 근무인구 비율",PCT((x.ti>0&&!nz(ct.wconc))?ct.wconc:c0.wconc),[
      [esc(x.lp||"과거")+" → "+esc(x.lb),"–"],
      [sn, (x.ti>0&&!nz(ct.wconc))?PCT(c0.wconc)+" → "+PCT(ct.wconc)+arrow(c0.wconc,ct.wconc):"–"],
      [wm!=null?rl:"참조값", wm!=null?N(wm,1)+"%":"도시위상 지정 시"]]);
    const e0=eqAt(s.conn_ts,0), et=eqAt(s.conn_ts,x.ti);
    if(e0){ const k=EQK;
      h+=tile("ov-net","네트워크성","③ 중심지 접근 형평성",PCT((x.ti>0&&et)?et.vals[k]:e0.vals[k]),[
        [e0.thr+"분 내 도달 인구 비율",esc(lvLab(e0.levels[k]))],
        [sn, (x.ti>0&&et)?PCT(e0.vals[k])+" → "+PCT(et.vals[k])+arrow(e0.vals[k],et.vals[k]):"–"]]); }
    else h+=tile("ov-net","네트워크성","③ 중심지 접근 형평성","–",[["30분 내 도달 인구 비율","격자→중심지 OD 입력 시"]]);
    const fr=[];
    [[x.fn,"근린"],[x.fh,"거점"]].forEach(a=>{ if(!a[0]) return; const p=facAt(s.fac_ts_g,a[0],0), q=facAt(s.fac_ts_g,a[0],x.ti);
      fr.push([esc(fnm(a[0]))+" ("+a[1]+")", p?PCT(p.cover)+((q&&x.ti>0)?" → "+PCT(q.cover)+arrow(p.cover,q.cover):""):"–"]); });
    if(s.fac_ts&&s.fac_ts.length) fr.push(["폐점 예상 시설 (전체)", N(s.fac_ts[0].mgmt)+((x.ti>0&&s.fac_ts[x.ti])?" → "+N(s.fac_ts[x.ti].mgmt):"")+"곳"]);
    h+=tile("ov-life","생활편리성","④ 서비스권 향유도",undefined,fr.length?fr:[["시설","근린·거점 시설 입력 시"]]);
    $id("ovTiles").innerHTML=h;
  }
  const td=(v,cls)=>'<td'+(cls?' class="'+cls+'"':'')+'>'+v+'</td>';
  const tr=cells=>'<tr>'+cells.join("")+'</tr>';
  const head=(txt,n)=>'<td class="l" rowspan="'+n+'">'+txt+'</td>';
  function renderT9(x){
    const s=x.s, ti=x.ti, T=tiersOf(s), R=[];
    $id("ovT9cap").textContent=D.region_name+" 계획권역 전체와 중심부의 공간구조 KPI 변화";
    const H='<tr><th>구분</th><th>공간 단위</th><th>'+esc(x.lp||"과거")+'</th><th>'+esc(x.lb)+'<br>(기준연도)</th><th>'+
      esc(x.lt)+'<br>('+esc(s.name)+')</th><th>현황 추세<br>('+esc(x.lp||"–")+'→'+esc(x.lb)+')</th><th>전망<br>('+
      esc(x.lb)+'→'+esc(x.lt)+', '+esc(s.name)+')</th><th>참조값</th></tr>';
    const p0=(P&&x.pi>=0)?P.region_pop[x.pi]:null, r0=s.region_ts[0].pop, rt=s.region_ts[ti].pop;
    R.push(tr([td("[참고] 총인구","l"),td("계획권역 전체"),td(nz(p0)?"–":N(p0)+"명"),td(N(r0)+"명"),td(N(rt)+"명"),
      td(chg(p0,r0,x.yp,x.yb,"pct")),td(chg(r0,rt,x.yb,x.yt,"pct")),td("–")]));
    const cB=cumList(s,0,rShareFn(s)), cT=cumList(s,ti,rShareFn(s)), cP=pastCum(s,x.pi), rR=refRows("r");
    if(cB.length) cB.forEach((c,i)=>{ const last=i===cB.length-1;
      const pv=cP?cP[i]:((last&&P&&x.pi>=0)?P.conc[x.pi]:null);
      R.push(tr([i===0?head("① 중심지<br>주거인구 비율",cB.length):"",td(c.lab+" 중심지 집합"),td(PCT(pv)),td(PCT(c.v)),
        td(nz(cT[i].v)?"–":"("+PCT(cT[i].v)+")"),td(chg(pv,c.v,x.yp,x.yb,"pp")),td(chg(c.v,cT[i].v,x.yb,x.yt,"pp")),
        td(last&&rR.length?rR.join("<br>"):"–")])); });
    const wf=wShareFn(s), wB=cumList(s,0,wf), wT=cumList(s,ti,wf), rW=refRows("w");
    wB.forEach((c,i)=>{ const last=i===wB.length-1;
      R.push(tr([i===0?head("② 중심지<br>근무인구 비율",wB.length):"",td(c.lab+" 중심지 집합"),td("–"),td(PCT(c.v)),
        td(nz(wT[i].v)?"–":"("+PCT(wT[i].v)+")"),td("–"),td(chg(c.v,wT[i].v,x.yb,x.yt,"pp")),
        td(last&&rW.length?rW.join("<br>"):"–")])); });
    const e0=eqAt(s.conn_ts,0), et=eqAt(s.conn_ts,ti);
    if(e0) e0.levels.forEach((l,i)=>{ const b=e0.vals[i], t=et?et.vals[i]:null;
      R.push(tr([i===0?head("③ 중심지 접근 형평성<br>("+e0.thr+"분 내 도달 가능<br>인구 비율)",e0.levels.length):"",
        td(esc(lvLab(l))),td("–"),td(PCT(b)),td(nz(t)?"–":"("+PCT(t)+")"),td("–"),td(chg(b,t,x.yb,x.yt,"pp")),td("–")])); });
    else R.push(tr([td("③ 중심지 접근 형평성","l"),td("–"),td("–"),'<td colspan="2">격자→중심지 OD 입력 시 산출</td>',td("–"),td("–"),td("–")]));
    const fk=[[x.fn,"근린"],[x.fh,"거점"]].filter(a=>a[0]);
    if(fk.length) fk.forEach((a,i)=>{ const p=facAt(s.fac_ts_g,a[0],0), q=facAt(s.fac_ts_g,a[0],ti);
      R.push(tr([i===0?head("④ 서비스권 향유도",fk.length):"",td(esc(fnm(a[0]))+" ("+a[1]+")"),td("–"),td(p?PCT(p.cover):"–"),
        td(q?PCT(q.cover):"–"),td("–"),td((p&&q)?chg(p.cover,q.cover,x.yb,x.yt,"pp"):"–"),td("–")])); });
    else R.push(tr([td("④ 서비스권 향유도","l"),td("–"),td("–"),'<td colspan="2">근린·거점 시설 입력 시 산출</td>',td("–"),td("–"),td("–")]));
    T.forEach((t,i)=>{ const a=(t.per_year[0]||{}).rdens, b=(t.per_year[ti]||{}).rdens;
      R.push(tr([i===0?head("[참고] 중심지<br>주거인구 밀도 (명/㎢)",T.length):"",td(t.tier+"차 중심지 (평균)"),td("–"),td(N(a)),td(N(b)),
        td("–"),td(chg(a,b,x.yb,x.yt,"pct")),td("–")])); });
    const sub=T.filter(t=>t.tier>1);
    if(sub.length){ const f=yi=>sub.map(t=>missCnt(s,yi,t.tier)+"곳").join(" / ");
      R.push(tr([td("└ 밀도 참고치 미달 중심지 수","l"),td(sub.map(t=>t.tier+"차").join(" / ")),td("–"),td(f(0)),td(f(ti)),
        td("–"),td("–"),td(N(M.dens_ref||2000)+"명/㎢")])); }
    T.forEach((t,i)=>{ const a=(t.per_year[0]||{}).wdens, b=(t.per_year[ti]||{}).wdens;
      R.push(tr([i===0?head("[참고] 중심지<br>근무인구 밀도 (명/㎢)",T.length):"",td(t.tier+"차 중심지 (평균)"),td("–"),td(N(a)),td(N(b)),
        td("–"),td(chg(a,b,x.yb,x.yt,"pct")),td("–")])); });
    if(fk.length) fk.forEach((a,i)=>{ const p=facAt(s.fac_ts_g,a[0],0), q=facAt(s.fac_ts_g,a[0],ti);
      R.push(tr([i===0?head("[참고] 관리 대상<br>(폐점 예상) 시설 수",fk.length):"",td(esc(fnm(a[0]))+" ("+a[1]+")"),td("–"),
        td(p?N(p.mgmt)+"곳":"–"),td(q?N(q.mgmt)+"곳":"–"),td("–"),td("–"),td("–")])); });
    $id("ovT9").innerHTML=H+R.join("");
    $id("ovT9note").innerHTML="주1) 괄호 안 값은 격자별 인구 비율·중심지 경계·도로망을 기준연도로 고정한 장래 산출값<br>"+
      "주2) 과거 연도는 내장 SGIS 인구총조사 격자, 기준연도는 입력 격자 기준으로 자료원이 다를 수 있음 · 근무인구와 접근 형평성은 과거 자료가 없어 기준연도부터 산출<br>"+
      "주3) 참조값은 가이드라인 표7·표8 중위값(2023년) · 밀도 참고치 "+N(M.dens_ref||2000)+"명/㎢";
  }
  function renderT10(x){
    const s=x.s, ti=x.ti, Z=s.zones||[], R=[];
    $id("ovT10cap").textContent=D.region_name+" 생활권별 공간구조 KPI 변화";
    const H='<tr><th colspan="2">지표</th>'+Z.map((z,i)=>'<th><i class="sw" style="background:'+ZPAL[i%ZPAL.length]+'"></i>'+esc(z.name)+'</th>').join("")+'<th>계획권역 전체</th></tr>';
    const cells=(f,g)=>Z.map((z,i)=>'<td>'+f(z,i)+'</td>').join("")+'<td>'+g()+'</td>';
    const ar=(a,b,fm)=>(nz(a)?"–":fm(a))+(ti>0?" → "+(nz(b)?"–":fm(b)):"");
    const yr=esc(x.lb)+(ti>0?" → "+esc(x.lt):"");
    R.push('<tr><td class="l" colspan="2">[참고] 총인구 ('+yr+')</td>'+cells(z=>ar(z.per_year[0].pop,(z.per_year[ti]||{}).pop,v=>N(v)),()=>ar(s.region_ts[0].pop,s.region_ts[ti].pop,v=>N(v)))+'</tr>');
    R.push('<tr><td class="l" colspan="2">① 중심지 주거인구 비율 ('+yr+')</td>'+cells(z=>ar(z.per_year[0].crconc,(z.per_year[ti]||{}).crconc,PCT),()=>ar(s.center_ts[0].rconc,s.center_ts[ti].rconc,PCT))+'</tr>');
    R.push('<tr><td class="l" colspan="2">② 중심지 근무인구 비율 ('+yr+')</td>'+cells(z=>ar(z.per_year[0].cwconc,(z.per_year[ti]||{}).cwconc,PCT),()=>ar(s.center_ts[0].wconc,s.center_ts[ti].wconc,PCT))+'</tr>');
    const eR=eqAt(s.conn_ts,0);
    R.push('<tr><td class="l" colspan="2">③ 중심지 접근 형평성 ('+(eR?esc(lvLab(eR.levels[EQK]))+' ':'')+(eR?eR.thr:30)+'분 내, '+yr+')</td>'+
      (eR?cells(z=>{ const e=eqAt(z.conn,0), f=eqAt(z.conn,ti); return e?ar(e.vals[EQK],f?f.vals[EQK]:null,PCT):"–"; },
               ()=>{ const f=eqAt(s.conn_ts,ti); return ar(eR.vals[EQK],f?f.vals[EQK]:null,PCT); })
         :'<td colspan="'+(Z.length+1)+'">격자→중심지 OD 입력 시 산출</td>')+'</tr>');
    const fk=[[x.fn,"근린"],[x.fh,"거점"]].filter(a=>a[0]);
    fk.forEach((a,i)=>{ R.push('<tr>'+(i===0?'<td class="l" rowspan="'+fk.length+'">④ 서비스권 향유도<br>('+yr+')</td>':'')+'<td>'+esc(fnm(a[0]))+'</td>'+
      cells(z=>{ const p=facAt(z.fac_g,a[0],0), q=facAt(z.fac_g,a[0],ti); return p?ar(p.cover,q&&q.cover,v=>N(v,1)+"%"):"–"; },
            ()=>{ const p=facAt(s.fac_ts_g,a[0],0), q=facAt(s.fac_ts_g,a[0],ti); return p?ar(p.cover,q&&q.cover,v=>N(v,1)+"%"):"–"; })+'</tr>'); });
    R.push('<tr><td class="l" colspan="2">[참고] 중심지 주거인구 밀도 (명/㎢, '+yr+')</td>'+cells(z=>ar(z.per_year[0].cdens,(z.per_year[ti]||{}).cdens,v=>N(v)),()=>ar(s.center_ts[0].density,s.center_ts[ti].density,v=>N(v)))+'</tr>');
    const allT=tiersOf(s).map(t=>t.tier), miss=(yi,zn)=>allT.reduce((a,t)=>a+missCnt(s,yi,t,zn),0);
    R.push('<tr><td class="l" colspan="2">└ 밀도 참고치 미달 중심지 수 ('+yr+')</td>'+cells(z=>miss(0,z.name)+(ti>0?" → "+miss(ti,z.name):"")+"곳",()=>miss(0,null)+(ti>0?" → "+miss(ti,null):"")+"곳")+'</tr>');
    const zwd=(zn,yi)=>{ let a=0,w=0; (s.centers||[]).forEach(c=>{ if(zn!=null&&c.zone!==zn) return; const p=c.per_year[yi]||{};
      if(!nz(p.wdens)&&c.area>0){ w+=p.wdens*c.area; a+=c.area; } }); return a>0?w/a:null; };
    const wFut=ti>0&&!nz(zwd(null,ti));   // 장래 근무인구가 있으면 기준 → 목표연도
    R.push('<tr><td class="l" colspan="2">[참고] 중심지 근무인구 밀도 (명/㎢, '+(wFut?yr:esc(x.lb))+')</td>'+
      cells(z=>wFut?ar(zwd(z.name,0),zwd(z.name,ti),v=>N(v)):N(zwd(z.name,0)),
            ()=>wFut?ar(zwd(null,0),zwd(null,ti),v=>N(v)):N(zwd(null,0)))+'</tr>');
    fk.forEach((a,i)=>{ R.push('<tr>'+(i===0?'<td class="l" rowspan="'+fk.length+'">[참고] 관리 대상<br>(폐점 예상) 시설 수 ('+esc(x.lt)+')</td>':'')+'<td>'+esc(fnm(a[0]))+'</td>'+
      cells(z=>{ const q=facAt(z.fac_g,a[0],ti); return q?N(q.mgmt)+"곳":"–"; },()=>{ const q=facAt(s.fac_ts_g,a[0],ti); return q?N(q.mgmt)+"곳":"–"; })+'</tr>'); });
    $id("ovT10").innerHTML=H+R.join("");
    $id("ovT10note").textContent="생활권 명칭은 ⑤ 상위생활권 도출의 명칭 설정을 따름 · 생활권별 값도 기준연도 경계·도로망 고정 가정";
  }
  // ---- 계획권역 핵심지표 카드 ----
  function spark(vals,labs,split){
    const pts=vals.map((v,i)=>({i:i,v:v})).filter(p=>!nz(p.v)); if(pts.length<2) return "";
    const w=300,h=82, lo=Math.min.apply(null,pts.map(p=>p.v)), hi=Math.max.apply(null,pts.map(p=>p.v)), pad=(hi-lo)*0.15||1;
    const X=i=>10+i*(w-20)/Math.max(1,vals.length-1), Y=v=>h-14-(v-(lo-pad))/((hi+pad)-(lo-pad))*(h-26);
    const seg=(a,b)=>pts.filter(p=>p.i>=a&&p.i<=b).map(p=>X(p.i).toFixed(1)+","+Y(p.v).toFixed(1)).join(" ");
    const lab=(i,anchor)=>'<text x="'+X(i).toFixed(1)+'" y="'+(h-2)+'" text-anchor="'+anchor+'" style="font-size:9px;fill:#7b8794">'+esc(labs[i])+'</text>';
    return '<svg viewBox="0 0 '+w+' '+h+'" class="ovspark">'+
      '<line x1="'+X(split)+'" y1="4" x2="'+X(split)+'" y2="'+(h-14)+'" stroke="#b8c2cc" stroke-dasharray="3 3"/>'+
      '<polyline points="'+seg(0,split)+'" fill="none" stroke="#2f5d8a" stroke-width="2"/>'+
      '<polyline points="'+seg(split,vals.length-1)+'" fill="none" stroke="#2f5d8a" stroke-width="2" stroke-dasharray="5 4"/>'+
      pts.filter(p=>p.i<=split).map(p=>'<circle cx="'+X(p.i).toFixed(1)+'" cy="'+Y(p.v).toFixed(1)+'" r="2.3" fill="#2f5d8a"/>').join("")+
      lab(0,"start")+(split>0&&split<vals.length-1?lab(split,"middle"):"")+lab(vals.length-1,"end")+'</svg>';
  }
  function card(cls,title,cat,left,right,links){
    return '<div class="ovcard"><div class="hd '+cls+'"><b>'+title+'</b><span class="tg ct">'+cat+'</span></div>'+
      '<div class="bd"><div>'+left+'</div><div>'+right+'</div></div>'+(links?'<div class="ovlinks">'+links+'</div>':'')+'</div>';
  }
  // 추이 차트: 과거~기준연도는 실선 하나, 장래는 시나리오별 점선(선택한 시나리오는 굵게)
  // x축은 연도에 비례하고 모든 시점(5년 단위·기준연도)에 연도를 표시
  function sparkSc(pv,pl,series,fl){
    const n=pv.length+fl.length, split=pv.length-1;
    const all=pv.concat([].concat.apply([],series.map(q=>q.vals))).filter(v=>!nz(v));
    if(all.length<2) return "";
    const labs=pl.concat(fl), yrs=labs.map(yNum), byYear=yrs.every(v=>v!==null)&&yrs[n-1]>yrs[0];
    const w=340,h=150, lo=Math.min.apply(null,all), hi=Math.max.apply(null,all), pad=(hi-lo)*0.15||1;
    const X=i=>14+(byYear?(yrs[i]-yrs[0])/(yrs[n-1]-yrs[0]):i/Math.max(1,n-1))*(w-62);
    const Y=v=>h-18-(v-(lo-pad))/((hi+pad)-(lo-pad))*(h-32);
    const pts=(vals,off)=>vals.map((v,i)=>nz(v)?null:X(i+off).toFixed(1)+","+Y(v).toFixed(1)).filter(Boolean).join(" ");
    const lab=i=>'<text x="'+X(i).toFixed(1)+'" y="'+(h-4)+'" text-anchor="middle" style="font-size:8.5px;fill:'+(i===split?'#334155':'#7b8794')+
      ';font-weight:'+(i===split?700:400)+'">'+esc(labs[i])+'</text>';
    let g='<svg viewBox="0 0 '+w+' '+h+'" class="ovspark">'+
      labs.map((t,i)=>'<line x1="'+X(i).toFixed(1)+'" y1="6" x2="'+X(i).toFixed(1)+'" y2="'+(h-18)+'" stroke="'+(i===split?'#b8c2cc':'#eef1f5')+'"'+(i===split?' stroke-dasharray="3 3"':'')+'/>').join("")+
      '<polyline points="'+pts(pv,0)+'" fill="none" stroke="#475569" stroke-width="2"/>'+
      pv.map((v,i)=>nz(v)?"":'<circle cx="'+X(i).toFixed(1)+'" cy="'+Y(v).toFixed(1)+'" r="2.2" fill="#475569"/>').join("");
    series.forEach(q=>{ const vals=[pv[split]].concat(q.vals), last=q.vals[q.vals.length-1];
      g+='<polyline points="'+pts(vals,split)+'" fill="none" stroke="'+q.color+'" stroke-width="'+(q.sel?3:1.6)+'" stroke-dasharray="5 3" opacity="'+(q.sel?1:0.8)+'"/>';
      if(q.sel) g+=q.vals.map((v,i)=>nz(v)?"":'<circle cx="'+X(i+split+1).toFixed(1)+'" cy="'+Y(v).toFixed(1)+'" r="2.4" fill="'+q.color+'"/>').join("");
      if(!nz(last)) g+='<circle cx="'+X(n-1).toFixed(1)+'" cy="'+Y(last).toFixed(1)+'" r="'+(q.sel?3.2:2.4)+'" fill="'+q.color+'"/>'+
        '<text x="'+(X(n-1)+5).toFixed(1)+'" y="'+(Y(last)+3).toFixed(1)+'" style="font-size:9.5px;font-weight:'+(q.sel?800:500)+';fill:'+q.color+'">'+N(last,1)+'</text>'; });
    g+=labs.map((t,i)=>lab(i)).join("")+'</svg>';
    return g+'<div class="ovsleg"><span><i style="background:#475569"></i>과거~기준연도</span>'+
      series.map(q=>'<span'+(q.sel?' style="font-weight:700;color:var(--text)"':'')+'><i style="background:'+q.color+'"></i>'+esc(q.name)+'</span>').join("")+'</div>';
  }
  function renderReg(x){
    // 핵심지표 4개를 같은 형식으로: 왼쪽 = 굵은 값(목표연도·선택 시나리오) · 과거→기준 · 기준→목표 · 참조값,
    // 오른쪽 = 과거~기준연도 실선 + 시나리오별 장래 점선 차트
    const s=x.s, ti=x.ti, fut=ti>0, c0=s.center_ts[0], ct=s.center_ts[ti], T=tiersOf(s), sub=T.filter(t=>t.tier>1);
    const tl=fut?esc(x.lt)+" ("+esc(s.name)+")":esc(x.lb)+" (기준연도)";
    const scs=get=>SC.map((q,i)=>({name:q.name,color:PAL[i%PAL.length],sel:q===s,vals:q.years.slice(1).map((y,j)=>get(q,j+1))}));
    const capS='<div class="ovsmall">실선: 과거~기준연도 · 점선: 시나리오별 장래 (굵은 선 = 선택한 시나리오) · 끝 숫자: '+esc(s.years[s.years.length-1])+'년 값</div>';
    const shortLv=l=>String(l).replace(/차\+/g,"·")+" 중심지까지";   // 1차+2차 → 1·2차 중심지까지
    function left(formula,v0,vt,pv,refs,rm,extra){
      const big=(fut&&!nz(vt))?vt:v0;
      return '<div class="ovformula">'+formula+'</div><dl class="ovkv">'+
        '<dt>'+tl+'</dt><dd><b style="font-size:20px">'+PCT(big)+'</b></dd>'+
        '<dt>'+esc(x.lp||"과거")+' → '+esc(x.lb)+'</dt><dd>'+(nz(pv)?"– (과거 자료 없음)":PCT(pv)+" → "+PCT(v0)+" · "+chg(pv,v0,x.yp,x.yb,"pp"))+'</dd>'+
        '<dt>'+esc(x.lb)+' → '+esc(x.lt)+'</dt><dd>'+((fut&&!nz(vt))?PCT(v0)+" → "+PCT(vt)+" · "+chg(v0,vt,x.yb,x.yt,"pp"):"–")+'</dd>'+
        (refs?'<dt>참조값</dt><dd>'+(refs.length?refs.join(" · "):"도시위상·인구추세 유형 지정 시 표시")+'</dd>':'')+
        (extra||"")+'</dl>'+
        ((rm!=null&&!nz(big))?'<div class="ovrefbar"><i style="width:'+Math.min(100,big)+'%"></i><em style="left:'+Math.min(100,rm)+'%"></em></div>'+
          '<div class="ovsmall">막대: '+tl+' 값 · 세로선: 참조값</div>':'');
    }
    const right=(pv,pl,series,desc)=>sparkSc(pv,pl,series,s.years.slice(1))+capS+
      '<details><summary>지표 해설 (가이드라인)</summary><p>'+desc+'</p></details>';
    // 과거 구간은 과거 비교연도부터(기본 2015 → 2015·2020·기준연도), 장래는 시나리오 시점 전부(5년 단위)
    const hasPast=!!(P&&P.conc&&P.years), p0=(hasPast&&x.pi>=0)?x.pi:0;
    const pl=hasPast?P.years.slice(p0).map((y,i,a)=>i===a.length-1?x.lb:y):[x.lb];
    const pc=(P&&x.pi>=0)?P.conc[x.pi]:null;
    // ① 중심지 주거인구 비율
    let h=card("ov-cmp","① 중심지 주거인구 비율","콤팩트성 · 핵심지표",
      left("권역 주거인구 중 1·2·3차 중심지(중심부)에 거주하는 인구 비율<br>= 중심부 주거인구 ÷ 권역 주거인구 × 100",c0.rconc,ct.rconc,pc,refRows("r"),refMain("r")),
      right(hasPast?P.conc.slice(p0):[c0.rconc],pl,scs((q,i)=>q.center_ts[i].rconc),
        "증가는 중심지로의 인구 집중을, 감소는 인구 분산을 뜻합니다. 총인구가 감소하는 지역에서는 주변부가 더 빨리 감소해 비율이 오를 수 있으므로 중심지 인구 수와 밀도를 함께 봅니다."),
      '관련 참고지표 — 중심지 주거인구 밀도 '+N(c0.density)+(fut?' → '+N(ct.density):'')+'명/㎢'+((P&&x.pi>=0&&P.center_dens)?' ('+esc(x.lp)+': '+N(P.center_dens[x.pi])+'명/㎢)':'')+
      (sub.length?' · 밀도 참고치 미달 중심지 '+sub.map(t=>t.tier+'차 '+missCnt(s,0,t.tier)+(fut?' → '+missCnt(s,ti,t.tier):'')+'곳').join(' / '):''));
    // ② 중심지 근무인구 비율
    h+=card("ov-cmp","② 중심지 근무인구 비율","콤팩트성 · 핵심지표",
      left("권역 근무인구 중 1·2·3차 중심지(중심부)에서 근무하는 인구 비율<br>= 중심부 근무인구 ÷ 권역 근무인구 × 100",c0.wconc,ct.wconc,null,refRows("w"),refMain("w")),
      right([c0.wconc],[x.lb],scs((q,i)=>q.center_ts[i].wconc),
        "증가는 상업·업무·생산 기능의 중심지 집적을, 감소는 일자리의 외곽 이동을 뜻합니다. 근무인구는 사업장 소재지 기준으로 집계됩니다."),
      '관련 참고지표 — 중심지 근무인구 밀도 '+T.map(t=>t.tier+'차 '+N((t.per_year[0]||{}).wdens)+(fut?' → '+N((t.per_year[ti]||{}).wdens):'')).join(' · ')+'명/㎢');
    // ③ 중심지 접근 형평성 (대표값 = EQK 단계)
    const e0=eqAt(s.conn_ts,0), et=eqAt(s.conn_ts,ti);
    if(e0){
      const others=e0.levels.map((l,i)=>i===EQK?"":'<dt>'+esc(shortLv(l))+'</dt><dd>'+PCT(e0.vals[i])+((fut&&et)?' → '+PCT(et.vals[i]):'')+'</dd>').join("");
      h+=card("ov-net","③ 중심지 접근 형평성","네트워크성 · 핵심지표",
        left("가장 가까운 "+esc(lvLab(e0.levels[EQK]))+" "+e0.thr+"분 내 도달 가능한 인구 비율 (도로 최단시간 기준)",e0.vals[EQK],et?et.vals[EQK]:null,null,null,null,
          others?'<dt style="grid-column:1/-1;margin-top:4px">참고 — 다른 위계 누적 집합</dt>'+others:""),
        right([e0.vals[EQK]],[x.lb],scs((q,i)=>{ const e=eqAt(q.conn_ts,i); return e?e.vals[EQK]:null; }),
          "도로망은 기준연도로 고정하므로 장래 값의 변화는 인구 분포 변화만 반영합니다."));
    } else h+=card("ov-net","③ 중심지 접근 형평성","네트워크성 · 핵심지표",
      '<dl class="ovkv"><dt>'+esc(x.lb)+'</dt><dd>격자→중심지 OD 입력 시 산출</dd><dt>공간 단위</dt><dd>가장 가까운 1차 / 1·2차 / 1·2·3차 중심지까지 30분 내</dd></dl>',
      '<details open><summary>지표 해설 (가이드라인)</summary><p>도로망은 기준연도로 고정하므로 장래 값의 변화는 인구 분포 변화만 반영합니다.</p></details>');
    // ④ 서비스권 향유도 (대표값 = 선택한 근린·거점 시설 평균)
    const fk=[[x.fn,"근린"],[x.fh,"거점"]].filter(a=>a[0]);
    const favg=(q,i)=>{ const v=fk.map(a=>facAt(q.fac_ts_g,a[0],i)).filter(u=>u&&!nz(u.cover)).map(u=>u.cover);
      return v.length?v.reduce((p,r)=>p+r,0)/v.length:null; };
    if(fk.length){
      const each=fk.map(a=>{ const p=facAt(s.fac_ts_g,a[0],0), q=facAt(s.fac_ts_g,a[0],ti);
        return '<dt>'+esc(fnm(a[0]))+' ('+a[1]+')</dt><dd>'+(p?PCT(p.cover)+((q&&fut)?' → '+PCT(q.cover):''):'–')+
          (p?' · 폐점 예상 '+N(p.mgmt)+((q&&fut)?' → '+N(q.mgmt):'')+'곳':'')+'</dd>'; }).join("");
      h+=card("ov-life","④ 서비스권 향유도","생활편리성 · 핵심지표",
        left("시설 서비스권 안에 거주하는 인구 비율 — 표시값은 "+fk.map(a=>esc(fnm(a[0]))).join("·")+(fk.length>1?" 평균":""),favg(s,0),favg(s,ti),null,null,null,each)+
          '<div class="ovsmall">표시 시설은 진단 요약 탭의 선택을 따름</div>',
        right([favg(s,0)],[x.lb],scs(favg),"장래 값은 인구 분포 변화와 폐점 예상 시설 제외를 반영해 산출합니다."));
    } else h+=card("ov-life","④ 서비스권 향유도","생활편리성 · 핵심지표",
      '<dl class="ovkv"><dt>시설</dt><dd>근린·거점 시설 폴더 입력 시 산출</dd></dl>',
      '<details open><summary>지표 해설 (가이드라인)</summary><p>장래 값은 인구 분포 변화와 폐점 예상 시설 제외를 반영해 산출합니다.</p></details>');
    $id("ovReg").innerHTML='<h3 class="ovh">핵심지표 — 계획권역 전체</h3>'+h;
  }
  // ---- 중심부(위계별) ----
  function renderCtr(x){
    const s=x.s, ti=x.ti, T=tiersOf(s);
    if(!T.length){ $id("ovCtrCum").innerHTML='<h3>중심부</h3><div class="ovnote">중심지 위계 값이 없어 누적 집합을 만들 수 없습니다.</div>'; $id("ovCtrRef").innerHTML=""; return; }
    const cB=cumList(s,0,rShareFn(s)), cT=cumList(s,ti,rShareFn(s)), wB=cumList(s,0,wShareFn(s)), e0=eqAt(s.conn_ts,0);
    let t='<tr><th>지표</th>'+cB.map(c=>'<th>'+esc(c.lab)+'</th>').join("")+'</tr>';
    t+='<tr><td class="l">① 주거인구 비율 '+esc(x.lb)+'</td>'+cB.map(c=>'<td>'+PCT(c.v)+'</td>').join("")+'</tr>';
    if(ti>0) t+='<tr><td class="l">① 주거인구 비율 '+esc(x.lt)+'</td>'+cT.map(c=>'<td>'+PCT(c.v)+'</td>').join("")+'</tr>';
    t+='<tr><td class="l">② 근무인구 비율 '+esc(x.lb)+'</td>'+wB.map(c=>'<td>'+PCT(c.v)+'</td>').join("")+'</tr>';
    t+='<tr><td class="l">③ 접근 형평성 '+esc(x.lb)+'</td>'+(e0?cB.map((c,i)=>'<td>'+PCT(e0.vals[i])+'</td>').join(""):'<td colspan="'+cB.length+'">OD 입력 시</td>')+'</tr>';
    $id("ovCtrCum").innerHTML='<h3>중심부 — 위계 누적 집합</h3><div class="ovtw"><table class="ovtbl">'+t+'</table></div>'+
      '<div class="ovnote">④ 서비스권 향유도는 가이드라인 표6에서 중심부 단위가 없음</div>';
    let r='<tr><th>지표</th>'+T.map(q=>'<th>'+q.tier+'차 ('+q.count+'곳)</th>').join("")+'</tr>';
    r+='<tr><td class="l">중심지 주거인구 밀도 (명/㎢, '+esc(x.lb)+(ti>0?' → '+esc(x.lt):'')+')</td>'+T.map(q=>'<td>'+N((q.per_year[0]||{}).rdens)+(ti>0?' → '+N((q.per_year[ti]||{}).rdens):'')+'</td>').join("")+'</tr>';
    r+='<tr><td class="l">밀도 참고치('+N(M.dens_ref||2000)+'명/㎢) 미달 중심지 ('+esc(x.lb)+(ti>0?' → '+esc(x.lt):'')+')</td>'+T.map(q=>'<td>'+missCnt(s,0,q.tier)+(ti>0?' → '+missCnt(s,ti,q.tier):'')+'곳</td>').join("")+'</tr>';
    r+='<tr><td class="l">중심지 근무인구 밀도 (명/㎢, '+esc(x.lb)+')</td>'+T.map(q=>'<td>'+N((q.per_year[0]||{}).wdens)+'</td>').join("")+'</tr>';
    $id("ovCtrRef").innerHTML='<div class="ovcap">참고지표 — 위계별</div><div class="ovtw"><table class="ovtbl">'+r+'</table></div>';
  }
  // ---- 생활권별 ----
  function renderZone(x){
    const s=x.s, ti=x.ti, Z=s.zones||[], fk=[[x.fn,"근린"],[x.fh,"거점"]].filter(a=>a[0]);
    // 목표연도를 고르면 기준연도 → 목표연도(선택 시나리오) 값을 함께 표시
    const yh=esc(x.lb)+(ti>0?' → '+esc(x.lt):''), two=(a,b,f)=>f(a)+((ti>0&&!nz(b))?' → '+f(b):'');
    let q='<tr><th>생활권</th><th>총인구 ('+yh+')</th><th>① 주거 비율</th><th>② 근무 비율</th></tr>';
    q+=Z.map((z,i)=>{ const a=z.per_year[0], b=z.per_year[ti]||{}; return '<tr><td class="l"><a href="#" data-gozone="'+z.zid+'"><i class="sw" style="background:'+ZPAL[i%ZPAL.length]+'"></i>'+esc(z.name)+'</a></td><td>'+
      two(a.pop,b.pop,N)+'</td><td>'+two(a.crconc,b.crconc,PCT)+'</td><td>'+two(a.cwconc,b.cwconc,PCT)+'</td></tr>'; }).join("");
    $id("ovZoneQuick").innerHTML='<h3>생활권 한눈에</h3><div class="ovtw"><table class="ovtbl">'+q+'</table></div>';
    const yr=esc(x.lb)+(ti>0?' → '+esc(x.lt):'');
    $id("ovZoneSecs").innerHTML=Z.map((z,i)=>{
      const p0=z.per_year[0], pt=z.per_year[ti]||{}, e=eqAt(z.conn,0), et=eqAt(z.conn,ti);
      const fr=fk.map(a=>{ const p=facAt(z.fac_g,a[0],0), u=facAt(z.fac_g,a[0],ti); return [esc(fnm(a[0])), p?PCT(p.cover)+((u&&ti>0)?' → '+PCT(u.cover):''):'–']; });
      const miss=yi=>tiersOf(s).reduce((a,t)=>a+missCnt(s,yi,t.tier,z.name),0);
      return '<div class="ovzdet" id="ovz-'+z.zid+'"><div class="zh"><i style="background:'+ZPAL[i%ZPAL.length]+'"></i><b>'+esc(z.name)+'</b>'+
        '<span class="ovnote" style="margin:0 0 0 auto">총인구 '+N(p0.pop)+(ti>0?' → '+N(pt.pop):'')+' ('+yr+')</span></div><div class="zb">'+
        // 굵은 값 = 목표연도(선택 시나리오) 값, 아래 줄 = 기준연도 → 목표연도
        tile("ov-cmp","콤팩트성","① 중심지 주거인구 비율",PCT(ti>0?pt.crconc:p0.crconc),[[yr, ti>0?PCT(p0.crconc)+' → '+PCT(pt.crconc)+arrow(p0.crconc,pt.crconc):"–"]])+
        tile("ov-cmp","콤팩트성","② 중심지 근무인구 비율",PCT((ti>0&&!nz(pt.cwconc))?pt.cwconc:p0.cwconc),[[yr,(ti>0&&!nz(pt.cwconc))?PCT(p0.cwconc)+' → '+PCT(pt.cwconc)+arrow(p0.cwconc,pt.cwconc):"–"]])+
        tile("ov-net","네트워크성","③ 중심지 접근 형평성",e?PCT((ti>0&&et)?et.vals[EQK]:e.vals[EQK]):"–",
          [[yr,(e&&ti>0&&et)?PCT(e.vals[EQK])+' → '+PCT(et.vals[EQK])+arrow(e.vals[EQK],et.vals[EQK]):"–"],
           [(e?e.thr:30)+"분 내 도달",e?esc(lvLab(e.levels[EQK])):"OD 입력 시"]])+
        tile("ov-life","생활편리성","④ 서비스권 향유도",undefined,fr.length?fr:[["시설","시설 입력 시"]])+
        '</div><div class="ovlinks">참고지표 — 중심지 주거인구 밀도 '+N(p0.cdens)+(ti>0?' → '+N(pt.cdens):'')+'명/㎢ · 밀도 참고치 미달 중심지 '+
        miss(0)+(ti>0?' → '+miss(ti):'')+'곳'+(fk.length?' · 폐점 예상 ('+esc(x.lt)+') '+fk.map(a=>{ const u=facAt(z.fac_g,a[0],ti); return esc(fnm(a[0]))+' '+(u?N(u.mgmt):'–')+'곳'; }).join(' / '):'')+
        '</div></div>';
    }).join("");
  }
  function renderOV(){ const x=st(); renderBasic(x); renderTiles(x); renderT9(x); renderT10(x); renderReg(x); renderCtr(x); renderZone(x); syncBars(); }
  selS.addEventListener("change",()=>{ fillTarget(); renderOV(); });
  [selP,selT,selN,selH].forEach(e=>e.addEventListener("change",renderOV));
  renderOV();

  // ---- 상세 분석 선택 상자 연동: 시나리오·목표시점을 탭 위 선택과 양방향으로 맞춤('시나리오 비교' 보기 상자는 제외) ----
  const LINK=[["kpiScen","kpiYear"],["zShareScen",null],["zTableScen","zTableYear"],["facTableScen","facTableYear"],
              ["ctScen","ctYear"],["ctShareScen",null],[null,"connYear"]];
  let linking=false;
  const hasOpt=(el,v)=>Array.prototype.some.call(el.options,o=>o.value===v);
  function pushLinks(){ if(linking) return; linking=true;
    LINK.forEach(p=>{ const sc=p[0]&&$id(p[0]), yr=p[1]&&$id(p[1]);
      if(sc&&sc.value!==selS.value&&hasOpt(sc,selS.value)){ sc.value=selS.value; sc.dispatchEvent(new Event("change")); }
      if(yr&&!selT.disabled&&yr.value!==selT.value&&hasOpt(yr,selT.value)){ yr.value=selT.value; yr.dispatchEvent(new Event("change")); } });
    linking=false; }
  LINK.forEach(p=>{ const sc=p[0]&&$id(p[0]), yr=p[1]&&$id(p[1]);
    if(sc) sc.addEventListener("change",()=>{ if(linking||!hasOpt(selS,sc.value)) return; selS.value=sc.value; selS.dispatchEvent(new Event("change")); });
    if(yr) yr.addEventListener("change",()=>{ if(linking||!hasOpt(selT,yr.value)) return; selT.value=yr.value; selT.dispatchEvent(new Event("change")); }); });
  [selS,selT].forEach(e=>e.addEventListener("change",pushLinks));
  pushLinks();

  // ---- 지도 상호작용 ----
  function goZone(zid){
    const tb=document.querySelector('.tab[data-tab="zones"]'); if(tb&&!tb.classList.contains("active")) tb.click();
    const el=$id("ovz-"+zid); if(!el) return;
    const maxY=document.documentElement.scrollHeight-innerHeight;
    const y=Math.min(maxY,Math.max(0,el.getBoundingClientRect().top+scrollY-20));
    window.scrollTo({top:y,behavior:"smooth"});
    setTimeout(()=>{ if(Math.abs(scrollY-y)>4) window.scrollTo(0,y); },700);
    el.classList.add("flash"); setTimeout(()=>el.classList.remove("flash"),1800);
  }
  document.addEventListener("click",e=>{
    const t=e.target;
    const b=t.closest&&t.closest(".ovsite");
    if(b){ const box=b.closest(".ovmapbox").querySelector(".ovinset"); box.hidden=!box.hidden; b.classList.toggle("on",!box.hidden); return; }
    const g=t.closest&&t.closest(".ovsgg");
    if(g){ const L=g.closest(".ovmapbox").querySelector('svg [data-lyr="sgg"]'); if(!L) return;
      const show=L.style.display==="none"; L.style.display=show?"":"none"; g.classList.toggle("on",show); return; }
    const li=t.closest&&t.closest(".ovli");
    if(li){ const off=li.classList.toggle("off");
      li.closest(".ovmapbox").querySelectorAll('svg [data-lyr="'+li.dataset.lyr+'"]').forEach(k=>{ k.style.display=off?"none":""; }); return; }
    const go=t.closest&&t.closest("[data-gozone]");
    if(go){ e.preventDefault(); goZone(go.dataset.gozone); return; }
    if(t.closest&&t.closest("#ovZoneMap svg")){
      const zp=(t.classList&&t.classList.contains("ovzp"))?t:
        document.elementsFromPoint(e.clientX,e.clientY).find(k=>k.classList&&k.classList.contains("ovzp"));
      if(zp) goZone(zp.dataset.zone);
    }
  });
  const tip=$id("ovTip");
  document.addEventListener("mousemove",e=>{
    const el=e.target.closest?e.target.closest(".ovmapbox svg [data-name]"):null;
    if(el&&tip){ tip.textContent=el.dataset.name; tip.style.display="block"; tip.style.left=(e.clientX+12)+"px"; tip.style.top=(e.clientY+12)+"px"; }
    else if(tip) tip.style.display="none";
  });
})();
</script>
<script>
// ===== 성과 평가 지원 (목표치 설정 → 평가 실행) =====
// 리포트에 들어 있는 시나리오별 연도 값(D.scenarios)만 써서 브라우저에서 계산한다. 플러그인 입력은 늘리지 않는다.
(function(){
  var btn=document.getElementById("evalBtn"), wrap=document.getElementById("evalWrap");
  if(!btn||!wrap) return;
  if(!SC.some(s=>s.years&&s.years.length>1)){ btn.style.display="none"; return; }
  const M=D.meta||{}, P=D.past;
  // 가이드라인 표7(주거)·표8(근무) 중위값 (2023년) — 비교값·참조값 목표에 사용
  const REF={r:{"거점도시":98.2,"강소도시":90.7,"자립도시":57.2,"성장형":91.8,"성숙·안정형":77.1,"감소형":59.9},
             w:{"거점도시":84.5,"강소도시":72.6,"자립도시":60.5,"성장형":73.2,"성숙·안정형":68.6,"감소형":65.3}};
  const $=id=>document.getElementById(id);
  const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  const nz=v=>v===null||v===undefined||Number.isNaN(v);
  // 리포트 본문과 같은 반올림(toLocaleString) — toFixed는 82.85를 82.8로 내려 본문(82.9)과 어긋남
  const fx=(v,d)=>Number(v).toLocaleString("ko-KR",{minimumFractionDigits:d,maximumFractionDigits:d,useGrouping:false});
  const f1=v=>nz(v)?"–":fx(v,1);
  const sg=(v,d)=>nz(v)?"–":(v>=0?"+":"−")+fx(Math.abs(v),d==null?1:d);
  const N0=v=>nz(v)?"–":Number(v).toLocaleString("ko-KR",{maximumFractionDigits:0});
  const yN=s=>{const n=parseInt(s,10); return Number.isNaN(n)?null:n;};
  const C_ACT="#2563eb", C_TGT="#ea580c", C_BAU="#94a3b8";
  const BADGE={"초과 달성":["#dcfce7","#166534","↗"],"달성":["#dcfce7","#166534","↗"],
               "소폭 미달":["#fef3c7","#92400e","→"],"미달":["#fee2e2","#991b1b","↘"]};
  const STORE="ssim_eval_"+(D.region_name||"")+"_"+(D.run_tag||"");
  let memo={}, saved={};
  try{ saved=JSON.parse(localStorage.getItem(STORE)||"{}")||{}; memo=saved.memo||{}; }catch(e){ saved={}; memo={}; }
  function save(){ try{ localStorage.setItem(STORE,JSON.stringify(Object.assign(state(),{memo:memo}))); }catch(e){} }

  const facAll=Object.keys((SC[0]&&SC[0].fac_ts_g)||{}).filter(k=>k.indexOf("t:")===0);
  const FN=facAll.filter(k=>/^t:근린/.test(k)), FH=facAll.filter(k=>/^t:거점/.test(k));
  const fnm=k=>String(k||"").replace(/^t:(근린|거점)_?/,"");
  const thr=(D.conn_meta&&D.conn_meta.thresholds&&D.conn_meta.thresholds[0])||30;
  const pastOK=!!(P&&P.years&&P.years.length>1&&P.conc);

  // ---- 지표 정의 ----
  function ser(s,k){
    const n=s.years.length;
    if(k==="r") return (s.center_ts||[]).slice(0,n).map(r=>r.rconc);
    if(k==="w") return (s.center_ts||[]).slice(0,n).map(r=>r.wconc);
    if(k==="e") return s.years.map((y,i)=>{ const c=(s.conn_ts||[])[i]; return c&&c.equity&&c.equity[0]?c.equity[0][0]:null; });
    if(k==="f"){ const g=s.fac_ts_g||{}, a=g[$("evFN").value]||[], b=g[$("evFH").value]||[];
      return s.years.map((y,i)=>{ const v=[a[i],b[i]].filter(x=>x&&!nz(x.cover)).map(x=>x.cover);
        return v.length?v.reduce((p,q)=>p+q,0)/v.length:null; }); }
    return [];
  }
  const IND=[
    {k:"r",t:"① 중심지 주거인구 비율",tag:"콤팩트성 · 핵심지표",unit:()=>"1·2·3차 중심지 누적"},
    {k:"w",t:"② 중심지 근무인구 비율",tag:"콤팩트성 · 핵심지표",unit:()=>"1·2·3차 중심지 누적"},
    {k:"e",t:"③ 중심지 접근 형평성",tag:"네트워크성 · 핵심지표",unit:()=>"1차 중심지까지 "+thr+"분 내 도달 인구 비율"},
    {k:"f",t:"④ 서비스권 향유도",tag:"생활편리성 · 핵심지표",unit:()=>[fnm($("evFN").value),fnm($("evFH").value)].filter(Boolean).join("·")+" 평균"}];
  function methods(k){
    const m=[];
    if(k==="r"&&pastOK) m.push(["trend","과거 추세 연장"]);
    m.push(["keep","기준연도 수준 유지"]);
    if(REF[k]){ if(REF[k][M.city_type]!=null) m.push(["refc",M.city_type+" 중위값"]);
                if(REF[k][M.trend_type]!=null) m.push(["reft",M.trend_type+" 중위값"]); }
    m.push(["manual","직접 입력"]);
    return m;
  }

  // ---- 설정 화면 ----
  const baseY=M.base_year||yN(SC[0].years[0]);
  function opt(sel,list,val){ sel.innerHTML=""; list.forEach(o=>sel.add(new Option(o[1],o[0]))); if(val!=null&&list.some(o=>String(o[0])===String(val))) sel.value=val; }
  $("evRow1").innerHTML=
    '<label>실적 시나리오 <select id="evA"></select></label>'+
    '<label>비교 시나리오 <select id="evB"></select></label>'+
    '<span>기준연도(t0) <b>'+esc(baseY)+'</b></span>'+
    '<label>평가 시점 <select id="evT"></select></label>'+
    (pastOK?'<label>추세 산정 시작연도 <select id="evP"></select></label>':'')+
    '<label>판정 구간 ± <input id="evTh" type="number" step="0.1" min="0" value="0.5"> %p</label>';
  $("evRow2").innerHTML=facAll.length?
    '<span>④ 서비스권 향유도 시설</span><label>근린 <select id="evFN"></select></label><label>거점 <select id="evFH"></select></label>'+
    '<span class="evhint">두 시설 향유도의 평균으로 평가</span>':'<span>시설 입력이 없어 ④ 서비스권 향유도는 평가하지 않습니다.</span><select id="evFN" hidden></select><select id="evFH" hidden></select>';
  const scOpts=SC.map((s,i)=>[String(i),s.name]);
  const iPlan=SC.findIndex(s=>/계획/.test(s.name)), iBau=SC.findIndex(s=>/BAU/i.test(s.name));
  opt($("evA"),scOpts,saved.a!=null?saved.a:String(iPlan>=0?iPlan:Math.min(1,SC.length-1)));
  opt($("evB"),scOpts,saved.b!=null?saved.b:String(iBau>=0?iBau:0));
  function fillT(){ const s=SC[+$("evA").value], cur=$("evT").value||saved.t;
    opt($("evT"),s.years.map((y,i)=>[String(i),y]).slice(1),cur!=null&&+cur<s.years.length?cur:String(s.years.length-1)); }
  fillT();
  if(pastOK){ const py=P.years.slice(0,-1).map((y,i)=>[String(i),y]);
    opt($("evP"),py,saved.p!=null?saved.p:String(Math.max(0,P.years.indexOf("2015")>=0?P.years.indexOf("2015"):py.length-1))); }
  if(saved.th!=null) $("evTh").value=saved.th;
  const ovN=$("ovFacN"), ovH=$("ovFacH");
  opt($("evFN"),FN.map(k=>[k,fnm(k)]),saved.fn||(ovN&&ovN.value));
  opt($("evFH"),FH.map(k=>[k,fnm(k)]),saved.fh||(ovH&&ovH.value));

  const avail=k=>ser(SC[+$("evA").value],k).some(v=>!nz(v));
  $("evTgBody").innerHTML=IND.map(x=>
    '<tr data-k="'+x.k+'"><td class="l">'+esc(x.t)+'</td><td class="rl" data-u="'+x.k+'"></td><td data-b="'+x.k+'"></td>'+
    '<td><select data-m="'+x.k+'"></select></td><td><input type="number" step="0.1" data-v="'+x.k+'"></td></tr>').join("");
  IND.forEach(x=>{ const sm=document.querySelector('[data-m="'+x.k+'"]'), ms=methods(x.k);
    opt(sm,ms,(saved.m&&saved.m[x.k])||ms[0][0]);
    if(saved.m&&saved.m[x.k]==="manual"&&saved.v&&!nz(saved.v[x.k])) document.querySelector('[data-v="'+x.k+'"]').value=saved.v[x.k]; });

  // 목표치 계산: [값, 설정 문구]
  function target(k){
    const A=SC[+$("evA").value], ti=+$("evT").value, YT=yN(A.years[ti]), b0=ser(A,k)[0];
    const m=document.querySelector('[data-m="'+k+'"]').value, inp=document.querySelector('[data-v="'+k+'"]');
    if(nz(b0)) return [null,"–"];
    if(m==="trend"){ const pi=+$("evP").value, py=yN(P.years[pi]), pv=P.conc[pi], sl=(b0-pv)/(baseY-py);
      return [b0+sl*(YT-baseY), py+"→"+baseY+" 연평균 "+sg(sl,2)+"%p 추세를 "+YT+"년까지 연장"]; }
    if(m==="keep") return [b0,"기준연도 수준 유지"];
    if(m==="refc") return [REF[k][M.city_type],M.city_type+" 중위값(가이드라인 표"+(k==="r"?7:8)+") 도달"];
    if(m==="reft") return [REF[k][M.trend_type],M.trend_type+" 중위값(가이드라인 표"+(k==="r"?7:8)+") 도달"];
    const v=parseFloat(inp.value); return [Number.isNaN(v)?null:v,"직접 입력"];
  }
  function refresh(){
    const A=SC[+$("evA").value];
    IND.forEach(x=>{ const ok=avail(x.k), tr=document.querySelector('tr[data-k="'+x.k+'"]');
      tr.style.opacity=ok?1:.45;
      tr.querySelector('[data-u]').textContent=ok?x.unit():"산출 자료 없음";
      tr.querySelector('[data-b]').textContent=f1(ser(A,x.k)[0]);
      const m=tr.querySelector('[data-m]').value, inp=tr.querySelector('[data-v]');
      inp.readOnly=(m!=="manual");
      if(m!=="manual"){ const t=target(x.k)[0]; inp.value=nz(t)?"":fx(t,1); } });
    const th=f1(Math.abs(parseFloat($("evTh").value)||0));
    $("evHint").textContent="판정(평가 시점 실적 − 목표): +"+th+"%p 이상 초과 달성 · 0 이상 달성 · −"+th+"%p 이상 소폭 미달 · 그 아래 미달";
  }
  function state(){ const m={}, v={}; IND.forEach(x=>{ m[x.k]=document.querySelector('[data-m="'+x.k+'"]').value;
      v[x.k]=parseFloat(document.querySelector('[data-v="'+x.k+'"]').value); });
    return {a:$("evA").value,b:$("evB").value,t:$("evT").value,p:pastOK?$("evP").value:null,th:$("evTh").value,
            fn:$("evFN").value,fh:$("evFH").value,m:m,v:v}; }
  $("evSet").addEventListener("change",e=>{ if(e.target.id==="evA") fillT(); refresh(); save(); });
  $("evSet").addEventListener("input",e=>{ if(e.target.dataset&&e.target.dataset.v) save(); });
  refresh();

  // ---- 평가 결과 ----
  function judge(d,th){ return d>=th?"초과 달성":d>=0?"달성":d>=-th?"소폭 미달":"미달"; }
  function chart(yrs,a,t,b,YT){
    const vals=a.concat(b,t).filter(v=>!nz(v)); let lo=Math.min.apply(null,vals), hi=Math.max.apply(null,vals);
    const pad=(hi-lo)*0.18||1; lo-=pad; hi+=pad;
    const w=520,h=210,l=46,r=16,tp=14,bt=30, Y0=yrs[0], Y1=yrs[yrs.length-1];
    const X=y=>l+(y-Y0)/((Y1-Y0)||1)*(w-l-r), Yv=v=>tp+(hi-v)/(hi-lo)*(h-tp-bt);
    const pts=arr=>yrs.map((y,i)=>nz(arr[i])?null:X(y).toFixed(1)+","+Yv(arr[i]).toFixed(1)).filter(Boolean).join(" ");
    let g='<svg viewBox="0 0 '+w+' '+h+'" width="100%">';
    for(let i=0;i<5;i++){ const v=lo+(hi-lo)*i/4;
      g+='<line x1="'+l+'" x2="'+(w-r)+'" y1="'+Yv(v).toFixed(1)+'" y2="'+Yv(v).toFixed(1)+'" stroke="#eef1f5"/>'+
         '<text x="'+(l-6)+'" y="'+(Yv(v)+4).toFixed(1)+'" text-anchor="end" class="ax">'+v.toFixed(1)+'</text>'; }
    yrs.forEach((y,i)=>{ g+='<text x="'+X(y).toFixed(1)+'" y="'+(h-10)+'" text-anchor="middle" class="ax">'+y+(i===0?" (t0)":"")+'</text>'; });
    g+='<line x1="'+X(YT).toFixed(1)+'" x2="'+X(YT).toFixed(1)+'" y1="'+tp+'" y2="'+(h-bt)+'" stroke="#64748b" stroke-dasharray="2 3"/>';
    g+='<polyline points="'+pts(b)+'" fill="none" stroke="'+C_BAU+'" stroke-width="2" stroke-dasharray="2 4"/>';
    g+='<polyline points="'+pts(t)+'" fill="none" stroke="'+C_TGT+'" stroke-width="2.2" stroke-dasharray="7 4"/>';
    g+='<polyline points="'+pts(a)+'" fill="none" stroke="'+C_ACT+'" stroke-width="3"/>';
    yrs.forEach((y,i)=>{ if(!nz(a[i])) g+='<circle cx="'+X(y).toFixed(1)+'" cy="'+Yv(a[i]).toFixed(1)+'" r="3.5" fill="'+C_ACT+'"/>'; });
    return g+'</svg>';
  }
  function supText(k,A,B,ti){
    if(k==="r"){ const c=A.center_ts, cb=B.center_ts;
      return "중심지 주거인구 밀도 "+N0(c[0].density)+" → "+N0(c[ti].density)+"명/㎢ ("+esc(B.name)+" "+N0(cb[ti].density)+")<br>중심지 주거인구 "+
             f1(c[0].pop/1e4)+"만 → "+f1(c[ti].pop/1e4)+"만 명"; }
    if(k==="w"){ const c=A.center_ts, cb=B.center_ts;
      return "중심지 근무인구 "+f1(c[0].wpop/1e4)+"만 → "+f1(c[ti].wpop/1e4)+"만 명 ("+esc(B.name)+" "+f1(cb[ti].wpop/1e4)+"만 명)"; }
    if(k==="e"){ const q=i=>{ const c=A.conn_ts[i]; return c&&c.equity&&c.equity[0]?c.equity[0]:[]; };
      // 누적 위계 이름은 실제 있는 위계로(2차가 없으면 1·3차). 대표값(첫 단계) 다음 단계들
      const lv=((A.conn_ts[0]||{}).levels||["1차","1차+2차","1차+2차+3차"]).map(l=>String(l).replace(/차\+/g,"·"));
      return lv.slice(1).map((l,j)=>l+" 중심지까지 "+f1(q(0)[j+1])+" → "+f1(q(ti)[j+1])+"%").join(" · "); }
    if(k==="f"){ const g=A.fac_ts_g||{}, out=[];
      const ks=[$("evFN").value,$("evFH").value].filter((v,i,a)=>v&&g[v]&&g[v][0]&&a.indexOf(v)===i);
      ks.forEach(f=>out.push(fnm(f)+" "+f1(g[f][0].cover)+" → "+f1(g[f][ti].cover)+"%"));
      const cov=out.join(" · ");
      return cov+(ks.length?"<br>폐점 예상 "+ks.map(f=>fnm(f)+" "+N0(g[f][0].mgmt)+" → "+N0(g[f][ti].mgmt)+"곳").join(" · "):""); }
    return "";
  }
  function refText(k){ if(!REF[k]) return "–"; const o=[];
    if(REF[k][M.city_type]!=null) o.push(M.city_type+" 중위 "+f1(REF[k][M.city_type])+"%");
    if(REF[k][M.trend_type]!=null) o.push(M.trend_type+" 중위 "+f1(REF[k][M.trend_type])+"%");
    return o.join(" · ")||"–"; }
  const memoEl=(key,ph,tag)=>'<'+(tag||"span")+' class="evmemo" contenteditable="true" data-memo="'+key+'" data-ph="'+esc(ph)+'">'+esc(memo[key]||"")+'</'+(tag||"span")+'>';

  function run(){
    const A=SC[+$("evA").value], B=SC[+$("evB").value], ti=+$("evT").value, th=Math.abs(parseFloat($("evTh").value)||0);
    const yrs=A.years.map(yN), Y0=yrs[0], YT=yrs[ti], res=[];
    let cards="";
    IND.forEach(x=>{
      if(!avail(x.k)) return;
      const a=ser(A,x.k), b=ser(B,x.k), tg=target(x.k), T=tg[0];
      if(nz(T)) return;
      const t=yrs.map(y=>y<=YT?a[0]+(T-a[0])*(y-Y0)/((YT-Y0)||1):null);
      const d=a[ti]-T, lab=judge(d,th), bd=BADGE[lab]; res.push({x:x,lab:lab,d:d,a:a[ti]});
      const rows=[["실적("+A.name+")",a,C_ACT,true],["목표 트렌드",t,C_TGT,false],[B.name,b,C_BAU,false]];
      const tb='<table><tr><th></th>'+A.years.map(y=>'<th>'+esc(y)+'</th>').join("")+'</tr>'+rows.map(r=>
        '<tr><td class="rl" style="color:'+r[2]+'">'+esc(r[0])+'</td>'+r[1].map((v,i)=>'<td'+(r[3]&&i===ti?' class="b"':'')+'>'+f1(v)+'</td>').join("")+'</tr>').join("")+'</table>';
      cards+='<div class="evcard"><div class="evch"><div><div class="evtag">'+esc(x.tag)+'</div><div class="evtt">'+esc(x.t)+'</div>'+
        '<div class="evun">'+esc(x.unit())+' (%)</div></div><div class="evbd" style="background:'+bd[0]+';color:'+bd[1]+'">'+bd[2]+' '+lab+
        '<span>'+YT+' 목표 대비 '+sg(d)+'%p</span></div></div>'+chart(yrs,a,t,b,YT)+tb+
        '<div class="evsup"><b>목표 설정</b><span>'+esc(tg[1])+' → '+YT+'년 '+f1(T)+'%</span></div>'+
        '<div class="evsup"><b>보충지표</b><span>'+supText(x.k,A,B,ti)+'</span></div>'+
        '<div class="evnote"><b>고려사항</b>'+memoEl("note_"+x.k,"해석·고려사항을 입력하세요")+'</div>'+
        '<div class="evref">비교값: '+esc(refText(x.k))+'</div></div>';
    });
    const ok=res.filter(r=>r.lab==="달성"||r.lab==="초과 달성");
    const auto="핵심지표 "+res.length+"개 중 "+ok.length+"개 달성"+(ok.length?"("+ok.map(r=>r.x.t.replace(/^\S+\s/,"")).join("·")+")":"")+"|"+
      res.map(r=>r.x.t.replace(/^(\S+)\s.*/,"$1")+" "+sg(r.d)+"%p "+r.lab).join(" · ");
    $("evalResult").innerHTML='<div class="evres">'+
      '<div class="evchips"><span>'+esc(D.region_name||"")+'</span><span>기준연도(t0) '+Y0+'</span><span>평가 시점 '+YT+(ti===A.years.length-1?"(목표연도)":"")+'</span>'+
      '<span>실적: '+esc(A.name)+' 시나리오</span><span>비교: '+esc(B.name)+'</span><span>목표: 사용자 설정</span>'+
      (avail("e")?'<span>접근 형평성 임계치 '+thr+'분</span>':'')+'</div>'+
      '<div class="evleg"><i style="border-color:'+C_ACT+'"></i>실적<i style="border-color:'+C_TGT+';border-top-style:dashed"></i>목표 트렌드'+
      '<i style="border-color:'+C_BAU+';border-top-style:dotted"></i>'+esc(B.name)+'<i class="vt"></i>평가 시점</div>'+
      '<div class="evgrid">'+cards+'</div>'+
      '<div class="evsum"><div><b>달성 상황 평가</b><span class="evauto">'+esc(auto).replace("|","<br>")+'</span>'+memoEl("sum_eval","평가 의견을 입력하세요","span")+'</div>'+
      '<div><b>향후 대응</b>'+memoEl("sum_act","향후 대응 방향을 입력하세요","span")+'</div></div>'+
      '<div class="evfn">주) 목표치는 사용자가 설정한 값이며, 실적은 '+esc(A.name)+' 시나리오의 '+YT+'년 전망값임. 판정 = 평가 시점 실적 − 목표(구간 ±'+f1(th)+'%p). 고려사항·평가 의견·향후 대응은 사용자 입력. 자료: S-Sim KPI 종합 리포트</div>'+
      '</div>';
    save();
  }
  $("evalResult").addEventListener("input",e=>{ const k=e.target.dataset&&e.target.dataset.memo; if(k){ memo[k]=e.target.innerText.trim(); save(); } });
  $("evRun").addEventListener("click",run);

  btn.addEventListener("click",()=>{ wrap.style.display="flex"; });
  $("evalClose").addEventListener("click",()=>{ wrap.style.display="none"; });
  wrap.addEventListener("click",e=>{ if(e.target===wrap) wrap.style.display="none"; });
  window.ssimEvalRun=run;   // 캡처·점검용
})();
</script>
</body>
</html>
"""
