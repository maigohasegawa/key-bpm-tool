#!/bin/bash
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then
  python3 key_bpm_app.py
else
  echo "Python 3 が見つかりませんでした。"
  echo "https://www.python.org/downloads/ からインストールしてから、もう一度ダブルクリックしてください。"
  read -n 1 -s -r -p "何かキーを押すと閉じます"
fi
