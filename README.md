# C2DB 材料数据库（单机 SQLite）

把筛选出的 **3,354 条**高完整度 C2DB 材料装进一个**可直接拷走的单文件数据库**：
三个子库按材料 ID 关联，缺失与出处都可追溯，一条命令重建，零服务器；
唯一的第三方依赖（读 Excel 用的 openpyxl）也随目录附带。

**本目录是自包含的**：代码、数据库、源数据、CIF 缓存、体检报告全在这里，
整个 `数据库` 文件夹拷到任意一台装了 Python 的机器上都能重建、查询、导出。

> **本目录位置：`D:\数据库`。** 下面命令示例以 `D:\` 为工作目录（即 `python 数据库\build.py`）；
> 进到本目录后把 `数据库\` 前缀去掉即可（`python build.py`）。

> 第一次用请看 [使用说明.md](使用说明.md)（简版，含三种打开方式与常用 SQL）；本文件是完整说明。

```
数据库/C2DB_materials.sqlite     ← 整个数据库就是这一个文件（约 11 MB，含 CIF 原文）
```

| 项 | 数量 |
|---|---|
| 材料（材料主库） | 3,354 |
| 结构记录（结构信息库） | 3,354（1:1） |
| 电学记录（电学性能子库） | 3,354（1:1） |
| 稳定性记录（辅助） | 3,354（1:1） |
| 元素 / 组成关系 | 62 种 / 9,687 条 |
| 有效质量（CBM / VBM·DOS，PBE） | 2,769 条（82.6%） |
| 逐行缺失登记 | 7,767 条 |
| CIF 结构文件（含哈希） | 3,354 |
| CIF 原文件（存库内 `structure.cif_content`） | 3,354 条，共 4.3 MB |
| 表 / 视图 | 10（全部 STRICT）/ 5 |
| schema 版本 | v2（`PRAGMA user_version`） |

---

## 1. 快速开始

在 `D:\` 下执行（或先 `cd D:\数据库`）：

```powershell
# 重建（导入 -> 体检 -> 结论），加 --export 顺带导出 Excel
python 数据库\build.py --export

# 按 ID 取一个完整对象
python 数据库\repo.py 2As-1

# 按条件查：含 Bi、带隙 >= 1 eV、只看动力学稳定
python 数据库\repo.py --search --element Bi --min-gap 1 --stable --limit 10

# 全库统计
python 数据库\repo.py --stats

# 直接跑 SQL（需 sqlite3 命令行；也可用任意 SQLite 客户端打开 .sqlite）
sqlite3 数据库\C2DB_materials.sqlite < 数据库\examples.sql
```

进到本目录后，命令里的 `数据库\` 前缀都可以去掉，如 `python build.py --export`。

> 环境：Python 3.13；读 Excel 需要 `openpyxl`，**已随本目录附带**（`pylibs/`，1.9 MB）。
> 运行前设置 `$env:PYTHONPATH='D:\数据库\pylibs'`；或 `pip install openpyxl` 后连这个变量都不用设。

---

## 2. 三子库 ↔ 表 ↔ 类

你要的三个子库，就是三张表 + 三个数据类；关联键是 `material_id`（C2DB 的 uid，如 `2As-1`）。

| 你的要求 | 表 | 数据类 | 关联 |
|---|---|---|---|
| 材料主库：ID / 化学式 / 名称 / 组成 / 类别 | `material` | `Material` | 主键 `material_id` |
| 结构信息库：晶格参数 / 空间群 / 晶相 / CIF | `structure` | `Structure` | 1:1，主键即外键 |
| 电学性能子库：带隙 / 迁移率 / 有效质量 / 介电常数 | `electrical` | `Electrical` | 1:1，主键即外键 |
| （辅助）稳定性与能量，评分依据 | `stability` | `Stability` | 1:1，主键即外键 |

C++ 里的"类封装"在这里是：

```python
from repo import MaterialRepo
with MaterialRepo() as repo:
    rec = repo.get("2As-1")        # MaterialRecord：三个子库 + 辅助库组合成一个完整对象
    rec.material.formula           # 'As2'
    rec.structure.a                # 3.608
    rec.electrical.gap_hse06       # 2.019
    rec.electrical.eff_mass_cbm    # 0.246  ← 电子（CBM DOS）有效质量
    rec.electrical.eff_mass_vbm    # 0.155  ← 空穴（VBM DOS）有效质量
    rec.electrical.mobility_e      # None   ← C2DB 无此字段
    rec.stability.dyn_stab         # 'Yes'
    rec.summary()                  # 打印成一段人读报告
