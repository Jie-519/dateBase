-- =====================================================================
-- catalog.sqlite —— 训练台账（索引层）
--
-- 定位（与母库对照，勿混）
--   母库 C2DB_materials.sqlite : 材料属性，只读输入
--   catalog.sqlite             : 训练结果的"目录"，可整库重建
--
-- 三条规矩
--   1. 真相在文件：runs/<run_id>/manifest.json 是唯一权威，本库只是它的索引；
--      全库 drop 掉，跑 `python cli.py record` 就能按原样重建。
--   2. 只增不改：一次训练一行 run，指标/产物作为子表挂上去；
--      重跑同一 run_id 用"先删后插"，绝不就地 UPDATE 数值。
--   3. 每个 run 存 manifest_sha256：文件被手改过，本库能立刻发现对不上。
--
-- 值域约定
--   step = -1  表示该 split 的最终/汇总量（避免 NULL 在唯一键里不等价，
--              SQLite 的 UNIQUE 认为两个 NULL 互不相同，会把重复行放进来）
-- =====================================================================

PRAGMA user_version = 1;

-- ---------------------------------------------------------------- 0. 元信息
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
) STRICT;

-- ---------------------------------------------------------------- 1. 一次训练
CREATE TABLE IF NOT EXISTS run (
    run_id            TEXT PRIMARY KEY,
    run_dir           TEXT NOT NULL UNIQUE,     -- 相对 PROJECT_ROOT，如 模型/XGboost/output_XGb/xxx
    schema_version    TEXT NOT NULL,            -- manifest.schema，如 run/1

    project           TEXT NOT NULL,
    task              TEXT,                     -- regression / classification
    model_family      TEXT,                     -- xgboost
    model_version     TEXT,
    objective         TEXT,
    target            TEXT NOT NULL,            -- gap_pbe

    dataset_id        TEXT NOT NULL,
    dataset_sha256    TEXT NOT NULL,            -- 快照 csv 的 sha256
    snapshot_rows     INTEGER NOT NULL,
    split_seed        INTEGER,
    split_strategy    TEXT,
    split_counts_json TEXT,                     -- {"train":2349,"valid":505,"test":500}

    feature_group     TEXT NOT NULL,            -- G0/G1/G2/G3/G3p
    n_features        INTEGER NOT NULL,
    features_sha256   TEXT NOT NULL,            -- 特征名单的指纹，用来查"名不副实"

    params_json       TEXT NOT NULL,

    best_iteration    INTEGER,
    n_rounds          INTEGER,
    duration_s        REAL,
    missing_cells     INTEGER,                  -- 特征矩阵里的空值格数

    deterministic     INTEGER NOT NULL CHECK (deterministic IN (0, 1)),
    tolerance_json    TEXT,

    env_python        TEXT,
    env_os            TEXT,
    env_git_commit    TEXT,
    env_git_dirty     INTEGER,

    status            TEXT NOT NULL,            -- ok / failed / aborted
    note              TEXT,
    tags_json         TEXT,

    created_at        TEXT NOT NULL,
    finished_at       TEXT NOT NULL,

    manifest_sha256   TEXT NOT NULL,            -- 台账侧防篡改锚
    manifest_bytes    INTEGER NOT NULL,
    verified          INTEGER NOT NULL CHECK (verified IN (0, 1)),
    verify_problems   TEXT,                     -- 多条用 \n 连接；空串 = 干净

    is_smoke          INTEGER NOT NULL DEFAULT 0 CHECK (is_smoke IN (0, 1)),
    is_leak           INTEGER NOT NULL DEFAULT 0 CHECK (is_leak IN (0, 1)),

    imported_at       TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS ix_run_group ON run(feature_group);
CREATE INDEX IF NOT EXISTS ix_run_created ON run(created_at);

-- ---------------------------------------------------------------- 2. 指标（长表）
CREATE TABLE IF NOT EXISTS metric (
    run_id TEXT    NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    split  TEXT    NOT NULL CHECK (split IN ('train', 'valid', 'test')),
    name   TEXT    NOT NULL,                    -- rmse / mae / r2
    step   INTEGER NOT NULL,                    -- -1 = 最终值；>=0 = 第 n 轮
    value  REAL    NOT NULL,
    PRIMARY KEY (run_id, split, name, step)
) STRICT;

-- ---------------------------------------------------------------- 3. 产物清单
CREATE TABLE IF NOT EXISTS artifact (
    run_id  TEXT    NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    path    TEXT    NOT NULL,                   -- 相对 run 目录
    kind    TEXT    NOT NULL,                   -- model / predictions / feature_importance
    bytes   INTEGER NOT NULL,
    sha256  TEXT    NOT NULL,
    present INTEGER NOT NULL CHECK (present IN (0, 1)),  -- 导入时文件是否还在盘上
    PRIMARY KEY (run_id, path)
) STRICT;

-- ---------------------------------------------------------------- 4. 逐条预测
-- 3362 行级别的小表，值得入库：跨 run 对比"哪些材料一直预测不准"最常用。
-- 仍可从 predictions.csv 重建，故只算缓存，不算真相。
CREATE TABLE IF NOT EXISTS prediction (
    run_id      TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    material_id TEXT NOT NULL,
    split       TEXT NOT NULL CHECK (split IN ('train', 'valid', 'test')),
    y_true      REAL NOT NULL,
    y_pred      REAL NOT NULL,
    PRIMARY KEY (run_id, material_id)
) STRICT;

CREATE INDEX IF NOT EXISTS ix_pred_mid ON prediction(material_id);

-- ---------------------------------------------------------------- 5. 特征重要性
CREATE TABLE IF NOT EXISTS feature_importance (
    run_id     TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    feature    TEXT NOT NULL,
    gain       REAL NOT NULL,
    weight     REAL NOT NULL,
    cover      REAL NOT NULL,
    rank_gain  INTEGER,                         -- 1 = 最重要
    rank_weight INTEGER,
    PRIMARY KEY (run_id, feature)
) STRICT;

-- ---------------------------------------------------------------- 6. 图（配方 + 产物）
-- run_id 为空 = 跨 run 的对比图。recipe_json 存画这张图的全部输入，
-- 图丢了照着重画，且能证明"图不是手 P 的"。
CREATE TABLE IF NOT EXISTS figure (
    fig_id       TEXT PRIMARY KEY,               -- 文件名去扩展名
    run_id       TEXT REFERENCES run(run_id) ON DELETE SET NULL,
    kind         TEXT NOT NULL,                  -- pred_true / residual / learning / importance / compare / leak
    title        TEXT NOT NULL,
    path         TEXT NOT NULL,                  -- 相对 PROJECT_ROOT
    bytes        INTEGER NOT NULL,
    sha256       TEXT NOT NULL,
    width_px     INTEGER,
    height_px    INTEGER,
    dpi          INTEGER,
    recipe_json  TEXT NOT NULL,
    generated_at TEXT NOT NULL
) STRICT;

-- ---------------------------------------------------------------- 视图
-- 一次训练一行，指标已摊平成列（只看最终值 step = -1）
CREATE VIEW IF NOT EXISTS v_run_score AS
SELECT r.run_id,
       r.project,
       r.feature_group,
       r.n_features,
       r.target,
       r.is_smoke,
       r.is_leak,
       r.status,
       r.note,
       r.best_iteration,
       r.n_rounds,
       r.duration_s,
       r.created_at,
       MAX(CASE WHEN m.split = 'train' AND m.name = 'r2'   THEN m.value END) AS train_r2,
       MAX(CASE WHEN m.split = 'valid' AND m.name = 'r2'   THEN m.value END) AS valid_r2,
       MAX(CASE WHEN m.split = 'test'  AND m.name = 'r2'   THEN m.value END) AS test_r2,
       MAX(CASE WHEN m.split = 'train' AND m.name = 'rmse' THEN m.value END) AS train_rmse,
       MAX(CASE WHEN m.split = 'valid' AND m.name = 'rmse' THEN m.value END) AS valid_rmse,
       MAX(CASE WHEN m.split = 'test'  AND m.name = 'rmse' THEN m.value END) AS test_rmse,
       MAX(CASE WHEN m.split = 'test'  AND m.name = 'mae'  THEN m.value END) AS test_mae
FROM run r
LEFT JOIN metric m ON m.run_id = r.run_id AND m.step = -1
GROUP BY r.run_id;

-- 能拿去用的榜：剔除冒烟测试与泄漏对照，按 valid R² 排（test 不参与选型）
CREATE VIEW IF NOT EXISTS v_leaderboard AS
SELECT * FROM v_run_score
WHERE is_smoke = 0 AND is_leak = 0
ORDER BY valid_r2 DESC;

-- 泄漏对照专用视图：就是要看"加了同源列会虚高多少"
CREATE VIEW IF NOT EXISTS v_leak_report AS
SELECT run_id, feature_group, n_features, is_leak, note,
       valid_r2, test_r2, test_rmse, test_mae, best_iteration
FROM v_run_score
WHERE is_smoke = 0
ORDER BY feature_group;
