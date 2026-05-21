#!/usr/bin/env python3
"""
npi_rtl_server.py — General-purpose NPI RTL analysis persistent server.

Loads the VCS design database (daidir) ONCE, then serves JSON queries
over stdin/stdout indefinitely. Avoids repeated load_design() calls so
upper-level AI agents can issue many trace queries cheaply.

Protocol:
  • Each request  = one JSON object per line on stdin
  • Each response = one JSON object per line on stdout
  • Server stderr = diagnostic/progress messages (safe to discard)

Startup:
  python3 npi_rtl_server.py [--daidir <path>] [--top <top>]

  On success prints:  {"status": "ready", "daidir": "<path>"}
  On failure prints:  {"status": "error", "msg": "..."}  then exits 1

Commands:
  {"cmd": "ping"}
      → {"status": "pong"}

  {"cmd": "handle_by_name", "signal": "<full.hier.sig>"}
      → {"found": true/false, "name":..., "width":..., "src_file":..., "src_line":...}
        Direct handle lookup (much faster than find_signal for exact names).

  {"cmd": "find_signal", "scope": "<full.hier>", "pattern": "<glob>"}
      → {"signals": [{"name":..., "width":..., "src_file":..., "src_line":...}, ...]}

  # DEPRECATED: trace_driver / trace_load built-in commands are commented out.
  # Use eval with lang.trace_driver_dump2() or lang.trace_driver_by_hdl2(is_pass_thr=True)
  # instead — they handle deep cross-module tracing natively without manual recursion.
  # See SKILL.md eval section for templates.

  {"cmd": "get_src_file", "signal": "<full.hier.sig>"}
      → {"signal": ..., "src_file": ..., "src_line": ...}

  {"cmd": "get_filelist", "scope": "<full.hier>" (optional), "use_csv": true}
      → {"files": ["path1", "path2", ...], "count": N}
        Returns unique RTL source file paths under scope.
        use_csv=true (default) uses hier_tree_dump_csv for accuracy.
        use_csv=false falls back to find_signal_wildcard sampling.

  {"cmd": "hier_search", "module": "<module_name>", "scope": "<root>" (optional),
                          "max_results": 100}
      → {"module": ..., "instances": [{"path":..., "src_file":..., "src_line":...}, ...]}
        Find all instances of a module by name under scope.
        Uses hier_tree_dump_csv internally (cached per scope).

  {"cmd": "get_module_ports", "scope": "<full.hier.instance>"}
      → {"scope":..., "ports": [{name, direction, width, src_file, src_line}]}
        List all port signals of the given scope/instance.

  {"cmd": "eval", "code": "<python code>", "timeout": 30}
      → {"result": <value of _result variable>}
        Execute arbitrary pynpi code in the loaded design context.
        The code has access to `lang`, `npisys`, `json`, `os` globals.
        Set `_result` to the value you want returned (must be JSON-serializable).
        Example: {"cmd":"eval","code":"_result = lang.handle_by_name('tb.dut.clk',None) is not None"}

  {"cmd": "quit"}
      → {"status": "quit"}

Usage notes:
  - Use npi_server_client.py for subprocess-based access.
  - For interactive testing: run server, pipe JSON lines to stdin.
"""

import argparse
import csv
import json
import os
import sys
import tempfile
import threading
import time
import traceback

# ── npi_utils lives alongside this file (no sibling-dir indirection) ────────
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from npi_utils import bootstrap_pynpi, bootstrap_pynpi_for_daidir, auto_daidir  # noqa: E402

# ── module-level CSV cache (scope_name → parsed rows list) ───────────────────
_hier_csv_cache: dict = {}   # {scope_key: [{"HierarchyPath":..., "ModuleName":..., "SrcFilePath":...}, ...]}

# ── global timing flag (set via --log-timing) ─────────────────────────────────
_log_timing: bool = False

# Peek at --daidir BEFORE argparse so the right Verdi-bundled python is picked
# (re-exec happens here if interpreter doesn't match the daidir's compile-time Verdi).
_early_daidir = ""
for _i, _a in enumerate(sys.argv):
    if _a == "--daidir" and _i + 1 < len(sys.argv):
        _early_daidir = sys.argv[_i + 1]; break
    if _a.startswith("--daidir="):
        _early_daidir = _a.split("=", 1)[1]; break
if _early_daidir:
    bootstrap_pynpi_for_daidir(_early_daidir)
else:
    bootstrap_pynpi()  # legacy path: rely on $VERDI_HOME

# ── stdout fd isolation ──────────────────────────────────────────────────────
# The NPI C library (load_design, etc.) can print thousands of warning
# messages directly to C-level stdout (fd 1), e.g. "was created by older
# version of VCS/elabCom".  These corrupt the JSON protocol on stdout.
#
# Fix: save the real stdout fd, then redirect fd 1 → stderr (fd 2).
# All subsequent NPI C-level writes land on stderr, while _emit() uses
# the saved fd for clean JSON output.
_real_stdout_fd = os.dup(1)          # duplicate the real stdout fd
os.dup2(2, 1)                        # fd 1 now points to stderr
_json_out = os.fdopen(_real_stdout_fd, 'w', buffering=1)  # line-buffered

import pynpi.npisys as npisys  # noqa: E402
from pynpi import lang           # noqa: E402
from pynpi import text           # noqa: E402
from pynpi import waveform       # noqa: E402

# pynpi.sdb was removed in some Verdi releases (e.g. 2024.09-SP2-7). It is only
# referenced by a couple of optional commands; keep it optional rather than crash
# the daemon on import. eval code that references `sdb` will get NameError instead.
try:
    from pynpi import sdb        # noqa: E402
except ImportError:
    sdb = None

