# 꿀툴 문서 변환 서버 v2 (any → any). deploy.sh가 이 파일을 빌드해요 (예전 Gotenberg 버전은 Dockerfile.v1).
#  - 바탕: 지금과 같은 Gotenberg Cloud Run 이미지(LibreOffice 26.8·자바·Noto CJK). Gotenberg 프로그램은 쓰지 않고 app/app.py가 받아요.
#  - 더하는 것: H2Orestart(HWP·HWPX 읽기, v1과 같음) · rhwp(HWP↔HWPX, MIT) · pandoc · pypandoc-hwpx(→HWPX, MIT)
#               pdf2docx(PDF→DOCX, PyMuPDF=AGPL) · pdfplumber(PDF→XLSX, MIT) · poppler-utils · img2pdf · unoserver
FROM gotenberg/gotenberg:8.37.0-libreoffice-cloudrun

ARG H2ORESTART_VERSION=0.7.14
ARG H2ORESTART_SHA256=cbea23bc37861361bbc534bc0675e5bc67b36f712072490f82a9bf410d7c04d8
ARG RHWP_VERSION=0.8.7
ARG RHWP_SHA256=24de2bdaa0b69f86302a27e7fdfb570ad3c53b0ab3a4eac06446adc651dfb988

USER root

# Debian 미러(특히 backports)가 가끔 500을 내요 → 받기·설치를 최대 5번까지 다시 시도해요.
RUN set -e; export DEBIAN_FRONTEND=noninteractive; \
    printf 'Acquire::Retries "5";\nAcquire::http::Timeout "60";\n' >/etc/apt/apt.conf.d/80kkul-retries; \
    for i in 1 2 3 4 5; do \
      if apt-get update -qq \
         && apt-get install -y -qq --no-install-recommends -t trixie-backports libreoffice-java-common \
         && apt-get install -y -qq --no-install-recommends \
              fonts-nanum fonts-nanum-extra fonts-unfonts-core python3-venv pandoc poppler-utils; then ok=1; break; fi; \
      echo "apt 실패 ($i/5) — $((i * 15))초 뒤 다시 시도" >&2; sleep $((i * 15)); \
    done; \
    [ "${ok:-}" = 1 ]; \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

COPY fontconfig/65-kkul-korean.conf /etc/fonts/conf.d/65-kkul-korean.conf
RUN fc-cache -f >/dev/null
COPY libreoffice/kkul-korean.xcd libreoffice/kkul-security.xcd /usr/lib/libreoffice/share/registry/

ENV JAVA_HOME=/opt/java
RUN mkdir -p /opt/h2orestart \
 && curl -fsSL -o /opt/h2orestart/H2Orestart.oxt \
      "https://github.com/ebandal/H2Orestart/releases/download/v${H2ORESTART_VERSION}/H2Orestart.oxt" \
 && echo "${H2ORESTART_SHA256}  /opt/h2orestart/H2Orestart.oxt" | sha256sum -c - \
 && HOME=/tmp/unopkg-home unopkg add --shared --suppress-license /opt/h2orestart/H2Orestart.oxt \
 && HOME=/tmp/unopkg-home unopkg list --shared | grep -q "H2Orestart" \
 && rm -rf /tmp/* /tmp/.[!.]* /var/tmp/*

# rhwp (https://github.com/edwardkim/rhwp, MIT) — 정적 바이너리 하나, 버전·체크섬 고정
RUN curl -fsSL -o /tmp/rhwp.tgz "https://github.com/edwardkim/rhwp/releases/download/v${RHWP_VERSION}/rhwp-v${RHWP_VERSION}-linux-x86_64.tar.gz" \
 && echo "${RHWP_SHA256}  /tmp/rhwp.tgz" | sha256sum -c - \
 && tar -xzf /tmp/rhwp.tgz -C /tmp && install -m 0755 /tmp/rhwp/rhwp /usr/local/bin/rhwp && rm -rf /tmp/*

# 파이썬 도구 (LibreOffice의 python3-uno를 쓰려고 --system-site-packages)
COPY app/requirements.txt /opt/kkul/requirements.txt
RUN python3 -m venv --system-site-packages /opt/venv \
 && /opt/venv/bin/pip install -q --no-cache-dir -r /opt/kkul/requirements.txt \
 && find /opt/venv -name '__pycache__' -prune -exec rm -rf {} +

# H2Orestart 로그(~/.cache/H2Orestart) 막기 — v1과 같음 (요청마다 HOME을 임시 폴더로 바꾸지만 기본 HOME도 막아 둬요)
RUN mkdir -p /home/gotenberg/.cache && chown gotenberg:gotenberg /home/gotenberg/.cache \
 && touch /home/gotenberg/.cache/H2Orestart && chown root:root /home/gotenberg/.cache/H2Orestart && chmod 0444 /home/gotenberg/.cache/H2Orestart

COPY app/ /opt/kkul/app/

# LibreOffice 프로필을 미리 만들어 둬요 (첫 실행 때 확장 등록·설정 캐시를 만드는 몇 초를 빌드 때 끝내요).
# 실행할 때는 이 폴더를 임시 폴더로 복사해서 써요(읽기 전용 그대로 쓰지 않아요). Writer·Calc·Impress·HWP(H2Orestart)를 한 번씩 열어 둬요.
RUN set -e; mkdir -p /opt/kkul/lo-profile /tmp/warm && cd /tmp/warm; \
    printf '# warm\n\nhello 꿀툴\n' >w.md; \
    pandoc w.md -o w.docx; printf 'a,b\n1,2\n' >c.csv; \
    /opt/venv/bin/pypandoc-hwpx w.md -o w.hwpx >/dev/null 2>&1 || true; \
    rhwp convert w.hwpx h.hwp >/dev/null 2>&1 || true; \
    chown -R gotenberg:gotenberg /opt/kkul/lo-profile /tmp/warm; \
    for f in w.docx c.csv h.hwp; do [ -f "$f" ] || continue; \
      su gotenberg -s /bin/sh -c "HOME=/tmp/warm soffice -env:UserInstallation=file:///opt/kkul/lo-profile --headless --norestore --nolockcheck --convert-to pdf --outdir /tmp/warm/out $f" >/dev/null 2>&1 || true; \
    done; \
    ls /tmp/warm/out; [ -f /tmp/warm/out/w.pdf ]; \
    rm -rf /opt/kkul/lo-profile/user/backup /opt/kkul/lo-profile/user/temp /tmp/* /tmp/.[!.]* /var/tmp/*; \
    chown -R root:root /opt/kkul/lo-profile && chmod -R a+rX /opt/kkul/lo-profile

ENV PATH=/opt/venv/bin:$PATH \
    CONVERT_USER=kkul \
    CONVERT_TIMEOUT=100 \
    CONVERT_MAX_BYTES=26214400 \
    LO_RESTART_AFTER=20 \
    LO_PROFILE_SEED=/opt/kkul/lo-profile \
    RHWP_MEM_BYTES=1258291200 \
    PYTHONDONTWRITEBYTECODE=1
USER gotenberg
# 비밀값: Cloud Run --set-secrets "CONVERT_SECRET=doc-convert-secret:latest" (v1의 GOTENBERG_API_BASIC_AUTH_PASSWORD 이름도 읽어요)
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/opt/kkul/app/app.py"]
