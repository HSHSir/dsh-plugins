#!/usr/bin/env python3
"""重绘 keeper.ico 的小尺寸图层，解决任务栏 / 标题栏图标发糊。

问题
----
原来的 16 / 24 / 32 / 48 四档图层都是把 256px 直接缩放得到的。实测 24px 图层
的 alpha 通道有 48 种取值（0、1、2、3……131、222……255），那是双线性插值的产物。
任务栏在 100% 缩放下正是按 24px 原尺寸取图（蓝色方块 22px ≈ 240/256 × 24），
不做额外缩放，所以插值糊掉的四像素笔画就直接呈现给了眼睛。

两条互相独立的结论
------------------
排查过程中最容易走错的一步是「二值化就好」。实测证明要分开看：

1. **圆角方块必须保留抗锯齿。** 圆角本身就是渐变，硬边化之后 24px 上只剩
   2 行阶梯，看起来就是个歪掉的方角，比糊着更难看。
2. **K 字必须二值化。** 字形是笔画，笔画糊了就是糊了，没有「渐变才对」的说法。
   手绘多边形在 4~7px 上必然歪，所以改用 Segoe UI Bold 渲染后阈值二值化——
   字体自带小尺寸骨架优化，二值化后每根笔画都落在整像素上。

做法
----
- 方块与圆角按 256px 原图量得的比例走（240/256、56/256），用 16×16 超采样
  算覆盖率当 alpha，**不做阈值截断**；
- K 用字体渲染 + NEAREST 缩放 + 阈值 110 二值化，字高取方块的 59%；
- 64px 及以上沿用原图（本来就清晰）。

用法
----
    python scripts/rebuild_icon.py          # 就地重写 keeper.ico
    python scripts/rebuild_icon.py --check  # 只打印各尺寸指标，不写文件
"""

from __future__ import annotations
# 直接运行本文件时，把项目根目录加入 sys.path（无需安装成包）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import numpy as np
from PIL import Image, ImageDraw, ImageFont

# 原图标的设计常量，从 256px 图层量得，按比例换算到各尺寸。
# 256px 图层实测：方块 239px、圆角 56px、K 83×111px、竖笔宽 18px。
BLOCK_RATIO = 240 / 256        # 方块边长占画布比例（四周留白 8/256）
RADIUS_RATIO = 56 / 256        # 圆角半径占画布比例
K_HEIGHT_RATIO = 111 / 239     # K 字高占**方块边长**的比例（不是画布！）
K_STEM_RATIO = 18 / 239        # 竖笔宽占方块边长的比例

BLUE = (53, 116, 240, 255)
WHITE = (255, 255, 255, 255)

REDRAW_MAX = 48                # 这个尺寸及以下重绘，以上沿用原图
SUPERSAMPLE = 16               # 圆角覆盖率采样倍数
K_THRESHOLD = 110              # 字体灰阶转二值的阈值

