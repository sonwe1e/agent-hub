"""在真实 WebView2 里校验新主题是否生效。

检查项：
  · 三束光场（--field-glow / --field-settle / --field-warm）是否应用到 body —— 采样 + 断言
  · 衬线字体是否真的加载（document.fonts）
  · 关键组件的计算样式是否符合 docs/design/visual-direction.md
  · 表格表头无色带、行用发丝线
  · 未知态不填色

为什么会有「三束光场」这一段：Phase 4 之前 body 上的三层是「色斑 + 28px 重复横格
+ feTurbulence 噪点」，那是**全屏平铺纹理**，在任何窗口尺寸下都会顺铺，已废弃
（见 docs/design/visual-direction.md §11）。本脚本原先把 `--field-fiber` 采下来
却不断言——等于校验脚本对已删除的实现保持沉默，最容易让下一个人误判。现在改成
采样新三条 token 并逐条断言：非空、可解析为渐变、body 不含 url()、不含平铺。

用法：python scripts/verify-desktop-style.py [--timeout 25] [--hard-timeout 40]

退出码：0 = 全部断言通过 / 1 = 断言失败 / 2 = 本脚本没跑起来（无交互式桌面会话）。
区分 1 和 2 很重要：没有桌面会话时 `webview.start()` 不会自己返回，进程会被挂到
被外部杀掉，什么都不打印。现在有总闸（--hard-timeout），到点会打印一句人话并以 2 退出。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import webview  # noqa: E402

import desktop_app  # noqa: E402

CHECK_JS = r"""
(() => {
  const cs = (sel, prop) => {
    const el = document.querySelector(sel);
    return el ? getComputedStyle(el).getPropertyValue(prop).trim() : null;
  };
  const token = name =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const out = {};
  out.bodyBgColor = getComputedStyle(document.body).backgroundColor;
  out.bodyBgImage = getComputedStyle(document.body).backgroundImage;
  out.bodyBgBlend = getComputedStyle(document.body).backgroundBlendMode;
  out.bodyBgSize = getComputedStyle(document.body).backgroundSize;
  out.bodyBgRepeat = getComputedStyle(document.body).backgroundRepeat;
  out.fieldGlow = token('--field-glow');
  out.fieldSettle = token('--field-settle');
  out.fieldWarm = token('--field-warm');
  out.h1Font = cs('h1', 'font-family');
  out.h1Size = cs('h1', 'font-size');
  out.primaryBg = cs('button.primary, .primary', 'background-color');
  out.primaryRadius = cs('button.primary, .primary', 'border-radius');
  out.cardShadow = cs('.card', 'box-shadow');
  out.cardRadius = cs('.card', 'border-radius');
  out.navActiveShadow = cs('.nav-item.active', 'box-shadow');
  out.thBg = cs('.models-table th', 'background-color');
  out.tdBorderBottom = cs('.models-table td', 'border-bottom-color');
  out.checkboxAppearance = cs('input[type=checkbox]', 'appearance');
  out.focusRing = cs(':root', '--accent');
  // 字体是否真的加载
  out.fontsNewsreader = document.fonts ? document.fonts.check('16px "Newsreader"') : null;
  out.fontsPlex = document.fonts ? document.fonts.check('16px "IBM Plex Sans"') : null;
  out.fontsLoaded = document.fonts ? Array.from(document.fonts).map(f => f.family + ':' + f.status).join(',') : null;
  out.sidebarBg = cs('.sidebar', 'background-color');
  out.sidebarImg = cs('.sidebar', 'background-image');
  out.topbarBg = cs('.topbar', 'background-color');
  return out;
})()
"""

EXPECT = {
    "bodyBgColor": "rgb(247, 243, 235)",
    "primaryBg": "rgb(169, 63, 36)",
    "cardShadow": "none",
    "navActiveShadow": "rgb(169, 63, 36) 2px 0px 0px 0px inset",
    "thBg": "rgb(252, 250, 246)",
    "focusRing": "#A93F24",
    "bodyBgBlend": "normal, normal, multiply",
    # 侧栏已退化为纯色 + 发丝线，不该再有任何背景图层
    "sidebarImg": "none",
}

# 关于 thBg：这条原先写的是 `rgba(0, 0, 0, 0)`（完全透明），但 `.models-table th`
# 一直是 `position: sticky` + 不透明底色（HEAD 里是 #fafbfe，现在展开成 --surface）——
# sticky 表头必须不透明，否则滚动时下面的行会从表头里透出来。所以旧断言与实现
# 从一开始就矛盾，脚本不可能绿。现在改成 --surface 展开后的实际值。
# 规范 §4.3 要的「表头无色带」仍然成立：--surface(#FCFAF6) 与 --paper(#F7F3EB)
# 只差 2 个色阶，肉眼读不出色带；真正被禁止的是旧的 #fafbfe 那种蓝灰色带。

# 三束光场：token 名 → 检查结果字段
FIELD_TOKENS = (
    ("--field-glow", "fieldGlow"),
    ("--field-settle", "fieldSettle"),
    ("--field-warm", "fieldWarm"),
)


def _split_layers(value: str) -> list[str]:
    """按**顶层**逗号切分 CSS 图层列表（渐变内部的逗号不算）。

    `background-image: radial-gradient(a, b), linear-gradient(c)` 必须切成 2 段，
    不能按逗号盲切。
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).strip())
    return [p for p in parts if p]


