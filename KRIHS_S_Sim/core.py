# -*- coding: utf-8 -*-
"""순수 파이썬 코어 — QGIS 비의존.

테이블 로드·검증·배분 계산을 담당한다.
입력 스펙은 INPUT_SPEC.md 강제 표준을 따르며, 위반 시 ValueError(한글 메시지)를
던지고 알고리즘 층에서 QgsProcessingException 으로 변환한다.
"""
import csv
import math
import os
import re

# 연도 컬럼 패턴 (강제 표준)
YEAR_POP = re.compile(r'^pop_(\d{4})$')   # 시군구 추계 테이블
YEAR_ANY = re.compile(r'^(?:pop_)?(\d{4})(?:년)?$')   # 2030 / 2030년 / pop_2030

# KOTI 개발계획 연도별 입주율 (한국교통연구원 2025, 충남 장래 중심지 분석 표 2-4)
# (계획인구 하한, (준공연도 D부터의 연도별 비율))
ENTRY_RATES = (
    (100_000, (0.30, 0.20, 0.20, 0.20, 0.10)),
    (50_000, (0.50, 0.20, 0.15, 0.15)),
    (0, (0.75, 0.15, 0.10)),
)


# ── 파일 로드 ─────────────────────────────────────────────
def _read_rows(path):
    """CSV(utf-8-sig) 또는 xlsx 첫 시트를 [{헤더: 값}] 목록으로 반환."""
    ext = os.path.splitext(path)[1].lower()
    if ext == '.csv':
        with open(path, 'r', encoding='utf-8-sig', newline='') as f:
            return list(csv.DictReader(f))
    if ext == '.xlsx':
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else '' for h in next(rows_iter, [])]
        rows = [dict(zip(header, r)) for r in rows_iter]
        wb.close()
        return rows
    raise ValueError(f'지원하지 않는 파일 형식입니다: {ext or path} (csv/xlsx만 가능)')


def _to_int(value, where):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f'{where} 값을 정수로 변환할 수 없습니다: {value!r}')


def _to_float(value, where):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f'{where} 값을 숫자로 변환할 수 없습니다: {value!r}')


def detect_year_cols(names, pattern):
    """컬럼명 목록에서 pattern(그룹1=연도)에 맞는 컬럼을 찾아 {연도: 컬럼명} 반환."""
    out = {}
    for n in names:
        m = pattern.match(str(n).strip())
        if m:
            out[int(m.group(1))] = n
    return out


def load_sgg_table(path):
    """시군구 추계 테이블 로드 (IN-2).

    필수: sgg_cd_h, pop_YYYY 1개 이상.
    Returns: ({sgg: {yr: float}}, [yr 오름차순], NULL→0 처리 건수)
    """
    rows = _read_rows(path)
    if not rows:
        raise ValueError(f'추계 테이블이 비어 있습니다: {path}')
    header = list(rows[0].keys())
    if 'sgg_cd_h' not in header:
        raise ValueError(
            '추계 테이블에 sgg_cd_h 컬럼이 없습니다. '
            'INPUT_SPEC.md 표준(sgg_cd_h + pop_YYYY)으로 변환 후 사용하세요.')
    year_cols = detect_year_cols(header, YEAR_POP)
    if not year_cols:
        raise ValueError(
            '추계 테이블에 연도 인구 컬럼(pop_2025 형식)이 없습니다. '
            f'현재 컬럼: {", ".join(header)}')

    table = {}
    n_null = 0
    for i, row in enumerate(rows, 2):
        raw = row.get('sgg_cd_h')
        if raw is None or str(raw).strip() == '':
            continue
        sgg = _to_int(raw, f'{i}행 sgg_cd_h')
        if sgg in table:
            raise ValueError(f'추계 테이블 sgg_cd_h 중복: {sgg}')
        vals = {}
        for yr, col in year_cols.items():
            v = row.get(col)
            if v is None or str(v).strip() == '':
                n_null += 1
                vals[yr] = 0.0
                continue
            fv = _to_float(v, f'{i}행 {col}')
            if fv < 0:
                raise ValueError(f'{i}행 {col} 음수 인구는 허용되지 않습니다: {fv}')
            vals[yr] = fv
        table[sgg] = vals
    if not table:
        raise ValueError(f'추계 테이블에 유효한 행이 없습니다: {path}')
    return table, sorted(year_cols), n_null


