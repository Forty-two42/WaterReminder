# -*- coding: utf-8 -*-
"""生成应用图标 water.ico (由 main.py 的绘制函数产出, 保证与托盘图标一致)"""

import os

from main import make_icon_image

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "water.ico")

SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40),
         (48, 48), (64, 64), (128, 128), (256, 256)]


def main():
    img = make_icon_image(256)
    img.save(OUT, format="ICO", sizes=SIZES)
    print("icon written:", OUT, os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()
