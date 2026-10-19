# -*- coding: utf-8 -*-
import os
from qgis.core import QgsApplication, QgsProcessingProvider
from PyQt5.QtGui import QIcon

from .alg_bau_allocate import BauAllocateAlgorithm
from .alg_center_identify import CenterIdentifyAlgorithm
from .alg_hub_allocate import HubAllocateAlgorithm
from .alg_plan_allocate import PlanAllocateAlgorithm
from .alg_policy_area import PolicyAreaAlgorithm

# 중심지 분석·생활권 경계 (krihs_spst_sim_tools 통합본)
from .cnt_classifier.processing.alg_extract_candidates import ExtractCandidatesAlgorithm
from .cnt_classifier.processing.alg_extract import ExtractCenterAttributesAlgorithm
from .cnt_classifier.processing.alg_classify import ClassifyCentersAlgorithm
from .cnt_classifier.processing.alg_weighted_centroid import PopWeightedCentroidAlgorithm
from .cnt_classifier.processing.alg_mobility_matrix import MobilityMatrixAlgorithm
from .cnt_classifier.processing.alg_attractiveness_upper_zone import (
    AttractivenessUpperZoneAlgorithm,
)
from .od_to_region.processing.alg_center_points import CenterGridPointsAlgorithm
from .od_to_region.processing.alg_od_region_assign import ODMatrixRegionAssign
from .od_to_region.processing.alg_smoothing import RegionSmoothing

# KPI 공간구조 진단
from .alg_kpi_indicators import KpiSpatialStructureIndicators

# scipy 경량 OD (사용자 2.2/2.6 이식 — QNEAT3 대체)
from .alg_grid_od_scipy import GridCenterOdScipyGraph
from .alg_center_od_scipy import OdMatrixRoadScipyAlgorithm


class PopAllocProvider(QgsProcessingProvider):

    def id(self) -> str:
        return 'krihs_ssim'

    def name(self) -> str:
        return 'KRIHS S-Sim'

    def longName(self) -> str:
        return self.name()

    def icon(self) -> QIcon:
        return QIcon(os.path.join(os.path.dirname(__file__), 'icon.png'))

    def loadAlgorithms(self) -> None:
        self.addAlgorithm(PolicyAreaAlgorithm())
        self.addAlgorithm(BauAllocateAlgorithm())
        self.addAlgorithm(HubAllocateAlgorithm())
        self.addAlgorithm(PlanAllocateAlgorithm())
        self.addAlgorithm(CenterIdentifyAlgorithm())
        # 중심지 분석
        self.addAlgorithm(ExtractCandidatesAlgorithm())
        self.addAlgorithm(ExtractCenterAttributesAlgorithm())
        self.addAlgorithm(ClassifyCentersAlgorithm())
        # 생활권 경계 도출
        self.addAlgorithm(CenterGridPointsAlgorithm())
        self.addAlgorithm(ODMatrixRegionAssign())
        self.addAlgorithm(RegionSmoothing())
        self.addAlgorithm(PopWeightedCentroidAlgorithm())
        self.addAlgorithm(MobilityMatrixAlgorithm())
        self.addAlgorithm(AttractivenessUpperZoneAlgorithm())
        # scipy 경량 OD (2.2 격자-중심지 · 2.6 중심지-중심지)
        self.addAlgorithm(GridCenterOdScipyGraph())
        self.addAlgorithm(OdMatrixRoadScipyAlgorithm())
        # KPI 공간구조 진단
        self.addAlgorithm(KpiSpatialStructureIndicators())
