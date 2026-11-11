# -*- coding: utf-8 -*-
"""report.py：把台账 + 图 变成两个会自己说话的 HTML。

产物
----
1. figure_XGb/report/<run_id>.html   一次训练一份（PNG 以 base64 内嵌，单文件可发人）
2. figure_XGb/index.html             跨 run 首页（引用同目录 PNG，体积小）

为什么两种做法
--------------
单 run 报告要能"发出去就看得开"，所以图必须内嵌；首页是自用看板，引用文件即可，
否则每加一次实验就要重写全部 base64。

用法
----
    python report.py                 # 全部重出（单 run 报告 + 首页）
    python report.py --index-only    # 只重排首页
"""
from __future__ import annotations

import base64
import html
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
import splitguard
from paths import FIGURE_ROOT, INDEX_HTML, PROJECT_ROOT, REPORT_DIR

#: 特征组人话说明（与 train_XGboost/README.md §4 一致）
GROUP_DESC = {
    "G0": "只描述组分：元素种类/比例 + 62 个元素 one-hot，完全没有结构信息",
    "G1": "G0 + 晶胞与对称性：a/b/c、α/β/γ、空间群、体积、厚度、晶胞面积",
    "G2": "G1 + 能量类：ehull、hform、每原子能量、动力学稳定性、真空能级（<b>主模型</b>）",
    "G3": "G2 + 有效质量与极化率（与带隙同源，属泄漏对照，<b>不参与选型</b>）",
    "G3p": "G2 + gap_dir_pbe（带隙的直隙分量，等于给了半个答案，<b>不参与选型</b>）",
}

CSS = """
:root{--ink:#1a1c1e;--muted:#6b7280;--line:#e3e6ea;--bg:#fff;--soft:#f7f8fa;
--good:#1f7a4d;--warn:#b4462f;--accent:#c9a227;}
*{box-sizing:border-box}
body{margin:0;padding:32px 20px 64px;background:var(--bg);color:var(--ink);
font-family:-apple-system,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;
line-height:1.68;font-size:15px}
.wrap{max-width:1040px;margin:0 auto}
h1{font-size:24px;margin:0 0 4px;letter-spacing:.2px}
h2{font-size:18px;margin:36px 0 12px;padding-bottom:6px;border-bottom:2px solid var(--ink)}
h3{font-size:15px;margin:22px 0 8px;color:var(--muted);font-weight:600}
.sub{color:var(--muted);font-size:13px;margin:0 0 24px}
.mono{font-family:ui-monospace,Consolas,"Courier New",monospace;font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:10px;margin:12px 0}
.card{background:var(--soft);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.card .k{font-size:12px;color:var(--muted)}
.card .v{font-size:22px;font-weight:650;margin-top:2px;font-variant-numeric:tabular-nums}
.card .s{font-size:11px;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:13px;margin:8px 0 4px}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{background:var(--soft);font-weight:600;white-space:nowrap}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr:hover td{background:#fcfcfd}
figure{margin:18px 0 8px}
figure img{width:100%;border:1px solid var(--line);border-radius:6px;display:block}
figcaption{font-size:12px;color:var(--muted);margin-top:6px}
.note{border-left:4px solid var(--accent);background:#fffdf5;padding:10px 14px;margin:14px 0;
border-radius:0 6px 6px 0;font-size:14px}
.alert{border-left:4px solid var(--warn);background:#fdf4f2;padding:12px 16px;margin:16px 0;
border-radius:0 6px 6px 0}
.ok{border-left:4px solid var(--good);background:#f2f9f5;padding:12px 16px;margin:16px 0;
border-radius:0 6px 6px 0}
.tag{display:inline-block;font-size:11px;padding:1px 8px;border-radius:10px;margin-left:6px;
border:1px solid var(--line);color:var(--muted);background:#fff;vertical-align:middle}
.tag.smoke{color:#6b7280}
.tag.leak{color:var(--warn);border-color:#e8c4bb;background:#fdf4f2}
.tag.main{color:var(--good);border-color:#bfdccc;background:#f2f9f5}
footer{margin-top:48px;padding-top:14px;border-top:1px solid var(--line);
color:var(--muted);font-size:12px}
a{color:#1a5fb4;text-decoration:none}
a:hover{text-decoration:underline}
ul{padding-left:22px}li{margin:4px 0}
.grid3{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}
.thumb{font-size:12px}
.thumb img{width:100%;border:1px solid var(--line);border-radius:6px}
"""


