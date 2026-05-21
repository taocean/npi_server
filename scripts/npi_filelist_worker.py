#!/usr/bin/env python3
"""
npi_filelist_worker.py — call NPI text.get_file_list() and emit a sorted/deduped
realpath list with comment header to stdout.

Used by npi_filelist_warm.sh; not intended for direct invocation.

Usage:
    npi_filelist_worker.py <daidir>

Exit codes:
    0  filelist printed to stdout (header status: ok)
    1  failure — header still printed with status: failed: <reason>
"""
import os, sys, time, datetime, json, traceback

if len(sys.argv) != 2:
    sys.stderr.write(f"usage: {sys.argv[0]} <daidir>\n")
    sys.exit(2)

DAIDIR = os.path.realpath(sys.argv[1])
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
SERVER_DIR = os.path.dirname(SCRIPT_DIR)  # npi_server skill root
sys.path.insert(0, SERVER_DIR)

# Extract config name from path: .../config/<NAME>/pub/sim/...
def extract_config(p):
    parts = p.split('/')
    if 'config' in parts:
        i = parts.index('config')
        if i + 1 < len(parts):
            return parts[i + 1]
    return 'unknown'

CFG = extract_config(DAIDIR)
NOW = datetime.datetime.now().astimezone().strftime('%Y-%m-%dT%H:%M:%S%z')

def daidir_mtime():
    try:
        ts = os.path.getmtime(DAIDIR)
        return datetime.datetime.fromtimestamp(ts).astimezone().strftime('%Y-%m-%dT%H:%M:%S%z')
    except Exception:
        return 'unknown'

def emit_header(status, raw_count=0, uniq_count=0, npi_ver='unknown'):
    sys.stdout.write(f"# npi_filelist cache v1\n")
    sys.stdout.write(f"# config:        {CFG}\n")
    sys.stdout.write(f"# daidir:        {DAIDIR}\n")
    sys.stdout.write(f"# daidir_mtime:  {daidir_mtime()}\n")
    sys.stdout.write(f"# npi_ver:       {npi_ver}\n")
    sys.stdout.write(f"# generated:     {NOW}\n")
    sys.stdout.write(f"# raw_count:     {raw_count}\n")
    sys.stdout.write(f"# uniq_count:    {uniq_count}\n")
    sys.stdout.write(f"# status:        {status}\n")

if not os.path.isdir(DAIDIR):
    emit_header(f"failed: daidir missing: {DAIDIR}")
    sys.exit(1)

# Use NpiServerClient (auto-spawns daemon if needed; reuses existing if up).
try:
    from npi_server_client import NpiServerClient
except Exception as e:
    emit_header(f"failed: cannot import NpiServerClient: {e}")
    sys.exit(1)

CODE = r'''
from pynpi import text
import os
fl = text.get_file_list() or []
paths = []
for fh in fl:
    try:
        p = fh.file_full_name()
        if p:
            paths.append(p)
    except Exception:
        pass
_result = {'raw': paths}
'''

# Allow timeout override for huge multi-die top trees (e.g. MAAM cosim ~30+ min).
# Default 1200s eval / 1300s client; override via env NPI_FILELIST_EVAL_TIMEOUT (seconds).
EVAL_TIMEOUT = int(os.environ.get('NPI_FILELIST_EVAL_TIMEOUT', '1200'))
CLIENT_TIMEOUT = EVAL_TIMEOUT + 100

t0 = time.time()
try:
    with NpiServerClient(daidir=DAIDIR, startup_timeout=300) as srv:
        # Warm + cold first call may take 50-600s on real SoCs.
        r = srv.query({"cmd": "eval", "code": CODE, "timeout": EVAL_TIMEOUT}, timeout=CLIENT_TIMEOUT)
        if 'error' in r:
            emit_header(f"failed: NPI eval error: {r.get('error')[:200]}")
            sys.exit(1)
        raw = r.get('result', {}).get('raw', [])
except Exception as e:
    emit_header(f"failed: NPI client error: {e}")
    sys.exit(1)

elapsed = time.time() - t0

# Dedup + realpath + sort
unique = sorted({os.path.realpath(p) for p in raw if p})

emit_header("ok", raw_count=len(raw), uniq_count=len(unique))
sys.stdout.write(f"# elapsed:    {elapsed:.1f}s\n")
for p in unique:
    sys.stdout.write(p + "\n")
sys.exit(0)
