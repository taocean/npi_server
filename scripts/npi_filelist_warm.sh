#!/usr/bin/env bash
# npi_filelist_warm.sh — fire-and-forget NPI elaborated filelist cache builder.
#
# Cache layout (per-user, persistent, shared across runouts of same config):
#   ~/.cache/npi_filelist/<config_name>/
#     filelist.txt              # final atomic result; presence + 'status: ok' = ready
#     filelist.txt.partial      # in-progress sentinel (writer still running)
#     .build.lock               # flock target (dedup concurrent invocations)
#     build.log                 # full worker stdout/stderr (debug)
#   ~/.cache/npi_filelist/INDEX.md  # discovery index (auto-updated post-build)
#
# Usage:
#   npi_filelist_warm.sh <daidir>           # fire-and-forget; caller backgrounds
#   npi_filelist_warm.sh <daidir> --rebuild # force re-warm even if ready
#   npi_filelist_warm.sh <daidir> --status  # print state, exit
#   npi_filelist_warm.sh <daidir> --path    # print the cache file path, exit
#
# Default: never auto-rebuild. Reuse cached filelist forever unless --rebuild.

set -uo pipefail

DAIDIR_RAW="${1:-}"
MODE="${2:-build}"

if [[ -z "$DAIDIR_RAW" ]]; then
    cat >&2 <<EOF
usage: $0 <daidir> [MODE]

modes:
  (none)         build Stage 1 (filelist.txt) — default
  --rebuild      force rebuild Stage 1
  --status       print state of both stages
  --path         print Stage 1 cache path (filelist.txt)
  --stage2       build Stage 1 then Stage 2 (module_index.txt)
  --stage2-only  build only Stage 2 (assumes Stage 1 ready)
  --path-stage2  print Stage 2 cache path
EOF
    exit 2
fi

# BUG #3 fix: canonicalize daidir upfront — relative path -> absolute, resolve
# symlinks. Avoids cache-key collision between './config/X/...' and
# '/abs/config/X/...'. Status/path modes work even if daidir doesn't exist now.
case "$MODE" in
    --status|--path|--path-stage2)
        DAIDIR=$(realpath -m "$DAIDIR_RAW" 2>/dev/null || echo "$DAIDIR_RAW")
        ;;
    *)
        DAIDIR=$(realpath -e "$DAIDIR_RAW" 2>/dev/null)
        if [[ -z "$DAIDIR" || ! -d "$DAIDIR" ]]; then
            echo "ERROR: daidir does not exist or is not a directory: $DAIDIR_RAW" >&2
            exit 2
        fi
        ;;
esac

# Resolve config name from daidir path: .../config/<NAME>/pub/sim/exec/<simv>.daidir
# BUG #2 fix: pick the OUTERMOST `/config/X/pub/sim/...` match (anchored to end
# of daidir), not the innermost. Approach: strip from RIGHT (use rev + first match).
# We require the `pub/sim/exec/` -> daidir tail to be present.
# BUG #4 fix: anchor `pub/sim` followed by `/` (not just any char), and `exec`
# segment to make the match unambiguous on real daidirs.
CFG=$(echo "$DAIDIR" | sed -nE 's|^.*/config/([^/]+)/pub/sim/exec/[^/]+\.daidir/?$|\1|p')
if [[ -z "$CFG" ]]; then
    # Fall back to the looser pattern for non-standard layouts (warn only).
    CFG=$(echo "$DAIDIR" | sed -nE 's|^.*/config/([^/]+)/pub/sim/.*|\1|p')
fi
if [[ -z "$CFG" ]]; then
    echo "ERROR: cannot extract config name from daidir: $DAIDIR" >&2
    echo "       expected pattern: .../config/<NAME>/pub/sim/exec/<simv>.daidir" >&2
    exit 2
fi

CACHE_ROOT=~/.cache/npi_filelist
CACHE_DIR="$CACHE_ROOT/$CFG"
CACHE_FILE="$CACHE_DIR/filelist.txt"
PARTIAL="$CACHE_FILE.partial"
LOCK="$CACHE_DIR/.build.lock"
LOG="$CACHE_DIR/build.log"
INDEX="$CACHE_ROOT/INDEX.md"
# Stage 2 (module_index) sibling files
S2_FILE="$CACHE_DIR/module_index.txt"
S2_PARTIAL="$S2_FILE.partial"
S2_LOG="$CACHE_DIR/build.stage2.log"

_status_one() {
    local label="$1" file="$2"
    if [[ -f $file ]]; then
        local st un gn
        st=$(grep -m1 '^# status:' "$file" 2>/dev/null | sed -E 's/^# status: *//;s/[[:space:]]+$//')
        un=$(grep -m1 '^# uniq_count:\|^# n_module_defs:' "$file" 2>/dev/null | sed -E 's/^# [^:]+: *//')
        gn=$(grep -m1 '^# generated:' "$file" 2>/dev/null | sed -E 's/^# generated: *//')
        if [[ "$st" == "ok" ]]; then echo "$label: ready  $file  count=$un  generated=$gn"
        else echo "$label: failed  $file  status=$st"
        fi
    elif [[ -f "$file.partial" ]]; then
        echo "$label: building  $file.partial"
    else
        echo "$label: absent   $file"
    fi
}

