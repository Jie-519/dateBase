-- =====================================================================
-- C2DB 材料数据库 · 单机 SQLite · 三子库结构（schema v4）
-- ---------------------------------------------------------------------
-- 设计原则（与 C++ 类封装一一对应）：
--   1) 一张表 = 一个类；一行 = 一个对象；主键 = 句柄；外键 = 组合关系
--   2) 三个子库：material(材料主库) / structure(结构信息库) / electrical(电学性能子库)
--      全部以 material_id (= C2DB uid) 关联；1:1 扩展用"主键即外键"，不往主表堆列
--   3) 缺失 = NULL；缺失原因进 data_gap；库内绝不存字符串"缺失/未计算"
--      （展示层导出 Excel 时才渲染成"缺失"）
--   4) 每个数值都能追到"方法/泛函"(property_dictionary) 与"出处"(source_url / CIF 哈希)
--   5) 0.000 是有效值（零带隙），与"未计算"严格区分
--   6) 所有表都是 STRICT 表（SQLite 3.37+）：
--      a) 每列只能存声明类型的值 —— 往 REAL 列写 'abc' 会被拒绝，不再是"能写进去"；
--      b) 主键列隐含 NOT NULL（下面仍显式写 NOT NULL，让 DDL 自己会说话）；
--      c) 代价：改约束不能再靠 ALTER TABLE，必须整库重建（python 数据库/build.py）。
--   7) CIF 原文件直接存在 structure.cif_content（BLOB）：内容取自官网
--      /material/<uid>/download/cif，与 cif_sha256 逐字节对应；库本身即可交付 CIF，
--      data/cif 只是 ETL 的输入缓存（不是运行期依赖）。
--   8) 不存"审核/自检结论"列（v4）：例如源表的极化率一致性自检（一致/无法校验/不一致）
--      已删除，需要时用 SQL 现算；库内只留数据本身与可校验的摘要（哈希）。
--
-- ⚠ 外键开关是"每连接"的：PRAGMA foreign_keys 默认关闭，必须在**每个**连接上执行
--    PRAGMA foreign_keys = ON，否则外键约束静默失效（db.py:connect 已做；临时用
--    sqlite3 命令行 / 别的客户端时务必自己开）。
-- =====================================================================
PRAGMA foreign_keys = ON;
PRAGMA user_version = 4;

-- ---------------------------------------------------------------- 0 导入批次（溯源）
CREATE TABLE IF NOT EXISTS import_batch (
    batch_id        INTEGER PRIMARY KEY NOT NULL,
    source_file     TEXT    NOT NULL,          -- 导入的 xlsx 文件名
    source_sha256   TEXT    NOT NULL,          -- 文件哈希（防"数据被改过"）
    source_rows     INTEGER NOT NULL,          -- 源表总行数 17,001
    scope           TEXT    NOT NULL CHECK (scope IN ('kept','all')),
    imported_rows   INTEGER NOT NULL,
    imported_at     TEXT    NOT NULL,
    note            TEXT
) STRICT;

-- ---------------------------------------------------------------- 1 材料主库
CREATE TABLE IF NOT EXISTS material (
    material_id     TEXT    PRIMARY KEY NOT NULL, -- C2DB uid，如 2As-1（跨库关联键）
    formula         TEXT    NOT NULL,
    reduced_formula TEXT,
    name            TEXT    NOT NULL,          -- 名称（C2DB 无名称字段，用 formula 生成临时名）
    name_source     TEXT    NOT NULL CHECK (name_source IN
                        ('formula_temp','c2db','manual')),
    composition     TEXT,                      -- 组成（人读文本）
    composition_json TEXT,                     -- 组成（机器读，{"As": 2}）
    n_elements      INTEGER CHECK (n_elements IS NULL OR n_elements BETWEEN 1 AND 8),
    category        TEXT,                      -- 类别：单质/二元化合物/三元化合物/多元化合物
    has_metal       INTEGER CHECK (has_metal IS NULL OR has_metal IN (0,1)),
    category_note   TEXT,                      -- 类别推断原文（含元素列表，作为依据留档）
    score           REAL    CHECK (score IS NULL OR (score >= 0 AND score <= 100)),
    verdict         TEXT    CHECK (verdict IS NULL OR verdict IN ('保留','候选','剔除')),
    verdict_reason  TEXT,
    source_url      TEXT,
    -- 溯源表不许级联删掉事实：批次是本库的"出处"，删它必须显式先清材料（RESTRICT）
    batch_id        INTEGER NOT NULL REFERENCES import_batch(batch_id) ON DELETE RESTRICT,
    CHECK (material_id = TRIM(material_id) AND material_id <> '')
) STRICT;
CREATE INDEX IF NOT EXISTS ix_material_category ON material(category, has_metal);
CREATE INDEX IF NOT EXISTS ix_material_score    ON material(score);
CREATE INDEX IF NOT EXISTS ix_material_formula  ON material(formula);