# 字重按尺寸分级：Bold 在 48px 上竖笔会到 5px（设计值只有 3.3px），明显偏胖；
# Semibold 在各尺寸都最接近原设计的笔画比例，只有 16px 字高不够时才上 Bold
# 补回辨识度。
FONT_BY_SIZE = {
    16: ("C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    24: ("C:/Windows/Fonts/seguisb.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    32: ("C:/Windows/Fonts/seguisb.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    48: ("C:/Windows/Fonts/seguisb.ttf", "C:/Windows/Fonts/arialbd.ttf"),
}


def _load_font(px: int, size: int) -> ImageFont.FreeTypeFont:
    """按目标尺寸挑字重；找不到 Semibold 就退回其余候选。"""
    candidates = FONT_BY_SIZE.get(size, ("C:/Windows/Fonts/seguisb.ttf",
                                         "C:/Windows/Fonts/segoeuib.ttf",
                                         "C:/Windows/Fonts/arialbd.ttf",
                                         "C:/Windows/Fonts/msyhbd.ttc"))
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, px)
    return ImageFont.load_default()


def _corner_coverage(size: int, ss: int = SUPERSAMPLE) -> np.ndarray:
    """圆角矩形的逐像素覆盖率（0~1）。

    返回连续的覆盖率而不是 0/1，是为了让圆角在 16~48px 上依然是**圆**的。
    """
    block = size * BLOCK_RATIO
    radius = size * RADIUS_RATIO
    off = (size - block) / 2.0
    x0 = y0 = off
    x1 = y1 = off + block

    ys, xs = np.mgrid[0:size * ss, 0:size * ss]
    fx = (xs + 0.5) / ss
    fy = (ys + 0.5) / ss

    # 到圆角圆心的距离分量：只有落在角区内才非零
    dx = np.maximum(np.maximum(x0 + radius - fx, fx - (x1 - radius)), 0.0)
    dy = np.maximum(np.maximum(y0 + radius - fy, fy - (y1 - radius)), 0.0)
    inside = (dx * dx + dy * dy <= radius * radius + 1e-9)
    inside &= (fx >= x0) & (fx <= x1) & (fy >= y0) & (fy <= y1)

    return inside.reshape(size, ss, size, ss).mean(axis=(1, 3))


def _k_glyph(height: int, size: int, threshold: int = K_THRESHOLD) -> Image.Image | None:
    """渲染一个**二值化**的 K 遮罩，高度等于 height。

    先按 3 倍字号渲染再 NEAREST 缩小，能让每根笔画刚好落进整像素格；
    直接按目标字号渲染的话，笔画会横跨半个像素，二值化后粗细不均。
    """
    if height < 3:
        return None
    probe = Image.new("L", (height * 8, height * 8), 0)
    ImageDraw.Draw(probe).text(
        (height * 4, height * 4), "K", font=_load_font(height * 3, size),
        fill=255, anchor="mm",
    )
    bbox = probe.getbbox()
    if bbox is None:
        return None
    glyph = probe.crop(bbox)
    width = max(1, round(glyph.width * height / glyph.height))
    small = glyph.resize((width, height), Image.NEAREST)
    binary = small.point(lambda v: 255 if v > threshold else 0)
    return binary if binary.getbbox() else None


def draw_small_icon(size: int) -> Image.Image:
    """按目标尺寸重新生成一个 size×size 的图标。"""
    cover = _corner_coverage(size)

    # 直接构造 RGBA 数组，**不要用 paste(..., mask)**：paste 带 mask 时会把
    # RGB 也一起混合（结果是 pre-multiplied alpha）。底色是全透明黑，混完边缘
    # 像素的 RGB 就变成 (2,5,11) 这种暗色，Windows 渲染时圆角会描出一圈暗边。
    # 正确做法是 RGB 恒为纯蓝，只让 alpha 变化。
    rgba = np.zeros((size, size, 4), dtype=np.uint8)
    rgba[..., 0], rgba[..., 1], rgba[..., 2] = BLUE[:3]
    rgba[..., 3] = (cover * 255).round().astype(np.uint8)
    img = Image.fromarray(rgba, mode="RGBA")

    # K 的尺寸照原设计比例走（方块边长的 46.4%），不做「小尺寸放大」——
    # 放大到 52~59% 会让 24px 上的 K 占比过大，和 256px 的原图明显不同。
    block = size * BLOCK_RATIO
    k_h = max(3, round(block * K_HEIGHT_RATIO))
    glyph = _k_glyph(k_h, size)
    if glyph is None:
        return img
    gx = round((size - glyph.width) / 2)
    gy = round((size - k_h) / 2)
    # glyph 是二值遮罩（只有 0/255），这里 paste 不会产生预乘问题——
    # 白色本身没有色相，混合前后 RGB 一样是白。
    img.paste(Image.new("RGBA", glyph.size, WHITE), (gx, gy), glyph)
    return img


def _metrics(size: int, img: Image.Image) -> str:
    """打一行体检数据：方块铺开多少、K 多大、边缘还有多少灰阶。"""
    alpha = np.array(img.split()[3])
    solid = alpha > 250
    if not solid.any():
        return f"{size:>2}px: 空图"
    ys, xs = np.nonzero(solid)
    side = max(xs.max() - xs.min() + 1, ys.max() - ys.min() + 1)

    rgb = np.array(img.convert("RGB")).astype(int)
    white = (rgb[..., 0] > 170) & (rgb[..., 1] > 170) & solid
    if white.any():
        wy, wx = np.nonzero(white)
        k_desc = f"{wx.max() - wx.min() + 1}x{wy.max() - wy.min() + 1}"
    else:
        k_desc = "无"

    levels = len(np.unique(alpha))
    return f"{size:>2}px: 方块={side} K={k_desc} alpha档位={levels}"


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[1] / "modelkeeper" / "assets"
    ico = root / "keeper.ico"
    if not ico.exists():
        print(f"找不到 {ico}")
        return 1

    check_only = "--check" in argv
    src = Image.open(ico)
    sizes = sorted(s for (s, _h) in (src.info.get("sizes") or [(16, 16)]))

    layers: dict[int, Image.Image] = {}
    for size in sizes:
        frame = Image.open(ico)
        frame.size = (size, size)
        if size <= REDRAW_MAX:
            layers[size] = draw_small_icon(size)
            tag = "重绘"
        else:
            layers[size] = frame.convert("RGBA")
            tag = "沿用"
        print(f"  {size:>3}x{size:<3} {tag}  {_metrics(size, layers[size])}")

    if check_only:
        return 0

    biggest = max(layers)
    layers[biggest].save(
        ico, format="ICO",
        sizes=[(s, s) for s in sorted(layers)],
        append_images=[layers[s] for s in sorted(layers) if s != biggest],
    )
    print(f"已写回 {ico}（{ico.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