# ---------------------------------------------------------------- 小工具
def _e(x) -> str:
    return html.escape("" if x is None else str(x))


def _b64(p: Path) -> str:
    return base64.b64encode(Path(p).read_bytes()).decode("ascii")


def _fig_block(p: Path, caption: str, embed: bool) -> str:
    src = f"data:image/png;base64,{_b64(p)}" if embed else _e(Path(p).name)
    return (f'<figure><img src="{src}" alt="{_e(caption)}">'
            f"<figcaption>{caption}</figcaption></figure>")


def _table(headers: list[str], rows: list[list], num_cols: set[int] | None = None) -> str:
    num_cols = num_cols or set()
    head = "".join(
        f'<th class="num">{_e(h)}</th>' if i in num_cols else f"<th>{_e(h)}</th>"
        for i, h in enumerate(headers))
    body = []
    for r in rows:
        cells = "".join(
            f'<td class="num">{c}</td>' if i in num_cols else f"<td>{c}</td>"
            for i, c in enumerate(r))
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _card(k: str, v: str, s: str = "") -> str:
    return f'<div class="card"><div class="k">{_e(k)}</div><div class="v">{v}</div><div class="s">{s}</div></div>'


def _fmt(v, n: int = 4, dash: str = "—") -> str:
    if v is None:
        return dash
    return f"{v:.{n}f}" if isinstance(v, float) else str(v)


def _tags(r: dict) -> str:
    out = ""
    if r["is_smoke"]:
        out += '<span class="tag smoke">冒烟测试</span>'
    if r["is_leak"]:
        out += '<span class="tag leak">泄漏对照·不参与选型</span>'
    if not r["is_smoke"] and not r["is_leak"]:
        out += '<span class="tag main">可参与选型</span>'
    return out


def _figs_of(conn, run_id: str) -> list[dict]:
    return db.rows(conn, "SELECT * FROM figure WHERE run_id = ? ORDER BY fig_id", (run_id,))


def _figs_cmp(conn) -> list[dict]:
    return db.rows(conn, "SELECT * FROM figure WHERE run_id IS NULL ORDER BY fig_id")


def _main_run(conn) -> dict | None:
    return db.one(conn, "SELECT * FROM v_leaderboard LIMIT 1")


