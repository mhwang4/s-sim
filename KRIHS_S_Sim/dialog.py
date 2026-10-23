# -*- coding: utf-8 -*-
"""통합 대화상자 — 왼쪽 트리 네비게이션 + 오른쪽 페이지 (2중 트리 구조).

거주인구 배분  ▸ 공통 데이터 / 1단계 BAU / 2단계 거점유도 / 3단계 계획인구
근무인구 배분  ▸ 공통 데이터 / 1단계 BAU / 2단계 계획반영
3D 시각화     (공통 — 모든 배분 결과 사용 가능)

근무인구는 같은 배분 엔진을 기준 필드(pop_w)·근무인구 추계 테이블로 재사용한다.
각 단계 페이지는 자체 [실행] 버튼·출력 파일을 가진다 (단계별 개별 저장).
계산은 기존 프로세싱 알고리즘(krihs_ssim:*)을 그대로 호출 (로직 중복 없음).
"""
import json
import os
import re
import statistics
import tempfile
import webbrowser

from PyQt5.QtCore import QSettings, Qt
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QScrollArea, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)
from qgis import processing
from qgis.core import (
    QgsApplication as QgsApp, QgsCategorizedSymbolRenderer, QgsFieldProxyModel,
    QgsFillSymbol, QgsGraduatedSymbolRenderer, QgsProcessingContext,
    QgsProcessingFeedback, QgsProcessingParameterDefinition, QgsProject,
    QgsRendererCategory, QgsRendererRange, QgsSingleSymbolRenderer,
    QgsStyle, QgsSymbol, QgsVectorLayer,
)
from PyQt5.QtGui import QColor
from qgis.gui import QgsCheckableComboBox, QgsFieldComboBox, QgsFileWidget

TYPE_OPTIONS = ['주변부', '중심지Ⅰ', '중심지Ⅱ', '중심지Ⅲ-1', '중심지Ⅲ-2']
VECTOR_FILTER = '벡터 레이어 (*.gpkg *.shp);;GeoPackage (*.gpkg);;Shapefile (*.shp)'
RES, WORK = '거주', '근무'

# 결과 폴더 구성 (결과 저장 폴더\S-Sim_분석결과(지역명)\ 아래) — 좌측 메뉴 구성과 같게
OUT_FUT = '장래인구 격자 배분 및 중심지 전망'
OUT_FUT_CTR = os.path.join(OUT_FUT, '장래 중심지 전망')
OUT_SP = '중심지 및 생활권 분석'
OUT_SP_CTR = os.path.join(OUT_SP, '중심지 분석')
OUT_SP_ZONE = os.path.join(OUT_SP, '생활권 분석')
OUT_KPI = 'KPI 지표 계산'


def out_alloc(d):
    """거주·근무인구 배분 결과 폴더 (결과 루트 기준 상대 경로)."""
    return os.path.join(OUT_FUT, f'{d}인구 배분')

# 입력 데이터 형식·샘플 (형식 버튼에서 표로 표시)
# {key: (제목, [(컬럼, 타입, 필수여부, 설명)], [샘플 헤더], [샘플 행], [비고])}
DATA_SPECS = {
    'pack': (
        'S-Sim 데이터팩 폴더 (지역별 공통 데이터 묶음)',
        [('region.json', 'JSON', '필수', '지역 정의 — 지역명·시군구 코드·자료 경로'),
         ('00_경계', 'GPKG', '필수', '계획권역·시군구·읍면동 경계'),
         ('01_격자', 'GPKG', '필수', '국토공간거점지도 1km 격자'),
         ('02_생활인프라', 'GPKG', '필수', '생활인프라 충족도'),
         ('03_인구추계', 'CSV/XLSX', '필수', '시군구 인구 추계·고용률'),
         ('04_개발사업', 'GPKG', '선택', '개발사업 지구'),
         ('05_시설', 'GPKG', '필수', '근린시설·거점시설'),
         ('06_탄소', 'CSV', '선택', '격자 탄소 배출량'),
         ('07_사용자자료', 'GPKG/CSV', '필수', '도로망·통행 행렬')],
        [], [],   # 예시 표 없음
        [],       # 주석 없음
    ),
    'grid': (
        '격자 레이어 (국토공간거점지도, 폴리곤 shp/gpkg)',
        [('gid', '문자', '필수', '격자 고유 ID — 중복 금지'),
         ('sgg_cd_h', '정수', '필수', '시군구 코드 — 추계 테이블과 같은 코드 체계'),
         ('pop_r', '숫자', '필수', '기준연도 거주인구 (거주인구 배분 기준)'),
         ('pop_w', '숫자', '근무 시 필수', '기준연도 근무인구 (근무인구 배분 기준)'),
         ('type', '문자', '선택', '거점 유형 — 정책관심구역 산출에 사용')],
        ['gid', 'sgg_cd_h', 'pop_r', 'pop_w', 'type'],
        [['다바4321', '34010', '1532.4', '2210.0', '중심지Ⅱ'],
         ['다바4322', '34010', '87.0', '31.0', '주변부'],
         ['다바5011', '34020', '0.0', '0.0', '주변부']],
        ['필드명이 달라도 필드 콤보에서 선택 가능'],
    ),
    'table': (
        '시군구 인구 추계 테이블 (csv/xlsx) — 거주·근무 공통 형식',
        [('sgg_cd_h', '정수', '필수', '시군구 코드 — 격자와 같은 코드 체계, 중복 금지'),
         ('pop_YYYY', '숫자', '필수', '연도별 목표 총인구(거주 또는 근무) — 1개 이상, 연도 자동 감지'),
         ('sgg_nm', '문자', '선택', '시군구 명칭 (참고용)')],
        ['sgg_cd_h', 'sgg_nm', 'pop_2025', 'pop_2030', 'pop_2035', 'pop_2040'],
        [['34010', '천안시', '658000', '672000', '680000', '683000'],
         ['34020', '공주시', '103000', '99000', '95000', '91000']],
        ['격자에 있는 모든 시군구가 이 테이블에 있어야 함 (없으면 에러)',
         '컬럼명 고정 (sgg_cd_h, pop_YYYY) — 근무인구 추계도 같은 형식으로 변환해 사용'],
    ),
    'hub': (
        '정책관심구역 — 거점 격자 목록 (shp/gpkg)',
        [('gid', '문자', '필수', '거점 격자 ID — 격자 레이어의 gid와 매칭')],
        ['gid', 'type'],
        [['다바4321', '중심지Ⅱ'],
         ['다바5100', '중심지Ⅰ']],
        ['이 레이어에 있는 gid = 거점, 없으면 비거점 (gid 외 컬럼 무시)',
         '지오메트리 불필요 — 테이블만 있어도 됨',
         '2단계 페이지에서 새로 산출할 수도 있음'],
    ),
    'infra': (
        '생활인프라 충족도 레이어 (폴리곤 shp/gpkg)',
        [('GRID_1K_CD', '문자', '필수', '격자 ID — 격자 레이어의 gid와 매칭'),
         ('Me_CI_1', '숫자', '필수', '마을시설 충족도 (0~10)'),
         ('Me_CI_2', '숫자', '필수', '거점시설 충족도 (0~10)')],
        ['GRID_1K_CD', 'Me_CI_1', 'Me_CI_2'],
        [['다바4321', '4.2', '3.1'],
         ['다바4322', '1.0', '0.5']],
        ['국토모니터링 국토생활인프라(20종) 기준 필드명',
         '미지정 시 거점 유형 필터만 적용'],
    ),
    'emp': (
        '고용률 시계열 (csv/xlsx, long 형식)',
        [('1열: 지역', '문자', '필수', '시군구 이름 — 격자 sgg_nm과 일치해야 함'),
         ('2열: 연도', '정수', '필수', '관측 연도 — 지역당 2개 이상'),
         ('3열: 고용률', '숫자', '필수', '고용률(%)')],
        ['지역', '연도', '고용률(%)'],
        [['천안시', '2017', '62.3'],
         ['천안시', '2022', '64.1'],
         ['공주시', '2017', '62.8']],
        ['컬럼 이름 무관 — 첫 3개 컬럼을 지역/연도/값 순으로 해석',
         '지역별 최초~최후 연도로 연평균 증감률(CAGR) 산출',
         "'합계' 포함 행은 자동 제외"],
    ),
    'pop15': (
        '15세 이상 장래인구 (csv/xlsx, wide 형식)',
        [('1열: 지역', '문자', '필수', '시군구 이름 — 격자 sgg_nm과 일치'),
         ('연도 컬럼', '숫자', '필수', '연도별 15세 이상 인구 — 2개 이상. 이 연도들이 추계 구간·목표연도가 됨')],
        ['지역', '2022', '2025', '2030', '2035', '2040'],
        [['천안시', '590000', '601000', '615000', '622000', '624000'],
         ['공주시', '92000', '90000', '87000', '84000', '81000']],
        ['연도 컬럼명은 2030 / 2030년 / pop_2030 모두 인식',
         '취업자 수 = 장래 고용률 × 이 인구'],
    ),
    'alloc': (
        '배분결과 격자 (1~3단계 출력, 폴리곤 gpkg/shp)',
        [('gid', '문자', '필수', '격자 고유 ID — 거주·근무 결과 간 매칭 키'),
         ('pop_YYYY', '숫자', '필수', '연도별 배분 인구 — 거주·근무 공통 연도만 분석'),
         ('pop_r / pop_w', '숫자', '선택', '기준연도 인구 — 기준연도 분석에 사용')],
        ['gid', 'pop_r', 'pop_2025', 'pop_2030', 'pop_2035', 'pop_2040'],
        [['다바4321', '1532.4', '1601.2', '1688.0', '1702.3', '1710.9'],
         ['다바4322', '87.0', '85.1', '82.6', '80.0', '77.7']],
        ['1~3단계 배분 결과 파일을 그대로 사용 (어느 시나리오든 가능)',
         '거주·근무 결과는 같은 격자(gid 집합)여야 함'],
    ),
    'sp_grid': (
        '국토공간거점지도 격자 (1km 폴리곤 shp/gpkg) — 생활권 분석 공통',
        [('gid', '문자', '필수', '격자 고유 ID — OD·중심점과 매칭 키'),
         ('type', '문자', '필수', "거점 유형 — 후보그룹1 판정 (값: '중심지Ⅰ', '중심지Ⅱ')"),
         ('pop_r', '숫자', '필수', '거주인구(밀도) — 후보그룹2 판정·속성 집계·대표점 가중치'),
         ('pop_w', '숫자', '권장', '근무인구 — 속성 집계(wpop_*)'),
         ('pc_in / pc_out', '숫자', '선택', '유입/유출 중심성 — 속성 집계')],
        ['gid', 'type', 'pop_r', 'pop_w', 'pc_in', 'pc_out'],
        [['다바4321', '중심지Ⅱ', '1532.4', '2210.0', '0.82', '0.44'],
         ['다바4322', '주변부', '87.0', '31.0', '0.01', '0.02']],
        ['필드명이 달라도 각 프로세스 페이지의 필드 콤보에서 선택 가능'],
    ),
    'sp_emd': (
        '읍면동 경계 (폴리곤 shp/gpkg, 선택)',
        [('emd_nm', '문자', '필수', '읍면동 명칭 — 후보 폴리곤 이름 부여·중복 제거')],
        ['emd_nm'],
        [['천안시 동남구 신부동'], ['공주시 웅진동']],
        ['통계청 읍면동 경계(bnd_dong) 사용 시 명칭 필드는 ADM_NM — 필드 콤보에서 선택'],
    ),
    'sp_infra': (
        '생활인프라충족도 (500m 격자 폴리곤 shp/gpkg)',
        [('*_ox 열들', '숫자', '필수', '시설별 충족 여부(0/1) — 마을시설·거점시설 열을 '
                                   '① 페이지에서 복수 선택')],
        ['GRID_CD', 'kinder_ox', 'phar_ox', 'hosp_ox', 'lib_ox'],
        [['다바43211', '1', '1', '0', '0'],
         ['다바43212', '0', '1', '1', '1']],
        ['마을시설 예: 어린이집·약국 등 / 거점시설 예: 병원·도서관 등',
         'tfac_avg(전체시설 평균) ≥ 4.0 → 생활중심지, bfac_avg(거점시설 평균) ≥ 5.0 → 지역중심지 기본 규칙에 사용'],
    ),
    'sp_od': (
        '격자→중심지 OD 행렬 (CSV) — 완성본 입력',
        [('origin_id', '문자', '필수', '출발 격자 ID — 격자 gid와 매칭'),
         ('destination_id', '문자', '필수', '도착 중심지 격자 ID — 중심점 gid와 매칭'),
         ('total_cost', '숫자', '필수', '이동 비용(거리·시간) — 최소값 중심지로 할당')],
        ['origin_id', 'destination_id', 'total_cost'],
        [['다바4321', '다바5100', '4820.5'],
         ['다바4321', '다바6200', '12030.1']],
        ['격자 1개 × 중심지 N개 조합의 롱 형식', '컬럼명이 달라도 ③ 페이지 필드 콤보에서 선택',
         '⑤ 중심점과 짝이 맞아야 함 — 재생성은 ③ 격자-중심지 OD 행렬 산출(도로망 최단거리)'],
    ),
    'sp_pt': (
        '중심지 중심점 (포인트 shp/gpkg)',
        [('gid', '문자', '필수', '중심지 격자 ID — OD의 destination_id와 매칭'),
         ('cent_id', '문자/정수', '필수', '중심지(권역) ID — 기초생활권 구분 키'),
         ('cent_nm', '문자', '필수', '중심지 이름 — 지도 심볼 표기')],
        ['gid', 'cent_id', 'cent_nm'],
        [['다바5100', 'C01', '천안역세권'],
         ['다바6200', 'C02', '아산배방']],
        ['OD 행렬을 만들 때 쓴 중심점 파일과 동일해야 함', '② 중심지 중심점 생성으로 생성 가능'],
    ),
    'sp_net': (
        '도로망 (라인 shp/gpkg — KTDB GIS 도로망도)',
        [('ONEWAY', '문자', '필수', '일방통행 여부 — 도로망 최단거리 계산에 사용')],
        ['LINK_ID', 'ONEWAY', 'MAX_SPD'],
        [['1000001', 'FT', '80'], ['1000002', 'B', '60']],
        ['CRS는 EPSG:5179 권장', '③ 격자-중심지 OD, ⑤ 중심지 간 거리행렬, KPI 네트워크성에 사용'],
    ),
    'sp_trip': (
        '1km격자 통행 행렬 (CSV — KT 모바일 통근 OD)',
        [('o_GRID_1K_CD', '문자', '필수', '출발 격자 ID'),
         ('d_GRID_1K_CD', '문자', '필수', '도착 격자 ID'),
         ('VOL2', '숫자', '필수', '통행량 — 매력도의 유입·유출 통행 집계')],
        ['o_GRID_1K_CD', 'd_GRID_1K_CD', 'VOL2'],
        [['다바4321', '다바5100', '152.3'],
         ['다바5100', '다바4321', '98.1']],
        ['컬럼명이 달라도 ④ 페이지 필드 콤보에서 선택'],
    ),
    'sp_cls': (
        '중심지 위계 결과 (① 중심지 추출 및 위계 분류 출력 폴리곤)',
        [('cand_id', '문자/정수', '필수', '중심지 고유 ID'),
         ('cent_cls', '문자', '필수', '위계 (생활중심지/지역중심지/광역중심지/이외)'),
         ('rpop_sum·tfac_avg 등', '숫자', '자동', '속성 추출 단계가 만든 통계 필드')],
        ['cand_id', 'cent_cls', 'rpop_sum', 'wpop_sum', 'tfac_avg', 'bfac_avg'],
        [['1', '광역중심지', '683200', '412000', '6.2', '7.1'],
         ['2', '생활중심지', '12400', '3300', '4.4', '2.0']],
        ['① 실행 시 자동 연결 — 기존 결과 파일 직접 지정도 가능'],
    ),
    'sp_zone': (
        '기초생활권 결과 (④ 3차중심지 권역 도출 출력 격자 폴리곤)',
        [('gid', '문자', '필수', '격자 ID'),
         ('cent_id', '문자/정수', '필수', '소속 생활권(중심지) ID'),
         ('cent_nm', '문자', '필수', '소속 생활권 이름')],
        ['gid', 'cent_id', 'cent_nm'],
        [['다바4321', 'C01', '천안역세권'],
         ['다바4322', 'C01', '천안역세권']],
        ['③ 실행 시 자동 연결 — 기존 결과 파일 직접 지정도 가능'],
    ),
    'zone': (
        '개발사업 지구 경계 (폴리곤, 1행 = 1개발사업)',
        [('plan_pop', '숫자', '필수', '계획인구(명) — 0/NULL 행은 제외'),
         ('yr_done', '정수', '필수', '준공연도 (예: 2028)'),
         ('prj_pop', '숫자', '선택', '사업 전체 계획인구 합 — 복수 시군 분할 사업의 입주율 구간 판정용'),
         ('sgg_cd_h', '정수', '선택', '관할 시군구 코드 — 있으면 해당 시군 격자로만, 없으면 교차 시군 1/n')],
        ['plan_pop', 'yr_done', 'prj_pop', 'sgg_cd_h'],
        [['12500', '2027', '12500', '34010'],
         ['48000', '2030', '96000', '34020']],
        ['격자와 개발사업 지구 경계를 공간 교차하여, 개발사업 지구에 해당하는 격자를 판정 (좌표체계(CRS) 자동 변환)',
         '필드명이 달라도 필드 콤보에서 선택 가능'],
    ),
}

# 방식별 산식 설명 ([산식] 버튼에서 표시)
METHOD_SPECS = {
    'wproj': ('장래 근무인구 추계 — 앵커 기반 (충남 PDF 2장, 표 2-6·2-7)', '''\
① 고용률 연평균 증감률
   r = (고용률_마지막 / 고용률_최초)^(1/관측연수) − 1
② 장래 고용률
   고용률(y) = 고용률_마지막 × (1+r)^(y − 마지막 관측연도)
③ 장래 취업자 수
   취업자(y) = 고용률(y)/100 × 15세 이상 장래인구(y)
④ 취업자 구간별 CAGR
   g(t0→t1) = (취업자(t1)/취업자(t0))^(1/(t1−t0)) − 1
⑤ 근무인구 연쇄 적용
   앵커 = 격자 기준연도 근무인구(pop_w)의 시·군 합산 (자동 산출)
   근무인구(t1) = 근무인구(직전 시점) × (1+g)^적용연수
   예: 앵커 2023 → 2025는 (1+g_22-25)², 2025 → 2030은 (1+g_25-30)⁵'''),
    'policy': ('정책관심구역 산출 — 거점 유형 ∪ 생활인프라 충족도 합집합', '''\
정책관심구역 = T ∪ C
  T = 선택한 거점 유형(type)에 속하는 격자
  C = 마을시설 충족도(Me_CI_1) ≥ 기준값
      또는 거점시설 충족도(Me_CI_2) ≥ 기준값 인 격자
출력 = 합집합 격자만 (src 필드: TYPE / CI / TYPE+CI 선정 근거)
생활인프라 미지정 시 C 없음 → 유형 필터만'''),
    'bau': ('BAU 배분 — 기준인구 단순 비례 (가중치 고정)', '''\
pop(g, y) = P(s, y) × w(g) / Σ w(g′)   (g′ ∈ 시군구 s)
  w(g)    = 기준연도 격자인구 — 모든 연도 동일 (분포 패턴 고정)
  P(s, y) = 시군구 s의 y년 추계 총량
시군구 합계 = 추계 테이블 값 그대로 보존
가중치 합 0인 시군구 → 균등 배분 + 경고'''),
    'hub': ('거점유도 배분 — 거점/비거점 지수모형, 연도 연쇄', '''\
연도 오름차순으로 순차 배분. 각 연도 y에서:
  ratio(g) = 직전 시점 격자인구 / 시군구 평균  (최초는 기준연도 인구)
  w(g) = ratio^a  (거점,   ratio ≥ 1),  평균 상회 거점에 더 몰림
       = ratio^b  (거점,   ratio < 1),  평균 하회 거점에서 덜 빠짐
       = ratio^c  (비거점, ratio ≥ 1),  평균 상회 비거점에 덜 몰림
       = ratio^d  (비거점, ratio < 1),  평균 하회 비거점에서 더 빠짐'''),
    'plan': ('계획 반영 배분 — 개발/비개발 분리', '''\
① 사업별 계획인구를 KOTI 입주율로 준공연도 D부터 연도 분산
   10만명 이상: 30/20/20/20/10%  ·  5만~10만: 50/20/15/15%
   5만 미만: 75/15/10%   (최대잔여법 정수 보정 — 합계 = 계획인구)
   규모 구간은 사업 전체 인구합(prj_pop)이 있으면 그 값으로 판정
② 유입연도 y → y보다 큰 최소 목표연도에 반영 (예: 2028·2029 → 2030)
③ 개발격자 = 사업 경계와 교차(경계가 걸린)하는 모든 격자
   개발인구는 개발격자에 균등 배분
   관할 시군 지정 시 해당 시군 격자만, 미지정 시 교차 시군 1/n 분할
④ 비개발 인구 = P(s,y) − 개발(s,y) 를 직전 시점 격자인구 비율 기반 비례 배분
⑤ ③+④ 합산 결과가 다음 목표연도의 배분 가중치로 연쇄 적용
   (이전 연도 개발지구는 거주지화되어 비개발 배분에 포함)
[근무 원단위] 개발 유입 = 계획인구 × 시군별 근무인구 합계/주거인구 합계'''),
    'center': ('중심지 식별', '''\
연도별(기준연도 + 거주·근무 공통 pop_YYYY)로:
① 격자 분류 — 주거인구 r, 근무인구 w
   집적지1: r ≥ 1,500 AND w ≥ 900
   집적지2: 집적지1 아니고 r ≥ 300 AND w ≥ 150
   집적지3: 집적지1/2 아니고 r ≥ 300      나머지: 주변
② 병합 — 집적지1+2 묶어서, 집적지3 따로
   100m 외부 버퍼 → 유니온 → 100m 내부 버퍼로 폴리곤 생성
③ 집적지3 폴리곤 중 집적지1/2 폴리곤과 거리 1km 이내 제거
   (독립적으로 기능하는 소규모 저밀 집적지만 선별)
   선택 시: 읍면동별 중복 제거 — 교차 면적 최대 읍면동에 배정, 읍면동마다 주거인구 최대 1개
   선택 시: 중심지 간 최소 거리(기본 1,500m) — 주거인구 큰 순으로 남기고,
            남긴 폴리곤과 경계 간 직선거리가 기준 미만이면 상위 중심지의 영향권으로 보고 제거
④ 위계 구분 - 폴리곤 주거인구 밀도 및 수, 근무인구 밀도 및 수로 사용자가 설정한 기준으로 위계 구분'''),
    'wf_center': ('중심지 추출 및 위계 분류 — 후보 추출 → 속성 추출 → 위계 설정', '''\
① 후보 추출
   후보그룹1 = 국토공간거점지도의 거점 유형이 중심지Ⅰ·중심지Ⅱ인 격자
   후보그룹2 = 거주인구밀도 ≥ 임계값(기본 300)인 격자 (그룹1 1km 밖)
   각각 100m 버퍼 → 병합해 후보 폴리곤 생성
   선택 시: 읍면동별 중복 제거 — 읍면동마다 거주인구 합계 최대 후보 1개
   선택 시: 중심지 간 최소 거리(기본 1,500m) — 거주인구 합계 큰 순으로 남기고,
            남긴 후보와 경계 간 직선거리가 기준 미만이면 상위 중심지의 영향권으로 보고 제거
   선택 시: 읍면동을 토대로 중심지 이름 부여
② 속성 추출 (인구 통계)
   후보 폴리곤과 교차하는 격자를 공간 집계:
   rpop_*(거주인구)·wpop_*(근무인구)·pcin/pcout(중심성) 합계·평균·밀도
③ 위계 설정 (중심지 후보 속성을 토대로 사용자가 정의한 위계 기준에 따라 1~3차 중심지로 구분)'''),
    'wf_odgen': ('중심지 중심점 생성', '''\
중심지 추출 및 위계 분류 결과 폴리곤에 속한 1km 격자의 중심점 추출
각 점에 소속 중심지 ID·이름(cent_id·cent_nm) 부여 — ID 체계가 위계와 일치
한 격자가 여러 중심지에 걸치면 첫 중심지에 배정 (경고)
※ 격자→중심지 OD의 D에 해당하는 중심지는 이 중심점과 짝이 맞아야 함 — 격자-중심지 OD 파일을 공통 데이터 ④에 입력
   (재생성: ③ 격자-중심지 OD 행렬 산출)'''),
    'wf_basic': ('3차중심지 권역(기초생활권) - OD행렬의 최소 비용을 기준으로 격자를 기초생활권으로 할당한 후 스무딩하여 잡음 제거', '''\
① 3차중심지 권역(기초생활권) 할당
   격자별로 OD 행렬에서 비용(total_cost) 최소인 중심지 선택
   출발지 = 도착지(자기 권역)는 비용 0.1로 고정 → 자기 권역 우선
   격자에 중심지 아이디(cent_id), 중심지 이름(cent_nm) 부여
② 스무딩
   이웃 격자 최빈값으로 반복 치환(기본 8회)해 경계 잡음 완화
   랜덤 시드 고정 → 같은 시드는 같은 결과'''),
    'wf_upper': ('상위생활권 도출 - 중심지 폴리곤 대표점 추출 → 중심지 간 거리행렬 계산 → 하위생활권-중심지 간 통행량 기반 매력도를 산출하여 하위생활권을 상위생활권으로 할당', '''\
① 인구가중 대표점
   중심지 폴리곤별로 격자 거주인구 가중 무게중심 포인트 산출
② 중심지-중심지 거리행렬 (도로망 최단거리)
   기점 유형(기본: 최하위생활권 이상 모든 중심지 유형) → 종점 유형(기본: 최하위 상위생활권 이상 모든 중심지 유형) 조합별 거리
③ 매력도 기반 상위생활권 배정
   매력도 = (유입통행 + 유출통행) ÷ 거리(km)²
   하위생활권별로 매력도 최대 상위중심지에 배정
   자기 자신이 상위중심지인 생활권은 자기에게 배정
   배정 결과를 디졸브 → 상위생활권 경계 (+ 격자 레이어·매력도 CSV·이동량 CSV)'''),
    'devw': ('연도별 개발지구 장래 근무인구 산출', '''\
사업별로:
① 계획인구(거주)를 KOTI 입주율로 준공연도부터 연도 분산
② 유입연도 → 자신보다 큰 최소 목표연도에 반영 (목표연도 = 근무 추계 테이블)
③ 근무인구 = 유입 계획인구 × 관할 시군 원단위(근무인구 합계/주거인구 합계)
   관할 시군이 지정되지 않은 사업은 전체 원단위 적용
시군별·연도별 합산 → CSV, 로그에 연도별 증분·누적 합계 (표 2-9 형태)'''),
}

# 유형·계수 설명 ([설명 보기] 버튼에서 표시)
DESC_SPECS = {
    'hub': ('거점(정책관심구역)유도 배분 — 유형·계수 설명', '''\
a: 거점,   시군구 평균 인구 이상 지역
b: 거점,   시군구 평균 인구 미만 지역
c: 비거점, 시군구 평균 인구 이상 지역
d: 비거점, 시군구 평균 인구 미만 지역

a 계수: 클수록   거점 a로 인구 더 몰림
b 계수: 작을수록 거점 b에서 인구 덜 빠짐
c 계수: 작을수록 비거점 c로 인구 덜 몰림
d 계수: 클수록   비거점 d에서 인구 더 빠짐

a 계수 > c 계수 AND b 계수 < d 계수: 거점 우대'''),
}

# ── 고급 위계 설정: 조건 행 빌더 (순수 Qt — QGIS 빌더 크래시 회피) ──
_ADV_FIELDS = [
    ('주거인구 합 (rpop_sum)', 'rpop_sum'),
    ('주거인구 밀도 (rpop_den)', 'rpop_den'),
    ('주거인구 격자평균 (rpop_avg)', 'rpop_avg'),
    ('근무인구 합 (wpop_sum)', 'wpop_sum'),
    ('근무인구 밀도 (wpop_den)', 'wpop_den'),
    ('근무인구 격자평균 (wpop_avg)', 'wpop_avg'),
    ('유입중심성 격자평균 (pcin_avg)', 'pcin_avg'),
    ('유출중심성 격자평균 (pcout_avg)', 'pcout_avg'),
    ('인프라 전체시설 평균 (tfac_avg)', 'tfac_avg'),
    ('인프라 마을시설 평균 (vfac_avg)', 'vfac_avg'),
    ('인프라 거점시설 평균 (bfac_avg)', 'bfac_avg'),
]
_ADV_OPS = [('≥', '>='), ('>', '>'), ('≤', '<='), ('<', '<'),
            ('=', '='), ('≠', '!=')]


