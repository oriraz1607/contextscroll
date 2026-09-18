#!/usr/bin/env python3
"""Render illustrative README demos; requires Pillow, PyGObject, Rsvg and Cairo.

Run from any directory with: python3 docs/demos/generate.py
These are scripted illustrations, not desktop recordings.
"""
from io import BytesIO
from pathlib import Path

import cairo
import gi
from PIL import Image, ImageDraw, ImageFont

gi.require_version('Rsvg', '2.0')
from gi.repository import Rsvg

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
FONT = '/usr/share/fonts/google-noto-vf/NotoSans[wght].ttf'
W, H, FPS = 800, 480, 12
BG, PANEL, INK, MUTED, ACCENT = '#101820', '#f7f9fb', '#192b3a', '#71808d', '#63e6bd'


def font(size):
    return ImageFont.truetype(FONT, size)


def text(draw, xy, value, size=16, fill=INK):
    draw.text(xy, value, font=font(size), fill=fill)


def cursor(name):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 36, 36)
    handle = Rsvg.Handle.new_from_file(str(ROOT / 'gnome-extension/icons' / name))
    viewport = Rsvg.Rectangle()
    viewport.x, viewport.y, viewport.width, viewport.height = 0, 0, 36, 36
    handle.render_document(cairo.Context(surface), viewport)
    data = BytesIO()
    surface.write_to_png(data)
    data.seek(0)
    return Image.open(data).convert('RGBA')


NEUTRAL = cursor('autoscroll-cursor.svg')
UP = cursor('autoscroll-direction.svg')
DOWN = UP.transpose(Image.Transpose.ROTATE_180)


def pointer(draw, x, y):
    draw.polygon([(x, y), (x, y + 24), (x + 6, y + 18), (x + 11, y + 29),
                  (x + 16, y + 26), (x + 11, y + 16), (x + 21, y + 16)],
                 fill='white', outline=INK, width=2)


def pulse(draw, x, y, phase):
    if 0 <= phase < 1:
        r = int(12 + phase * 22)
        draw.ellipse((x-r, y-r, x+r, y+r), outline='#15a67e', width=3)


def base(title, caption, detail, extra_tab=False):
    im = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(im)
    text(d, (28, 17), 'ContextScroll', 22, 'white')
    text(d, (560, 23), 'ANIMATED ILLUSTRATION', 12, ACCENT)
    d.rounded_rectangle((24, 65, 776, 381), radius=14, fill=PANEL)
    d.rounded_rectangle((24, 65, 776, 112), radius=14, fill='#dfe6ed')
    d.rectangle((24, 95, 776, 112), fill='#dfe6ed')
    d.rounded_rectangle((42, 76, 259, 112), radius=8, fill=PANEL)
    text(d, (56, 82), 'Reading room', 15)
    text(d, (233, 81), '×', 18)
    if extra_tab:
        d.rounded_rectangle((264, 76, 492, 109), radius=8, fill='#c4d6e0')
        text(d, (278, 82), 'Trail guide', 15)
        text(d, (465, 81), '×', 18)
    text(d, (49, 120), 'example.local / reading-room', 12, MUTED)
    d.line((24, 147, 776, 147), fill='#dfe6ed', width=1)
    text(d, (28, 399), title, 21, ACCENT)
    text(d, (28, 434), caption, 17, 'white')
    text(d, (28, 460), detail, 11, '#9dabb7')
    return im


def content(im, offset):
    page = Image.new('RGB', (714, 800), PANEL)
    d = ImageDraw.Draw(page)
    text(d, (25, 14), 'A little room to explore', 25)
    text(d, (25, 53), 'Long reads. Open trails. Keep moving at your own pace.', 15, MUTED)
    for i, name in enumerate(['The forest path', 'Along the river', 'Up to the ridge', 'The way home']):
        y = 100 + i * 160
        d.rounded_rectangle((25, y, 140, y+115), radius=10, fill=['#b9dacb', '#bad4e6', '#ddd0b9', '#cfc9e2'][i])
        d.polygon([(35, y+96), (76, y+27), (128, y+96)], fill='#658e82')
        text(d, (162, y+4), name, 20)
        for j, width in enumerate([427, 385, 412, 300]):
            d.rounded_rectangle((162, y+43+j*16, 162+width, y+48+j*16), radius=2, fill='#d0d9e1')
    im.paste(page.crop((0, int(offset), 714, int(offset)+220)), (43, 151))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((759, 160, 763, 366), radius=2, fill='#e1e7ec')
    y = int(160 + offset / 580 * 148)
    d.rounded_rectangle((759, y, 763, y+54), radius=2, fill='#91a2af')


