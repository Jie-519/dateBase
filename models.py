# -*- coding: utf-8 -*-
"""领域模型（对应 C++ 里的类）。

    material      -> class Material      （材料主库：ID / 化学式 / 名称 / 组成 / 类别）
    structure     -> class Structure     （结构信息库：晶格参数 / 空间群 / 晶相 / CIF）
    electrical    -> class Electrical    （电学性能子库：带隙 / 迁移率 / 有效质量 / 介电常数）
    stability     -> class Stability     （辅助：稳定性与能量，完整度评分的依据）

三个子库通过 material_id 关联；MaterialRecord 把它们组合成一个"完整对象"，
外部只跟这一个对象打交道 —— 这就是 C++ 里的组合（composition）而非继承。

约定：缺失一律是 None（Python）/ NULL（SQLite），绝不用字符串"缺失"。
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

# ---------------------------------------------------------------- 常量
SUBLIBRARIES = {
    "material": "材料主库",
    "structure": "结构信息库",
    "electrical": "电学性能子库",
    "stability": "辅助库：稳定性与能量",
}

#: 缺失原因码（写进 data_gap.reason）
REASONS = {
    "not_computed": "未计算",
    "no_field": "源表无此字段",
    "not_applicable": "不适用",
    "unknown": "未知（源表为 Unknown）",
    "fetch_failed": "获取失败",
}

#: 允许出现在库里的"缺失"字符串（校验用：库内文本字段不应命中）
FORBIDDEN_TOKENS = ("缺失", "未计算", "源表无此字段", "Unknown", "NULL", "None")

#: 官网对所有数值统一显示 3 位小数（本库按该精度存）
DISPLAY_PRECISION = 3

SYMBOLS = (
    "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn "
    "Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce "
    "Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn "
    "Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl "
    "Mc Lv Ts Og"
).split()
Z_OF = {s: i + 1 for i, s in enumerate(SYMBOLS)}
SYMBOL_OF = {z: s for s, z in Z_OF.items()}

#: 空间群号区间 -> 晶系（派生值，仅作辅助分类，不等于源表缺失的"晶相"）
CRYSTAL_SYSTEMS = (
    (1, 2, "三斜"), (3, 15, "单斜"), (16, 74, "正交"), (75, 142, "四方"),
    (143, 167, "三方"), (168, 194, "六方"), (195, 230, "立方"),
)


# ---------------------------------------------------------------- 解析/派生
def parse_formula(formula: str | None) -> dict[str, float]:
    """'Cs2AgBiBr6' -> {'Cs': 2.0, 'Ag': 1.0, 'Bi': 1.0, 'Br': 6.0}"""
    out: dict[str, float] = {}
    for sym, num in re.findall(r"([A-Z][a-z]?)(\d*\.?\d*)", formula or ""):
        if sym not in Z_OF:
            continue
        n = float(num) if num else 1.0
        out[sym] = out.get(sym, 0.0) + n
    return out


def crystal_system_of(space_group: int | None) -> str | None:
    if space_group is None:
        return None
    for lo, hi, name in CRYSTAL_SYSTEMS:
        if lo <= int(space_group) <= hi:
            return name
    return None


def cell_volume(a, b, c, alpha, beta, gamma) -> float | None:
    """由晶格常数算晶胞体积（派生值，字典里标 origin=derived）。"""
    if None in (a, b, c, alpha, beta, gamma):
        return None
    ca, cb, cg = (math.cos(math.radians(x)) for x in (alpha, beta, gamma))
    t = 1 - ca * ca - cb * cb - cg * cg + 2 * ca * cb * cg
    return round(a * b * c * math.sqrt(t), 3) if t > 0 else None


def compose_text(counts: dict[str, float], reduced: str | None = None) -> str:
    """组成的人读文本，如 'Tl×1、Cl×1（2 种元素）；约化式 TlCl'"""
    if not counts:
        return ""
    parts = "、".join(f"{s}×{int(n) if n == int(n) else n}"
                     for s, n in sorted(counts.items(), key=lambda kv: Z_OF.get(kv[0], 999)))
    text = f"{parts}（{len(counts)} 种元素）"
    if reduced:
        text += f"；约化式 {reduced}"
    return text


# ---------------------------------------------------------------- 实体
@dataclass(frozen=True, slots=True)
class Material:
    """材料主库：ID / 化学式 / 名称 / 组成 / 类别"""
    material_id: str
    formula: str
    reduced_formula: str | None = None
    name: str = ""
    name_source: str = "formula_temp"
    composition: str | None = None
    composition_counts: dict[str, float] = field(default_factory=dict)
    n_elements: int | None = None
    category: str | None = None
    has_metal: bool | None = None
    category_note: str | None = None
    score: float | None = None
    verdict: str | None = None
    verdict_reason: str | None = None
    source_url: str | None = None

    @property
    def is_metal_compound(self) -> bool | None:
        return None if self.has_metal is None else bool(self.has_metal)


@dataclass(frozen=True, slots=True)
class Structure:
    """结构信息库：晶格参数 / 空间群 / 晶相 / CIF 结构"""
    material_id: str
    a: float | None = None
    b: float | None = None
    c: float | None = None
    alpha: float | None = None
    beta: float | None = None
    gamma: float | None = None
    space_group: int | None = None
    crystal_system: str | None = None          # 派生
    crystal_phase: str | None = None           # 源表无此字段，恒为 None
    cif_url: str | None = None
    cif_sha256: str | None = None
    cif_bytes: int | None = None
    cif_cache: str | None = None
    cell_volume: float | None = None           # 派生

    @property
    def has_cell(self) -> bool:
        return None not in (self.a, self.b, self.c, self.alpha, self.beta, self.gamma)


@dataclass(frozen=True, slots=True)
class Electrical:
    """电学性能子库：带隙 / 迁移率 / 有效质量 / 介电常数"""
    material_id: str
    gap_pbe: float | None = None
    gap_dir_pbe: float | None = None
    gap_hse06: float | None = None
    gap_gw: float | None = None
    gap_kind_pbe: str | None = None
    mobility_e: float | None = None
    mobility_h: float | None = None
    eff_mass_cbm: float | None = None      # 电子有效质量（CBM DOS, PBE, m0）
    eff_mass_vbm: float | None = None      # 空穴有效质量（VBM DOS, PBE, m0）
    dielectric_static: float | None = None
    polarizability_el: float | None = None
    polarizability_lat: float | None = None
    polarizability_total: float | None = None

    def gap(self, method: str) -> float | None:
        """按方法取带隙：'pbe' / 'dir' / 'hse06' / 'gw'（未计算返回 None，不是 0）。"""
        return {
            "pbe": self.gap_pbe,
            "dir": self.gap_dir_pbe,
            "hse06": self.gap_hse06,
            "gw": self.gap_gw,
        }.get(method.lower())

    @property
    def is_direct_pbe(self) -> bool | None:
        return None if self.gap_kind_pbe is None else self.gap_kind_pbe == "直接"

    @property
    def has_hse06(self) -> bool:
        return self.gap_hse06 is not None


@dataclass(frozen=True, slots=True)
class Stability:
    """辅助库：稳定性与能量（参与完整度评分，不属于三个必需子库）"""
    material_id: str
    dyn_stab: str | None = None
    ehull: float | None = None
    hform: float | None = None
    energy: float | None = None

    @property
    def is_dynamically_stable(self) -> bool | None:
        if self.dyn_stab is None or self.dyn_stab == "Unknown":
            return None
        return self.dyn_stab == "Yes"


@dataclass(frozen=True, slots=True)
class MaterialRecord:
    """完整对象：三个子库 + 辅助库，通过 material_id 组合。

    用法：
        rec = repo.get("2As-1")
        rec.material.formula, rec.structure.a, rec.electrical.gap_hse06
        rec.to_dict(), rec.summary()
    """
    material: Material
    structure: Structure
    electrical: Electrical
    stability: Stability

    #: 组合关系（C++ 里 `MaterialRecord` 持有三个子对象引用）
    @property
    def id(self) -> str:
        return self.material.material_id

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for part in ("material", "structure", "electrical", "stability"):
            d.update(asdict(getattr(self, part)))
        return d

    def summary(self) -> str:
        m, s, e, st = self.material, self.structure, self.electrical, self.stability
        def fmt(v, unit=""):
            if v is None:
                return "缺失"
            if isinstance(v, str):
                return v
            if isinstance(v, float):
                if v == float("inf"):
                    return f"∞{unit}"
                return f"{v:g}{unit}"
            return f"{v}{unit}"
        lines = [
            f"ID        : {m.material_id}",
            f"化学式    : {m.formula}（约化式 {m.reduced_formula}）",
            f"名称      : {m.name}  [来源 {m.name_source}]",
            f"组成      : {m.composition}",
            f"类别      : {m.category}（含金属={m.has_metal}）[规则推断]",
            f"空间群    : {fmt(s.space_group)}  晶系(派生) {fmt(s.crystal_system)}  "
            f"晶相 {fmt(s.crystal_phase)}",
            f"晶格参数  : a={fmt(s.a,' Å')} b={fmt(s.b,' Å')} c={fmt(s.c,' Å')} "
            f"α={fmt(s.alpha,'°')} β={fmt(s.beta,'°')} γ={fmt(s.gamma,'°')}",
            f"CIF       : {fmt(s.cif_url)}"
            + (f"  [{s.cif_bytes} 字节，原文已内嵌库内]" if s.cif_bytes else "  [库内无原文]"),
            f"带隙      : PBE={fmt(e.gap_pbe,' eV')} dir={fmt(e.gap_dir_pbe,' eV')} "
            f"HSE06={fmt(e.gap_hse06,' eV')} G0W0={fmt(e.gap_gw,' eV')}（{fmt(e.gap_kind_pbe)}）",
            f"迁移率    : 电子={fmt(e.mobility_e)} 空穴={fmt(e.mobility_h)}"
            "  [C2DB 无此字段]",
            f"有效质量  : CBM={fmt(e.eff_mass_cbm,' m0')} VBM={fmt(e.eff_mass_vbm,' m0')}"
            "  [DOS 有效质量，PBE]",
            f"介电常数  : {fmt(e.dielectric_static)}  [C2DB 无 εr；仅有二维极化率 α(Å)]",
            f"稳定性    : dyn_stab={fmt(st.dyn_stab)} ehull={fmt(st.ehull,' eV/atom')}",
            f"完整度    : {fmt(m.score)} 分 / 结论 {m.verdict}",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------- 行 -> 对象
def material_from_row(row: Any) -> Material:
    counts = {}
    if row["composition_json"]:
        try:
            counts = {k: float(v) for k, v in json.loads(row["composition_json"]).items()}
        except (ValueError, TypeError):
            counts = {}
    return Material(
        material_id=row["material_id"], formula=row["formula"],
        reduced_formula=row["reduced_formula"], name=row["name"],
        name_source=row["name_source"], composition=row["composition"],
        composition_counts=counts, n_elements=row["n_elements"],
        category=row["category"],
        has_metal=None if row["has_metal"] is None else bool(row["has_metal"]),
        category_note=row["category_note"], score=row["score"],
        verdict=row["verdict"], verdict_reason=row["verdict_reason"],
        source_url=row["source_url"],
    )


def structure_from_row(row: Any) -> Structure:
    return Structure(
        material_id=row["material_id"], a=row["a"], b=row["b"], c=row["c"],
        alpha=row["alpha"], beta=row["beta"], gamma=row["gamma"],
        space_group=row["space_group"], crystal_system=row["crystal_system"],
        crystal_phase=row["crystal_phase"], cif_url=row["cif_url"],
        cif_sha256=row["cif_sha256"], cif_bytes=row["cif_bytes"],
        cif_cache=row["cif_cache"], cell_volume=row["cell_volume"],
    )


def electrical_from_row(row: Any) -> Electrical:
    return Electrical(
        material_id=row["material_id"], gap_pbe=row["gap_pbe"],
        gap_dir_pbe=row["gap_dir_pbe"], gap_hse06=row["gap_hse06"],
        gap_gw=row["gap_gw"], gap_kind_pbe=row["gap_kind_pbe"],
        mobility_e=row["mobility_e"], mobility_h=row["mobility_h"],
        eff_mass_cbm=row["eff_mass_cbm"], eff_mass_vbm=row["eff_mass_vbm"],
        dielectric_static=row["dielectric_static"],
        polarizability_el=row["polarizability_el"],
        polarizability_lat=row["polarizability_lat"],
        polarizability_total=row["polarizability_total"],
    )


def stability_from_row(row: Any) -> Stability:
    return Stability(
        material_id=row["material_id"], dyn_stab=row["dyn_stab"],
        ehull=row["ehull"], hform=row["hform"], energy=row["energy"],
    )


def record_from_join_row(row: Any) -> MaterialRecord:
    """由 v_material_full（三子库 JOIN）的一行构造完整对象。"""
    counts: dict[str, float] = {}
    raw = row["composition_json"] if "composition_json" in row.keys() else None
    if raw:
        try:
            counts = {k: float(v) for k, v in json.loads(raw).items()}
        except (ValueError, TypeError):
            counts = {}
    return MaterialRecord(
        material=Material(
            material_id=row["id"], formula=row["formula"],
            reduced_formula=row["reduced_formula"], name=row["name"],
            name_source=row["name_source"], composition=row["composition"],
            composition_counts=counts, n_elements=row["n_elements"],
            category=row["category"],
            has_metal=None if row["has_metal"] is None else bool(row["has_metal"]),
            category_note=row["category_note"], score=row["score"],
            verdict=row["verdict"], verdict_reason=row["verdict_reason"],
            source_url=row["source_url"],
        ),
        structure=Structure(
            material_id=row["id"], a=row["a"], b=row["b"], c=row["c"],
            alpha=row["alpha"], beta=row["beta"], gamma=row["gamma"],
            space_group=row["space_group"], crystal_system=row["crystal_system"],
            crystal_phase=row["crystal_phase"], cif_url=row["cif_url"],
            cif_sha256=row["cif_sha256"], cif_bytes=row["cif_bytes"],
            cif_cache=row["cif_cache"], cell_volume=row["cell_volume"],
        ),
        electrical=Electrical(
            material_id=row["id"], gap_pbe=row["gap_pbe"],
            gap_dir_pbe=row["gap_dir_pbe"], gap_hse06=row["gap_hse06"],
            gap_gw=row["gap_gw"], gap_kind_pbe=row["gap_kind_pbe"],
            mobility_e=row["mobility_e"], mobility_h=row["mobility_h"],
            eff_mass_cbm=row["eff_mass_cbm"], eff_mass_vbm=row["eff_mass_vbm"],
            dielectric_static=row["dielectric_static"],
            polarizability_el=row["polarizability_el"],
            polarizability_lat=row["polarizability_lat"],
            polarizability_total=row["polarizability_total"],
        ),
        stability=Stability(
            material_id=row["id"], dyn_stab=row["dyn_stab"],
            ehull=row["ehull"], hform=row["hform"], energy=row["energy"],
        ),
    )


def records_from_join_rows(rows: Iterable[Any]) -> list[MaterialRecord]:
    return [record_from_join_row(r) for r in rows]
