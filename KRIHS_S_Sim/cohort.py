# -*- coding: utf-8 -*-
"""코호트 변화율 배분 계산 (QGIS 비의존).

단위(그릇)
  - 읍·면이 있는 시군구: 읍·면 각각 + 동 전체를 묶은 '동지역' 하나. 격자는 겹친 면적이 가장 큰 읍면동의 단위에 속함
  - 동만 있는 시군구: 동 각각. 격자는 겹친 면적 비율로 여러 동에 나눠 연결
변화율·투영·기준연도 맞추기·시군구 총량·근무 15세 이상 배율은 국토연 코호트 배분 코드
(cohort_units_emd.py, cohort_project_emd.py, work_project_emd.py)와 같다. 수렴 기준은 시군구 평균.
"""
from collections import Counter, defaultdict

DONG_UNIT = '동지역'
SGIS_YEARS = (2000, 2005, 2010, 2015, 2020)
NA = 18           # 5세 18구간 (0~4 … 80~84, 85+)
AGE = range(NA)
_H = '가나다라마바사아자차카타파하'


def _blank():
    return {'T': [0.0] * NA, 'M': [0.0] * NA, 'F': [0.0] * NA, 'tot': 0.0}


def build_links(overlap, sgg_of, emd_names):
    """overlap {gid: [(읍면동 이름, 겹친 면적)]}, emd_names {시군구: {읍면동 이름}} → {gid: {(시군구, 단위): 가중치}}, 동만 시군구 집합."""
    only = {s for s, ns in emd_names.items() if ns and all(n.endswith('동') for n in ns)}
    links = {}
    for g, L in overlap.items():
        s = sgg_of[g]
        if s in only:
            tot = sum(a for _, a in L)
            w = defaultdict(float)
            for n, a in L:
                w[(s, n)] += a / tot
            links[g] = dict(w)
        else:
            nm = max(L, key=lambda t: t[1])[0]
            links[g] = {(s, DONG_UNIT if nm.endswith('동') else nm): 1.0}
    return links, only


def aggregate(links, sgis, sgg_of, years):
    U = {y: defaultdict(_blank) for y in years}
    C = {y: defaultdict(_blank) for y in years}
    for y in years:
        for g, d in sgis[y].items():
            if g not in links:
                continue
            for u, a in links[g].items():
                x = U[y][u]
                x['tot'] += d['tot'] * a
                for k in 'TMF':
                    for i in AGE:
                        x[k][i] += d[k][i] * a
            x = C[y][sgg_of[g]]
            x['tot'] += d['tot']
            for k in 'TMF':
                for i in AGE:
                    x[k][i] += d[k][i]
    return U, C


def _scaled(d):
    s = sum(d['T'])
    k = d['tot'] / s if s else 1.0
    return {key: [v * k for v in d[key]] for key in 'TMF'}


def _xy(g):
    return _H.index(g[0]) * 100 + int(g[2:4]), _H.index(g[1]) * 100 + int(g[4:6])


def make_plan(links, U, last, pool_sex_pop, borrow_pop):
    """단위별 성별 계산 여부·변화율 산출 단위(작은 단위는 맞댄 격자 변이 가장 많은 이웃과 묶음)."""
    pos = {}
    for g in links:
        try:
            pos[_xy(g)] = g
        except (ValueError, IndexError):
            pass
    touch = defaultdict(Counter)

    def add(a, b):
        for u in a:
            for v in b:
                if u != v and u[0] == v[0]:
                    touch[u][v] += 1
                    touch[v][u] += 1

    for (x, y), g in pos.items():
        for dx, dy in ((1, 0), (0, 1)):
            h = pos.get((x + dx, y + dy))
            if h:
                add(links[g], links[h])
        K = list(links[g])
        for i, u in enumerate(K):
            add([u], K[i + 1:])
    units = sorted({u for d in links.values() for u in d})
    plan = {}
    for u in units:
        p = U[last][u]['tot'] if u in U[last] else 0.0
        ru = [u]
        if p < borrow_pop and touch[u]:
            ru = [u, max(touch[u].items(), key=lambda t: t[1])[0]]
        plan[u] = {'sex': p >= pool_sex_pop, 'ru': ru}
    return units, plan


