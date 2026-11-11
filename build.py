# -*- coding: utf-8 -*-
"""一键建库：重建 SQLite -> 体检 -> 打印结论（可选再导出 Excel）。

    python 数据库/build.py                 # 用"保留数据"3,354 条重建
    python 数据库/build.py --scope all     # 全量 17,001 条
    python 数据库/build.py --export        # 顺带导出 Excel
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import etl  # noqa: E402
import verify  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="重建 C2DB SQLite 数据库并体检")
    ap.add_argument("--scope", choices=["kept", "all"], default="kept",
                    help="kept=只导保留数据（默认）；all=保留+候选+剔除")
    ap.add_argument("--no-verify", action="store_true", help="跳过体检")
    ap.add_argument("--export", action="store_true", help="建库后导出 Excel")
    args = ap.parse_args(argv)

    t0 = time.time()
    print(f"[1/3] 导入（scope={args.scope}）...")
    summary = etl.build(args.scope, verbose=False)
    print(f"      材料 {summary['material']} 条 / 结构 {summary['structure']} / "
          f"电学 {summary['electrical']} / 组成关系 {summary['material_element']} / "
          f"缺失登记 {summary['data_gap']} / CIF 哈希 {summary['cif_hashed']}")

    if not args.no_verify:
        print("[2/3] 体检 ...")
        res = verify.run()
        for c in res["checks"]:
            print(f"      [{'OK' if c.ok else 'FAIL'}] {c.name}")
        if not res["ok"]:
            print("体检未通过，见", res["report"])
            return 1
    else:
        print("[2/3] 跳过体检")

    if args.export:
        print("[3/3] 导出 Excel ...")
        import export
        out = export.run()
        print("      ", out)
    else:
        print("[3/3] 未导出 Excel（加 --export 可导出）")

    print(f"完成，用时 {time.time() - t0:.1f}s；库文件 {HERE / 'C2DB_materials.sqlite'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
