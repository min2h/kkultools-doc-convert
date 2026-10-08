"""자식 프로세스 진입점: docfmt_cli.py <입력> <from> <to> <작업폴더> <결과경로> → 표준 출력에 결과 확장자"""
import os, shutil, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from docfmt import convert, ConvertError

src, se, de, work, out = sys.argv[1:6]
os.makedirs(work, exist_ok=True)
try:
    p = convert(src, se, de, work)
except ConvertError as e:
    print(str(e)[:300], file=sys.stderr); sys.exit(2)
shutil.move(p, out)
print(os.path.splitext(p)[1].lstrip('.').lower() or de)