-- 组成拆解（主库内部的规范化表；不是独立子库）
CREATE TABLE IF NOT EXISTS element (
    z        INTEGER PRIMARY KEY NOT NULL CHECK (z BETWEEN 1 AND 118),
    symbol   TEXT    NOT NULL UNIQUE
) STRICT;

CREATE TABLE IF NOT EXISTS material_element (
    material_id TEXT    NOT NULL REFERENCES material(material_id) ON DELETE CASCADE,
    z           INTEGER NOT NULL REFERENCES element(z) ON DELETE RESTRICT,
    count       REAL    NOT NULL CHECK (count > 0),
    -- 没有 fraction 列：占比 = count / Σcount（同材料内），纯派生，
    -- 查询时用窗口函数算即可，存下来只会多一个能写错的地方（schema v2 删除）：
    --   SELECT material_id, z, count,
    --          count*1.0/SUM(count) OVER (PARTITION BY material_id) AS fraction
    --   FROM material_element;
    PRIMARY KEY (material_id, z)
) STRICT;
CREATE INDEX IF NOT EXISTS ix_melement_z ON material_element(z);

-- ---------------------------------------------------------------- 2 结构信息库（1:1）
CREATE TABLE IF NOT EXISTS structure (
    material_id     TEXT PRIMARY KEY NOT NULL REFERENCES material(material_id) ON DELETE CASCADE,
    a               REAL    CHECK (a IS NULL OR a > 0),
    b               REAL    CHECK (b IS NULL OR b > 0),
    c               REAL    CHECK (c IS NULL OR c > 0),
    alpha           REAL    CHECK (alpha IS NULL OR (alpha > 0 AND alpha < 180)),
    beta            REAL    CHECK (beta  IS NULL OR (beta  > 0 AND beta  < 180)),
    gamma           REAL    CHECK (gamma IS NULL OR (gamma > 0 AND gamma < 180)),
    space_group     INTEGER CHECK (space_group IS NULL OR space_group BETWEEN 1 AND 230),
    crystal_system  TEXT,                      -- 由空间群推导（辅助分类，非"晶相"）
    crystal_phase   TEXT,                      -- 晶相：源表无此字段，恒为 NULL
    cif_url         TEXT,
    cif_sha256      TEXT,                      -- CIF 原文件内容哈希（sha256）
    cif_bytes       INTEGER,
    cif_cache       TEXT,                      -- 下载时的本地缓存路径（出处标注，非运行期依赖）
    -- 原文件本身：官网 /material/<uid>/download/cif 的字节，一字不改
    -- 校验：sha256(cif_content) = cif_sha256（体检第 13 项全量重算）
    cif_content     BLOB,
    cell_volume     REAL    CHECK (cell_volume IS NULL OR cell_volume > 0),  -- 由 a,b,c,α,β,γ 推导
    origin_note     TEXT                       -- 数值出处/派生说明
) STRICT;
CREATE INDEX IF NOT EXISTS ix_structure_sg ON structure(space_group);
CREATE INDEX IF NOT EXISTS ix_structure_cs ON structure(crystal_system);

