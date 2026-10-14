"""Theme tests - no device needed.

    python tests/test_theme.py

These exist because the file views once styled only QTreeView. The list and
icon views then took their background from the system palette while their
text colour came from the stylesheet, so on a dark desktop the names were
drawn in dark grey on near-black and could not be read at all.
"""
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from PyQt6.QtWidgets import QApplication, QListView, QTreeView
from PyQt6.QtGui import QColor, QPalette

from ui import theme

app = QApplication(sys.argv)
app.setStyle("Fusion")

PASS, FAIL = [], []


def check(name, condition, detail=''):
    (PASS if condition else FAIL).append(name)
    print(('  PASS  ' if condition else '  FAIL  ') + name + (f'  [{detail}]' if detail else ''))


def relative_luminance(colour):
    def channel(value):
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    return (0.2126 * channel(colour.red())
            + 0.7152 * channel(colour.green())
            + 0.0722 * channel(colour.blue()))


def contrast(one, two):
    lighter, darker = sorted([relative_luminance(one), relative_luminance(two)], reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def use_system_palette(background, foreground):
    """Pretend the desktop is dark, or light, underneath the stylesheet."""
    palette = QPalette()
    for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base,
                 QPalette.ColorRole.Button):
        palette.setColor(role, QColor(background))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText):
        palette.setColor(role, QColor(foreground))
    app.setPalette(palette)


print("\n=== 1. dark is the default ===")
check("the default theme is dark", theme.DEFAULT_DARK is True)
check("stylesheet() returns the dark sheet by default",
      theme.stylesheet(None) is theme.DARK or theme.stylesheet() is theme.DARK)
check("light is still reachable", theme.stylesheet(False) is theme.LIGHT)

print("\n=== 2. every file view is readable, whatever the desktop looks like ===")
MINIMUM = 4.5        # the usual threshold for body text
for desktop, background, foreground in (("dark desktop", "#1b1b1b", "#f0f0f0"),
                                        ("light desktop", "#ffffff", "#000000")):
    use_system_palette(background, foreground)
    for theme_name, dark in (("dark theme", True), ("light theme", False)):
        app.setStyleSheet(theme.stylesheet(dark))
        for view_class in (QTreeView, QListView):
            view = view_class()
            view.show()
            view.ensurePolished()
            text = view.palette().color(QPalette.ColorRole.Text)
            base = view.palette().color(QPalette.ColorRole.Base)
            ratio = contrast(text, base)
            check(f"{view_class.__name__} on a {desktop} with the {theme_name}",
                  ratio >= MINIMUM,
                  f"{text.name()} on {base.name()} = {ratio:.1f}:1")
            view.deleteLater()

print("\n=== 3. both sheets style the views explicitly ===")
for name, sheet in (("LIGHT", theme.LIGHT), ("DARK", theme.DARK)):
    check(f"{name} styles QListView", "QListView {" in sheet)
    check(f"{name} gives QListView items a colour",
          "QListView::item {" in sheet and "color:" in sheet.split("QListView::item {")[1][:120])
    check(f"{name} gives QTreeView items a colour",
          "QTreeView::item {" in sheet and "color:" in sheet.split("QTreeView::item {")[1][:120])

print("\n=== 4. the choice survives a restart ===")
original = theme.load_dark()
theme.save_dark(False)
check("a light choice is remembered", theme.load_dark() is False)
theme.save_dark(True)
check("a dark choice is remembered", theme.load_dark() is True)
theme.save_dark(original)

print(f"\n{'=' * 60}\nPASSED {len(PASS)}  FAILED {len(FAIL)}")
if FAIL:
    print("FAILURES:")
    for name in FAIL:
        print("  -", name)
sys.exit(1 if FAIL else 0)
