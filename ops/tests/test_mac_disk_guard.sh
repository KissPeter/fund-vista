#!/bin/sh
# Hermetic tests for ops/mac-disk-guard.sh (CR-009 phase 2).
#
# The guard talks to df, colima, docker, logger, osascript and the host cache
# directories. All of those are replaced with stubs under a temporary
# directory, so the test never touches the Mac's real disk or VM.
set -eu

# shellcheck disable=SC1007
SCRIPT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)/mac-disk-guard.sh
TEST_DIR=$(mktemp -d "${TMPDIR:-/tmp}/fv-disk-guard-test.XXXXXX")
trap 'rm -rf "$TEST_DIR"' EXIT INT TERM
BIN="$TEST_DIR/bin"
FAKE_HOME="$TEST_DIR/home"
STATE_DIR="$TEST_DIR/state"
WORKTREE_DIR="$TEST_DIR/worktrees"
CALL_LOG="$TEST_DIR/calls.log"
OUT="$TEST_DIR/out.log"
RECOVERED_FLAG="$TEST_DIR/recovered"
mkdir -p "$BIN" "$FAKE_HOME/Library/Caches" "$FAKE_HOME/.cache" \
    "$FAKE_HOME/.npm" "$STATE_DIR" "$WORKTREE_DIR"

cat > "$BIN/df" <<'EOF'
#!/bin/sh
avail=${FAKE_AVAIL_KB:-0}
used=${FAKE_USED_PCT:-0}
if [ "${FAKE_RECOVERY_WORKS:-0}" = "1" ] && [ -f "$FAKE_RECOVERED_FLAG" ]; then
    avail=10485760
    used=40
fi
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "/dev/fake 100000 90000 $avail ${used}% /"
EOF

cat > "$BIN/date" <<'EOF'
#!/bin/sh
if [ "${1:-}" = "+%s" ]; then
    echo "${FAKE_NOW:-1000000}"
else
    /bin/date "$@"
fi
EOF

cat > "$BIN/colima" <<'EOF'
#!/bin/sh
echo "colima $*" >> "$CALL_LOG"
if [ "${1:-}" = "status" ] && [ -f "$FAKE_RECOVERED_FLAG" ]; then
    exit 0
fi
case "${1:-}" in
    status) [ "${FAKE_COLIMA_STATUS:-0}" = "0" ];;
    stop) exit 0;;
    start)
        if [ "${FAKE_RECOVERY_WORKS:-0}" = "1" ]; then
            : > "$FAKE_RECOVERED_FLAG"
        fi
        exit 0;;
    ssh) exit 0;;
    *) exit 0;;
esac
EOF

cat > "$BIN/docker" <<'EOF'
#!/bin/sh
echo "docker $*" >> "$CALL_LOG"
exit 0
EOF

for stub in logger osascript chflags; do
    cat > "$BIN/$stub" <<'EOF'