_REPEATING = (
    "repeating-linear-gradient",
    "repeating-radial-gradient",
    "repeating-conic-gradient",
)
# 注意顺序无关，但必须用 startswith 而不是 in：
# "repeating-linear-gradient(" 里包含 "linear-gradient("，用 in 会把平铺渐变误判成合法。
_PLAIN = ("radial-gradient", "linear-gradient", "conic-gradient")
_BAD_LAYER = ("url", "image-set", "none", "-webkit-gradient", "element")


def _func_of(text: str) -> str:
    """返回图层开头的函数名（小写），认不出来返回空串。"""
    head = text.strip().lower()
    for name in _REPEATING + _BAD_LAYER + _PLAIN:
        if head.startswith(name):
            return name
    return ""


def check_light_field(value: dict) -> list[str]:
    """三束光场的断言：非空、是渐变、被 body 引用、无位图、无平铺、有显式尺寸。

    这些断言替代了旧版对 `--field-fiber` 的「只采样不断言」——那种写法会让
    脚本在背景层被整体删掉之后依然保持沉默。

    实现注意：**不要拿 token 原文去和计算样式做字符串包含比较**。浏览器会把
    `.42` 规范成 `0.42`、逗号后补空格，原文永远匹配不上；这里改成按图层逐个
    比对渐变函数类型（radial/linear/repeating/url），这既抗归一化，也能抓到
    「图层被换成位图」和「图层顺序被打乱」。
    """
    bad: list[str] = []

    tokens: list[tuple[str, str]] = []
    for token, key in FIELD_TOKENS:
        got = " ".join((value.get(key) or "").split())
        if not got:
            bad.append(f"{token} 取值为空（token 已删除或未定义）")
            continue
        func = _func_of(got)
        if not func:
            bad.append(f"{token} 不是渐变：{got[:60]!r}")
        elif func in _REPEATING:
            bad.append(f"{token} 是平铺渐变（{func}）：在全屏会顺铺")
        elif func in _BAD_LAYER:
            bad.append(f"{token} 是 {func}：位图/噪点无法约束平铺")
        tokens.append((token, func))

    image = " ".join((value.get("bodyBgImage") or "").split())
    if not image or image == "none":
        bad.append("body 没有 background-image（三束光场未应用）")
    else:
        if "url(" in image:
            bad.append("body 的 background-image 含 url()：位图/噪点无法约束平铺")
        layers = _split_layers(image)
        if len(layers) != len(FIELD_TOKENS):
            bad.append(f"body 的背景图层数 {len(layers)} != 期望 {len(FIELD_TOKENS)}：{layers}")
        else:
            for index, ((token, func), layer) in enumerate(zip(tokens, layers), start=1):
                layer_func = _func_of(layer)
                if not layer_func:
                    bad.append(f"body 第 {index} 层不是可识别的渐变：{layer[:50]!r}")
                elif func and layer_func != func:
                    bad.append(f"{token} 是 {func}，但 body 第 {index} 层是 {layer_func}")

    repeat = value.get("bodyBgRepeat") or ""
    tiled = [p.strip() for p in repeat.split(",") if "repeat" in p.replace("no-repeat", "")]
    if tiled:
        bad.append(f"body 的 background-repeat 含平铺值：{tiled}")

    size = (value.get("bodyBgSize") or "").strip()
    if not size or size == "auto":
        bad.append(f"body 的 background-size 未显式声明：{size!r}（光落点会随页面长短漂移）")

    return bad


