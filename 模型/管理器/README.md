# 管理器 · 训练台账与出图

一句话：**训练脚本只管训练**，练完把产物丢进 `output_XGb/<run_id>/`；
本目录负责剩下的三件事——**记账、出图、出报告**，并保证任何一张图、任何一个数字都能查回来。

```
训练（train.py）          记录（record.py）         展示（charts/report.py）
   │                          │                        │
   ├─ artifacts/model.json    ├─ catalog.sqlite        ├─ 19 张 PNG
   ├─ artifacts/predictions   │   7 表 3 视图          ├─ 4 份单 run HTML
   └─ manifest.json  ────────▶│   （纯索引，可重建）   └─ 1 份首页 index.html
```

---

## 一、三层各管什么（别混）

| 层 | 载体 | 回答的问题 | 能不能重造 |
|---|---|---|---|
| 材料事实 | `C2DB_materials.sqlite`（母库） | 这些材料本身是什么 | 不能，是唯一真相 |
| 喂给模型的东西 | `snapshots/*.csv` + `.split.json` + `.features.json` | 到底是哪些行列进了模型、怎么切的 | 能，`snapshot.py` 重跑即得 |
| 实验记录 | `output_XGb/<run_id>/` 里的 `manifest.json` | 这次训练用了什么、得了多少分、凭什么 | **不能**，一次训练一份，写死不动 |
| 索引 | `catalog.sqlite` | 哪次最好、差在哪、图在哪 | 能，`python cli.py all` 从 manifest 重建 |

规则：**真相在文件，台账只是索引**。删掉 `catalog.sqlite` 不会丢任何信息。

---

## 二、文件地图

| 文件 | 职责 | 谁调它 |
|---|---|---|
| `paths.py` | 全项目位置常量，只在这里定义一次 | 所有模块 |
| `runlog.py` | 一次训练的"装箱单 + 身份证"：校验特征、写 manifest、复核产物哈希 | `train.py`（**已被训练脚本调用，勿改签名**） |
| `snapshot.py` | 从母库抽快照（93 列）+ 分组分层划 train/valid/test | 一次性/换数据时 |
| `schema.sql` | `catalog.sqlite` 的 7 张表 + 3 个视图 | `db.py` |
| `db.py` | 建库、连接（只读/可写）、`rows/one/scalar` 小工具 | 所有模块 |
| `record.py` | 把 run 目录收进台账；补 `runlog.verify` 查不到的校验；打"冒烟/泄漏"标 | `cli.py record` |
| `splitguard.py` | **只做一件事**：算三集条数、材料 ID / 化学式的两两交叠，判定测试集干不干净 | `verify`、`charts`、`report`、`tests` |
| `charts.py` | 出图，登记 `figure` 表（图 + 配方 + sha256） | `cli.py chart` |
| `report.py` | 单 run 报告（图 base64 内嵌）+ 跨 run 首页 | `cli.py report` |
| `verify.py` | 台账自检：结构版本、外键、manifest 指纹、产物哈希、图 | `cli.py verify` |
| `cli.py` | 唯一入口，只做调度与打印 | 你 |
| `tests/` | 13 个用例：结构、幂等、篡改检测、真实台账回归 | `python -m unittest` |

> 没有单独的 `query.py`：查询就是 `db.rows(conn, sql)` 或 `python cli.py sql "..."`，
> 再包一层只会多一层转发，不增加信息。

---

## 三、上手（四条命令覆盖日常）

```bat
cd D:\数据库\模型\管理器

python cli.py all          # 从零重建：建库→收录→出图→出报告→自检（约 4 秒）
python cli.py record       # 又训完一次？只做这一步，其它重跑一遍即可
python cli.py top          # 看能选型的排行榜（按验证集 R²）
python cli.py open         # 用浏览器打开首页

python cli.py list         # 全部训练，含冒烟与泄漏对照
python cli.py show 3db     # 某一次的细节（run_id 前缀即可）
python cli.py verify       # 台账自检
python cli.py sql "select feature,gain from feature_importance where run_id like '%c07a0c' order by gain desc limit 5"
```

