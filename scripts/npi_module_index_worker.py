#!/usr/bin/env python3
"""
npi_module_index_worker.py — Stage 2 cache builder.

Walks `lang.find_inst_wildcard("tb_top", ["*"])` over the entire elaborated
design, groups by `def_name()`, captures `(realpath(def_file), def_line_no)`
for the *first* instance of each def, and counts total instances.

Emits TSV to stdout:
    # header lines starting with '#'
    <def_name>\t<realpath>\t<def_line>\t<inst_count>

Header fields mirror Stage 1 (filelist.txt) format so AI consumers know how to
parse both with the same grep idiom.

Usage:
    npi_module_index_worker.py <daidir>

Exit codes:
    0  module_index printed to stdout (header status: ok)
    1  failure — header still printed with status: failed: <reason>
"""
import os, sys, time, datetime

if len(sys.argv) != 2:
    sys.stderr.write(f"usage: {sys.argv[0]} <daidir>\n")
    sys.exit(2)

DAIDIR = os.path.realpath(sys.argv[1])
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
SERVER_DIR = "/home/<your_user>/.claude/skills/npi_server"
sys.path.insert(0, SERVER_DIR)

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

def emit_header(status, n_module_defs=0, n_total_instances=0,
                n_inst_walked=0, scope="tb_top", elapsed=0.0):
    sys.stdout.write(f"# npi_module_index cache v1\n")
    sys.stdout.write(f"# config:             {CFG}\n")
    sys.stdout.write(f"# daidir:             {DAIDIR}\n")
    sys.stdout.write(f"# daidir_mtime:       {daidir_mtime()}\n")
    sys.stdout.write(f"# scope:              {scope}\n")
    sys.stdout.write(f"# generated:          {NOW}\n")
    sys.stdout.write(f"# n_module_defs:      {n_module_defs}\n")
    sys.stdout.write(f"# n_total_instances:  {n_total_instances}\n")
    sys.stdout.write(f"# n_inst_walked:      {n_inst_walked}\n")
    sys.stdout.write(f"# elapsed:            {elapsed:.1f}s\n")
    sys.stdout.write(f"# status:             {status}\n")
    sys.stdout.write(f"# format:             def_name\\trealpath\\tdef_line\\tinst_count\n")

if not os.path.isdir(DAIDIR):
    emit_header(f"failed: daidir missing: {DAIDIR}")
    sys.exit(1)

try:
    from npi_server_client import NpiServerClient
except Exception as e:
    emit_header(f"failed: cannot import NpiServerClient: {e}")
    sys.exit(1)

# Walk every instance under tb_top, group by def_name, record file/line + count.
# Cost on Aurora (8.26M instances): walk 296s, extract 17s, total ~313s wall.
CODE = r'''
import time
from pynpi import lang
t0 = time.time()
insts = lang.find_inst_wildcard("tb_top", ["*"]) or []
t_walk = time.time() - t0

t1 = time.time()
per_def = {}  # def_name -> [file, line, count]
for h in insts:
    try:
        dn = h.def_name()
        if not dn:
            continue
        rec = per_def.get(dn)
        if rec is None:
            df = None
            dl = -1
            try:
                df = h.def_file()
            except Exception:
                pass
            try:
                dl = h.def_line_no()
            except Exception:
                pass
            per_def[dn] = [df or "", dl, 1]
        else:
            rec[2] += 1
    except Exception:
        pass
t_extract = time.time() - t1

rows = [(dn, rec[0], rec[1], rec[2]) for dn, rec in per_def.items()]
_result = {
    "n_inst_walked": len(insts),
    "n_module_defs": len(per_def),
    "t_walk": t_walk,
    "t_extract": t_extract,
    "rows": rows,
}
'''

t0 = time.time()
try:
    with NpiServerClient(daidir=DAIDIR, startup_timeout=300) as srv:
        r = srv.query({"cmd": "eval", "code": CODE, "timeout": 1500}, timeout=1600)
        if 'error' in r:
            emit_header(f"failed: NPI eval error: {r.get('error')[:200]}")
            sys.exit(1)
        result = r.get('result', {})
except Exception as e:
    emit_header(f"failed: NPI client error: {e}")
    sys.exit(1)

elapsed = time.time() - t0
rows = result.get("rows", [])
n_inst_walked = result.get("n_inst_walked", 0)
n_module_defs = result.get("n_module_defs", 0)

# Realpath the def_file (may be symlinked); compute total instances served.
total_inst = sum(r[3] for r in rows)
# Sort by def_name for deterministic, grep-friendly output.
rows.sort(key=lambda r: r[0])

emit_header("ok",
            n_module_defs=n_module_defs,
            n_total_instances=total_inst,
            n_inst_walked=n_inst_walked,
            elapsed=elapsed)

for dn, df, dl, ct in rows:
    df_real = os.path.realpath(df) if df else ""
    sys.stdout.write(f"{dn}\t{df_real}\t{dl}\t{ct}\n")
sys.exit(0)
