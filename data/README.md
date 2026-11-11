# data —— 数据库的唯一输入

重建数据库（`python 数据库\build.py`）只读这个目录；删了就无法重建，请勿改动。

| 文件 / 目录 | 内容 | 大小 |
|---|---|---|
| `C2DB_筛选结果_20261031.xlsx` | 17,001 行 C2DB 导出数据的筛选结果（8 页：字段映射表、筛选汇总、评分档位对照、保留数据、待补充候选、剔除或低完整度、缺失字段统计、抽样校验） | 5.6 MB |
| `emass.json` | 有效质量补抓结果：3,664 条 `uid → {cbm, vbm}`（CBM/VBM DOS 有效质量，PBE，单位 m₀） | 0.2 MB |
| `c2db_keys.json` | 官网 help#keys 的 88 个检索键（核查介电常数/迁移率是否存在用的证据） | 20 KB |
| `cif\` | 3,354 个 CIF 结构文件（保留数据每条一个，文件名 = `material_id.cif`） | 4.3 MB |

## 来源

| 项 | 值 |
|---|---|
| 上游 | C2DB，`https://c2db.fysik.dtu.dk`，主数据访问日 2026-10-28 |
| 抓取范围 | 清空默认 `ehull 0–0.2` 过滤后 681 页 × 25 行 = 17,001 条 |
| 结构文件 | 每条 `https://c2db.fysik.dtu.dk/material/<uid>/download/cif` |
| 有效质量 | `https://c2db.fysik.dtu.dk/table?filter=emass_cbm`（3,664 条 / 147 页），2026-10-31 抓取 |
| 显示精度 | 官网所有数值 3 位小数；库内按该精度存储 |

## 校验

- `C2DB_筛选结果_20261031.xlsx` 的 SHA256：
  `3d7723633958890b7d64a53f1f68498387a66daa5c01df805958e68a8c981adc`
- `cif/` 下每个文件的原文本体都已存进 `structure.cif_content`，哈希存在 `structure.cif_sha256`；
  体检第 13 项会**全量重算**哈希，并抽 20 条与本目录文件逐字节比对。
  也就是说：`cif/` 只是重建时的输入，运行期取 CIF 请用 `structure.cif_content`
  （`python 数据库/repo.py <uid> --cif --out x.cif`），不依赖本目录。
- `emass.json` 与库内 `electrical.eff_mass_cbm/vbm` 由体检第 9 项逐条比对。

## 导入的是哪一部分

`etl.py` 默认只读"保留数据"页（3,354 行，完整度 ≥90）；
`--scope all` 会把"待补充候选""剔除或低完整度"也导进来（共 17,001 行，
`material.verdict` 区分 保留/候选/剔除）。后两页在本次筛选里分别有 13,647 和 0 行。

## 特殊值

- 有效质量 `0.000`：官网三位小数取整（<0.0005 m₀），不是缺失；
- 有效质量 `∞`：CBM 132 条 / VBM 130 条，平带、无穷大质量，库里存 IEEE 无穷大，Excel 渲染为 `∞`。
