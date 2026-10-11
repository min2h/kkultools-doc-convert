"""unoserver launcher with a shorter startup wait.

unoserver 3.7 sleeps a fixed 5 s + 2 s while LibreOffice starts. Polling every
0.5 s instead cuts about 6 s off a cold start; its connect retries still cover
a slow LibreOffice.
"""

import sys
import time as _time
import types

import unoserver.server as S

_sleep = _time.sleep
_fast = types.SimpleNamespace(
    **{k: getattr(_time, k) for k in dir(_time) if not k.startswith("__")}
)
_fast.sleep = lambda s: _sleep(0.5 if s >= 2 else s)
S.time = _fast

if __name__ == "__main__":
    sys.exit(S.main())
