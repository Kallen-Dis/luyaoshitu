"""一键验收：和 CI 跑同样的检查，本机一条命令跑完。不联网、不花配额。

依次执行：
1. 密钥扫描（scripts/check_secrets.py）
2. 后端 lint（ruff check）、格式检查（ruff format --check）
   与测试（pytest，全部用假客户端，不调百度）
3. 前端 lint、算法测试、类型检查与构建（需要已在 frontend/ 下 npm install）

用法：
    python scripts/verify.py                  # 全部
    python scripts/verify.py --skip-frontend  # 只查后端（没装 Node 时）

任一步失败，最后的汇总里标出来，退出码为 1。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"


def run(
    label: str, cmd: list[str], cwd: Path, env: dict[str, str] | None = None
) -> tuple[str, bool, float]:
    print(f"\n==> {label}\n    {' '.join(cmd)}", flush=True)
    start = time.perf_counter()
    try:
        ok = subprocess.run(cmd, cwd=cwd, env=env).returncode == 0
    except OSError as exc:
        print(f"    启动失败：{exc}")
        ok = False
    return label, ok, time.perf_counter() - start


def main() -> int:
    parser = argparse.ArgumentParser(description="一键验收（与 CI 相同的检查）")
    parser.add_argument("--skip-frontend", action="store_true", help="跳过前端检查")
    args = parser.parse_args()

    py = sys.executable
    backend_env = {**os.environ, "PYTHONPATH": "."}
    results = [
        run("密钥扫描", [py, "scripts/check_secrets.py"], ROOT),
        run(
            "后端 lint ruff",
            [py, "-m", "ruff", "check", "app", "tests", "../scripts"],
            BACKEND,
        ),
        # 只检查不改写：不合格时运行 `ruff format app tests ../scripts`（在 backend/ 下）
        run(
            "后端格式 ruff format",
            [py, "-m", "ruff", "format", "--check", "app", "tests", "../scripts"],
            BACKEND,
        ),
        run("后端测试 pytest", [py, "-m", "pytest", "tests", "-q"], BACKEND, backend_env),
    ]

    if not args.skip_frontend:
        npm = shutil.which("npm")
        npx = shutil.which("npx")
        if not npm or not npx:
            print(
                "\n==> 前端：没找到 npm / npx"
                "（装好 Node.js 22.18+（22.x）或 24+ 后再跑，或加 --skip-frontend）"
            )
            results.append(("前端（未安装 Node.js）", False, 0.0))
        elif not (FRONTEND / "node_modules").exists():
            print("\n==> 前端：还没装依赖，请先在 frontend/ 下执行 npm ci")
            results.append(("前端（缺 node_modules）", False, 0.0))
        else:
            results += [
                run("前端 lint", [npm, "run", "lint"], FRONTEND),
                run("前端测试", [npm, "test"], FRONTEND),
                run("前端类型检查", [npx, "tsc", "--noEmit", "-p", "tsconfig.app.json"], FRONTEND),
                run("前端构建", [npm, "run", "build"], FRONTEND),
            ]

    print("\n汇总")
    for label, ok, secs in results:
        print(f"  {'通过' if ok else '失败'}  {label:<18} {secs:5.1f} 秒")
    failed = [label for label, ok, _ in results if not ok]
    if failed:
        print(f"\n{len(failed)} 项没通过：{'、'.join(failed)}")
        return 1
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
