"""unoserver 실행기 — unoserver 3.7은 시작할 때 무조건 5초 + 2초를 쉬어요(LibreOffice가 뜨기를 기다리려고).
그 기다림을 0.5초 간격 확인으로 줄여 콜드 스타트를 약 6초 앞당겨요. 나머지 동작(연결 재시도·변환)은 그대로예요.
(LibreOffice가 늦게 떠도 serve()가 '연결 거부'면 다시 시도해요: 20번 × 0.5초)
"""
import sys
import time as _time
import types

import unoserver.server as S

_sleep = _time.sleep
_fast = types.SimpleNamespace(**{k: getattr(_time, k) for k in dir(_time) if not k.startswith('__')})
_fast.sleep = lambda s: _sleep(0.5 if s >= 2 else s)
S.time = _fast

if __name__ == '__main__':
    sys.exit(S.main())
