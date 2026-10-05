"""Repair cc-switch.db: agent-hub 曾用 datetime('now') 写入 TEXT 型 created_at，
而 cc-switch (Tauri) 按 INTEGER epoch 毫秒读取该列，启动 setup 阶段即崩溃
("Invalid column type Text at index: 5, name: created_at")。

本脚本：
  1. 先扫描所有表，列出 INTEGER 声明列里实际存放 TEXT 的情况（只读报告）；
  2. 备份 cc-switch.db 到 agent-hub/backups/；
  3. 把 TEXT 的 created_at 解析成 INTEGER epoch 毫秒（SQLite datetime('now') 为 UTC）；
  4. VACUUM 回收 freelist（本库约 74% 页为空闲，压缩后启动更快）；
  5. 校验：不再有 TEXT，integrity_check 通过。

NULL 值说明：备份证据显示 app 自身长期容忍 created_at 为 NULL（rusqlite Option<i64>），
崩溃信息只指向 Text，因此 NULL 行保持原样。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

DB = os.path.expanduser(r"~\.cc-switch\cc-switch.db")
BACKUP_DIR = os.path.expanduser(r"~\.cc-switch\backups")
FALLBACK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backups")

FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f")


def parse_text_ts(value: str) -> int:
    """SQLite datetime('now') 输出的是 UTC 'YYYY-MM-DD HH:MM:SS'；转 epoch 毫秒。"""
    text = value.strip()
    for fmt in FORMATS:
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(f"无法解析的时间文本: {value!r}")


def scan_integer_columns(cur: sqlite3.Cursor) -> list[tuple[str, str]]:
    """报告所有 INTEGER 声明列中 typeof 为 text 的 (table, column)。"""
    problems = []
    tables = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        for _cid, col, coltype, *_ in cur.execute(f"PRAGMA table_info({table})"):
            if coltype.upper().startswith("INT"):
                n = cur.execute(
                    f"SELECT count(*) FROM {table} WHERE typeof({col}) = 'text'"
                ).fetchone()[0]
                if n:
                    problems.append((table, col, n))
    return problems


def main() -> int:
    if not os.path.exists(DB):
        print(f"找不到数据库: {DB}")
        return 1

    con = sqlite3.connect(DB)
    try:
        cur = con.cursor()
        print("== 修复前 ==")
        problems = scan_integer_columns(cur)
        for table, col, n in problems:
            print(f"  {table}.{col}: {n} 行 TEXT")
        if not problems:
            print("  未发现 TEXT 型数据")
        print("  created_at 类型分布:",
              cur.execute("SELECT typeof(created_at), count(*) FROM providers GROUP BY typeof(created_at)").fetchall())

        text_rows = cur.execute(
            "SELECT id, created_at FROM providers WHERE typeof(created_at) = 'text' ORDER BY id"
        ).fetchall()
        if not text_rows:
            print("\n无需修复。")
            return 0

        # 备份（毫秒精度，连续执行不互相覆盖）
        backup_dir = BACKUP_DIR if os.path.isdir(BACKUP_DIR) else FALLBACK_DIR
        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = os.path.join(backup_dir, f"cc-switch.db.pre-fix.{stamp}")
        shutil.copy2(DB, backup)
        print(f"\n已备份: {backup} ({os.path.getsize(backup)} 字节)")

        # 转换 TEXT -> INTEGER epoch 毫秒
        converted = 0
        for pid, raw in text_rows:
            try:
                epoch_ms = parse_text_ts(raw)
            except ValueError as e:
                print(f"  [跳过] {pid}: {e}")
                continue
            cur.execute("UPDATE providers SET created_at = ? WHERE id = ?", (epoch_ms, pid))
            converted += 1
            print(f"  转换 {pid}: {raw!r} -> {epoch_ms}")
        con.commit()

        # VACUUM（必须在事务外执行）
        before = os.path.getsize(DB)
        cur.execute("VACUUM")
        after = os.path.getsize(DB)
        print(f"\nVACUUM: {before} -> {after} 字节")

        print("\n== 修复后 ==")
        print("  created_at 类型分布:",
              cur.execute("SELECT typeof(created_at), count(*) FROM providers GROUP BY typeof(created_at)").fetchall())
        print("  integrity_check:", cur.execute("PRAGMA integrity_check").fetchone()[0])
        print(f"\n完成: {converted}/{len(text_rows)} 行已转换为 INTEGER。")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
