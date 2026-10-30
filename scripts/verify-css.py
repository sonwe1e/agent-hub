"""确定性 CSS 校验：不依赖 WebView2 / playwright。

对照 git HEAD 的旧样式，检查新主题是否合规：
  1. 语法：括号平衡、无未闭合规则
  2. 选择器守恒：旧版每个选择器在新版仍存在（避免漏改导致样式丢失）
  3. token 守恒：新版 :root 必须包含规范 §2 要求的关键 token
  4. 反模式：Inter/Roboto/Arial、紫色系、模糊阴影、圆角 >5px、
     中文 letter-spacing >.05em、外链字体、非 400/500/600/700 字重
  5. 字体文件：@font-face 引用的 woff2 都在盘上
  6. 背景：禁止 repeating-linear-gradient / feTurbulence / fractalNoise，
     body 必须是「三束单层渐变 + background-size + no-repeat」（Phase 4 回归约束）
  7. daily.css 不得再定义颜色 token

用法：python scripts/verify-css.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"
DAILY_CSS = "ui/features/workbench/daily.css"
MODELS_CSS = "ui/features/models/models.css"

BANNED_FONT = re.compile(r"\b(Inter|Roboto|Arial|Space Grotesk|system-ui|-apple-system)\b", re.I)
PURPLE = re.compile(r"#(?:6254e8|5143d3|f0edff|f5f3ff|f6f3ff|fbfaff|faf8ff|e5dfff|e2dcff|e9e2ff|eee9ff|c9bfef|dcd5fa|c8c2e8|cfd6ea|b8adff|8a7df0|a595db|8b7cdb|8e80e9|8065b5|7767a9|8a7abc|9b88cf|b0a3e2|9a8ba6|c89bdc|8566ab)", re.I)
OLD_GREY = re.compile(r"#(?:f6f7fb|f6f7fa|fafbfe|fbfcfe|fdfcff|fcfcfe|fcfcff|f7f8fc|f0f1f6|f1f2f7|e9ebf2|dfe2ec|e8ebf2|202433|222539|d34c58|18876b)", re.I)
BLUR_SHADOW = re.compile(r"box-shadow:[^;]*\b\d+px\s+\d+px\s+\d+px\b")
FONT_URL = re.compile(r"url\(\s*['\"]?https?://", re.I)
WEIGHT = re.compile(r"font-weight:\s*(\d+)")
RADIUS = re.compile(r"border-radius:\s*(\d+(?:\.\d+)?)px")

REQUIRED_TOKENS = [
    "--paper", "--paper-sunk", "--surface", "--surface-sunk", "--ink", "--ink-2", "--ink-3", "--ink-4",
    "--rule-faint", "--rule", "--rule-strong", "--rule-control",
    "--accent", "--accent-strong", "--accent-bright", "--accent-wash",
    "--success", "--danger", "--warn", "--unknown",
    "--cat-1", "--cat-2", "--cat-3", "--cat-4",
    "--font-display", "--font-sans", "--font-mono", "--fs-num",
    "--field-glow", "--field-settle", "--field-warm",
    "--r-xs", "--r-sm", "--r-md", "--r-lg",
]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def git_show(rel: str) -> str:
    try:
        return subprocess.run(
            ["git", "show", f"HEAD:{rel}"], cwd=ROOT, capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=True
        ).stdout
    except Exception:
        return ""


def strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def selectors_of(css: str) -> set[str]:
    css = strip_comments(css)
    out: set[str] = set()
    # 去掉 @media / @keyframes / @font-face 块，只取普通规则的选择器
    css = re.sub(r"@(?:media|supports)[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", " ", css, flags=re.S)
    for block in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        head = " ".join(block[0].split())
        if head.startswith("@"):
            continue
    # 组合子两侧空格归一化：.metric > span 与 .metric>span 视为同一个选择器
        head = re.sub(r"\s*([>+~])\s*", r"\1", head)
        for sel in head.split(","):
            sel = sel.strip()
            if sel:
                out.add(sel)
    return out


def main() -> int:
    problems: list[str] = []
    new_css = strip_comments(read("ui/styles.css")) + "\n" + strip_comments(read(DAILY_CSS)) + "\n" + strip_comments(read(MODELS_CSS)) + "\n" + strip_comments(read("ui/fonts.css"))
    fonts_css = read("ui/fonts.css")

    # 1. 括号平衡
    for name in ("ui/styles.css", DAILY_CSS, MODELS_CSS, "ui/fonts.css"):
        text = strip_comments(read(name))
        if text.count("{") != text.count("}"):
            problems.append(f"{name}: 花括号不平衡 ({text.count('{')} vs {text.count('}')})")
        if text.count("(") != text.count(")"):
            problems.append(f"{name}: 圆括号不平衡")

    # 2. 选择器守恒（daily.css 的 :root 缺失是规范 §8.2 的预期结果，不算问题）
    # 已提交迁移后优先比较新路径；旧 HEAD 仍从迁移前的路径读取基线。
    for rel, legacy_rel in (("ui/styles.css", "ui/styles.css"),
                            (DAILY_CSS, "ui/daily.css"), (MODELS_CSS, "ui/models.css")):
        baseline = git_show(rel)
        if not baseline and legacy_rel != rel:
            baseline = git_show(legacy_rel)
        old = selectors_of(baseline)
        new = selectors_of(read(rel))
        if not old:
            # 未跟踪 / HEAD 尚无此文件：守恒没有基线，说明白而不是静默跳过。
            print(f"注: {rel} 在 HEAD 中不存在，选择器守恒自首次提交后生效")
            continue
        missing = sorted(old - new)
        if rel.endswith("daily.css"):
            missing = [s for s in missing if s != ":root"]
        if missing:
            problems.append(f"{rel}: 丢失 {len(missing)} 个选择器 -> {missing[:12]}")

    # 3. token 守恒
    for token in REQUIRED_TOKENS:
        if f"{token}:" not in new_css:
            problems.append(f"缺少必需 token: {token}")

    # 4. 反模式
    for label, pat, limit in (
        ("禁用字体", BANNED_FONT, 0),
        ("紫色系残留", PURPLE, 0),
        ("旧灰蓝残留", OLD_GREY, 0),
        ("外链字体", FONT_URL, 0),
    ):
        hits = pat.findall(new_css)
        if len(hits) > limit:
            problems.append(f"{label}: {len(hits)} 处 -> {sorted(set(hits))[:8]}")

    blurred = BLUR_SHADOW.findall(new_css)
    # 规范只允许 dialog/toast 的零模糊硬压印
    for decl in blurred:
        if "0 0 0" in decl or "3px 3px 0" in decl:
            continue
        problems.append(f"带模糊的 box-shadow: {decl.strip()[:70]}")

    for css_name in ("ui/styles.css", DAILY_CSS, MODELS_CSS):
        text = strip_comments(read(css_name))
        for m in RADIUS.finditer(text):
            if float(m.group(1)) > 5:
                problems.append(f"{css_name}: border-radius {m.group(1)}px > 5px")
                break
        weights = {int(w) for w in WEIGHT.findall(text)}
        bad_w = sorted(w for w in weights if w not in (400, 500, 600, 700))
        if bad_w:
            problems.append(f"{css_name}: 非法 font-weight {bad_w}")
        for m in re.finditer(r"letter-spacing:\s*([\d.]+)em", text):
            if float(m.group(1)) > 0.05:
                problems.append(f"{css_name}: letter-spacing {m.group(1)}em > .05em（中文禁大字距）")
                break

    # 5. 字体文件存在
    for m in re.finditer(r'url\("\./fonts/([^"]+)"\)', fonts_css):
        p = UI / "fonts" / m.group(1)
        if not p.is_file():
            problems.append(f"字体文件缺失: {p}")
    woff2 = list((UI / "fonts").glob("*.woff2"))
    sizes = sum(f.stat().st_size for f in woff2)
    print(f"字体文件: {len(woff2)} 个，共 {sizes/1024:.0f} KB")

    # 6. 背景：禁止全屏可平铺纹理（Phase 4 回归约束）
    #    旧版三层里的「28px 重复横格」与「feTurbulence 噪点」在任意窗口尺寸下都会
    #    tiling，全屏周期读作扫描件伪影。这里把「不许再出现」变成可回归断言：
    #    只要有人在声明里写回重复渐变或噪点滤镜，校验就红。
    #    匹配在 strip_comments 之后做——注释里提到这些名字是合法的（文档要能解释历史）。
    plain = strip_comments(new_css)
    for needle, why in (
        ("repeating-linear-gradient", "可平铺的重复渐变（旧版账页横格）"),
        ("feTurbulence", "SVG 噪点滤镜（旧版纸纤维）"),
        ("fractalNoise", "SVG 噪点滤镜（旧版纸纤维）"),
    ):
        if needle in plain:
            problems.append(f"背景出现 {needle}：{why}，在任意窗口下都会 tiling")

    # body 必是「三束单层渐变 + 显式尺寸 + 不平铺」。这三件事缺一不可：
    # 少了 size 光落点会随页面长短漂移；加了 repeat 光场会退化成贴图。
    body_rule = re.search(r"(?:^|\})\s*body\s*\{([^{}]*)\}", plain)
    if not body_rule:
        problems.append("找不到 body 规则，无法校验背景图层")
    else:
        decls = body_rule.group(1)
        image = re.search(r"background-image\s*:\s*([^;]+);", decls)
        if not image:
            problems.append("body 缺少 background-image（分层光场）")
        else:
            value = image.group(1)
            if "url(" in value:
                problems.append("body 的 background-image 不得含 url()（位图无法约束平铺）")
            for token in ("--field-glow", "--field-settle", "--field-warm"):
                if token not in value:
                    problems.append(f"body 的 background-image 缺少 {token}")
        repeat = re.search(r"background-repeat\s*:\s*([^;]+);", decls)
        if not repeat:
            problems.append("body 缺少 background-repeat: no-repeat")
        elif "repeat" in repeat.group(1).replace("no-repeat", ""):
            problems.append("body 的 background-repeat 含平铺值（光场会退化成贴图）")
        if "background-size" not in decls:
            problems.append("body 缺少 background-size：光落点会随页面长短漂移")

    # 7. daily.css 不得定义颜色 token
    daily = strip_comments(read(DAILY_CSS))
    if re.search(r":root\s*\{", daily):
        problems.append("daily.css 仍然重新定义 :root（规范 §8.2 明令禁止）")

    if problems:
        print(f"\n发现 {len(problems)} 个问题:")
        for p in problems:
            print("  -", p)
        return 1
    print(
        "\nCSS 校验全部通过（styles / daily / models / fonts）：语法 / 选择器守恒 / token / "
        "反模式 / 字体文件 / 背景无平铺纹理 / daily.css 无 token"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
