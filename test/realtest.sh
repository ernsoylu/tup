#!/usr/bin/env bash
# ============================================================================
# TUP End-to-End Real Test Script
#
# Usage:
#   ./test/realtest.sh                    # interactive: prompts for alias & chat ID
#   ./test/realtest.sh VFS -1004342175024 # non-interactive: provide alias & chat ID
#
# Environment:
#   TUP_CMD  - path to the tup binary (default: ./tup)
# ============================================================================

set +e  # Don't exit on first error — collect all results

# ── Configuration ──────────────────────────────────────────────────────────

TUP_CMD=${TUP_CMD:-./tup}
ALIAS=""
CHAT_ID=""
TMPDIR=$(mktemp -d)
PASSED=0
FAILED=0
SKIPPED=0
SECTION=""

# All test paths live under this prefix so the rest of the drive is untouched
ROOT=""

# ── Colours ────────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

# ── Helpers ────────────────────────────────────────────────────────────────

banner() {
    echo ""
    echo -e "${BOLD}${CYAN}═══════════════════════════════════════════════════${RESET}"
    echo -e "${BOLD}${CYAN}  $1${RESET}"
    echo -e "${BOLD}${CYAN}═══════════════════════════════════════════════════${RESET}"
}

section() {
    SECTION="$1"
    echo ""
    echo -e "${BOLD}── $1 ──${RESET}"
}

run_test() {
    local name="$1"
    shift
    local label="[$SECTION] $name"
    printf "  %-50s " "$label"

    if output=$($TUP_CMD "$@" 2>&1); then
        echo -e "${GREEN}PASS${RESET}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${RESET}"
        echo "$output" | sed 's/^/    │ /'
        FAILED=$((FAILED + 1))
    fi
}

run_test_expect_output() {
    local name="$1"
    local expect="$2"
    shift 2
    local label="[$SECTION] $name"
    printf "  %-50s " "$label"

    output=$($TUP_CMD "$@" 2>&1)
    rc=$?
    if echo "$output" | grep -qi "$expect"; then
        echo -e "${GREEN}PASS${RESET}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${RESET} (expected '$expect', rc=$rc)"
        echo "$output" | sed 's/^/    │ /'
        FAILED=$((FAILED + 1))
    fi
}

run_test_expect_fail() {
    local name="$1"
    shift
    local label="[$SECTION] $name"
    printf "  %-50s " "$label"

    if output=$($TUP_CMD "$@" 2>&1); then
        echo -e "${RED}FAIL${RESET} (expected error but succeeded)"
        echo "$output" | sed 's/^/    │ /'
        FAILED=$((FAILED + 1))
    else
        echo -e "${GREEN}PASS${RESET}"
        PASSED=$((PASSED + 1))
    fi
}

skip_test() {
    local name="$1"
    local reason="$2"
    local label="[$SECTION] $name"
    printf "  %-50s " "$label"
    echo -e "${YELLOW}SKIP${RESET} ($reason)"
    SKIPPED=$((SKIPPED + 1))
}

# ── Input ──────────────────────────────────────────────────────────────────

banner "TUP End-to-End Test Suite"

echo ""
echo -e "  Binary:  ${BOLD}$TUP_CMD${RESET}"

if [ ! -f "$TUP_CMD" ] && ! command -v "$TUP_CMD" &>/dev/null; then
    echo -e "  ${RED}Error: '$TUP_CMD' not found.${RESET}"
    echo "  Build it first: go build -o tup ./cmd/tup"
    exit 1
fi

if [ -n "$1" ] && [ -n "$2" ]; then
    ALIAS="$1"
    CHAT_ID="$2"
else
    echo ""
    echo "  Enter the drive alias and chat ID to use for testing."
    echo "  (Run './tup drive chats' first to find your chat ID)"
    echo ""
    read -rp "  Drive alias (e.g. VFS): " ALIAS
    read -rp "  Chat ID (e.g. -1004342175024): " CHAT_ID
fi

ROOT="$ALIAS:/_e2e"

echo -e "  Alias:   ${BOLD}$ALIAS${RESET}"
echo -e "  Chat ID: ${BOLD}$CHAT_ID${RESET}"
echo -e "  Sandbox: ${BOLD}$ROOT${RESET}"
echo ""

