#!/usr/bin/env bash
# Build and deploy the conversion server to Google Cloud Run.
#
#   PROJECT_ID=my-project ./deploy.sh all               # setup + build + deploy
#   PROJECT_ID=my-project ./deploy.sh budget            # budget alerts
#   PROJECT_ID=my-project ./deploy.sh smoke [FILE]      # convert a file on the deployed service
#   PROJECT_ID=my-project ./deploy.sh secret-to-worker  # copy the secret to a Cloudflare Worker
#   Individual steps: setup | build | deploy | url
#
# Requires an authenticated gcloud CLI and a project with billing enabled.
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-asia-northeast1}"
SERVICE="${SERVICE:-kkul-doc-convert}"
REPO="${REPO:-kkul}"
SECRET="${SECRET:-doc-convert-secret}"
SA_NAME="${SA_NAME:-kkul-doc-convert}"
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/doc-convert"
HERE="$(cd "$(dirname "$0")" && pwd)"
G=(gcloud --project "$PROJECT_ID" --quiet)

say() { printf '\n\033[1;34m> %s\033[0m\n' "$*"; }

setup() {
  say "Enabling APIs"
  "${G[@]}" services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
    secretmanager.googleapis.com billingbudgets.googleapis.com cloudbilling.googleapis.com

  say "Artifact Registry repository $REPO ($REGION)"
  if ! "${G[@]}" artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1; then
    "${G[@]}" artifacts repositories create "$REPO" --repository-format docker --location "$REGION" --description "kkultools"
  fi
  local pol
  pol="$(mktemp)"
  cat >"$pol" <<'JSON'
[
  {"name": "keep-recent-2", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 2}},
  {"name": "delete-old", "action": {"type": "Delete"}, "condition": {"tagState": "any", "olderThan": "86400s"}}
]
JSON
  "${G[@]}" artifacts repositories set-cleanup-policies "$REPO" --location "$REGION" --policy "$pol" --no-dry-run >/dev/null
  rm -f "$pol"

  say "Shared secret $SECRET"
  if ! "${G[@]}" secrets describe "$SECRET" >/dev/null 2>&1; then
    openssl rand -hex 32 | tr -d '\n' | "${G[@]}" secrets create "$SECRET" --data-file=- \
      --replication-policy user-managed --locations "$REGION"
  fi

  say "Service account $SA"
  if ! "${G[@]}" iam service-accounts describe "$SA" >/dev/null 2>&1; then
    "${G[@]}" iam service-accounts create "$SA_NAME" --display-name "kkultools doc-convert (Cloud Run)"
  fi
  # A new service account can take up to a minute to become visible to IAM.
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if "${G[@]}" secrets add-iam-policy-binding "$SECRET" --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor >/dev/null 2>&1; then return 0; fi
    echo "  service account not visible yet, retrying..." >&2
    sleep $((i * 10 > 30 ? 30 : i * 10))
  done
  "${G[@]}" secrets add-iam-policy-binding "$SECRET" --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor >/dev/null
}

build() {
  TAG="$(date +%Y%m%d-%H%M%S)"
  say "Cloud Build -> ${IMAGE_BASE}:${TAG}"
  "${G[@]}" builds submit "$HERE" --region "$REGION" --tag "${IMAGE_BASE}:${TAG}" --timeout 1800s
  echo "${IMAGE_BASE}:${TAG}" >"$HERE/.last-image"
}

deploy() {
  local image="${IMAGE:-$(cat "$HERE/.last-image" 2>/dev/null || true)}"
  [ -n "$image" ] || {
    echo "no image; run ./deploy.sh build first" >&2
    exit 1
  }
  say "Deploying $SERVICE ($REGION) <- $image"
  # Public ingress; requests are authenticated by the app with a shared secret
  # rather than Cloud Run IAM, which would need signed tokens from the caller.
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
  say "Service URL"
  "${G[@]}" run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)'
}

secret_to_worker() {
  say "Copying the secret to Worker secret DOC_CONVERT_SECRET"
  cd "$HERE/../.."
  # wrangler 4 needs Node 22+.
  local wr=(npx -y wrangler@4)
  [ "$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)" -ge 22 ] || wr=(npx -y -p node@22 -p wrangler@4 -- wrangler)
  "${G[@]}" secrets versions access latest --secret "$SECRET" | "${wr[@]}" secret put DOC_CONVERT_SECRET
}

smoke() {
  local u s
  u="$("${G[@]}" run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"
  s="$("${G[@]}" secrets versions access latest --secret "$SECRET")"
  local f="${1:-$HERE/test/sample.docx}"
  [ -f "$f" ] || {
    echo "missing test file: $f (see test/gen-samples.py)" >&2
    exit 1
  }
  say "No auth (expect 401)"
  curl -s -o /dev/null -w '%{http_code}\n' -X POST "$u/forms/libreoffice/convert" -F "files=@$f"
  say "Compat endpoint: $(basename "$f") -> pdf"
  curl -s -o /tmp/kkul-smoke.pdf -w 'HTTP %{http_code} %{time_total}s %{size_download}B\n' \
    -u "kkul:$s" -X POST "$u/forms/libreoffice/convert" -F "files=@$f;filename=document.${f##*.}"
  head -c 5 /tmp/kkul-smoke.pdf
  echo
  say "/convert: $(basename "$f") -> hwpx"
  curl -s -o /tmp/kkul-smoke.hwpx -D /tmp/kkul-smoke.h -w 'HTTP %{http_code} %{time_total}s %{size_download}B\n' \
    -u "kkul:$s" -X POST "$u/convert?from=${f##*.}&to=hwpx" -H 'content-type: application/octet-stream' --data-binary "@$f"
  grep -i '^x-kkul-ext' /tmp/kkul-smoke.h || true
  unset s
}

budget() {
  local ba
  ba="$("${G[@]}" billing projects describe "$PROJECT_ID" --format 'value(billingAccountName)' | sed 's#billingAccounts/##')"
  [ -n "$ba" ] || {
    echo "no billing account linked to this project" >&2
    exit 1
  }
  # The budget currency must match the billing account's.
  local cur="${BUDGET_CURRENCY:-$(gcloud billing accounts describe "$ba" --format 'value(currencyCode)' 2>/dev/null || true)}"
  cur="${cur:-USD}"
  local def=5
  [ "$cur" = KRW ] && def=7000
  local amount="${BUDGET_AMOUNT:-$def}"
  if gcloud billing budgets list --billing-account "$ba" --format 'value(displayName)' 2>/dev/null | grep -qx "kkultools doc-convert"; then
    echo "budget 'kkultools doc-convert' already exists"
    return 0
  fi
  say "Budget alerts: $amount $cur at 50/90/100/400%"
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
  smoke)
    shift
    smoke "$@"
    ;;
  budget) budget ;;
  secret-to-worker) secret_to_worker ;;
  all)
    setup
    build
    deploy
    ;;
  *)
    sed -n '2,10p' "$0"
    exit 1
    ;;
esac
