# train_XGboost · C2DB 带隙回归训练脚本

一个**只做训练**的脚本：读管理器准备好的快照与划分，训练 XGBoost，落盘模型与预测，
写一份可复现的 `manifest.json`。不划分数据、不画图、不写数据库。

## 1. 一条命令

```powershell
cd D:\数据库\模型\XGboost\train_XGboost
$env:PYTHONIOENCODING='utf-8'
python train.py                         # 用 config.yaml 默认值（G2 主模型）
python train.py --dry-run               # 只装配数据、打印形状，不训练
python train.py --features G3 --tag leak-check
python train.py --n-estimators 500 --max-depth 5 --learning-rate 0.03
```

结果落在 `..\output_XGb\<时间戳>_c2db-gap-<hash6>\`，每次训练一个**不可变目录**。

## 2. 依赖

| 项 | 版本 | 说明 |
|---|---|---|
| Python | 3.13.9 | 与母库项目一致 |
| xgboost | 3.0.2 | 官方 wheel，**随目录附带在 `..\libs\`**，离线可用 |
| numpy / scipy | 2.3.4 / 1.18.1 | xgboost 运行依赖 |
| PyYAML | 6.0.3 | 读 config.yaml |

`train.py` 会自己把 `..\libs` 加进 `sys.path`，**不需要装任何东西**。
换机器时若要正式安装：`python -m pip install -r requirements.lock.txt`（见该文件里的 wheel 哈希）。

> 源码仓库 `..\xgboost\` 只作参照实现（commit 记在 manifest 的 `env.reference_repo`），
> **不参与运行、未编译**。

## 3. 数据从哪来

```
D:\数据库\C2DB_materials.sqlite        ← 母库（只读）
        │  python ..\管理器\snapshot.py
        ▼
..\管理器\snapshots\
    c2db-kept-20261031.csv             3354 行 × 93 列（4 元信息 + 89 个特征列，其中 62 个元素 one-hot）
    c2db-kept-20261031.split.json      三集 uid 清单 + 比例 + 种子 + 哈希
    c2db-kept-20261031.features.json   特征契约（分组/单位/来源/黑名单）
        │  python train.py
        ▼
output_XGb\<run_id>\manifest.json
```

**脚本自己不做 random 划分**，一律以 `split.json` 为准，并核对：快照哈希、uid 集合一一对齐
（不一致直接退出）。划分口径：按 `formula` 分组、按（带隙分箱 × 含金属）分层、
种子 42，实测 2349 / 505 / 500（70.04 / 15.06 / 14.91 %，偏差 0.09pp）。

## 4. 特征组

| 组 | 内容 | 用途 |
|---|---|---|
| `G0` | 元素 one-hot(62) + `n_elements/has_metal/frac_max/n_atoms` | 只看组成的基线 |
| `G1` | G0 + `a,b,c,α,β,γ,space_group,cell_volume,thickness,unit_cell_area` | + 结构 |
| **`G2`** | G1 + `ehull,hform,energy_per_atom,dyn_stab` + `evac` | **主模型** |
| `G3` | G2 + 有效质量 + 极化率（+ 缺失指示） | 同源泄漏对照，**只用来说明，不给结论** |
| `G3p` | G2 + `gap_dir_pbe` | 答案泄漏对照，证明 R² 会虚高 |

黑名单（`features.json` 里写死，`runlog.start()` 二次拦截）：
`gap_pbe`（目标）、`gap_hse06`、`gap_gw`、`gap_dir_pbe`、`gap_kind_pbe`、`score`、`verdict`、
`formula`、`cif_*`、`source_url` 等。

## 5. 产物与 manifest

```
output_XGb\20261111-223846_c2db-gap-c07a0c\
├── manifest.json                 台账：数据指纹/划分/特征/参数/指标/环境/产物哈希
├── artifacts\
│   ├── model.json                4.7 MB，1173 棵树
│   ├── predictions.csv           uid, split, y_true, y_pred（三集全量 3354 行）
│   └── feature_importance.csv    gain / weight / cover
└── logs\train.log
```

`manifest.json` 里最关键的几项：

| 字段 | 含义 |
|---|---|
| `dataset.sha256` | 训练快照的哈希，换数据就对不上 |
| `dataset.split.indices_sha256` | 三集 uid 清单的哈希，证明"同一刀切" |
| `features.sha256` + `names` | 特征名单与顺序（元素列按 Z 升序固定） |
| `params` | 全部超参原值 |
| `env.packages` / `env.git` | 依赖版本与代码提交号 |
| `artifacts[].sha256` | 模型/预测/重要性的指纹，可随时复核 |
| `reproducibility` | 是否确定性 + 容差（`nthread=1` → `rmse ±1e-6`） |

复核：

```powershell
python ..\管理器\runlog.py ..\output_XGb\<run_id>
```

## 6. 边界（刻意不做的事）

- **不自己划分数据**：划分是实验设计的一部分，必须落盘、可追溯；
- **不画图**：图由管理器 `charts.py` 统一出，保证配色/字号/中文一致；
- **不写数据库**：收录由管理器 `record.py` 做，缺 manifest 就不许入库；
- **不猜参数**：缺字段报错，不填默认值；
- **不把 `test` 用于调参**：早停只看 `valid`，`test` 只在最后算一次。

## 7. 已知现象（不是 bug）

基线（G2，2000 上限 + 早停 50）：

| 集 | n | RMSE | MAE | R² |
|---|---|---|---|---|
| train | 2349 | 0.0350 | 0.0264 | 0.9993 |
| valid | 505 | 0.5646 | 0.3844 | **0.8199** |
| test | 500 | 0.6101 | 0.3881 | **0.8148** |

train 几乎完美而 valid/test 停在 0.82，说明**过拟合**：早停选在第 1122 轮仍偏深。
下一步值得试的方向（都只改 `config.yaml`，不动代码）：
`max_depth 6→4`、`learning_rate 0.05→0.03`、`min_child_weight 3→5`、
`reg_lambda 1→3`、`colsample_bytree 0.8→0.6`。