class AdvCondWidget(QWidget):
    """한 유형의 위계 판정 조건을 행 단위로 조립 — 행 사이 AND/OR, 앞에서부터 순차 결합."""

    def __init__(self, fields, rows):
        super().__init__()
        self._rows = []
        self._fields = list(fields) if fields else list(_ADV_FIELDS)
        # 저장된 조건의 필드가 목록에 없으면(재실행으로 사라짐) 표시용으로 추가
        known = {n for _l, n in self._fields}
        for _logic, f, _op, _v in rows:
            if f not in known:
                self._fields.append((f'(현재 결과에 없음) {f}', f))
                known.add(f)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._rows_lay = QVBoxLayout()
        self._rows_lay.setSpacing(4)
        lay.addLayout(self._rows_lay)

        btns = QHBoxLayout()
        add = QPushButton('+ 조건 행 추가')
        add.clicked.connect(lambda: (self._add_row(), self._refresh()))
        clear = QPushButton('비우기 (숫자 기준 사용)')
        clear.clicked.connect(self._clear_rows)
        btns.addWidget(add)
        btns.addWidget(clear)
        btns.addStretch()
        lay.addLayout(btns)

        self._preview = QLabel('')
        self._preview.setWordWrap(True)
        self._preview.setStyleSheet('color: gray')
        lay.addWidget(self._preview)

        for logic, field, op, val in rows:
            self._add_row(logic, field, op, val)
        self._refresh()

    def _add_row(self, logic='AND', field=None, op='>=', val=0.0):
        if field is None:
            field = self._fields[0][1]
        roww = QWidget()
        h = QHBoxLayout(roww)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)

        logic_cb = QComboBox()
        logic_cb.addItems(['AND', 'OR'])
        logic_cb.setCurrentText(logic if logic in ('AND', 'OR') else 'AND')
        logic_cb.setFixedWidth(64)

        field_cb = QComboBox()
        for label, name in self._fields:
            field_cb.addItem(label, name)
        idx = next((i for i, (_l, n) in enumerate(self._fields) if n == field), 0)
        field_cb.setCurrentIndex(idx)

        op_cb = QComboBox()
        for label, sym in _ADV_OPS:
            op_cb.addItem(label, sym)
        oidx = next((i for i, (_l, s) in enumerate(_ADV_OPS) if s == op), 0)
        op_cb.setCurrentIndex(oidx)
        op_cb.setFixedWidth(56)

        val_sp = QDoubleSpinBox()
        val_sp.setDecimals(3)
        val_sp.setRange(-1e9, 1e12)
        val_sp.setValue(val)
        val_sp.setMinimumWidth(110)

        rm = QPushButton('✕')
        rm.setFixedWidth(28)

        for wdg in (logic_cb, field_cb, op_cb, val_sp, rm):
            h.addWidget(wdg)

        row = {'w': roww, 'logic': logic_cb, 'field': field_cb,
               'op': op_cb, 'val': val_sp}
        rm.clicked.connect(lambda _=False, r=row: self._remove_row(r))
        logic_cb.currentIndexChanged.connect(self._refresh)
        field_cb.currentIndexChanged.connect(self._refresh)
        op_cb.currentIndexChanged.connect(self._refresh)
        val_sp.valueChanged.connect(self._refresh)

        self._rows.append(row)
        self._rows_lay.addWidget(roww)

    def _remove_row(self, row):
        self._rows.remove(row)
        row['w'].setParent(None)
        row['w'].deleteLater()
        self._refresh()

    def _clear_rows(self):
        for row in list(self._rows):
            self._remove_row(row)

    def _refresh(self):
        for i, row in enumerate(self._rows):
            row['logic'].setEnabled(i > 0)   # 첫 행은 결합 연산 없음
        self._preview.setText('조건식: ' + (self.expression() or '(없음 — 숫자 기준 사용)'))

    def result_rows(self):
        out = []
        for i, row in enumerate(self._rows):
            out.append((row['logic'].currentText() if i > 0 else None,
                        row['field'].currentData(),
                        row['op'].currentData(),
                        row['val'].value()))
        return out

    def expression(self):
        expr = ''
        for i, (logic, field, op, val) in enumerate(self.result_rows()):
            v = int(val) if float(val).is_integer() else val
            cond = f'"{field}" {op} {v}'
            expr = cond if i == 0 else f'({expr}) {logic} {cond}'
        return expr


class AdvAllDialog(QDialog):
    """전체 유형(1차·2차·3차중심지)의 고급 위계 조건을 한 창에서 설정."""

    _SECTIONS = [('1차중심지', 'TYPE_3_EXPR'),
                 ('2차중심지', 'TYPE_2_EXPR'),
                 ('3차중심지', 'TYPE_1_EXPR')]

    def __init__(self, parent, fields, prev_rows):
        super().__init__(parent)
        self.setWindowTitle('고급 위계 설정 — 전체 유형')
        self.setMinimumSize(660, 560)
        self.sections = {}

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            '유형별 판정 조건을 조립하세요. 행 사이 AND/OR는 앞에서부터 순차 결합: '
            '((조건1 AND 조건2) OR 조건3).\n'
            '조건이 비어 있는 유형은 3단계의 숫자 기준을 그대로 사용합니다.\n'
            '필드 목록은 2단계 속성 추출 결과에 실제로 있는 필드만 표시됩니다.'))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        ilay = QVBoxLayout(inner)
        for title, pname in self._SECTIONS:
            gb = QGroupBox(title)
            gl = QVBoxLayout(gb)
            w = AdvCondWidget(fields, prev_rows.get(pname, []))
            gl.addWidget(w)
            self.sections[pname] = w
            ilay.addWidget(gb)
        ilay.addStretch()
        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


MARGIN = 12       # 페이지 내용 여백
V_SPACE = 8       # 폼 행 간격
H_SPACE = 12      # 라벨-위젯 간격
BTN_H = 32        # 실행 버튼 높이


class _Feedback(QgsProcessingFeedback):
    """알고리즘 로그·진행률을 대화상자 하단 로그창으로 전달."""

    def __init__(self, log, bar):
        super().__init__()
        self._log = log
        self._bar = bar
        self.progressChanged.connect(self._on_progress)

    def _on_progress(self, p):
        self._bar.setValue(int(p))
        QApplication.processEvents()

    def pushInfo(self, info):
        self._log.appendPlainText(info)
        QApplication.processEvents()

    def pushWarning(self, warning):
        self._log.appendPlainText('[경고] ' + warning)
        QApplication.processEvents()

    def reportError(self, error, fatalError=False):
        self._log.appendPlainText('[오류] ' + error)
        QApplication.processEvents()


