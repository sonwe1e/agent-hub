#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent Hub 桌面小 GUI（无控制台窗口启动请用 pythonw / hub.cmd gui）。

交互要点:
  - 左侧点选供应商即加载详情，改完点「保存」或 Ctrl+S
  - 「检测模型」自动拉取远端列表，勾选后一键导入
  - 模型表首列点击勾选，Delete 键批量删除
  - 顶部「同步」一键写入所有 agent
"""

from __future__ import annotations

import copy
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

# Hub 自身目录不再绑定用户主目录：按本文件位置定位，
# 整个目录可以任意搬迁（例如从 ~/.agent-hub 移到 G:\workspaces\agent-hub）。
HUB_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HUB_DIR))

from hub import (Hub, HUB_CONFIG, assert_expected_model_changes, backup_file,  # noqa: E402
                 load_json, now_iso, save_json)
from discover import discover_models  # noqa: E402

# ---- 视觉 ----
COL_BG = "#f0f2f5"
COL_PANEL = "#ffffff"
COL_INK = "#1c1e21"
COL_MUTED = "#6b7280"
COL_LINE = "#e2e5e9"
COL_ACCENT = "#2563eb"
COL_ACCENT_SOFT = "#dbeafe"
COL_DANGER = "#dc2626"
COL_DANGER_SOFT = "#fee2e2"
COL_OK = "#16a34a"
COL_WARN = "#d97706"
FONT_UI = ("Segoe UI", 10)
FONT_UI_B = ("Segoe UI", 10, "bold")
FONT_TITLE = ("Segoe UI", 13, "bold")
FONT_MONO = ("Consolas", 9)
PAD = 8


class HubGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Agent Hub")
        self.geometry("1040x680")
        self.minsize(900, 560)
        self.configure(bg=COL_BG)

        self.hub = Hub()
        self.providers = self.hub.cfg.get("providers") or {}
        self.current_id: str | None = None
        self.model_checks: dict[str, tk.BooleanVar] = {}
        self.discover_result: list[dict] = []
        self._dirty_provider = False

        self._style()
        self._layout()
        self._bind_keys()
        self.after(60, self.refresh_list)

    # ---------- style ----------
    def _style(self) -> None:
        s = ttk.Style(self)
        if "vista" in s.theme_names():
            s.theme_use("vista")
        elif "clam" in s.theme_names():
            s.theme_use("clam")
        s.configure(".", font=FONT_UI, background=COL_BG, foreground=COL_INK)
        s.configure("TFrame", background=COL_BG)
        s.configure("Card.TFrame", background=COL_PANEL)
        s.configure("TLabel", background=COL_BG, foreground=COL_INK)
        s.configure("Card.TLabel", background=COL_PANEL, foreground=COL_INK)
        s.configure("Muted.TLabel", background=COL_PANEL, foreground=COL_MUTED)
        s.configure("Title.TLabel", background=COL_BG, font=FONT_TITLE)
        s.configure("TButton", padding=(10, 5))
        s.configure("Accent.TButton", background=COL_ACCENT, foreground="white")
        s.map("Accent.TButton", background=[("active", "#1d4ed8")])
        s.configure("Danger.TButton", foreground=COL_DANGER)
        s.configure("TLabelframe", background=COL_PANEL, bordercolor=COL_LINE)
        s.configure("TLabelframe.Label", background=COL_PANEL, foreground=COL_MUTED, font=FONT_UI_B)
        s.configure("TEntry", fieldbackground="white")
        s.configure("Treeview", background="white", fieldbackground="white", rowheight=26, font=FONT_UI)
        s.configure("Treeview.Heading", font=FONT_UI_B, background="#f8fafc")
        s.map("Treeview", background=[("selected", COL_ACCENT_SOFT)], foreground=[("selected", COL_INK)])
        s.configure("TCheckbutton", background=COL_PANEL)
        s.configure("Horizontal.TProgressbar", troughcolor=COL_LINE, background=COL_ACCENT)

    # ---------- layout ----------
    def _layout(self) -> None:
        # 顶栏
        top = tk.Frame(self, bg=COL_BG, padx=12, pady=10)
        top.pack(fill=tk.X)
        tk.Label(top, text="Agent Hub", font=FONT_TITLE, bg=COL_BG, fg=COL_INK).pack(side=tk.LEFT)
        tk.Label(top, text="供应商 / 模型统一管理", font=FONT_UI, bg=COL_BG, fg=COL_MUTED).pack(side=tk.LEFT, padx=10)

        self.progress = ttk.Progressbar(top, length=120, mode="indeterminate")
        self.progress.pack(side=tk.RIGHT, padx=(8, 0))

        btn_sync = ttk.Button(top, text="⟳ 同步", style="Accent.TButton", command=self.do_sync)
        btn_sync.pack(side=tk.RIGHT, padx=4)
        ttk.Button(top, text="刷新", command=self.refresh_list).pack(side=tk.RIGHT, padx=4)

        # 主体
        body = tk.Frame(self, bg=COL_BG, padx=12, pady=8)
        body.pack(fill=tk.BOTH, expand=True)

        # 左卡片
        left = tk.Frame(body, bg=COL_PANEL, highlightbackground=COL_LINE, highlightthickness=1)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        left.configure(width=240)
        left.pack_propagate(False)

        lf_head = tk.Frame(left, bg=COL_PANEL)
        lf_head.pack(fill=tk.X, padx=10, pady=(10, 6))
        tk.Label(lf_head, text="供应商", font=FONT_UI_B, bg=COL_PANEL).pack(side=tk.LEFT)
        ttk.Button(lf_head, text="＋", width=3, command=self.add_provider).pack(side=tk.RIGHT, padx=2)
        ttk.Button(lf_head, text="－", width=3, command=self.delete_provider).pack(side=tk.RIGHT)

        self.prov_list = tk.Listbox(
            left,
            border=0,
            highlightthickness=0,
            activestyle="none",
            font=FONT_UI,
            selectbackground=COL_ACCENT_SOFT,
            selectforeground=COL_INK,
            exportselection=False,
        )
        self.prov_list.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 10))
        self.prov_list.bind("<<ListboxSelect>>", self.on_select)
        self.prov_list.bind("<Double-Button-1>", lambda e: self.focus_form())

        # 右卡片
        right = tk.Frame(body, bg=COL_PANEL, highlightbackground=COL_LINE, highlightthickness=1)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 右：表单
        form_wrap = tk.Frame(right, bg=COL_PANEL)
        form_wrap.pack(fill=tk.X, padx=12, pady=10)

        self.lbl_title = tk.Label(form_wrap, text="选择左侧供应商", font=FONT_UI_B, bg=COL_PANEL, fg=COL_INK)
        self.lbl_title.pack(anchor=tk.W)

        self.vars = {
            "name": tk.StringVar(),
            "displayName": tk.StringVar(),
            "baseUrl": tk.StringVar(),
            "apiKey": tk.StringVar(),
            "api": tk.StringVar(value="openai-completions"),
            "anthropicBaseUrl": tk.StringVar(),
            "notes": tk.StringVar(),
            "enabled": tk.BooleanVar(value=True),
        }
        for v in self.vars.values():
            if hasattr(v, "trace_add"):
                v.trace_add("write", self._mark_dirty)

        grid = tk.Frame(form_wrap, bg=COL_PANEL)
        grid.pack(fill=tk.X, pady=(8, 0))
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(2, weight=1)

        def add_field(r, c, label, key, show=""):
            fr = tk.Frame(grid, bg=COL_PANEL)
            fr.grid(row=r, column=c, sticky="ew", padx=(0, 10), pady=3)
            tk.Label(fr, text=label, font=FONT_UI, bg=COL_PANEL, fg=COL_MUTED).pack(anchor=tk.W)
            ent = ttk.Entry(fr, textvariable=self.vars[key], show=show)
            ent.pack(fill=tk.X, pady=(2, 0))
            return ent

        self.entry_name = add_field(0, 0, "名称", "name")
        self.entry_disp = add_field(0, 1, "显示名", "displayName")
        self.entry_url = add_field(0, 2, "Base URL", "baseUrl")
        self.entry_key = add_field(1, 0, "API Key", "apiKey", show="•")
        self.entry_anthropic = add_field(1, 1, "Anthropic URL（可选）", "anthropicBaseUrl")

        fr_api = tk.Frame(grid, bg=COL_PANEL)
        fr_api.grid(row=1, column=2, sticky="ew", padx=(0, 10), pady=3)
        tk.Label(fr_api, text="协议", font=FONT_UI, bg=COL_PANEL, fg=COL_MUTED).pack(anchor=tk.W)
        self.combo_api = ttk.Combobox(
            fr_api,
            textvariable=self.vars["api"],
            values=["openai-completions", "openai-responses", "openai-chat-completions", "anthropic-messages"],
            state="readonly",
        )
        self.combo_api.pack(fill=tk.X, pady=(2, 0))

        fr_bottom = tk.Frame(grid, bg=COL_PANEL)
        fr_bottom.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Checkbutton(fr_bottom, text="启用该供应商", variable=self.vars["enabled"]).pack(side=tk.LEFT)
        tk.Label(fr_bottom, text="备注", bg=COL_PANEL, fg=COL_MUTED).pack(side=tk.LEFT, padx=(16, 6))
        self.entry_notes = ttk.Entry(fr_bottom, textvariable=self.vars["notes"])
        self.entry_notes.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 表单按钮行
        form_btns = tk.Frame(form_wrap, bg=COL_PANEL)
        form_btns.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(form_btns, text="保存供应商", style="Accent.TButton", command=self.save_provider).pack(side=tk.LEFT)
        ttk.Button(form_btns, text="检测模型", command=self.detect_models).pack(side=tk.LEFT, padx=8)
        ttk.Button(form_btns, text="导入到列表", command=self.import_detected).pack(side=tk.LEFT)
        ttk.Button(form_btns, text="删除供应商", style="Danger.TButton", command=self.delete_provider).pack(side=tk.RIGHT)

        # 分隔
        ttk.Separator(right, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=12)

        # 模型区
        model_head = tk.Frame(right, bg=COL_PANEL)
        model_head.pack(fill=tk.X, padx=12, pady=(8, 4))
        tk.Label(model_head, text="模型", font=FONT_UI_B, bg=COL_PANEL).pack(side=tk.LEFT)
        self.lbl_model_count = tk.Label(model_head, text="(0)", bg=COL_PANEL, fg=COL_MUTED)
        self.lbl_model_count.pack(side=tk.LEFT, padx=6)

        ttk.Button(model_head, text="＋ 添加", command=self.add_model).pack(side=tk.RIGHT, padx=3)
        ttk.Button(model_head, text="批量删除", style="Danger.TButton", command=self.batch_delete).pack(side=tk.RIGHT, padx=3)
        ttk.Button(model_head, text="保存模型", style="Accent.TButton", command=self.save_models).pack(side=tk.RIGHT, padx=3)
        ttk.Button(model_head, text="全选", command=self.select_all).pack(side=tk.RIGHT, padx=3)
        ttk.Button(model_head, text="清空", command=self.select_none).pack(side=tk.RIGHT)

        tree_frame = tk.Frame(right, bg=COL_PANEL)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        cols = ("sel", "id", "name", "ctx", "max", "input", "reason")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", selectmode="extended")
        heads = {
            "sel": ("✓", 34),
            "id": ("模型 ID", 200),
            "name": ("名称", 150),
            "ctx": ("上下文", 80),
            "max": ("最大输出", 80),
            "input": ("输入", 110),
            "reason": ("推理", 50),
        }
        for c, (t, w) in heads.items():
            if c == "sel":
                self.tree.heading(c, text=t, command=self._toggle_all)
            else:
                self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=tk.CENTER if c in ("sel", "ctx", "max", "reason") else tk.W)
        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<Double-Button-1>", self.on_tree_dblclick)

        # 检测结果预览条
        self.discover_bar = tk.Frame(right, bg=COL_ACCENT_SOFT)
        self.discover_label = tk.Label(
            self.discover_bar,
            text="",
            bg=COL_ACCENT_SOFT,
            fg=COL_INK,
            font=FONT_UI,
            anchor=tk.W,
            justify=tk.LEFT,
        )
        self.discover_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10, pady=6)

        # 状态栏
        status = tk.Frame(self, bg="#e8eaed", height=28)
        status.pack(fill=tk.X, side=tk.BOTTOM)
        self.status = tk.Label(status, text="就绪", bg="#e8eaed", fg=COL_MUTED, font=FONT_UI, anchor=tk.W)
        self.status.pack(fill=tk.X, padx=12, pady=4)

    def _bind_keys(self) -> None:
        self.bind("<Control-s>", lambda e: self.save_provider())
        self.bind("<Control-S>", lambda e: self.save_provider())
        self.bind("<Delete>", lambda e: self.batch_delete())
        self.bind("<Control-a>", self._ctrl_a)

    def _ctrl_a(self, _e):
        w = self.focus_get()
        if w is self.tree:
            self.select_all()
            return "break"
        return None

    def _mark_dirty(self, *_):
        self._dirty_provider = True

    def _toggle_all(self):
        selected = [mid for mid, v in self.model_checks.items() if v.get()]
        if selected and len(selected) == len(self.model_checks):
            self.select_none()
        else:
            self.select_all()

    def set_status(self, msg: str, busy: bool = False) -> None:
        self.status.configure(text=msg)
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()
        self.update_idletasks()

    def focus_form(self):
        self.entry_name.focus_set()

    # ---------- data ----------
    def refresh_list(self, keep: str | None = None) -> None:
        try:
            self.hub = Hub()
            self.providers = self.hub.cfg.get("providers") or {}
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("错误", f"读取配置失败: {e}")
            return

        self.prov_list.delete(0, tk.END)
        for pid, p in self.providers.items():
            mark = "●" if p.get("enabled", True) else "○"
            n = len(p.get("models") or [])
            self.prov_list.insert(tk.END, f"  {mark}  {p.get('displayName') or p.get('name') or pid}   ({n})")

        target = keep or self.current_id
        if target and target in self.providers:
            idx = list(self.providers.keys()).index(target)
            self.prov_list.selection_clear(0, tk.END)
            self.prov_list.selection_set(idx)
            self.prov_list.see(idx)
            self.current_id = target
            self.load_provider(target)
        elif self.providers:
            self.prov_list.selection_set(0)
            self.current_id = list(self.providers.keys())[0]
            self.load_provider(self.current_id)
        else:
            self.current_id = None
            self.clear_form()
        self.set_status(f"已加载 {len(self.providers)} 个供应商")

    def on_select(self, _e=None):
        if self._dirty_provider and self.current_id:
            if not messagebox.askyesno("未保存", "当前供应商有未保存修改，切换会丢弃。\n继续？"):
                # 还原选中
                if self.current_id in self.providers:
                    idx = list(self.providers.keys()).index(self.current_id)
                    self.prov_list.selection_clear(0, tk.END)
                    self.prov_list.selection_set(idx)
                return
        sel = self.prov_list.curselection()
        if not sel:
            return
        self.current_id = list(self.providers.keys())[sel[0]]
        self.load_provider(self.current_id)
        self._dirty_provider = False

    def clear_form(self):
        for k, v in self.vars.items():
            if k == "enabled":
                v.set(True)
            elif k == "api":
                v.set("openai-completions")
            else:
                v.set("")
        self.tree.delete(*self.tree.get_children())
        self.model_checks.clear()
        self.lbl_title.configure(text="选择左侧供应商")
        self.hide_discover_bar()

    def load_provider(self, pid: str):
        p = self.providers.get(pid) or {}
        self.lbl_title.configure(text=f"{p.get('displayName') or p.get('name') or pid}   [{pid}]")
        self.vars["name"].set(p.get("name") or "")
        self.vars["displayName"].set(p.get("displayName") or "")
        self.vars["baseUrl"].set(p.get("baseUrl") or "")
        self.vars["apiKey"].set(p.get("apiKey") or "")
        self.vars["api"].set(p.get("api") or "openai-completions")
        self.vars["anthropicBaseUrl"].set(p.get("anthropicBaseUrl") or "")
        self.vars["notes"].set(p.get("notes") or "")
        self.vars["enabled"].set(p.get("enabled", True))
        self.fill_models(p.get("models") or [])
        self._dirty_provider = False
        self.hide_discover_bar()

    def fill_models(self, models: list[dict]):
        self.tree.delete(*self.tree.get_children())
        self.model_checks.clear()
        for m in models:
            self._insert_row(m, checked=False)
        self._update_counts()

    def _insert_row(self, m: dict, checked: bool = False):
        mid = m.get("id") or ""
        if not mid:
            return
        var = tk.BooleanVar(value=checked)
        self.model_checks[mid] = var
        self.tree.insert(
            "",
            tk.END,
            iid=mid,
            values=(
                "☑" if checked else "□",
                mid,
                m.get("name") or mid,
                m.get("contextWindow") or "",
                m.get("maxTokens") or "",
                ",".join(m.get("input") or ["text"]),
                "是" if m.get("reasoning", True) else "否",
            ),
        )

    def _update_counts(self):
        total = len(self.model_checks)
        sel = sum(1 for v in self.model_checks.values() if v.get())
        self.lbl_model_count.configure(text=f"({sel} 选中 / 共 {total})")

    # ---------- model selection ----------
    def on_tree_click(self, event):
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        row = self.tree.identify_row(event.y)
        if not row or row not in self.model_checks:
            return
        var = self.model_checks[row]
        var.set(not var.get())
        vals = list(self.tree.item(row, "values"))
        vals[0] = "☑" if var.get() else "□"
        self.tree.item(row, values=vals)
        self._update_counts()

    def on_tree_dblclick(self, event):
        # 双击切换「推理」列方便快速调整
        row = self.tree.identify_row(event.y)
        if not row:
            return
        col = self.tree.identify_column(event.x)
        vals = list(self.tree.item(row, "values"))
        if col == "#7":  # 推理
            vals[6] = "否" if vals[6] == "是" else "是"
            self.tree.item(row, values=vals)
            self._dirty_provider = True
        elif col == "#1":
            # 双击 ID 可改名（弹输入）
            new = self._ask("修改模型 ID", "新 ID:", vals[1])
            if new and new != vals[1]:
                if new in self.model_checks:
                    messagebox.showwarning("提示", "该 ID 已存在")
                    return
                self.tree.item(row, iid=new, values=(vals[0], new, *vals[2:]))
                self.model_checks[new] = self.model_checks.pop(vals[1], tk.BooleanVar(value=False))
                self._update_counts()

    def select_all(self):
        for mid, var in self.model_checks.items():
            var.set(True)
            vals = list(self.tree.item(mid, "values"))
            vals[0] = "☑"
            self.tree.item(mid, values=vals)
        self._update_counts()

    def select_none(self):
        for mid, var in self.model_checks.items():
            var.set(False)
            vals = list(self.tree.item(mid, "values"))
            vals[0] = "□"
            self.tree.item(mid, values=vals)
        self._update_counts()

    def selected_ids(self):
        return [mid for mid, v in self.model_checks.items() if v.get()]

    # ---------- provider ----------
    def _ask(self, title, prompt, initial=""):
        from tkinter import simpledialog

        return simpledialog.askstring(title, prompt, parent=self, initialvalue=initial)

    def add_provider(self):
        pid = self._ask("添加供应商", "Provider ID（字母/数字/-/_）")
        if not pid:
            return
        pid = pid.strip()
        if pid in self.providers:
            messagebox.showwarning("提示", "该 ID 已存在")
            return
        self.providers[pid] = {
            "name": pid,
            "displayName": pid,
            "baseUrl": "",
            "apiKey": "",
            "api": "openai-completions",
            "enabled": True,
            "models": [],
        }
        self.persist()
        self.refresh_list(keep=pid)
        self.set_status(f"已创建 {pid}，请填写 Base URL / API Key 后保存")
        self.entry_url.focus_set()

    def delete_provider(self):
        if not self.current_id:
            return
        pid = self.current_id
        n = len(self.providers.get(pid, {}).get("models") or [])
        if not messagebox.askyesno(
            "删除供应商",
            f"删除「{pid}」及其 {n} 个模型？\n会从 hub 与所有 agent 配置中移除，不可撤销。",
        ):
            return
        self.set_status("正在删除…", busy=True)
        try:
            msgs = self.hub.remove_provider(pid)
            self.hub = Hub()
            self.providers = self.hub.cfg.get("providers") or {}
            self.current_id = None
            self._dirty_provider = False
            self.refresh_list()
            self.set_status(" | ".join(msgs))
        except Exception as e:  # noqa: BLE001
            self.set_status("删除失败")
            messagebox.showerror("错误", str(e))

    def save_provider(self):
        if not self.current_id:
            messagebox.showinfo("提示", "请先选择或添加供应商")
            return
        p = self.providers.setdefault(self.current_id, {})
        for key in ("name", "displayName", "baseUrl", "apiKey", "api", "anthropicBaseUrl", "notes"):
            p[key] = self.vars[key].get().strip()
        p.setdefault("name", self.current_id)
        p.setdefault("displayName", p["name"])
        if not p.get("name"):
            p["name"] = self.current_id
        if not p.get("displayName"):
            p["displayName"] = p["name"]
        p["enabled"] = bool(self.vars["enabled"].get())
        if not p.get("anthropicBaseUrl"):
            p.pop("anthropicBaseUrl", None)
        self.persist()
        self._dirty_provider = False
        self.refresh_list(keep=self.current_id)
        self.set_status("供应商已保存")

    # ---------- models ----------
    def collect_models(self) -> list[dict]:
        out = []
        for iid in self.tree.get_children():
            vals = self.tree.item(iid, "values")
            mid = str(vals[1] or "").strip()
            if not mid:
                continue
            out.append(
                {
                    "id": mid,
                    "name": str(vals[2] or mid),
                    "contextWindow": int(vals[3]) if str(vals[3]).isdigit() else 128000,
                    "maxTokens": int(vals[4]) if str(vals[4]).isdigit() else 32768,
                    "input": [s.strip() for s in str(vals[5] or "text").split(",") if s.strip()],
                    "reasoning": vals[6] == "是",
                }
            )
        return out

    def save_models(self):
        if not self.current_id:
            return
        models = self.collect_models()
        old_models = self.providers.get(self.current_id, {}).get("models") or []
        prev = len(old_models)
        if prev and not models:
            if not messagebox.askyesno("确认", "当前列表为空，确认清空该供应商的所有模型？"):
                return
        # 防呆：大幅缩减需二次确认
        if prev >= 5 and len(models) < prev * 0.5:
            if not messagebox.askyesno(
                "确认缩减",
                f"模型数量将从 {prev} 减到 {len(models)}。\n这可能是误操作，确认继续？",
            ):
                return
        old_ids = {m.get("id") for m in old_models}
        new_ids = {m["id"] for m in models}
        self.providers[self.current_id]["models"] = models
        # 用户在对话框里确认过的删除，才允许这一次数量下降
        self.persist(allowed_removed={self.current_id: old_ids - new_ids})
        self.refresh_list(keep=self.current_id)
        self.set_status(f"已保存 {len(models)} 个模型" + (f"（原 {prev}）" if prev != len(models) else ""))

    def add_model(self):
        mid = self._ask("添加模型", "模型 ID:")
        if not mid:
            return
        mid = mid.strip()
        if mid in self.model_checks:
            messagebox.showwarning("提示", "该模型 ID 已存在")
            return
        self._insert_row(
            {"id": mid, "name": mid, "contextWindow": 128000, "maxTokens": 32768, "input": ["text"], "reasoning": True},
            checked=True,
        )
        self._update_counts()

    def batch_delete(self):
        ids = self.selected_ids()
        if not ids:
            self.set_status("请先勾选要删除的模型（点行首复选框）")
            return
        preview = "\n".join(ids[:15]) + ("\n…" if len(ids) > 15 else "")
        if not messagebox.askyesno("批量删除", f"删除 {len(ids)} 个模型？\n\n{preview}"):
            return
        for mid in ids:
            if self.tree.exists(mid):
                self.tree.delete(mid)
            self.model_checks.pop(mid, None)
        self._update_counts()
        self.save_models()
        self.set_status(f"已删除 {len(ids)} 个模型")

    # ---------- detect ----------
    def detect_models(self):
        if not self.current_id:
            return
        # 静默保存表单，保证 URL/Key 最新
        try:
            self.save_provider()
        except Exception:
            pass
        p = self.providers.get(self.current_id) or {}
        self.set_status("正在检测远端模型…", busy=True)

        def worker():
            try:
                models = discover_models(p)
                self.after(0, lambda: self._on_detected(models))
            except Exception as e:  # noqa: BLE001
                self.after(0, lambda: self._on_detect_error(str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_detect_error(self, msg: str):
        self.set_status("检测失败")
        messagebox.showerror("检测模型失败", msg)

    def _on_detected(self, models: list[dict]):
        self.set_status(f"检测到 {len(models)} 个模型")
        self.discover_result = models
        existing = {m.get("id") for m in self.collect_models()}
        new_n = sum(1 for m in models if m["id"] not in existing)

        self.discover_bar.pack(fill=tk.X, padx=12, pady=(0, 12))
        self.discover_label.configure(
            text=f"检测到 {len(models)} 个模型，其中新增 {new_n} 个。点「导入到列表」合并进下方表格。",
        )
        for w in list(self.discover_bar.winfo_children()):
            if w is not self.discover_label:
                w.destroy()
        ttk.Button(self.discover_bar, text=f"导入到列表 ({new_n} 新增)", command=self.import_detected).pack(side=tk.RIGHT, padx=8)

    def hide_discover_bar(self):
        self.discover_bar.pack_forget()
        self.discover_result = []

    def import_detected(self):
        if not self.discover_result:
            self.set_status("请先点「检测模型」")
            return
        current = {m["id"]: m for m in self.collect_models()}
        added = 0
        for m in self.discover_result:
            if m["id"] not in current:
                self._insert_row(m, checked=True)
                added += 1
            else:
                # 已存在的勾选，方便批量操作
                if m["id"] in self.model_checks:
                    self.model_checks[m["id"]].set(True)
                    vals = list(self.tree.item(m["id"], "values"))
                    vals[0] = "☑"
                    self.tree.item(m["id"], values=vals)
        self._update_counts()
        self.hide_discover_bar()
        self.set_status(f"已合并检测结果：新增 {added} 个（已勾选）。点「保存模型」写入。")

    # ---------- sync ----------
    def do_sync(self):
        if self._dirty_provider and self.current_id:
            if messagebox.askyesno("同步", "有未保存修改，先保存当前供应商再同步？"):
                try:
                    self.save_provider()
                except Exception as e:  # noqa: BLE001
                    messagebox.showerror("保存失败", str(e))
                    return
        self.set_status("正在同步到各 agent…", busy=True)

        def worker():
            try:
                self.hub = Hub()
                changed = self.hub.changed_files()
                results = self.hub.push_all()
                self.hub.record_hashes()
                self.hub.save_state()
                prefix = f"检测到 {len(changed)} 处外部变更；" if changed else ""
                self.after(0, lambda: self.set_status(prefix + "同步完成"))
            except Exception as e:  # noqa: BLE001
                self.after(0, lambda: self._sync_fail(str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _sync_fail(self, msg: str):
        self.set_status("同步失败")
        messagebox.showerror("同步失败", msg)

    def persist(self, allowed_removed: dict | None = None):
        """把当前供应商的改动写回 providers.json。

        不能直接回写 self.hub.cfg：那是打开窗口时的内存快照，
        新版工作台/其它进程刚写入的模型会被整段抹掉（历史事故根因之一）。
        这里重新读盘，只覆盖当前编辑的供应商。
        """
        disk = load_json(HUB_CONFIG, default=None)
        fresh = disk if isinstance(disk, dict) else dict(self.hub.cfg)
        before = copy.deepcopy(fresh)
        providers = fresh.setdefault("providers", {})
        if self.current_id:
            providers[self.current_id] = copy.deepcopy(self.providers.get(self.current_id) or {})
        else:
            providers.update(copy.deepcopy(self.providers))
        # 本次会话里新建的供应商必须落盘（它们不可能造成丢失）
        for pid, p in (self.providers or {}).items():
            if pid not in providers:
                providers[pid] = copy.deepcopy(p)
        fresh["providers"] = providers
        fresh["updatedAt"] = now_iso()
        assert_expected_model_changes(before, fresh, allowed_removed=allowed_removed or {})
        backup_file(HUB_CONFIG)
        save_json(HUB_CONFIG, fresh)
        self.hub.cfg = fresh
        self.hub.providers = providers
        self.providers = providers


def main() -> int:
    # 无控制台场景下避免 tk 把错误吞掉
    try:
        app = HubGUI()
        app.mainloop()
    except Exception as e:  # noqa: BLE001
        try:
            messagebox.showerror("Agent Hub 异常", str(e))
        except Exception:  # noqa: BLE001
            (HUB_DIR / "gui-error.log").write_text(str(e), encoding="utf-8")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
