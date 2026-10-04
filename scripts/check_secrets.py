"""提交前检查：仓库里有没有写死的 AK、Token。

扫的是「会进仓库的文件」：git 已跟踪的，加上没被 .gitignore 排除的新文件。
两道检查：

1. **本机 .env 里的真实值**：.env 里所有 AK / KEY / TOKEN / SECRET 的值，逐个在这些文件里找原文。
   这是最准的一道——只要出现就一定是泄漏，不会误报。CI 上没有 .env，这一道自动跳过。
2. **常见写法**：`XXX_AK=一长串`、URL 里的 `ak=一长串`、`sk-` 开头的 Token。
   占位符（your_xxx、空值、<...>、${...}）不算。

输出只给文件、行号和打了码的前几位，不打印原值。发现问题时退出码为 1，CI 会失败。

用法：
    python scripts/check_secrets.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 示例配置本来就该只有占位符；它也被扫，但只按「常见写法」查
SKIP_SUFFIX = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".woff",
    ".woff2",
    ".ttf",
    ".db",
    ".sqlite",
}
SKIP_NAMES = {"package-lock.json"}  # 全是依赖哈希，没有配置项
MAX_BYTES = 2_000_000

SECRET_KEY = re.compile(r"(AK|KEY|TOKEN|SECRET|PASSWORD)$", re.IGNORECASE)
PATTERNS = [
    (
        "疑似写死的密钥赋值",
        re.compile(
            r"\b([A-Z][A-Z0-9_]*(?:_AK|_KEY|_TOKEN|_SECRET))\s*[=:]\s*[\"']?([A-Za-z0-9]{24,})"
        ),
    ),
    ("URL 里的 ak 参数", re.compile(r"[?&]ak=([A-Za-z0-9]{24,})")),
    ("sk- 开头的 Token", re.compile(r"\b(sk-[A-Za-z0-9][A-Za-z0-9_-]{19,})")),
]
PLACEHOLDER = re.compile(r"^(your_|xxx|example|placeholder|test|fake|dummy)", re.IGNORECASE)


def repo_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        raise SystemExit("需要在 git 仓库里运行（用 git ls-files 列出会进仓库的文件）") from None
    names = [n for n in out.decode("utf-8", "replace").split("\0") if n]
    files = []
    for name in names:
        path = ROOT / name
        if path.suffix.lower() in SKIP_SUFFIX or path.name in SKIP_NAMES:
            continue
        if path.is_file() and path.stat().st_size <= MAX_BYTES:
            files.append(path)
    return files


def env_secrets() -> dict[str, str]:
    """本机 .env 里像密钥的值（键名以 AK / KEY / TOKEN / SECRET / PASSWORD 结尾，长度 ≥ 12）。"""
    env = ROOT / ".env"
    if not env.exists():
        return {}
    found: dict[str, str] = {}
    for line in env.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip().strip("\"'")
        if SECRET_KEY.search(key.strip()) and len(value) >= 12 and not PLACEHOLDER.match(value):
            found[key.strip()] = value
    return found


def mask(value: str) -> str:
    return f"{value[:4]}…（{len(value)} 位）"


def main() -> int:
    secrets = env_secrets()
    problems: list[str] = []
    files = repo_files()
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # 二进制或读不了的文件不扫
        rel = path.relative_to(ROOT).as_posix()
        for lineno, line in enumerate(text.splitlines(), start=1):
            for key, value in secrets.items():
                if value in line:
                    problems.append(f"{rel}:{lineno}  出现了 .env 里 {key} 的真实值 {mask(value)}")
            for label, pattern in PATTERNS:
                for m in pattern.finditer(line):
                    value = m.group(m.lastindex or 0)
                    if PLACEHOLDER.match(value):
                        continue
                    problems.append(f"{rel}:{lineno}  {label} {mask(value)}")

    if problems:
        print(f"发现 {len(problems)} 处疑似密钥：", file=sys.stderr)
        for p in sorted(set(problems)):
            print(f"  {p}", file=sys.stderr)
        print(
            "\n密钥只放在 .env（已被 .gitignore 排除）。"
            "已经提交过的，除了删掉，还要去控制台作废重建。",
            file=sys.stderr,
        )
        return 1
    source = (
        f"，并核对了 .env 里的 {len(secrets)} 个密钥值"
        if secrets
        else "（没有 .env，只查常见写法）"
    )
    print(f"检查了 {len(files)} 个会进仓库的文件{source}：没有发现写死的密钥。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