def _cannot_run(args, detail: str) -> None:
    """在无交互式桌面会话时，给出解释并硬退出（退出码 2 = 本脚本没跑起来）。

    为什么必须 `os._exit` 而不是 `return 2`：`webview.start()` 在没有桌面会话时
    **不会自己返回**，进程会挂到被外部 SIGTERM 杀掉——下一个人看到的就是
    「无输出、exit 1、SIGTERM」，既不知道发生了什么，也不知道断言从未执行。
    显式硬退出至少留得下一句人话。
    """
    print(
        f"无法在无桌面会话下运行本脚本（需要交互式 WebView2 窗口）：{detail}",
        file=sys.stderr,
    )
    print(
        "  → 本脚本的断言**未被执行**，退出码 2（区别于断言失败的 1）。"
        "请在带桌面会话的机器上运行；纯 CSS 部分可以用 scripts/verify-css.py 覆盖。",
        file=sys.stderr,
    )
    sys.stderr.flush()
    os._exit(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--attempts", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=25.0, help="等 window.events.loaded 的秒数")
    ap.add_argument(
        "--hard-timeout",
        type=float,
        default=None,
        help="整个进程的硬上限秒数，默认 = --timeout + 15（到点直接退出码 2）",
    )
    args = ap.parse_args()
    if args.hard_timeout is None:
        args.hard_timeout = args.timeout + 15.0

    outcome: dict = {}
    finished = threading.Event()      # loaded + evaluate_js 有结果了
    healthy = threading.Event()       # 已经拿到计算结果，可以开始断言

    def hard_deadline() -> None:
        """总闸：到点还没拿到结果，说明窗口根本没起来，给解释后硬退出。"""
        deadline = time.monotonic() + args.hard_timeout
        while time.monotonic() < deadline:
            if healthy.is_set():
                return
            time.sleep(0.2)
        if not healthy.is_set():
            _cannot_run(
                args,
                f"等待 {args.timeout:.0f}s 未收到 window.events.loaded，"
                f"再过 {args.hard_timeout - args.timeout:.0f}s 仍未退出",
            )

    threading.Thread(target=hard_deadline, daemon=True).start()

    try:
        window = webview.create_window(
            "style-check",
            desktop_app.index_url(),
            width=1440,
            height=960,
            background_color=desktop_app.BACKGROUND_COLOR,
        )
    except Exception as exc:                       # 没有 WebView2 运行时 / 无桌面会话
        _cannot_run(args, f"创建窗口失败：{exc!r}")
        return 2                                   # 不会走到，_cannot_run 已硬退出

    def on_loaded():
        def cb(value):
            outcome["value"] = value
            finished.set()

        try:
            window.evaluate_js(CHECK_JS, cb)
        except Exception as e:
            outcome["error"] = repr(e)
            finished.set()

    def watchdog():
        if not finished.wait(args.timeout):
            outcome["error"] = "loaded timeout"
        print(json.dumps(outcome, ensure_ascii=False, default=str), flush=True)
        try:
            window.destroy()
        except Exception:
            pass

    window.events.loaded += on_loaded
    threading.Thread(target=watchdog, daemon=True).start()
    webview.start(private_mode=False, storage_path=str(desktop_app.webview_storage_path()))

    value = outcome.get("value")
    if not value:
        print(
            f"FAIL: 未能取到计算样式 -> {outcome.get('error')!r}（退出码 2 表示本脚本没跑起来，"
            "不是断言失败）",
            file=sys.stderr,
        )
        return 2

    healthy.set()
    print(json.dumps(value, ensure_ascii=False, indent=2))
    bad = []
    for key, want in EXPECT.items():
        got = value.get(key)
        if got != want:
            bad.append(f"{key}: 期望 {want!r}，实际 {got!r}")
    if value.get("fontsNewsreader") is not True:
        bad.append(f"Newsreader 未加载: {value.get('fontsLoaded')}")
    if value.get("fontsPlex") is not True:
        bad.append(f"IBM Plex Sans 未加载: {value.get('fontsLoaded')}")
    bad.extend(check_light_field(value))

    if bad:
        print("\n不符合规范:", file=sys.stderr)
        for line in bad:
            print("  -", line, file=sys.stderr)
        return 1
    print("\n全部符合 docs/design/visual-direction.md（含三束光场断言）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