新增一次训练的标准动作：

```bat
cd ..\XGboost\train_XGboost
python train.py --config config.yaml --features G2 --note "改了 max_depth"
cd ..\..\管理器
python cli.py record && python cli.py chart && python cli.py report && python cli.py verify
```

---

## 四、一次训练到底发生了什么

1. `train.py` 读快照、按 `split.json` 切数据；
2. 调 `runlog.start(...)`：**校验特征**（黑名单 + 组内成员 + 快照哈希）→ 建 `output_XGb/<run_id>/` → 写 `manifest.partial.json`；
3. 训练、早停；`test` 只在最后评一次，**不参与调参**；
4. 写 `artifacts/{model.json, predictions.csv, feature_importance.csv}`；
5. `runlog.finish()`：逐个算 sha256 → 原子替换成 `manifest.json`（此后只读）；
6. `record.py` 扫一遍 `output_XGb/`：`runlog.verify` + 6 项补充校验 → 写进 `catalog.sqlite`；
7. `charts.py` 从台账取数画图，把"图 + 配方 + sha256"登记进 `figure` 表；
8. `report.py` 把台账和图片拼成 HTML。

---

## 五、catalog.sqlite 结构

| 表 | 一行是 | 关键字段 |
|---|---|---|
| `run` | 一次训练 | `run_id` PK、`feature_group`、`n_features`、`params_json`、`manifest_sha256`、`is_smoke`、`is_leak` |
| `metric` | 一个指标值 | (`run_id`,`split`,`name`,`step`) PK，`step=-1` 表示最终值 |
| `artifact` | 一个产物 | (`run_id`,`path`) PK，`sha256`、`present` |
| `prediction` | 一个材料的预测 | (`run_id`,`material_id`) PK，`y_true`/`y_pred` |
| `feature_importance` | 一个特征的重要性 | (`run_id`,`feature`) PK，`gain`/`weight`/`cover`/`rank_gain` |
| `figure` | 一张图 | `fig_id` PK，`recipe_json`（画这张图的全部输入）、`sha256` |
| `meta` | 键值 | 结构版本、项目根 |

三个视图：

- `v_run_score`：一次训练一行，指标摊平成列（含 `is_smoke` / `is_leak` 标记）；
- `v_leaderboard`：**剔掉冒烟与泄漏对照**，按 `valid_r2` 降序 —— 选型只看这张；
- `v_leak_report`：专看泄漏对照"虚高了多少"。

两个刻意的决定：

- **`step = -1` 而不是 `NULL`**：SQLite 的唯一约束认为两个 `NULL` 互不相同，用 `NULL` 会让 `PRIMARY KEY (run_id,split,name,step)` 放进重复行。
- **`manifest_sha256`**：manifest 无法把自身的哈希写进自身，所以这个锚存在台账侧。文件被手改过，`cli.py verify` 当场报出来。

---

## 六、图（19 张，全部可重画）

命名：`fig_<run 短码>_<序号>_<含义>.png` 与 `fig_cmp_<序号>_<含义>.png`，纯 ASCII 防跨机器乱码，中文只在图内标题。

| 图 | 回答什么 |
|---|---|
| `_01_pred_true` | 预测贴不贴理想线；哪几个点最离谱 |
| `_02_residual` | 残差分布 + 有没有"带隙越大越低估"的系统偏差 |
| `_03_learning` | 训练/验证 RMSE 曲线 + 早停点（看有没有过拟合） |
| `_04_importance` | 模型到底在依赖哪些特征（Top15 gain） |
| `fig_cmp_01_r2` | 4 次训练的训练/验证/测试 R² 并排 |
| `fig_cmp_02_leak` | **主叙事**：加同源列、加答案列各能把 R² 抬多少 |
| `fig_cmp_03_err_by_gap` | 误差随带隙怎么变 —— 在哪不可信 |

图不入 git（`.gitignore` 已忽略 `figure_XGb/`），删了跑 `python cli.py chart` 原样重画：
每张图的输入都记在 `figure.recipe_json` 里。

**每张图底部都有一行固定声明**：

