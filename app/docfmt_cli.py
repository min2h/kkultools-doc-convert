"""Run one conversion in a child process.

Usage: docfmt_cli.py SRC FROM TO WORKDIR OUT
Prints the extension of the result on stdout.
"""

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from docfmt import ConvertError, convert  # noqa: E402

src, se, de, work, out = sys.argv[1:6]
os.makedirs(work, exist_ok=True)
try:
    p = convert(src, se, de, work)
except ConvertError as e:
    print(str(e)[:300], file=sys.stderr)
    sys.exit(2)
shutil.move(p, out)
print(os.path.splitext(p)[1].lstrip(".").lower() or de)
