#!/usr/bin/env bash
# 꿀툴 문서 변환 서버(v2, any → any · Dockerfile)를 Google Cloud Run(도쿄)에 올려요.
# 예전 Gotenberg 주소(/forms/libreoffice/convert → PDF)도 계속 받아서, Worker를 바꾸기 전에 올려도 괜찮아요.
#
#   PROJECT_ID=my-project ./deploy.sh all       # 처음 한 번: API 켜기 → 이미지 빌드 → 배포 → Worker에 비밀값 넣기 안내
#   PROJECT_ID=my-project ./deploy.sh budget    # 예산 알림 ($5 기준 50%·90%·100%·400%=$20)
#   PROJECT_ID=my-project ./deploy.sh smoke     # 배포된 서버로 실제 변환 한 번 (비밀값은 화면에 안 나와요)
#   PROJECT_ID=my-project ./deploy.sh secret-to-worker   # 비밀값을 Cloudflare Worker 비밀(DOC_CONVERT_SECRET)로 복사
#   단계별: setup | build | deploy | url
#
# 필요한 것: gcloud CLI 로그인(gcloud auth login), 결제 계정이 연결된 프로젝트.
# 비용 상한: 최대 인스턴스 2개 · 동시 처리 1건 · 요청당 120초. (예산 알림은 알림일 뿐 돈을 막지 않아요)
set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID 를 지정해 주세요 (예: PROJECT_ID=kkultools-123 ./deploy.sh all)}"
REGION="${REGION:-asia-northeast1}"          # 도쿄 (개인정보처리방침의 국외 이전 국가 = 일본)
SERVICE="${SERVICE:-kkul-doc-convert}"
REPO="${REPO:-kkul}"
SECRET="${SECRET:-doc-convert-secret}"
SA_NAME="${SA_NAME:-kkul-doc-convert}"
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/doc-convert"
HERE="$(cd "$(dirname "$0")" && pwd)"
G=(gcloud --project "$PROJECT_ID" --quiet)

say() { printf '\n\033[1;34m▶ %s\033[0m\n' "$*"; }

setup() {
  say "API 켜기 (Cloud Run · Artifact Registry · Cloud Build · Secret Manager · Budgets)"
  "${G[@]}" services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
    secretmanager.googleapis.com billingbudgets.googleapis.com cloudbilling.googleapis.com

  say "이미지 저장소 ($REPO, $REGION) — 최근 2개만 남겨 저장 비용을 줄여요"
  if ! "${G[@]}" artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1; then
    "${G[@]}" artifacts repositories create "$REPO" --repository-format docker --location "$REGION" --description "kkultools"
  fi
  local pol; pol="$(mktemp)"
  cat >"$pol" <<'JSON'
[
  {"name": "keep-recent-2", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 2}},
  {"name": "delete-old", "action": {"type": "Delete"}, "condition": {"tagState": "any", "olderThan": "86400s"}}
]
JSON
  "${G[@]}" artifacts repositories set-cleanup-policies "$REPO" --location "$REGION" --policy "$pol" --no-dry-run >/dev/null
  rm -f "$pol"

  say "공유 비밀값 ($SECRET) — 처음 한 번만 무작위로 만들어요 (화면에 나오지 않아요)"
  if ! "${G[@]}" secrets describe "$SECRET" >/dev/null 2>&1; then
    openssl rand -hex 32 | tr -d '\n' | "${G[@]}" secrets create "$SECRET" --data-file=- \
      --replication-policy user-managed --locations "$REGION"
  fi

  say "전용 서비스 계정 ($SA) — 이 비밀값 읽기 권한만 줘요"
  if ! "${G[@]}" iam service-accounts describe "$SA" >/dev/null 2>&1; then
    "${G[@]}" iam service-accounts create "$SA_NAME" --display-name "kkultools doc-convert (Cloud Run)"
  fi
  # 새 서비스 계정은 IAM에 퍼지는 데 몇 초~1분 걸려요("does not exist" 400) → 잠깐씩 기다리며 다시 시도해요.
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if "${G[@]}" secrets add-iam-policy-binding "$SECRET" --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor >/dev/null 2>&1; then return 0; fi
    echo "  서비스 계정이 아직 안 보여요. ${i}0초 뒤 다시 시도해요…" >&2; sleep $((i * 10 > 30 ? 30 : i * 10))
  done
  "${G[@]}" secrets add-iam-policy-binding "$SECRET" --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor >/dev/null
}

build() {
  TAG="$(date +%Y%m%d-%H%M%S)"
  say "이미지 빌드 (Cloud Build) → ${IMAGE_BASE}:${TAG}"
  "${G[@]}" builds submit "$HERE" --region "$REGION" --tag "${IMAGE_BASE}:${TAG}" --timeout 1800s
  echo "${IMAGE_BASE}:${TAG}" >"$HERE/.last-image"
}

