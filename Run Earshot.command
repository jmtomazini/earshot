#!/bin/bash
# Double-click to run Earshot and open the report.
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed on this Mac. Install it from python.org and try again."
  read -n 1 -s -r -p "Press any key to close."
  exit 1
fi
python3 earshot.py && open report.html
echo
read -n 1 -s -r -p "Press any key to close."