```
训练集 2349 条 · 验证集 505 条 · 测试集 500 条  ｜  测试集材料严禁进入训练集（已核：零交叠）
```

数字取自 `splitguard`，与 `cli.py verify` 用的是同一份计算 —— 图上写的条数，
和自检报的条数，不可能是两套。

---

## 七、数据分集：测试集必须干净

**硬规则：测试集里的材料，严禁出现在训练集（或验证集）里。**

不是靠"记得别混"，而是三层设防：

| 层 | 机制 | 谁在管 |
|---|---|---|
| ① 切分 | 按 `formula` **整组**切分（同一化学式的材料只落一个集）+ (带隙分箱 × 有无金属) 分层，seed 42，比例容差 0.5 pp | `snapshot.py` |
| ② 指纹 | `split.json` 存快照 sha256 与三集 `indices_sha256`；`runlog.start()` 每次训练前比对快照哈希 | `snapshot.py` / `runlog.py` |
| ③ 复核 | 每次 `cli.py verify` 重读快照，算 material_id 与 formula 的两两交叠；**不为 0 即报错**；同时逐 run 核对"落库的 split 是否就是快照那份" | `splitguard.py` / `verify.py` |

当前实测（可直接复跑 `python splitguard.py`）：

```
训练集 2349 条 / 验证集 505 条 / 测试集 500 条   合计 3354
材料交叠   train∩test = 0 · valid∩test = 0 · train∩valid = 0
化学式交叠 0 / 0 / 0        重复 material_id：0
与 split.json 对账：条数一致、快照指纹一致
```

为什么要查**化学式**交叠而不只查 ID：同一化学式的不同晶型/掺杂如果被拆到两集，
等于把"同族材料的答案"提前给了模型，分数会虚高但查不出来。
按化学式整组切分就是为堵这个口子。

图上的条数、报告里「三、数据怎么分的」那一节、`verify` 的「测试集干净」一项、
`tests` 里的 `test_test_set_is_clean`，**四处用的是同一份计算**，不存在口径不一致的可能。

---

## 八、两层校验，各管一段

| | 管什么 | 什么时候跑 |
|---|---|---|
| `runlog.verify(run_dir)` | **一次训练的自证**：manifest 在、产物哈希一致、指标非空 | `train.py` 收尾、`record.py` 入库前 |
| `verify.py` | **整本台账的横向自洽**：结构版本、外键、manifest 是否被改、产物是否成套、预测条数、图是否可用 | `python cli.py verify` |

两者不重叠，所以 `runlog.py` 一行没动（它已被训练脚本调用，改它要连带动别处）。

---

## 九、扩展点

- **加模型家族**（LightGBM/线性基线）：不用改本目录，只要训练脚本照样写 `manifest.json`；
  `record.py` 的 `model_family` 是自由文本。
- **加一张图**：在 `charts.py` 写一个 `fig_xxx(conn, run)` 返回 `_save(...)`，加进 `build_run_figures` 或 `build_compare_figures` 即可，登记是自动的。
- **加"按需选数据出图"**：`charts.py` 的取数只依赖 `prediction` / `metric` 两个表，
  要支持"只画某几个材料/某几组实验"，加一个 `WHERE` 条件 + CLI 参数即可，不动表结构。
- **补 G0/G1 消融**：`python train.py --features G0`（各约 2 秒），收录后对比表自动多两行。

---

## 十、边界（明确不做的事）

- **单机 SQLite**，不并发写：`record` 一次跑一个进程就够，多人同时训请各自建库。
- **不做 GUI**：本目录出的是静态 HTML，双击即看，不依赖任何服务。
- **不自动删旧实验**：失败也是记录，`run` 表只增不删（同 run_id 重收除外）。
- **不改训练脚本**：`train.py` 是用户提供的原件，本目录只消费它写出的东西。

---

## 十一、自检

```bat
python -m unittest discover -s tests -v
python cli.py verify
```

当前实测：17 个用例全过；台账 4 次训练、958 条指标、13,416 条预测、299 行重要性、19 张图，自检 12 项全通过（含「测试集干净」「各 run 同一划分」）。
