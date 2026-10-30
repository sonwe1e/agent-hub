#!/usr/bin/env python3
"""生成 Agent Hub 的标记文件（favicon / ICO / PNG / 对比接触表）。

两套拓扑
--------
`slots`（默认，variant A）—— **负空间开槽**
    墨色圆角印版是主体轮廓，骨白只是从版面上挖走的槽：
    三条等宽竖槽从近顶部向版内挖，槽不到底（底部留下一段厚墨＝壳体），
    每条槽末端是一个**与槽同宽**的圆孔（＝端子孔，不是加宽的脚）。
    语义：连接器 / 端子排的正面——壳体 + 三个插槽 + 三个孔。

    与最初设想的一处偏离：槽**没有直接开口在上边缘**，而是在上方留了一道
    窄唇（lip）。实测留唇是必需的——槽一旦开到顶边，16–24px 下顶边会碎成
    四根细墨齿，"方块主体"的读法就丢了。留唇之后方块轮廓始终完整，
    这恰好是 A 版要解决的问题。

`posts`（variant B，保留供对比）—— 母线三柱
    一条横向母线 + 三根向下垂的接线柱 + 柱端焊盘。
    **已判为不通过**：三条等宽等长平行的腿穿出到负空间，是家具剪影的定义特征，
    在 32px 以下被读成"一张桌子"。保留它只是为了留一份可对比的实物证据。

A 为什么能解决问题：主体剪影退化成一个**方块**，没有任何东西穿出边界，
"桌子/凳子"的读法从拓扑上消失，而不是靠调比例去掩盖。

两套拓扑共用同一块印版（同圆角、同墨色、满幅出血）。

提示档（hinting）
----------------
位图不是把 1024 缩小，而是**按目标尺寸各自重画**。三档只在"墨与白的配比"上分叉，
轮廓（圆角、槽数、唇、底带位置）在所有尺寸上一致：

    micro   16, 20 px   槽 148 / 壁 178（槽最宽，AA 后不发灰）、唇 140、底带 310
    compact 24, 32, 40  槽 112 / 壁 210（墨开始压过白）、唇 130、底带 292
    full    48 px 起    槽 100 / 壁 220（墨明显主导）、唇 118、底带 280

孔一律取「与槽同宽」（bore = 1.0）。试过 1.3 / 1.55 / 1.75 倍：孔径一放大，
槽末端就长出一个球头，48px 以上读作"棒棒糖 / 勺子"，反而更差。

颜色只取品牌墨与骨白：墨 #1E1C19 / 骨白 #F7F3EB。不用朱砂——按
docs/design/visual-direction.md，朱砂只用于「此刻唯一在发生的事」；
图元是身份而不是状态。

已知取舍（刻意不做）：深色任务栏下印版 #1E1C19 会并入背景（对比度 1.00–1.04:1），
标记退化成三根亮条；当前用户环境 AppsUseLightTheme=1 / SystemUsesLightTheme=1，
任务栏是浅色，所以**不加外描边**——描边会在浅底上引入一道原本不存在的 1px 灰边，
为了一个不存在的场景付出可见代价。

产物
----
    ui/favicon.svg                  矢量（slot 档，16–32px 渲染优化）
    ui/agent-hub.ico                16/20/24/32/40/48/64/128/256 九档
    ui/icon/agent-hub-256.png
    ui/icon/agent-hub-1024.png
    ui/icon/compare-icon.png        A/B 对比接触表（给用户看，入库）

自检用的放大图（16/24/32 各放大 8 倍，最近邻）写到**系统临时目录**，不入库——
它们每次执行都会重生，属于一次性自检产物。路径会打印在 stdout：
    <tempdir>/agenthub-icon-zoom-16.png 等

用法（构建用的隔离环境，已装 Pillow）：
    .venv-build/Scripts/python.exe scripts/make_icon.py

脚本是纯确定性的：无随机、无时间戳、无个人绝对路径，重复执行字节一致。
"""
from __future__ import annotations