# BUG #1 fix: --status checks header status, not just file presence.
case "$MODE" in
    --path)
        echo "$CACHE_FILE"
        exit 0
        ;;
    --path-stage2)
        echo "$S2_FILE"
        exit 0
        ;;
    --status)
        _status_one "stage1" "$CACHE_FILE"
        _status_one "stage2" "$S2_FILE"
        # BUG #5/#6: detect stale .partial (no live builder).
        # Only clean if partial is older than 2h AND flock succeeds AND .build.lock
        # is the SAME inode the candidate writer would hold (avoid race where a
        # concurrent --status recreates .build.lock under a fresh inode and
        # nukes a still-running build's partial — observed on MAAM cosim where
        # build legitimately takes >1h).
        for p in "$PARTIAL" "$S2_PARTIAL"; do
            if [[ -f $p && ! -f ${p%.partial} ]]; then
                age=$(( $(date +%s) - $(stat -c %Y "$p" 2>/dev/null || echo 0) ))
                if (( age > 7200 )) && [[ -f "$LOCK" ]]; then
                    if exec 9>"$LOCK" && flock -n 9; then
                        rm -f "$p"
                        exec 9>&-
                        echo "(cleaned stale: $p, age=${age}s)"
                    fi
                fi
            fi
        done
        exit 0
        ;;
    --rebuild)
        REBUILD=1
        STAGE2=
        ;;
    --stage2)
        REBUILD=
        STAGE2=1
        ;;
    --stage2-only)
        REBUILD=
        STAGE2=only
        ;;
    build|"")
        REBUILD=
        STAGE2=
        ;;
    *)
        echo "unknown mode: $MODE" >&2
        exit 2
        ;;
esac

mkdir -p "$CACHE_DIR" || { echo "ERROR: cannot create $CACHE_DIR" >&2; exit 1; }

# Acquire non-blocking flock; if held, another build is already underway → exit 0.
exec 9>"$LOCK"
if ! flock -n 9; then
    exit 0
fi

# BUG #5 fix: clean up partials on any exit.
trap 'rm -f "$PARTIAL" "$S2_PARTIAL"' EXIT

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORKER="$SCRIPT_DIR/npi_filelist_worker.py"
S2_WORKER="$SCRIPT_DIR/npi_module_index_worker.py"

EXIT_RC=0

# ---------------------------------------------------------------------------
# Stage 1 — filelist.txt
# Skip when --stage2-only OR (cache ready AND not --rebuild).
# ---------------------------------------------------------------------------
S1_NEEDED=1
if [[ "$STAGE2" == "only" ]]; then
    S1_NEEDED=0
elif [[ -z "${REBUILD:-}" && -f $CACHE_FILE ]] && grep -q '^# status: *ok' "$CACHE_FILE"; then
    S1_NEEDED=0
fi
if [[ $S1_NEEDED -eq 1 ]]; then
    if [[ ! -f $WORKER ]]; then
        echo "ERROR: stage1 worker not found: $WORKER" >&2
        exit 1
    fi
    if python3 "$WORKER" "$DAIDIR" > "$PARTIAL" 2> "$LOG"; then
        mv -f "$PARTIAL" "$CACHE_FILE"
    else
        mv -f "$PARTIAL" "$CACHE_FILE"
        EXIT_RC=1
    fi
fi

# ---------------------------------------------------------------------------
# Stage 2 — module_index.txt (opt-in via --stage2 / --stage2-only)
# ---------------------------------------------------------------------------
if [[ -n "$STAGE2" ]]; then
    if [[ ! -f $S2_WORKER ]]; then
        echo "ERROR: stage2 worker not found: $S2_WORKER" >&2
        exit 1
    fi
    if python3 "$S2_WORKER" "$DAIDIR" > "$S2_PARTIAL" 2> "$S2_LOG"; then
        mv -f "$S2_PARTIAL" "$S2_FILE"
    else
        mv -f "$S2_PARTIAL" "$S2_FILE"
        EXIT_RC=1
    fi
fi

# Update INDEX.md with current state of all caches under CACHE_ROOT.
{
    echo "# npi_filelist cache index"
    echo
    echo "Auto-updated by npi_filelist_warm.sh. One row per config."
    echo
    echo "| Config | Stage1 status | Files | Stage2 status | Module defs | Daidir |"
    echo "|---|---|---:|---|---:|---|"
    for d in "$CACHE_ROOT"/*/; do
        [[ -d "$d" ]] || continue
        cf="$d/filelist.txt"
        cm="$d/module_index.txt"
        cn=$(basename "$d")
        s1_st="—"; s1_n="—"; s2_st="—"; s2_n="—"; dd=""
        if [[ -f "$cf" ]]; then
            s1_st=$(grep -m1 '^# status:' "$cf" | sed 's/^# status: *//')
            s1_n=$(grep -m1 '^# uniq_count:' "$cf" | sed 's/^# uniq_count: *//')
            dd=$(grep -m1 '^# daidir:' "$cf" | sed 's/^# daidir: *//')
        fi
        if [[ -f "$cm" ]]; then
            s2_st=$(grep -m1 '^# status:' "$cm" | sed 's/^# status: *//')
            s2_n=$(grep -m1 '^# n_module_defs:' "$cm" | sed 's/^# n_module_defs: *//')
        fi
        echo "| $cn | $s1_st | $s1_n | $s2_st | $s2_n | \`$dd\` |"
    done
} > "$INDEX.tmp" && mv -f "$INDEX.tmp" "$INDEX"

exit $EXIT_RC