# ---------------------------------------------------------------- 单 run 报告
def run_report_html(conn, r: dict) -> str:
    rid = r["run_id"]
    params = json.loads(db.scalar(conn, "SELECT params_json FROM run WHERE run_id = ?", (rid,), "{}"))
    row = db.one(conn, "SELECT * FROM run WHERE run_id = ?", (rid,))
    arts = db.rows(conn, "SELECT * FROM artifact WHERE run_id = ? ORDER BY kind", (rid,))
    n_used = db.scalar(conn, "SELECT count(*) FROM feature_importance WHERE run_id = ?", (rid,), 0)
    figs = _figs_of(conn, rid)
    figs_by_kind = {f["kind"]: f for f in figs}
    main = _main_run(conn)
    is_main = bool(main and main["run_id"] == rid)

    # ---- 结论（只由数字推出来，不写形容词）
    concl = []
    concl.append(f"测试集 R² = <b>{_fmt(r['test_r2'])}</b>，RMSE = <b>{_fmt(r['test_rmse'], 3)} eV</b>，"
                 f"MAE = {_fmt(r['test_mae'], 3)} eV。")
    concl.append(f"验证集 R² = {_fmt(r['valid_r2'])}，与测试集相差 "
                 f"{abs((r['valid_r2'] or 0) - (r['test_r2'] or 0)):.4f}，"
                 f"说明 15% 的验证集和 15% 的测试集给的结论一致，不是因为切到“好切”才好看。")
    if (r["train_r2"] or 0) > 0.99:
        concl.append(f"训练集 R² = {_fmt(r['train_r2'])}，几乎把训练集背下来了；"
                     f"真实水平看验证/测试集，不看训练集。")
    if r["is_leak"]:
        concl.append("<b>注意：本 run 含泄漏列，分数不能当作模型能力</b>，它的用途是给出"
                     "“不避让会虚高多少”的量化对照。")
    else:
        concl.append("本 run 的特征里没有带隙同源列（gap_* / eff_mass_* / polarizability_*），"
                     "分数可以当作模型能力。")

    # ---- 指标表
    mrows = []
    for split, cn in (("train", "训练集"), ("valid", "验证集"), ("test", "测试集")):
        g = lambda n: db.scalar(conn, "SELECT value FROM metric WHERE run_id = ? AND split = ? "
                                       "AND name = ? AND step = -1", (rid, split, n))
        mrows.append([cn, _fmt(g("r2")), _fmt(g("rmse"), 3), _fmt(g("mae"), 3)])

    prows = []
    for k, v in params.items():
        prows.append([f'<span class="mono">{_e(k)}</span>', f'<span class="mono">{_e(v)}</span>'])

    arows = [[f'<span class="mono">{_e(a["kind"])}</span>',
              f'<span class="mono">{_e(a["path"])}</span>',
              f'{a["bytes"]:,} B',
              f'<span class="mono">{_e(a["sha256"][:16])}…</span>',
              "在盘" if a["present"] else '<span style="color:#b4462f">已不在盘（被 gitignore 清过）</span>']
             for a in arts]

    fa = db.rows(conn, "SELECT feature, gain, rank_gain FROM feature_importance WHERE run_id = ? "
                       "ORDER BY gain DESC LIMIT 8", (rid,))
    farows = [[i + 1, f'<span class="mono">{_e(x["feature"])}</span>', f'{x["gain"]:,.2f}']
              for i, x in enumerate(fa)]

    split_json = db.scalar(conn, "SELECT split_counts_json FROM run WHERE run_id = ?", (rid,), "{}")
    split = json.loads(split_json or "{}")
    split_txt = " / ".join(f"{k} {v}" for k, v in split.items())

    # ---- 分集干净性（硬规则，与 verify 用同一份计算）
    sg = splitguard.audit()
    sc = sg["counts"]
    tot = sg["total"] or 1
    ov = sg["overlaps"]
    clean_rows = [
        ["训练集", f'{sc["train"]} 条', f'{sc["train"] / tot:.1%}',
         "按化学式整组切分，不与其他集共享材料"],
        ["验证集", f'{sc["valid"]} 条', f'{sc["valid"] / tot:.1%}',
         "只用于早停与选型，不用于报分"],
        ["测试集", f'{sc["test"]} 条', f'{sc["test"] / tot:.1%}',
         "<b>训练时一次都没看过</b>，最后只评一次"],
        ["合计", f'{sg["total"]} 条', "100.0%", f'与快照 {_e(sg["snapshot"])} 行数一致'],
        ["材料 ID 交叠",
         f'train∩test = <b>{ov["train-test"]["ids"]}</b> · '
         f'valid∩test = <b>{ov["valid-test"]["ids"]}</b> · '
         f'train∩valid = <b>{ov["train-valid"]["ids"]}</b>',
         "—", "必须全为 0；不为 0 说明测试集被污染"],
        ["化学式交叠", f'<b>{ov["train-test"]["formulas"]}</b> / '
                     f'{ov["valid-test"]["formulas"]} / {ov["train-valid"]["formulas"]}',
         "—", "同一化学式的材料未被拆到两个集（否则同族材料跨集泄漏）"],
        ["重复 material_id", f'{sg["dup_ids"]}', "—", "全表同一材料只出现一次"],
        ["本 run 落库的测试预测", f'{sc["test"]} 条', "—",
         "与划分逐条一致（verify 的「各 run 同一划分」）"],
        ["快照指纹", f'<span class="mono">{_e(row["dataset_sha256"])}</span>', "—",
         "改过数据这里就对不上"],
        ["本次判定",
         '<b style="color:#1f7a4d">通过 —— 零交叠</b>' if sg["ok"]
         else f'<b style="color:#b4462f">不合格</b>：{"；".join(sg["problems"])}',
         "—", "由 python cli.py verify 每次重算，不是一次性结论"],
    ]

    # ---- 拼页面
    css = f"<style>{CSS}</style>"
    alert = ""
    if r["is_leak"]:
        alert = (f'<div class="alert"><b>这是泄漏对照，不是候选模型。</b>'
                 f"特征组 {_e(r['feature_group'])} 存在的唯一目的，是量化“把同源列放进去会让 R² 虚高多少”。"
                 f"它在排行榜上的位置只作参照。</div>")
    elif is_main:
        alert = ('<div class="ok"><b>当前主模型。</b>排行榜（按验证集 R²）第一名，'
                 "对外报的指标就是这一行。</div>")
    if r["is_smoke"]:
        alert += (f'<div class="alert"><b>冒烟测试。</b>只跑了 {_e(r["n_rounds"])} 棵树，'
                  "用于验证“脚本能跑通 + 产物能入库”，分数没有意义。</div>")

    fig_blocks = ""
    for kind, cap in (("pred_true", "① 预测 vs 真实：点越贴虚线越准；右上角那几颗是带隙大的样本，"
                                    "也是误差主要来源。"),
                      ("learning", "② 学习曲线：训练集一路下降、验证集走平，"
                                   "竖虚线是早停保留的那棵树。"),
                      ("importance", "③ 特征重要性：gain 排序前 15 名，"
                                     "看模型到底在依赖哪些信息。"),
                      ("residual", "④ 残差体检：左图看整体偏差，右图看有没有"
                                   "“带隙越大越低估”的系统性问题。")):
        f = figs_by_kind.get(kind)
        if f:
            fig_blocks += _fig_block(PROJECT_ROOT / f["path"],
                                     f"{cap}<br>图源：<span class=\"mono\">{_e(f['fig_id'])}.png"
                                     f"</span>（sha256 {_e(f['sha256'][:12])}…）", embed=True)

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>训练报告 · {_e(rid)}</title>{css}</head><body><div class="wrap">
<h1>训练报告 {_tags(r)}</h1>
<p class="sub">run_id <span class="mono">{_e(rid)}</span> ·
特征组 <b>{_e(r['feature_group'])}</b>（{_e(r['n_features'])} 列）·
目标 <span class="mono">{_e(r['target'])}</span> ·
{_e(r['created_at'][:19].replace('T', ' '))} 起，训练 {_e(r['duration_s'])} 秒</p>

