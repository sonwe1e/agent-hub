"""生成一次性的开始菜单图标；Pillow 仅用于生成，GUI 启动不依赖它。"""
from pathlib import Path
from PIL import Image, ImageDraw

root = Path(__file__).resolve().parents[1]
scale = 4
image = Image.new("RGBA", (256 * scale, 256 * scale), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)

def box(coords):
    return tuple(int(v * scale) for v in coords)

draw.rounded_rectangle(box((0, 0, 255, 255)), radius=64 * scale, fill="#6254e8")
for rect in ((50, 50, 96, 96), (159, 50, 205, 96), (50, 159, 96, 205)):
    draw.rounded_rectangle(box(rect), radius=11 * scale, outline="white", width=10 * scale)
for points in (((96, 73), (159, 73)), ((73, 96), (73, 159)), ((182, 159), (182, 205)), ((159, 182), (205, 182))):
    draw.line([tuple(v * scale for v in point) for point in points], fill="white", width=10 * scale)
image = image.resize((256, 256), Image.Resampling.LANCZOS)
output = root / "ui/agent-hub.ico"
image.save(output, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(output)