```

对应关系：**表 = class，行 = 对象，主键 = 句柄，外键 = 组合关系，视图 = 只读接口，
`MaterialRepo` = 成员函数**。换 Postgres 只改连接串，模型和查询不动。

> 每张表到底代表什么对象、表与表的关联基数、外键放在哪张表、是否满足三大范式、
> 以及结构上还该改什么 —— 见 [表关系与范式评审.md](表关系与范式评审.md)。
>
> 要拿这个库去支撑专利/论文里的机器学习建模（带隙 + 介电常数 + 17 维描述符），
> 先看 [专利数据需求对照.md](专利数据需求对照.md)：逐项标了"有 / 可算 / 缺"。

---

## 3. 表结构（三子库）

### material — 材料主库

| 列 | 说明 |
|---|---|
| `material_id` | C2DB uid，主键，跨子库关联键 |
| `formula` / `reduced_formula` | 化学式 / 约化化学式 |
| `name` / `name_source` | 名称（C2DB 无名称字段，用化学式生成，`name_source='formula_temp'` 标注） |
| `composition` / `composition_json` | 组成：人读文本 / 机器可读 `{"As": 2}` |
| `n_elements` | 元素种类数 |
| `category` / `has_metal` / `category_note` | 类别：单质/二元/三元/多元 + 是否含金属（**规则推断**，原文留档） |
| `score` / `verdict` / `verdict_reason` | 完整度评分 / 筛选结论 / 筛选理由 |
| `source_url` / `batch_id` | 官网详情页 / 导入批次 |

### structure — 结构信息库

| 列 | 说明 |
|---|---|
| `a,b,c` (Å) / `alpha,beta,gamma` (°) | 晶格参数，取自官网 CIF，3 位小数 |
| `space_group` | 空间群号（体相 AA 堆叠），1–230 |
| `crystal_system` | 晶系，**由空间群推导**（辅助分类；不等于"晶相"） |
| `crystal_phase` | 晶相：C2DB 没有这个字段 → **恒为 NULL**，字典里写"源表无此字段" |
| `cif_url` / `cif_sha256` / `cif_bytes` / `cif_cache` | CIF 链接 + 原文件哈希 + 字节数 + 下载时的缓存路径（出处标注） |
| `cif_content` | **CIF 原文件本身**（BLOB，官网 `/material/<uid>/download/cif` 的字节，一字不改）；`sha256(cif_content) = cif_sha256` |
| `cell_volume` | 晶胞体积，由六参数推导（含真空层，按二维胞理解） |

### electrical — 电学性能子库

| 列 | 单位 | 方法 | 说明 |
|---|---|---|---|
| `gap_pbe` | eV | PBE | 0.000 是零带隙有效值 |
| `gap_dir_pbe` | eV | PBE | 最小直接带隙 |
| `gap_hse06` | eV | HSE06@PBE | 本库全部有值 |
| `gap_gw` | eV | G0W0@PBE | 只有 339 条有值 |
| `gap_kind_pbe` | — | 派生 | 直接/间接（gap 与 gap_dir 相等=直接） |
| `eff_mass_cbm` | m₀ | PBE | 电子有效质量（CBM DOS）；2,769 条有值，∞ = 平带，0.000 = 小于 0.0005 m₀ |
| `eff_mass_vbm` | m₀ | PBE | 空穴有效质量（VBM DOS）；同上 |
| `mobility_e/h` | cm²/(V·s) | — | **C2DB 无此字段**，全 NULL |
| `dielectric_static` | 1 | — | **C2DB 无此字段**（只有二维极化率 α，单位 Å），全 NULL |
| `polarizability_el/lat/total` | Å | RPA | 极化率，**明确不是介电常数** |

> schema v4 起，源表给的"极化率一致性自检"（一致/无法校验/不一致）不再入库——那是审核结论、不是数据。
> 要复核就现算：`WHERE ABS(polarizability_el + polarizability_lat - polarizability_total) > 0.001`。

方法/泛函不写在这张表里，而写在 `property_dictionary`（见 §4）：一个列 = 一种方法的结果，
所以 HSE06 的带隙永远不会和 PBE 的带隙混在一列里。

> 有效质量是 2026-10-31 补抓的（官网键 `emass_cbm` / `emass_vbm`，PBE，DOS 口径）；
> 介电常数与迁移率经核查官网确实没有，证据与复现方式见
> [字段核查_有效质量_介电常数_迁移率.md](字段核查_有效质量_介电常数_迁移率.md)。

### 辅助表

| 表 | 用途 |
|---|---|
| `stability` | 动力学稳定性 `dyn_stab`、`ehull`、`hform`、`energy`（评分依据） |
| `element` / `material_element` | 组成拆解（元素 ↔ 化学计量比），支撑"按元素筛选" |
| `property_dictionary` | 性质 → 单位/方法/泛函/精度/源表列/可用性（15 条） |
| `field_dictionary` | 列级说明（48 条，中文名、来源、缺失口径） |
| `data_gap` | 逐行缺失登记（字段 + 原因码） |
| `import_batch` | 导入批次：源文件、SHA256、行数、时间 |

---

## 4. 七条硬规则

1. **缺失 = NULL。** 库内不存"缺失/未计算/源表无此字段"这类字符串（体检第 7 项会全表扫描，命中即 FAIL）。
   展示时才渲染：导出 Excel 里 `缺失`（源表有列但没值）与 `源表无此字段`（源表根本没这列）用两种颜色区分。
2. **0.000 是有效值。** 78 条零带隙材料 `gap_pbe = 0`，与"未计算"（NULL）严格区分。
3. **方法/泛函跟着值走。** `gap_hse06 = HSE06@PBE`、`gap_gw = G0W0@PBE`、`polarizability = RPA`；
   将来加 PBE+U、HSE03 只加一行字典，表结构不变。
4. **官网没有的字段，留空并打标记。** 晶相、迁移率、介电常数：`crystal_phase` 恒 NULL，
   `mobility_*`/`dielectric_static` 恒 NULL，字典里 `availability='no_field'`，
   `v_material_missing` 会把它们列进每条材料的缺失清单。**绝不补值、绝不用极化率冒充介电常数。**
   有效质量经核查官网有（`emass_cbm`/`emass_vbm`），已补抓入库，未给出的 585 条逐行登记 `not_computed`。
5. **推断字段带依据。** 名称（`name_source`）、类别（`category_note` 留推断原文）、晶系与晶胞体积
   （字典标 `derived`）—— 都能看出"这是推的，不是官网写的"。
6. **类型与删除都由数据库强制（schema v2 起）。** 10 张表全部是 `STRICT` 表：往 REAL 列写 `'abc'`
   会被拒绝，主键列显式 `NOT NULL`；`material.batch_id` 用 `ON DELETE RESTRICT`，
   **删批次不会连带删掉材料**（删材料仍会级联删三个子库）。这条由单元测试
   `test_02b_schema_is_strict_and_restrict` 守着，改了 `schema.sql` 跑一遍就知道会不会破。
7. **CIF 原文存库内（schema v3 起）。** `structure.cif_content` 存的是官网
   `/material/<uid>/download/cif` 返回的字节，`sha256(cif_content) = cif_sha256`，
   体检第 13 项逐条重算 + 抽 20 条与 `data/cif` 磁盘文件比对。
   所以**只要有这个 .sqlite，就能拿到 3,354 个 CIF 原文件**，不需要 `data/cif` 目录
   （`data/cif` 只是重建时的输入）。取法：`python 数据库/repo.py 2As-1 --cif --out 2As-1.cif`。

---

## 5. 视图（封装后的读接口）

| 视图 | 用途 |
|---|---|
| `v_material_full` | 一条材料 = 一行完整对象（三子库 JOIN + 评分） |
| `v_method_map` | 每个数值的方法/泛函/单位/源表列 |
| `v_material_missing` | 逐材料缺失清单（含"源表无此字段"全局项） |
| `v_missing_counts` | 字段级缺失矩阵（分母 = 库内材料数） |
| `v_category_summary` | 类别 × 含金属的规模/均分/平均带隙 |

```sql
-- 器件筛选：直接带隙 1–2 eV、动力学稳定、含金属
SELECT id, formula, gap_pbe, gap_hse06, space_group
FROM v_material_full
WHERE gap_kind_pbe = '直接' AND gap_pbe BETWEEN 1 AND 2
  AND dyn_stab = 'Yes' AND has_metal = 1