#!/bin/sh
exit 0
EOF
done
chmod +x "$BIN"/*

fail() { echo "FAIL: $*" >&2; exit 1; }
assert_contains() {
    grep -q -- "$2" "$1" || fail "expected '$2' in:\n$(cat "$1")"
}
assert_not_contains() {
    if grep -q -- "$2" "$1"; then
        fail "unexpected '$2' in:\n$(cat "$1")"
    fi
}
assert_count_at_least() {
    count=$(grep -c -- "$2" "$1" || true)
    [ "$count" -ge "$3" ] || fail "expected >= $3 of '$2', got $count"
}

make_caches() {
    mkdir -p "$FAKE_HOME/Library/Caches/Homebrew" \
        "$FAKE_HOME/.cache/uv" "$FAKE_HOME/.npm/_cacache"
}

run_guard() {
    # run_guard <prune> <recover> <dry_run> <avail_kb> <used_pct>
    #            <colima_status> <recovery_works>
    : > "$CALL_LOG"
    make_caches
    set +e
    PATH="$BIN:$PATH" \
    CACHE_ROOT="$FAKE_HOME" \
    WORKTREE_DIR="$WORKTREE_DIR" \
    STATE_DIR="$STATE_DIR" \
    GUARD_MOUNT="$TEST_DIR" \
    GUARD_MIN_FREE_GB=5 \
    RECOVERY_RETRY_HOURS=6 \
    FAKE_NOW=1000000 \
    FAKE_AVAIL_KB="$4" \
    FAKE_USED_PCT="$5" \
    FAKE_COLIMA_STATUS="$6" \
    FAKE_RECOVERY_WORKS="$7" \
    FAKE_RECOVERED_FLAG="$RECOVERED_FLAG" \
    CALL_LOG="$CALL_LOG" \
    PRUNE="$1" RECOVER="$2" DRY_RUN="$3" \
        sh "$SCRIPT" > "$OUT" 2>&1
    EXIT_CODE=$?
    set -e
    rm -f "$RECOVERED_FLAG"
}

# 1. Healthy disk: no cleanup, no recovery, exit 0.
run_guard 0 1 0 10485760 40 0 0
[ "$EXIT_CODE" -eq 0 ] || fail "healthy run exited $EXIT_CODE"
assert_not_contains "$OUT" "PRUNE:"
assert_not_contains "$OUT" "RECOVER:"
[ ! -s "$CALL_LOG" ] || fail "healthy run called colima/docker: $(cat "$CALL_LOG")"

# 2. Breach with healthy colima: prune and fstrim, but no VM restart.
run_guard 1 0 0 1024 93 0 0
[ "$EXIT_CODE" -eq 1 ] || fail "breach run exited $EXIT_CODE"
assert_contains "$OUT" "PRUNE: docker image + builder cache in colima"
assert_contains "$OUT" "TRIM: colima VM disk"
assert_not_contains "$OUT" "RECOVER: restarting colima"
assert_not_contains "$CALL_LOG" "colima stop"
[ ! -d "$FAKE_HOME/.cache/uv" ] || fail "uv cache was not pruned"

# 3. Breach with broken colima: restart once, retry prune, recover.
run_guard 1 1 0 1024 93 1 1
[ "$EXIT_CODE" -eq 1 ] || fail "recovering run exited $EXIT_CODE"
assert_contains "$OUT" "RECOVER: restarting colima"
assert_contains "$CALL_LOG" "colima stop"
assert_contains "$CALL_LOG" "colima start"
assert_count_at_least "$OUT" "PRUNE: docker image + builder cache in colima" 2
assert_contains "$OUT" "final mount="
[ -f "$STATE_DIR/disk-guard-last-recovery" ] || fail "no recovery stamp written"

# 4. Recent restart: back off instead of bouncing the VM again.
echo 1000000 > "$STATE_DIR/disk-guard-last-recovery"
run_guard 1 1 0 1024 93 1 1
[ "$EXIT_CODE" -eq 1 ] || fail "backoff run exited $EXIT_CODE"
assert_contains "$OUT" "RECOVER: skipped"
assert_not_contains "$CALL_LOG" "colima stop"
[ "$(cat "$STATE_DIR/disk-guard-last-recovery")" = "1000000" ] \
    || fail "recovery stamp was overwritten during backoff"

# 5. Dry run: show the actions, change nothing.
rm -f "$STATE_DIR/disk-guard-last-recovery"
run_guard 1 1 1 1024 93 1 1
[ "$EXIT_CODE" -eq 1 ] || fail "dry-run exited $EXIT_CODE"
assert_contains "$OUT" "PRUNE (dry-run):"
assert_contains "$OUT" "RECOVER (dry-run): colima stop + colima start"
assert_not_contains "$CALL_LOG" "colima stop"
[ -d "$FAKE_HOME/.cache/uv" ] || fail "dry run deleted the uv cache"
[ ! -f "$STATE_DIR/disk-guard-last-recovery" ] || fail "dry run wrote a recovery stamp"

echo "mac-disk-guard tests: 5 passed"