import io
import struct
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- 固定路径

ROOT = Path(__file__).resolve().parent.parent
UI_DIR = ROOT / "ui"
ICON_DIR = UI_DIR / "icon"
SVG_PATH = UI_DIR / "favicon.svg"
ICO_PATH = UI_DIR / "agent-hub.ico"
COMPARE_PATH = ROOT / "docs" / "design" / "icon-compare.png"

# ---------------------------------------------------------------- 设计常量

GRID = 1024                     # 绘制网格（所有几何都在这个坐标系里定义）
INK_HEX = "#1E1C19"             # 墨：印版底色
PAPER_HEX = "#F7F3EB"           # 骨白：图元色（= 侧栏 brand-mark 的 color）
PLATE_RADIUS = 100              # 印版圆角 ≈ 9.8%，读作"近方角的印版"而不是药丸
OVERLAP = 2                     # 形状重叠量，消除亚像素级接缝

ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
PNG_SIZES = (256, 1024)
ZOOM_SIZES = (16, 24, 32)       # 自检用：这几档放大 8 倍逐张看
ZOOM_FACTOR = 8

DEFAULT_TOPOLOGY = "slots"
TOPOLOGIES = ("slots", "posts")

# 每档提示参数（全部是 1024 网格单位）
#
# slots: slot 槽宽 / gap 槽间壁厚 / lip 顶部留唇（0 = 槽直接开口在上边缘）
#        bore 孔径与槽宽之比（0 = 不做孔） / band 底部保留的墨带厚度
# posts: bus 母线厚度 / drop 柱宽 / pitch 柱中心距 / drop_h 柱长 / pads 是否画焊盘
_VARIANTS: dict[str, dict[str, dict[str, object]]] = {
    "slots": {
        "micro": {"slot": 148, "gap": 178, "lip": 140, "bore": 1.0, "band": 310},
        "compact": {"slot": 112, "gap": 210, "lip": 130, "bore": 1.0, "band": 292},
        "full": {"slot": 100, "gap": 220, "lip": 118, "bore": 1.0, "band": 280},
    },
    "posts": {
        "micro": {"bus": 176, "drop": 130, "pitch": 240, "drop_h": 380, "pads": False},
        "compact": {"bus": 150, "drop": 116, "pitch": 226, "drop_h": 350, "pads": False},
        "full": {"bus": 116, "drop": 110, "pitch": 220, "drop_h": 300, "pads": True},
    },
}


def variant_for(size: int) -> str:
    """按目标边长选择提示档。"""
    if size <= 20:
        return "micro"
    if size <= 40:
        return "compact"
    return "full"


def supersample(size: int) -> int:
    """超采样倍率：小图需要更多倍才能画出干净的抗锯齿边缘。"""
    if size <= 64:
        return 8
    if size <= 256:
        return 4
    return 2


def _rgba(hex_color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    """把 #RRGGBB 转成 RGBA 元组。"""
    value = hex_color.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"只接受 #RRGGBB：{hex_color!r}")
    return (
        int(value[0:2], 16),
        int(value[2:4], 16),
        int(value[4:6], 16),
        alpha,
    )


# ---------------------------------------------------------------- 几何

# 形状统一用两种元组表达，渲染器与 SVG 生成器共用：
#   ("rect",   x, y, w, h, radius)
#   ("circle", cx, cy, r)
Shape = tuple