# ── 장래 근무인구 추계 (충남 PDF 2장 방식) ───────────────
def load_long_table(path):
    """(지역, 연도, 값) 3열 long 테이블 → {지역: {연도: float}}.

    첫 3개 컬럼을 순서대로 이름/연도/값으로 해석 (컬럼명 무관).
    '합계' 포함 지역명은 제외.
    """
    rows = _read_rows(path)
    if not rows:
        raise ValueError(f'테이블이 비어 있습니다: {path}')
    header = list(rows[0].keys())
    if len(header) < 3:
        raise ValueError(f'(지역, 연도, 값) 3개 컬럼이 필요합니다: {header}')
    name_c, yr_c, val_c = header[:3]
    out = {}
    for i, row in enumerate(rows, 2):
        name = str(row.get(name_c) or '').strip()
        if not name or '합계' in name:
            continue
        yr = _to_int(row.get(yr_c), f'{i}행 {yr_c}')
        out.setdefault(name, {})[yr] = _to_float(row.get(val_c), f'{i}행 {val_c}')
    if not out:
        raise ValueError(f'유효한 행이 없습니다: {path}')
    return out


def load_wide_year_table(path):
    """지역 + 연도 컬럼(wide) 테이블 → {지역: {연도: float}}.

    첫 컬럼 = 지역명, 연도 컬럼명은 2030 / 2030년 / pop_2030 모두 인식.
    '합계' 포함 지역명은 제외.
    """
    rows = _read_rows(path)
    if not rows:
        raise ValueError(f'테이블이 비어 있습니다: {path}')
    header = list(rows[0].keys())
    name_c = header[0]
    year_cols = {}
    for h in header[1:]:
        m = YEAR_ANY.match(str(h).strip())
        if m:
            year_cols[int(m.group(1))] = h
    if not year_cols:
        raise ValueError(f'연도 컬럼(2030/2030년/pop_2030 형식)이 없습니다: {header}')
    out = {}
    for i, row in enumerate(rows, 2):
        name = str(row.get(name_c) or '').strip()
        if not name or '합계' in name:
            continue
        out[name] = {y: _to_float(row.get(c), f'{i}행 {c}')
                     for y, c in year_cols.items()}
    if not out:
        raise ValueError(f'유효한 행이 없습니다: {path}')
    return out


def worker_projection(emp_rates, pop15, anchors):
    """시·군 장래 근무인구 추계 — 충남 장래 중심지 분석(2026) 2장 방식.

    1) 고용률 시계열의 연평균 증감률(CAGR)을 마지막 관측 고용률에 적용해 장래 고용률 산출
    2) 취업자(y) = 장래 고용률(y)/100 × 15세 이상 장래인구(y)
    3) 취업자 구간별 CAGR을 실측 근무인구(앵커)에 연쇄 적용

    emp_rates: {지역: {연도: 고용률%}}  (관측 2개 연도 이상)
    pop15:     {지역: {연도: 15세 이상 인구}}  (연도들이 목표연도 구간을 정의)
    anchors:   {지역: (기준연도, 실측 근무인구)}
    Returns: ({지역: {목표연도: 근무인구}}, [목표연도])
    """
    result = {}
    target_years = None
    for name, (ay, aval) in anchors.items():
        if name not in emp_rates:
            raise ValueError(f'고용률 시계열에 없는 지역: {name!r}')
        if name not in pop15:
            raise ValueError(f'15세 이상 장래인구에 없는 지역: {name!r}')
        rates = emp_rates[name]
        if len(rates) < 2:
            raise ValueError(f'{name}: 고용률 관측 연도가 2개 미만입니다')
        y0, y1 = min(rates), max(rates)
        if rates[y0] <= 0:
            raise ValueError(f'{name}: {y0}년 고용률이 0 이하입니다')
        r = (rates[y1] / rates[y0]) ** (1.0 / (y1 - y0)) - 1.0   # 연평균 고용률 증감률

        pyrs = sorted(pop15[name])
        if len(pyrs) < 2:
            raise ValueError(f'{name}: 15세 이상 인구 연도가 2개 미만입니다')
        emp = {y: rates[y1] * (1 + r) ** (y - y1) / 100.0 * pop15[name][y]
               for y in pyrs}   # 취업자 수

        out, cur, cur_y = {}, float(aval), ay
        for t0, t1 in zip(pyrs, pyrs[1:]):
            if t1 <= ay:
                continue
            if emp[t0] <= 0:
                raise ValueError(f'{name}: {t0}년 취업자 수가 0 이하입니다')
            g = (emp[t1] / emp[t0]) ** (1.0 / (t1 - t0)) - 1.0   # 구간 CAGR
            span = t1 - max(cur_y, t0)
            cur = cur * (1 + g) ** span
            cur_y = t1
            out[t1] = cur
        if not out:
            raise ValueError(f'{name}: 앵커 연도({ay}) 이후 목표연도가 없습니다')
        yrs = sorted(out)
        if target_years is None:
            target_years = yrs
        elif yrs != target_years:
            raise ValueError(f'{name}: 목표연도가 다른 지역과 다릅니다 ({yrs} vs {target_years})')
        result[name] = out
    return result, target_years


