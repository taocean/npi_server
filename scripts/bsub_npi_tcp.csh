#!/bin/tcsh
# bsub_npi_tcp.csh — one-shot launcher for npi_rtl_server in TCP mode.
#
# Usage:
#     cd <test-runout>           # where rendezvous file will live
#     bsub_npi_tcp.csh <daidir> [JOB_TAG]
#
# What it does:
#   1. Derive default rendezvous path from cwd + md5(daidir) — same scheme the
#      client uses, so a `python3 npi_server_client.py --transport tcp ...`
#      invoked from this same cwd will find the daemon automatically.
#   2. If rendezvous file already exists, print it and bail (one daemon per
#      daidir per cwd).
#   3. lsf_bsub the daemon to regr_high (no preempt, no wall limit, no idle
#      reaper at LSF layer — daemon self-reaps via NPI_IDLE_TIMEOUT_HOUR).
#
# Tunables (env vars):
#   NPI_BSUB_MEM            reservation in MB         (default 20000)
#   NPI_BSUB_MEM_HARD       hard-limit -M in MB        (default ${NPI_BSUB_MEM}+5000)
#   NPI_BSUB_QUEUE          LSF queue                  (default regr_high)
#   NPI_BSUB_PROJECT        -P project                 (default loveland-ver)
#   NPI_BSUB_GROUP          -G group                   (default loveland-ver)
#   NPI_IDLE_TIMEOUT_HOUR   daemon self-reap idle      (default 24 = 1 day)
#   NPI_RENDEZVOUS_PATH     full endpoint file path    (default cwd + hash)
#
# Mem sizing rules of thumb (RSS, not disk):
#   - mid die single config             ~12-15 GB → NPI_BSUB_MEM=18000
#   - mid die full / cumberland mango   ~15-20 GB → 20000  (default)
#   - bigboy_nlp / MAAM top tree        ~30-40 GB → 45000

if ($#argv < 1) then
    echo "usage: $0 <daidir> [JOB_TAG]"
    echo "  cd to the test runout first; rendezvous file lands there."
    exit 1
endif

set DAIDIR_RAW = "$1"
set TAG = "npi"
if ($#argv >= 2) set TAG = "$2"

# Resolve daidir to absolute path (md5 key requires it).
set DAIDIR = `python3 -c "import os,sys; p=os.path.abspath(sys.argv[1]); print(p if os.path.isdir(p) else '')" "$DAIDIR_RAW"`
if ("$DAIDIR" == "") then
    echo "ERROR: not a valid daidir directory: $DAIDIR_RAW"
    exit 1
endif

# Locate the skill (this script lives in <skill>/scripts/).
set SCRIPT_DIR = `dirname $0`
set SKILL_DIR = `cd $SCRIPT_DIR/.. && pwd`
set SERVER_PY = "$SKILL_DIR/npi_rtl_server.py"
if (! -f $SERVER_PY) then
    echo "ERROR: npi_rtl_server.py not found at $SERVER_PY"
    exit 1
endif

# Derive rendezvous path — same scheme as server / client default.
set CWD = `pwd`
set HASH = `python3 -c "import hashlib,sys; print(hashlib.md5(sys.argv[1].encode()).hexdigest()[:12])" "$DAIDIR"`
set RENDEZVOUS = "$CWD/.npi_server_${USER}_${HASH}.endpoint"
if ($?NPI_RENDEZVOUS_PATH) then
    set RENDEZVOUS = "$NPI_RENDEZVOUS_PATH"
endif

# If rendezvous already exists, daemon may already be alive — show + bail.
if (-f "$RENDEZVOUS") then
    echo "Rendezvous file already exists: $RENDEZVOUS"
    echo "Contents:"
    cat "$RENDEZVOUS"
    echo ""
    echo "If the daemon is alive, just connect:"
    echo "    python3 $SKILL_DIR/npi_server_client.py --transport tcp --daidir $DAIDIR --cmd ping"
    echo "If you suspect it's stale, shut it down first or rm the rendezvous file."
    exit 0
endif

# Mem sizing.
set MEM = 20000
if ($?NPI_BSUB_MEM) then
    set MEM = "$NPI_BSUB_MEM"
endif
set MEM_HARD = `expr $MEM + 5000`
if ($?NPI_BSUB_MEM_HARD) then
    set MEM_HARD = "$NPI_BSUB_MEM_HARD"
endif

set QUEUE = "regr_high"
if ($?NPI_BSUB_QUEUE) then
    set QUEUE = "$NPI_BSUB_QUEUE"
endif
set PROJECT = "loveland-ver"
if ($?NPI_BSUB_PROJECT) then
    set PROJECT = "$NPI_BSUB_PROJECT"
endif
set GROUP = "loveland-ver"
if ($?NPI_BSUB_GROUP) then
    set GROUP = "$NPI_BSUB_GROUP"
endif

set IDLE_H = "24"
if ($?NPI_IDLE_TIMEOUT_HOUR) then
    set IDLE_H = "$NPI_IDLE_TIMEOUT_HOUR"
endif

# Log path also lives in cwd (so it stays with the rendezvous + runout).
set LOG = "$CWD/.npi_server_${USER}_${HASH}.%J.log"

echo "Submitting NPI TCP daemon:"
echo "  daidir:     $DAIDIR"
echo "  rendezvous: $RENDEZVOUS"
echo "  log:        $LOG"
echo "  queue:      $QUEUE  (project=$PROJECT group=$GROUP)"
echo "  mem:        ${MEM}MB reserve / ${MEM_HARD}MB hard limit"
echo "  idle:       ${IDLE_H}h auto-shutdown (NPI_IDLE_TIMEOUT_HOUR)"
echo ""

# Locate lsf_bsub. Standard AMD path; fall back to PATH if not present.
set LSF_BSUB = "/tool/pandora64/.package/lsf-tools-1.3/bin/lsf_bsub"
if (! -x $LSF_BSUB) then
    set LSF_BSUB = "lsf_bsub"
endif

# Pass NPI_IDLE_TIMEOUT_HOUR into the bsub'd env via env-prefix.
$LSF_BSUB \
    -q $QUEUE -P $PROJECT -G $GROUP -n 1 \
    -R "select[type==RHEL8_64] rusage[mem=$MEM] span[hosts=1]" \
    -M $MEM_HARD -J "npi_${TAG}" -o "$LOG" \
    "env NPI_IDLE_TIMEOUT_HOUR=$IDLE_H python3 $SERVER_PY --transport tcp --daidir $DAIDIR --rendezvous $RENDEZVOUS"

echo ""
echo "Daemon will be ready in ~30-120s (depending on daidir size)."
echo "Check readiness:"
echo "    ls -la $RENDEZVOUS         # rendezvous file appears when ready"
echo "    cat $RENDEZVOUS            # host:port:pid:token"
echo "    tail -f ${LOG:s/%J/<jobid>/}  # daemon log"
echo ""
echo "Connect (from THIS same cwd, on any host):"
echo "    python3 $SKILL_DIR/npi_server_client.py --transport tcp --daidir $DAIDIR --cmd ping"
echo ""
echo "Shutdown:"
echo "    python3 $SKILL_DIR/npi_server_client.py --transport tcp --daidir $DAIDIR --shutdown-daemon"