class PopAllocDialog(QDialog):

    # 고정 범례 구간 (전 단계·전 연도 공통)
    CLASS_BREAKS = ((0, 100), (100, 200), (200, 500),
                    (500, 1000), (1000, 5000), (5000, 10**9))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('KRIHS S-Sim')
        self.resize(860, 780)

        # 파일에서 로드한 레이어 캐시 {용도 키: QgsVectorLayer}
        self._layers = {}
        # 도메인별 공통 위젯 {도메인: 위젯}
        self.grid_file, self.gid_cb, self.sgg_cb, self.pop_cb = {}, {}, {}, {}
        self.table_file = {}
        self.bau_out = {}
        self.zone_file, self.z_pop_cb, self.z_yr_cb = {}, {}, {}
        self.z_prj_cb, self.z_sgg_cb = {}, {}
        self.plan_out, self.plan_csv = {}, {}

        # ── 레이어 속성창 스타일: 왼쪽 목록 + 오른쪽 페이지 ─
        self.stack = QStackedWidget()
        self.nav = QListWidget()
        self.nav.setFixedWidth(230)
        self.nav.setSpacing(1)

        def category(text):
            # 대분류 — 크고 굵게, 선택 불가
            it = QListWidgetItem(text)
            it.setFlags(Qt.NoItemFlags)
            f = it.font()
            f.setBold(True)
            f.setPointSize(f.pointSize() + 1)
            it.setFont(f)
            it.setBackground(self.palette().alternateBase())
            self.nav.addItem(it)

        def header(text):
            it = QListWidgetItem('  ' + text)
            it.setFlags(Qt.NoItemFlags)   # 선택 불가 소분류 제목
            f = it.font()
            f.setBold(True)
            it.setFont(f)
            self.nav.addItem(it)

        def entry(text, page):
            # 페이지가 세로로 길어도 잘리지 않게 스크롤 영역으로 감쌈
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.NoFrame)
            scroll.setWidget(page)
            idx = self.stack.addWidget(scroll)
            it = QListWidgetItem('      ' + text)
            it.setData(Qt.UserRole, idx)
            self.nav.addItem(it)
            return it

        self._nav = {}   # 단계 키 → 좌측 메뉴 항목 (완료 체크·자동 이동용)
        self._flow_refreshers = {}   # 플로우 키 → [(공통슬롯, 콤보들, 캐시)] (페이지 열 때 재갱신)
        self._region = None          # 적용된 지역 {name, folder, json, out_root}
        category('분석 지역 설정')
        first = entry('데이터팩 · 결과 폴더', self._page_region())
        self._nav['region'] = first
        category('장래인구 격자 배분 및 중심지 전망')
        header('거주인구 배분')
        entry('공통 데이터', self._page_common(RES))
        self._nav['res_bau'] = entry('① BAU 배분', self._page_bau(RES))
        self._nav['res_hub'] = entry('② 거점(정책관심구역) 유도 배분', self._page_hub())
        self._nav['res_plan'] = entry('③ 계획인구 반영 배분', self._page_plan(RES))
        header('근무인구 배분')
        self._nav['work_common'] = entry('공통 데이터', self._page_common(WORK))
        self._nav['work_proj'] = entry('장래 근무인구 추계 (시·군)', self._page_wproj())
        self._nav['work_bau'] = entry('① BAU 배분', self._page_bau(WORK))
        self._nav['work_plan'] = entry('② 계획인구 반영 배분', self._page_plan(WORK))
        header('장래 중심지 전망')
        self._nav['center'] = entry('장래 중심지 식별', self._page_center())
        self._spst_fw = []   # 통합 플로우 파일 위젯 (경로 기억용)
        category('중심지-생활권 분석')
        entry('공통 데이터', self._page_sp_common())
        for flow in self.SPST_FLOWS:
            it = entry(flow['menu'], self._page_flow(flow))
            self._nav[flow['key']] = it
            it.setData(Qt.UserRole + 1, flow['key'])   # 플로우 키(콤보 재갱신용)
        category('KPI 지표 계산')
        self._nav['kpi_common'] = entry('공통 데이터', self._page_kpi_common())
        self._nav['kpi_batch'] = entry('일괄 산출', self._page_kpi_batch())
        self._nav['kpi_basic'] = entry('① 기초통계 산출', self._page_kpi_basic())
        self._nav['kpi_compact'] = entry('② 콤팩트성 산출', self._page_kpi_compact())
        self._nav['kpi_net'] = entry('③ 네트워크성 산출', self._page_kpi_network())
        self._nav['kpi_life'] = entry('④ 생활편리성 산출', self._page_kpi_life())
        category('종합 리포트 생성')
        self._nav['kpi_report'] = entry('종합 리포트', self._page_kpi_report())
        category('시각화')
        self._nav['viz'] = entry('3D 시각화', self._page_viz())
        category('데이터 탐색')
        self._nav['explore'] = entry('기초 통계 탐색', self._page_explore())

        self.nav.currentItemChanged.connect(self._on_nav_changed)
        self.nav.setCurrentItem(first)

        # ── 로그 (기능 페이지 아래, 우측 영역) ──────────────
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMinimumHeight(40)   # 스플리터로 자유 조절
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setFixedHeight(20)

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(4)
        bl.addWidget(QLabel('실행 로그'))
        bl.addWidget(self._log)
        bl.addWidget(self._bar)

        splitter = QSplitter(Qt.Vertical)          # 로그 영역 크기 드래그 조절
        splitter.addWidget(self.stack)
        splitter.addWidget(bottom)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(10)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([520, 260])

        # 좌측 메뉴 + 초기화 버튼(하단)
        left = QWidget()
        left_l = QVBoxLayout(left)
        left_l.setContentsMargins(0, 0, 0, 0)
        left_l.setSpacing(6)
        left_l.addWidget(self.nav, 1)
        reset_btn = QPushButton('초기화 (경로·진행기록 비우기)')
        reset_btn.setToolTip('입력·출력 경로와 좌측 완료 표시(✓)를 모두 지우고 처음 상태로 되돌립니다.')
        reset_btn.clicked.connect(self._reset_session)
        left_l.addWidget(reset_btn)

        # 좌측 메뉴는 하단까지 전체 높이, 우측 = 페이지 + 로그
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        lay.addWidget(left)
        lay.addWidget(splitter, 1)

        self._bind_settings()   # 마지막 사용 경로 복원 + 변경 시 자동 저장
        self._region_restore()  # 지난번 지역 설정 복원(빈 칸만 채움)

    # ── 지역 설정 (S-Sim 데이터팩) ────────────────────────
    def _page_region(self):
        w = QWidget()
        form = self._form(w)
        form.addRow(self._title(
            '<b>분석 지역 설정</b><br>'
            '<span style="color:gray">입력 형식은 각 [형식] 버튼 참고</span>'))
        self.rg_pack = QgsFileWidget()
        self.rg_pack.setStorageMode(QgsFileWidget.GetDirectory)
        self.rg_region = QComboBox()
        self.rg_out = QgsFileWidget()
        self.rg_out.setStorageMode(QgsFileWidget.GetDirectory)
        for fw in (self.rg_pack, self.rg_out):
            fw.setMinimumHeight(28)
        g1 = QGroupBox('S-Sim 데이터팩')
        f1 = self._form(g1)
        f1.addRow('데이터팩 폴더', self._file_row(self.rg_pack, 'pack'))
        f1.addRow('지역', self.rg_region)
        form.addRow(g1)
        g2 = QGroupBox('분석 결과')
        f2 = self._form(g2)
        f2.addRow('결과 저장 폴더', self.rg_out)
        form.addRow(g2)
        form.addRow(self._button('지역 적용', lambda _=False: self._apply_region(overwrite=True)))
        self.rg_pack.fileChanged.connect(self._scan_regions)
        s = QSettings('KRIHS', 'S-Sim')
        self.rg_out.fileChanged.connect(lambda p, ss=s: ss.setValue('region_out', p))
        return w

    def _scan_regions(self, pack):
        """데이터팩 폴더 → 지역\\ 하위 폴더 중 region.json 이 있는 것을 목록에."""
        self.rg_region.blockSignals(True)
        self.rg_region.clear()
        base = os.path.join((pack or '').strip(), '지역')
        if os.path.isdir(base):
            for d in sorted(os.listdir(base)):
                jp = os.path.join(base, d, 'region.json')
                if not os.path.isfile(jp):
                    continue
                try:
                    with open(jp, encoding='utf-8') as fp:
                        js = json.load(fp)
                except (OSError, ValueError):
                    continue
                self.rg_region.addItem(js.get('name') or d,
                                       {'folder': os.path.join(base, d), 'json': js})
        self.rg_region.blockSignals(False)
        QSettings('KRIHS', 'S-Sim').setValue('region_pack', (pack or '').strip())
        # 결과 저장 폴더 기본값 = 데이터팩이 있는 위치 (S-Sim_데이터팩 옆에 S-Sim_분석결과(지역명) 생성)
        parent = os.path.dirname(os.path.normpath((pack or '').strip())) if (pack or '').strip() else ''
        if parent and os.path.isdir(parent):
            self.rg_out.setDefaultRoot(parent)          # 폴더 선택 창이 열리는 위치
            if not self.rg_out.filePath().strip():
                self.rg_out.setFilePath(parent)

    def _region_file(self, reg, key):
        """region.json data 항목 → 절대 경로 (없는 파일이면 '')."""
        v = reg['json'].get('data', {}).get(key)
        rel = v if isinstance(v, str) else (v or {}).get('path')
        if not rel:
            return ''
        p = os.path.normpath(os.path.join(reg['folder'], rel))
        return p if os.path.exists(p) else ''

    def _region_user_file(self, reg, key):
        """region.json user_inputs 항목(도로망·통행 행렬) → 절대 경로 (없으면 '')."""
        v = reg['json'].get('user_inputs', {}).get(key)
        rel = v.get('path') if isinstance(v, dict) else None
        if not rel:
            return ''
        p = os.path.normpath(os.path.join(reg['folder'], rel))
        return p if os.path.exists(p) else ''

    def _region_result_root(self):
        return (self._region or {}).get('out_root')

    def _apply_region(self, overwrite=True, quiet=False, add_group=True):
        """선택한 지역의 데이터팩 자료를 각 파트 공통 데이터 칸에 채우고 결과 폴더를 지역 폴더로."""
        reg = self.rg_region.currentData()
        out = self.rg_out.filePath().strip()
        if not reg or not out:
            if not quiet:
                QMessageBox.warning(self, '지역 설정', '데이터팩 폴더·지역·결과 저장 폴더를 지정하세요.')
            return
        js = reg['json']
        name = js.get('name') or os.path.basename(reg['folder'])
        out_root = os.path.join(out, f'S-Sim_분석결과({name})')   # 모든 파트의 결과가 이 폴더 아래에 저장
        try:
            if not quiet:      # 창을 열 때의 자동 복원(quiet)에서는 폴더를 만들지 않음 — 결과를 저장할 때 생김
                os.makedirs(out_root, exist_ok=True)
        except OSError as e:
            if not quiet:
                QMessageBox.critical(self, '지역 설정', f'결과 폴더를 만들 수 없습니다.\n{e}')
            return
        self._region = {'name': name, 'folder': reg['folder'], 'json': js, 'out_root': out_root}
        data = js.get('data', {})
        filled = []

        def put(fw, path, label):
            if fw is None or not path:
                return
            if overwrite or not fw.filePath().strip():
                fw.setFilePath(path)
                filled.append(label)

        def lyr(key, layer_key):
            p = self._region_file(reg, key)
            ln = (data.get(key) or {}).get(layer_key) if isinstance(data.get(key), dict) else None
            return f'{p}|layername={ln}' if p and ln else p

        grid = lyr('grid', 'layer')
        bnd = data.get('boundary') or {}
        sgg, emd = lyr('boundary', 'sgg_layer'), lyr('boundary', 'emd_layer')
        # 도로망·통행 행렬: 데이터팩 07_사용자자료 (없으면 해당 파트 공통 데이터에서 직접 지정)
        net = self._region_user_file(reg, 'network')
        trip = self._region_user_file(reg, 'trip')
        dev = self._region_file(reg, 'dev_projects')

        # 장래인구 격자 배분
        for d, nm in ((RES, '거주'), (WORK, '근무')):
            put(self.grid_file.get(d), grid, f'{nm}인구 배분 · 격자')
            put(self.zone_file.get(d), dev, f'{nm}인구 배분 · 개발사업')
        put(self.table_file.get(RES), self._region_file(reg, 'resid_proj'), '거주인구 배분 · 추계 테이블')
        put(getattr(self, 'ctr_emd_file', None), emd, '장래 중심지 식별 · 읍면동')
        put(getattr(self, 'infra_file', None), self._region_file(reg, 'infra_1km'), '거점 유도 · 생활인프라')
        put(getattr(self, 'emp_file', None), self._region_file(reg, 'emp_rate'), '근무인구 추계 · 고용률')
        put(getattr(self, 'pop15_file', None), self._region_file(reg, 'pop15_proj'), '근무인구 추계 · 15세 이상 인구')
        # 중심지-생활권 분석
        sc = getattr(self, '_sp_common', {}) or {}
        put(sc.get('grid'), grid, '중심지-생활권 · 격자')
        put(sc.get('emd'), emd, '중심지-생활권 · 읍면동')
        put(sc.get('infra'), lyr('infra_500m', 'layer'), '중심지-생활권 · 생활인프라')
        put(sc.get('network'), net, '중심지-생활권 · 도로망')
        put(sc.get('trip'), trip, '중심지-생활권 · 통행 행렬')
        # KPI
        self._kpi_setup()
        k = self._kpi
        if overwrite or k['region'].text().strip() in ('', '지역'):
            k['region'].setText(name)
            if js.get('base_year'):
                k['base_year'].setText(str(js['base_year']))
            for key, val in (('city_type', js.get('city_type')), ('trend_type', js.get('trend_type'))):
                i = k[key].findText(val or '')
                if i >= 0:
                    k[key].setCurrentIndex(i)
        put(k['grid'], grid, 'KPI · 격자')
        put(k['region_bnd'], sgg, 'KPI · 계획권역 시군구 경계')
        put(k['net'], net, 'KPI · 도로망')
        put(k['fac_neigh_dir'], self._region_file(reg, 'fac_neigh_dir'), 'KPI · 근린시설 폴더')
        put(k['fac_hub_dir'], self._region_file(reg, 'fac_hub_dir'), 'KPI · 거점시설 폴더')
        co2 = data.get('carbon') or {}
        put(k['co2_src'], self._region_file(reg, 'carbon'), 'KPI · 탄소 배출')
        if co2.get('join'):
            k['co2_join'].setCurrentText(co2['join'])
        if co2.get('value'):
            k['co2_value'].setCurrentText(co2['value'])
        # 3D 시각화 지명
        put(getattr(self, 'viz_emd', None), emd, '3D 시각화 · 읍면동')
        if getattr(self, 'viz_emd_name', None) is not None and bnd.get('emd_name'):
            self.viz_emd_name.setField(bnd['emd_name'])

        if overwrite:
            # 이전 지역의 산출 경로 비우기 → 새 결과 폴더 기준으로 다시 채워짐
            outs = [self.policy_out, self.hub_out, self.wproj_out, self.devw_csv,
                    self.ctr_out, self.ctr_csv, self.ctr_res_file, self.ctr_work_file,
                    self.viz_out, self.table_file.get(WORK),
                    k['out_html'], k['batch_dir'], k['rep_basic'], k['rep_compact'], k['rep_network'], k['rep_life'],
                    k['zones'], k['centers'], k['od'], k['scen_folder']]
            for d in (RES, WORK):
                outs += [self.bau_out.get(d), self.plan_out.get(d), self.plan_csv.get(d)]
            for fw in outs:
                if fw is not None:
                    fw.setFilePath('')
            if hasattr(self, '_center_outputs'):
                self._center_outputs.clear()
            # 이 지역 결과 폴더에 이미 있는 중심지-생활권 결과는 다시 연결 (이어서 작업)
            for slot, rel in (('center_cls', (OUT_SP_CTR, '중심지_위계.gpkg')),
                              ('center_pt', (OUT_SP_ZONE, '중심지_중심점.gpkg')),
                              ('od', (OUT_SP_ZONE, '격자_중심지_OD.csv')),
                              ('sub_zone', (OUT_SP_ZONE, '기초생활권_스무딩.gpkg'))):
                fw = sc.get(slot)
                if fw is None:
                    continue
                p = os.path.join(out_root, *rel)
                fw.setFilePath(p if os.path.exists(p) else '')
                if os.path.exists(p):
                    filled.append('기존 결과 · ' + rel[1])

        self._link_scen_folder()
        self._link_kpi_json()
        if add_group:
            self._add_admin_group(sgg, emd, js.get('unit', '시도'))
        s = QSettings('KRIHS', 'S-Sim')
        s.setValue('region_name', name)
        s.setValue('region_applied', True)
        msg = (f'[지역 설정] {name} 적용 — 결과 폴더: {out_root}\n'
               + '\n'.join(f'  · {x}' for x in filled))
        self._log.appendPlainText(msg)

    def _add_admin_group(self, sgg, emd, unit):
        """지도에 '행정경계' 그룹(시군구·읍면동)을 켠 상태로 추가."""
        prj = QgsProject.instance()
        root = prj.layerTreeRoot()
        old = root.findGroup('행정경계')
        if old is not None:
            root.removeChildNode(old)
        grp = root.addGroup('행정경계')
        for path, nm, on, color, width in ((sgg, '시군구', True, '#4b5563', '0.5'),
                                           (emd, '읍면동', True, '#9ca3af', '0.26')):
            if not path:
                continue
            lyr = QgsVectorLayer(path, nm, 'ogr')
            if not lyr.isValid():
                continue
            lyr.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple(
                {'color': '0,0,0,0', 'outline_color': color, 'outline_width': width})))
            prj.addMapLayer(lyr, False)
            grp.addLayer(lyr).setItemVisibilityChecked(on)

    def _region_restore(self):
        """지난번 지역 설정 복원 — 지정했던 칸을 되살리고, 적용했던 지역은 빈 칸만 다시 채움."""
        s = QSettings('KRIHS', 'S-Sim')
        v = s.value('region_out', '')
        if v and os.path.exists(v):
            self.rg_out.setFilePath(v)
        pack = s.value('region_pack', '')
        if pack and os.path.isdir(pack):
            self.rg_pack.setFilePath(pack)
            i = self.rg_region.findText(s.value('region_name', '') or '')
            if i >= 0:
                self.rg_region.setCurrentIndex(i)
            if s.value('region_applied', False) in (True, 'true'):
                self._apply_region(overwrite=False, quiet=True, add_group=False)

    # ── 경로 기억 (QSettings) ─────────────────────────────
    def _iter_file_widgets(self):
        yield 'res_grid', self.grid_file[RES]
        yield 'work_grid', self.grid_file[WORK]
        yield 'res_table', self.table_file[RES]
        yield 'work_table', self.table_file[WORK]
        yield 'hub', self.hub_file
        yield 'infra', self.infra_file
        yield 'policy_out', self.policy_out
        yield 'hub_out', self.hub_out
        yield 'emp', self.emp_file
        yield 'pop15', self.pop15_file
        yield 'wproj_out', self.wproj_out
        for d in (RES, WORK):
            yield f'{d}_zone', self.zone_file[d]
            yield f'{d}_bau_out', self.bau_out[d]
            yield f'{d}_plan_out', self.plan_out[d]
            yield f'{d}_plan_csv', self.plan_csv[d]
        yield 'devw_csv', self.devw_csv
        yield 'ctr_res', self.ctr_res_file
        yield 'ctr_work', self.ctr_work_file
        yield 'ctr_emd', self.ctr_emd_file
        yield 'ctr_out', self.ctr_out
        yield 'ctr_csv', self.ctr_csv
        for k, fw in self.viz_files.items():
            yield f'viz_{k}', fw
        yield 'viz_emd', self.viz_emd
        yield 'viz_out', self.viz_out
        yield 'explore_file', self.exp_file
        yield from self._spst_fw

    def _bind_settings(self):
        s = QSettings('KRIHS', 'S-Sim')
        for key, fw in self._iter_file_widgets():
            saved = s.value(key, '')
            if saved and os.path.exists(saved):
                fw.setFilePath(saved)
            fw.fileChanged.connect(lambda p, k=key, ss=s: ss.setValue(k, p))

    # ── 초기화: 경로·완료표시·캐시를 처음 상태로 ───────────
    def _reset_session(self):
        if QMessageBox.question(
                self, '초기화',
                '입력·출력 경로와 좌측 완료 표시(✓)를 모두 비우고\n'
                '처음 상태로 되돌립니다. 진행할까요?',
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        QSettings('KRIHS', 'S-Sim').clear()   # 저장된 경로 삭제
        self._region = None                                 # 지역 설정 해제 → 결과 폴더 기본 규칙
        for fw in (self.rg_pack, self.rg_out):
            fw.setFilePath('')
        for _key, fw in self._iter_file_widgets():     # 모든 파일 위젯 비우기
            try:
                fw.setFilePath('')
            except Exception:
                pass
        for fw in (getattr(self, '_kpi', None) or {}).values():   # KPI 공통 데이터·결과 칸
            if isinstance(fw, QgsFileWidget):
                fw.setFilePath('')
        for it in self._nav.values():                  # 완료 표시(✓) 제거
            t = it.text()
            if '✓' in t:
                it.setText(t.split('✓')[0].rstrip())
        self._layers.clear()                           # 로드 레이어 캐시
        if hasattr(self, '_center_outputs'):
            self._center_outputs.clear()
        self._log.appendPlainText('[초기화] 경로·완료 표시·캐시를 비웠습니다.')

    # ── 좌측 메뉴 전환 + 플로우 페이지 콤보 자동 재갱신 ────
    def _on_nav_changed(self, cur, _prev):
        if cur is None:
            return
        self.stack.setCurrentIndex(cur.data(Qt.UserRole))
        fkey = cur.data(Qt.UserRole + 1)
        if fkey:   # 플로우 페이지 → 현재 공통데이터로 필드 콤보 재갱신·자동선택
            for fw, combos, cache in self._flow_refreshers.get(fkey, []):
                path = fw.filePath().strip()
                if path:
                    self._flow_layer_changed(cache, path, combos)
        if cur in (self._nav.get('kpi_common'), self._nav.get('kpi_batch')):
            self._kpi_autolink()       # KPI 공통데이터·일괄 산출 진입 → 상류 결과·시나리오 폴더 자동연결
            self._link_scen_folder()
            self._kpi_ensure_layers()
            if cur is self._nav.get('kpi_batch'):
                self._refresh_batch_page()
        if cur is self._nav.get('center'):       # 장래 중심지 식별 진입 → 계획 반영 배분 결과 자동연결
            self._center_autolink()
        if cur is self._nav.get('kpi_report'):   # 종합 리포트 진입 → 일괄 산출 결과(JSON) 연결
            self._link_kpi_json()

    # 거주·근무인구 배분 결과 → 종합 리포트가 읽는 시나리오 폴더 양식
    #   시나리오별_장래인구\{BAU·유도정책·계획반영}\{지역명}_{유형}_{연도}.shp
    #   (주거인구 필드 pop_tot, 근무인구 필드 pop_w — 근무 배분 결과가 있을 때)
    _SCEN_SRC = (('BAU', 'bau', '배분_1_BAU.gpkg'), ('유도정책', 'hub', '배분_2_거점유도.gpkg'),
                 ('계획반영', 'plan', '배분_3_계획인구.gpkg'))
    # 시나리오별 근무인구 출처 — 근무는 거점유도 배분이 없으므로 유도정책 시나리오도 BAU 근무인구를 씀
    _SCEN_WORK_SRC = {'bau': ('bau', '배분_1_BAU.gpkg'), 'hub': ('bau', '배분_1_BAU.gpkg'),
                      'plan': ('plan', '배분_2_계획반영.gpkg')}

    def _link_scen_folder(self, force=False):
        """결과 폴더의 시나리오별_장래인구가 있으면 종합 리포트 칸에 연결 (파일 작업 없음)."""
        root = self._analysis_root(RES)
        if not root or not getattr(self, '_kpi', None):
            return
        scen_root = os.path.join(root, out_alloc(RES), '시나리오별_장래인구')
        fw = self._kpi['scen_folder']
        if os.path.isdir(scen_root) and fw.filePath().strip() != scen_root \
                and (force or not fw.filePath().strip()):
            fw.setFilePath(scen_root)
            self._log.appendPlainText(f'[자동연결] 종합 리포트 시나리오 폴더 ← {scen_root}')

    def _export_scenarios(self, only=None):
        """거주 배분 실행 직후 호출 — 그 결과(연도별 열 pop_YYYY)를 시나리오·연도별 파일로 내보내고 리포트 칸에 연결.

        only: 'bau'·'hub'·'plan' 중 방금 실행한 배분만 내보냄(None이면 있는 결과 전부).
        """
        root = self._analysis_root(RES)
        if not root:
            return
        from qgis.core import (QgsFeature, QgsField, QgsFields, QgsVectorFileWriter)
        from PyQt5.QtCore import QVariant
        name = (self._region or {}).get('name') or ''
        if not name and getattr(self, '_kpi', None):
            name = self._kpi['region'].text().strip()
        name = name or '지역'
        scen_root = os.path.join(root, out_alloc(RES), '시나리오별_장래인구')
        widgets = {'bau': self.bau_out.get(RES), 'hub': getattr(self, 'hub_out', None),
                   'plan': self.plan_out.get(RES)}
        made = []
        work_cache = {}

        def _work_values(wkey):
            """시나리오의 근무 배분 결과 → ({gid: {연도: 근무인구}}, 파일 수정시각) 또는 (None, 0)."""
            kind, wsuffix = self._SCEN_WORK_SRC[wkey]
            if kind in work_cache:
                return work_cache[kind]
            wfw = (self.bau_out if kind == 'bau' else self.plan_out).get(WORK)
            wsrc = wfw.filePath().strip() if wfw is not None else ''
            wroot = self._analysis_root(WORK) or root
            if not wsrc or not os.path.isfile(wsrc):
                wsrc = os.path.join(wroot, out_alloc(WORK), f'{WORK}{wsuffix}')
            vals, wmt = None, 0
            if os.path.isfile(wsrc):
                # OGR로 직접 읽고 바로 닫음 — QgsVectorLayer는 연결 풀이 파일을 붙잡아
                # 같은 세션에서 근무 배분을 다시 실행할 때 덮어쓰기가 실패함
                from osgeo import ogr
                ds = ogr.Open(wsrc)
                if ds is not None:
                    wlay = ds.GetLayer(0)
                    defn = wlay.GetLayerDefn()
                    wn = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
                    wyears = [n[4:] for n in wn if re.fullmatch(r'pop_\d{4}', n)]
                    if 'gid' in wn and wyears:
                        vals = {str(f.GetField('gid')): {y: f.GetField(f'pop_{y}') for y in wyears}
                                for f in wlay}
                        wmt = os.path.getmtime(wsrc)
                    wlay = None
                ds = None
            work_cache[kind] = (vals, wmt)
            return work_cache[kind]

        for label, wkey, suffix in self._SCEN_SRC:
            if only and wkey not in ((only,) if isinstance(only, str) else only):
                continue
            fw = widgets.get(wkey)
            src = fw.filePath().strip() if fw is not None else ''
            if not src or not os.path.isfile(src):
                src = os.path.join(root, out_alloc(RES), f'{RES}{suffix}')
            if not os.path.isfile(src):
                continue
            lyr = QgsVectorLayer(src, label, 'ogr')
            if not lyr.isValid():
                continue
            names = [f.name() for f in lyr.fields()]
            years = sorted(n[4:] for n in names if re.fullmatch(r'pop_\d{4}', n))
            if not years or 'gid' not in names:
                continue
            sdir = os.path.join(scen_root, label)
            outs = {y: os.path.join(sdir, f'{name}_{label}_{y}.shp') for y in years}
            wvals, wmt = _work_values(wkey)
            mt = max(os.path.getmtime(src), wmt)
            if all(os.path.isfile(p) and os.path.getmtime(p) >= mt for p in outs.values()):
                continue                                   # 이미 최신
            os.makedirs(sdir, exist_ok=True)
            keep = [n for n in ('gid', 'sgg_cd_h', 'sgg_nm', 'pop_r') if n in names]
            flds = QgsFields()
            for n in keep:
                flds.append(QgsField(lyr.fields().field(n)))
            flds.append(QgsField('pop_tot', QVariant.Double))
            if wvals is not None:
                flds.append(QgsField('pop_w', QVariant.Double))
            opt = QgsVectorFileWriter.SaveVectorOptions()
            opt.driverName = 'ESRI Shapefile'
            opt.fileEncoding = 'UTF-8'
            wr = {y: QgsVectorFileWriter.create(p, flds, lyr.wkbType(), lyr.crs(),
                                                QgsProject.instance().transformContext(), opt)
                  for y, p in outs.items()}
            for f in lyr.getFeatures():
                base = [f[n] for n in keep]
                wrow = wvals.get(str(f['gid']), {}) if wvals is not None else None
                for y in years:
                    nf = QgsFeature(flds)
                    nf.setGeometry(f.geometry())
                    nf.setAttributes(base + [f[f'pop_{y}']] + ([wrow.get(y)] if wrow is not None else []))
                    wr[y].addFeature(nf)
            del wr
            made.append(f'{label}({", ".join(years)}{" · 근무 포함" if wvals is not None else ""})')
        if made:
            self._log.appendPlainText(f'[시나리오] 장래인구 내보내기 → {scen_root}: ' + ' · '.join(made))
        self._link_scen_folder(force=bool(made))

    def _center_autolink(self):
        """장래 중심지 식별 입력(거주·근무 배분결과)에 계획 반영 배분 결과를 연결 — 빈 칸만 채움."""
        for d, fw, fname in ((RES, self.ctr_res_file, f'{RES}배분_3_계획인구.gpkg'),
                             (WORK, self.ctr_work_file, f'{WORK}배분_2_계획반영.gpkg')):
            if fw.filePath().strip():
                continue
            cands = [self.plan_out[d].filePath().strip()] if d in self.plan_out else []
            root = self._analysis_root(d)
            if root:
                cands.append(os.path.join(root, out_alloc(d), fname))
            for p in cands:
                if p and os.path.isfile(p):
                    fw.setFilePath(p)
                    self._log.appendPlainText(f'[자동연결] 장래 중심지 식별 ← {d}인구 계획 반영 배분 결과: {p}')
                    break

    def _load_kpi_centers(self, path):
        """중심지 레이어 로드 — 위계 필드 tier 우선, 없으면 cent_cls 자동선택."""
        cb = self._kpi['f_chier']
        lyr = self._load_layer('kpi_centers', path, [(cb, 'tier')])
        if lyr is not None and not cb.currentField():
            cb.setField('cent_cls')

    def _load_kpi_zones(self, path):
        """생활권 경계 로드 — 이름 필드 zone_nm(생활권 명칭) 우선, 없으면 upper_id → cent_nm."""
        cb = self._kpi['f_zone']
        lyr = self._load_layer('kpi_zones', path, [(cb, 'zone_nm')])
        if lyr is not None and not cb.currentField():
            cb.setField('upper_id')
        if lyr is not None and not cb.currentField():
            cb.setField('cent_nm')

    def _kpi_autolink(self):
        """중심지·생활권 분석 결과를 KPI 공통 데이터에 자동 연결(빈 칸만 채움)."""
        if getattr(self, '_kpi', None) is None:
            return
        k = self._kpi
        src = getattr(self, '_sp_common', {}) or {}

        def setw(wkey, path, name):
            w = k.get(wkey)
            if w is None or not path or w.filePath().strip():
                return
            w.setFilePath(path)
            self._log.appendPlainText(f'[자동연결] KPI 공통데이터 ← {name}: {path}')

        # 격자·도로망·격자중심지 OD ← 생활권 분석 공통데이터
        for wkey, skey, name in (('grid', 'grid', '격자(거점지도)'),
                                 ('net', 'network', '도로망'),
                                 ('od', 'od', '격자→중심지 OD')):
            fw = src.get(skey)
            setw(wkey, fw.filePath().strip() if fw else '', name)
        # 생활권 경계 ← 최상위 생활권(1차 중심지 권역) 우선 → 2차 → 기초생활권(sub_zone)
        gpath = src['grid'].filePath().strip() if src.get('grid') else ''
        roots = []
        if gpath:
            gr = self._unified_result_root(gpath)
            if gr:
                roots.append(gr)   # 통합 폴더 data\분석결과
            # 폴더 통일 이전(구) SP 산출 위치도 탐색
            roots.append(os.path.join(os.path.dirname(os.path.dirname(gpath)), '분석결과'))
        zpath = ''
        for r in roots:
            for fn in ('상위생활권_1차중심지권역.gpkg', '상위생활권_2차중심지권역.gpkg'):
                cand = os.path.join(r, OUT_SP_ZONE, fn)
                if os.path.exists(cand):
                    zpath = cand
                    break
            if zpath:
                break
        if not zpath and src.get('sub_zone'):
            zpath = src['sub_zone'].filePath().strip()
        setw('zones', zpath, '최상위 생활권(1차 중심지 권역)')
        # 중심지 폴리곤 ← 중심지-생활권 분석의 중심지 위계 결과(기준연도·현재) 우선
        #   (KPI 중심지·생활권 = 기준연도 현재 구조로 본다 — 사용자 결정 2026-09-17)
        cpath = ''
        if src.get('center_cls'):
            cpath = src['center_cls'].filePath().strip()
        if not cpath:
            vf = getattr(self, 'viz_files', {}).get('중심지_식별')
            if vf and vf.filePath().strip():
                cpath = vf.filePath().strip()
        setw('centers', cpath, '중심지 폴리곤(중심지 위계·기준연도)')

    # ── 단계 완료 표시·자동 이동 ──────────────────────────
    def _finish_step(self, done, nxt=None):
        it = self._nav.get(done)
        if it is not None and not it.text().endswith('✓'):
            it.setText(it.text() + '  ✓')
        nxt_it = self._nav.get(nxt)
        if nxt_it is not None:
            self.nav.setCurrentItem(nxt_it)

    def _set_step_check(self, key, on):
        """공통데이터에 결과물(중심점·OD)을 직접 넣으면 해당 단계 ✓, 지우면 해제."""
        it = self._nav.get(key)
        if it is None:
            return
        t = it.text()
        has = '✓' in t
        if on and not has:
            it.setText(t + '  ✓')
        elif not on and has:
            it.setText(t.split('✓')[0].rstrip())

    # ── 레이아웃 헬퍼 ─────────────────────────────────────
    def _action_row(self, method_key, text, slot, desc_key=None):
        """[산식 보기] (+ 선택적 [설명 보기]) + 강조 실행 버튼 한 줄."""
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 10, 0, 2)
        h.setSpacing(8)
        h.addWidget(self._method_button(method_key))
        if desc_key is not None:
            h.addWidget(self._desc_button(desc_key))
        btn = self._button(text, slot)
        h.addWidget(btn, 1)
        return row

    @staticmethod
    def _form(parent=None):
        form = QFormLayout(parent) if parent is not None else QFormLayout()
        form.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        form.setVerticalSpacing(V_SPACE)
        form.setHorizontalSpacing(H_SPACE)
        # 필드가 남는 폭을 전부 차지 — 입력 위젯이 오른쪽에 몰리는 문제 방지
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.DontWrapRows)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        return form

    @staticmethod
    def _title(text):
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        return lbl

    @staticmethod
    def _button(text, slot):
        btn = QPushButton(text)
        btn.setMinimumHeight(BTN_H)
        btn.setProperty('primary', True)
        btn.clicked.connect(slot)
        return btn

    @staticmethod
    def _open_widget(on_change):
        fw = QgsFileWidget()
        fw.setFilter(VECTOR_FILTER)
        fw.setMinimumHeight(28)
        fw.fileChanged.connect(on_change)
        return fw

    def _file_row(self, widget, spec_key):
        """파일 위젯 옆에 [형식] 버튼 — 필요 컬럼·샘플 안내창."""
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        h.addWidget(widget, 1)
        btn = QPushButton('형식')
        btn.setFixedWidth(48)
        btn.setToolTip('이 입력에 필요한 컬럼과 샘플 데이터 보기')
        btn.clicked.connect(lambda: self._show_spec(spec_key))
        h.addWidget(btn)
        return row

    def _method_button(self, key):
        btn = QPushButton('산식 보기')
        btn.setFixedWidth(90)
        btn.setToolTip('이 방식이 어떤 산식으로 작동하는지 보기')
        btn.clicked.connect(lambda: self._show_method(key))
        return btn

    def _desc_button(self, key):
        btn = QPushButton('설명 보기')
        btn.setFixedWidth(90)
        btn.setToolTip('유형·계수의 의미 설명 보기')
        btn.clicked.connect(lambda: self._show_desc(key))
        return btn

    @staticmethod
    def _esc(s):
        # RichText 팝업에서 본문의 <, >, & 가 태그로 해석돼 잘리는 것 방지
        return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    def _show_desc(self, key):
        title, body = DESC_SPECS[key]
        box = QMessageBox(self)
        box.setWindowTitle('설명')
        box.setTextFormat(Qt.RichText)
        box.setText(f'<b>{self._esc(title)}</b>'
                    f'<pre style="font-size:12px">{self._esc(body)}</pre>')
        box.exec_()

    _METHOD_WIN_TITLES = {'policy': '정책관심구역 산출 산식', 'devw': '산식',
                          'wf_center': '산식', 'wf_odgen': '산식',
                          'wf_basic': '산식', 'wf_upper': '산식',
                          'center': '중심지 식별 방법'}

    def _show_method(self, key):
        title, body = METHOD_SPECS[key]
        box = QMessageBox(self)
        box.setWindowTitle(self._METHOD_WIN_TITLES.get(key, '배분 방식 산식'))
        box.setTextFormat(Qt.RichText)
        box.setText(f'<b>{self._esc(title)}</b>'
                    f'<pre style="font-size:12px">{self._esc(body)}</pre>')
        box.exec_()

    def _show_spec(self, key):
        title, cols, s_head, s_rows, notes = DATA_SPECS[key]

        def table(head, rows):
            h = ''.join(f'<th bgcolor="#e8e8e8">{c}</th>' for c in head)
            b = ''.join('<tr>' + ''.join(f'<td>{c}</td>' for c in r) + '</tr>'
                        for r in rows)
            return (f'<table border="1" cellpadding="5" cellspacing="0">'
                    f'<tr>{h}</tr>{b}</table>')

        # 데이터팩은 컬럼이 아니라 폴더 구성을 보여줌
        sec1, head1, sec2 = (('폴더 구성', ['폴더·파일', '형식', '필수', '내용'], '예시')
                             if key == 'pack' else
                             ('컬럼 구성', ['컬럼', '타입', '필수', '설명'], '샘플 데이터'))
        html = (f'<b>{title}</b><br><br>'
                f'<b>{sec1}</b><br>'
                + table(head1, cols)
                + ((f'<br><br><b>{sec2}</b><br>' + table(s_head, s_rows)) if s_rows else '')
                + (('<br>' + '<br>'.join(f'※ {n}' for n in notes)) if notes else ''))
        box = QMessageBox(self)
        box.setWindowTitle('입력 데이터 형식')
        box.setTextFormat(Qt.RichText)
        box.setText(html)
        box.exec_()

    @staticmethod
    def _save_widget(filt):
        fw = QgsFileWidget()
        fw.setStorageMode(QgsFileWidget.SaveFile)
        fw.setFilter(filt)
        fw.setMinimumHeight(28)
        return fw

    def _load_layer(self, key, path, field_combos):
        """파일 경로 → 레이어 로드(캐시), 필드 콤보 갱신. 실패 시 None."""
        path = (path or '').strip()
        if not path:
            self._layers.pop(key, None)
            for cb, _ in field_combos:
                cb.setLayer(None)
            return None
        # 같은 키·같은 경로면 캐시 재사용 — 재로드하면 기존 레이어가 C++에서
        # 삭제되어, 그 레이어를 보던 다른 페이지의 필드 콤보가 비워진다
        cached = self._layers.get(key)
        if cached is not None and cached.source() == path:
            layer = cached
        else:
            layer = QgsVectorLayer(
                path, os.path.splitext(os.path.basename(path))[0], 'ogr')
            if not layer.isValid():
                QMessageBox.warning(self, '파일 오류',
                                    f'레이어를 열 수 없습니다:\n{path}')
                self._layers.pop(key, None)
                return None
            self._layers[key] = layer
        for cb, default in field_combos:
            cb.setLayer(layer)
            cb.setField(default)
        return layer

    def _load_od_field_combos(self, path):
        """OD 파일 컬럼명을 OD 필드 콤보 3개에 채운다."""
        k = getattr(self, '_kpi', None)
        if not k:
            return
        self._fill_field_combos(path, [
            (k['od_origin'], 'origin_id'),
            (k['od_dest'], 'destination_id'),
            (k['od_dist'], 'total_cost')])

    def _fill_field_combos(self, path, combos):
        """파일 컬럼명을 읽어 편집가능 콤보들을 채운다.
        대용량 CSV는 헤더 한 줄만 읽어 UI가 멈추지 않게 한다."""
        path = (path or '').strip()
        cols = []
        if path:
            ext = os.path.splitext(path)[1].lower()
            try:
                if ext in ('.csv', '.tsv', '.txt'):
                    import csv as _csv
                    for enc in ('utf-8-sig', 'cp949'):
                        try:
                            with open(path, 'r', encoding=enc, newline='') as f:
                                cols = next(_csv.reader(f))
                            break
                        except (UnicodeDecodeError, StopIteration):
                            continue
                else:
                    lyr = QgsVectorLayer(path, 'od_tmp', 'ogr')
                    if lyr.isValid():
                        cols = [fld.name() for fld in lyr.fields()]
            except Exception:
                cols = []
        cols = [c.strip() for c in cols if c and c.strip()]
        for cb, default in combos:
            prev = cb.currentText().strip()
            cb.blockSignals(True)
            cb.clear()
            if cols:
                cb.addItems(cols)
                if default in cols:
                    cb.setCurrentText(default)
                elif prev in cols:
                    cb.setCurrentText(prev)
                else:
                    cb.setCurrentIndex(0)
            else:
                cb.setCurrentText(prev or default)
            cb.blockSignals(False)

    # ── 페이지: 공통 데이터 (도메인별) ────────────────────
    def _page_common(self, d):
        w = QWidget()
        form = self._form(w)

        self.gid_cb[d] = QgsFieldComboBox()
        self.sgg_cb[d] = QgsFieldComboBox()
        self.pop_cb[d] = QgsFieldComboBox()
        self.pop_cb[d].setFilters(QgsFieldProxyModel.Numeric)
        self.grid_file[d] = self._open_widget(
            lambda p, dd=d: self._load_layer(f'{dd}_grid', p, self._grid_combos(dd)))

        self.table_file[d] = QgsFileWidget()
        self.table_file[d].setFilter('추계 테이블 (*.csv *.xlsx)')
        self.table_file[d].setMinimumHeight(28)
        if d == WORK:   # 추계 테이블 넣으면 장래 근무인구 추계 단계 ✓, 지우면 해제
            self.table_file[d].fileChanged.connect(
                lambda p: self._set_step_check('work_proj', bool(p.strip())))

        kind = '거주인구' if d == RES else '근무인구'
        base_field = 'pop_r(거주)' if d == RES else 'pop_w(근무)'
        if d == RES:
            table_note = '입력 형식은 각 [형식] 버튼 참고'
        else:
            table_note = ('추계 테이블이 없으면 장래 근무인구 추계를 실행하세요. '
                          '직접 넣으면 추계 단계가 자동 완료(✓)됩니다. 입력 형식은 [형식] 버튼 참고')
        form.addRow(self._title(
            f'<b>{kind} 배분 공통 입력</b><br>'
            f'<span style="color:gray">{table_note}</span>'))
        form.addRow('격자 파일 (shp/gpkg)', self._file_row(self.grid_file[d], 'grid'))
        form.addRow('  ↳ 격자 ID 필드', self.gid_cb[d])
        form.addRow('  ↳ 시군구 코드 필드', self.sgg_cb[d])
        form.addRow(f'  ↳ 기준연도 {kind} 필드', self.pop_cb[d])
        form.addRow(f'{kind} 추계 테이블 (csv/xlsx)',
                    self._file_row(self.table_file[d], 'table'))
        return w

    def _grid_combos(self, d):
        combos = [(self.gid_cb[d], 'gid'), (self.sgg_cb[d], 'sgg_cd_h'),
                  (self.pop_cb[d], 'pop_r' if d == RES else 'pop_w')]
        if d == WORK and hasattr(self, 'w_res_cb'):
            combos.append((self.w_res_cb, 'pop_r'))   # 원단위 분모 (계획반영 페이지)
        return combos

    # ── 페이지: 장래 근무인구 추계 (근무 전용) ────────────
    def _page_wproj(self):
        w = QWidget()
        form = self._form(w)
        form.addRow(self._title(
            '<b>시·군 장래 근무인구 추계</b><br>'
            '<span style="color:gray">거주인구 배분의 공통 데이터에서 격자 파일을 먼저 선택할 것 — '
            '생성된 추계 CSV는 공통 데이터에 자동 연결됨.<br>'
            '입력 형식은 [형식] 버튼, 계산 방식은 [산식 보기] 버튼 참고</span>'))

        self.emp_file = QgsFileWidget()
        self.pop15_file = QgsFileWidget()
        for fw in (self.emp_file, self.pop15_file):
            fw.setFilter('테이블 (*.csv *.xlsx)')
            fw.setMinimumHeight(28)
        form.addRow('고용률 시계열 (지역/연도/고용률)', self._file_row(self.emp_file, 'emp'))
        form.addRow('15세 이상 장래인구 (지역+연도 컬럼)',
                    self._file_row(self.pop15_file, 'pop15'))
        self.anchor_year = QDoubleSpinBox()
        self.anchor_year.setDecimals(0)
        self.anchor_year.setRange(1990, 2100)
        self.anchor_year.setValue(2023)
        form.addRow('앵커 기준연도 (격자 근무인구의 시점)', self.anchor_year)
        self.wproj_out = self._save_widget('CSV (*.csv)')
        form.addRow('추계 CSV 저장 파일', self.wproj_out)
        form.addRow(self._action_row('wproj', '장래 근무인구 추계 실행', self._run_wproj))
        return w

    def _run_wproj(self):
        from . import core
        grid = self._layers.get(f'{WORK}_grid')
        if grid is None:
            QMessageBox.warning(self, '입력 확인',
                                '근무인구 배분 > 공통 데이터에서 격자 파일을 먼저 선택하세요.')
            return
        if grid.fields().indexOf('sgg_nm') < 0:
            QMessageBox.warning(self, '입력 확인',
                                '격자에 sgg_nm(시군구 이름) 필드가 없어 지역 이름을 '
                                '코드로 변환할 수 없습니다.')
            return
        paths = {}
        for label, fw in (('고용률 시계열', self.emp_file),
                          ('15세 이상 장래인구', self.pop15_file)):
            p = fw.filePath().strip()
            if not p:
                QMessageBox.warning(self, '입력 확인', f'{label} 파일을 선택하세요.')
                return
            paths[label] = p

        self._log.appendPlainText('━' * 50)
        self._log.appendPlainText('[근무인구 추계] 장래 근무인구 추계')
        try:
            emp = core.load_long_table(paths['고용률 시계열'])
            pop15 = core.load_wide_year_table(paths['15세 이상 장래인구'])
            # 앵커 = 격자 근무인구(pop_w)의 시·군 합산 (PDF: 국토공간거점지도 합산)
            ay = int(self.anchor_year.value())
            pop_field = self.pop_cb[WORK].currentField() or 'pop_w'
            sums = {}
            for feat in grid.getFeatures():
                nm = str(feat['sgg_nm'] or '').strip()
                if not nm:
                    continue
                try:
                    sums[nm] = sums.get(nm, 0.0) + float(feat[pop_field])
                except (TypeError, ValueError):
                    pass
            anchors = {n: (ay, v) for n, v in sums.items()}
            self._log.appendPlainText(
                f'  앵커: 격자 {pop_field} 시·군 합산 ({ay}년, {len(anchors)}개 시군구)')
            proj, years = core.worker_projection(emp, pop15, anchors)
        except ValueError as e:
            self._log.appendPlainText(f'[오류] {e}')
            QMessageBox.critical(self, '추계 실패', str(e))
            return

        # 지역 이름 → 시군구 코드 (격자 sgg_nm 기준)
        sgg_field = self.sgg_cb[WORK].currentField() or 'sgg_cd_h'
        name_to_code = {}
        for feat in grid.getFeatures():
            nm = str(feat['sgg_nm'] or '').strip()
            if nm and nm not in name_to_code:
                try:
                    name_to_code[nm] = int(str(feat[sgg_field]).strip())
                except (TypeError, ValueError):
                    pass
        unmatched = sorted(set(proj) - set(name_to_code))
        if unmatched:
            QMessageBox.critical(self, '추계 실패',
                                 f'격자 sgg_nm에 없는 지역 이름: {", ".join(unmatched)}')
            return

        out = self._out_path(self.wproj_out, '추계_근무인구.csv', WORK)
        import csv as _csv
        with open(out, 'w', encoding='utf-8-sig', newline='') as f:
            wtr = _csv.writer(f)
            wtr.writerow(['sgg_cd_h', 'sgg_nm'] + [f'pop_{y}' for y in years])
            for name in sorted(proj, key=lambda n: name_to_code[n]):
                wtr.writerow([name_to_code[name], name]
                             + [round(proj[name][y], 1) for y in years])
        for name in sorted(proj, key=lambda n: name_to_code[n]):
            vals = ' / '.join(f'{y}: {proj[name][y]:,.0f}' for y in years)
            self._log.appendPlainText(f'  {name}: {vals}')
        self._log.appendPlainText(f'  → 추계 CSV 저장: {out}')
        self.table_file[WORK].setFilePath(out)   # 공통 데이터 추계 테이블로 자동 연결
        QMessageBox.information(self, '추계 완료',
                                f'{len(proj)}개 시군구 × {len(years)}개 연도 추계 완료.\n'
                                '공통 데이터의 추계 테이블로 자동 입력되었습니다.')
        self._finish_step('work_proj', 'work_bau')

    # ── 페이지: BAU (도메인별) ────────────────────────────
    # ── 페이지: 중심지 식별 (분석) ────────────────────────
    def _page_center(self):
        w = QWidget()
        form = self._form(w)

        def spin(default, maximum=10**9):
            sp = QDoubleSpinBox()
            sp.setDecimals(0)
            sp.setRange(0, maximum)
            sp.setValue(default)
            sp.setMinimumWidth(90)
            return sp

        def pair_row(*widgets):
            row = QHBoxLayout()
            row.setSpacing(8)
            for x in widgets:
                row.addWidget(QLabel(x[0]) if isinstance(x, tuple) else x)
            row.addStretch()
            return row

        form.addRow(self._title(
            '<b>장래 중심지 식별</b> — 거주·근무인구 배분 결과로 연도별 중심지 폴리곤과 위계 산출.<br>'
            '<span style="color:gray">입력 형식은 [형식] 버튼, 계산 방식은 '
            '[산식 보기] 버튼 참고</span>'))

        self.ctr_res_gid = QgsFieldComboBox()
        self.ctr_res_file = self._open_widget(
            lambda p: self._load_layer('ctr_res', p, [(self.ctr_res_gid, 'gid')]))
        form.addRow('거주인구 배분결과 파일', self._file_row(self.ctr_res_file, 'alloc'))
        form.addRow('  ↳ 격자 ID 필드', self.ctr_res_gid)
        self.ctr_work_gid = QgsFieldComboBox()
        self.ctr_work_file = self._open_widget(
            lambda p: self._load_layer('ctr_work', p, [(self.ctr_work_gid, 'gid')]))
        form.addRow('근무인구 배분결과 파일', self._file_row(self.ctr_work_file, 'alloc'))
        form.addRow('  ↳ 격자 ID 필드', self.ctr_work_gid)

        self.ctr_base = spin(2023, 2100)
        form.addRow('기준연도', pair_row(self.ctr_base))

        self.ctr_c1r, self.ctr_c1w = spin(1500), spin(900)
        self.ctr_c2r, self.ctr_c2w = spin(300), spin(150)
        self.ctr_c3r = spin(300)
        form.addRow('집적지1 기준(격자)', pair_row(('주거 ≥',), self.ctr_c1r,
                                          ('근무 ≥',), self.ctr_c1w))
        form.addRow('집적지2 기준(격자)', pair_row(('주거 ≥',), self.ctr_c2r,
                                          ('근무 ≥',), self.ctr_c2w))
        form.addRow('집적지3 기준(격자)', pair_row(('주거 ≥',), self.ctr_c3r))

        # 위계 4조건: 조건별 체크박스로 활성/비활성. 체크 시만 적용(미체크=무시).
        # 디폴트 = 전남광주 프리셋(주거밀도·주거인구·근무인구 활성, 근무밀도 비활성).
        def cond(label, default):
            cb = QCheckBox(label)
            sp = QDoubleSpinBox()
            sp.setDecimals(0); sp.setRange(0, 10**9)
            sp.setValue(default); sp.setMinimumWidth(80)
            cb.setChecked(default > 0)
            sp.setEnabled(cb.isChecked())
            cb.toggled.connect(sp.setEnabled)
            return cb, sp

        def cond_row(*pairs):
            row = QHBoxLayout(); row.setSpacing(6)
            for cb, sp in pairs:
                row.addWidget(cb); row.addWidget(sp)
            row.addStretch()
            return row

        self.ctr_t1d_u, self.ctr_t1d = cond('주거인구 밀도 ≥', 4500)
        self.ctr_t1p_u, self.ctr_t1p = cond('주거인구수 ≥', 50000)
        self.ctr_t1wd_u, self.ctr_t1wd = cond('근무인구 밀도 ≥', 0)
        self.ctr_t1wp_u, self.ctr_t1wp = cond('근무인구수 ≥', 50000)
        self.ctr_t2d_u, self.ctr_t2d = cond('주거인구 밀도 ≥', 300)
        self.ctr_t2p_u, self.ctr_t2p = cond('주거인구수 ≥', 5000)
        self.ctr_t2wd_u, self.ctr_t2wd = cond('근무인구 밀도 ≥', 0)
        self.ctr_t2wp_u, self.ctr_t2wp = cond('근무인구수 ≥', 5000)
        self.ctr_t3d_u, self.ctr_t3d = cond('주거인구 밀도 ≥', 300)
        self.ctr_t3wd_u, self.ctr_t3wd = cond('근무인구 밀도 ≥', 0)
        self.ctr_t3wp_u, self.ctr_t3wp = cond('근무인구수 ≥', 0)
        form.addRow('1차중심지 기준(중심지 폴리곤)', cond_row(
            (self.ctr_t1d_u, self.ctr_t1d), (self.ctr_t1p_u, self.ctr_t1p),
            (self.ctr_t1wd_u, self.ctr_t1wd), (self.ctr_t1wp_u, self.ctr_t1wp)))
        form.addRow('2차중심지 기준(중심지 폴리곤)', cond_row(
            (self.ctr_t2d_u, self.ctr_t2d), (self.ctr_t2p_u, self.ctr_t2p),
            (self.ctr_t2wd_u, self.ctr_t2wd), (self.ctr_t2wp_u, self.ctr_t2wp)))
        form.addRow('3차중심지 기준(중심지 폴리곤)', cond_row(
            (self.ctr_t3d_u, self.ctr_t3d),
            (self.ctr_t3wd_u, self.ctr_t3wd), (self.ctr_t3wp_u, self.ctr_t3wp)))
        form.addRow('', QLabel('<span style="color:gray">각 조건 체크 시 적용, 해제 시 무시. '
                               '조건은 AND 결합. 전남광주 프리셋 기본값.</span>'))

        # 후처리 (선택) — 중심지-생활권 분석 ① 후보 추출과 같은 규칙
        self.ctr_emd_file = self._open_widget(lambda p: None)
        form.addRow('읍면동 경계 (선택)', self._file_row(self.ctr_emd_file, 'sp_emd'))
        self.ctr_dedup = QCheckBox('읍면동별 중복 제거 (읍면동 내 주거인구 최대 중심지 1개만 유지)')
        self.ctr_dedup.setChecked(True)
        form.addRow(self.ctr_dedup)
        self.ctr_mind_u = QCheckBox('중심지 간 최소 거리 적용')
        self.ctr_mind_u.setChecked(True)
        self.ctr_mind = spin(1500, 100000)
        self.ctr_mind_u.toggled.connect(self.ctr_mind.setEnabled)
        form.addRow(pair_row(self.ctr_mind_u, self.ctr_mind, ('m (폴리곤 경계 간 직선거리 — 안쪽의 작은 중심지 제거)',)))

        self.ctr_out = self._save_widget('GeoPackage (*.gpkg)')
        form.addRow('중심지 폴리곤 저장 파일', self.ctr_out)
        self.ctr_csv = self._save_widget('CSV (*.csv)')
        form.addRow('중심지 요약 통계 CSV 저장 파일', self.ctr_csv)
        form.addRow(self._action_row('center', '장래 중심지 식별 실행', self._run_center))
        return w

    @staticmethod
    def _ctr_out_path(res_layer, widget, default_name):
        """빈 저장 경로 → 장래인구 격자 배분 및 중심지 전망\\장래 중심지 전망\\ (입력이 배분 결과 폴더 밖이면 입력 파일 옆)."""
        path = widget.filePath().strip()
        if not path:
            src = res_layer.source().split('|')[0]
            base = os.path.dirname(src) if os.path.isfile(src) else tempfile.gettempdir()
            if os.path.basename(base) in ('거주인구 배분', '근무인구 배분'):
                base = os.path.join(os.path.dirname(base), '장래 중심지 전망')
                os.makedirs(base, exist_ok=True)
            path = os.path.join(base, default_name)
            widget.setFilePath(path)
        return path

    def _run_center(self):
        res = self._layers.get('ctr_res')
        work = self._layers.get('ctr_work')
        if res is None or work is None:
            QMessageBox.warning(self, '입력 확인',
                                '거주·근무 배분결과 파일을 모두 선택하세요.')
            return
        cv = lambda sp, cb: sp.value() if cb.isChecked() else 0   # 미체크=조건 무시(0)
        params = {
            'RES_LAYER': res,
            'RES_GID': self.ctr_res_gid.currentField() or 'gid',
            'WORK_LAYER': work,
            'WORK_GID': self.ctr_work_gid.currentField() or 'gid',
            'BASE_YEAR': int(self.ctr_base.value()),
            'C1_RES': self.ctr_c1r.value(), 'C1_WORK': self.ctr_c1w.value(),
            'C2_RES': self.ctr_c2r.value(), 'C2_WORK': self.ctr_c2w.value(),
            'C3_RES': self.ctr_c3r.value(),
            'T1_DENS': cv(self.ctr_t1d, self.ctr_t1d_u), 'T1_POP': cv(self.ctr_t1p, self.ctr_t1p_u),
            'T1_WDENS': cv(self.ctr_t1wd, self.ctr_t1wd_u), 'T1_WPOP': cv(self.ctr_t1wp, self.ctr_t1wp_u),
            'T2_DENS': cv(self.ctr_t2d, self.ctr_t2d_u), 'T2_POP': cv(self.ctr_t2p, self.ctr_t2p_u),
            'T2_WDENS': cv(self.ctr_t2wd, self.ctr_t2wd_u), 'T2_WPOP': cv(self.ctr_t2wp, self.ctr_t2wp_u),
            'T3_DENS': cv(self.ctr_t3d, self.ctr_t3d_u),
            'T3_WDENS': cv(self.ctr_t3wd, self.ctr_t3wd_u), 'T3_WPOP': cv(self.ctr_t3wp, self.ctr_t3wp_u),
            'EMD_LAYER': self.ctr_emd_file.filePath().strip() or None,
            'DEDUP_BY_EMD': self.ctr_dedup.isChecked(),
            'MIN_DIST_APPLY': self.ctr_mind_u.isChecked(),
            'MIN_DIST': int(self.ctr_mind.value()),
            'OUTPUT': self._ctr_out_path(res, self.ctr_out, '중심지_식별.gpkg'),
            'OUTPUT_CSV': self._ctr_out_path(res, self.ctr_csv, '중심지_요약.csv'),
        }
        if self._run('krihs_ssim:center_identify', params):
            self._add_center_group(params['OUTPUT'], '중심지_식별')
            self.viz_files['중심지_식별'].setFilePath(params['OUTPUT'])
            self._log.appendPlainText(f'[분석] 요약 CSV: {params["OUTPUT_CSV"]}')
            self._finish_step('center', 'viz')

    # 위계별 채움색 (3D 시각화와 동일)
    TIER_COLORS = (('1차', '#d73027'), ('2차', '#ff7f00'),
                   ('3차', '#1a9850'), ('위계외', '#969696'))

    def _add_center_group(self, path, group_name):
        """중심지 결과를 그룹으로 추가 — 연도별 레이어 1개씩, 위계 색상.

        전 연도 같은 gpkg를 year 필터(subsetString)로 나눠 그림 — 파일 중복 없음.
        마지막 연도만 켠 상태로 추가.
        """
        base = QgsVectorLayer(path, group_name, 'ogr')
        if not base.isValid():
            self._log.appendPlainText(f'[경고] 결과 레이어 로드 실패: {path}')
            return None
        years = sorted({f['year'] for f in base.getFeatures()})
        if not years:
            self._log.appendPlainText(f'[경고] 중심지 피처 없음: {path}')
            return None

        cats = []
        for tier, color in self.TIER_COLORS:
            sym = QgsSymbol.defaultSymbol(base.geometryType())
            sym.setColor(QColor(color))
            cats.append(QgsRendererCategory(tier, sym, f'{tier} 중심지'))
        renderer = QgsCategorizedSymbolRenderer('tier', cats)

        root = QgsProject.instance().layerTreeRoot()
        old = root.findGroup(group_name)
        if old is not None:
            root.removeChildNode(old)
        group = root.insertGroup(0, group_name)
        for y in years:
            lyr = QgsVectorLayer(path, f'{y}년', 'ogr')
            lyr.setSubsetString(f'"year" = {y}')
            lyr.setRenderer(renderer.clone())
            QgsProject.instance().addMapLayer(lyr, False)
            node = group.addLayer(lyr)
            node.setItemVisibilityChecked(y == years[-1])
        self._log.appendPlainText(
            f'[스타일] {group_name} 그룹: 연도별 위계 지도 {len(years)}개 · 저장: {path}')
        return group

    # ── 생활권 분석 통합 플로우 (파편화된 도구를 3개 프로세스로 연쇄) ──
    # 각 플로우 = 기존 알고리즘 순차 실행. 로직 무수정, 대화상자에서 오케스트레이션.
    # 파일 입력은 '공통 데이터' 페이지(SP_COMMON)에 모으고, 플로우 페이지는
    # 파라미터(필드·임계값·유형)와 실행 버튼만 가진다.
    # expose: 페이지 노출 파라미터 / common: 공통 데이터 파일 연결
    # wire: 이전 단계 출력·공통 파일 자동 연결 / outs: 출력 → 자동 저장 파일명
    # load: 완료 후 지도에 올릴 출력 / after: 완료 시 공통 데이터에 자동 연결할 출력
    _DEST_TYPES = ('sink', 'vectorDestination', 'fileDestination', 'folderDestination')

    SP_COMMON = [
        ('grid', '① 국토공간거점지도 격자 (1km)', '전 프로세스 공통', 'sp_grid'),
        ('emd', '② 읍면동 경계 (선택)', '① 후보 이름 부여·중복 제거', 'sp_emd'),
        ('infra', '③ 생활인프라충족도 (500m 격자)', '① 속성 추출', 'sp_infra'),
        ('network', '④ 도로망 (KTDB, EPSG:5179)', '③ OD 산출·④ 거리행렬', 'sp_net'),
        ('trip', '⑤ 1km격자 통행 행렬 (CSV)', '⑤ 매력도 산출', 'sp_trip'),
        ('center_pt', '⑥ 중심지 중심점 (없으면 ② 단계 실행)', '없으면 ② 중심지 중심점 생성 실행 — 넣으면 ② 생략', 'sp_pt'),
        ('od', '⑦ 격자→중심지 OD 행렬 (없으면 ③ 단계 실행)', '없으면 ③ 격자-중심지 OD 산출 실행 — 넣으면 ③ 생략', 'sp_od'),
        ('center_cls', '결과 데이터', '① 실행 시 자동 연결 (직접 지정 가능)', 'sp_cls'),
        ('sub_zone', '기초생활권 결과', '④ 실행 시 자동 연결 (직접 지정 가능)', 'sp_zone'),
    ]

    # 가이드라인(안) 표4(주거)·표5(근무) — 도시위상별 1·2·3차 위계 참고치
    # 값: ((1차 밀도 명/㎢, 규모 명), (2차), (3차))
    # 포함비율: (1차 시·군청, 2차 읍, 3차 면 소재지 포함 %)
    _HIER_T4 = {
        100: {'거점': ((4000, 100000), (750, 5000), (100, 200)),
              '강소': ((1500, 20000), (500, 3000), (100, 200)),
              '자립': ((1250, 3000), (500, 500), (100, 200))},
        150: {'거점': ((4000, 100000), (750, 5000), (150, 150)),
              '강소': ((1500, 20000), (500, 3000), (150, 150)),
              '자립': ((1250, 3000), (500, 500), (150, 150))},
        200: {'거점': ((4000, 100000), (750, 5000), (200, 200)),
              '강소': ((1500, 20000), (500, 3000), (200, 200)),
              '자립': ((1250, 3000), (500, 500), (200, 200))},
        300: {'거점': ((3250, 100000), (750, 3000), (300, 300)),
              '강소': ((750, 30000), (750, 3000), (300, 300)),
              '자립': ((1000, 3000), (250, 700), (300, 300))},
    }
    _HIER_T4_RATIO = {
        100: {'거점': (91, 80, 86), '강소': (90, 81, 89), '자립': (92, 87, 77)},
        150: {'거점': (91, 80, 84), '강소': (90, 81, 91), '자립': (92, 87, 78)},
        200: {'거점': (91, 80, 76), '강소': (90, 81, 79), '자립': (92, 87, 61)},
        300: {'거점': (91, 80, 65), '강소': (90, 81, 61), '자립': (92, 87, 39)},
    }
    _HIER_T5 = {'거점': ((1750, 50000), (250, 2000), (100, 100)),
                '강소': ((750, 15000), (250, 1500), (100, 100)),
                '자립': ((750, 3000), (250, 300), (100, 100))}
    _HIER_T5_RATIO = {'거점': (91, 80, 89), '강소': (90, 81, 91), '자립': (92, 92, 75)}

    SPST_FLOWS = [
        dict(
            key='wf_center', nxt='wf_odgen', menu='① 중심지 추출 및 위계 분류',
            title='중심지 추출 및 위계 분류', sub=OUT_SP_CTR,
            stepwise=True,   # 단계별 실행 버튼 — 1단계 완료 → 2단계 활성 → 3단계·고급 활성
            desc='1단계 후보 추출 → 2단계 속성 집계 → 3단계 위계 설정을 '
                 '순서대로 실행. 앞 단계를 완료해야 다음 단계 버튼이 열림.',
            after={'center_cls': (2, 'OUTPUT')},
            steps=[
                dict(alg='extract_candidates', label='중심지 후보 추출',
                     common={'GEOJEOM_LAYER': 'grid', 'EMD_LAYER': 'emd'},
                     expose=['TYPE_FIELD', 'POP_FIELD', 'POP_THRESHOLD',
                             'EMD_NAME_FIELD', 'DEDUP_BY_EMD',
                             'MIN_DIST_APPLY', 'MIN_DIST'],
                     outs={'OUTPUT': '중심지후보_통합.gpkg',
                           'OUTPUT_GROUP1': '중심지후보_그룹1.gpkg',
                           'OUTPUT_GROUP2': '중심지후보_그룹2.gpkg'},
                     maps={'OUTPUT': (None, False)}),
                dict(alg='extract_center_attributes', label='후보 속성 추출',
                     common={'INFRA_LAYER': 'infra', 'GEOJEOM_LAYER': 'grid'},
                     attrsel=True,   # 집계 항목 체크박스 — _attrs_param_overrides
                     expose=['VILLAGE_COLS', 'BASE_COLS'],
                     wire={'CENTER_LAYER': ('out', 0, 'OUTPUT')},
                     outs={'OUTPUT': '중심지_속성.shp'},
                     maps={'OUTPUT': (None, False)}),
                dict(alg='classify_centers', label='중심지 및 위계 설정',
                     degurba=True,   # 중심지 식별과 같은 밀도·인구 임계값 입력 UI
                     expose=[],
                     wire={'INPUT': ('out', 1, 'OUTPUT')},
                     outs={'OUTPUT': '중심지_위계.gpkg'},
                     maps={'OUTPUT': ('tier', True)}),
            ]),
        dict(
            key='wf_odgen', nxt='wf_gridod', menu='② 중심지 중심점 생성',
            title='중심지 중심점 생성', sub=OUT_SP_ZONE,
            desc='① 중심지 추출 및 위계 분류의 결과 폴리곤에 속한 격자의 중심점을 만들어 '
                 '공통 데이터 ⑤에 자동 연결. 격자→중심지 OD는 이 중심점을 이용해 '
                 '③ 격자-중심지 OD 행렬 산출 탭에서 만들거나, 완성본을 공통 데이터 ④에 직접 입력.',
            after={'center_pt': (0, 'OUTPUT')},
            steps=[
                dict(alg='center_grid_points', label='중심지 중심점 생성',
                     header='중심지 ID',
                     common={'CENTER_LAYER': 'center_cls', 'GRID_LAYER': 'grid'},
                     expose=['CENT_ID_FIELD', 'CENT_NM_FIELD'],
                     outs={'OUTPUT': '중심지_중심점.gpkg'},
                     maps={'OUTPUT': (None, True)}),
            ]),
        dict(
            key='wf_gridod', nxt='wf_basic', menu='③ 격자-중심지 OD 행렬 산출',
            title='격자-중심지 OD 행렬 산출', sub=OUT_SP_ZONE,
            desc='격자→중심지 도로 최단거리 OD 행렬을 새로 산출(OD가 없거나 재생성 시). '
                 'scipy 다중출발 + 단방향(ONEWAY) → 도달불가 셀만 양방향 폴백(직선거리 없음). '
                 '산출 CSV는 공통 데이터 ④(격자→중심지 OD)에 자동 연결.',
            after={'od': (0, 'OUTPUT')},
            steps=[
                dict(alg='grid_center_od_scipy_graph', label='격자-중심지 OD 행렬 산출',
                     common={'GRID': 'grid', 'CENTER': 'center_pt', 'NETWORK': 'network'},
                     expose=['GRID_ID', 'CENTER_ID', 'ONEWAY_FIELD'],
                     outs={'OUTPUT': '격자_중심지_OD.csv'}),
            ]),
        dict(
            key='wf_basic', nxt='wf_upper', menu='④ 3차중심지 권역 도출',
            title='3차중심지 권역(기초생활권) 도출', sub=OUT_SP_ZONE,
            desc='격자→중심지 OD 행렬(CSV)를 토대로 격자를 3차중심지 권역으로 할당 → '
                 '스무딩(권역할당 시 발생한 잡음 제거 절차)을 한 번에 실행. '
                 'OD·중심점은 ②에서 산출하거나 완성본을 공통 데이터에 직접 입력.',
            after={'sub_zone': (1, 'OUTPUT')},
            steps=[
                dict(alg='od_matrix_region_assign', label='3차중심지 권역(기초생활권)',
                     common={'INPUT_OD': 'od', 'INPUT_GRID': 'grid',
                             'INPUT_CENTER': 'center_pt'},
                     expose=['FIELD_ORIGIN', 'FIELD_DEST', 'FIELD_COST',
                             'FIELD_GRID_GID', 'FIELD_CENTER_GID',
                             'FIELD_CENT_ID', 'FIELD_CENT_NM'],
                     outs={'OUTPUT': '기초생활권_할당.gpkg'},
                     maps={'OUTPUT': ('cent_nm', False)}),
                dict(alg='region_smoothing', label='3차중심지 권역(기초생활권) 스무딩',
                     expose=['N_ITERATIONS', 'RANDOM_SEED'],
                     wire={'INPUT_LAYER': ('out', 0, 'OUTPUT')},
                     outs={'OUTPUT': '기초생활권_스무딩.gpkg',
                           'OUTPUT_DISSOLVE': '기초생활권_권역.gpkg'},
                     maps={'OUTPUT': ('cent_nm', True),
                           'OUTPUT_DISSOLVE': ('cent_nm', False)}),
            ]),
        dict(
            key='wf_upper', nxt='kpi_common', menu='⑤ 상위생활권 도출',   # 끝나면 KPI 공통 데이터로
            title='상위생활권 도출', sub=OUT_SP_ZONE,
            desc='위계(tier) 기준 자동 재귀 — 버튼만 누르면 됨(값 선택 불필요). '
                 '기초생활권(3차)에서 tier로: 패스A 상위=tier 2·1(2차+1차) → 2차중심지 권역, '
                 '패스B 상위=tier 1 → 1차중심지 권역을 한 번에 각각 저장(파일명 분리로 덮어쓰기 없음). '
                 '통행량은 패스마다 현재 존 단위로 자동 재집계. '
                 '통행량은 패스마다 현재 존 단위로 자동 재집계. '
                 '(중심지 추출 및 위계 분류 출력의 tier 필드 사용 — 3위계 → 2개 상위생활권.)',
            steps=[
                dict(alg='pop_weighted_centroid', label='중심지 폴리곤 인구가중 대표점 추출',
                     common={'INPUT': 'center_cls', 'GEOJEOM_LAYER': 'grid'},
                     expose=[],
                     outs={'OUTPUT': '중심지_대표점.gpkg'},
                     maps={'OUTPUT': (None, False)}),
                dict(alg='od_matrix_road_distance_scipy',
                     label='중심지-중심지 거리행렬 (scipy, tier 전체↔상위)',
                     common={'NETWORK_LAYER': 'network'},
                     expose=[],
                     wire={'INPUT': ('out', 0, 'OUTPUT')},
                     preset={'TYPE_FIELD': 'tier', 'FROM_TYPE': '3차중심지,2차중심지,1차중심지',
                             'TO_TYPE': '2차중심지,1차중심지'},
                     outs={'OUTPUT': '중심지_거리행렬.gpkg'},
                     maps={'OUTPUT': (None, False)}),
                dict(alg='attractiveness_upper_zone',
                     label='패스A · 매력도 → 2차중심지 권역 (자동: 상위 tier 2·1)',
                     common={'UPPER_LAYER': 'center_cls', 'SUB_LAYER': 'sub_zone',
                             'CSV_LAYER': 'trip'},
                     expose=['CSV_FROM_FIELD', 'CSV_TO_FIELD', 'CSV_TRIP_FIELD'],
                     wire={'DIST_LAYER': ('out', 1, 'OUTPUT')},
                     preset={'UPPER_TYPE_FIELD': 'tier', 'UPPER_TYPES': '2차중심지,1차중심지',
                             'ZONE_LABEL_PREFIX': '2차 권역', 'NAME_BY_CITY': False},
                     outs={'OUTPUT_DISSOLVE': '상위생활권_2차중심지권역.gpkg',
                           'OUTPUT_GRID': '상위생활권_2차_격자.gpkg',
                           'OUTPUT_SCORE_CSV': '매력도_점수_2차.csv',
                           'OUTPUT_MOBILITY_CSV': '이동량_2차.csv'},
                     maps={'OUTPUT_DISSOLVE': ('zone_nm', False),
                           'OUTPUT_GRID': ('zone_nm', False)}),
                dict(alg='attractiveness_upper_zone',
                     label='패스B · 매력도 → 1차중심지 권역 (자동: 상위 tier 1)',
                     common={'UPPER_LAYER': 'center_cls', 'CSV_LAYER': 'trip'},
                     expose=['NAME_BY_CITY'],   # 최상위(1차) 생활권만 명칭 선택
                     wire={'DIST_LAYER': ('out', 1, 'OUTPUT'),
                           'SUB_LAYER': ('out', 2, 'OUTPUT_GRID')},
                     preset={'UPPER_TYPE_FIELD': 'tier', 'UPPER_TYPES': '1차중심지',
                             'SUB_ZONE_ID_FIELD': 'upper_id', 'SUB_GRID_ID_FIELD': 'gid',
                             'ZONE_LABEL_PREFIX': '생활권'},
                     outs={'OUTPUT_DISSOLVE': '상위생활권_1차중심지권역.gpkg',
                           'OUTPUT_GRID': '상위생활권_1차_격자.gpkg',
                           'OUTPUT_SCORE_CSV': '매력도_점수_1차.csv',
                           'OUTPUT_MOBILITY_CSV': '이동량_1차.csv'},
                     maps={'OUTPUT_DISSOLVE': ('zone_nm', True),
                           'OUTPUT_GRID': ('zone_nm', False)}),
            ]),
    ]

    _FLOW_FILTER = ('벡터/테이블 (*.gpkg *.shp *.csv);;GeoPackage (*.gpkg);;'
                    'Shapefile (*.shp);;CSV (*.csv);;모든 파일 (*.*)')

    def _flow_alg(self, name):
        return QgsApp.processingRegistry().algorithmById(f'krihs_ssim:{name}')

    # ── 생활권 분석 공통 데이터 페이지 ────────────────────
    def _page_sp_common(self):
        self._sp_common = {}
        page = QWidget()
        form = self._form(page)
        form.addRow(self._title(
            '<b>중심지-생활권 분석 공통 입력</b> — 하위 기능이 공통적으로 쓰는 원천 파일.<br>'
            '<span style="color:gray">⑥ 중심지 중심점·⑦ 격자-중심지 OD는 없으면 ②·③ 단계를 실행해 만드세요. '
            '직접 넣으면 그 단계는 자동 완료(✓) 처리되어 생략 가능합니다.</span>'))
        # 중심점·OD 슬롯에 파일을 넣으면 해당 단계(②·③)를 완료 표시, 지우면 해제
        _slot_step = {'center_pt': 'wf_odgen', 'od': 'wf_gridod'}
        for key, label, note, spec in self.SP_COMMON:
            fw = QgsFileWidget()
            fw.setFilter(self._FLOW_FILTER)
            fw.setMinimumHeight(28)
            fw.setToolTip(note)
            self._sp_common[key] = fw
            self._spst_fw.append((f'spc_{key}', fw))
            if key in _slot_step:
                fw.fileChanged.connect(
                    lambda p, k=_slot_step[key]: self._set_step_check(k, bool(p.strip())))
            if key in ('center_cls', 'sub_zone'):
                continue   # 결과 슬롯 — ③ 상위생활권 페이지에 표시
            form.addRow(label, self._file_row(fw, spec))
        return page

    # ── 플로우 페이지 (파라미터 + 실행) ───────────────────
    def _page_flow(self, flow):
        if not hasattr(self, '_flow_widgets'):
            self._flow_widgets = {}
        widgets = {}
        self._flow_widgets[flow['key']] = widgets

        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        lay.setSpacing(12)
        lay.addWidget(self._title(
            f'<b>{flow["title"]}</b><br>'
            f'<span style="color:gray">{flow["desc"]}<br>'
            f'입력 파일은 공통 데이터 페이지에서 선택. 산출물은 격자 폴더와 같은 '
            f'위계의 분석결과\\{flow["sub"]}\\ 에 자동 저장</span>'))

        # 이전 단계 결과 슬롯 — 소비하는 첫 페이지에 표시 (실행 시 자동 연결,
        # 기존 파일 직접 지정도 가능). 위계는 ②, 기초생활권은 ④.
        _RESULT_SLOTS = {'wf_odgen': ('center_cls', '① 실행 시 자동 연결',
                                      '중심지 추출 및 위계 분류 결과'),
                         'wf_upper': ('sub_zone', '④ 실행 시 자동 연결',
                                      '3차중심지 권역(기초생활권) 추출 결과')}
        if flow['key'] in _RESULT_SLOTS:
            skey, note, box_title = _RESULT_SLOTS[flow['key']]
            box = QGroupBox(f'{box_title} ({note})')
            form = self._form(box)
            for key, label, _note, spec in self.SP_COMMON:
                if key == skey:
                    form.addRow(label, self._file_row(self._sp_common[key], spec))
            lay.addWidget(box)

        if flow.get('stepwise'):
            self._center_outputs = {}      # (단계, 출력) → 경로 (단계별 실행 공유)
            self._center_step_btns = {}
        for si, step in enumerate(flow['steps']):
            alg = self._flow_alg(step['alg'])
            if len(flow['steps']) == 1:
                box = QGroupBox(step.get('header', step['label']))
            else:
                box = QGroupBox(f'{si + 1}단계. {step["label"]}')
            form = self._form(box)
            if alg is None:
                form.addRow(QLabel(f'알고리즘 로드 실패: {step["alg"]}'))
                lay.addWidget(box)
                continue
            pdefs = {p.name(): p for p in alg.parameterDefinitions()}
            children = {}   # 부모 파라미터명 → [(콤보, 기본값, 다중여부)]
            if step.get('attrsel'):
                # 집계 항목 선택 — 체크한 것만 통계 산출
                self._attr_checks = {}
                for key, clabel, default in [
                    ('res', '주거인구 (합·밀도·격자평균)', True),
                    ('wor', '근무인구 (합·밀도·격자평균)', True),
                    ('pcin', '유입중심성 (격자 평균)', True),
                    ('pcout', '유출중심성 (격자 평균)', True),
                    ('tfac', '생활인프라 전체시설 평균 (선택한 열 전체)', False),
                    ('vfac', '생활인프라 마을시설 평균', False),
                    ('bfac', '생활인프라 거점시설 평균', False),
                ]:
                    cb = QCheckBox(clabel)
                    cb.setChecked(default)
                    self._attr_checks[key] = cb
                    form.addRow(cb)
            if step.get('degurba'):
                # 중심지 식별 페이지와 같은 방식의 임계값 입력
                # (실행 시 QGIS 표현식으로 변환 — _run_flow_steps 참고)
                def _dsp(v):
                    s = QDoubleSpinBox()
                    s.setDecimals(0)
                    s.setRange(0, 10**9)
                    s.setValue(v)
                    s.setMinimumWidth(90)
                    return s
                self._degurba_adv = {}    # 파라미터명 → {'rows': [...], 'expr': str}
                def _dcond(label, field, default):
                    cb = QCheckBox(label); sp = _dsp(default)
                    cb.setChecked(default > 0); sp.setEnabled(cb.isChecked())
                    cb.toggled.connect(sp.setEnabled)
                    return (field, cb, sp)
                def _drow(label, conds):
                    row = QHBoxLayout(); row.setSpacing(6)
                    for _f, cb, sp in conds:
                        row.addWidget(cb); row.addWidget(sp)
                    row.addStretch(); form.addRow(label, row)
                # 조건별 체크박스 4조건(주거밀도·주거인구수·근무밀도·근무인구수). 체크 시만 적용.
                self._degurba_conds = {
                    # 기본값 = 가이드라인 표4(최소 150명/㎢·거점) + 표5(거점, 1·2차만)
                    't1': [_dcond('주거인구 밀도 ≥', 'rpop_den', 4000),
                           _dcond('주거인구수 ≥', 'rpop_sum', 100000),
                           _dcond('근무인구 밀도 ≥', 'wpop_den', 1750),
                           _dcond('근무인구수 ≥', 'wpop_sum', 50000)],
                    't2': [_dcond('주거인구 밀도 ≥', 'rpop_den', 750),
                           _dcond('주거인구수 ≥', 'rpop_sum', 5000),
                           _dcond('근무인구 밀도 ≥', 'wpop_den', 250),
                           _dcond('근무인구수 ≥', 'wpop_sum', 2000)],
                    't3': [_dcond('주거인구 밀도 ≥', 'rpop_den', 150),
                           _dcond('주거인구수 ≥', 'rpop_sum', 150),
                           _dcond('근무인구 밀도 ≥', 'wpop_den', 0),
                           _dcond('근무인구수 ≥', 'wpop_sum', 0)]}
                # 가이드라인 표4(주거)·표5(근무) 프리셋 — [기준 적용] 시 아래 조건칸을
                # 채움. 조건은 기존과 같이 체크한 것만 모두 AND, 채운 뒤 직접 수정 가능.
                self._hier_min = QComboBox()
                for v in (100, 150, 200, 300):
                    self._hier_min.addItem(f'{v}명/㎢ 이상', v)
                self._hier_min.setCurrentIndex(1)   # 150명/㎢ (전남광주 민감도 결과 채택값)
                self._hier_city = QComboBox()
                self._hier_city.addItems(['거점', '강소', '자립'])
                self._hier_work = QCheckBox('근무인구 기준 함께 적용 (1·2차)')
                self._hier_work.setChecked(True)
                self._hier_work.setToolTip('근무인구 지표 참고치는 1·2차 중심지 위계 설정을 위한 참고치이므로 '
                                           '근무 조건은 1·2차에만 걸고, 3차는 주거 조건만 적용')
                prow = QHBoxLayout(); prow.setSpacing(6)
                prow.addWidget(QLabel('후보 최소 주거밀도'))
                prow.addWidget(self._hier_min)
                prow.addWidget(QLabel('  도시위상'))
                prow.addWidget(self._hier_city)
                prow.addStretch()
                form.addRow('가이드라인 기준', prow)
                prow2 = QHBoxLayout(); prow2.setSpacing(6)
                prow2.addWidget(self._hier_work)
                abtn = QPushButton('기준 적용')
                abtn.clicked.connect(
                    lambda _=False, f=flow, w=widgets: self._apply_hier_preset(f, w))
                prow2.addWidget(abtn)
                hbtn = QPushButton('기준표 보기')
                hbtn.clicked.connect(self._show_hier_table)
                prow2.addWidget(hbtn)
                prow2.addStretch()
                form.addRow('', prow2)
                form.addRow(QLabel(
                    '<span style="color:gray">최소밀도는 1단계 후보 추출 임계값에 반영됩니다 — '
                    '최소밀도를 바꾸면 1단계부터, 도시위상만 바꾸면 3단계만 다시 실행.</span>'))
                _drow('1차중심지 기준', self._degurba_conds['t1'])
                _drow('2차중심지 기준', self._degurba_conds['t2'])
                _drow('3차중심지 기준', self._degurba_conds['t3'])
                self._degurba_btn = QPushButton('고급 설정… (전체 유형 한번에, 기준 지표 다양하게)')
                self._degurba_btn.clicked.connect(self._edit_adv_all)
                form.addRow(self._degurba_btn)
            elif not step['expose'] and not step.get('attrsel'):
                form.addRow(QLabel('추가 입력 없음 — 공통 데이터·이전 단계 결과로 '
                                   '자동 실행'))
            for name in step['expose']:
                p = pdefs[name]
                label = re.sub(r'\s*\(.*?\)\s*$', '', p.description() or name)
                if p.flags() & QgsProcessingParameterDefinition.FlagOptional:
                    label += ' (선택)'
                w = self._build_param_widget(p, step, children)
                widgets[(si, name)] = w
                if isinstance(w, QCheckBox):
                    form.addRow(w)
                elif isinstance(w, QgsCheckableComboBox):
                    # 다중 선택 콤보 — [전체 선택] 버튼 동반 (예: 시설 열 20종)
                    roww = QHBoxLayout()
                    roww.setSpacing(8)
                    roww.addWidget(w, 1)
                    ab = QPushButton('전체 선택')
                    ab.clicked.connect(
                        lambda _=False, cb=w: cb.setCheckedItems(
                            [cb.itemText(i) for i in range(cb.count())]))
                    roww.addWidget(ab)
                    cb_clear = QPushButton('전체 해제')
                    cb_clear.clicked.connect(
                        lambda _=False, cb=w: cb.deselectAllOptions())
                    roww.addWidget(cb_clear)
                    form.addRow(label, roww)
                else:
                    form.addRow(label, w)
            # 부모 레이어(같은 단계 또는 공통 데이터) → 자식 필드 콤보 연결
            for parent, combos in children.items():
                if parent in step.get('common', {}):
                    ckey = step['common'][parent]
                    fw = self._sp_common.get(ckey)
                    cache = f'spc:{ckey}'
                else:
                    fw = widgets.get((si, parent))
                    cache = f'{flow["key"]}:{si}:{parent}'
                if fw is None:
                    continue
                fw.fileChanged.connect(
                    lambda path, ch=combos, k=cache:
                    self._flow_layer_changed(k, path, ch))
                # 페이지 열 때 현재 공통데이터로 콤보 자동 재갱신용
                self._flow_refreshers.setdefault(flow['key'], []).append(
                    (fw, combos, cache))
            if flow.get('stepwise'):
                # 단계별 실행 버튼 — 앞 단계 완료 시 다음 단계 활성화
                sbtn = self._button(
                    f'{si + 1}단계 {"적용" if si == 1 else "실행"} — {step["label"]}',
                    lambda _=False, i=si: self._run_center_step(i))
                self._center_step_btns[si] = sbtn
                if step.get('attrsel'):
                    brow = QHBoxLayout()
                    brow.setSpacing(8)
                    brow.addWidget(sbtn, 1)
                    tbtn = QPushButton('집계 결과 표 보기')
                    tbtn.setToolTip('2단계 적용 후 — 후보별 집계 속성을 '
                                    '정렬 가능한 표로 확인')
                    tbtn.clicked.connect(self._show_attrs_table)
                    self._attr_table_btn = tbtn
                    brow.addWidget(tbtn)
                    form.addRow(brow)
                else:
                    form.addRow(sbtn)
            lay.addWidget(box)

        if flow.get('stepwise'):
            mrow = QHBoxLayout()
            mrow.addWidget(self._method_button(flow['key']))
            mrow.addStretch()
            lay.addLayout(mrow)
            self._update_center_buttons()
        else:
            lay.addWidget(self._action_row(
                flow['key'],
                f'{flow["title"]} 실행',
                lambda _=False, f=flow: self._run_flow(f)))
        lay.addStretch()
        return page

    # 후보 속성 추출의 생활인프라 시설 열 기본 선택 (국토생활인프라 20종 충족여부 *_ox)
    _DEFAULT_MULTI_FIELDS = {
        'VILLAGE_COLS': ['kg_ox', 'el_ox', 'sl_ox', 'ns_ox', 'cc_ox',
                         'sf_ox', 'cli_ox', 'ph_ox', 'spark_ox', 'bus_ox'],
        'BASE_COLS': ['ps_ox', 'pl_ox', 'sw_ox', 'slw_ox', 'hmo_ox',
                      'eg_ox', 'pcf_ox', 'tp_ox', 'poli_ox', 'fire_ox'],
    }

    def _build_param_widget(self, p, step, children):
        """파라미터 정의 → 입력 위젯 (플로우 페이지 전용)."""
        t = p.type()
        name = p.name()
        if t == 'field':
            multiple = getattr(p, 'allowMultiple', lambda: False)()
            if multiple:
                cb = QgsCheckableComboBox()
            else:
                cb = QgsFieldComboBox()
                cb.setAllowEmptyFieldName(
                    bool(p.flags() & QgsProcessingParameterDefinition.FlagOptional))
            default = p.defaultValue() if isinstance(p.defaultValue(), str) else ''
            if multiple:
                default = self._DEFAULT_MULTI_FIELDS.get(name, [])
            children.setdefault(p.parentLayerParameterName(), []).append(
                (cb, default, multiple))
            return cb
        if t == 'number':
            sp = QDoubleSpinBox()
            try:
                integer = (p.dataType() == p.Integer)
            except AttributeError:
                integer = False
            sp.setDecimals(0 if integer else 2)
            sp.setRange(max(p.minimum(), -1e9), min(p.maximum(), 1e9))
            try:
                sp.setValue(float(p.defaultValue() or 0))
            except (TypeError, ValueError):
                pass
            return sp
        if t == 'boolean':
            cb = QCheckBox(re.sub(r'\s*\(.*?\)\s*$', '', p.description() or name))
            cb.setChecked(bool(p.defaultValue()))
            if p.help():
                cb.setToolTip(p.help())
            return cb
        if t == 'enum':
            opts = p.options()
            if p.allowMultiple():
                cb = QgsCheckableComboBox()
                cb.addItems(opts)
            else:
                cb = QComboBox()
                cb.addItems(opts)
                try:
                    cb.setCurrentIndex(int(p.defaultValue() or 0))
                except (TypeError, ValueError):
                    pass
            return cb
        if t in ('vector', 'source', 'file'):
            fw = QgsFileWidget()   # 공통 데이터 밖의 예외적 파일 입력
            fw.setFilter(self._FLOW_FILTER)
            fw.setMinimumHeight(28)
            self._spst_fw.append((f'flow:{step["alg"]}:{name}', fw))
            return fw
        le = QLineEdit()   # string / expression / 기타
        preset = step.get('preset', {}).get(name)
        dv = preset if preset is not None else p.defaultValue()
        if dv:
            le.setText(str(dv))
        return le

    _FIELD_ALIASES = {'emd_nm': ('ADM_NM', 'ADM_DR_NM', 'EMD_KOR_NM', 'emd_kor_nm')}

    def _flow_layer_changed(self, cache_key, path, combos):
        layer = self._load_layer(cache_key, path, [])
        for cb, default, multiple in combos:
            if multiple:
                cb.clear()
                if layer is not None:
                    names = [f.name() for f in layer.fields()]
                    cb.addItems(names)
                    if default:            # 기본 선택 (레이어에 있는 열만)
                        cb.setCheckedItems([n for n in default if n in names])
            else:
                cb.setLayer(layer)
                if default:
                    cb.setField(default)
                    # 기본 필드명이 없으면 흔한 대체 이름으로 (예: 통계청 읍면동 경계 ADM_NM)
                    if layer is not None and not cb.currentField():
                        names = [f.name() for f in layer.fields()]
                        for alt in self._FIELD_ALIASES.get(default, ()):
                            if alt in names:
                                cb.setField(alt)
                                break

    @staticmethod
    def _read_flow_widget(p, w):
        """위젯 → 파라미터 값. (없으면 None — 기본값 사용)"""
        t = p.type()
        if t in ('vector', 'source', 'file'):
            return w.filePath().strip() or None
        if t == 'field':
            if isinstance(w, QgsCheckableComboBox):
                return w.checkedItems() or None
            return w.currentField() or None
        if t == 'number':
            v = w.value()
            return int(v) if w.decimals() == 0 else v
        if t == 'boolean':
            return w.isChecked()
        if t == 'enum':
            if isinstance(w, QgsCheckableComboBox):
                opts = p.options()
                return [opts.index(x) for x in w.checkedItems()]
            return w.currentIndex()
        return w.text().strip() or None

    # 위계(cent_cls) 고정 색 — 낮은 등급 → 높은 등급
    _CLS_COLORS = {'이외': '#cccccc', '생활중심지': '#fee090',
                   '지역중심지': '#fc8d59', '광역중심지': '#d73027'}
    _TIER_COLORS = {'1차중심지': '#d73027', '2차중심지': '#ff7f00',
                    '3차중심지': '#1a9850', '이외': '#cccccc'}

    def _categorized_renderer(self, layer, field):
        """필드 고유값 카테고리 심볼 — cent_cls는 고정 색, 그 외 골든앵글 색상환."""
        values = sorted(str(v) for v in layer.uniqueValues(
            layer.fields().indexOf(field)) if v is not None)
        cats = []
        for i, v in enumerate(values):
            sym = QgsSymbol.defaultSymbol(layer.geometryType())
            if field == 'cent_cls' and v in self._CLS_COLORS:
                sym.setColor(QColor(self._CLS_COLORS[v]))
            elif field == 'tier' and v in self._TIER_COLORS:
                sym.setColor(QColor(self._TIER_COLORS[v]))
            else:
                sym.setColor(QColor.fromHsv(int(i * 137.5) % 360, 150, 220))
            cats.append(QgsRendererCategory(v, sym, v))
        return QgsCategorizedSymbolRenderer(field, cats)

    _SP_PARENT_GROUP = '생활권 분석'

    def _drop_flow_group(self, group_name):
        """프로세스 결과 그룹의 레이어를 프로젝트에서 제거 (파일 잠금 해제).

        산출 파일이 지도에 열려 있으면 재실행 시 같은 경로 덮어쓰기가 실패하므로,
        실행 전에 이전 결과를 내려야 한다.
        """
        root = QgsProject.instance().layerTreeRoot()
        parent = root.findGroup(self._SP_PARENT_GROUP)
        grp = (parent or root).findGroup(group_name)
        if grp is None:
            return
        ids = [n.layerId() for n in grp.findLayers()]
        if ids:
            QgsProject.instance().removeMapLayers(ids)
        (parent if parent is not None else root).removeChildNode(grp)

    # ── ① 중심지 위계 분석: 단계별 실행 ──────────────────
    def _center_flow(self):
        return next(f for f in self.SPST_FLOWS if f.get('stepwise'))

    def _update_center_buttons(self):
        """단계 버튼·고급 버튼 활성화 — 앞 단계 산출이 있어야 다음이 열림."""
        o = getattr(self, '_center_outputs', {})
        btns = getattr(self, '_center_step_btns', {})
        if 1 in btns:
            btns[1].setEnabled((0, 'OUTPUT') in o)
        ok2 = (1, 'OUTPUT') in o
        if 2 in btns:
            btns[2].setEnabled(ok2)
        tbtn = getattr(self, '_attr_table_btn', None)
        if tbtn is not None:
            tbtn.setEnabled(ok2)
        btn = getattr(self, '_degurba_btn', None)
        if btn is not None:
            btn.setEnabled(ok2)
            btn.setToolTip(
                '집계 속성(인구·밀도·중심성·인프라 평균)으로 유형별 AND·OR '
                '조건을 조립 — 지정하면 숫자 기준 대체' if ok2 else
                '2단계(후보 속성 추출)를 먼저 적용하면 사용할 수 있습니다.')

    def _attrs_param_overrides(self):
        """2단계 집계 항목 체크 → 속성 추출 파라미터."""
        c = self._attr_checks
        ov = {
            'FIELD_RES_POP': 'pop_r' if c['res'].isChecked() else '',
            'FIELD_WOR_POP': 'pop_w' if c['wor'].isChecked() else '',
            'FIELD_INFLOW': 'pc_in' if c['pcin'].isChecked() else '',
            'FIELD_OUTFLOW': 'pc_out' if c['pcout'].isChecked() else '',
            # 통계 종류: 인구 = 합계·평균·밀도, 중심성 = 평균 (최대·최소 제외)
            'RES_POP_STATS': [0, 3, 4],
            'WOR_POP_STATS': [0, 3, 4],
            'INFLOW_STATS': [2],
            'OUTFLOW_STATS': [2],
        }
        agg = [i for i, k in enumerate(('tfac', 'vfac', 'bfac'))
               if c[k].isChecked()]
        if agg:
            ov['INFRA_AGG'] = agg
        # 인프라 전부 미선택이면 INFRA_AGG 미포함 — 열 파라미터는
        # _exec_flow_step에서 아예 생략 (빈 리스트 전달은 QGIS가 거부)
        return ov

    def _run_center_step(self, si):
        flow = self._center_flow()
        widgets = self._flow_widgets[flow['key']]
        grid_path = self._sp_common['grid'].filePath().strip()
        if not grid_path or not os.path.isfile(grid_path.split('|')[0]):
            QMessageBox.warning(self, '입력 확인',
                                '생활권 분석 > 공통 데이터에서 격자 파일을 선택하세요.')
            return
        outdir = os.path.join(self._unified_result_root(grid_path), flow['sub'])
        os.makedirs(outdir, exist_ok=True)
        self._release_result_locks(os.path.dirname(outdir))

        outputs = self._center_outputs
        for _p, (_kind, ref_si, ref_name) in flow['steps'][si].get('wire', {}).items():
            if (ref_si, ref_name) not in outputs:
                QMessageBox.warning(self, '입력 확인',
                                    f'{ref_si + 1}단계를 먼저 실행하세요.')
                return

        self._drop_flow_group(flow['title'])
        before_ids = set(QgsProject.instance().mapLayers().keys())
        try:
            ok = self._exec_flow_step(flow, si, widgets, outdir, outputs, [])
        finally:
            extra = set(QgsProject.instance().mapLayers().keys()) - before_ids
            if extra:
                QgsProject.instance().removeMapLayers(list(extra))
        if ok and flow['steps'][si]['alg'] == 'extract_candidates':
            # 후보를 뽑은 최소밀도 기록 — 기준 적용 시 밀도가 바뀌면 재실행 안내
            tw = widgets.get((si, 'POP_THRESHOLD'))
            self._center_cand_thr = float(tw.value()) if tw is not None else None
        # 지도 그룹 — 지금까지 단계 산출 누적 재구성
        items = []
        for i, st in enumerate(flow['steps']):
            for out_name, (style, vis) in st.get('maps', {}).items():
                p = outputs.get((i, out_name))
                if p and os.path.isfile(p):
                    items.append((p, style, vis))
        self._add_flow_group(flow['title'], items)
        self._update_center_buttons()
        if not ok:
            return
        if si == len(flow['steps']) - 1:
            for ckey, (ref_si, ref_name) in flow.get('after', {}).items():
                path = outputs.get((ref_si, ref_name))
                if path:
                    fw = self._sp_common[ckey]
                    if fw.filePath() == path:
                        fw.setFilePath('')
                    fw.setFilePath(path)
                    self._log.appendPlainText(f'[연결] 공통 데이터 ← {path}')
            self._log.appendPlainText(f'[완료] {flow["title"]} — 산출물: {outdir}')
            self._finish_step(flow['key'], flow['nxt'])

    def _show_attrs_table(self):
        """2단계 집계 결과를 정렬 가능한 표 팝업으로 표시."""
        path = self._center_outputs.get((1, 'OUTPUT'))
        if not path or not os.path.isfile(path):
            QMessageBox.information(self, '안내', '2단계(후보 속성 추출)를 먼저 적용하세요.')
            return
        lyr = QgsVectorLayer(path, '중심지_속성', 'ogr')
        if not lyr.isValid():
            QMessageBox.warning(self, '파일 오류', f'결과를 열 수 없습니다:\n{path}')
            return
        fields = [f.name() for f in lyr.fields()]
        feats = list(lyr.getFeatures())

        dlg = QDialog(self)
        dlg.setWindowTitle('후보 속성 집계 표 — 중심지_속성')
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f'후보 {len(feats)}개 — 열 머리글 클릭으로 정렬'))
        t = QTableWidget()
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        t.setSortingEnabled(False)      # 채우는 동안 꺼둠
        t.setColumnCount(len(fields))
        t.setHorizontalHeaderLabels(fields)
        t.setRowCount(len(feats))
        for r, ft in enumerate(feats):
            for c, fn in enumerate(fields):
                val = ft[fn]
                item = QTableWidgetItem()
                try:
                    item.setData(Qt.DisplayRole, round(float(val), 2))
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                except (TypeError, ValueError):
                    item.setText('' if val is None else str(val))
                t.setItem(r, c, item)
        t.setSortingEnabled(True)
        t.resizeColumnsToContents()
        v.addWidget(t)
        dlg.resize(960, 600)
        dlg.exec_()

    def _edit_adv_all(self):
        path = self._center_outputs.get((1, 'OUTPUT'))
        if not path or not os.path.isfile(path):
            QMessageBox.information(
                self, '안내', '2단계(후보 속성 추출)를 먼저 적용하세요.')
            return
        lyr = QgsVectorLayer(path, '중심지_속성', 'ogr')
        have = {f.name() for f in lyr.fields()} if lyr.isValid() else set()
        fields = [(lb, nm) for lb, nm in _ADV_FIELDS if nm in have]
        prev = {p: self._degurba_adv.get(p, {}).get('rows', [])
                for _t, p in AdvAllDialog._SECTIONS}
        dlg = AdvAllDialog(self, fields or None, prev)
        if dlg.exec_() == QDialog.Accepted:
            any_set = False
            for pname, w in dlg.sections.items():
                rows = w.result_rows()
                if rows:
                    self._degurba_adv[pname] = {'rows': rows,
                                                'expr': w.expression()}
                    any_set = True
                else:
                    self._degurba_adv.pop(pname, None)
            self._degurba_btn.setText(
                '고급 설정 ✓ (사용 중)' if any_set else '고급 설정… (전체 유형 한번에, 기준 지표 다양하게)')

    def _apply_hier_preset(self, flow, widgets):
        """가이드라인 표4(+표5) 값을 위계 조건칸과 1단계 후보 임계값에 채운다."""
        mind = self._hier_min.currentData()
        city = self._hier_city.currentText()
        use_w = self._hier_work.isChecked()
        t4 = self._HIER_T4[mind][city]
        t5 = self._HIER_T5[city]
        for i, key in enumerate(('t1', 't2', 't3')):
            vals = {'rpop_den': t4[i][0], 'rpop_sum': t4[i][1],
                    'wpop_den': t5[i][0], 'wpop_sum': t5[i][1]}
            for field, cb, sp in self._degurba_conds[key]:
                sp.setValue(vals[field])
                # 표5는 1·2차용 참고치 — 3차는 주거 조건만
                cb.setChecked(field.startswith('r') or (use_w and key != 't3'))
        # 고급 설정(조건식)이 있으면 숫자 기준보다 우선하므로 해제
        if self._degurba_adv:
            self._degurba_adv.clear()
            self._degurba_btn.setText('고급 설정… (전체 유형 한번에, 기준 지표 다양하게)')
            self._log.appendPlainText('[기준 적용] 고급 설정 조건식 해제 — 프리셋 값 사용')
        # 최소밀도 → 1단계 후보 추출 임계값
        csi = next((i for i, s in enumerate(flow['steps'])
                    if s['alg'] == 'extract_candidates'), None)
        thr_w = widgets.get((csi, 'POP_THRESHOLD')) if csi is not None else None
        if thr_w is not None:
            thr_w.setValue(float(mind))
        used = getattr(self, '_center_cand_thr', None)
        outs = getattr(self, '_center_outputs', {})
        if csi is not None and (csi, 'OUTPUT') in outs and used is not None \
                and abs(used - float(mind)) > 1e-9:
            # 다른 최소밀도로 뽑은 후보에 위계를 걸지 않도록 1단계부터 다시 열기
            for k in [k for k in outs if k[0] >= csi]:
                outs.pop(k)
            self._update_center_buttons()
            QMessageBox.information(
                self, '1단계 재실행 필요',
                f'현재 후보는 최소밀도 {used:g}명/㎢로 추출돼 있습니다.\n'
                f'{mind}명/㎢로 바꿨으므로 1단계 후보 추출부터 다시 실행하세요.')
        self._log.appendPlainText(
            f'[기준 적용] 주거인구 기준 · 최소밀도 {mind}명/㎢ · {city}'
            + (' + 근무인구 기준(1·2차)' if use_w else '') + ' (조건 모두 AND)')

    def _show_hier_table(self):
        """가이드라인 표4·표5 참고치 팝업."""
        def fmt(dp):
            return f'{dp[0]:,}명/㎢ AND {dp[1]:,}명'

        def head(first):
            cols = [first, '위상', '1차 기준', '시·군청', '2차 기준', '읍',
                    '3차 기준', '면']
            return '<tr>' + ''.join(
                f'<th bgcolor="#e8e8e8">{c}</th>' for c in cols) + '</tr>'

        CITIES = ('거점', '강소', '자립')

        def row(m, c, v, r):
            # 최소 밀도 칸은 위상 3행에 걸쳐 하나로
            first = (f'<td rowspan="{len(CITIES)}" align="center" valign="middle"><b>{m}</b></td>'
                     if c == CITIES[0] else '')
            return (f'<tr>{first}<td>{c}</td>'
                    f'<td>{fmt(v[0])}</td><td>{r[0]}%</td>'
                    f'<td>{fmt(v[1])}</td><td>{r[1]}%</td>'
                    f'<td>{fmt(v[2])}</td><td>{r[2]}%</td></tr>')

        t = '<table border="1" cellpadding="4" cellspacing="0">'
        rows4 = ''.join(row(m, c, self._HIER_T4[m][c], self._HIER_T4_RATIO[m][c])
                        for m in (100, 150, 200, 300)
                        for c in CITIES)
        rows5 = ''.join(row(100, c, self._HIER_T5[c], self._HIER_T5_RATIO[c])
                        for c in CITIES)
        # 좌: 표4(주거) · 우: 표5(근무) — 세로로 쌓으면 화면 아래가 잘림
        html = ('<table cellspacing="0" cellpadding="0"><tr>'
                '<td valign="top"><b>도시위상별 중심지 위계 설정을 위한 주거인구 지표 참고치</b><br>'
                + t + head('후보 최소<br>주거밀도') + rows4 + '</table></td>'
                '<td width="18"></td>'
                '<td valign="top"><b>도시위상별 중심지 위계 설정을 위한 근무인구 지표 참고치</b><br>'
                + t + head('후보 최소<br>근무밀도') + rows5 + '</table></td>'
                '</tr></table>')
        # 화면보다 크면 스크롤 — 메시지 상자는 넘치는 부분이 잘림
        dlg = QDialog(self)
        dlg.setWindowTitle('위계 설정 기준표')
        lay = QVBoxLayout(dlg)
        lab = QLabel(html)
        lab.setTextFormat(Qt.RichText)
        lab.setMargin(10)
        area = QScrollArea()
        area.setWidget(lab)
        lay.addWidget(area)
        bb = QDialogButtonBox(QDialogButtonBox.Ok)
        bb.accepted.connect(dlg.accept)
        lay.addWidget(bb)
        scr = QApplication.desktop().availableGeometry(self)
        sz = lab.sizeHint()
        dlg.resize(min(sz.width() + 60, int(scr.width() * 0.95)),
                   min(sz.height() + 90, int(scr.height() * 0.9)))
        dlg.exec_()

    def _release_result_locks(self, results_base):
        """분석결과 파일을 잡고 있는 캐시 레이어 해제 — 슬롯 자동 연결로 열린
        레이어가 파일을 잠그면 재실행 덮어쓰기가 "already exists"로 실패한다."""
        results_base = os.path.normcase(results_base)
        for key in list(self._layers):
            lyr = self._layers[key]
            try:
                src = os.path.normcase(lyr.source().split('|')[0])
            except RuntimeError:      # 이미 삭제된 레이어
                self._layers.pop(key)
                continue
            if src.startswith(results_base):
                self._layers.pop(key)

    def _writable_out_path(self, outdir, fname):
        """산출 경로 확보 — 기존 파일 삭제 시도, 잠겨 있으면 _2, _3… 번호로 우회.

        지도 레이어·캐시는 실행 전에 내리지만 QGIS의 OGR 연결 풀이 파일 핸들을
        잠시 더 잡고 있을 수 있어(Windows) 삭제가 거부되면 새 이름으로 저장한다.
        """
        stem, ext = os.path.splitext(os.path.join(outdir, fname))
        exts = ([ext] if ext.lower() != '.shp'
                else ['.shp', '.shx', '.dbf', '.prj', '.cpg', '.qix'])

        def clear(base):
            for e in exts:
                p = base + e
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        return False
            return True

        if clear(stem):
            return stem + ext
        n = 2
        while not clear(f'{stem}_{n}'):
            n += 1
        self._log.appendPlainText(
            f'[경고] {os.path.basename(stem + ext)} 파일이 잠겨 있어 '
            f'{os.path.basename(stem)}_{n}{ext} 로 저장합니다')
        return f'{stem}_{n}{ext}'

    def _add_flow_group(self, group_name, items):
        """플로우 산출물을 '생활권 분석' 그룹 아래 프로세스별 하위 그룹으로 추가.

        뒤 단계 결과가 위, 최종만 켬. 같은 프로세스 재실행 시 하위 그룹 교체.
        """
        if not items:
            return
        self._drop_flow_group(group_name)
        root = QgsProject.instance().layerTreeRoot()
        parent = root.findGroup(self._SP_PARENT_GROUP)
        if parent is None:
            parent = root.insertGroup(0, self._SP_PARENT_GROUP)
        group = parent.insertGroup(0, group_name)
        for path, style_field, visible in reversed(items):
            name = os.path.splitext(os.path.basename(path))[0]
            lyr = QgsVectorLayer(path, name, 'ogr')
            if not lyr.isValid():
                self._log.appendPlainText(f'[경고] 레이어 로드 실패: {path}')
                continue
            if style_field and lyr.fields().indexOf(style_field) >= 0:
                lyr.setRenderer(self._categorized_renderer(lyr, style_field))
            QgsProject.instance().addMapLayer(lyr, False)
            node = group.addLayer(lyr)
            node.setItemVisibilityChecked(visible)
        self._log.appendPlainText(
            f'[스타일] {group_name} 그룹: 레이어 {len(items)}개 추가')

    def _run_flow(self, flow):
        widgets = self._flow_widgets[flow['key']]

        # 출력 폴더: 공용 data\분석결과\<sub> (모든 분석 결과 통합)
        grid_path = self._sp_common['grid'].filePath().strip()
        if not grid_path or not os.path.isfile(grid_path.split('|')[0]):
            QMessageBox.warning(self, '입력 확인',
                                '생활권 분석 > 공통 데이터에서 격자 파일을 선택하세요.')
            return
        outdir = os.path.join(self._unified_result_root(grid_path), flow['sub'])
        os.makedirs(outdir, exist_ok=True)

        self._release_result_locks(os.path.dirname(outdir))

        # 이전 실행 결과가 지도에 열려 있으면 산출 파일이 잠겨 재실행이
        # 실패하므로 (Windows), 해당 프로세스의 결과 그룹을 먼저 내린다
        self._drop_flow_group(flow['title'])

        outputs = {}     # (단계, 출력파라미터) → 실제 경로
        map_items = []   # 지도에 올릴 (경로, 스타일 필드, 기본 켬)
        # 일부 알고리즘이 postProcessAlgorithm에서 결과를 지도에 직접 올림 →
        # 대화상자 그룹과 중복. 실행 전 스냅샷을 떠서 새로 생긴 낱개 레이어는
        # 걷어내고, 지도 적재는 아래 _add_flow_group에서만 한다.
        before_ids = set(QgsProject.instance().mapLayers().keys())
        try:
            self._run_flow_steps(flow, widgets, outdir, outputs, map_items)
        finally:
            extra = set(QgsProject.instance().mapLayers().keys()) - before_ids
            if extra:
                QgsProject.instance().removeMapLayers(list(extra))
        if outputs.get('__aborted__'):
            return

        self._add_flow_group(flow['title'], map_items)
        # 완료 산출물을 공통 데이터에 자동 연결 (다음 프로세스 입력)
        for ckey, (ref_si, ref_name) in flow.get('after', {}).items():
            path = outputs.get((ref_si, ref_name))
            if path:
                fw = self._sp_common[ckey]
                if fw.filePath() == path:
                    # 같은 경로 재연결 — 신호를 강제로 내 캐시·필드 콤보 재로드
                    fw.setFilePath('')
                fw.setFilePath(path)
                self._log.appendPlainText(f'[연결] 공통 데이터 ← {path}')
        self._log.appendPlainText(f'[완료] {flow["title"]} — 산출물: {outdir}')
        self._finish_step(flow['key'], flow['nxt'])

    def _run_flow_steps(self, flow, widgets, outdir, outputs, map_items):
        for si in range(len(flow['steps'])):
            if not self._exec_flow_step(flow, si, widgets, outdir,
                                        outputs, map_items):
                outputs['__aborted__'] = True
                return

    def _exec_flow_step(self, flow, si, widgets, outdir, outputs, map_items):
        """플로우의 한 단계 실행 — 성공 시 True. outputs·map_items 갱신."""
        if True:   # (들여쓰기 유지용 — 아래 본문은 기존 단계 루프 본문)
            step = flow['steps'][si]
            alg = self._flow_alg(step['alg'])
            self._log.appendPlainText('━' * 50)
            self._log.appendPlainText(
                f'[{flow["title"]}] {si + 1}/{len(flow["steps"])}단계: {step["label"]}')
            params = {}
            abort = None
            for p in alg.parameterDefinitions():
                name = p.name()
                optional = bool(p.flags()
                                & QgsProcessingParameterDefinition.FlagOptional)
                if step.get('attrsel') and name in (
                        'FIELD_RES_POP', 'FIELD_WOR_POP', 'FIELD_INFLOW',
                        'FIELD_OUTFLOW', 'INFRA_AGG',
                        'RES_POP_STATS', 'WOR_POP_STATS',
                        'INFLOW_STATS', 'OUTFLOW_STATS'):
                    ov = self._attrs_param_overrides()
                    if name in ov:
                        params[name] = ov[name]
                        continue
                    # INFRA_AGG 미포함(인프라 전부 미선택) → 기본값에 맡김
                if step.get('attrsel') and name in ('VILLAGE_COLS', 'BASE_COLS'):
                    if 'INFRA_AGG' not in self._attrs_param_overrides():
                        # 인프라 전부 미선택 → 열 선택 무시, 파라미터 생략
                        continue
                    # 인프라 선택됨 → 아래에서 위젯 값 사용
                if name in step.get('common', {}):
                    path = self._sp_common[step['common'][name]].filePath().strip()
                    if not path:
                        if not optional:
                            ckey = step['common'][name]
                            label = dict((k, l) for k, l, *_ in self.SP_COMMON)[ckey]
                            if ckey in ('center_cls', 'sub_zone'):
                                need, where = (
                                    ('① 중심지 추출 및 위계 분류', '②')
                                    if ckey == 'center_cls'
                                    else ('④ 3차중심지 권역 도출', '⑤'))
                                abort = (f'{step["label"]}: "{label}"이 비어 있습니다.\n'
                                         f'{need}를 먼저 실행하거나, {where} 페이지의 '
                                         f'이전 단계 결과에 기존 파일을 지정하세요.')
                            else:
                                abort = (f'{step["label"]}: 공통 데이터에서 '
                                         f'"{label}" 파일을 선택하세요.')
                            break
                        continue
                    params[name] = path
                    continue
                if name in step.get('wire', {}):
                    _kind, ref_si, ref_name = step['wire'][name]
                    params[name] = outputs[(ref_si, ref_name)]
                    continue
                if name in step.get('outs', {}):
                    params[name] = self._writable_out_path(
                        outdir, step['outs'][name])
                    continue
                if step.get('degurba') and name.endswith('_EXPR'):
                    # 고급 조건(조건 행 빌더)이 있으면 우선, 없으면 스핀박스 기준
                    adv = self._degurba_adv.get(name)
                    if adv:
                        params[name] = adv['expr']
                        self._log.appendPlainText(
                            f'  조건 {name} (고급): {adv["expr"]}')
                        continue
                    # 체크된 조건만 AND 결합 → QGIS 표현식 (TYPE_1=3차 … TYPE_3=1차)
                    def _dexpr(conds):
                        parts = [f'"{f}" >= {int(sp.value())}'
                                 for f, cb, sp in conds if cb.isChecked()]
                        return ' AND '.join(parts) if parts else '"rpop_den" >= 0'
                    params[name] = {
                        'TYPE_1_EXPR': _dexpr(self._degurba_conds['t3']),
                        'TYPE_2_EXPR': _dexpr(self._degurba_conds['t2']),
                        'TYPE_3_EXPR': _dexpr(self._degurba_conds['t1']),
                    }[name]
                    self._log.appendPlainText(f'  조건 {name}: {params[name]}')
                    continue
                w = widgets.get((si, name))
                if w is None:
                    if name in step.get('preset', {}):
                        params[name] = step['preset'][name]
                    continue   # 미노출 → 알고리즘 기본값
                val = self._read_flow_widget(p, w)
                if val is None:
                    # 필드 콤보가 값을 반환 못해도, 파라미터에 문자열 기본값(예: cand_id)이
                    # 있으면 그걸로 폴백 — 부모 레이어에 그 필드가 있으면 알고리즘이 처리.
                    dv = p.defaultValue()
                    if isinstance(dv, str) and dv:
                        val = dv
                    elif not optional:
                        abort = f'{step["label"]}: {p.description()} 입력 필요'
                        break
                    else:
                        continue
                params[name] = val
            if abort:
                QMessageBox.warning(self, '입력 확인', abort)
                return False

            result = self._run(f'krihs_ssim:{step["alg"]}', params)
            if not result:
                self._log.appendPlainText(
                    f'[중단] {si + 1}단계({step["label"]}) 실패')
                return False
            for out_name in step.get('outs', {}):
                path = result.get(out_name) or params.get(out_name)
                outputs[(si, out_name)] = path
            for out_name, (style_field, visible) in step.get('maps', {}).items():
                path = outputs.get((si, out_name))
                if path and os.path.isfile(path):
                    map_items.append((path, style_field, visible))
            return True

    # ══════════════════════════════════════════════════════════
    # KPI 진단 — 공통 데이터(파일 로드) / KPI 지표 산출(필드 선택·실행)
    # ══════════════════════════════════════════════════════════
    _KPI_FILTER = '벡터 레이어 (*.gpkg *.shp);;GeoPackage (*.gpkg);;Shapefile (*.shp)'

    def _kpi_setup(self):
        if getattr(self, '_kpi', None) is not None:
            return
        self._kpi = {}
        k = self._kpi

        def fieldcb(numeric=False):
            cb = QgsFieldComboBox()
            if numeric:
                cb.setFilters(QgsFieldProxyModel.Numeric)
            cb.setAllowEmptyFieldName(True)
            return cb

        k['f_resid'] = fieldcb(True); k['f_work'] = fieldcb(True); k['f_gid'] = fieldcb()
        k['f_zone'] = fieldcb(); k['f_chier'] = fieldcb()
        k['f_base1'] = fieldcb(True); k['f_base2'] = fieldcb(True); k['f_base3'] = fieldcb(True)

        def filew(mode=None, filt=None):
            fw = QgsFileWidget()
            fw.setFilter(filt or self._KPI_FILTER)
            fw.setMinimumHeight(28)
            if mode is not None:
                fw.setStorageMode(mode)
            return fw

        k['grid'] = filew()
        k['grid'].fileChanged.connect(lambda p: self._load_layer(
            'kpi_grid', p, [(k['f_resid'], 'pop_r'), (k['f_work'], 'pop_w'), (k['f_gid'], 'gid')]))
        k['zones'] = filew()
        k['zones'].fileChanged.connect(self._load_kpi_zones)
        k['centers'] = filew()
        k['centers'].fileChanged.connect(self._load_kpi_centers)
        k['region_bnd'] = filew()
        k['net'] = filew()
        # 격자→중심지 OD (계획권역·생활권 접근 형평성/연결성용)
        k['od'] = filew(filt='OD (*.csv *.gpkg *.shp)')
        # OD 필드는 입력이 아니라 파일 컬럼에서 선택(편집 가능 콤보 — 기본값 유지)
        k['od_origin'] = QComboBox(); k['od_origin'].setEditable(True)
        k['od_origin'].setCurrentText('origin_id')
        k['od_dest'] = QComboBox(); k['od_dest'].setEditable(True)
        k['od_dest'].setCurrentText('destination_id')
        k['od_dist'] = QComboBox(); k['od_dist'].setEditable(True)
        k['od_dist'].setCurrentText('total_cost')
        k['od'].fileChanged.connect(self._load_od_field_combos)
        try:
            npath = self._sp_common.get('network')
            if npath and npath.filePath().strip():
                k['net'].setFilePath(npath.filePath().strip())
        except Exception:
            pass
        k['fac_neigh_dir'] = filew(mode=QgsFileWidget.GetDirectory, filt='폴더')
        k['fac_hub_dir'] = filew(mode=QgsFileWidget.GetDirectory, filt='폴더')
        k['fac_radius'] = QLineEdit('1500')       # 향유도 반경 근린(m)
        k['fac_radius_hub'] = QLineEdit('3000')   # 향유도 반경 거점(m)
        k['fac_minpop'] = QLineEdit('500')        # 관리대상 최소배후 근린(명)
        k['fac_minpop_hub'] = QLineEdit('3000')   # 관리대상 최소배후 거점(명)
        for i in (1, 2, 3):
            k['base%d' % i] = filew()
            k['base%d' % i].fileChanged.connect(
                lambda p, cb=k['f_base%d' % i], key='kpi_base%d' % i:
                self._load_layer(key, p, [(cb, '')]))

        # 탄소 총배출 (탄소공간지도 원천 1개 — 격자코드 조인)
        k['co2_src'] = filew()
        # 탄소 필드도 입력이 아니라 원천 파일 컬럼에서 선택(편집 가능 콤보)
        k['co2_join'] = QComboBox(); k['co2_join'].setEditable(True)
        k['co2_join'].setCurrentText('grid_cd')
        k['co2_value'] = QComboBox(); k['co2_value'].setEditable(True)
        k['co2_value'].setCurrentText('exhst_vl')
        k['co2_src'].fileChanged.connect(lambda p: self._fill_field_combos(
            p, [(k['co2_join'], 'grid_cd'), (k['co2_value'], 'exhst_vl')]))

        k['region'] = QLineEdit('지역')
        # 가이드라인 참조값(표7·표8)·기준연도·밀도 참고치 — 대시보드 진단 요약용
        k['base_year'] = QLineEdit('2023')
        k['city_type'] = QComboBox(); k['city_type'].addItems(['미지정', '거점도시', '강소도시', '자립도시'])
        k['trend_type'] = QComboBox(); k['trend_type'].addItems(['미지정', '성장형', '성숙·안정형', '감소형'])
        k['dens_ref'] = QLineEdit('2000')
        k['base_label1'] = QLineEdit('노후주택 비율'); k['base_unit1'] = QLineEdit('%')
        k['base_label2'] = QLineEdit('토지이용 압축도'); k['base_unit2'] = QLineEdit('지수')
        k['base_label3'] = QLineEdit('토지이용 복합도'); k['base_unit3'] = QLineEdit('지수')
        k['scen_folder'] = filew(mode=QgsFileWidget.GetDirectory, filt='폴더')
        k['fut_years'] = QLineEdit('2025, 2030, 2035, 2040'); k['fut_resid'] = QLineEdit('pop_tot')
        k['out_html'] = self._save_widget('HTML (*.html)')
        k['open_html'] = QCheckBox('완료 후 브라우저 열기'); k['open_html'].setChecked(True)
        k['batch_dir'] = filew(mode=QgsFileWidget.GetDirectory, filt='폴더')   # KPI 산출 폴더 (비우면 자동)
        # 종합 리포트: 단계별 결과(JSON) 자동 연결 칸 — 단계 실행은 자기 칸, 일괄 산출은 네 칸 모두
        for key, _nm in self._KPI_AREAS:
            k['rep_' + key] = filew(filt='JSON (*.json)')

    def _kpi_area_brief(self, key):
        """산출 페이지용 간략 지표 설명(HTML) — 상세는 결과 HTML의 '지표 설명 보기'."""
        try:
            from .alg_kpi_indicators import KPI_INDICATOR_DESC
        except Exception:
            return ''
        for g in KPI_INDICATOR_DESC:
            if g['key'] == key:
                items = '<br>'.join('· <b>%s</b> — %s' % (it['n'], it['d']) for it in g['items'])
                return ('<span style="color:gray">■ 산출 지표<br>%s<br>'
                        '<i>· 자세한 정의·산출식·해석은 결과 HTML의 [지표 설명 보기]에서 확인</i></span>'
                        % items)
        return ''

    def _page_kpi_common(self):
        self._kpi_setup(); k = self._kpi
        page = QWidget(); lay = QVBoxLayout(page)
        lay.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN); lay.setSpacing(12)
        lay.addWidget(self._title(
            '<b>KPI 공통 데이터</b> — 중심지·생활권 분석 결과가 <b>자동 연결</b>됩니다(빈 칸만; '
            '직접 지정 시 그대로 유지).<br>'
            '<span style="color:gray">격자←거점지도 격자 · 생활권 경계←기초생활권 결과 · '
            '중심지 폴리곤←중심지 위계 결과 · 도로망←생활권 공통데이터. '
            '파일을 불러오면 아래 필드가 드롭다운으로 채워집니다.</span>'))
        g1 = QGroupBox('공통 입력 (앞 단계 산출물 · 자동 연결)'); f1 = self._form(g1)
        f1.addRow('격자 인구(기준년도)', k['grid']); f1.addRow('생활권 경계', k['zones'])
        f1.addRow('중심지 폴리곤(위계 포함)', k['centers'])
        f1.addRow('계획권역(시군구) 경계(선택)', k['region_bnd'])
        lay.addWidget(g1)
        g0 = QGroupBox('지역명 · 필드 선택'); f0 = self._form(g0)
        f0.addRow('지역명(대시보드 제목)', k['region'])
        f0.addRow('기준연도', k['base_year'])
        f0.addRow('도시위상 (참조값 표시)', k['city_type'])
        f0.addRow('인구추세 유형 (참조값 표시)', k['trend_type'])
        f0.addRow('중심지 밀도 참고치 (명/㎢)', k['dens_ref'])
        f0.addRow('격자 주거인구 필드', k['f_resid']); f0.addRow('격자 근무인구 필드(선택)', k['f_work'])
        f0.addRow('격자 ID 필드', k['f_gid']); f0.addRow('생활권 이름 필드(선택)', k['f_zone'])
        f0.addRow('중심지 위계 필드', k['f_chier'])
        lay.addWidget(g0)

        # ② 콤팩트성 — 탄소
        g2 = QGroupBox('탄소 총배출 (② 콤팩트성, 선택) — 격자코드로 기준년도 격자에 조인'); f2 = self._form(g2)
        f2.addRow('탄소 총배출 원천(격자)', k['co2_src'])
        crow = QWidget(); crl = QHBoxLayout(crow)
        crl.setContentsMargins(0, 0, 0, 0); crl.setSpacing(6)
        crl.addWidget(QLabel('격자코드 필드')); crl.addWidget(k['co2_join'], 2)
        crl.addWidget(QLabel('총배출량 필드')); crl.addWidget(k['co2_value'], 2)
        f2.addRow('  ↳ 필드', crow)
        lay.addWidget(g2)

        # ③ 네트워크성 — 도로망·OD
        g3 = QGroupBox('도로망 · 격자→중심지 OD (③ 네트워크성)'); f3 = self._form(g3)
        f3.addRow('도로망 (KTDB)', k['net'])
        f3.addRow('격자→중심지 OD', k['od'])
        orow = QWidget(); orl = QHBoxLayout(orow)
        orl.setContentsMargins(0, 0, 0, 0); orl.setSpacing(6)
        for _lbl, _w in (('출발ID', k['od_origin']), ('도착ID', k['od_dest']), ('거리(m)', k['od_dist'])):
            orl.addWidget(QLabel(_lbl))
            _w.setMinimumWidth(120); orl.addWidget(_w)
        orl.addStretch(1)
        f3.addRow('  ↳ OD 필드', orow)
        lay.addWidget(g3)

        # ④ 생활편리성 — 시설
        g4 = QGroupBox('시설 — 폴더 지정 (④ 생활편리성, 폴더 안 gpkg·shp 전부)'); f4 = self._form(g4)
        f4.addRow('근린시설 폴더', k['fac_neigh_dir']); f4.addRow('거점시설 폴더', k['fac_hub_dir'])

        def _fac_param_row(radius_w, minpop_w):
            row = QWidget(); rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(6)
            rl.addWidget(QLabel('향유도 반경(m)'))
            radius_w.setMinimumWidth(80); rl.addWidget(radius_w)
            rl.addWidget(QLabel('   최소 배후인구(명)'))
            minpop_w.setMinimumWidth(80); rl.addWidget(minpop_w)
            rl.addStretch(1)
            return row
        f4.addRow('근린시설 기준', _fac_param_row(k['fac_radius'], k['fac_minpop']))
        f4.addRow('거점시설 기준', _fac_param_row(k['fac_radius_hub'], k['fac_minpop_hub']))
        lay.addWidget(g4)

        # 장래 추계 시계열
        g5 = QGroupBox('장래 추계 시계열 (선택; 장래인구 배분 결과 자동 연결)'); f5 = self._form(g5)
        f5.addRow('시나리오별 추계 인구 폴더', k['scen_folder'])
        f5.addRow('(장래) 연도 리스트', k['fut_years']); f5.addRow('(장래) 주거인구 필드', k['fut_resid'])
        lay.addWidget(g5)
        lay.addStretch(1)
        return page

    # ── 일괄 산출 ─────────────────────────────────────────
    def _kpi_dir(self):
        """KPI 결과 폴더 — 일괄 산출 페이지에서 지정한 폴더, 비어 있으면
        결과 저장 폴더\\S-Sim_분석결과(지역명)\\KPI (없으면 만듦, 못 정하면 None)."""
        k = self._kpi
        chosen = k['batch_dir'].filePath().strip()
        if chosen:
            try:
                os.makedirs(chosen, exist_ok=True)
                return chosen
            except OSError:
                pass
        root = (self._unified_result_root(k['grid'].filePath().strip())
                or self._analysis_root(RES) or self._analysis_root(WORK))
        if not root:
            return None
        d = os.path.join(root, OUT_KPI)
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            return None
        return d

    _KPI_AREAS = (('basic', '기초통계'), ('compact', '콤팩트성'), ('network', '네트워크성'), ('life', '생활편리성'))

    def _link_kpi_json(self):
        """결과 폴더에 단계별 결과(JSON)가 있으면 종합 리포트의 빈 칸에 연결 (파일 작업 없음)."""
        if not getattr(self, '_kpi', None):
            return
        rr = self._region_result_root() or self._unified_result_root(self._kpi['grid'].filePath().strip())
        if not rr:
            return
        kdir = self._kpi['batch_dir'].filePath().strip() or os.path.join(rr, OUT_KPI)
        for key, nm in self._KPI_AREAS:
            p = os.path.join(kdir, nm + '_결과.json')
            fw = self._kpi['rep_' + key]
            if os.path.isfile(p) and not fw.filePath().strip():
                fw.setFilePath(p)
                self._log.appendPlainText(f'[자동연결] 종합 리포트 ← {nm} 결과 {p}')

    def _kpi_ensure_layers(self):
        """KPI 공통 데이터 칸에 경로가 있으면 레이어·필드 목록을 확실히 불러 둔다.

        초기화 뒤 [지역 적용]으로 같은 경로가 다시 들어오면 경로 변경 신호가 없어
        레이어를 다시 읽지 않는 경우가 있음 → 칸 경로 기준으로 직접 확인.
        """
        k = self._kpi
        loaders = (('grid', 'kpi_grid', lambda p: self._load_layer(
                        'kpi_grid', p, [(k['f_resid'], 'pop_r'), (k['f_work'], 'pop_w'), (k['f_gid'], 'gid')])),
                   ('zones', 'kpi_zones', self._load_kpi_zones),
                   ('centers', 'kpi_centers', self._load_kpi_centers))
        for key, lkey, load in loaders:
            path = k[key].filePath().strip()
            if not path:
                continue
            lyr = self._layers.get(lkey)
            try:
                ok = lyr is not None and lyr.isValid() and lyr.source() == path
            except RuntimeError:          # 이미 지워진 레이어
                ok = False
            if not ok:
                self._layers.pop(lkey, None)
                load(path)

    def _kpi_missing(self):
        """산출에 반드시 필요한데 공통 데이터에 빠진 것 (이름 목록)."""
        k = self._kpi
        self._kpi_ensure_layers()
        miss = []
        for key, lkey, name in (('grid', 'kpi_grid', '격자 인구(기준년도)'),
                                ('zones', 'kpi_zones', '생활권 경계'),
                                ('centers', 'kpi_centers', '중심지 폴리곤(위계 포함)')):
            if not k[key].filePath().strip() or self._layers.get(lkey) is None:
                miss.append(name)
        if self._layers.get('kpi_grid') is not None and not k['f_resid'].currentField():
            miss.append('격자 주거인구 필드')
        return miss

    def _refresh_batch_page(self):
        pass                                  # 저장 폴더는 실행 때 정해짐 (비우면 자동, 실행 후 채워짐)

    def _page_kpi_batch(self):
        self._kpi_setup()
        page = QWidget(); lay = QVBoxLayout(page)
        lay.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN); lay.setSpacing(12)
        lay.addWidget(self._title(
            '<b>일괄 산출</b> — ①~④ 지표를 한 번에 산출합니다.<br>'
            '<span style="color:gray">입력은 공통 데이터에서 지정합니다. '
            '끝나면 종합 리포트로 이동합니다.</span>'))
        g = QGroupBox('출력'); f = self._form(g)
        f.addRow('저장 폴더', self._kpi['batch_dir'])
        f.addRow('산출물', QLabel('① 기초통계 결과 (csv, json)<br>② 콤팩트성 결과 (csv, json)<br>'
                                '③ 네트워크성 결과 (csv, json)<br>④ 생활편리성 결과 (csv, json)'))
        lay.addWidget(g)
        lay.addWidget(self._button('일괄 산출', lambda _=False: self._run_kpi(scope='all')))
        lay.addStretch(1)
        return page

    def _page_kpi_basic(self):
        self._kpi_setup(); k = self._kpi
        page = QWidget(); lay = QVBoxLayout(page)
        lay.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN); lay.setSpacing(12)
        lay.addWidget(self._title(
            '<b>① 기초통계</b> — 공통 데이터만으로 산출됩니다(추가 입력 없음).<br>'
            '<span style="color:gray">면적 · 주거/근무 인구 수 · 주거/근무 밀도. '
            '실행하면 결과가 CSV로 저장됩니다.</span>'))
        lay.addWidget(self._title(self._kpi_area_brief('basic')))
        lay.addWidget(self._button('① 기초통계 산출',
                                   lambda _=False: self._run_kpi(scope='basic')))
        lay.addStretch(1)
        return page

    def _page_kpi_compact(self):
        self._kpi_setup(); k = self._kpi
        page = QWidget(); lay = QVBoxLayout(page)
        lay.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN); lay.setSpacing(12)
        lay.addWidget(self._title(
            '<b>② 콤팩트성</b> — 중심지 수·면적·비율·인구·밀도·인구과소지역 비율을 산출.<br>'
            '<span style="color:gray">공통 데이터에 탄소 총배출을 지정하면 1인당 탄소(활동·주거·근무)가 포함됩니다.</span>'))
        lay.addWidget(self._title(self._kpi_area_brief('compact')))
        lay.addWidget(self._button('② 콤팩트성 산출',
                                   lambda _=False: self._run_kpi(scope='compact')))
        lay.addStretch(1)
        return page

    def _page_kpi_network(self):
        self._kpi_setup(); k = self._kpi
        page = QWidget(); lay = QVBoxLayout(page)
        lay.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN); lay.setSpacing(12)
        lay.addWidget(self._title(
            '<b>③ 네트워크성</b> — 중심지 간 연결성(상위·동급 수/비율)·접근 형평성을 산출.<br>'
            '<span style="color:gray">공통 데이터의 도로망으로 중심지 간 연결성을, '
            '격자→중심지 OD로 계획권역·생활권 접근 형평성을 산출합니다.</span>'))
        lay.addWidget(self._title(self._kpi_area_brief('network')))
        lay.addWidget(self._button('③ 네트워크성 산출',
                                   lambda _=False: self._run_kpi(scope='network')))
        lay.addStretch(1)
        return page

    def _page_kpi_life(self):
        self._kpi_setup(); k = self._kpi
        page = QWidget(); lay = QVBoxLayout(page)
        lay.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN); lay.setSpacing(12)
        lay.addWidget(self._title(
            '<b>④ 생활편리성</b> — 천인당 시설수·중심지 내 시설 비율·서비스권 향유도·관리대상 비율·'
            '평균 접근성을 산출.<br>'
            '<span style="color:gray">시설 폴더와 기준은 공통 데이터에서 지정합니다.</span>'))
        lay.addWidget(self._title(self._kpi_area_brief('life')))
        lay.addWidget(self._button('④ 생활편리성 산출',
                                   lambda _=False: self._run_kpi(scope='life')))
        lay.addStretch(1)
        return page

    def _page_kpi_report(self):
        self._kpi_setup(); k = self._kpi
        page = QWidget(); lay = QVBoxLayout(page)
        lay.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN); lay.setSpacing(12)
        lay.addWidget(self._title('<b>종합 리포트 생성</b> — ①~④ 산출 결과를 모아 대시보드 HTML을 만듭니다.'))
        g0 = QGroupBox('산출 결과 (자동 연결)'); f0 = self._form(g0)
        for i, (key, nm) in enumerate(self._KPI_AREAS):
            f0.addRow('%s %s 결과' % ('①②③④'[i], nm), k['rep_' + key])
        lay.addWidget(g0)
        g5 = QGroupBox('출력'); f5 = self._form(g5)
        f5.addRow('HTML 대시보드 저장 경로', k['out_html']); f5.addRow('', k['open_html'])
        lay.addWidget(g5)
        lay.addWidget(self._button('종합 리포트 생성', lambda _=False: self._build_report()))
        lay.addStretch(1)
        return page

    def _build_report(self):
        """①~④ 결과(JSON)를 합쳐 HTML (분석 없음). 있는 결과만으로 만들고, 입력이 서로 다르면 중단."""
        k = self._kpi
        self._link_kpi_json()
        items = [(key, nm, k['rep_' + key].filePath().strip()) for key, nm in self._KPI_AREAS]
        items = [(key, nm, p) for key, nm, p in items if p]
        if not items:
            QMessageBox.warning(self, '입력 확인', '①~④ 산출 또는 일괄 산출을 먼저 실행하세요.')
            return
        lost = [nm for _, nm, p in items if not os.path.isfile(p)]
        if lost:
            QMessageBox.warning(self, '입력 확인', '결과 파일이 없습니다: ' + ' · '.join(lost))
            return
        from .alg_kpi_indicators import _build_html, merge_payloads
        try:
            parts = []
            for key, nm, p in items:
                with open(p, encoding='utf-8') as fp:
                    parts.append((key, nm, json.load(fp)))
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, '종합 리포트', f'결과 파일을 읽을 수 없습니다.\n{e}')
            return
        base_tag = parts[0][2].get('run_tag', '')
        diff = [nm for _, nm, d in parts[1:] if d.get('run_tag', '') != base_tag]
        if diff:
            QMessageBox.warning(self, '입력 확인',
                                f'다음 결과는 {parts[0][1]} 결과와 다른 입력으로 산출되었습니다.\n\n'
                                + ' · '.join(diff) + '\n\n같은 공통 데이터로 다시 실행하세요.')
            return
        out_html = k['out_html'].filePath().strip()
        if not out_html:
            out_html = os.path.join(os.path.dirname(items[0][2]), '공간구조_진단_대시보드.html')
            k['out_html'].setFilePath(out_html)   # 자동 채움
        if not out_html.lower().endswith('.html'):
            out_html += '.html'
        try:
            with open(out_html, 'w', encoding='utf-8') as fp:
                fp.write(_build_html(merge_payloads([(key, d) for key, _, d in parts])))
        except OSError as e:
            QMessageBox.critical(self, '종합 리포트', f'리포트를 만들 수 없습니다.\n{e}')
            return
        self._log.appendPlainText('[완료] KPI 종합 리포트 (%s): %s' % (' · '.join(nm for _, nm, _ in items), out_html))
        self._finish_step('kpi_report')
        if k['open_html'].isChecked():
            webbrowser.open('file:///' + out_html.replace('\\', '/'))

    def _kpi_run_tag(self, params):
        """실행 입력 기록 — 단계별 결과를 합칠 때 같은 입력으로 산출됐는지 확인용."""
        k = self._kpi

        def mtime(p):
            p = (p or '').split('|')[0].strip()
            try:
                return round(os.path.getmtime(p))
            except OSError:
                return None
        files = {key: k[key].filePath().strip() for key in ('grid', 'zones', 'centers', 'region_bnd', 'scen_folder')}
        tag = {'files': files, 'mtime': {key: mtime(p) for key, p in files.items()},
               'params': {key: params.get(key) for key in ('RESID1', 'WORK1', 'GRID_ID', 'ZONE_NAME', 'CENTER_HIER',
                                                         'YEAR1', 'FUT_YEARS', 'FUT_RESID_FIELD')}}
        return json.dumps(tag, ensure_ascii=False, sort_keys=True)

    def _run_kpi(self, scope='all'):
        """KPI 산출 — basic·compact·network·life = 그 단계 CSV, all = 일괄 산출(단계 CSV 4종 + 종합 리포트용 JSON)."""
        k = self._kpi
        missing = self._kpi_missing()
        if missing:
            QMessageBox.warning(self, '입력 확인', '공통 데이터에 다음 데이터를 입력해야 합니다.\n\n'
                                + '\n'.join('· ' + m for m in missing))
            return
        grid = self._layers.get('kpi_grid'); zones = self._layers.get('kpi_zones'); centers = self._layers.get('kpi_centers')
        # 결과 폴더: 결과 저장 폴더\S-Sim_분석결과(지역명)\KPI
        kpi_dir = self._kpi_dir() or tempfile.gettempdir()

        def folder_layers(key):
            d = k[key].filePath().strip()
            if not d or not os.path.isdir(d):
                return []
            return [os.path.join(d, fn) for fn in sorted(os.listdir(d)) if fn.lower().endswith(('.gpkg', '.shp'))]

        region_bnd = k['region_bnd'].filePath().strip() or zones
        params = {
            'REGION_NAME': k['region'].text().strip() or '지역',
            'SIGUNGU': region_bnd, 'ZONES': zones, 'CENTERS': centers, 'GRID1': grid,
            'OUT_ZONES': 'TEMPORARY_OUTPUT', 'OUT_CENTERS': 'TEMPORARY_OUTPUT',
            'SCOPE': scope,
        }

        def put_field(pkey, cb):
            v = cb.currentField()
            if v:
                params[pkey] = v
        put_field('RESID1', k['f_resid']); put_field('WORK1', k['f_work']); put_field('GRID_ID', k['f_gid'])
        put_field('ZONE_NAME', k['f_zone']); put_field('CENTER_HIER', k['f_chier'])
        try:
            params['YEAR1'] = int(k['base_year'].text().strip())
        except ValueError:
            pass
        params['CITY_TYPE'] = k['city_type'].currentIndex()
        params['TREND_TYPE'] = k['trend_type'].currentIndex()
        try:
            params['DENS_REF'] = float(k['dens_ref'].text().strip() or 2000)
        except ValueError:
            pass

        # 단계별 필요한 부수 입력만 전달 (기초통계는 공통데이터만)
        want_net = scope in ('network', 'all')
        want_fac = scope in ('life', 'all')
        want_co2 = scope in ('compact', 'all')
        if want_net:
            net = k['net'].filePath().strip()
            if net:
                params['NET'] = net
            od = k['od'].filePath().strip()
            if od:
                params['OD_MATRIX'] = od
                params['OD_ORIGIN'] = k['od_origin'].currentText().strip() or 'origin_id'
                params['OD_DEST'] = k['od_dest'].currentText().strip() or 'destination_id'
                params['OD_DIST'] = k['od_dist'].currentText().strip() or 'total_cost'
        if scope == 'life':   # 평균 접근성은 도로거리 — 일괄 산출과 같게 도로망 전달(OD는 불필요)
            net = k['net'].filePath().strip()
            if net:
                params['NET'] = net
        if want_fac:
            neigh, hub = folder_layers('fac_neigh_dir'), folder_layers('fac_hub_dir')
            if neigh:
                params['FAC_NEIGH'] = neigh
            if hub:
                params['FAC_HUB'] = hub
            def _numf(key, pk, dv):
                try:
                    params[pk] = float(k[key].text().strip() or dv)
                except ValueError:
                    pass
            _numf('fac_radius', 'FAC_RADIUS', 1500)
            _numf('fac_radius_hub', 'FAC_RADIUS_HUB', 3000)
            _numf('fac_minpop', 'FAC_MINPOP', 500)
            _numf('fac_minpop_hub', 'FAC_MINPOP_HUB', 3000)
        if want_co2:
            co2_src = k['co2_src'].filePath().strip()
            if co2_src:
                params['CO2_SRC'] = co2_src
                params['CO2_JOIN'] = k['co2_join'].currentText().strip() or 'grid_cd'
                params['CO2_VALUE'] = k['co2_value'].currentText().strip() or 'exhst_vl'

        # 장래 추계 시계열 (과거는 내장 데이터로 처리 → 버전 구분 불필요)
        sf = k['scen_folder'].filePath().strip()
        if sf:
            params['SCEN_FOLDER'] = sf
        if k['fut_years'].text().strip():
            params['FUT_YEARS'] = k['fut_years'].text().strip()
        if k['fut_resid'].text().strip():
            params['FUT_RESID_FIELD'] = k['fut_resid'].text().strip()
        params['FUT_WORK_FIELD'] = 'pop_w'      # 시나리오 파일에 근무인구가 있으면 장래 근무 지표도 산출

        labels = {'basic': '기초통계', 'compact': '콤팩트성', 'network': '네트워크성',
                  'life': '생활편리성', 'all': '일괄 산출'}
        params['RUN_TAG'] = self._kpi_run_tag(params)
        # scope별 출력: 단계 = 그 단계 CSV·JSON, 일괄 = 단계별 CSV·JSON 4종씩
        if scope == 'all':
            params['OUT_AREA_DIR'] = kpi_dir
            params['OUT_REGION_CSV'] = os.path.join(tempfile.gettempdir(), 'kpi_region.csv')
        else:
            area_path = os.path.join(kpi_dir, '%s_결과.csv' % labels[scope])   # 파일 이름은 메뉴의 한글 이름 그대로
            json_path = os.path.join(kpi_dir, '%s_결과.json' % labels[scope])
            params['OUT_AREA_CSV'] = area_path
            params['OUT_JSON'] = json_path

        navmap = {'basic': 'kpi_basic', 'compact': 'kpi_compact', 'network': 'kpi_net', 'life': 'kpi_life'}
        nextmap = {'basic': 'kpi_compact', 'compact': 'kpi_net', 'network': 'kpi_life', 'life': None}
        self._log.appendPlainText('━' * 50)
        self._log.appendPlainText('[KPI] %s 시작' % labels[scope])
        result = self._run('krihs_ssim:spatial_structure_indicators_kpi', params)
        if not result:
            return
        if not k['batch_dir'].filePath().strip():
            k['batch_dir'].setFilePath(kpi_dir)          # 자동 채움
        if scope == 'all':
            for key, nm in self._KPI_AREAS:               # 종합 리포트 네 칸 모두 연결
                k['rep_' + key].setFilePath(os.path.join(kpi_dir, nm + '_결과.json'))
            for key in ('kpi_basic', 'kpi_compact', 'kpi_net', 'kpi_life'):
                self._finish_step(key)
            self._log.appendPlainText(f'[완료] 일괄 산출 — 단계별 결과 CSV·JSON 4종: {kpi_dir}')
            self._finish_step('kpi_batch', 'kpi_report')  # ✓ + 종합 리포트로 이동
        else:
            k['rep_' + scope].setFilePath(json_path)      # 종합 리포트의 해당 칸에 연결
            self._finish_step(navmap[scope], nextmap[scope])
            self._log.appendPlainText('[완료] %s 결과: %s · %s' % (labels[scope], area_path, json_path))

    def _page_bau(self, d):
        w = QWidget()
        form = self._form(w)
        kind = '거주인구' if d == RES else '근무인구'
        form.addRow(self._title(
            '<b>BAU 배분</b> — 현상 유지 시나리오.<br>'
            '<span style="color:gray">계산 방식은 [산식 보기] 버튼 참고</span>'))
        self.bau_out[d] = self._save_widget('GeoPackage (*.gpkg)')
        form.addRow('결과 저장 파일', self.bau_out[d])
        form.addRow(self._action_row(
            'bau', 'BAU 배분 실행', lambda _=False, dd=d: self._run_bau(dd)))
        return w

    # ── 페이지: 거점유도 (거주 전용) ──────────────────────
    def _page_hub(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        lay.setSpacing(14)

        # 2-1. 정책관심구역
        g1 = QGroupBox('정책관심구역')
        f1 = self._form(g1)

        self.hub_gid_cb = QgsFieldComboBox()
        self.hub_file = self._open_widget(
            lambda p: self._load_layer('hub', p, [(self.hub_gid_cb, 'gid')]))
        f1.addRow(self._title(
            '<b>기존 파일 이용</b> — 기존 정책관심구역 파일을 선택.'))
        f1.addRow('정책관심구역 파일', self._file_row(self.hub_file, 'hub'))
        f1.addRow('  ↳ 격자 ID 필드', self.hub_gid_cb)

        f1.addRow(self._title(
            '<b>새로 산출</b> — 거점 유형 ∪ 생활인프라 충족도. '
            '산출 결과가 위 파일로 자동 연결됨.'))
        type_row = QHBoxLayout()
        type_row.setSpacing(12)
        self.type_checks = []
        for i, name in enumerate(TYPE_OPTIONS):
            cb = QCheckBox(name)
            cb.setChecked(i > 0)   # 중심지 4종 기본 선택
            self.type_checks.append(cb)
            type_row.addWidget(cb)
        type_row.addStretch()
        f1.addRow('거점 유형', type_row)

        self.infra_file = self._open_widget(
            lambda p: self._load_layer('infra', p, []))
        f1.addRow('생활인프라 파일 (선택)', self._file_row(self.infra_file, 'infra'))

        ci_row = QHBoxLayout()
        ci_row.setSpacing(10)
        self.ci1_use = QCheckBox('마을시설(Me_CI_1) ≥')
        self.ci1_use.setChecked(True)
        self.ci1_min = QDoubleSpinBox()
        self.ci1_min.setRange(0.0, 10.0)
        self.ci1_min.setValue(3.0)
        self.ci2_use = QCheckBox('거점시설(Me_CI_2) ≥')
        self.ci2_use.setChecked(True)
        self.ci2_min = QDoubleSpinBox()
        self.ci2_min.setRange(0.0, 10.0)
        self.ci2_min.setValue(3.0)
        for x in (self.ci1_use, self.ci1_min, self.ci2_use, self.ci2_min):
            ci_row.addWidget(x)
        ci_row.addStretch()
        f1.addRow('충족도 기준', ci_row)

        self.policy_out = self._save_widget('GeoPackage (*.gpkg)')
        f1.addRow('정책관심구역 저장 파일', self.policy_out)
        f1.addRow(self._action_row('policy', '정책관심구역 산출', self._run_policy))
        lay.addWidget(g1)

        # 2-2. 배분
        g2 = QGroupBox('거점(정책관심구역) 유도 배분')
        f2 = self._form(g2)
        exp_row = QHBoxLayout()
        exp_row.setSpacing(10)
        self.exp_spins = {}
        for key, label, default in (
            ('PARAM_A', 'a 거점·多', 1.1),
            ('PARAM_B', 'b 거점·少', 0.9),
            ('PARAM_C', 'c 비거점·多', 0.9),
            ('PARAM_D', 'd 비거점·少', 1.1),
        ):
            sp = QDoubleSpinBox()
            sp.setRange(0.01, 10.0)
            sp.setSingleStep(0.05)
            sp.setValue(default)
            self.exp_spins[key] = sp
            exp_row.addWidget(QLabel(label))
            exp_row.addWidget(sp)
        exp_row.addStretch()
        f2.addRow('지수 계수', exp_row)
        f2.addRow(self._title(
            '<span style="color:gray">a. 거점·평균↑   b. 거점·평균↓   '
            'c. 비거점·평균↑   d. 비거점·평균↓</span>'))
        self.hub_out = self._save_widget('GeoPackage (*.gpkg)')
        f2.addRow('결과 저장 파일', self.hub_out)
        f2.addRow(self._action_row('hub', '거점유도 배분 실행', self._run_hub,
                                   desc_key='hub'))
        lay.addWidget(g2)
        lay.addStretch()
        return w

    # ── 페이지: 계획인구/계획반영 (도메인별) ──────────────
    def _page_plan(self, d):
        w = QWidget()
        form = self._form(w)
        kind = '거주인구' if d == RES else '근무인구'
        form.addRow(self._title(
            '<b>계획인구 반영 배분</b> — 개발계획 반영 시나리오.<br>'
            '<span style="color:gray">입력 형식은 [형식] 버튼, 계산 방식은 '
            '[산식 보기] 버튼 참고</span>'))

        self.z_pop_cb[d] = QgsFieldComboBox()
        self.z_pop_cb[d].setFilters(QgsFieldProxyModel.Numeric)
        self.z_yr_cb[d] = QgsFieldComboBox()
        self.z_prj_cb[d] = QgsFieldComboBox()
        self.z_prj_cb[d].setFilters(QgsFieldProxyModel.Numeric)
        self.z_prj_cb[d].setAllowEmptyFieldName(True)
        self.z_sgg_cb[d] = QgsFieldComboBox()
        self.z_sgg_cb[d].setAllowEmptyFieldName(True)
        self.zone_file[d] = self._open_widget(
            lambda p, dd=d: self._load_layer(f'{dd}_zone', p, [
                (self.z_pop_cb[dd], 'plan_pop'), (self.z_yr_cb[dd], 'yr_done'),
                (self.z_prj_cb[dd], 'prj_pop'), (self.z_sgg_cb[dd], 'sgg_cd_h')]))

        form.addRow('개발사업 지구 파일', self._file_row(self.zone_file[d], 'zone'))
        form.addRow('  ↳ 계획인구 필드', self.z_pop_cb[d])
        form.addRow('  ↳ 준공연도 필드', self.z_yr_cb[d])
        form.addRow('  ↳ 사업 전체 인구합 필드 (선택)', self.z_prj_cb[d])
        form.addRow('  ↳ 관할 시군구 코드 필드 (선택)', self.z_sgg_cb[d])

        if d == WORK:
            self.w_ratio_use = QCheckBox(
                '개발지구 근무 원단위 적용 — 유입 = 계획인구 × 시군별 (근무인구 합계/주거인구 합계)')
            self.w_ratio_use.setChecked(True)
            form.addRow(self.w_ratio_use)
            self.w_res_cb = QgsFieldComboBox()
            self.w_res_cb.setFilters(QgsFieldProxyModel.Numeric)
            grid = self._layers.get(f'{WORK}_grid')
            if grid is not None:
                self.w_res_cb.setLayer(grid)
                self.w_res_cb.setField('pop_r')
            form.addRow('  ↳ 원단위 분모(주거인구) 필드', self.w_res_cb)

        self.plan_out[d] = self._save_widget('GeoPackage (*.gpkg)')
        form.addRow('결과 저장 파일', self.plan_out[d])
        self.plan_csv[d] = self._save_widget('CSV (*.csv)')
        form.addRow('시군별 개발/비개발지 배분 인구' if d == WORK else '개발/비개발 분배 CSV',
                    self.plan_csv[d])
        form.addRow(self._action_row(
            'plan', '계획인구 반영 배분 실행', lambda _=False, dd=d: self._run_plan(dd)))

        if d == WORK:
            form.addRow(self._title(
                '<br><b>연도별 개발지구 장래 근무인구 산출</b> — 격자 배분 없이 '
                '개발지구 근무인구 총량만 산출.<br>'
                '<span style="color:gray">계산 방식은 [산식 보기] 버튼 참고</span>'))
            self.devw_csv = self._save_widget('CSV (*.csv)')
            form.addRow('산출 CSV 저장 파일', self.devw_csv)
            form.addRow(self._action_row(
                'devw', '개발지구 장래 근무인구 산출', self._run_devworkers))
        return w

    def _run_devworkers(self):
        from . import core
        d = WORK
        grid = self._layers.get(f'{d}_grid')
        zone = self._layers.get(f'{d}_zone')
        if grid is None:
            QMessageBox.warning(self, '입력 확인',
                                '근무인구 배분 > 공통 데이터에서 격자 파일을 선택하세요.')
            return
        if zone is None:
            QMessageBox.warning(self, '입력 확인', '개발사업 지구 파일을 선택하세요.')
            return
        try:
            table, years, _ = core.load_sgg_table(self._table_path(d))
        except ValueError as e:
            QMessageBox.warning(self, '입력 확인', f'근무 추계 테이블 오류: {e}')
            return

        # 시군별 원단위 = Σpop_w / Σpop_r (격자에서 계산)
        sgg_field = self.sgg_cb[d].currentField() or 'sgg_cd_h'
        pop_w_field = self.pop_cb[d].currentField() or 'pop_w'
        res_field = self.w_res_cb.currentField() if hasattr(self, 'w_res_cb') else 'pop_r'
        res_field = res_field or 'pop_r'
        num, den = {}, {}
        for feat in grid.getFeatures():
            try:
                sgg = int(str(feat[sgg_field]).strip())
                num[sgg] = num.get(sgg, 0.0) + float(feat[pop_w_field])
                den[sgg] = den.get(sgg, 0.0) + float(feat[res_field])
            except (TypeError, ValueError):
                continue
        unit = {s: num[s] / den[s] for s in num if den.get(s, 0) > 0}
        overall = sum(num.values()) / sum(den.values()) if sum(den.values()) > 0 else 1.0

        self._log.appendPlainText('━' * 50)
        self._log.appendPlainText('[근무] 개발지구 장래 근무인구 산출 (표 2-9)')
        f_pop = self.z_pop_cb[d].currentField() or 'plan_pop'
        f_yr = self.z_yr_cb[d].currentField() or 'yr_done'
        f_prj = self.z_prj_cb[d].currentField()
        f_sgg = self.z_sgg_cb[d].currentField()
        dev = {}          # {sgg: {yr: 근무인구}}
        n_use = n_skip = n_nosgg = 0
        dropped = 0.0
        for zf in zone.getFeatures():
            try:
                pp = float(zf[f_pop])
            except (TypeError, ValueError):
                pp = 0.0
            if pp <= 0:
                n_skip += 1
                continue
            try:
                yd = int(str(zf[f_yr]).strip())
            except (TypeError, ValueError):
                n_skip += 1
                continue
            band = None
            if f_prj:
                try:
                    band = float(zf[f_prj])
                except (TypeError, ValueError):
                    band = None
            zsgg = None
            if f_sgg and zf[f_sgg] is not None and str(zf[f_sgg]).strip():
                try:
                    zsgg = int(str(zf[f_sgg]).strip())
                except (TypeError, ValueError):
                    zsgg = None
            k = unit.get(zsgg, overall)
            if zsgg is None or zsgg not in unit:
                n_nosgg += 1
            for y, v in core.entry_schedule(pp, yd, band).items():
                t = core.target_of(y, years)
                if t is None:
                    dropped += v * k
                    continue
                dev.setdefault(zsgg if zsgg is not None else -1, {})
                dev[zsgg if zsgg is not None else -1][t] = \
                    dev[zsgg if zsgg is not None else -1].get(t, 0.0) + v * k
            n_use += 1

        if not dev:
            QMessageBox.warning(self, '산출 실패', '반영 가능한 개발사업이 없습니다.')
            return
        if n_nosgg:
            self._log.appendPlainText(
                f'[경고] 관할 시군 미지정/미매칭 사업 {n_nosgg}건 → 전체 평균 원단위 {overall:.2f} 적용')

        out = self._out_path(self.devw_csv, f'{d}_개발지구_장래근무인구.csv', d)
        import csv as _csv
        with open(out, 'w', encoding='utf-8-sig', newline='') as f:
            wtr = _csv.writer(f)
            wtr.writerow(['sgg_cd_h'] + [f'dev_w_{y}' for y in years])
            for sgg in sorted(dev):
                wtr.writerow([sgg if sgg != -1 else '미지정']
                             + [round(dev[sgg].get(y, 0.0), 1) for y in years])
        # 표 2-9 형태: 연도별 증분·누적
        cum = 0.0
        for y in years:
            inc = sum(v.get(y, 0.0) for v in dev.values())
            cum += inc
            self._log.appendPlainText(f'  {y}년: 증분 {inc:,.0f}명 / 누적 {cum:,.0f}명')
        self._log.appendPlainText(
            f'  사업 {n_use}건 반영, {n_skip}건 제외'
            + (f', 목표연도 이후 유입 {dropped:,.0f}명 반영 불가' if dropped > 0 else ''))
        self._log.appendPlainText(f'  → CSV 저장: {out}')

    # ── 페이지: 3D 시각화 (공통) ──────────────────────────
    # ── 페이지: 격자 통계 탐색 ────────────────────────────
    _EXP_STATS = ['유효 N', 'NULL', '0 개수', '합계', '평균', '표준편차',
                  '0 제외 평균', '최소', 'Q1', '중앙값', 'Q3', '최대',
                  '왜도', '첨도']

    @staticmethod
    def _moments(vals):
        """(평균, 왜도, 초과첨도) — 모멘트 기반. 값 부족·분산 0이면 None."""
        n = len(vals)
        if n < 3:
            return None, None
        m = sum(vals) / n
        m2 = sum((x - m) ** 2 for x in vals) / n
        if m2 <= 0:
            return None, None
        m3 = sum((x - m) ** 3 for x in vals) / n
        m4 = sum((x - m) ** 4 for x in vals) / n
        return m3 / m2 ** 1.5, m4 / m2 ** 2 - 3

    def _page_explore(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        lay.setSpacing(10)
        lay.addWidget(self._title(
            '<b>격자 통계 탐색</b> — 격자(벡터·테이블) 파일의 숫자 필드 '
            '기초통계를 탐색적으로 확인.<br><span style="color:gray">'
            '평균·표준편차·0 제외 평균·사분위 등 — 원천 격자·배분 결과 등 '
            '어떤 파일이든 가능</span>'))

        form = QFormLayout()
        form.setVerticalSpacing(V_SPACE)
        form.setHorizontalSpacing(H_SPACE)
        self.exp_fields = QgsCheckableComboBox()
        self.exp_file = self._open_widget(self._explore_file_changed)
        form.addRow('격자/테이블 파일', self.exp_file)
        frow = QHBoxLayout()
        frow.setSpacing(8)
        frow.addWidget(self.exp_fields, 1)
        btn_all = QPushButton('숫자 필드 전체 선택')
        btn_all.clicked.connect(self._explore_check_all)
        frow.addWidget(btn_all)
        form.addRow('통계 필드 (복수 선택)', frow)
        lay.addLayout(form)
        lay.addWidget(self._button('통계 계산', self._run_explore))

        grow = QHBoxLayout()
        grow.setSpacing(8)
        gbtn = QPushButton('분포 그래프 (표에서 필드 행 선택)')
        gbtn.setToolTip('히스토그램 + 같은 평균·표준편차의 정규분포 곡선')
        gbtn.clicked.connect(self._explore_graph)
        self.exp_zero_excl = QCheckBox('0 제외')
        grow.addWidget(gbtn)
        grow.addWidget(self.exp_zero_excl)
        grow.addStretch()
        lay.addLayout(grow)

        self.exp_table = QTableWidget()
        self.exp_table.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(self.exp_table, 1)
        return w

    def _explore_file_changed(self, path):
        layer = self._load_layer('explore', path, [])
        self.exp_fields.clear()
        if layer is not None:
            self.exp_fields.addItems(
                [f.name() for f in layer.fields() if f.isNumeric()])

    def _explore_check_all(self):
        items = [self.exp_fields.itemText(i)
                 for i in range(self.exp_fields.count())]
        self.exp_fields.setCheckedItems(items)

    def _run_explore(self):
        layer = self._layers.get('explore')
        if layer is None:
            QMessageBox.warning(self, '입력 확인', '격자/테이블 파일을 선택하세요.')
            return
        fields = self.exp_fields.checkedItems()
        if not fields:
            QMessageBox.warning(self, '입력 확인', '통계 필드를 1개 이상 선택하세요.')
            return

        data = {f: [] for f in fields}
        nulls = {f: 0 for f in fields}
        for feat in layer.getFeatures():
            for f in fields:
                try:
                    data[f].append(float(feat[f]))
                except (TypeError, ValueError):
                    nulls[f] += 1
        self._exp_data = data          # 분포 그래프에서 재사용
        self._exp_row_fields = fields

        t = self.exp_table
        t.clear()
        t.setColumnCount(len(self._EXP_STATS))
        t.setHorizontalHeaderLabels(self._EXP_STATS)
        t.setRowCount(len(fields))
        t.setVerticalHeaderLabels(fields)

        def fmt(x):
            if x is None:
                return '—'
            if float(x).is_integer():
                return f'{int(x):,}'
            return f'{x:,.2f}'

        for r, f in enumerate(fields):
            vals = sorted(data[f])
            n = len(vals)
            nz = [v for v in vals if v != 0]

            def q(p):
                if not n:
                    return None
                k = (n - 1) * p
                lo = int(k)
                hi = min(lo + 1, n - 1)
                return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)

            skew, kurt = self._moments(vals)
            row = [n, nulls[f], n - len(nz),
                   sum(vals) if n else None,
                   (sum(vals) / n) if n else None,
                   statistics.stdev(vals) if n > 1 else None,
                   (sum(nz) / len(nz)) if nz else None,
                   vals[0] if n else None, q(0.25), q(0.5), q(0.75),
                   vals[-1] if n else None, skew, kurt]
            for c, v in enumerate(row):
                item = QTableWidgetItem(fmt(v))
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                t.setItem(r, c, item)
        t.resizeColumnsToContents()
        self._log.appendPlainText(
            f'[탐색] {layer.name()} — 필드 {len(fields)}개 · '
            f'피처 {layer.featureCount():,}개 통계 계산')

    def _explore_graph(self):
        data = getattr(self, '_exp_data', None)
        if not data:
            QMessageBox.warning(self, '안내', '먼저 [통계 계산]을 실행하세요.')
            return
        fields = self._exp_row_fields
        r = self.exp_table.currentRow()
        field = fields[r] if 0 <= r < len(fields) else fields[0]
        vals = data[field]
        if self.exp_zero_excl.isChecked():
            vals = [v for v in vals if v != 0]
        if len(vals) < 3:
            QMessageBox.warning(self, '안내', f'{field}: 그래프를 그릴 값이 부족합니다.')
            return
        try:
            import matplotlib
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
        except ImportError:
            QMessageBox.warning(
                self, '안내',
                'matplotlib이 설치되어 있지 않습니다.\n'
                'OSGeo4W 설치 관리자에서 python3-matplotlib을 추가하세요.')
            return
        import math
        matplotlib.rcParams['font.family'] = ['Malgun Gothic', 'sans-serif']
        matplotlib.rcParams['axes.unicode_minus'] = False

        n = len(vals)
        m = sum(vals) / n
        s = statistics.stdev(vals)
        skew, kurt = self._moments(vals)

        fig = Figure(figsize=(7.2, 4.6))
        ax = fig.add_subplot(111)
        ax.hist(vals, bins=50, density=True, color='#7ca7d8',
                edgecolor='white', alpha=.9, label='실제 분포')
        if s > 0:
            lo, hi = min(vals), max(vals)
            xs = [lo + (hi - lo) * i / 200 for i in range(201)]
            ys = [math.exp(-((x - m) ** 2) / (2 * s * s))
                  / (s * math.sqrt(2 * math.pi)) for x in xs]
            ax.plot(xs, ys, color='#c2620e', linewidth=2,
                    label='정규분포 (같은 평균·표준편차)')
        ax.axvline(m, color='#c2620e', linestyle='--', linewidth=1)
        sk = '—' if skew is None else f'{skew:.2f}'
        ku = '—' if kurt is None else f'{kurt:.2f}'
        ax.set_title(f'{field} — N {n:,} · 평균 {m:,.1f} · 표준편차 {s:,.1f} · '
                     f'왜도 {sk} · 첨도 {ku}', fontsize=10)
        ax.legend(fontsize=9)
        fig.tight_layout()

        dlg = QDialog(self)
        dlg.setWindowTitle(f'분포 그래프 — {field}'
                           + (' (0 제외)' if self.exp_zero_excl.isChecked() else ''))
        v = QVBoxLayout(dlg)
        v.addWidget(FigureCanvasQTAgg(fig))
        dlg.resize(780, 540)
        dlg.exec_()

    def _page_viz(self):
        w = QWidget()
        form = self._form(w)
        form.addRow(self._title(
            '<b>장래인구 격자 배분 결과 시계열 3D 보기 (deck.gl)</b> — 결과 파일(pop_YYYY 컬럼)을 '
            '단일 HTML로 내보내 브라우저에서 연도·시나리오를 전환하면서 결과를 탐색합니다.<br>'
            '<span style="color:gray">각 단계 실행 시 결과 파일이 아래에 자동 입력됨. '
            '비어 있는 시나리오는 제외. 배경지도·deck.gl은 온라인 로드</span>'))

        self.viz_files = {}
        for key, label in ((f'{RES}_BAU', '거주 ① BAU 결과'),
                           (f'{RES}_거점유도', '거주 ② 거점유도 결과'),
                           (f'{RES}_계획인구', '거주 ③ 계획인구 반영 결과'),
                           (f'{WORK}_BAU', '근무 ① BAU 결과'),
                           (f'{WORK}_계획반영', '근무 ② 계획인구 반영 결과'),
                           ('중심지_식별', '분석 중심지 식별 결과 (폴리곤)')):
            fw = QgsFileWidget()
            fw.setFilter(VECTOR_FILTER)
            fw.setMinimumHeight(28)
            self.viz_files[key] = fw
            form.addRow(label, fw)

        self.viz_emd_name = QgsFieldComboBox()
        self.viz_emd = self._open_widget(
            lambda p: self._load_layer('viz_emd', p, [(self.viz_emd_name, 'ADM_DR_NM')]))
        form.addRow('읍면동 경계 (선택, 지명 표시용)', self._file_row(self.viz_emd, 'sp_emd'))
        form.addRow('  ↳ 읍면동 이름 필드', self.viz_emd_name)

        self.viz_vworld_key = QLineEdit('C0F7281E-922B-40AA-BA40-BB86FE9184B1')
        self.viz_vworld_key.setToolTip(
            'V-World 인증키(기타/QGIS). 비우면 배경지도 없이 검정/회색 배경 + 읍면동 경계만 표시.')
        form.addRow('인증키 (선택)', self.viz_vworld_key)
        self.viz_vworld_layer = QComboBox()
        for lbl in ('일반 (Base)', '회색 (gray)', '야간 (midnight)', '위성 (Satellite)'):
            self.viz_vworld_layer.addItem(lbl)
        form.addRow('  ↳ 배경지도 종류', self.viz_vworld_layer)

        self.viz_out = self._save_widget('HTML (*.html)')
        form.addRow('HTML 저장 파일', self.viz_out)
        form.addRow(self._button('3D 시각화 생성 후 브라우저 열기', self._run_viz))
        return w

    def _run_viz(self):
        from . import viz3d
        scenarios = []
        try:
            for key, fw in self.viz_files.items():
                path = fw.filePath().strip()
                if not path:
                    continue
                if key == '중심지_식별':
                    years, items = viz3d.extract_centers(path)
                    scenarios.append({'name': '중심지 식별', 'type': 'center',
                                      'years': years, 'items': items})
                    self._log.appendPlainText(
                        f'[3D] {key}: 폴리곤 {len(items):,}개, 연도 {years}')
                    continue
                years, points = viz3d.extract_series(path)
                scenarios.append({'name': key.replace('_', ' '), 'type': 'grid',
                                  'years': years, 'points': points})
                self._log.appendPlainText(
                    f'[3D] {key}: 격자 {len(points):,}개, 연도 {years}')
        except ValueError as e:
            QMessageBox.warning(self, '입력 확인', str(e))
            return
        if not scenarios:
            QMessageBox.warning(self, '입력 확인',
                                '배분 결과 파일을 하나 이상 선택하세요.')
            return
        root = self._analysis_root(RES) or self._analysis_root(WORK)
        if not self.viz_out.filePath().strip() and root:
            os.makedirs(os.path.join(root, OUT_FUT), exist_ok=True)
            self.viz_out.setFilePath(os.path.join(root, OUT_FUT, '인구배분_3D.html'))
        out = self._out_path(self.viz_out, '인구배분_3D.html', RES)
        # 분석영역 bbox (WGS84) — 읍면동 클립용
        xs, ys = [], []
        for s in scenarios:
            if s.get('type') == 'center':
                for it in s.get('items', []):
                    for ring in it[3]:
                        for pt in ring:
                            xs.append(pt[0]); ys.append(pt[1])
            else:
                for p in s.get('points', []):
                    xs.append(p[0]); ys.append(p[1])
        bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else None
        emd_geo = None
        emd_path = self.viz_emd.filePath().strip()
        if emd_path:
            try:
                emd_geo = viz3d.emd_to_geojson(
                    emd_path, self.viz_emd_name.currentField() or '', clip_bbox=bbox)
                self._log.appendPlainText(
                    f'[3D] 읍면동 경계 {len(emd_geo["features"]):,}개 오버레이(분석영역 클립·단순화)')
            except ValueError as e:
                QMessageBox.warning(self, '읍면동 경계', str(e))
                return
        # V-World 인증키 → WMTS 타일 URL (레이어별 코드·확장자)
        bg_url = ''
        vkey = self.viz_vworld_key.text().strip()
        if vkey:
            layer, ext = {
                0: ('Base', 'png'), 1: ('gray', 'png'),
                2: ('midnight', 'png'), 3: ('Satellite', 'jpeg'),
            }[self.viz_vworld_layer.currentIndex()]
            bg_url = (f'https://api.vworld.kr/req/wmts/1.0.0/{vkey}/{layer}/'
                      + '{z}/{y}/{x}.' + ext)
        # 요약지표(그림 4-14) — 중심지 식별 + (거주·근무 배분결과)로 산출
        summary = None
        cpath = self.viz_files['중심지_식별'].filePath().strip()
        if cpath:
            def _first(*keys):
                for k in keys:
                    p = self.viz_files[k].filePath().strip()
                    if p:
                        return p
                return None
            res_p = _first(f'{RES}_계획인구', f'{RES}_거점유도', f'{RES}_BAU')
            work_p = _first(f'{WORK}_계획반영', f'{WORK}_BAU')
            try:
                summary = viz3d.summarize_centers(cpath, res_p, work_p)
                if summary:
                    self._log.appendPlainText(
                        f'[3D] 요약지표 산출: 연도 {summary["years"]}, 중심지 수 {summary["center_count"]}')
            except Exception as e:
                self._log.appendPlainText(f'[3D] 요약지표 산출 건너뜀: {e}')
        try:
            viz3d.build_html(scenarios, out, emd_geo, basemap_url=bg_url, summary=summary)
        except OSError as e:
            QMessageBox.critical(self, '저장 실패', str(e))
            return
        self._log.appendPlainText(f'[3D] HTML 저장: {out}')
        webbrowser.open('file:///' + out.replace('\\', '/'))

    # ── 공통 헬퍼 ─────────────────────────────────────────
    def _analysis_root(self, d):
        """입력 격자 폴더와 같은 위계의 '분석결과' 폴더 경로 (없으면 None).

        예: ...\\충남표준\\거주인구 배분용\\격자.shp → ...\\충남표준\\분석결과
        """
        rr = self._region_result_root()      # 지역 설정 적용 시: 결과 저장 폴더\S-Sim_분석결과(지역명)
        if rr:
            return rr
        grid = self._layers.get(f'{d}_grid')
        if grid is None:
            return None
        src = grid.source().split('|')[0]
        if not os.path.isfile(src):
            return None
        return os.path.join(os.path.dirname(os.path.dirname(src)), '분석결과')

    def _unified_result_root(self, grid_path):
        """모든 분석 결과를 모을 공용 '분석결과' 폴더(data\\분석결과) — 격자 경로에서 유추.

        입력 폴더(…배분용 / 생활권분석 / KPI 산출용)를 이름으로 찾아 그 부모의 분석결과 반환.
        생활권 분석 격자는 번호 하위폴더(01_…) 안이라 한 단계 더 깊은 것을 자동 보정.
        """
        rr = self._region_result_root()      # 지역 설정 적용 시: 결과 저장 폴더\S-Sim_분석결과(지역명)
        if rr:
            return rr
        grid_path = (grid_path or '').split('|')[0].strip()
        if not grid_path:
            return None
        cur = os.path.dirname(grid_path)
        for _ in range(5):
            base = os.path.basename(cur)
            if base.endswith('배분용') or base == '생활권분석' or base.startswith('KPI'):
                return os.path.join(os.path.dirname(cur), '분석결과')
            parent = os.path.dirname(cur)
            if not parent or parent == cur:
                break
            cur = parent
        # 폴백: 격자 폴더의 상위\분석결과
        return os.path.join(os.path.dirname(os.path.dirname(grid_path)), '분석결과')

    def _default_dir(self, d):
        root = self._analysis_root(d)
        if root:
            out = os.path.join(root, out_alloc(d))
            os.makedirs(out, exist_ok=True)
            return out
        home = QgsProject.instance().homePath()
        return home or tempfile.gettempdir()

    def _out_path(self, widget, default_name, d):
        path = widget.filePath().strip()
        if not path:
            path = os.path.join(self._default_dir(d), default_name)
            widget.setFilePath(path)
        return path

    def _grid_params(self, d):
        grid = self._layers.get(f'{d}_grid')
        if grid is None:
            raise ValueError(f'{d}인구 배분 > 공통 데이터에서 격자 파일을 선택하세요.')
        return {
            'INPUT': grid,
            'GID_FIELD': self.gid_cb[d].currentField(),
            'SGG_FIELD': self.sgg_cb[d].currentField(),
            'POP_FIELD': self.pop_cb[d].currentField(),
        }

    def _table_path(self, d):
        path = self.table_file[d].filePath().strip()
        if not path:
            if d == WORK:
                raise ValueError('근무인구 배분 > 근무인구 추계를 먼저 실행하세요.')
            raise ValueError(f'{d}인구 배분 > 공통 데이터에서 추계 테이블 파일을 선택하세요.')
        return path

    def _run(self, alg_id, params):
        self._log.appendPlainText('━' * 50)
        self._bar.setValue(0)
        fb = _Feedback(self._log, self._bar)
        # 실행 중 조작 잠금 (processEvents로 UI는 살아 있으므로 중복 클릭 방지)
        self.nav.setEnabled(False)
        self.stack.setEnabled(False)
        try:
            # QGIS 3.34+의 QgsField 생성자 DeprecationWarning이 로그를 도배 —
            # 동작 무해하므로 실행 중에는 억제
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', DeprecationWarning)
                result = processing.run(alg_id, params, feedback=fb)
        except Exception as e:                       # QgsProcessingException 포함
            self._log.appendPlainText(f'[오류] {e}')
            QMessageBox.critical(self, '실행 실패', str(e))
            self._bar.reset()
            return None
        finally:
            self.nav.setEnabled(True)
            self.stack.setEnabled(True)
        self._bar.reset()   # 완료 후 진행바 비움 (100% 잔류 방지)
        return result

    def _add_layer(self, path, name):
        layer = QgsVectorLayer(path, name, 'ogr')
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
        else:
            self._log.appendPlainText(f'[경고] 결과 레이어 로드 실패: {path}')
        return layer

    def _add_result_group(self, path, group_name):
        """결과를 레이어 그룹으로 추가 — 연도별 레이어 1개씩, 고정 구간 단계구분도.

        구간이 전 단계·전 연도 공통이라 어떤 조합이든 같은 색 = 같은 인구 구간.
        마지막 연도만 켠 상태로 추가.
        """
        base = QgsVectorLayer(path, group_name, 'ogr')
        if not base.isValid():
            self._log.appendPlainText(f'[경고] 결과 레이어 로드 실패: {path}')
            return None
        years = sorted(int(m.group(1)) for m in
                       (re.match(r'^pop_(\d{4})$', f.name()) for f in base.fields()) if m)
        if not years:
            QgsProject.instance().addMapLayer(base)
            return None

        ramp = QgsStyle.defaultStyle().colorRamp('YlOrRd')
        ranges = []
        n = len(self.CLASS_BREAKS)
        for i, (lo, hi) in enumerate(self.CLASS_BREAKS):
            symbol = QgsSymbol.defaultSymbol(base.geometryType())
            if ramp:
                symbol.setColor(ramp.color(i / (n - 1)))
            label = f'{lo:,} ~' if hi >= 10**9 else f'{lo:,} ~ {hi:,}'
            ranges.append(QgsRendererRange(lo, hi, symbol, label))
        renderer = QgsGraduatedSymbolRenderer(f'pop_{years[-1]}', ranges)

        root = QgsProject.instance().layerTreeRoot()
        old = root.findGroup(group_name)
        if old is not None:
            root.removeChildNode(old)
        group = root.insertGroup(0, group_name)
        for y in years:
            lyr = QgsVectorLayer(path, f'{y}년', 'ogr')
            r = renderer.clone()
            r.setClassAttribute(f'pop_{y}')
            lyr.setRenderer(r)
            QgsProject.instance().addMapLayer(lyr, False)
            node = group.addLayer(lyr)
            node.setItemVisibilityChecked(y == years[-1])
        self._log.appendPlainText(
            f'[스타일] {group_name} 그룹: 연도별 단계구분도 {len(years)}개 '
            f'(고정 구간 0/100/200/500/1000/5000)')
        return group

    def _add_outline(self, source_layer, name, color, group=None):
        """소스 폴리곤을 dissolve → 채움 없음·외곽선만인 레이어로 지도에 추가.

        group을 주면 해당 레이어 그룹 맨 위에 넣는다.
        """
        ctx = QgsProcessingContext()   # 컨텍스트 유지 — 메모리 결과 레이어 소유권 회수용
        try:
            out = processing.run(
                'native:dissolve',
                {'INPUT': source_layer, 'OUTPUT': 'memory:'},
                context=ctx)['OUTPUT']
        except Exception as e:
            self._log.appendPlainText(f'[경고] {name} dissolve 실패: {e}')
            return
        dissolved = ctx.takeResultLayer(out) if isinstance(out, str) else out
        if dissolved is None or not dissolved.isValid():
            self._log.appendPlainText(f'[경고] {name} dissolve 결과 레이어가 유효하지 않음')
            return
        dissolved.setName(name)
        symbol = QgsFillSymbol.createSimple({
            'style': 'no',
            'outline_color': color,
            'outline_width': '0.9',
        })
        dissolved.setRenderer(QgsSingleSymbolRenderer(symbol))
        if group is not None:
            QgsProject.instance().addMapLayer(dissolved, False)
            group.insertLayer(0, dissolved)   # 그룹 맨 위 = 결과 위에 그려짐
        else:
            QgsProject.instance().addMapLayer(dissolved)
        self._log.appendPlainText(f'[스타일] {name} 경계 표시 (dissolve, 채움 없음)')

    # ── 실행 핸들러 ───────────────────────────────────────
    def _run_bau(self, d):
        try:
            params = {**self._grid_params(d), 'TABLE': self._table_path(d)}
        except ValueError as e:
            QMessageBox.warning(self, '입력 확인', str(e))
            return
        params['OUTPUT'] = self._out_path(self.bau_out[d], f'{d}배분_1_BAU.gpkg', d)
        if self._run('krihs_ssim:bau_grid_allocate', params):
            self._add_result_group(params['OUTPUT'], f'{d}_BAU_배분')
            self.viz_files[f'{d}_BAU'].setFilePath(params['OUTPUT'])
            if d == RES:
                self._export_scenarios('bau')
                self._finish_step('res_bau', 'res_hub')
            else:
                self._export_scenarios(('bau', 'hub'))     # 근무 BAU → BAU·유도정책 시나리오에 근무인구 추가
                self._finish_step('work_bau', 'work_plan')

    def _run_policy(self):
        try:
            grid = self._grid_params(RES)
        except ValueError as e:
            QMessageBox.warning(self, '입력 확인', str(e))
            return
        params = {
            'GRID': grid['INPUT'],
            'GID_FIELD': grid['GID_FIELD'],
            'TYPE_FIELD': 'type',
            'SELECTED_TYPES': [i for i, cb in enumerate(self.type_checks)
                               if cb.isChecked()],
            'USE_CI1': self.ci1_use.isChecked(),
            'CI1_MIN': self.ci1_min.value(),
            'USE_CI2': self.ci2_use.isChecked(),
            'CI2_MIN': self.ci2_min.value(),
            'OUTPUT': self._out_path(self.policy_out, '정책관심구역.gpkg', RES),
        }
        infra = self._layers.get('infra')
        if infra is not None:
            params['INFRA'] = infra
        if self._run('krihs_ssim:policy_interest_area', params):
            self._add_layer(params['OUTPUT'], '정책관심구역')
            self.hub_file.setFilePath(params['OUTPUT'])   # 2-2 입력으로 자동 연결

    def _run_hub(self):
        try:
            params = {**self._grid_params(RES), 'TABLE': self._table_path(RES)}
        except ValueError as e:
            QMessageBox.warning(self, '입력 확인', str(e))
            return
        hub = self._layers.get('hub')
        if hub is None:
            QMessageBox.warning(self, '입력 확인',
                                '정책관심구역 파일을 선택하거나 먼저 산출하세요.')
            return
        params.update({
            'HUB_LAYER': hub,
            'HUB_GID_FIELD': self.hub_gid_cb.currentField() or 'gid',
            'OUTPUT': self._out_path(self.hub_out, f'{RES}배분_2_거점유도.gpkg', RES),
        })
        for key, sp in self.exp_spins.items():
            params[key] = sp.value()
        if self._run('krihs_ssim:hub_grid_allocate', params):
            grp = self._add_result_group(params['OUTPUT'], f'{RES}_거점유도_배분')
            self._add_outline(hub, '정책관심구역_경계', '#0066ff', grp)
            self.viz_files[f'{RES}_거점유도'].setFilePath(params['OUTPUT'])
            self._export_scenarios('hub')
            self._finish_step('res_hub', 'res_plan')

    def _run_plan(self, d):
        try:
            params = {**self._grid_params(d), 'TABLE': self._table_path(d)}
        except ValueError as e:
            QMessageBox.warning(self, '입력 확인', str(e))
            return
        zone = self._layers.get(f'{d}_zone')
        if zone is None:
            QMessageBox.warning(self, '입력 확인', '개발사업 지구 파일을 선택하세요.')
            return
        stage = '3' if d == RES else '2'
        label = '계획인구' if d == RES else '계획반영'
        params.update({
            'ZONE_LAYER': zone,
            'Z_POP_FIELD': self.z_pop_cb[d].currentField(),
            'Z_YR_FIELD': self.z_yr_cb[d].currentField(),
            'OUTPUT': self._out_path(
                self.plan_out[d], f'{d}배분_{stage}_{label}.gpkg', d),
            'OUT_SPLIT_CSV': self._out_path(
                self.plan_csv[d], f'{d}분배_{stage}_{label}.csv', d),
        })
        if self.z_prj_cb[d].currentField():
            params['Z_PRJ_FIELD'] = self.z_prj_cb[d].currentField()
        if self.z_sgg_cb[d].currentField():
            params['Z_SGG_FIELD'] = self.z_sgg_cb[d].currentField()
        if d == WORK and self.w_ratio_use.isChecked():
            if not self.w_res_cb.currentField():
                QMessageBox.warning(self, '입력 확인',
                                    '원단위 분모(주거인구) 필드를 선택하세요.')
                return
            params['RATIO_FIELD'] = self.w_res_cb.currentField()
        if self._run('krihs_ssim:plan_grid_allocate', params):
            grp = self._add_result_group(
                params['OUTPUT'], f'{d}_계획인구반영_배분')
            self._add_outline(zone, f'개발사업지_경계_{d}', '#cc00cc', grp)
            self.viz_files[f'{d}_{label}'].setFilePath(params['OUTPUT'])
            # 계획 반영 배분 결과 → 장래 중심지 식별 입력에 자동 연결
            (self.ctr_res_file if d == RES else self.ctr_work_file).setFilePath(params['OUTPUT'])
            self._export_scenarios('plan')              # 거주 → 새로 내보냄, 근무 → 근무인구 열 추가
            # 거주 계획배분 후엔 근무 공통데이터로(근무까지 있어야 중심지 식별 가능),
            # 근무 계획배분 후엔 장래 중심지 식별로
            self._finish_step('res_plan', 'work_common') if d == RES \
                else self._finish_step('work_plan', 'center')