# Register short aliases so that `import npisys` / `import lang` work inside eval()
sys.modules.setdefault("npisys", npisys)
sys.modules.setdefault("lang", lang)
if sdb is not None:
    sys.modules.setdefault("sdb", sdb)

# ── helpers ──────────────────────────────────────────────────────────────────

def _emit(obj: dict) -> None:
    """Write one JSON response line to the real stdout (saved fd) and flush."""
    _json_out.write(json.dumps(obj, ensure_ascii=False) + '\n')
    _json_out.flush()


def _err(msg: str) -> dict:
    return {"error": msg}


def _sig_info(hdl, include_dir: bool = False) -> dict:
    """Extract name / width / src_file / src_line (and optionally direction) from a lang handle.

    For module instances, returns the *definition* file (e.g. cf_clkc1_0_mid_t.v),
    not the instantiation site (chip_mid.v).  The instantiation location is available
    via ``hdl.file()`` / ``hdl.line_no()`` if needed.

    Correct pynpi handle methods (NOT file_name/line_num — those don't exist):
      hdl.file()         — file where the instance is instantiated
      hdl.line_no()      — line where the instance is instantiated
      hdl.def_file()     — file where the module is defined (for module instances)
      hdl.def_line_no()  — line where the module is defined
      hdl.def_name()     — module definition name
    """
    info = {"name": "", "width": "", "src_file": "", "src_line": ""}
    try:
        info["name"] = hdl.full_name() or hdl.name() or ""
    except Exception:
        pass
    try:
        sz = hdl.size()
        info["width"] = str(sz) if sz is not None else ""
    except Exception:
        pass

    # Primary: use hdl.def_file() for modules (gives the definition file),
    # fall back to hdl.file() for signals (gives the declaration file).
    try:
        df = hdl.def_file()
        if df:
            info["src_file"] = df
            try:
                dl = hdl.def_line_no()
                info["src_line"] = str(dl) if dl is not None and dl >= 0 else ""
            except Exception:
                pass
    except Exception:
        pass

    if not info["src_file"]:
        try:
            f = hdl.file()
            if f:
                info["src_file"] = f
                try:
                    ln = hdl.line_no()
                    info["src_line"] = str(ln) if ln is not None and ln >= 0 else ""
                except Exception:
                    pass
        except Exception:
            pass

    if include_dir:
        try:
            d = hdl.direction()
            info["direction"] = {0: "input", 1: "output", 2: "inout"}.get(d, "unknown")
        except Exception:
            info["direction"] = ""
    return info


def _safe_expr(use_hdl) -> str:
    """Try to decompile a driver expression handle to a readable string."""
    if use_hdl is None:
        return "<tie>"
    # Try lang.get_hdl_info first (returns descriptive string like "1'h0")
    for fn in (
        lambda h: lang.get_hdl_info(h),
        lambda h: lang.expr_decompile(h),
    ):
        try:
            s = fn(use_hdl)
            if s:
                return str(s).strip()
        except Exception:
            pass
    return "<const>"


def _trc_opt():
    opt = lang.TrcOption()
    try:
        opt.set_ignore_port_dir(True)
    except Exception:
        pass
    return opt


# ── command handlers ─────────────────────────────────────────────────────────

def cmd_ping(_req):
    return {"status": "pong"}


def cmd_handle_by_name(req):
    """Direct lookup by full hierarchical name — fastest way to check if a signal exists."""
    sig_name = req.get("signal", "")
    if not sig_name:
        return _err("'signal' is required")
    hdl = lang.handle_by_name(sig_name, None)
    if hdl is None:
        return {"found": False, "signal": sig_name,
                "hint": "use cmd 'diagnose_hier' to find where the path breaks + sibling/fuzzy suggestions"}
    info = _sig_info(hdl)
    info["found"] = True
    return info


def cmd_diagnose_hier(req):
    """Walk a hier path top-down, find where it first breaks, list siblings + fuzzy suggestions
    at the breakpoint. Eliminates the ambiguous 'found: False' problem from cmd_handle_by_name —
    instead of guessing alternate names, AI gets a concrete diagnosis in one call.

    Use case: IP-level docs (fusedoc csv, strap_param csv, RTL .v port lists) give hier names
    that work in IP testbench but mismatch the chip-level elaborated naming after integration
    (e.g. `cip_nbif_t` becomes `cip_nbif_mid_t` after die-suffix wrapping).
    """
    import difflib
    sig_name = req.get("signal", "")
    if not sig_name:
        return _err("'signal' is required")
    parts = sig_name.split(".")
    found_up_to = None
    parent_hdl = None
    breakpoint_idx = None
    for i in range(1, len(parts) + 1):
        prefix = ".".join(parts[:i])
        h = lang.handle_by_name(prefix, None)
        if h is None:
            breakpoint_idx = i - 1     # parts[breakpoint_idx] is the missing token
            break
        found_up_to = prefix
        parent_hdl = h
    if breakpoint_idx is None:
        # Whole path resolved — same as handle_by_name returning True
        info = _sig_info(parent_hdl)
        info["found"] = True
        info["diagnosis"] = "full path resolved"
        return info
    missing_token = parts[breakpoint_idx]
    # Collect siblings at the parent of the missing token
    siblings = []
    if parent_hdl is not None:
        for getter in ("internal_scope_handles", "instance_handles", "i_o_decl_handles"):
            try:
                items = getattr(parent_hdl, getter, lambda: None)() or []
                for c in items:
                    nm = None
                    try:
                        nm = c.name() or c.full_name()
                    except Exception:
                        pass
                    if nm:
                        # name() returns leaf name; prefer leaf for fuzzy matching against missing_token
                        leaf = nm.split(".")[-1]
                        siblings.append(leaf)
                if siblings:
                    break
            except Exception:
                continue
    siblings = sorted(set(siblings))
    fuzzy = difflib.get_close_matches(missing_token, siblings, n=5, cutoff=0.5)
    return {
        "found": False,
        "signal": sig_name,
        "found_up_to": found_up_to or "(nothing — top-level missing)",
        "first_missing": missing_token,
        "first_missing_at_index": breakpoint_idx,
        "siblings_count": len(siblings),
        "siblings_sample": siblings[:30],
        "fuzzy_suggestions": fuzzy,
    }


