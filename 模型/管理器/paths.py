# -*- coding: utf-8 -*-
"""路径与常量：全项目只在这里定义一次位置，别处一律 import。

三类东西分三个地方放，互不掺和：

    D:\\数据库\\
    ├── 模型\\XGboost\\          ← 只放"怎么算"：脚本 + 依赖 + 参考源码
    │   ├── xgboost\\            ← 官方源码仓库（只读参考，不参与运行）
    │   ├── libs\\               ← xgboost 3.0.2 wheel（随目录走，免联网）
    │   └── train_XGboost\\      ← 训练脚本
    ├── 训练数据\\              ← 只放"算出了什么"：按模型名分目录
    │   └── XGboost\\<run_id>\\  ← 一次训练一个不可变目录
    ├── 图与报告\\              ← 只放"给人看什么"：按数据集组织、跨模型共享
    │   ├── exhibit\\<dataset_id>\\
    │   ├── report\\
    │   └── index.html
    └── 模型\\管理器\\           ← 本模块（快照/划分/台账/出图）
"""
from __future__ import annotations

import os
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

# ---- 训练侧：脚本与依赖（"怎么算"）
XGB_ROOT = MODEL_ROOT / "XGboost"
XGB_LIBS = XGB_ROOT / "libs"
TRAIN_DIR = XGB_ROOT / "train_XGboost"

# ---- 训练产物：按模型名分目录，一个模型一个文件夹，模型之间互不干扰
#   训练数据\\XGboost\\<run_id>\\
DATA_ROOT = PROJECT_ROOT / "训练数据"
OUTPUT_ROOT = DATA_ROOT / "XGboost"          # 当前只有 xgboost 一家

#: 家族名 -> 产物目录名。接第二个模型时在这里加一行即可（或由 trainers 登记表给出）。
FAMILY_DIR = {"xgboost": "XGboost"}


def output_root(family: str = "xgboost") -> Path:
    """模型家族 -> 它自己的产物根目录。"""
    return DATA_ROOT / FAMILY_DIR.get(family, family)


# ---- 出图侧：图与报告独立于 模型/ 与 训练数据/
# 按"数据集"组织、跨模型共享：一套展品里可能同时含多个模型在同一划分上的对比图，
# 塞进某一个模型的目录就放不下第二个模型。可随时重画，不入 git。
FIGURE_ROOT = PROJECT_ROOT / "图与报告"
REPORT_DIR = FIGURE_ROOT / "report"          # 每次训练一份 HTML：report/<run_id>.html
INDEX_HTML = FIGURE_ROOT / "index.html"      # 跨 run 演示首页
FIGURES_DIR = FIGURE_ROOT                    # 兼容旧名

# ---- 展品侧（一套"按你输入的条数生成"的完整展品 = 一个目录）
# 目录名用 dataset_id，例如 exhibit/c2db-T2349-V505-S500-s42/
EXHIBIT_ROOT = FIGURE_ROOT / "exhibit"
#: 最多保留多少套展品；超出只提示，不自动删（用户选择）
MAX_EXHIBITS = 20

#: 复现用的数据快照命名（不含扩展名）
#: BASE_DATASET_ID = 母库抽出来的那份"母快照"；DATASET_ID = 当前进程实际要用的那份。
#: 训练脚本 import 的是 DATASET_ID，所以 build.py 用环境变量切换数据集，
#: 不需要改 train.py 一行代码。
BASE_DATASET_ID = "c2db-kept-20261031"
DATASET_ID = os.environ.get("C2DB_DATASET_ID", BASE_DATASET_ID)

#: 目标列
TARGET = "gap_pbe"

#: 划分口径
SEED = 42
RATIOS = {"train": 0.70, "valid": 0.15, "test": 0.15}
#: 分组/分层的比例容差（百分点），超过则换种子重抽
RATIO_TOLERANCE_PP = 0.5

#: 按条数生成划分时的规模下限（低于此值直接拒绝：样本太少的 R² 没有统计意义）
MIN_SIZES = {"train": 1000, "valid": 100, "test": 100}


def ensure_dirs() -> None:
    for d in (SNAPSHOT_DIR, FIGURE_ROOT, REPORT_DIR, EXHIBIT_ROOT, OUTPUT_ROOT, TRAIN_DIR):
        d.mkdir(parents=True, exist_ok=True)
