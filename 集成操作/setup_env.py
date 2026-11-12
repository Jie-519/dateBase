# -*- coding: utf-8 -*-
"""setup_env.py：把训练依赖补齐到 模型/XGboost/libs/，让 clone 下来就能跑。

为什么要这一步
--------------
xgboost 的 Windows wheel 里有 201 MB 的 xgboost.dll，超过 GitHub 单文件 100 MB 的
硬限制，因此 libs/ 永远不可能进仓库（.gitignore 已忽略，只留 .gitkeep）。
所以依赖只能"事后补齐"，本脚本把这件事做成一条命令。

它按这个顺序尝试，任一步成功就停
--------------------------------
1. libs/xgboost/ 已存在            -> 什么都不做
2. _tmp/ 里已有同名 wheel          -> 校验 sha256 后解包到 libs/
3. 从 GitHub Release 下载 wheel    -> 校验 sha256 后解包到 libs/
4. 联网 pip 安装 xgboost==3.0.2    -> 装到 site-packages（libs 仍为空）
5. 全失败                          -> 打印手工处理办法

为什么优先"解包到 libs/"而不是 pip 安装：train.py 会把 libs/ 插进 sys.path，
这样一台机器上可以有多个版本共存，也不污染全局环境；只有下载不到时才退回 pip。

用法
----
    双击 集成操作\\恢复环境.bat
    或 python setup_env.py [--offline]
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIBS = PROJECT_ROOT / "模型" / "XGboost" / "libs"
TMP = PROJECT_ROOT / "模型" / "XGboost" / "_tmp"

WHEEL_NAME = "xgboost-3.0.2-py3-none-win_amd64.whl"
WHEEL_URL = ("https://github.com/Jie-519/dateBase/releases/download/XGb/"
             + WHEEL_NAME)
WHEEL_SHA = "b4c89b71d134da9fa6318e3c9f5459317d1013b4d57059d10ed2840750e2f7e1"
VERSION = "3.0.2"

LINE = "-" * 58


def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(n: str, msg: str) -> None:
    say(f"\n{n} {msg}")


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def libs_ready() -> str | None:
    """libs/ 里能 import 到 xgboost 就返回版本号。"""
    if not (LIBS / "xgboost" / "__init__.py").exists():
        return None
    code = ("import sys; sys.path.insert(0, r'%s'); "
            "import xgboost; print(xgboost.__version__)" % LIBS)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def global_ready() -> str | None:
    r = subprocess.run([sys.executable, "-c", "import xgboost; print(xgboost.__version__)"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def unpack(whl: Path) -> None:
    LIBS.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(whl) as z:
        z.extractall(LIBS)
    say(f"   已解包到 {LIBS}")


def download(dst: Path) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_suffix(dst.suffix + ".part")
    say(f"   下载 {WHEEL_URL}")
    tty = sys.stdout.isatty()
    try:
        with urllib.request.urlopen(WHEEL_URL, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            mark = 0
            with part.open("wb") as fh:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
                    if not total:
                        continue
                    pct = got * 100 // total
                    if tty:                       # 控制台里原地刷新
                        print(f"\r   {got/1048576:6.1f} / {total/1048576:.1f} MB "
                              f"({pct}%)", end="", flush=True)
                    elif pct >= mark + 25:        # 重定向到文件时每 25% 一行
                        say(f"   {pct}%  ({got/1048576:.1f} / {total/1048576:.1f} MB)")
                        mark = pct
        if tty:
            say()
    except Exception as e:                                    # noqa: BLE001
        say(f"\n   下载失败：{type(e).__name__}: {e}")
        part.unlink(missing_ok=True)
        return False
    part.replace(dst)
    return True


def try_pip() -> bool:
    say("   执行 python -m pip install xgboost==3.0.2（需要联网）")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                        f"xgboost=={VERSION}"], capture_output=True, text=True)
    if r.returncode != 0:
        say("   pip 安装失败：" + (r.stderr or r.stdout or "")[-400:])
        return False
    return True


def main(argv: list[str]) -> int:
    offline = "--offline" in argv
    say(LINE)
    say(" C2DB 训练环境恢复")
    say(LINE)
    say(f" 项目目录 {PROJECT_ROOT}")

    step("①", "检查 libs/")
    v = libs_ready()
    if v:
        say(f"   已就绪：libs/ 里是 xgboost {v}")
        say("\n完成，可以直接运行 开始训练.bat。")
        return 0
    say("   libs/ 里没有可用的 xgboost")

    step("②", "检查全局安装")
    v = global_ready()
    if v:
        say(f"   已就绪：全局 xgboost {v}（libs/ 留空也能跑）")
        say("\n完成，可以直接运行 开始训练.bat。")
        return 0
    say("   全局也没有")

    step("③", "找本地 wheel")
    whl = TMP / WHEEL_NAME
    if not whl.exists():
        others = sorted(TMP.glob("xgboost-*.whl")) if TMP.exists() else []
        whl = others[0] if others else whl
    if whl.exists():
        say(f"   找到 {whl.name}（{whl.stat().st_size/1048576:.1f} MB）")
    else:
        say(f"   未找到，需要下载（{WHEEL_NAME}）")
        if offline:
            say("   --offline 模式下不下载，转下一步")
        elif download(whl):
            say("   下载完成")
        else:
            say("   改用 pip 安装")
            if try_pip():
                say("\n完成（python -m pip install），可以直接运行 开始训练.bat。")
                return 0
            _manual()
            return 1

    step("④", "校验 sha256")
    got = sha256_of(whl)
    if got != WHEEL_SHA:
        say(f"   校验不通过！\n     期望 {WHEEL_SHA}\n     实际 {got}")
        say("   文件可能下载不完整或被替换，已中止。删掉它重跑本脚本即可。")
        return 1
    say("   通过")

    step("⑤", "解包到 libs/")
    unpack(whl)

    step("⑥", "验证")
    v = libs_ready()
    if not v:
        say("   解包后仍无法 import，请把上面的输出发给维护者。")
        _manual()
        return 1
    say(f"   libs/ 可用，xgboost {v}")
    say("\n完成，可以直接运行 开始训练.bat。")
    return 0


def _manual() -> None:
    say("\n" + LINE)
    say(" 手工处理办法（任选其一）")
    say(LINE)
    say(f" 1) 联网：python -m pip install xgboost=={VERSION}")
    say(" 2) 离线：从下面地址下载 wheel，放到")
    say(f"      {TMP}")
    say("    再重跑本脚本（会自动校验并解包）")
    say(f"    {WHEEL_URL}")
    say(" 3) 从另一台已装好的机器复制整个目录：")
    say(f"      {LIBS}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main(sys.argv[1:]))