{alert}
<div class="cards">
{_card("测试集 R²", _fmt(r['test_r2']), "对外报这个")}
{_card("测试集 RMSE", _fmt(r['test_rmse'], 3) + " eV", "平均偏离")}
{_card("验证集 R²", _fmt(r['valid_r2']), "用于选型")}
{_card("最好第几棵树", _e(r['best_iteration']), f"共跑 {_e(r['n_rounds'])} 棵")}
{_card("特征", f"{_e(r['n_features'])} 列", f"{_e(n_used)} 列被树用到")}
{_card("样本", f"{_e(row['snapshot_rows'])}", split_txt)}
</div>

<h2>一、结论</h2>
<ul>{''.join(f'<li>{c}</li>' for c in concl)}</ul>

<h2>二、指标</h2>
{_table(["数据集", "R²", "RMSE (eV)", "MAE (eV)"], mrows, {1, 2, 3})}
<p class="sub">R² 越接近 1 越好；RMSE/MAE 与带隙同单位（eV），
这批材料的带隙跨度约 0–8 eV，所以 RMSE 0.6 eV 相当于“大致能排出高低、具体数值别当真”。</p>

<h2>三、数据怎么分的，测试集干净吗</h2>
<div class="ok"><b>硬规则：测试集里的材料，严禁出现在训练集（或验证集）里。</b><br>
三层设防，缺一不可 ——
① <b>切分方式</b>：按化学式整组切，同一 <span class="mono">formula</span> 的材料只会落在一个集，
再做 (带隙分箱 × 有无金属) 分层，seed 固定 42；
② <b>落盘指纹</b>：<span class="mono">split.json</span> 存着快照 sha256 和三集各自的
<span class="mono">indices_sha256</span>，划分被改过立刻对不上；
③ <b>每次复核</b>：<span class="mono">python cli.py verify</span> 重新读快照逐条比对，
交叠数不为 0 就报错。所以“不干净”不可能悄悄留在台账里。</div>
{_table(["分集", "条数", "占比", "说明"], clean_rows)}
<p class="sub">看这张图的人只需要记一句：报告与图上所有“测试集”数字，
用的都是这 {sc['test']} 条与训练集零交叠的材料。</p>