# ── 배분 계산 ─────────────────────────────────────────────
def exp_weight(ratio, is_hub, a, b, c, d):
    """거점/비거점 × ratio 크기에 따른 지수 가중치."""
    r = max(ratio, 0.0)
    if is_hub:
        return r ** a if r >= 1 else r ** b
    return r ** c if r >= 1 else r ** d


def allocate_by_weight(weights, total):
    """가중치 비례 배분. 가중치 합 0이면 균등 배분.

    weights: {gid: w}, total: 배분할 총량
    Returns: ({gid: pop}, equal_fallback 여부)
    """
    if total <= 0:
        return {gid: 0.0 for gid in weights}, False
    total_w = sum(weights.values())
    if total_w <= 0:
        eq = total / len(weights)
        return {gid: eq for gid in weights}, True
    return {gid: w / total_w * total for gid, w in weights.items()}, False


def init_ratio(grids):
    """초기 ratio = 기준인구 / 시군구 평균. 평균 0이면 1.0."""
    mean = sum(g['pop0'] for g in grids) / len(grids)
    for g in grids:
        g['ratio'] = g['pop0'] / mean if mean > 0 else 1.0


def update_ratio(grids, result):
    """배분 결과 기준으로 ratio 갱신 (다음 연도 연쇄 전달)."""
    mean = sum(result[g['gid']] for g in grids) / len(grids)
    for g in grids:
        g['ratio'] = result[g['gid']] / mean if mean > 0 else 1.0


# ── 계획인구(개발/비개발) 계산 ────────────────────────────
def entry_rates(pop):
    """계획인구 규모 구간에 해당하는 입주율 배열."""
    for threshold, rates in ENTRY_RATES:
        if pop >= threshold:
            return rates
    return ENTRY_RATES[-1][1]


def entry_schedule(plan_pop, yr_done, band_pop=None):
    """사업 계획인구를 준공연도(D)부터 입주율 규칙으로 연도별 분산.

    - 규모 구간 판정은 band_pop(사업 전체 계획인구 합, 복수 시군 분할 사업용)이
      있으면 그 값으로, 없으면 plan_pop으로 한다.
    - 연도별 값은 최대잔여법으로 정수 보정 — 합계가 plan_pop과 정확히 일치.
      (plan_pop이 소수이면 잔여 소수분을 최대잔여 슬롯에 더해 합계 보존)

    Returns: {연도: 유입 인구}
    """
    if plan_pop <= 0:
        return {}
    rates = entry_rates(band_pop if band_pop and band_pop > 0 else plan_pop)
    shares = [plan_pop * r for r in rates]
    base = [float(math.floor(s)) for s in shares]
    rem = plan_pop - sum(base)
    order = sorted(range(len(rates)),
                   key=lambda i: shares[i] - base[i], reverse=True)
    i = 0
    while rem >= 1 - 1e-9:
        base[order[i % len(order)]] += 1
        rem -= 1
        i += 1
    if rem > 1e-9:
        base[order[i % len(order)]] += rem
    return {yr_done + k: v for k, v in enumerate(base) if v > 0}


def target_of(year, targets):
    """연간 유입 연도 → 반영할 목표연도 (자신보다 큰 최소 목표연도).

    예: 목표 [2025,2030,2035,2040] 에서 2028·2029 → 2030, 2030·2031 → 2035.
    마지막 목표연도 이후 유입이면 None (반영 불가).
    """
    for t in targets:
        if t > year:
            return t
    return None