def rates(units, plan, U, C, Y):
    per = list(zip(Y[:-1], Y[1:]))
    S = {y: {u: _scaled(U[y][u]) for u in units if u in U[y]} for y in Y}
    SC = {y: {s: _scaled(C[y][s]) for s in C[y]} for y in Y}
    zero = [0.0] * NA

    def ccr(src, key):
        num, den = [0.0] * NA, [0.0] * NA
        for y0, y1 in per:
            p0, p1 = src(y0, key), src(y1, key)
            for a in range(1, 17):
                num[a] += p1[a]
                den[a] += p0[a - 1]
            num[17] += p1[17]
            den[17] += p0[16] + p0[17]
        return [round(num[a] / den[a], 4) if den[a] > 0 and num[a] > 0 else None for a in AGE]

    def cwr(src):
        k = sum(src(y, 'T')[0] for y in Y[1:])
        w = sum(sum(src(y, 'F')[3:10]) for y in Y[1:])
        return round(k / w, 4) if w and k else None

    def many(us):
        def f(y, key):
            arr = [0.0] * NA
            for x in us:
                arr = [p + q for p, q in zip(arr, S[y].get(x, {key: zero})[key])]
            return arr
        return f

    own_c, own_w = {}, {}
    for u in units:
        src = many(plan[u]['ru'])
        own_c[u] = {k: ccr(src, k) for k in (('M', 'F') if plan[u]['sex'] else ('T',))}
        own_w[u] = cwr(src)
    city_c, city_w = {}, {}
    for s in {u[0] for u in units}:
        if s not in SC[Y[-1]]:
            continue
        src = (lambda sg: (lambda y, key: SC[y].get(sg, _blank())[key]))(s)
        city_c[s] = {k: ccr(src, k) for k in 'MFT'}
        city_w[s] = cwr(src)
    return S, own_c, own_w, city_c, city_w


def weight(k, half_life):
    if half_life <= 0:
        return 0.0
    return 0.5 ** ((5 * k + 2.5) / half_life)


def _converge(own, city, w):
    if own and city:
        return city * (own / city) ** w
    return city if city else (own or 0)


def _step(p, r):
    n = [0.0] * NA
    for a in range(1, 17):
        n[a] = p[a - 1] * (r[a] or 0)
    n[17] = (p[16] + p[17]) * (r[17] or 0)
    return n


def project(units, plan, rr, Y, half_life, end):
    """단위별 2020 → end 5년 단계 투영. 반환: 총인구 {t}, 연령 3구간 {t}, SGIS 인구 없는 단위 목록."""
    S, own_c, own_w, city_c, city_w = rr
    y0, ym = Y[-1], Y[-2]
    proj, ages, none = {}, {}, []
    for u in units:
        sg = u[0]
        if u not in S[y0] or sum(S[y0][u]['T']) <= 0 or sg not in city_c:
            none.append(u)
            continue
        sex = plan[u]['sex']
        ru = plan[u]['ru']
        m0 = sum(S[y].get(x, {'M': [0]})['M'][0] for y in (ym, y0) for x in ru)
        f0 = sum(S[y].get(x, {'F': [0]})['F'][0] for y in (ym, y0) for x in ru)
        ms = m0 / (m0 + f0) if m0 + f0 else 0.512
        s1 = S[y0][u]
        if sex:
            M, F = s1['M'][:], s1['F'][:]
        else:
            T = s1['T'][:]
            fs = [s1['F'][a] / s1['T'][a] if s1['T'][a] else 0.5 for a in AGE]
        ser = {y0: sum(s1['T'])}
        ag = {}
        for k, t in enumerate(range(y0 + 5, end + 1, 5)):
            w = weight(k, half_life)
            r = {s: [_converge(own_c[u][s][a], city_c[sg][s][a], w) for a in AGE] for s in own_c[u]}
            c = _converge(own_w[u], city_w[sg], w)
            if sex:
                M, F = _step(M, r['M']), _step(F, r['F'])
                b = c * sum(F[3:10])
                M[0], F[0] = b * ms, b * (1 - ms)
                arr = [M[a] + F[a] for a in AGE]
            else:
                T = _step(T, r['T'])
                T[0] = c * sum(T[a] * fs[a] for a in range(3, 10))
                arr = T
            ser[t] = sum(arr)
            ag[t] = (sum(arr[:3]), sum(arr[3:13]), sum(arr[13:]))
        proj[u] = ser
        ages[u] = ag
    return proj, ages, none


def _at(series, t, geometric=True):
    """5년 계열 {연도: 값}에서 t의 값 — 계열에 없으면 앞뒤 5년 값 사이를 보간."""
    if t in series:
        return series[t]
    lo = max(y for y in series if y < t)
    hi = min(y for y in series if y > t)
    f = (t - lo) / (hi - lo)
    a, b = series[lo], series[hi]
    if geometric and a > 0 and b > 0:
        return a * (b / a) ** f
    return a + (b - a) * f