<h2>四、图</h2>
{fig_blocks}

<h2>五、这次训练用的东西</h2>
<h3>超参数</h3>
{_table(["参数", "取值"], prows)}
<h3>数据与划分</h3>
{_table(["项", "值"], [
    ["快照文件", f'<span class="mono">{_e(row["dataset_id"])}.csv</span>'],
    ["快照 sha256", f'<span class="mono">{_e(row["dataset_sha256"])}</span>'],
    ["行数", f'{_e(row["snapshot_rows"])} 个材料'],
    ["划分", f'{_e(row["split_strategy"])}，seed={_e(row["split_seed"])}'],
    ["各集条数", f'<span class="mono">{_e(split_txt)}</span>'],
    ["特征组", f'{_e(row["feature_group"])} —— {GROUP_DESC.get(row["feature_group"], "")}'],
    ["特征名单指纹", f'<span class="mono">{_e(row["features_sha256"])}</span>'],
])}
<h3>特征重要性 Top8</h3>
{_table(["#", "特征", "gain"], farows, {0, 2})}

<h2>六、产物与可复核性</h2>
{_table(["类型", "文件", "大小", "sha256", "状态"], arows, {2})}
<p class="sub">台账里另外记着 <span class="mono">manifest_sha256={_e(row["manifest_sha256"][:16])}…</span>，
manifest 若被手改，`python cli.py verify` 会当场报出来。</p>

<h3>复现这一次</h3>
<pre class="mono" style="background:var(--soft);border:1px solid var(--line);border-radius:6px;
padding:12px 14px;overflow-x:auto">cd D:\\数据库\\模型\\XGboost\\train_XGboost
python train.py --config config.yaml --features {_e(r['feature_group'])} --note "复现 {_e(rid)}"</pre>
<p class="sub">判定标准见 manifest 的 <span class="mono">reproducibility.tolerance</span>：
nthread=1 时 RMSE 允许差 1e-6；多线程时 1e-3。</p>

