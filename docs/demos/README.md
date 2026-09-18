# README animations

These scripted illustrations explain ContextScroll's interactions. They are not
screen recordings or evidence of a live desktop session. All sample browser
content is drawn locally; no personal windows or browsing data are captured.
The active cursor is rendered directly from the extension's SVG assets.

- `autoscroll.gif`: toggle activation, downward scrolling, reversal, and stop.
- `native-middle-click.gif`: opening a background tab and closing it.

Regenerate from the repository root:

```sh
python3 docs/demos/generate.py
```

The generator requires Python 3, Pillow, PyGObject, Cairo, librsvg's GI bindings,
and Noto Sans. `FONT` in the script points to Fedora's Noto Sans variable font;
adjust it to your local Noto Sans path on other distributions. No running daemon,
root privileges, or input-device access is needed.

The GIFs are 800 × 480, loop automatically, and use a shared palette per animation.
The README's Use section provides a static alternative. All artwork and generator
code are covered by the repository's MIT license.
