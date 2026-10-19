# -*- coding: utf-8 -*-
"""信息泄漏审计：扫描仓库文本文件中疑似密钥与个人路径的字符串。

用法: python scripts/audit-leak-scan.py
退出码 0 = 未发现可疑内容；1 = 有命中，需人工复核。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

KEY_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"[A-Za-z0-9]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}"),
]
# 个人主目录字面量（任何用户名都算）；仓库文档只允许用 ~ 或 %USERPROFILE% 表达。
PERSONAL = re.compile(r"[A-Za-z]:[/\\]+Users[/\\]+[^/\\\"'\s]+")
URL = re.compile(r"https?://[^\s\"'\\,]+")

skip_dirs = {"node_modules", "__pycache__", ".git", "artifacts", "backups", "data", ".zcode", ".pytest_cache"}
skip_files = {"providers.json", "state.json"}

findings = 0
for path in sorted(ROOT.rglob("*")):
    if not path.is_file():
        continue
    if any(part in skip_dirs for part in path.parts):
        continue
    if path.name in skip_files or path.name.startswith("providers.json.drop"):
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeError, OSError):
        continue
    hits = []
    for pat in KEY_PATTERNS:
        hits += pat.findall(text)
    if hits:
        print(f"[KEY?] {path.relative_to(ROOT)}: {len(hits)} potential matches (values withheld)")
        findings += 1
    for m in PERSONAL.finditer(text):
        print(f"[PATH] {path.relative_to(ROOT)}: {text[max(0, m.start() - 30):m.end() + 30]!r}")
        findings += 1
        break

# 单独看 gold / example（允许出现端点 URL，但不允许密钥值）
for name in ("providers.gold.json", "providers.example.json"):
    p = ROOT / name
    if not p.exists():
        continue
    text = p.read_text(encoding="utf-8")
    keys = re.findall(r'"apiKey"\s*:\s*"([^"]*)"', text)
    urls = sorted(set(URL.findall(text)))
    print(f"{name}: nonempty apiKey fields={sum(bool(k) for k in keys)} urls={len(urls)}")

sys.exit(1 if findings else 0)