deploy() {
  local image="${IMAGE:-$(cat "$HERE/.last-image" 2>/dev/null || true)}"
  [ -n "$image" ] || { echo "이미지가 없어요. 먼저 ./deploy.sh build" >&2; exit 1; }
  say "Cloud Run 배포 ($SERVICE, $REGION) ← $image"
  # --allow-unauthenticated: Cloud Run IAM 대신 기본 인증(공유 비밀값, kkul:<비밀값>)으로 막아요.
  #   환경 변수 이름은 v1(Gotenberg) 그대로 써요 — app.py가 GOTENBERG_API_BASIC_AUTH_PASSWORD 도 읽어요.
  #   Cloudflare Worker에서 IAM 토큰을 만들려면 서비스 계정 키·JWT 서명이 필요해서, 비밀값 한 개가 가장 단순하고 안전해요.
  "${G[@]}" run deploy "$SERVICE" \
    --image "$image" --region "$REGION" --platform managed \
    --service-account "$SA" \
    --allow-unauthenticated --ingress all \
    --port 8080 --cpu 1 --memory 2Gi --cpu-boost \
    --concurrency 1 --min-instances 0 --max-instances 2 --timeout 120 \
    --set-env-vars "GOTENBERG_API_BASIC_AUTH_USERNAME=kkul" \
    --set-secrets "GOTENBERG_API_BASIC_AUTH_PASSWORD=${SECRET}:latest" \
    --labels app=kkultools,part=doc-convert
  url
}

url() {
  local u; u="$("${G[@]}" run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"
  say "서비스 주소"
  echo "$u"
  cat <<TXT

다음 단계 (꿀툴 저장소에서):
  1) 비밀값을 Worker로:  PROJECT_ID=$PROJECT_ID server/doc-convert/deploy.sh secret-to-worker
  2) 주소·Polar ID 넣기:  node scripts/pro-config.mjs --doc-url $u --org <Polar 조직 ID> --checkout <Polar Checkout Link> [--benefit <혜택 ID>] --enable-doc
  3) 커밋하고 main에 push → Workers Builds가 배포해요.
TXT
}

secret_to_worker() {
  say "비밀값을 Cloudflare Worker 비밀 DOC_CONVERT_SECRET 으로 복사 (화면에 나오지 않아요)"
  cd "$HERE/../.."
  # wrangler 4는 Node 22 이상이 필요해요 → 지금 node가 낮으면 npx로 Node 22를 같이 받아 실행해요.
  local wr=(npx -y wrangler@4)
  [ "$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)" -ge 22 ] || wr=(npx -y -p node@22 -p wrangler@4 -- wrangler)
  "${G[@]}" secrets versions access latest --secret "$SECRET" | "${wr[@]}" secret put DOC_CONVERT_SECRET
}

smoke() {
  local u; u="$("${G[@]}" run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"
  local s; s="$("${G[@]}" secrets versions access latest --secret "$SECRET")"
  local f="${1:-$HERE/test/sample.docx}"
  [ -f "$f" ] || { echo "시험 파일이 없어요: $f (python3 test/gen-samples.py 로 만들 수 있어요)" >&2; exit 1; }
  say "인증 없이 → 401 이어야 해요"
  curl -s -o /dev/null -w '%{http_code}\n' -X POST "$u/forms/libreoffice/convert" -F "files=@$f"
  say "변환 시험: $(basename "$f")"
  curl -s -o /tmp/kkul-smoke.pdf -w 'HTTP %{http_code} · %{time_total}s · %{size_download}B\n' \
    -u "kkul:$s" -X POST "$u/forms/libreoffice/convert" -F "files=@$f;filename=document.${f##*.}"
  head -c 5 /tmp/kkul-smoke.pdf; echo
  say "v2 변환 시험: $(basename "$f") → hwpx"
  curl -s -o /tmp/kkul-smoke.hwpx -D /tmp/kkul-smoke.h -w 'HTTP %{http_code} · %{time_total}s · %{size_download}B\n' \
    -u "kkul:$s" -X POST "$u/convert?from=${f##*.}&to=hwpx" -H 'content-type: application/octet-stream' --data-binary "@$f"
  grep -i '^x-kkul-ext' /tmp/kkul-smoke.h || true
  unset s
}

budget() {
  local ba; ba="$("${G[@]}" billing projects describe "$PROJECT_ID" --format 'value(billingAccountName)' | sed 's#billingAccounts/##')"
  [ -n "$ba" ] || { echo "이 프로젝트에 결제 계정이 연결돼 있지 않아요" >&2; exit 1; }
  # 예산 통화는 결제 계정 통화와 같아야 해요. 지정이 없으면 결제 계정 통화를 읽어서 USD면 5, KRW면 7000으로 해요.
  local cur="${BUDGET_CURRENCY:-$(gcloud billing accounts describe "$ba" --format 'value(currencyCode)' 2>/dev/null || true)}"
  cur="${cur:-USD}"
  local def=5; [ "$cur" = KRW ] && def=7000
  local amount="${BUDGET_AMOUNT:-$def}"
  if gcloud billing budgets list --billing-account "$ba" --format 'value(displayName)' 2>/dev/null | grep -qx "kkultools doc-convert"; then
    echo "이미 'kkultools doc-convert' 예산이 있어요 (바꾸려면 콘솔 → 결제 → 예산 및 알림)"; return 0
  fi
  say "예산 알림: $amount $cur (50%·90%·100%, 그리고 400% = 약 \$20) — 결제 계정 관리자 메일로 와요"
  gcloud --quiet billing budgets create --billing-account "$ba" \
    --display-name "kkultools doc-convert" \
    --budget-amount "${amount}${cur}" \
    --filter-projects "projects/$PROJECT_ID" \
    --threshold-rule percent=0.5 --threshold-rule percent=0.9 --threshold-rule percent=1.0 \
    --threshold-rule percent=4.0
}

case "${1:-}" in
  setup) setup ;;
  build) build ;;
  deploy) deploy ;;
  url) url ;;
  smoke) shift; smoke "$@" ;;
  budget) budget ;;
  secret-to-worker) secret_to_worker ;;
  all) setup; build; deploy ;;
  *) sed -n '2,12p' "$0"; exit 1 ;;
esac
