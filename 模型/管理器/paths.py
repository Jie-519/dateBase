# -*- coding: utf-8 -*-
"""路径与常量：全项目只在这里定义一次位置，别处一律 import。

    D:\\数据库\\模型\\
    ├── 管理器\\            ← 本模块（快照/划分/台账/出图）
    ├── XGboost\\
    │   ├── xgboost\\       ← 官方源码仓库（只读参考，不参与运行）
    │   ├── libs\\          ← xgboost 3.0.2 wheel（随目录走，免联网）
    │   ├── train_XGboost\\ ← 训练脚本
    │   └── output_XGb\\    ← 训练产物（每次训练一个不可变目录）
"""
from __future__ import annotations

from pathlib import Path

# ---- 根目录
MODEL_ROOT = Path(__file__).resolve().parent.parent          # ...\\模型
PROJECT_ROOT = MODEL_ROOT.parent                             # ...\\数据库
MANAGER_DIR = Path(__file__).resolve().parent                # ...\\模型\\管理器

# ---- 输入：母库（只读打开，绝不写）
MOTHER_DB = PROJECT_ROOT / "C2DB_materials.sqlite"
MOTHER_SOURCE_XLSX = PROJECT_ROOT / "data" / "C2DB_筛选结果_20261031.xlsx"

# ---- 管理器侧产物
SNAPSHOT_DIR = MANAGER_DIR / "snapshots"
CATALOG_DB = MANAGER_DIR / "catalog.sqlite"
FIGURES_DIR = MANAGER_DIR / "figures"

# ---- 训练侧
XGB_ROOT = MODEL_ROOT / "XGboost"
XGB_LIBS = XGB_ROOT / "libs"
TRAIN_DIR = XGB_ROOT / "train_XGboost"
OUTPUT_ROOT = XGB_ROOT / "output_XGb"

#: 复现用的数据快照命名（不含扩展名）
DATASET_ID = "c2db-kept-20261031"

#: 目标列
TARGET = "gap_pbe"

#: 划分口径
SEED = 42
RATIOS = {"train": 0.70, "valid": 0.15, "test": 0.15}
#: 分组/分层的比例容差（百分点），超过则换种子重抽
RATIO_TOLERANCE_PP = 0.5


def ensure_dirs() -> None:
    for d in (SNAPSHOT_DIR, FIGURES_DIR, OUTPUT_ROOT, TRAIN_DIR):
        d.mkdir(parents=True, exist_ok=True)