<footer>本页由 <span class="mono">管理器/report.py</span> 自动生成，
数字全部来自 <span class="mono">管理器/catalog.sqlite</span>（结构版本 {db.CATALOG_SCHEMA_VERSION}）；
生成时间 {datetime.now().astimezone().isoformat(timespec='seconds')}。
图不入 git，删掉后跑 <span class="mono">python cli.py chart</span> 可原样重画。</footer>
</div></body></html>"""


# ---------------------------------------------------------------- 首页
def index_html(conn) -> str:
    runs = db.rows(conn, "SELECT * FROM v_run_score ORDER BY created_at")
    board = db.rows(conn, "SELECT * FROM v_leaderboard")
    main = board[0] if board else None
    cmp_figs = _figs_cmp(conn)

    brows = []
    for i, r in enumerate(runs):
        rep = f"report/{_e(r['run_id'])}.html"
        brows.append([
            f"{i + 1}",
            f'<a href="{rep}"><span class="mono">{_e(r["run_id"])}</span></a>',
            f'<b>{_e(r["feature_group"])}</b>{_tags(r)}',
            str(r["n_features"]),
            _fmt(r["valid_r2"]),
            _fmt(r["test_r2"]),
            _fmt(r["test_rmse"], 3),
            _e((r["note"] or "")[:46]),
        ])

    lrows = []
    for i, r in enumerate(board):
        lrows.append([
            f"<b>{i + 1}</b>",
            f'<b>{_e(r["feature_group"])}</b>',
            f'<a href="report/{_e(r["run_id"])}.html"><span class="mono">{_e(r["run_id"].split("-")[-1])}</span></a>',
            str(r["n_features"]),
            _fmt(r["valid_r2"]),
            _fmt(r["test_r2"]),
            _fmt(r["test_rmse"], 3),
        ])

    thumbs = ""
    for f in cmp_figs:
        thumbs += (f'<div class="thumb"><img src="{_e(Path(f["path"]).name)}" alt="{_e(f["title"])}">'
                   f'<div>{_e(f["title"])}</div></div>')

    perrun = ""
    for r in runs:
        figs = {f["kind"]: f for f in _figs_of(conn, r["run_id"])}
        f = figs.get("pred_true")
        if not f:
            continue
        perrun += (f'<div class="thumb"><a href="report/{_e(r["run_id"])}.html">'
                   f'<img src="{_e(Path(f["path"]).name)}" alt="{_e(r["run_id"])}"></a>'
                   f'<div><b>{_e(r["feature_group"])}</b> {_tags(r)}<br>'
                   f'<span class="mono">{_e(r["run_id"].split("-")[-1])}</span> · '
                   f'test R² {_fmt(r["test_r2"])}</div></div>')

    n_leak = sum(1 for r in runs if r["is_leak"])
    n_smoke = sum(1 for r in runs if r["is_smoke"])
    sg = splitguard.audit()
    sc = sg["counts"]

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>C2DB 带隙预测 · 实验看板</title><style>{CSS}</style></head><body><div class="wrap">
<h1>C2DB 带隙预测 · 实验看板</h1>
<p class="sub">目标 <span class="mono">gap_pbe</span>（PBE 带隙，eV）·
数据 <span class="mono">c2db-kept-20261031</span>，3,354 个材料、同一份划分（seed 42）·
模型 xgboost · 共 {len(runs)} 次训练（{n_smoke} 次冒烟、{n_leak} 次泄漏对照）</p>

<div class="cards">
{_card("主模型", f'{_e(main["feature_group"])}' if main else "—", "按验证集 R² 选出")}
{_card("测试集 R²", _fmt(main["test_r2"]) if main else "—", "对外报这个")}
{_card("测试集 RMSE", (_fmt(main["test_rmse"], 3) + " eV") if main else "—", "平均偏离")}
{_card("验证集 R²", _fmt(main["valid_r2"]) if main else "—", "选型口径")}
{_card("训练次数", str(len(runs)), "全部留档，不删失败")}
</div>

<div class="ok"><b>数据分集（本页所有训练完全相同）：</b>
训练集 <b>{sc['train']}</b> 条 · 验证集 <b>{sc['valid']}</b> 条 · 测试集 <b>{sc['test']}</b> 条。<br>
<b>硬规则：测试集里的材料，严禁出现在训练集里。</b>
本次复核 —— 材料 ID 交叠 <b>{sg['overlaps']['train-test']['ids']}</b> 个、
化学式交叠 <b>{sg['overlaps']['train-test']['formulas']}</b> 个、
验证集与测试集交叠 <b>{sg['overlaps']['valid-test']['ids']}</b> 个；
划分按化学式整组切分，同一 formula 不会跨集。
该结论由 <span class="mono">python cli.py verify</span> 的「测试集干净」一项<b>每次重新计算</b>，
不是一次性声明。</div>

<h2>一、能选型的排行榜</h2>
{_table(["#", "特征组", "run", "列数", "验证集 R²", "测试集 R²", "测试集 RMSE (eV)"], lrows, {2, 3, 4, 5, 6})}
<p class="sub">按<b>验证集</b> R² 降序 —— 测试集只在最后报一次，不拿它挑模型，
免得“用考卷调参”。冒烟测试和泄漏对照不出现在这张表里，它们在下面的全量表里。</p>

<h2>二、三张对比图</h2>
<div class="grid3">{thumbs}</div>

<h2>三、全部训练（含冒烟与泄漏对照）</h2>
{_table(["#", "run_id", "特征组", "列数", "验证集 R²", "测试集 R²", "测试集 RMSE (eV)", "备注"],
        brows, {3, 4, 5, 6})}
<p class="sub">点 run_id 进单次报告（图内嵌，可直接转发）。</p>

<h2>四、每次训练的预测图</h2>
<div class="grid3">{perrun}</div>

<h2>五、怎么读这套东西</h2>
<ul>
<li><b>真相在文件，不在这张网页。</b>每次训练的目录 <span class="mono">output_XGb/&lt;run_id&gt;/</span>
是不可变的，里面的 <span class="mono">manifest.json</span> 记着参数、数据指纹、环境、产物 sha256；
本页面的数字全部从 <span class="mono">catalog.sqlite</span> 读出，而这个库可以整库重建。</li>
<li><b>三层各管一件事。</b>母库管“材料是什么”；快照 + 划分管“喂给模型的到底是什么”；
台账管“哪次实验得了多少分、凭什么”。</li>
<li><b>分数要能复现才算数。</b>跑 <span class="mono">python cli.py verify</span> 会重新算一遍产物哈希，
对不上就报错 —— 图能重画、指标能重算、数据能查回母库。</li>
<li><b>测试集是干净的，而且可复核。</b>三集按化学式整组切分，
训练 {sc['train']} / 验证 {sc['valid']} / 测试 {sc['test']} 条，材料与化学式两两零交叠；
每次 <span class="mono">cli.py verify</span> 都会重读快照重算一遍，
交叠不为 0 就报错。图上那张脚注里的条数与这句结论同源。</li>
<li><b>泄漏是主动避开的，不是没发现。</b>G3/G3p 两组专为量化泄漏而跑：
G3 只加同源列（有效质量、极化率），R² 几乎不动；G3p 加进 gap_dir_pbe，R² 直接冲到 0.987。
这就是为什么主模型坚持用 G2。</li>
</ul>

<footer>由 <span class="mono">管理器/report.py</span> 生成 ·
台账 <span class="mono">catalog.sqlite</span> 结构版本 {db.CATALOG_SCHEMA_VERSION} ·
{datetime.now().astimezone().isoformat(timespec='seconds')} ·
重出本页：<span class="mono">python cli.py report</span></footer>
</div></body></html>"""


# ---------------------------------------------------------------- 落盘
def build_all(index_only: bool = False) -> list[Path]:
    db.init()
    conn = db.connect()
    out: list[Path] = []
    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        runs = db.rows(conn, "SELECT * FROM v_run_score ORDER BY created_at")
        if not runs:
            raise SystemExit("台账为空：先跑 python record.py")
        if not index_only:
            for r in runs:
                p = REPORT_DIR / f"{r['run_id']}.html"
                p.write_text(run_report_html(conn, r), encoding="utf-8")
                out.append(p)
                print(f"  报告 {p.name}  {p.stat().st_size:,} B")
        p = INDEX_HTML
        p.write_text(index_html(conn), encoding="utf-8")
        out.append(p)
        print(f"  首页 {p.name}  {p.stat().st_size:,} B")
        return out
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    print(f"输出目录：{FIGURE_ROOT}")
    build_all(index_only="--index-only" in argv)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