def _shapes_slots(params: dict[str, object]) -> tuple[list[Shape], dict[str, int]]:
    """负空间开槽：三条自上边缘向内的竖槽，槽末端一个圆孔。

    返回形状列表和实测度量（写进报告用）。
    """
    slot = int(params["slot"])          # type: ignore[arg-type]
    gap = int(params["gap"])            # type: ignore[arg-type]
    lip = int(params["lip"])            # type: ignore[arg-type]
    bore = float(params["bore"])        # type: ignore[arg-type]
    band = int(params["band"])          # type: ignore[arg-type]

    margin = (GRID - 3 * slot - 2 * gap) // 2
    if margin <= 0:
        raise ValueError(f"槽宽/壁厚过大，侧边距非正：{margin}")

    diameter = int(round(slot * bore)) if bore > 0 else 0
    if diameter:
        bore_center_y = GRID - band - diameter // 2
        slot_bottom = bore_center_y
    else:
        bore_center_y = 0
        slot_bottom = GRID - band

    # 顶部开口：让矩形探出版面一点，避免边缘出现半像素缝；有唇时从唇线开始
    slot_top = (lip - OVERLAP) if lip > 0 else -OVERLAP
    slot_h = slot_bottom - slot_top

    shapes: list[Shape] = []
    for index in range(3):
        x = margin + index * (slot + gap)
        shapes.append(("rect", x, slot_top, slot, slot_h, 0))
        if diameter:
            shapes.append(("circle", x + slot // 2, bore_center_y, diameter // 2))

    meta = {
        "margin": margin,
        "slot": slot,
        "gap": gap,
        "bore_diameter": diameter,
        "band": band,
        "slot_top": slot_top,
        "bore_center_y": bore_center_y,
    }
    return shapes, meta


def _shapes_posts(params: dict[str, object]) -> tuple[list[Shape], dict[str, int]]:
    """母线三柱（variant B，已判为不通过，仅保留作对比）。"""
    bus_t = int(params["bus"])          # type: ignore[arg-type]
    drop_w = int(params["drop"])        # type: ignore[arg-type]
    pitch = int(params["pitch"])        # type: ignore[arg-type]
    drop_h = int(params["drop_h"])      # type: ignore[arg-type]
    has_pads = bool(params["pads"])

    pad = int(round(drop_w * 1.25)) if has_pads else 0
    pad_radius = int(round(drop_w * 0.16)) if has_pads else 0

    glyph_h = bus_t + drop_h + pad // 2
    top = (GRID - glyph_h) // 2
    bus_bottom = top + bus_t

    overhang = int(round(drop_w * 0.55))
    span = 2 * pitch + drop_w + 2 * overhang
    x0 = (GRID - span) // 2
    center_x = x0 + span // 2
    centers = [center_x + offset * pitch for offset in (-1, 0, 1)]

    shapes: list[Shape] = [("rect", x0, top, span, bus_t, 0)]
    for cx in centers:
        shapes.append(
            ("rect", cx - drop_w // 2, bus_bottom - OVERLAP, drop_w, drop_h + OVERLAP, 0)
        )
    if has_pads:
        pad_center_y = bus_bottom + drop_h
        for cx in centers:
            shapes.append(
                ("rect", cx - pad // 2, pad_center_y - pad // 2, pad, pad, pad_radius)
            )

    meta = {"span": span, "glyph_h": glyph_h, "pad": pad}
    return shapes, meta


def layout(
    topology: str = DEFAULT_TOPOLOGY,
    variant: str | None = None,
    size: int | None = None,
) -> dict[str, object]:
    """返回某套拓扑在某档位下的完整几何（1024 网格）。"""
    if topology not in _VARIANTS:
        raise ValueError(f"未知拓扑：{topology!r}（可选 {TOPOLOGIES}）")
    chosen = variant or variant_for(size if size is not None else 256)
    params = _VARIANTS[topology].get(chosen)
    if params is None:
        raise ValueError(f"拓扑 {topology} 没有档位 {chosen}")

    if topology == "slots":
        shapes, meta = _shapes_slots(params)
    else:
        shapes, meta = _shapes_posts(params)

    return {
        "topology": topology,
        "variant": chosen,
        "radius": PLATE_RADIUS,
        "shapes": shapes,
        "meta": meta,
    }


# ---------------------------------------------------------------- 位图渲染

def _draw_shape(draw: ImageDraw.ImageDraw, shape: Shape, scale: float, fill) -> None:
    """把一个形状按比例画到画布上。"""
    if shape[0] == "rect":
        _, x, y, w, h, radius = shape
        box = [
            round(x * scale),
            round(y * scale),
            round((x + w) * scale) - 1,
            round((y + h) * scale) - 1,
        ]
        if radius:
            draw.rounded_rectangle(box, radius=radius * scale, fill=fill)
        else:
            draw.rectangle(box, fill=fill)
    elif shape[0] == "circle":
        _, cx, cy, r = shape
        draw.ellipse(
            [
                round((cx - r) * scale),
                round((cy - r) * scale),
                round((cx + r) * scale) - 1,
                round((cy + r) * scale) - 1,
            ],
            fill=fill,
        )
    else:
        raise ValueError(f"未知形状：{shape[0]!r}")


def render(size: int, topology: str = DEFAULT_TOPOLOGY) -> Image.Image:
    """在给定边长上重画标记（超采样后降采样，得到干净边缘）。"""
    if size < 8:
        raise ValueError(f"尺寸过小：{size}")
    lay = layout(topology=topology, size=size)

    factor = supersample(size)
    canvas = size * factor
    scale = canvas / GRID

    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # 墨色印版：圆角方块，满幅出血（与侧栏 brand-mark 一样是"压印"而不是"贴纸"）
    draw.rounded_rectangle(
        [0, 0, canvas - 1, canvas - 1],
        radius=int(lay["radius"]) * scale,  # type: ignore[arg-type]
        fill=_rgba(INK_HEX),
    )
    for shape in lay["shapes"]:  # type: ignore[union-attr]
        _draw_shape(draw, shape, scale, _rgba(PAPER_HEX))

    if factor > 1:
        image = image.resize((size, size), Image.LANCZOS)
    return image


def png_bytes(image: Image.Image) -> bytes:
    """确定性地编码成 PNG（不带 tIME 等元数据）。"""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# ---------------------------------------------------------------- ICO 容器

def write_ico(
    path: Path,
    sizes: tuple[int, ...] = ICO_SIZES,
    topology: str = DEFAULT_TOPOLOGY,
) -> list[tuple[int, int]]:
    """手工封装 ICO：每档都是独立渲染的 PNG 负载。

    不用 Pillow 的 `save(format="ICO", sizes=...)`，因为它只会把最大那张
    逐级缩小——那正是旧图标"发虚"的原因（7 档全是 40px 原图的上采样）。
    """
    payloads: list[tuple[int, bytes]] = []
    for size in sizes:
        payloads.append((size, png_bytes(render(size, topology))))

    count = len(payloads)
    header = struct.pack("<HHH", 0, 1, count)          # reserved / type=1 / count
    data_offset = 6 + 16 * count

    entries = bytearray()
    blobs = bytearray()
    for size, payload in payloads:
        dimension = 0 if size >= 256 else size        # 256 在字节字段里写作 0
        entries += struct.pack(
            "<BBBBHHII",
            dimension,          # width
            dimension,          # height
            0,                  # palette size
            0,                  # reserved
            1,                  # colour planes
            32,                 # bits per pixel
            len(payload),       # bytes in resource
            data_offset + len(blobs),
        )
        blobs += payload

    path.write_bytes(header + bytes(entries) + bytes(blobs))
    return [(size, len(payload)) for size, payload in payloads]


def audit_ico(path: Path) -> list[tuple[int, int, int, int]]:
    """直接解析 ICO 目录，返回 [(边长, 位深, 字节数, 偏移)]。"""
    data = path.read_bytes()
    if len(data) < 6:
        raise ValueError("ICO 文件过短")
    reserved, kind, count = struct.unpack("<HHH", data[0:6])
    if reserved != 0 or kind != 1:
        raise ValueError(f"ICO 头不合法：reserved={reserved} type={kind}")

    rows: list[tuple[int, int, int, int]] = []
    for index in range(count):
        start = 6 + 16 * index
        width, height, _colors, _res, _planes, bits, length, offset = struct.unpack(
            "<BBBBHHII", data[start : start + 16]
        )
        if width != height:
            raise ValueError(f"第 {index} 档不是正方形：{width}x{height}")
        side = 256 if width == 0 else width
        if data[offset : offset + 8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError(f"第 {index} 档（{side}px）不是 PNG 负载")
        if offset + length > len(data):
            raise ValueError(f"第 {index} 档（{side}px）负载越界")
        rows.append((side, bits, length, offset))
    return rows


# ---------------------------------------------------------------- SVG

def build_svg(topology: str = DEFAULT_TOPOLOGY, variant: str = "compact") -> str:
    """从同一份几何生成 favicon.svg。

    SVG 的实际渲染环境是 16–32px 的标签页，所以默认用 compact 档
    （槽更宽、孔比 full 档略小，避免在小尺寸糊成点）。
    """
    lay = layout(topology=topology, variant=variant)
    parts: list[str] = []
    for shape in lay["shapes"]:  # type: ignore[union-attr]
        if shape[0] == "rect":
            _, x, y, w, h, radius = shape
            # 位图里的 OVERLAP 与负坐标只为消除抗锯齿接缝；矢量不需要
            if y < 0:
                h += y
                y = 0
            attrs = f'x="{x}" y="{y}" width="{w}" height="{h}"'
            if radius:
                attrs += f' rx="{radius}"'
            parts.append(f"  <rect {attrs}/>")
        else:
            _, cx, cy, r = shape
            parts.append(f'  <circle cx="{cx}" cy="{cy}" r="{r}"/>')
    body = "\n".join(parts)
    return (
        "<!-- 由 scripts/make_icon.py 生成，请勿手改。\n"
        "     墨色近方角印版 + 骨白负空间开槽：三条等宽竖槽从近顶部向版内挖，\n"
        "     槽末端一个同宽的圆孔，下方留一段厚墨带＝壳体。语义是端子排的正面。\n"
        "     槽上方保留一道窄唇（lip）——不留唇的话，16–24px 下顶边会碎成四根墨齿。\n"
        f"     topology = {topology} / geometry = {variant}（按 16–32px 标签页优化） -->\n"
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {GRID} {GRID}"\n'
        f'     fill="{PAPER_HEX}" role="img" aria-label="Agent Hub">\n'
        f'  <rect width="{GRID}" height="{GRID}" rx="{lay["radius"]}" fill="{INK_HEX}"/>\n'
        f"{body}\n"
        "</svg>\n"
    )


# ---------------------------------------------------------------- 对比接触表

def _label_font(size: int) -> ImageFont.ImageFont:
    """优先用 Pillow 内置的可缩放默认字体，失败则退回位图默认字体。"""
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                                    # Pillow < 10
        return ImageFont.load_default()


def build_compare_sheet(path: Path) -> tuple[int, int]:
    """A/B 两套拓扑的对比接触表：2 行 × 4 列 + 每行右侧一个 256 缩略。

    背景用 --paper #F7F3EB：图标绝大多数时间出现在浅色环境里，
    放在透明或深色底上看会误判对比度。
    """
    columns = (16, 24, 32, 64)
    cell_w, cell_h = 96, 256
    thumb = 256
    pad = 24
    label_h = 30
    row_gap = 18

    width = pad * 2 + cell_w * len(columns) + thumb + pad
    height = pad * 2 + label_h + (cell_h + label_h) * 2 + row_gap
    sheet = Image.new("RGB", (width, height), _rgba(PAPER_HEX)[:3])
    draw = ImageDraw.Draw(sheet)
    font = _label_font(15)
    tiny = _label_font(13)

    rows = (
        ("A  slots  (negative space, default)", "slots"),
        ("B  posts  (bus + 3 lugs, rejected)", "posts"),
    )

    y = pad
    for title, topology in rows:
        draw.text((pad, y), title, fill=(30, 28, 25), font=font)
        y += label_h
        x = pad
        for size in columns:
            art = render(size, topology)
            # 放大到 1:1 实际尺寸展示（16px 就画 16px，不缩放），
            # 但垂直居中在 col 高的居位格里，便于对齐比较
            sheet.paste(art, (x + (cell_w - size) // 2, y + (cell_h - size) // 2 - 20), art)
            text = f"{size}"
            tw = draw.textlength(text, font=tiny)
            draw.text(
                (x + (cell_w - tw) / 2, y + cell_h - 14),
                text,
                fill=(110, 104, 92),
                font=tiny,
            )
            x += cell_w
        big = render(thumb, topology)
        sheet.paste(big, (x, y + (cell_h - thumb) // 2 - 20), big)
        tw = draw.textlength("256", font=tiny)
        draw.text((x + (thumb - tw) / 2, y + cell_h - 14), "256", fill=(110, 104, 92), font=tiny)
        y += cell_h + label_h + row_gap

    sheet.save(path, format="PNG", optimize=True)
    return width, height


# ---------------------------------------------------------------- 入口

def main() -> int:
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    topology = DEFAULT_TOPOLOGY

    print(f"网格 {GRID}px · 墨 {INK_HEX} · 骨白 {PAPER_HEX} · 默认拓扑 {topology}")
    for name in TOPOLOGIES:
        for variant in ("micro", "compact", "full"):
            lay = layout(topology=name, variant=variant)
            metrics = "  ".join(f"{k}={v}" for k, v in lay["meta"].items())  # type: ignore[union-attr]
            print(f"  {name:<5} {variant:<8} 形状 {len(lay['shapes']):>2}  {metrics}")  # type: ignore[arg-type]

    print(f"\n按 {topology} 产出：")
    written: list[tuple[Path, int]] = []

    for size in PNG_SIZES:
        payload = png_bytes(render(size, topology))
        target = ICON_DIR / f"agent-hub-{size}.png"
        target.write_bytes(payload)
        written.append((target, len(payload)))
        print(f"  PNG {size:>4}px  {len(payload):>8,} B  ({variant_for(size)} 档)")

    svg_text = build_svg(topology, "compact")
    SVG_PATH.write_text(svg_text, encoding="utf-8")
    written.append((SVG_PATH, len(svg_text.encode("utf-8"))))
    print(f"  SVG          {len(svg_text.encode('utf-8')):>8,} B")

    rows = write_ico(ICO_PATH, ICO_SIZES, topology)
    print(f"  ICO {ICO_PATH.name}：{len(rows)} 档")
    for size, length in rows:
        print(f"    {size:>3}px  {length:>7,} B")

    audited = audit_ico(ICO_PATH)
    sizes_seen = tuple(row[0] for row in audited)
    if sizes_seen != ICO_SIZES:
        print(f"FAIL: ICO 档位不符，期望 {ICO_SIZES}，实际 {sizes_seen}", file=sys.stderr)
        return 1
    if any(row[1] != 32 for row in audited):
        print("FAIL: 存在非 32 位色的档位", file=sys.stderr)
        return 1
    if ICO_PATH.stat().st_size != 6 + 16 * len(audited) + sum(row[2] for row in audited):
        print("FAIL: ICO 体积与目录不符", file=sys.stderr)
        return 1
    print("  ICO 自检通过：档位 / 位深 / 负载偏移 / 体积一致")

    size_w, size_h = build_compare_sheet(COMPARE_PATH)
    print(f"  对比接触表   {COMPARE_PATH.name}  {size_w}x{size_h}")

    for size in ZOOM_SIZES:
        art = render(size, topology)
        zoom_path = Path(tempfile.gettempdir()) / f"agenthub-icon-zoom-{size}.png"
        art.resize((size * ZOOM_FACTOR, size * ZOOM_FACTOR), Image.NEAREST).save(
            zoom_path, format="PNG", optimize=True
        )
        print(f"  自检放大图   {zoom_path}  ({size}x{ZOOM_FACTOR}，临时目录，不入库)")

    print()
    for path, length in written:
        print(f"  写出 {path.relative_to(ROOT).as_posix()}  {length:,} B")
    return 0


if __name__ == "__main__":
    sys.exit(main())
