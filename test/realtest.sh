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
    if echo "$output" | grep -qi "$expect"; then
        echo -e "${GREEN}PASS${RESET}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}FAIL${RESET} (expected '$expect')"
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

# Check binary exists
if [ ! -f "$TUP_CMD" ] && ! command -v "$TUP_CMD" &>/dev/null; then
    echo -e "  ${RED}Error: '$TUP_CMD' not found.${RESET}"
    echo "  Build it first: go build -o tup ./cmd/tup"
    exit 1
fi

# Get alias and chat ID from args or prompt
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

echo -e "  Alias:   ${BOLD}$ALIAS${RESET}"
echo -e "  Chat ID: ${BOLD}$CHAT_ID${RESET}"
echo ""

# ── Create test fixtures ──────────────────────────────────────────────────

echo "  Creating test fixtures..."
echo "Hello from tup realtest!" > "$TMPDIR/small.txt"
dd if=/dev/urandom of="$TMPDIR/medium.bin" bs=1024 count=512 2>/dev/null  # 512KB
echo '{"test": true, "items": [1,2,3]}' > "$TMPDIR/test.json"
echo "# Test Markdown" > "$TMPDIR/test.md"
echo "  ✓ Created 4 test files in $TMPDIR"

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

run_test "mkdir (root dir)" mkdir "$ALIAS:/test_dir"
run_test "mkdir (nested dir)" mkdir "$ALIAS:/uploads"
run_test "ls (root)" ls "$ALIAS:/"

# ── 3. File Upload (cp local → remote) ──────────────────────────────────

section "File Upload"

run_test "cp small.txt" cp "$TMPDIR/small.txt" "$ALIAS:/small.txt"
run_test "cp medium.bin" cp "$TMPDIR/medium.bin" "$ALIAS:/medium.bin"
run_test "cp test.json" cp "$TMPDIR/test.json" "$ALIAS:/test.json"
run_test "cp test.md" cp "$TMPDIR/test.md" "$ALIAS:/test.md"

# ── 4. Listing & Verification ───────────────────────────────────────────

section "List & Verify"

run_test "ls (after uploads)" ls "$ALIAS:/"

# ── 5. File Removal ─────────────────────────────────────────────────────

section "File Removal"

run_test "rm (test.json)" rm "$ALIAS:/test.json"
run_test "rm (test.md)" rm "$ALIAS:/test.md"

# ── 6. Stub Commands (should not crash) ─────────────────────────────────

section "Stub Commands (no-crash)"

run_test "mv" mv "$ALIAS:/small.txt" "$ALIAS:/renamed.txt"
run_test "cat" cat "$ALIAS:/medium.bin"
run_test "find" find "$ALIAS:/" "*.bin"
run_test "stat" stat "$ALIAS:/medium.bin"
run_test "tree" tree "$ALIAS:/"
run_test "touch" touch "$ALIAS:/touched.txt"
run_test "du" du "$ALIAS:/"

# ── 7. Backup & Restore ─────────────────────────────────────────────────

section "Backup & Restore"

run_test "backup" backup "$ALIAS"

# ── 8. Edge Cases ────────────────────────────────────────────────────────

section "Edge Cases"

run_test_expect_fail "cp (no args)" cp
run_test_expect_output "rm (local path)" "standard" rm "/tmp/localfile.txt"
run_test_expect_output "ls (local path)" "standard" ls "/tmp"
run_test_expect_output "mkdir (local path)" "standard" mkdir "/tmp/nope"

# ── 9. Help & Info ───────────────────────────────────────────────────────

section "Help & Info"

run_test "help" --help
run_test "drive help" drive --help
run_test "cp help" cp --help
run_test "ls help" ls --help

# ══════════════════════════════════════════════════════════════════════════
# CLEANUP
# ══════════════════════════════════════════════════════════════════════════

section "Cleanup"

echo "  Removing remote test files..."
$TUP_CMD rm "$ALIAS:/small.txt"     >/dev/null 2>&1 || true
$TUP_CMD rm "$ALIAS:/renamed.txt"   >/dev/null 2>&1 || true
$TUP_CMD rm "$ALIAS:/medium.bin"    >/dev/null 2>&1 || true
$TUP_CMD rm "$ALIAS:/test.json"     >/dev/null 2>&1 || true
$TUP_CMD rm "$ALIAS:/test.md"       >/dev/null 2>&1 || true
$TUP_CMD rm "$ALIAS:/touched.txt"   >/dev/null 2>&1 || true
echo "  Removing local test fixtures..."
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
