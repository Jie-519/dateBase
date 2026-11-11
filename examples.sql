-- =====================================================================
-- C2DB 材料数据库 · 示例查询（sqlite3 命令行或任意 SQL 客户端里跑）
--   sqlite3 数据库/C2DB_materials.sqlite < 数据库/examples.sql
-- =====================================================================

-- 1. 一条材料 = 一个完整对象（三子库 JOIN 视图）
SELECT id, formula, category, space_group, crystal_system,
       a, b, c, gap_pbe, gap_hse06, gap_gw, dyn_stab, score
FROM v_material_full
WHERE id = '2As-1';

-- 2. 电子器件筛选：直接带隙 1.0–2.0 eV、动力学稳定、含金属
SELECT id, formula, category, gap_pbe, gap_hse06, space_group
FROM v_material_full
WHERE gap_kind_pbe = '直接'
  AND gap_pbe BETWEEN 1.0 AND 2.0
  AND dyn_stab = 'Yes'
  AND has_metal = 1
ORDER BY gap_pbe DESC
LIMIT 20;

-- 3. 有 G0W0 数据的材料（最可信带隙），按 PBE→GW 修正量排序
SELECT id, formula, gap_pbe, gap_hse06, gap_gw,
       ROUND(gap_gw - gap_pbe, 3) AS gw_correction
FROM v_material_full
WHERE gap_gw IS NOT NULL
ORDER BY gw_correction DESC
LIMIT 20;

-- 4. 按元素找材料（组成表是规范化存储，可精确匹配元素）
SELECT m.material_id, m.formula, m.category, e.gap_pbe, e.gap_hse06
FROM material m
JOIN material_element me ON me.material_id = m.material_id
JOIN element el          ON el.z = me.z
JOIN electrical e         ON e.material_id = m.material_id
WHERE el.symbol = 'Bi'
ORDER BY e.gap_pbe
LIMIT 20;

-- 5. 某空间群的全部材料（看结构族的电学分布）
SELECT space_group, crystal_system, COUNT(*) AS n,
       ROUND(AVG(gap_pbe), 3)  AS avg_gap,
       ROUND(AVG(score), 2)    AS avg_score
FROM v_material_full
GROUP BY space_group, crystal_system
ORDER BY n DESC
LIMIT 15;

-- 6. 某个材料缺什么、为什么缺（含"源表无此字段"的全局项）
SELECT property_key, reason,
       CASE is_global WHEN 1 THEN '源表无此字段' ELSE '缺失' END AS kind,
       detail
FROM v_material_missing
WHERE material_id = '2As-1'
ORDER BY is_global, property_key;

-- 7. 字段缺失矩阵（分母 = 库内材料数）
SELECT property_key, availability, n_missing,
       ROUND(missing_rate * 100, 2) AS missing_pct
FROM v_missing_counts
ORDER BY n_missing DESC;

-- 8. 每个数值是怎么算出来的（方法 / 泛函 / 单位 / 源表列）
SELECT property_key, name_cn, db_column, unit, method, functional, source_column, availability
FROM v_method_map;

-- 9. 类别 × 是否含金属的规模与平均带隙
SELECT category,
       CASE has_metal WHEN 1 THEN '含金属' ELSE '不含金属' END AS metal,
       n_materials, avg_score, avg_gap_pbe, n_direct
FROM v_category_summary
ORDER BY n_materials DESC;

-- 10. 零带隙材料（gap = 0.000 是有效值，不是缺失）
SELECT id, formula, category, gap_pbe, gap_hse06, dyn_stab
FROM v_material_full
WHERE gap_pbe = 0
ORDER BY gap_hse06 DESC
LIMIT 20;

-- 11. 同一约化化学式的不同结构（同族/多晶型）
SELECT reduced_formula, COUNT(*) AS n, GROUP_CONCAT(material_id, ' / ') AS ids
FROM material
GROUP BY reduced_formula
HAVING n > 1
ORDER BY n DESC
LIMIT 10;

-- 12. 数据溯源：这批数据从哪来、什么时候导的
SELECT batch_id, source_file, scope, source_rows, imported_rows, imported_at,
       substr(source_sha256, 1, 16) || '…' AS sha256_head
FROM import_batch;

-- 13. 有效质量：直接带隙 + 轻电子轻空穴（迁移率通常更高）
--     注：eff_mass 是 DOS 有效质量（PBE，m0）；NULL=官网未给出，∞=平带
SELECT id, formula, gap_pbe, gap_hse06,
       eff_mass_cbm, eff_mass_vbm,
       ROUND(eff_mass_cbm * eff_mass_vbm, 4) AS mass_product
FROM v_material_full
WHERE gap_kind_pbe = '直接'
  AND eff_mass_cbm BETWEEN 0.01 AND 0.3
  AND eff_mass_vbm BETWEEN 0.01 AND 0.5
ORDER BY mass_product
LIMIT 20;
