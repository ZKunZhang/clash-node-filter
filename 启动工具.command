#!/bin/zsh
cd -- "${0:A:h}" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  print '需要 Python 3，请安装后再运行。'
  read -r '?按回车关闭'
  exit 1
fi
python3 app.py