-- ---------------------------------------------------------------- 3 电学性能子库（1:1）
-- 方法/泛函写在 property_dictionary，本表只存数值：一个格 = 一种方法的结果
CREATE TABLE IF NOT EXISTS electrical (
    material_id          TEXT PRIMARY KEY NOT NULL REFERENCES material(material_id) ON DELETE CASCADE,
    gap_pbe              REAL CHECK (gap_pbe      IS NULL OR gap_pbe      >= 0),
    gap_dir_pbe          REAL CHECK (gap_dir_pbe  IS NULL OR gap_dir_pbe  >= 0),
    gap_hse06            REAL CHECK (gap_hse06    IS NULL OR gap_hse06    >= 0),
    gap_gw               REAL CHECK (gap_gw       IS NULL OR gap_gw       >= 0),
    gap_kind_pbe         TEXT CHECK (gap_kind_pbe IS NULL OR gap_kind_pbe IN ('直接','间接')),
    mobility_e           REAL CHECK (mobility_e   IS NULL OR mobility_e   >= 0),  -- 官网无此字段
    mobility_h           REAL CHECK (mobility_h   IS NULL OR mobility_h   >= 0),  -- 官网无此字段
    eff_mass_cbm         REAL CHECK (eff_mass_cbm IS NULL OR eff_mass_cbm >= 0),  -- emass_cbm（DOS, PBE）
    eff_mass_vbm         REAL CHECK (eff_mass_vbm IS NULL OR eff_mass_vbm >= 0),  -- emass_vbm（DOS, PBE）
    dielectric_static    REAL CHECK (dielectric_static IS NULL OR dielectric_static > 0), -- 官网无此字段
    polarizability_el    REAL CHECK (polarizability_el    IS NULL OR polarizability_el    >= 0),
    polarizability_lat   REAL CHECK (polarizability_lat   IS NULL OR polarizability_lat   >= 0),
    polarizability_total REAL CHECK (polarizability_total IS NULL OR polarizability_total >= 0),
    -- 不存"一致性自检"结果列（v4 删除）：源表的 el+lat vs total 对账结论属于审核信息，
    -- 需要时现算：abs(el+lat-total) 与源表精度比较即可，不必占一列。
    -- 极化率不是介电常数：本表用列名 + 下面的 CHECK 双重防混淆
    CHECK (dielectric_static IS NULL OR polarizability_total IS NULL
           OR dielectric_static <> polarizability_total)
) STRICT;
CREATE INDEX IF NOT EXISTS ix_electrical_gap ON electrical(gap_pbe);

-- ---------------------------------------------------------------- 4 辅助：稳定性/能量（评分依据）
CREATE TABLE IF NOT EXISTS stability (
    material_id TEXT PRIMARY KEY NOT NULL REFERENCES material(material_id) ON DELETE CASCADE,
    dyn_stab    TEXT CHECK (dyn_stab IS NULL OR dyn_stab IN ('Yes','No','Unknown')),
    ehull       REAL,                          -- 能量高于凸包 eV/atom（数值上可能为 -0.0）
    hform       REAL,
    energy      REAL
) STRICT;

-- ---------------------------------------------------------------- 5 字典：性质 → 方法/泛函/单位
CREATE TABLE IF NOT EXISTS property_dictionary (
    property_key    TEXT PRIMARY KEY NOT NULL,
    name_cn         TEXT NOT NULL,
    db_column       TEXT NOT NULL,             -- 落在哪张表哪一列
    db_table        TEXT NOT NULL CHECK (db_table IN ('electrical','stability','structure')),
    unit            TEXT,
    method          TEXT,                      -- PBE / HSE06 / G0W0 / RPA / 待定
    functional      TEXT,                      -- 完整泛函标记，如 HSE06@PBE
    functional_base TEXT,                      -- 起点结构泛函，如 PBE
    precision       INTEGER,                   -- 显示精度（官网 3 位小数）
    source_column   TEXT,                      -- 源表列名
    origin          TEXT NOT NULL CHECK (origin IN
                        ('c2db_table','c2db_detail','cif','derived','own_calc','no_field')),
    availability    TEXT NOT NULL CHECK (availability IN ('source','derived','no_field')),
    note            TEXT
) STRICT;

-- ---------------------------------------------------------------- 6 字典：字段级说明（列目录）
CREATE TABLE IF NOT EXISTS field_dictionary (
    column_key    TEXT PRIMARY KEY NOT NULL,   -- 如 electrical.gap_hse06
    table_name    TEXT NOT NULL,
    column_name   TEXT NOT NULL,
    name_cn       TEXT NOT NULL,
    sublibrary    TEXT NOT NULL,
    unit          TEXT,
    origin        TEXT NOT NULL,
    source_column TEXT,
    missing_policy TEXT,
    note          TEXT
) STRICT;