def cmd_find_signal(req):
    scope   = req.get("scope", "")
    pattern = req.get("pattern", "*")
    if not scope:
        return _err("'scope' is required")
    try:
        hdl_list = lang.find_signal_wildcard(scope, [pattern])
    except Exception as e:
        return _err(f"find_signal_wildcard failed: {e}")
    results = []
    for h in sorted(hdl_list, key=lambda x: (x.full_name() or "")):
        results.append(_sig_info(h))
    return {"signals": results}


# DEPRECATED: cmd_trace_driver and cmd_trace_load are commented out.
# They used is_pass_thr=False with manual recursion, which is:
#   1. Shallow — each depth level only crosses one module boundary
#   2. Prone to fan-out explosion — depth=10 can produce 200k+ results
#   3. Poor constant detection — name-based cycle detection fails for constants
#
# Use eval instead with:
#   - lang.trace_driver_dump2(sig, buf, is_pass_thr=True)  — structured text, deep trace
#   - lang.trace_driver_by_hdl2(hdl, True, None, trc_opt)  — handle list, fastest
#   - lang.trace_load_dump2(sig, buf, is_pass_thr=True)    — same for loads
#
# See SKILL.md eval section for ready-to-use templates.
#
# def cmd_trace_driver(req): ...
# def cmd_trace_load(req): ...


def cmd_get_module_ports(req):
    """
    List all port signals of the given scope (instance) using find_signal_wildcard.
    Signals with direction info are tagged; direction detection is best-effort.
    """
    scope = req.get("scope", "")
    if not scope:
        return _err("'scope' is required")
    try:
        hdls = lang.find_signal_wildcard(scope, ["*"])
    except Exception as e:
        return _err(f"find_signal_wildcard failed: {e}")

    ports = []
    for h in sorted(hdls, key=lambda x: (x.full_name() or "")):
        info = _sig_info(h, include_dir=True)
        ports.append(info)
    return {"scope": scope, "ports": ports, "count": len(ports)}


def cmd_get_src_file(req):
    sig_name = req.get("signal", "")
    if not sig_name:
        return _err("'signal' is required")
    hdl = lang.handle_by_name(sig_name, None)
    if hdl is None:
        return _err(f"signal not found: {sig_name!r}")
    info = _sig_info(hdl)
    return {"signal": sig_name, "src_file": info["src_file"], "src_line": info["src_line"]}