ORDER BY gap_pbe DESC;
```

更多见 [examples.sql](examples.sql)（13 条常用查询）。

---

## 6. 目录结构

```
数据库/
├── C2DB_materials.sqlite           # 数据库（生成物，约 11 MB，含 CIF 原文）
├── C2DB_三子库导出_20261031.xlsx    # Excel 浏览版（7 页，给不装数据库的人看）
├── 体检报告.txt                      # 13 项体检结果
├── 字段核查_有效质量_介电常数_迁移率.md  # 官网有没有这几项的核查记录
├── 表关系与范式评审.md                 # 每张表代表什么对象、关联基数、三大范式判定、修改建议
├── 专利数据需求对照.md                 # 专利要的 5 个属性 / 17 维描述符 ↔ 本库字段的逐项对照与缺口
├── 使用说明.md                       # 简版使用说明（新手先看这个）
├── README.md                       # 本文件
│
├── schema.sql                      # 建表 + 视图 + 约束（唯一的结构定义）
├── models.py                       # 数据类：Material / Structure / Electrical / Stability / MaterialRecord
├── db.py                           # 连接、建库、查询小工具
├── etl.py                          # data/ 下的 xlsx + emass.json -> 三子库
├── repo.py                         # MaterialRepo + 命令行
├── verify.py                       # 13 项体检 -> 体检报告.txt
├── build.py                        # 一键：导入 + 体检（+ 导出 Excel）
├── export.py                       # 库 -> Excel（7 页）
├── examples.sql                    # 13 条示例查询
│
├── pylibs\                         # 随目录附带的第三方库（openpyxl + et_xmlfile，1.9 MB）
│
├── data\                           # 唯一数据输入（自包含）
│   ├── C2DB_筛选结果_20261031.xlsx  # 17,001 行筛选结果，取"保留数据"3,354 条
│   ├── emass.json                  # 有效质量补抓：3,664 条 uid → CBM/VBM（PBE, m0）
│   ├── c2db_keys.json              # 官网 88 个检索键（核查介电常数/迁移率的证据）
│   ├── cif\                        # 3,354 个 CIF 原文件（ETL 输入；内容已内嵌进库）
│   └── README.md                   # 数据说明
│
└── tests\
    └── test_database.py            # 14 个单元测试（标准库 unittest）