def residential(links, sgg_of, pop_r, sgis, totals, years, base_year,
                half_life=5.0, pool_sex_pop=5000, borrow_pop=1000):
    """거주 코호트 배분. totals {시군구: {연도: 추계}}.

    반환 dict: grid {gid: {연도: 인구}}, units, unit 정보(기준인구, 연도별 인구, 15세 이상 배율 r15), scale(시군구 배율)
    """
    Y = SGIS_YEARS
    y0 = Y[-1]
    end = max(years)
    end5 = y0 + 5 * max(1, -(-(end - y0) // 5))
    U, C = aggregate(links, sgis, sgg_of, Y)
    units, plan = make_plan(links, U, y0, pool_sex_pop, borrow_pop)
    rr = rates(units, plan, U, C, Y)
    proj, ages, none = project(units, plan, rr, Y, half_life, end5)
    base = defaultdict(float)
    for g, d in links.items():
        for u, a in d.items():
            base[u] += pop_r.get(g, 0.0) * a
    anch = {}
    for u in proj:
        p0 = _at(proj[u], base_year)
        anch[u] = {t: (base[u] * _at(proj[u], t) / p0 if p0 else base[u]) for t in years}
    for u in none:      # SGIS 인구가 없는 단위: 같은 시군구 단위들의 평균 배율
        S_ = [v for v in proj if v[0] == u[0]]
        sb = sum(base[v] for v in S_)
        anch[u] = {t: base[u] * (sum(anch[v][t] for v in S_) / sb if sb else 1.0) for t in years}
    final, scale = {u: {} for u in units}, defaultdict(dict)
    for sg in {u[0] for u in units}:
        S_ = [u for u in units if u[0] == sg]
        for t in years:
            sa = sum(anch[u][t] for u in S_)
            scale[sg][t] = totals[sg][t] / sa if sa else 1.0
            for u in S_:
                final[u][t] = anch[u][t] * scale[sg][t] if sa else (totals[sg][t] * base[u] / (sum(base[v] for v in S_) or 1))
    mult = {u: {t: (final[u][t] / base[u] if base[u] else 1.0) for t in years} for u in units}
    # 15세 이상 변화 배율 (근무 배분용)
    S20 = rr[0][y0]
    r15 = {}
    for u in proj:
        if not base[u]:
            continue
        a0 = S20[u]['T']
        s = {y0: 1 - sum(a0[:3]) / sum(a0)}
        s.update({t: 1 - ages[u][t][0] / sum(ages[u][t]) for t in ages[u]})
        sb = _at(s, base_year, geometric=False)
        r15[u] = {t: (final[u][t] * _at(s, t, geometric=False)) / (base[u] * sb) for t in years}
    grid = {g: {t: pop_r.get(g, 0.0) * sum(a * mult[u][t] for u, a in d.items()) for t in years}
            for g, d in links.items()}
    info = {u: {'base': base[u], 'final': final[u], 'r15': r15.get(u), 'sex': plan[u]['sex'],
                'rate_units': plan[u]['ru'], 'no_sgis': u in none} for u in units}
    return {'grid': grid, 'units': units, 'info': info, 'scale': scale}


def work(links, sgg_of, pop_w, r15, totals_w, years):
    """근무 코호트 연동 배분. r15 {단위: {연도: 15세 이상 배율} 또는 None}."""
    units = sorted({u for d in links.values() for u in d})
    w23 = defaultdict(float)
    for g, d in links.items():
        for u, a in d.items():
            w23[u] += pop_w.get(g, 0.0) * a
    wmult = {u: {} for u in units}
    scale = defaultdict(dict)
    for sg in {u[0] for u in units}:
        S_ = [u for u in units if u[0] == sg]
        V = [u for u in S_ if r15.get(u)]
        for t in years:
            den = sum(w23[u] for u in V)
            mean = sum(w23[u] * r15[u][t] for u in V) / den if den else 1.0
            raw = {u: w23[u] * (r15[u][t] if r15.get(u) else mean) for u in S_}
            sr = sum(raw.values())
            k = totals_w[sg][t] / sr if sr else 1.0
            scale[sg][t] = k
            for u in S_:
                wmult[u][t] = raw[u] * k / w23[u] if w23[u] else 1.0
    grid = {g: {t: pop_w.get(g, 0.0) * sum(a * wmult[u][t] for u, a in d.items()) for t in years}
            for g, d in links.items()}
    return {'grid': grid, 'w23': w23, 'wmult': wmult, 'scale': scale}