def _load_hier_csv(scope: str) -> list:
    """
    Dump hierarchy CSV for scope (skipping lib cells) and parse it.
    Caches result in _hier_csv_cache keyed by scope.
    Returns list of dicts with keys: HierarchyPath, ModuleName, PortName,
    Direction, Width, SrcFilePath, SrcLine  (columns from npi_export.py format).
    """
    global _hier_csv_cache
    key = scope or "__top__"
    if key in _hier_csv_cache:
        return _hier_csv_cache[key]

    from pynpi.lang_l1 import hier_tree_dump_csv as _dump_csv

    tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False, prefix="npi_hier_")
    tmp_path = tmp.name
    tmp.close()

    try:
        print(f"[hier_csv] Dumping hierarchy CSV for scope={scope!r} → {tmp_path}",
              file=sys.stderr, flush=True)
        root = scope if scope else ""
        _dump_csv(root, tmp_path, True)   # True = skip lib cells

        rows = []
        with open(tmp_path, newline="", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(dict(row))
        print(f"[hier_csv] Parsed {len(rows)} rows", file=sys.stderr, flush=True)
        _hier_csv_cache[key] = rows
        return rows
    except Exception as e:
        print(f"[hier_csv] Error: {e}", file=sys.stderr, flush=True)
        _hier_csv_cache[key] = []
        return []
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def cmd_get_filelist(req):
    """
    Return unique RTL source file paths under scope.
    use_csv=true (default): uses hier_tree_dump_csv for full accuracy.
    use_csv=false: fast fallback using find_signal_wildcard sampling.
    """
    scope    = req.get("scope", "")
    use_csv  = req.get("use_csv", True)
    files    = set()

    if use_csv:
        rows = _load_hier_csv(scope)
        for row in rows:
            f = row.get("SrcFilePath", "").strip()
            if f and f != "N/A" and f != "":
                files.add(f)
    else:
        # fast fallback: sample signals, collect their source files
        try:
            hdls = lang.find_signal_wildcard(scope or "tb", ["*"])
            for h in hdls:
                try:
                    f = h.file_name() or ""
                    if f:
                        files.add(f)
                except Exception:
                    pass
        except Exception as e:
            return {"error": f"find_signal_wildcard failed: {e}"}

    return {"files": sorted(files), "count": len(files)}


def cmd_hier_search(req):
    """
    Find all instances of a given module name under scope.
    Uses hier_tree_dump_csv (cached). Returns list of instance paths + src info.
    """
    module_name = req.get("module", "")
    scope       = req.get("scope", "")
    max_results = int(req.get("max_results", 200))

    if not module_name:
        return {"error": "'module' is required"}

    rows = _load_hier_csv(scope)

    instances = []
    seen_paths = set()
    for row in rows:
        m = row.get("ModuleName", "").strip()
        if m.lower() == module_name.lower():
            path = row.get("HierarchyPath", "").strip()
            if path in seen_paths:
                continue
            seen_paths.add(path)
            instances.append({
                "path":     path,
                "src_file": row.get("SrcFilePath", "").strip(),
                "src_line": row.get("SrcLine", "").strip(),
            })
            if len(instances) >= max_results:
                break

    return {
        "module":    module_name,
        "scope":     scope,
        "instances": instances,
        "total":     len(instances),
        "truncated": len(instances) >= max_results,
    }


def cmd_eval(req):
    """
    Execute arbitrary Python code in the loaded design context.

    The code has access to `lang`, `npisys`, `json` in its global scope.
    Set `_result` to the value you want returned (must be JSON-serializable).

    Request:
      {"cmd": "eval", "code": "python code string", "timeout": 30}

    Response:
      {"result": <value of _result>}
      or {"error": "...", "traceback": "..."}

    Example:
      {"cmd": "eval", "code": "_result = lang.handle_by_name('tb.dut.clk', None) is not None"}
      → {"result": true}
    """
    code = req.get("code", "")
    if not code:
        return _err("'code' is required")

    exec_globals = {
        "lang": lang,
        "npisys": npisys,
        "sdb": sdb,           # may be None on Verdi versions that removed pynpi.sdb
        "text": text,
        "waveform": waveform,
        "json": json,
        "os": os,
        "_result": None,
    }
    try:
        exec(code, exec_globals)
        result = exec_globals.get("_result", None)
        return {"result": result}
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


# ── dispatch table ────────────────────────────────────────────────────────────

HANDLERS = {
    "ping":             cmd_ping,
    "handle_by_name":   cmd_handle_by_name,
    "diagnose_hier":    cmd_diagnose_hier,
    "find_signal":      cmd_find_signal,
    # "trace_driver" and "trace_load" removed — use eval with
    # lang.trace_driver_dump2 / lang.trace_driver_by_hdl2(is_pass_thr=True) instead.
    "get_src_file":     cmd_get_src_file,
    "get_filelist":     cmd_get_filelist,
    "hier_search":      cmd_hier_search,
    "get_module_ports": cmd_get_module_ports,
    "eval":             cmd_eval,
}


# ── main ─────────────────────────────────────────────────────────────────────

def _resolve_daidir(arg_daidir: str) -> str:
    """Resolve daidir: explicit arg > auto_daidir() > error."""
    if arg_daidir:
        # Support pointer file (like build_size.vcs_sim_exe.daidir)
        if os.path.isfile(arg_daidir):
            try:
                content = open(arg_daidir).read().split()
                candidate = content[-1] if content else ""
                if candidate and os.path.isdir(candidate):
                    return candidate
            except Exception:
                pass
        if os.path.isdir(arg_daidir):
            return arg_daidir
        return arg_daidir  # let NPI report the error
    detected = auto_daidir()
    return detected


# ── TCP transport helpers (used only when --transport tcp / NPI_TRANSPORT=tcp) ──
#
# WHY THIS EXISTS (for later study):
# AF_UNIX mode is the default and covers 95% of the use case (same-host Claude
# session ↔ daidir on local NFS). TCP mode exists so the daemon can be bsub'd
# to a beefy LSF exec host — where daidir's 15-40 GB RSS fits — while clients
# on any other host (login boxes, other LSF jobs, future Claude sessions on
# different machines) can still connect over the cluster TCP network.
#
# Trade-offs we ACCEPT to enable cross-host:
#   1) zero-config disappears: TCP needs port allocation + hostname + a
#      rendezvous file on shared FS so clients can find <host:port>.
#   2) kernel-enforced auth disappears: AF_UNIX uses chmod 0600 (kernel checks
#      uid); TCP has no such concept, so we hand-roll a 128-bit hex token.
#      Without the token, server drops the connection before any handler runs.
#   3) latency: ~50 µs (AF_UNIX) → ~150 µs (TCP loopback) / ~300 µs (cross-host
#      gigabit). Negligible for NPI workloads (queries are 1 ms - 2 s).
#
# Threat model: the rendezvous file is chmod 0600 (owner read/write only). It
# lives in the test runout directory (cwd at daemon start) — that runout is
# already NFS-resident and access-controlled by the workspace owner. Only YOU
# (same uid on any cluster host) can read the token. Without the token, an
# attacker on the same network sees only "Auth failed" — eval / handlers are
# never reached. Token is regenerated on every daemon start, so a leaked old
# token is invalidated immediately.
#
# Liveness across hosts: PID check (kill -0) doesn't work across hosts. We
# rely on TCP connect-with-timeout itself as the authoritative liveness probe.
# On the client side: if connect refused / times out, treat endpoint as stale.

# ── Idle auto-shutdown (transport-agnostic) ──────────────────────────────────
#
# Why: an LSF-bsub'd daemon eats 15-40 GB RSS. If the user (or AI session)
# walks away without sending {"cmd":"shutdown"}, that memory stays pinned until
# the LSF wall-time kills it (regr_high has no wall limit → potentially weeks).
# Auto-shutdown reaps the daemon after $NPI_IDLE_TIMEOUT_HOUR hours of zero
# client activity (default 24h = 1 day).
#
# Activity definition: a request has been received AND no handler is currently
# running. A long-running query (e.g. trace_driver_dump2 that takes minutes)
# does NOT count as idle — _active_requests stays > 0 throughout.
#
# Both _serve_socket (AF_UNIX) and _serve_tcp (AF_INET) wire in begin/end_request
# calls around their handler dispatch, and spin up _idle_watchdog_loop as a
# daemon thread.

_activity_lock = threading.Lock()
_last_activity_ts: float = time.time()
_active_requests: int = 0


def _idle_timeout_sec() -> int:
    """Idle shutdown threshold in seconds. Set via $NPI_IDLE_TIMEOUT_HOUR.

    Default 24 hours (1 day). Set to 0 to disable auto-shutdown entirely
    (useful for interactive dev sessions where you want the daemon to stay
    until killed manually).
    """
    raw = os.environ.get("NPI_IDLE_TIMEOUT_HOUR", "24")
    try:
        h = float(raw)
    except ValueError:
        print(f"[NPI] Warning: invalid NPI_IDLE_TIMEOUT_HOUR={raw!r}, using 24h",
              file=sys.stderr, flush=True)
        h = 24.0
    return 0 if h <= 0 else int(h * 3600)


def _begin_request():
    """Mark the start of a request; resets idle clock and pins it."""
    global _last_activity_ts, _active_requests
    with _activity_lock:
        _last_activity_ts = time.time()
        _active_requests += 1


def _end_request():
    """Mark the end of a request; unpins the idle clock."""
    global _active_requests
    with _activity_lock:
        if _active_requests > 0:
            _active_requests -= 1


def _idle_watchdog_loop(shutdown_evt: "threading.Event", label: str):
    """Run as daemon thread; set shutdown_evt when idle past threshold.

    Polling cadence: every 5% of the timeout, clamped to [30s, 5min].
    A 24h timeout polls every ~72s — plenty fine-grained, near-zero cost.
    """
    timeout = _idle_timeout_sec()
    if timeout == 0:
        print(f"[NPI] Idle auto-shutdown: DISABLED ({label})",
              file=sys.stderr, flush=True)
        return
    print(f"[NPI] Idle auto-shutdown: after {timeout/3600:.1f}h "
          f"of no activity ({label})", file=sys.stderr, flush=True)
    poll_sec = max(30.0, min(timeout / 20.0, 300.0))
    while not shutdown_evt.is_set():
        # wait() returns True if evt was set during sleep → quick exit
        if shutdown_evt.wait(poll_sec):
            return
        with _activity_lock:
            idle_for = time.time() - _last_activity_ts
            active = _active_requests
        if active > 0:
            continue
        if idle_for < timeout:
            continue
        print(f"[NPI] Idle timeout fired: {idle_for/3600:.2f}h idle "
              f">= {timeout/3600:.1f}h threshold; shutting down ({label})",
              file=sys.stderr, flush=True)
        shutdown_evt.set()
        return


def _default_transport() -> str:
    """Auto-detect default transport based on LSF job context.

    Rule:
      - Inside an LSF job ($LSB_JOBID is set) → 'unix' (this is your dedicated
        exec host; AF_UNIX is fine and zero-config).
      - Otherwise (login host, devsrv, container, etc.) → 'tcp' (multiple
        sessions / hosts may want to share the daemon).

    Override: --transport flag > $NPI_TRANSPORT env > this auto-default.
    """
    return "unix" if os.environ.get("LSB_JOBID") else "tcp"


def _default_rendezvous_path(daidir: str) -> str:
    """Default rendezvous file path: <cwd>/.npi_server_<user>_<md5>.endpoint.

    Convention follows fsdb_skill: the daemon is launched from the test runout
    directory (NFS-resident, workspace-scoped), and the rendezvous file lives
    right there. Clients invoked from the same runout find it via the same
    derivation. If client and daemon end up in different cwds, the client must
    pass --rendezvous PATH explicitly (or set $NPI_RENDEZVOUS_PATH).

    Override priority:
        --rendezvous flag  >  $NPI_RENDEZVOUS_PATH (full file path)  >  cwd default

    Dot-prefixed filename so it doesn't clutter `ls` in the runout.
    md5(daidir)[:12] in the name still guarantees one daemon per daidir per user
    if the user runs multiple daemons in the same cwd.
    """
    import hashlib as _hashlib
    abs_d = os.path.abspath(daidir) if daidir else "default"
    h = _hashlib.md5(abs_d.encode()).hexdigest()[:12]
    user = os.environ.get("USER", "user")
    return os.path.join(os.getcwd(), f".npi_server_{user}_{h}.endpoint")


def _gen_auth_token() -> str:
    """128-bit random hex token. New token on every daemon start.

    Why per-start: invalidates any leaked old token the instant the daemon
    restarts. Clients always re-read the endpoint file before connecting, so
    they pick up the fresh token automatically.
    """
    import secrets
    return secrets.token_hex(16)


def _write_rendezvous_atomic(path: str, hostname: str, port: int, token: str):
    """Atomic endpoint write: tmp file + rename. Format (one field per line):

        <hostname>
        <port>
        <pid>
        <token>

    Atomic via tmp+rename so clients polling the file never see a half-written
    hostname or empty token. chmod 0600 because the token = arbitrary `eval`
    code execution under your uid.
    """
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(f"{hostname}\n{port}\n{os.getpid()}\n{token}\n")
    os.chmod(tmp, 0o600)
    os.rename(tmp, path)


def _serve_tcp(rendezvous_path: str, daidir: str, bind_host: str = "0.0.0.0"):
    """Daemon mode over AF_INET (TCP). See module header comments for rationale.

    Lifecycle:
      1. socket(AF_INET) + bind(bind_host, 0) — port 0 = let kernel pick a free
         ephemeral port (avoids hardcoded port collisions on shared LSF hosts).
      2. Generate auth_token, write <rendezvous_path> with host:port:pid:token.
      3. accept() loop → each conn → thread → REQUIRES first message to be
         {"cmd":"auth","token":"<token>"}, else connection is dropped before
         any handler runs (no daidir leak, no eval exposure).
      4. On shutdown: unlink rendezvous file so next client knows daemon is gone.

    NOTE — this duplicates a lot of _serve_socket's command loop on purpose.
    Keeping the AF_UNIX path completely untouched and the TCP path readable in
    isolation makes the TCP code easier to audit when revisiting later.
    If we ever clean up: extract `_run_handler_loop(conn, lock, evt, auth=None)`
    as a module-level function and have both _serve_socket and _serve_tcp call it.
    """
    import socket as _socket
    import threading

    auth_token = _gen_auth_token()

    srv_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    # SO_REUSEADDR lets us re-bind quickly after a crash (skip TIME_WAIT wait).
    # Safe with port 0 — kernel picks fresh port each time, no risk of stealing
    # someone else's port. Do NOT enable SO_REUSEPORT (would let two daemons
    # bind the same port, which we explicitly do NOT want — single daemon per
    # daidir is enforced via the rendezvous file).
    srv_sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    srv_sock.bind((bind_host, 0))
    actual_port = srv_sock.getsockname()[1]
    srv_sock.listen(32)
    srv_sock.settimeout(1.0)   # so accept() returns periodically and we can poll shutdown_evt

    # FQDN (not gethostname()): clients on other hosts need a fully-qualified
    # name to resolve us. Short hostname may be unresolvable outside the local
    # subnet — getfqdn() asks DNS for the canonical name.
    hostname = _socket.getfqdn()

    _write_rendezvous_atomic(rendezvous_path, hostname, actual_port, auth_token)
    print(f"[NPI] TCP daemon listening on {hostname}:{actual_port} "
          f"(rendezvous={rendezvous_path})", file=sys.stderr, flush=True)
    print(f"[NPI] Auth token: {auth_token[:8]}... (full token in endpoint file)",
          file=sys.stderr, flush=True)

    _handler_lock = threading.Lock()
    _shutdown_evt = threading.Event()
    _client_seq = [0]
    _client_seq_lock = threading.Lock()

    def _handle_client(conn, addr, cid):
        try:
            # ── auth gate ──────────────────────────────────────────────────
            # Before sending "ready", require the client to send
            # {"cmd":"auth","token":"..."} matching auth_token. On failure we
            # send a one-line error and drop the connection — handlers never
            # run, daidir is not revealed.
            #
            # 10s timeout for the auth message so a stalled / scanning client
            # can't tie up the thread forever.
            conn.settimeout(10.0)
            auth_buf = b""
            while b"\n" not in auth_buf:
                try:
                    chunk = conn.recv(4096)
                except (_socket.timeout, OSError):
                    print(f"[NPI] Client #{cid} from {addr}: auth timeout",
                          file=sys.stderr, flush=True)
                    return
                if not chunk:
                    return
                auth_buf += chunk
                if len(auth_buf) > 16384:
                    conn.sendall((json.dumps({"error": "Auth payload too large"}) + '\n').encode())
                    return
            # Carry over anything past the auth newline — if a future client
            # pipelines `auth\n<cmd>\n` in one sendall, the bytes after the
            # first \n are real command data and must NOT be discarded.
            auth_line, auth_residual = auth_buf.split(b"\n", 1)
            try:
                first = json.loads(auth_line.decode().strip())
            except json.JSONDecodeError:
                conn.sendall((json.dumps({"error": "First message must be JSON {cmd:auth,token:...}"}) + '\n').encode())
                return
            if first.get("cmd") != "auth" or first.get("token") != auth_token:
                conn.sendall((json.dumps({"error": "Auth failed"}) + '\n').encode())
                print(f"[NPI] Client #{cid} from {addr}: AUTH FAILED, dropping",
                      file=sys.stderr, flush=True)
                return

            # Auth OK — restore blocking mode and announce ready.
            conn.settimeout(None)
            conn.sendall((json.dumps({"status": "ready", "daidir": daidir}) + '\n').encode())

            # ── normal command loop (mirrors _serve_socket; kept verbatim for
            #     readability — refactor candidate, see NOTE above) ───────────
            # Seed with any bytes already received after the auth newline so
            # pipelined first-command does not get dropped.
            buf = auth_residual
            while not _shutdown_evt.is_set():
                try:
                    chunk = conn.recv(65536)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip().decode()
                    if not line:
                        continue
                    try:
                        req = json.loads(line)
                    except json.JSONDecodeError as e:
                        conn.sendall((json.dumps({"error": f"JSON parse: {e}"}) + '\n').encode())
                        continue
                    cmd = req.get("cmd", "")
                    if cmd == "quit":
                        conn.sendall((json.dumps({"status": "quit"}) + '\n').encode())
                        return
                    if cmd == "shutdown":
                        conn.sendall((json.dumps({"status": "shutdown"}) + '\n').encode())
                        _shutdown_evt.set()
                        return
                    handler = HANDLERS.get(cmd)
                    if handler is None:
                        conn.sendall((json.dumps({"error": f"Unknown cmd: {cmd!r}. Available: {sorted(HANDLERS)}"}) + '\n').encode())
                        continue
                    # Wrap dispatch with idle-watchdog activity tracking.
                    # _begin_request resets the idle clock AND pins it (long
                    # handlers can't trigger idle timeout while running).
                    _begin_request()
                    try:
                        t_wait = time.time()
                        with _handler_lock:
                            wait_s = time.time() - t_wait
                            t0 = time.time()
                            try:
                                result = handler(req)
                            except Exception as e:
                                result = {"error": f"Handler exception: {e}",
                                          "traceback": traceback.format_exc()}
                            elapsed = time.time() - t0
                        if _log_timing:
                            result["_elapsed_s"] = round(elapsed, 3)
                            if wait_s > 1.0:
                                result["_lock_wait_s"] = round(wait_s, 3)
                        conn.sendall((json.dumps(result) + '\n').encode())
                    finally:
                        _end_request()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            print(f"[NPI] Client #{cid} thread error: {e}", file=sys.stderr, flush=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass
            print(f"[NPI] Client #{cid} disconnected", file=sys.stderr, flush=True)

    # Spin up the idle watchdog (no-op if NPI_IDLE_TIMEOUT_HOUR=0)
    threading.Thread(target=_idle_watchdog_loop,
                     args=(_shutdown_evt, "tcp"), daemon=True).start()

    try:
        while not _shutdown_evt.is_set():
            try:
                conn, addr = srv_sock.accept()
            except _socket.timeout:
                continue
            except OSError:
                break
            with _client_seq_lock:
                _client_seq[0] += 1
                cid = _client_seq[0]
            print(f"[NPI] Client #{cid} connected from {addr}",
                  file=sys.stderr, flush=True)
            t = threading.Thread(target=_handle_client, args=(conn, addr, cid),
                                 name=f"npi-tcp-{cid}", daemon=True)
            t.start()
    finally:
        srv_sock.close()
        try:
            if os.path.exists(rendezvous_path):
                os.unlink(rendezvous_path)
        except OSError:
            pass
        npisys.end()
        print("[NPI] TCP daemon shutdown complete.", file=sys.stderr, flush=True)


def _serve_socket(sock_path: str, daidir: str):
    """Daemon mode: bind Unix socket, serve clients **concurrently**.

    Each accepted connection runs in its own daemon thread, but every
    handler() call is serialized through `_handler_lock` because pynpi /
    Verdi KDB hold global state and are NOT thread-safe.

    Effect: multiple subagents can hold connections simultaneously and
    issue queries; the queries themselves still run one at a time, but
    no client gets stuck in OS listen-backlog waiting for a peer's whole
    session to end.

    Stays alive after client disconnect. Use {"cmd":"shutdown"} to stop."""
    import socket as _socket
    import threading

    if os.path.exists(sock_path):
        os.unlink(sock_path)
    srv_sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    srv_sock.bind(sock_path)
    os.chmod(sock_path, 0o600)
    srv_sock.listen(32)
    srv_sock.settimeout(1.0)  # so we can poll _shutdown_evt periodically
    print(f"[NPI] Daemon listening on {sock_path} (multi-threaded)",
          file=sys.stderr, flush=True)

    _handler_lock = threading.Lock()        # serializes all handler() calls
    _shutdown_evt = threading.Event()
    _client_seq = [0]                       # mutable counter for client IDs
    _client_seq_lock = threading.Lock()

    def _handle_client(conn, cid):
        try:
            conn.sendall((json.dumps({"status": "ready", "daidir": daidir}) + '\n').encode())
            buf = b""
            cmd = ""
            while not _shutdown_evt.is_set():
                try:
                    chunk = conn.recv(65536)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip().decode()
                    if not line:
                        continue
                    try:
                        req = json.loads(line)
                    except json.JSONDecodeError as e:
                        conn.sendall((json.dumps({"error": f"JSON parse: {e}"}) + '\n').encode())
                        continue
                    cmd = req.get("cmd", "")
                    if cmd == "quit":
                        conn.sendall((json.dumps({"status": "quit"}) + '\n').encode())
                        return
                    if cmd == "shutdown":
                        conn.sendall((json.dumps({"status": "shutdown"}) + '\n').encode())
                        _shutdown_evt.set()
                        return
                    handler = HANDLERS.get(cmd)
                    if handler is None:
                        conn.sendall((json.dumps({"error": f"Unknown cmd: {cmd!r}. Available: {sorted(HANDLERS)}"}) + '\n').encode())
                        continue
                    # Wrap dispatch with idle-watchdog activity tracking.
                    _begin_request()
                    try:
                        # Serialize all handler invocations: pynpi/KDB is not thread-safe.
                        t_wait = time.time()
                        with _handler_lock:
                            wait_s = time.time() - t_wait
                            t0 = time.time()
                            try:
                                result = handler(req)
                            except Exception as e:
                                result = {"error": f"Handler exception: {e}",
                                          "traceback": traceback.format_exc()}
                            elapsed = time.time() - t0
                        if _log_timing:
                            result["_elapsed_s"] = round(elapsed, 3)
                            if wait_s > 1.0:
                                result["_lock_wait_s"] = round(wait_s, 3)
                        conn.sendall((json.dumps(result) + '\n').encode())
                    finally:
                        _end_request()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            print(f"[NPI] Client #{cid} thread error: {e}", file=sys.stderr, flush=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass
            print(f"[NPI] Client #{cid} disconnected", file=sys.stderr, flush=True)

    # Spin up the idle watchdog (no-op if NPI_IDLE_TIMEOUT_HOUR=0)
    threading.Thread(target=_idle_watchdog_loop,
                     args=(_shutdown_evt, "unix"), daemon=True).start()

    try:
        while not _shutdown_evt.is_set():
            try:
                conn, _addr = srv_sock.accept()
            except _socket.timeout:
                continue
            except OSError:
                break
            with _client_seq_lock:
                _client_seq[0] += 1
                cid = _client_seq[0]
            print(f"[NPI] Client #{cid} connected", file=sys.stderr, flush=True)
            t = threading.Thread(target=_handle_client, args=(conn, cid),
                                 name=f"npi-client-{cid}", daemon=True)
            t.start()
    finally:
        srv_sock.close()
        if os.path.exists(sock_path):
            os.unlink(sock_path)
        npisys.end()
        print("[NPI] Daemon shutdown complete.", file=sys.stderr, flush=True)


def main():
    ap = argparse.ArgumentParser(
        description="npi_rtl_server — General-purpose NPI RTL analysis persistent server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--daidir", default="",
                    help="Path to vcs_sim_exe.daidir or build_size pointer file "
                         "(auto-detected from cwd if omitted)")
    ap.add_argument("--top", default="",
                    help="Top module override (passed to load_design)")
    ap.add_argument("--log-timing", action="store_true",
                    help="Log elapsed time for each query to stderr and include _elapsed_s in response")
    ap.add_argument("--socket", default="",
                    help="Daemon mode (AF_UNIX): bind Unix socket at this path")
    # ── TCP transport (opt-in; see _serve_tcp docstring) ──
    # Selection priority: --transport flag > $NPI_TRANSPORT env > default 'unix'
    ap.add_argument("--transport", choices=["unix", "tcp"], default=None,
                    help="Transport: 'unix' (AF_UNIX same-host) or 'tcp' "
                         "(AF_INET cross-host). "
                         "Default auto-detected: 'unix' inside an LSF job "
                         "($LSB_JOBID set), 'tcp' on login hosts. "
                         "Override: --transport flag > $NPI_TRANSPORT env > auto.")
    ap.add_argument("--rendezvous", default="",
                    help="TCP mode only: full path to endpoint file (host:port:pid:token). "
                         "Must be on shared NFS so cross-host clients can read it. "
                         "Default: <cwd>/.npi_server_<user>_<md5(daidir)>.endpoint "
                         "(override via $NPI_RENDEZVOUS_PATH env).")
    ap.add_argument("--bind-host", default="0.0.0.0",
                    help="TCP mode only: interface to bind. Default 0.0.0.0 (all). "
                         "Use 'localhost' to restrict to same-host (then there's no "
                         "reason not to use --socket / AF_UNIX instead).")
    args = ap.parse_args()

    # Resolve transport: explicit flag > $NPI_TRANSPORT env > auto-detect.
    transport = (args.transport
                 or os.environ.get("NPI_TRANSPORT")
                 or _default_transport())
    if transport not in ("unix", "tcp"):
        _emit({"status": "error", "msg": f"Invalid transport: {transport!r}"})
        sys.exit(1)

    global _log_timing
    _log_timing = args.log_timing
    daidir = _resolve_daidir(args.daidir)
    if not daidir:
        _emit({"status": "error", "msg": "Cannot locate daidir. Use --daidir."})
        sys.exit(1)

    _t_start = time.time()
    print(f"[NPI] Initializing with daidir: {daidir}", file=sys.stderr, flush=True)

    load_opts = ["npi", "-dbdir", daidir, "-ssv", "-ssy", "-ssz",
                 "+disable_message+error"]
    if args.top:
        load_opts += ["-top", args.top]

    if not npisys.init(["-quiet"]):
        _emit({"status": "error", "msg": "npisys.init() failed"})
        sys.exit(1)

    if not npisys.load_design(load_opts):
        _emit({"status": "error", "msg": f"npisys.load_design() failed for {daidir}"})
        npisys.end()
        sys.exit(1)

    _load_elapsed = round(time.time() - _t_start, 1)
    print(f"[NPI] Design loaded in {_load_elapsed}s. Ready.", file=sys.stderr, flush=True)

    # ── daemon mode dispatch ──────────────────────────────────────────────
    # Three sub-modes:
    #   (a) --transport tcp                 → AF_INET, requires --rendezvous (or auto-derive)
    #   (b) --socket <path>                 → AF_UNIX, original behavior (default for transport=unix)
    #   (c) neither                         → fall through to stdin/stdout single-client mode
    if transport == "tcp":
        # Resolve rendezvous path: --rendezvous flag > $NPI_RENDEZVOUS_PATH env > cwd default.
        rendezvous = (args.rendezvous
                      or os.environ.get("NPI_RENDEZVOUS_PATH", "")
                      or _default_rendezvous_path(daidir))
        _emit({"status": "ready", "daidir": daidir, "load_elapsed_s": _load_elapsed,
               "mode": "daemon", "transport": "tcp", "rendezvous": rendezvous,
               "bind_host": args.bind_host})
        _serve_tcp(rendezvous, daidir, bind_host=args.bind_host)
        return

    # transport == "unix" — preserve original AF_UNIX path completely unchanged
    if args.socket:
        _emit({"status": "ready", "daidir": daidir, "load_elapsed_s": _load_elapsed,
               "mode": "daemon", "transport": "unix", "socket": args.socket})
        _serve_socket(args.socket, daidir)
        return

    # ── stdin/stdout mode: original single-client behavior ─────────────────
    _emit({"status": "ready", "daidir": daidir, "load_elapsed_s": _load_elapsed})

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as e:
            _emit({"error": f"JSON parse error: {e}", "input": raw[:120]})
            continue

        cmd = req.get("cmd", "")

        if cmd == "quit":
            _emit({"status": "quit"})
            break

        handler = HANDLERS.get(cmd)
        if handler is None:
            _emit({"error": f"Unknown cmd: {cmd!r}. Available: {sorted(HANDLERS)}"})
            continue

        t0 = time.time()
        try:
            result = handler(req)
        except Exception as e:
            result = {"error": f"Handler exception: {e}",
                      "traceback": traceback.format_exc()}
        elapsed = time.time() - t0

        if _log_timing:
            result["_elapsed_s"] = round(elapsed, 3)
            print(f"[timing] cmd={cmd!r} elapsed={elapsed:.3f}s", file=sys.stderr, flush=True)

        _emit(result)

    npisys.end()
    print("[NPI] Shutdown complete.", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