# ── Create test fixtures ──────────────────────────────────────────────────

echo "  Creating test fixtures..."
echo "Hello from tup realtest!" > "$TMPDIR/small.txt"
dd if=/dev/urandom of="$TMPDIR/medium.bin" bs=1024 count=512 2>/dev/null  # 512KB
echo '{"test": true, "items": [1,2,3]}' > "$TMPDIR/test.json"
echo "# Test Markdown" > "$TMPDIR/test.md"
mkdir -p "$TMPDIR/nested/sub"
echo "nested file" > "$TMPDIR/nested/sub/deep.txt"
echo "  ✓ Created fixtures in $TMPDIR"

# Clean any leftover sandbox from a previous run
$TUP_CMD rm -rf "$ROOT" >/dev/null 2>&1 || true

# ══════════════════════════════════════════════════════════════════════════
# TEST SECTIONS
# ══════════════════════════════════════════════════════════════════════════

# ── 1. Drive Management ──────────────────────────────────────────────────

section "Drive Management"

run_test "drive add" drive add "$ALIAS" "$CHAT_ID"
run_test_expect_output "drive list shows alias" "$ALIAS" drive list
run_test "drive chats" drive chats

# ── 2. Directory Operations ──────────────────────────────────────────────

section "Directory Operations"

run_test "mkdir -p sandbox" mkdir -p "$ROOT"
run_test "mkdir (child)" mkdir "$ROOT/test_dir"
run_test "mkdir -p nested" mkdir -p "$ROOT/uploads/a/b"
run_test_expect_fail "mkdir without -p missing parent" mkdir "$ROOT/missing/parent/child"
run_test "ls root sandbox" ls "$ROOT"
run_test "ls -l" ls -l "$ROOT"
run_test "ls -lH" ls -lH "$ROOT"
run_test "ls -R" ls -R "$ROOT"
run_test "tree sandbox" tree "$ROOT"
run_test_expect_output "tree --json" '"type"' tree --json "$ROOT"
run_test_expect_output "tree path --json" '"summary"' tree "$ROOT" --json

# ── 3. File Upload (cp local → remote) ──────────────────────────────────

section "File Upload"

run_test "cp small.txt" cp "$TMPDIR/small.txt" "$ROOT/small.txt"
run_test "cp medium.bin" cp "$TMPDIR/medium.bin" "$ROOT/medium.bin"
run_test "cp test.json" cp "$TMPDIR/test.json" "$ROOT/test.json"
run_test "cp test.md" cp "$TMPDIR/test.md" "$ROOT/test.md"
run_test "cp -r nested dir" cp -r "$TMPDIR/nested" "$ROOT/"
run_test_expect_output "ls shows deep.txt path" "deep.txt" find "$ROOT" -name "deep.txt"

# ── 4. Listing & Verification ───────────────────────────────────────────

section "List & Verify"

run_test_expect_output "ls after uploads" "small.txt" ls "$ROOT"
run_test_expect_output "find *.bin" "medium.bin" find "$ROOT" -name "*.bin"
run_test_expect_output "stat medium.bin" "Size:" stat "$ROOT/medium.bin"
run_test_expect_output "du -s" "total\|B\|K\|M\|[0-9]" du -s "$ROOT"
run_test "du -sH" du -sH "$ROOT"
run_test "tree after upload" tree "$ROOT"

# ── 5. Download (cp remote → local) ─────────────────────────────────────

section "File Download"

mkdir -p "$TMPDIR/dl"
run_test "cp download small.txt" cp "$ROOT/small.txt" "$TMPDIR/dl/small.txt"
run_test_expect_output "downloaded content" "Hello from tup realtest" cat "$ROOT/small.txt"
# verify local file content
printf "  %-50s " "[$SECTION] local file matches"
if grep -q "Hello from tup realtest" "$TMPDIR/dl/small.txt" 2>/dev/null; then
    echo -e "${GREEN}PASS${RESET}"
    PASSED=$((PASSED + 1))
else
    echo -e "${RED}FAIL${RESET}"
    FAILED=$((FAILED + 1))
