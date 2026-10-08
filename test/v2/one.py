import sys, os
sys.path.insert(0, '/app')
from docfmt import convert
src, se, de, work, out = sys.argv[1:6]
os.makedirs(work, exist_ok=True)
p = convert(src, se, de, work)
import shutil; shutil.copyfile(p, out)
print(os.path.basename(p))