def scroll_frame(t):
    if t < 1.5:
        title, caption = '01 / Middle-click the page', 'Start on the document body.'
        offset, cy, icon = 0, 238, None
    elif t < 2.5:
        title, caption = '02 / Move down to scroll', 'The replacement cursor follows your mouse.'
        offset, cy, icon = 0, 238, NEUTRAL
    elif t < 5:
        title, caption = '02 / Move down to scroll', 'Move farther from the starting point to scroll faster.'
        p = (t-2.5)/2.5
        offset, cy, icon = 300*p*p, 238+70*min(p*2, 1), DOWN
    elif t < 7.5:
        title, caption = '03 / Move up to reverse', 'The arrow shows the current vertical scroll direction.'
        p = (t-5)/2.5
        offset, cy, icon = 300*(1-p), 195, UP
    else:
        title, caption = '04 / Click to stop', 'The normal pointer returns where you moved it.'
        offset, cy, icon = 0, 195, None
    im = base(title, caption, 'Toggle-mode illustration · Uses the GNOME extension’s cursor artwork')
    content(im, offset)
    d = ImageDraw.Draw(im)
    if icon:
        im.paste(icon, (612, int(cy)-18), icon)
    else:
        pointer(d, 630, int(cy))
    pulse(d, 630, int(cy), (t-1.5)*2)
    pulse(d, 630, int(cy), (t-7.5)*2)
    return im


def native_frame(t):
    opened = 2 <= t < 5.5
    if t < 2:
        title, caption = '01 / Middle-click a link', 'Links keep their native middle-click action.'
    elif t < 4:
        title, caption = '02 / Open in a background tab', 'Continue reading without starting autoscroll.'
    elif t < 5.5:
        title, caption = '03 / Middle-click the new tab', 'Browser tabs keep their native behavior, too.'
    else:
        title, caption = '04 / Tab closed', 'The original page stays right where you left it.'
    im = base(title, caption, 'Illustration · Native actions depend on your application and its settings', opened)
    d = ImageDraw.Draw(im)
    text(d, (69, 173), 'Find your next trail', 26)
    text(d, (69, 217), 'Keep the article open while you explore a related link.', 17, MUTED)
    d.rounded_rectangle((69, 261, 725, 343), radius=10, fill='#e5efec')
    text(d, (90, 275), 'Read the trail guide', 21, '#146953')
    d.line((90, 303, 313, 303), fill='#146953', width=1)
    text(d, (90, 311), 'A link with a native middle-click action', 13, MUTED)
    if t < 3:
        x, y = 281, 294
    elif t < 4:
        p = t-3
        x, y = 281+75*p, 294-200*p
    else:
        x, y = 356, 94
    pointer(d, int(x), int(y))
    pulse(d, int(x), int(y), (t-2)*2)
    pulse(d, int(x), int(y), (t-5.5)*2)
    return im


def save(name, render, seconds):
    frames = [render(i/FPS) for i in range(seconds*FPS)]
    # One shared palette avoids color flicker between adjacent GIF frames.
    palette = Image.new('RGB', (W, H*3))
    for i, frame in enumerate([frames[0], frames[len(frames)//2], frames[-1]]):
        palette.paste(frame, (0, i*H))
    palette = palette.quantize(colors=128)
    frames = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    frames[0].save(OUT/name, save_all=True, append_images=frames[1:],
                   duration=round(1000/FPS), loop=0, optimize=True)


if __name__ == '__main__':
    save('autoscroll.gif', scroll_frame, 9)
    save('native-middle-click.gif', native_frame, 7)
