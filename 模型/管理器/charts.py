# -*- coding: utf-8 -*-
"""charts.py：把台账里的数字画成图。

设计
----
* 只读台账（`mode=ro` 连接），画的每个点都能追溯到 prediction / metric 表；
* 每张图登记 (fig_id, run_id, 配方, sha256) 到 figure 表 —— 图丢了照配方重画，
  也能证明"图不是手改的"；
* 文件名单调：fig_<run 短码>_<序号>_<含义>.png / fig_cmp_<序号>_<含义>.png，
  纯 ASCII 防跨机器乱码；中文只出现在图内标题；
* 常量 150 dpi、8~9 英寸宽，A4 打印和屏幕都够看。

用法
----
    python charts.py              # 全部重画
    python charts.py --only cmp   # 只画跨 run 对比图
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")                      # 无界面后端，服务器/脚本安全

import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
import splitguard
from paths import CATALOG_DB, FIGURE_ROOT, PROJECT_ROOT

DPI = 150
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["figure.autolayout"] = False
plt.rcParams["savefig.bbox"] = "tight"

C = {"train": "#9ecae1", "valid": "#4292c6", "test": "#08519c",
     "ok": "#2f7f4f", "leak": "#b4462f", "smoke": "#9aa0a6",
     "accent": "#c9a227", "grid": "#e3e6ea"}


# ---------------------------------------------------------------- 小工具
def _sha256(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _r2(y: np.ndarray, p: np.ndarray) -> float:
    ss_res = float(((y - p) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    return 1.0 - ss_res / ss_tot


def _rmse(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.sqrt(((y - p) ** 2).mean()))


def _mae(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.abs(y - p).mean())


def _style(ax) -> None:
    ax.grid(True, color=C["grid"], linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)


def _footnote(fig, note: str = "") -> None:
    """图脚注：第一行永远是"三集各多少条 + 测试集干净"的固定声明。

    为什么每张图都写
    ----------------
    看图的人第一件事会问"这图用了多少数据、测试集干不干净"。
    与其让人去翻文档，不如印在图上；数字取自 splitguard，和 verify 用的是同一份。

    位置怎么定
    ----------
    不用固定 y 值 —— 有的图 x 轴刻度是两行（如 "G2 / 7efdfc"），固定 y 会压上去。
    这里先 draw 一次，量出本图所有坐标轴、刻度文字、轴标签的最低像素，再往下让 3% 画布高度。
    """
    fig.canvas.draw()
    r = fig.canvas.get_renderer()
    lows = []
    for ax in fig.axes:
        lows.append(ax.get_window_extent(r).y0)
        for t in ax.get_xticklabels():
            if t.get_text().strip():
                lows.append(t.get_window_extent(r).y0)
        try:
            lows.append(ax.xaxis.label.get_window_extent(r).y0)
        except Exception:
            pass
    low = min(lows) if lows else fig.bbox.y0
    y = fig.transFigure.inverted().transform((0, low))[1] - 0.035
    fig.text(0.01, y, splitguard.fig_footer_text(),
             fontsize=8.2, color="#333", fontweight="bold", va="top")
    if note:
        fig.text(0.01, y - 0.045, note, fontsize=7.5, color="#666", va="top")


def _save(fig, fig_id: str, title: str, kind: str, run_id: str | None,
          recipe: dict, note: str = "") -> dict:
    FIGURE_ROOT.mkdir(parents=True, exist_ok=True)
    path = FIGURE_ROOT / f"{fig_id}.png"
    _footnote(fig, note)
    fig.savefig(path, dpi=DPI)
    w, h = fig.get_size_inches() * DPI
    plt.close(fig)
    return {"fig_id": fig_id, "run_id": run_id, "kind": kind, "title": title,
            "path": path.relative_to(PROJECT_ROOT).as_posix(), "bytes": path.stat().st_size,
            "sha256": _sha256(path), "width_px": int(round(w)), "height_px": int(round(h)),
            "dpi": DPI, "recipe_json": json.dumps(recipe, ensure_ascii=False, sort_keys=True)}


def _short(run_id: str) -> str:
    return run_id.split("-")[-1]


# ---------------------------------------------------------------- 取数
def _pred(conn, run_id: str) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    out: dict[str, tuple] = {}
    for split in ("train", "valid", "test"):
        rs = db.rows(conn, "SELECT y_true, y_pred FROM prediction WHERE run_id = ? AND split = ?",
                     (run_id, split))
        if rs:
            out[split] = (np.array([r["y_true"] for r in rs]),
                          np.array([r["y_pred"] for r in rs]))
    return out


def _curve(conn, run_id: str, split: str, name: str = "rmse") -> tuple[list[int], list[float]]:
    rs = db.rows(conn, "SELECT step, value FROM metric WHERE run_id = ? AND split = ? "
                       "AND name = ? AND step >= 0 ORDER BY step", (run_id, split, name))
    return [r["step"] for r in rs], [r["value"] for r in rs]


# ---------------------------------------------------------------- 单 run 图
def fig_pred_true(conn, run: dict) -> dict:
    rid = run["run_id"]
    data = _pred(conn, rid)
    c = splitguard.audit()["counts"]
    fig, axes = plt.subplots(1, 2, figsize=(10.5, 5.0))
    for ax, split, color in ((axes[0], "valid", C["valid"]), (axes[1], "test", C["test"])):
        if split not in data:
            ax.set_visible(False)
            continue
        y, p = data[split]
        ax.scatter(y, p, s=14, alpha=0.55, color=color, edgecolors="none")
        lo = float(min(y.min(), p.min()))
        hi = float(max(y.max(), p.max()))
        pad = 0.04 * (hi - lo)
        ax.plot([lo - pad, hi + pad], [lo - pad, hi + pad], "--", color="#555", linewidth=1,
                label="理想线 y = x")
        ax.set_xlim(lo - pad, hi + pad)
        ax.set_ylim(lo - pad, hi + pad)
        ax.set_xlabel("真实带隙 gap_pbe (eV)")
        ax.set_ylabel("预测带隙 (eV)")
        cn = "验证集" if split == "valid" else "测试集"
        ax.set_title(f"{cn}  n = {len(y)} 条", fontsize=11)
        ax.text(0.03, 0.97,
                f"R² = {_r2(y, p):.4f}\nRMSE = {_rmse(y, p):.3f} eV\nMAE = {_mae(y, p):.3f} eV",
                transform=ax.transAxes, va="top", ha="left", fontsize=9,
                bbox=dict(boxstyle="round,pad=0.35", fc="white", ec="#ccc", alpha=0.9))
        _style(ax)
        ax.legend(loc="lower right", fontsize=8, frameon=False)
    fig.suptitle(f"预测 vs 真实 · {run['feature_group']} · {_short(rid)}", fontsize=12)
    fig.tight_layout()
    return _save(fig, f"fig_{_short(rid)}_01_pred_true",
                 "预测值 vs 真实值（验证集 / 测试集）", "pred_true", rid,
                 {"table": "prediction", "split": ["valid", "test"], "run_id": rid},
                 note=f"本图只画验证集 {c['valid']} 条与测试集 {c['test']} 条；"
                      f"训练集 {c['train']} 条未入图（点太密会遮住趋势）")


def fig_residual(conn, run: dict) -> dict:
    rid = run["run_id"]
    data = _pred(conn, rid)
    if "test" not in data:
        raise RuntimeError("无 test 预测")
    y, p = data["test"]
    r = p - y
    fig, axes = plt.subplots(1, 2, figsize=(10.5, 4.6))
    ax = axes[0]
    ax.hist(r, bins=50, color=C["test"], alpha=0.85, edgecolor="white", linewidth=0.3)
    ax.axvline(0, color="#333", linewidth=1, linestyle="--")
    ax.axvline(float(r.mean()), color=C["accent"], linewidth=1.2,
               label=f"均值 {r.mean():+.3f} eV")
    ax.set_xlabel("残差 = 预测 − 真实 (eV)")
    ax.set_ylabel("材料数")
    ax.set_title("残差分布（测试集）")
    ax.legend(fontsize=8, frameon=False)
    _style(ax)

    ax = axes[1]
    ax.scatter(y, r, s=14, alpha=0.55, color=C["test"], edgecolors="none")
    ax.axhline(0, color="#333", linewidth=1, linestyle="--")
    ax.set_xlabel("真实带隙 (eV)")
    ax.set_ylabel("残差 (eV)")
    ax.set_title("残差 vs 真实值（看有没有系统性偏差）")
    # 分箱中位线，暴露"大带隙普遍低估/高估"
    edges = np.linspace(y.min(), y.max(), 9)
    idx = np.clip(np.digitize(y, edges[1:-1]), 0, len(edges) - 2)
    xs, med = [], []
    for b in range(len(edges) - 1):
        m = idx == b
        if m.sum() >= 5:
            xs.append(float(y[m].mean()))
            med.append(float(np.median(r[m])))
    ax.plot(xs, med, "o-", color=C["accent"], linewidth=1.4, markersize=4,
            label="分箱中位残差")
    ax.legend(fontsize=8, frameon=False)
    _style(ax)
    fig.suptitle(f"残差体检 · {run['feature_group']} · {_short(rid)}", fontsize=12)
    fig.tight_layout()
    c = splitguard.audit()["counts"]
    return _save(fig, f"fig_{_short(rid)}_02_residual",
                 "残差分布与系统性偏差", "residual", rid,
                 {"table": "prediction", "split": "test", "run_id": rid},
                 note=f"只画测试集 {c['test']} 条 —— 这一集训练时一次都没参与调参，"
                      f"是这份模型最干净的成绩单")


def fig_learning(conn, run: dict) -> dict:
    rid = run["run_id"]
    c = splitguard.audit()["counts"]
    fig, ax = plt.subplots(figsize=(8.6, 4.8))
    got = False
    for split, color in (("train", C["train"]), ("valid", C["valid"])):
        xs, ys = _curve(conn, rid, split, "rmse")
        if xs:
            cn = "训练集" if split == "train" else "验证集"
            ax.plot(xs, ys, color=color, linewidth=1.4,
                    label=f"{cn} RMSE（n = {c[split]} 条）")
            got = True
    if not got:
        raise RuntimeError("无逐步指标")
    best = run.get("best_iteration")
    if isinstance(best, int):
        ax.axvline(best, color=C["accent"], linewidth=1.1, linestyle="--",
                   label=f"早停点 best_iteration={best}")
    ax.set_xlabel("第 n 棵树")
    ax.set_ylabel("RMSE (eV)")
    ax.set_title(f"学习曲线 · {run['feature_group']} · {_short(rid)}")
    ax.legend(fontsize=8, frameon=False)
    ax.set_yscale("log")
    _style(ax)
    fig.tight_layout()
    return _save(fig, f"fig_{_short(rid)}_03_learning",
                 "训练/验证 RMSE 随迭代变化（对数轴）", "learning", rid,
                 {"table": "metric", "name": "rmse", "step": ">=0", "run_id": rid},
                 note="训练集一路下降、验证集走平 → 之后继续加树只会更过拟合；最终只保留 best_iteration 那一棵。\n"
                      "测试集不在这条曲线上：它既不参与早停，也不参与选树。")


def fig_importance(conn, run: dict, top: int = 15) -> dict:
    rid = run["run_id"]
    rs = db.rows(conn, "SELECT feature, gain FROM feature_importance WHERE run_id = ? "
                       "ORDER BY gain DESC LIMIT ?", (rid, top))
    if not rs:
        raise RuntimeError("无特征重要性")
    used = db.scalar(conn, "SELECT count(*) FROM feature_importance WHERE run_id = ?", (rid,))
    names = [r["feature"] for r in rs][::-1]
    vals = [r["gain"] for r in rs][::-1]
    fig, ax = plt.subplots(figsize=(8.6, 0.34 * len(names) + 1.6))
    ax.barh(names, vals, color=C["valid"], height=0.68)
    ax.set_xlabel("gain（该特征在所有树里带来的增益总和）")
    ax.set_title(f"特征重要性 Top{len(names)} · {run['feature_group']} · {_short(rid)}")
    for i, v in enumerate(vals):
        ax.text(v, i, f" {v:,.2f}", va="center", fontsize=7.5, color="#444")
    ax.margins(x=0.14)
    _style(ax)
    fig.tight_layout()
    return _save(fig, f"fig_{_short(rid)}_04_importance",
                 "特征重要性 Top15（gain）", "importance", rid,
                 {"table": "feature_importance", "order": "gain desc", "top": top, "run_id": rid},
                 note=f"本 run 共 {run['n_features']} 个特征，其中 {used} 个被树用到；"
                      f"gain=0 的特征 xgboost 不输出，故条形不足 {run['n_features']} 条属正常")


# ---------------------------------------------------------------- 跨 run 图
def fig_compare_r2(conn) -> dict:
    rs = db.rows(conn, "SELECT run_id, feature_group, n_features, is_smoke, is_leak, "
                       "train_r2, valid_r2, test_r2, best_iteration FROM v_run_score "
                       "ORDER BY created_at")
    if not rs:
        raise RuntimeError("台账为空，先跑 record.py")
    labels = [f"{r['feature_group']}\n{_short(r['run_id'])}" for r in rs]
    x = np.arange(len(rs))
    w = 0.27
    fig, ax = plt.subplots(figsize=(max(8.6, 1.9 * len(rs)), 5.0))
    for k, (key, color, name) in enumerate((
            ("train_r2", C["train"], "训练集 R²"),
            ("valid_r2", C["valid"], "验证集 R²"),
            ("test_r2", C["test"], "测试集 R²"))):
        vals = [r[key] if r[key] is not None else 0.0 for r in rs]
        bars = ax.bar(x + (k - 1) * w, vals, w, color=color, label=name)
        for b, r in zip(bars, rs):
            if r[key] is None:
                b.set_hatch("///")
                b.set_alpha(0.4)
            else:
                ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 0.012,
                        f"{r[key]:.3f}", ha="center", fontsize=7.5, rotation=90)
    for i, r in enumerate(rs):
        tag = "冒烟" if r["is_smoke"] else ("泄漏对照" if r["is_leak"] else "")
        if tag:
            ax.annotate(tag, (i, 0.06), ha="center", fontsize=8.5, color="#8a1f11",
                        fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_ylim(0, 1.22)
    ax.set_ylabel("R²")
    ax.set_title("各次训练 R² 对照（同一快照、同一划分，只换特征组）")
    ax.axhline(0.9, color=C["grid"], linewidth=1)
    ax.legend(fontsize=8, frameon=False, ncol=3, loc="upper left")
    _style(ax)
    fig.tight_layout()
    return _save(fig, "fig_cmp_01_r2", "各 run 训练/验证/测试 R² 对照", "compare", None,
                 {"view": "v_run_score", "metric": "r2", "order": "created_at"},
                 note="冒烟测试（20 棵树）与两组泄漏对照都留在图上：低分不是模型不行、高分不是模型厉害；\n"
                      "选型只看 v_leaderboard 的行。四次训练用的是同一份划分，三集条数与材料完全一致。")


def fig_leak_gap(conn) -> dict:
    """主叙事：同一份数据，只把"同源列"和"答案列"放进去，R² 会跳到多少。"""
    rs = db.rows(conn, "SELECT run_id, feature_group, n_features, is_leak, valid_r2, test_r2, "
                       "test_rmse, note FROM v_run_score WHERE is_smoke = 0 "
                       "ORDER BY CASE feature_group WHEN 'G2' THEN 0 WHEN 'G3' THEN 1 "
                       "WHEN 'G3p' THEN 2 ELSE 3 END, n_features")
    if len(rs) < 2:
        raise RuntimeError("至少需要 2 个非冒烟 run")
    labels = [f"{r['feature_group']}\n{r['n_features']} 列" for r in rs]
    x = np.arange(len(rs))
    w = 0.34
    fig, ax = plt.subplots(figsize=(max(8.6, 1.8 * len(rs)), 5.2))
    for k, (key, color, name) in enumerate((("valid_r2", C["valid"], "验证集 R²"),
                                            ("test_r2", C["test"], "测试集 R²"))):
        vals = [r[key] or 0.0 for r in rs]
        bars = ax.bar(x + (k - 0.5) * w, vals, w, color=color, label=name)
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, v + 0.012, f"{v:.3f}",
                    ha="center", fontsize=8.5)
    base = next((r for r in rs if not r["is_leak"]), rs[0])
    for i, r in enumerate(rs):
        if r is base or not r["is_leak"]:
            continue
        d = (r["test_r2"] or 0) - (base["test_r2"] or 0)
        if abs(d) >= 0.06:      # 差得少就不画箭头，免得和虚线缠在一起
            ax.annotate("", xy=(i + 0.5 * w, r["test_r2"] or 0),
                        xytext=(i + 0.5 * w, base["test_r2"] or 0),
                        arrowprops=dict(arrowstyle="<->", color=C["leak"], linewidth=1.1,
                                        shrinkA=2, shrinkB=2))
        ax.text(i + 0.5 * w, (r["test_r2"] or 0) + 0.055, f"ΔR² {d:+.3f}",
                color=C["leak"], fontsize=9.5, fontweight="bold", ha="center", va="bottom")
    ax.axhline(base["test_r2"] or 0, color=C["ok"], linewidth=1, linestyle="--",
               label=f"主模型 {base['feature_group']} 的 test R² = {base['test_r2']:.3f}")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=9.5)
    ax.set_ylim(0, 1.14)
    ax.set_ylabel("R²")
    ax.set_title("为什么主模型不用「同源列」：加了会虚高，但那是泄漏")
    ax.legend(fontsize=8, frameon=False, loc="upper left")
    _style(ax)
    fig.tight_layout()
    return _save(fig, "fig_cmp_02_leak", "泄漏对照：同源列与答案列把 R² 抬了多少", "leak", None,
                 {"view": "v_run_score", "filter": "is_smoke=0", "baseline": base["run_id"]},
                 note="G3 = 主特征 + 有效质量/极化率（与带隙同一套 DFT 派生，涨得少是因为这些列本身缺 17%~41%）；\n"
                      "G3p = 主特征 + gap_dir_pbe（带隙的分量，等于给了半个答案，所以直接冲到 0.987）。\n"
                      "三个 run 的测试集是同一批 500 条材料 —— 差异只可能来自特征，不可能来自数据。")


def fig_err_by_gap(conn, run_ids: list[str] | None = None, nbins: int = 8) -> dict:
    """误差随带隙大小怎么变——回答"这模型在哪不可信"。"""
    if run_ids is None:
        run_ids = [r["run_id"] for r in db.rows(
            conn, "SELECT run_id FROM v_run_score WHERE is_smoke = 0 ORDER BY n_features")]
    fig, ax = plt.subplots(figsize=(8.6, 5.0))
    edges = np.array([0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0, 9.5])
    palette = [C["ok"], C["leak"], C["accent"], "#6a4c93"]
    ymax_seen = 0.0
    xmax_seen = 0.0
    for k, rid in enumerate(run_ids):
        y, p = _pred(conn, rid)["test"]
        ymax_seen = max(ymax_seen, float(y.max()))
        idx = np.clip(np.digitize(y, edges[1:-1]), 0, len(edges) - 2)
        xs, ys, ns = [], [], []
        for b in range(len(edges) - 1):
            m = idx == b
            if m.sum() >= 8:
                xs.append(float(y[m].mean()))
                ys.append(_rmse(y[m], p[m]))
                ns.append(int(m.sum()))
        grp = db.scalar(conn, "SELECT feature_group FROM run WHERE run_id = ?", (rid,))
        ax.plot(xs, ys, "o-", color=palette[k % len(palette)], linewidth=1.5, markersize=5,
                label=f"{grp}（测试集 n={sum(ns)}）")
        if xs:
            xmax_seen = max(xmax_seen, max(xs))
    for b in range(len(edges) - 1):
        ax.axvspan(edges[b], edges[b + 1], color="#f4f6f8" if b % 2 == 0 else "white", zorder=0)
    ax.set_xlim(0, xmax_seen + 0.6)
    ax.set_xlabel("真实带隙区间 (eV)")
    ax.set_ylabel("该区间 RMSE (eV)")
    ax.set_title("误差随带隙变化（测试集，同一批材料）")
    ax.legend(fontsize=8, frameon=False, loc="upper left")
    _style(ax)
    fig.tight_layout()
    return _save(fig, "fig_cmp_03_err_by_gap", "测试集 RMSE 随真实带隙的分布", "compare", None,
                 {"table": "prediction", "split": "test", "bins": edges.tolist(),
                  "run_ids": run_ids},
                 note="大带隙段样本少、误差大：这批材料的预测只能当趋势看，不能当精确值用。"
                      "（样本少于 8 个的区间不画点；图例里的 n 是实际画到的测试集条数）")


# ---------------------------------------------------------------- 汇总
def build_run_figures(conn, run: dict) -> list[dict]:
    out = []
    for fn in (fig_pred_true, fig_residual, fig_learning, fig_importance):
        try:
            out.append(fn(conn, run))
        except Exception as e:                        # 缺某项数据不致命，记下来继续
            print(f"  [跳过] {run['run_id']} {fn.__name__}: {e}")
    return out


def build_compare_figures(conn) -> list[dict]:
    out = []
    for fn in (fig_compare_r2, fig_leak_gap, fig_err_by_gap):
        try:
            out.append(fn(conn))
        except Exception as e:
            print(f"  [跳过] {fn.__name__}: {e}")
    return out


def register(db_path, metas: list[dict]) -> None:
    conn = db.connect(db_path)
    try:
        with conn:
            for m in metas:
                conn.execute(
                    "INSERT OR REPLACE INTO figure (fig_id, run_id, kind, title, path, bytes,"
                    " sha256, width_px, height_px, dpi, recipe_json, generated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (m["fig_id"], m["run_id"], m["kind"], m["title"], m["path"], m["bytes"],
                     m["sha256"], m["width_px"], m["height_px"], m["dpi"],
                     m["recipe_json"], db.now()))
    finally:
        conn.close()


def build_all(only: str | None = None) -> list[dict]:
    db.init()
    conn = db.connect()
    try:
        runs = db.rows(conn, "SELECT * FROM v_run_score ORDER BY created_at")
        metas: list[dict] = []
        if only in (None, "run"):
            for r in runs:
                print(f"  画 {r['run_id']} ...")
                metas += build_run_figures(conn, r)
        if only in (None, "cmp"):
            print("  画跨 run 对比 ...")
            metas += build_compare_figures(conn)
        register(CATALOG_DB, metas)
        return metas
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    only = None
    if "--only" in argv:
        only = argv[argv.index("--only") + 1]
    print(f"输出目录：{FIGURE_ROOT}")
    metas = build_all(only=only)
    print(f"\n共 {len(metas)} 张：")
    for m in sorted(metas, key=lambda x: x["fig_id"]):
        print(f"  {m['fig_id']:<34} {m['bytes']:>7,} B  {m['width_px']}x{m['height_px']}  "
              f"{m['title']}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
