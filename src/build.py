"""src/app.html を src/key_bpm_app.py に埋め込んで、ルートの key_bpm_app.py を作ります。
使い方: python3 src/build.py
"""
from pathlib import Path

here = Path(__file__).resolve().parent
html = (here / "app.html").read_text(encoding="utf-8")
src = (here / "key_bpm_app.py").read_text(encoding="utf-8")
assert "'''" not in html
out = src.replace("APP_HTML = None  # build 時に app.html の内容が入ります", "APP_HTML = r'''" + html + "'''")
(here.parent / "key_bpm_app.py").write_text(out, encoding="utf-8")
print("key_bpm_app.py を更新しました")
