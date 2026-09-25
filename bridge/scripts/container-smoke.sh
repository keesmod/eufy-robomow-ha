#!/usr/bin/env bash
# Clean-Linux proof for the mower bridge images. Runs with no network and synthetic values.
#   scripts/container-smoke.sh <image>            standalone image built from bridge/
#   scripts/container-smoke.sh --app <image>      Home Assistant app candidate built from ha_app/
set -euo pipefail

mode=standalone
map_mode=file
while [[ "${1:-}" = --* ]]; do
  case "$1" in
    --app) mode=app ;;
    --cloud-maps) map_mode=cloud ;;
    *) echo "smoke: unknown argument $1" >&2; exit 1 ;;
  esac
  shift
done
image=${1:?usage: container-smoke.sh [--app] [--cloud-maps] <image>}
name="eufy-mower-smoke-$$"
token="synthetic-bridge-token-0123456789abcdef"
password="synthetic-password-not-real"
workdir=$(mktemp -d)

cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  # The app container creates /data/eufy-mower as uid 1000 inside the bind mount, which the
  # calling user cannot delete on Linux. Remove it through a root container first.
  if [ -d "$workdir/data" ]; then
    docker run --rm --user 0 --network none -v "$workdir:/work" "$image" rm -rf /work/data >/dev/null 2>&1 || true
  fi
  rm -rf "$workdir" 2>/dev/null || true
}
trap cleanup EXIT

fail() {
  echo "smoke: $*" >&2
  docker logs "$name" 2>&1 | tail -40 >&2 || true
  exit 1
}

echo "smoke: $mode image $image"

# 1. Without configuration the entrypoint refuses to start with exit 78.
set +e
docker run --rm --network none "$image" >"$workdir/noconfig.log" 2>&1
status=$?
set -e
[ "$status" -eq 78 ] || { cat "$workdir/noconfig.log" >&2; echo "smoke: expected exit 78 without configuration, got $status" >&2; exit 1; }

# 2. Synthetic configuration, no network at all.
if [ "$mode" = app ]; then
  mkdir -p "$workdir/data"
  printf '{"token":"%s","email":"synthetic@example.invalid","password":"%s","country":"NL","map_provisioning_mode":"%s"}\n' "$token" "$password" "$map_mode" >"$workdir/data/options.json"
  docker run -d --name "$name" --network none -v "$workdir/data:/data" "$image" >/dev/null
else
  docker run -d --name "$name" --network none \
    -e "EUFY_MOWER_BRIDGE_TOKEN=$token" \
    -e EUFY_MOWER_EMAIL=synthetic@example.invalid \
    -e "EUFY_MOWER_PASSWORD=$password" \
    -e EUFY_MOWER_COUNTRY=NL \
    -e "EUFY_MOWER_MAP_PROVISIONING_MODE=$map_mode" \
    "$image" >/dev/null
fi

# 3. The Docker health check must report healthy within the start period plus one interval.
health=starting
for _ in $(seq 1 60); do
  health=$(docker inspect --format '{{.State.Health.Status}}' "$name" 2>/dev/null || echo missing)
  [ "$health" = healthy ] && break
  running=$(docker inspect --format '{{.State.Running}}' "$name" 2>/dev/null || echo false)
  [ "$running" = true ] || fail "container stopped before becoming healthy"
  sleep 1
done
[ "$health" = healthy ] || fail "health status is $health"

# 4. The health check script answers with a summary and the state route needs the token.
summary=$(docker exec "$name" node dist/healthcheck.js) || fail "health check script failed"
echo "smoke: $summary"
case "$summary" in
  *'"lifecycle":"running"'*) ;;
  *) fail "bridge is not running: $summary" ;;
esac
case "$summary" in
  *'"auth":"disconnected"'*) ;;
  *) fail "authentication must stay disconnected without network: $summary" ;;
esac
unauthorized=$(docker exec "$name" node -e 'fetch("http://127.0.0.1:8090/v1/state").then(r => console.log(r.status))') || fail "unauthenticated request failed"
[ "$unauthorized" = 401 ] || fail "expected 401 without token, got $unauthorized"

# Exercise options.json through the real root-dropping app bootstrap, not only config parsing.
docker exec -e "SMOKE_TOKEN=$token" -e "SMOKE_MAP_MODE=$map_mode" "$name" node --input-type=module -e '
  import assert from "node:assert/strict";
  const response = await fetch("http://127.0.0.1:8090/v1/state", {
    headers: {Authorization: `Bearer ${process.env.SMOKE_TOKEN}`},
  });
  assert.equal(response.status, 200);
  const state = await response.json();
  assert.equal(state.routes.maps, process.env.SMOKE_MAP_MODE === "cloud");
  assert.equal(state.routes.control, false);
  assert.equal(state.routes.settings, false);
' || fail "configured map mode did not reach the running bridge"

# 5. Stop within the shutdown deadline and exit 0.
docker stop -t 25 "$name" >/dev/null
exit_code=$(docker inspect --format '{{.State.ExitCode}}' "$name")
[ "$exit_code" = 0 ] || fail "expected exit 0 after SIGTERM, got $exit_code"

# 6. The log never contains the token or the password.
docker logs "$name" >"$workdir/run.log" 2>&1
if grep -q -F "$token" "$workdir/run.log" || grep -q -F "$password" "$workdir/run.log"; then
  echo "smoke: secret material found in the container log" >&2
  exit 1
fi
grep -q "listening on" "$workdir/run.log" || fail "startup log line missing"
grep -q "stopping" "$workdir/run.log" || fail "shutdown log line missing"
echo "smoke: $mode image passed"