```

---

## 7. 体检与测试

```powershell
python 数据库\verify.py                 # 13 项断言，输出 数据库\体检报告.txt
python 数据库\tests\test_database.py    # 14 个单元测试
```

体检覆盖：行数一致、外键完整、三子库 1:1 覆盖、主键唯一、评分口径、数值域、
无缺失字符串、官网无字段必须为空、有效质量与抓取文件逐条一致、极化率≠介电常数、
方法/泛函齐全、30 条抽样逐值比对（11 个数值列）、CIF 原文内嵌且逐条重算哈希。

当前结果：**13/13 通过**（详见 [体检报告.txt](体检报告.txt)）。

---

## 8. 数据来源与精度

| 项 | 值 |
|---|---|
| 来源 | C2DB（`https://c2db.fysik.dtu.dk`），访问日 2026-10-28 |
| 上游工作表 | `C2DB_筛选结果_20261031.xlsx` 的"保留数据"页（3,354 / 17,001 = 19.73%） |
| 上游文件哈希 | `3d7723633958890b7d64a53f1f68498387a66daa5c01df805958e68a8c981adc` |
| 显示精度 | 全部数值按官网显示精度 3 位小数存储 |
| 结构来源 | 每条 `/material/<uid>/download/cif`，原文存 `structure.cif_content` + SHA256 |
| 有效质量来源 | `table?filter=emass_cbm` 的 `emass_cbm`/`emass_vbm` 两列（3,664 条，2026-10-31 补抓） |

---

## 9. 扩展方式

| 想做的事 | 怎么改 |
|---|---|
| 增加一种方法（如 PBE+U 带隙） | `property_dictionary` 加一行 + `electrical` 加一列（或改建长表） |
| 补迁移率/介电常数（自算或实验） | 直接写 `electrical` 对应列，把字典 `availability` 改成 `own_calc` |
| 换数据库 | `schema.sql` 语法基本通用；Postgres 换类型即可，Python 侧只改 `db.connect` |
| 做网页 | 把 `MaterialRepo` 挂到 FastAPI / Streamlit，模型层不用动 |
| 全量入库 | `python 数据库\build.py --scope all`（17,001 条，候选/剔除带 `verdict` 标记） |

**为什么不用 MySQL/Postgres/MongoDB**：3,354 行、单人、只读为主，起服务只会增加部署负担；
SQLite 单文件可随项目拷走，支持外键/约束/视图/全文索引，性能在这个量级绰绰有余。
需要多人并发或 Web 部署时，再用同一套模型迁 Postgres —— 迁移成本只是连接串。