-- ---------------------------------------------------------------- 7 逐行缺失登记（缺失 = 有列但没值）
CREATE TABLE IF NOT EXISTS data_gap (
    material_id  TEXT NOT NULL REFERENCES material(material_id) ON DELETE CASCADE,
    property_key TEXT NOT NULL REFERENCES property_dictionary(property_key) ON DELETE RESTRICT,
    reason       TEXT NOT NULL CHECK (reason IN
                    ('not_computed','no_field','not_applicable','unknown','fetch_failed')),
    detail       TEXT,
    PRIMARY KEY (material_id, property_key)
) STRICT;
CREATE INDEX IF NOT EXISTS ix_gap_reason ON data_gap(property_key, reason);

-- =====================================================================
-- 视图：封装后的只读接口（等价于"完整对象"的访问入口）
-- =====================================================================

-- 一条材料 = 一行完整对象：三子库 JOIN + 评分/筛选结论
CREATE VIEW IF NOT EXISTS v_material_full AS
SELECT m.material_id                AS id,
       m.formula,
       m.reduced_formula,
       m.name,
       m.name_source,
       m.composition,
       m.composition_json,
       m.n_elements,
       m.category,
       m.has_metal,
       m.category_note,
       s.space_group,
       s.crystal_system,
       s.crystal_phase,
       s.a, s.b, s.c, s.alpha, s.beta, s.gamma,
       s.cell_volume,
       s.cif_url, s.cif_sha256, s.cif_bytes, s.cif_cache,
       e.gap_pbe,
       e.gap_dir_pbe,
       e.gap_hse06,
       e.gap_gw,
       e.gap_kind_pbe,
       e.mobility_e, e.mobility_h,
       e.eff_mass_cbm, e.eff_mass_vbm,
       e.dielectric_static,
       e.polarizability_el, e.polarizability_lat, e.polarizability_total,
       st.dyn_stab, st.ehull, st.hform, st.energy,
       m.score, m.verdict, m.verdict_reason, m.source_url
FROM material m
LEFT JOIN structure  s  ON s.material_id  = m.material_id
LEFT JOIN electrical e  ON e.material_id  = m.material_id
LEFT JOIN stability  st ON st.material_id = m.material_id;

-- 方法/泛函一览（解释每个数值是怎么算出来的）
CREATE VIEW IF NOT EXISTS v_method_map AS
SELECT property_key, name_cn, db_table, db_column, unit,
       method, functional, functional_base, precision,
       source_column, origin, availability, note
FROM property_dictionary
ORDER BY db_table, property_key;

-- 逐材料缺失清单（含"源表根本没有"的全局字段）
CREATE VIEW IF NOT EXISTS v_material_missing AS
SELECT material_id, property_key, reason, detail, 0 AS is_global
FROM data_gap
UNION ALL
SELECT m.material_id, d.property_key, 'no_field', d.note, 1
FROM material m
CROSS JOIN property_dictionary d
WHERE d.availability = 'no_field';

-- 缺失矩阵（按字段汇总，分母 = 当前库材料数）
CREATE VIEW IF NOT EXISTS v_missing_counts AS
SELECT d.property_key,
       d.name_cn,
       d.availability,
       (SELECT COUNT(*) FROM material) AS n_total,
       COUNT(g.material_id)            AS n_missing,
       ROUND(1.0 * COUNT(g.material_id) / (SELECT COUNT(*) FROM material), 4) AS missing_rate
FROM property_dictionary d
LEFT JOIN data_gap g ON g.property_key = d.property_key
GROUP BY d.property_key, d.name_cn, d.availability;

-- 类别汇总
CREATE VIEW IF NOT EXISTS v_category_summary AS
SELECT m.category,
       m.has_metal,
       COUNT(*)                          AS n_materials,
       ROUND(AVG(m.score), 2)            AS avg_score,
       ROUND(AVG(e.gap_pbe), 3)          AS avg_gap_pbe,
       SUM(CASE WHEN e.gap_kind_pbe = '直接' THEN 1 ELSE 0 END) AS n_direct
FROM material m
LEFT JOIN electrical e ON e.material_id = m.material_id
GROUP BY m.category, m.has_metal;