fi
run_test "cp -r download nested" cp -r "$ROOT/nested" "$TMPDIR/dl/"

# ── 6. Remote copy ──────────────────────────────────────────────────────

section "Remote Copy"

run_test "cp remote file" cp "$ROOT/small.txt" "$ROOT/small-copy.txt"
run_test "cp -r remote dir" cp -r "$ROOT/nested" "$ROOT/nested-copy"
run_test_expect_output "remote copy listed" "small-copy.txt" ls "$ROOT"

# ── 7. Move / rename ────────────────────────────────────────────────────

section "Move & Rename"

run_test "mv rename file" mv "$ROOT/small-copy.txt" "$ROOT/renamed.txt"
run_test_expect_output "renamed exists" "renamed.txt" ls "$ROOT"
run_test "mv into dir" mv "$ROOT/renamed.txt" "$ROOT/test_dir/"
run_test_expect_output "moved into test_dir" "renamed.txt" ls "$ROOT/test_dir"

# ── 8. Touch / cat / rmdir ──────────────────────────────────────────────

section "Touch Cat Rmdir"

run_test "touch new file" touch "$ROOT/touched.txt"
run_test "touch existing" touch "$ROOT/touched.txt"
run_test "mkdir empty" mkdir "$ROOT/empty_dir"
run_test "rmdir empty" rmdir "$ROOT/empty_dir"
run_test_expect_fail "rmdir non-empty" rmdir "$ROOT/test_dir"
run_test "cat small.txt" cat "$ROOT/small.txt"

# ── 9. File Removal ─────────────────────────────────────────────────────

section "File Removal"

run_test "rm file" rm "$ROOT/test.json"
run_test "rm -f missing" rm -f "$ROOT/does-not-exist"
run_test_expect_fail "rm dir without -r" rm "$ROOT/nested"
run_test "rm -r dir" rm -r "$ROOT/nested-copy"
run_test "rm test.md" rm "$ROOT/test.md"

# ── 10. Backup ──────────────────────────────────────────────────────────

section "Backup & Restore"

run_test "backup" backup "$ALIAS"

# ── 11. Edge Cases ──────────────────────────────────────────────────────

section "Edge Cases"

run_test_expect_fail "cp (no args)" cp
run_test_expect_output "rm (local path)" "standard" rm "/tmp/localfile.txt"
run_test_expect_output "ls (local path)" "standard" ls "/tmp"
run_test_expect_output "mkdir (local path)" "standard" mkdir "/tmp/nope"
run_test_expect_output "bare alias tree" "directories\|files\|/" tree "$ALIAS"

# ── 12. Help & Info ─────────────────────────────────────────────────────

section "Help & Info"

run_test "help" --help
run_test "drive help" drive --help
run_test "cp help" cp --help
run_test "ls help" ls --help
run_test "find help" find --help
run_test "du help" du --help

# ══════════════════════════════════════════════════════════════════════════
# CLEANUP
# ══════════════════════════════════════════════════════════════════════════

section "Cleanup"

echo "  Removing sandbox $ROOT ..."
$TUP_CMD rm -rf "$ROOT" >/dev/null 2>&1 || $TUP_CMD rm -r "$ROOT" >/dev/null 2>&1 || true
echo "  Removing local fixtures..."
rm -rf "$TMPDIR"
echo "  ✓ Cleanup complete"

# ══════════════════════════════════════════════════════════════════════════
# REPORT
# ══════════════════════════════════════════════════════════════════════════

TOTAL=$((PASSED + FAILED + SKIPPED))

banner "Test Results"

echo ""
echo -e "  Total:   ${BOLD}$TOTAL${RESET}"
echo -e "  Passed:  ${GREEN}${BOLD}$PASSED${RESET}"
echo -e "  Failed:  ${RED}${BOLD}$FAILED${RESET}"
echo -e "  Skipped: ${YELLOW}${BOLD}$SKIPPED${RESET}"
echo ""

if [ "$FAILED" -eq 0 ]; then
    echo -e "  ${GREEN}${BOLD}✅ All tests passed!${RESET}"
    exit 0
else
    echo -e "  ${RED}${BOLD}❌ $FAILED test(s) failed.${RESET}"
    exit 1
fi
