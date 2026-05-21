#!/usr/bin/env python3
"""
debug_from_vcs_error.py — AI-in-the-Loop Zero-Knowledge Debug.

Pipeline:
    Phase 0 : runout/*.sim.ini → testdll SO → find_func_via_so.sh → exact src file path
    Phase 1 : read source code context (±N lines around error line)
    Phase 2 : Claude API → semantic analysis → {rtl_signals, chip_scope, hypothesis, npi_queries}
    Phase 2.5: user confirmation checkpoint (gvim review)
    Phase 3 : NPI server executes AI-suggested queries
    Phase 4 : Claude API analyzes NPI results → verdict / next_npi_queries
    Loop    : repeat Phase 3-4 up to MAX_ITER times until PROVED or INCONCLUSIVE

Usage:
    python3 debug_from_vcs_error.py \\
        --runout <runout_dir> \\
        --daidir <vcs_sim_exe.daidir> \\
        [--error "soc_fsdl_wrapper.cpp:121:FSDL_POLL_SMN_ADDR '...'"] \\
        [--context-lines 30] \\
        [--max-iter 5] \\
        [--skip-confirm]
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime

# ── paths ─────────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from npi_server_client import NpiServerClient

_DAIDIR_DEFAULT = (
    "/proj/<your_workspace>/<your_user>/arcadia_mid_dv2"
    "/out/linux_4.18.0_64/loveland/config/mang_uciebfm"
    "/pub/sim/exec/vcs_sim_exe.daidir"
)
_FIND_FUNC_SCRIPT = "/home/<your_user>/Scripts/find_func_via_so.sh"
_RESULTS_DIR = os.path.join(_HERE, "results")

MAX_ITER = 5
CONTEXT_LINES = 30

# ── Claude API ────────────────────────────────────────────────────────────────
try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

_MODEL = "claude-sonnet-4-6"


def _make_anthropic_client():
    """Build Anthropic client, parsing ANTHROPIC_CUSTOM_HEADERS if set."""
    kwargs = {}
    custom_headers_raw = os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "")
    if custom_headers_raw:
        headers = {}
        for entry in custom_headers_raw.split(";"):
            entry = entry.strip()
            if ":" in entry:
                k, v = entry.split(":", 1)
                headers[k.strip()] = v.strip()
        if headers:
            kwargs["default_headers"] = headers
    return anthropic.Anthropic(**kwargs)


def _extract_json(text: str, key_hint: str = "") -> str:
    """
    Extract the best JSON object from a text that may contain prose + JSON.
    If key_hint is given (e.g. 'verdict'), finds the JSON object containing that key.
    """
    # 1. Try ```json ... ``` block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # 2. Strip leading fence if present
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()

    # 3. If key_hint given, find the JSON object containing that key
    if key_hint:
        idx = text.find(f'"{key_hint}"')
        if idx >= 0:
            # Walk back to find the '{' that opens this object
            start = text.rfind("{", 0, idx)
            if start >= 0:
                return text[start:]

    # 4. Find first '{' and return from there
    m2 = re.search(r"\{", text)
    if m2:
        return text[m2.start():]

    return text


def _ai_call(system: str, user: str, max_tokens: int = 1024,
             key_hint: str = "") -> dict:
    """Call Claude API; return parsed JSON dict from response."""
    if not _ANTHROPIC_AVAILABLE:
        raise RuntimeError("anthropic SDK not installed. Run: pip install anthropic")
    client = _make_anthropic_client()
    msg = client.messages.create(
        model=_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = msg.content[0].text.strip()
    _log("AI", f"raw response ({len(text)} chars): {repr(text[:300])}")
    text = _extract_json(text, key_hint=key_hint)
    return json.loads(text)


# ── logging ───────────────────────────────────────────────────────────────────
def _log(phase: str, msg: str):
    print(f"[{phase}] {msg}", file=sys.stderr, flush=True)


# ── Phase 0: parse VCS error string ──────────────────────────────────────────
def parse_vcs_error(error_string: str) -> tuple:
    """
    Extract (cpp_basename, line_num, func_name) from VCS error strings like:
      '303775000 ERROR   soc_fsdl_wrapper.cpp:121:FSDL_POLL_SMN_ADDR ...'
      'nbio_pcie_ip.cpp:487:Check_PwrBrk_Status Fail...'
    Returns (basename, line_num, func_name).  func_name may be '' if absent.
    """
    m = re.search(r'([\w./]+\.cpp(?:\.dpl)?):(\d+):(\w+)', error_string)
    if m:
        return os.path.basename(m.group(1)), int(m.group(2)), m.group(3)
    # Fallback: no function name
    m2 = re.search(r'([\w./]+\.cpp(?:\.dpl)?):(\d+)', error_string)
    if m2:
        return os.path.basename(m2.group(1)), int(m2.group(2)), ""
    raise ValueError(
        f"Cannot extract cpp filename from error string:\n  {error_string!r}"
    )


# ── Phase 0: find SO from runout ─────────────────────────────────────────────
def find_so_from_runout(runout_dir: str) -> str:
    """
    Parse runout/*.sim.ini for '-testdll=<path>.so' and return the SO path.
    Raises FileNotFoundError if not found.
    """
    ini_files = []
    for f in os.listdir(runout_dir):
        if f.endswith("_sim.ini") or f.endswith(".sim.ini"):
            ini_files.append(os.path.join(runout_dir, f))

    for ini_path in ini_files:
        with open(ini_path) as f:
            content = f.read()
        m = re.search(r'-testdll=(\S+\.so)', content)
        if m:
            so_path = m.group(1)
            _log("Phase0", f"Found SO from {os.path.basename(ini_path)}: {so_path}")
            return so_path

    raise FileNotFoundError(
        f"No -testdll=*.so found in *.sim.ini files under {runout_dir}"
    )


# ── Phase 0: resolve source file via find_func_via_so.sh ─────────────────────
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def find_src_via_so(so_path: str, func_name: str) -> str:
    """
    Run find_func_via_so.sh to find the source file that defines func_name.
    Returns the absolute source file path, or raises RuntimeError if not found.
    """
    _log("Phase0", f"Running find_func_via_so.sh: SO={os.path.basename(so_path)}"
                   f", pattern={func_name!r}")
    result = subprocess.run(
        [_FIND_FUNC_SCRIPT, f"--binary={so_path}", f"--pattern={func_name}"],
        capture_output=True, text=True, timeout=60
    )
    output = result.stdout + result.stderr
    # Strip ANSI codes
    clean = _ANSI_RE.sub("", output)

    # Parse lines like: "FSDL_POLL_SMN_ADDR at /path/to/file.cpp:117"
    # addr2line output: "<func> at <path>:<line>" or "<func>\n<path>:<line>"
    candidates = []
    for line in clean.splitlines():
        line = line.strip()
        # Match "something at /abs/path/file.cpp:NNN"
        m = re.search(r' at (/[^\s:]+\.cpp(?:\.dpl)?)(?::(\d+))?', line)
        if m:
            path = m.group(1)
            if os.path.exists(path) and "??" not in path:
                candidates.append(path)
        # Match bare "/abs/path/file.cpp:NNN" line
        m2 = re.match(r'(/[^\s:]+\.cpp(?:\.dpl)?):(\d+)', line)
        if m2:
            path = m2.group(1)
            if os.path.exists(path) and "??" not in path:
                candidates.append(path)

    if not candidates:
        _log("Phase0", f"find_func_via_so.sh output:\n{clean[:600]}")
        raise RuntimeError(
            f"Cannot find source file for function {func_name!r} in {so_path}"
        )

    # Deduplicate, prefer the first unique match
    seen = []
    for p in candidates:
        if p not in seen:
            seen.append(p)
    src_path = seen[0]
    _log("Phase0", f"Resolved source: {src_path}")
    return src_path


# ── Phase 1: read source code context ────────────────────────────────────────
def read_with_highlight(src_path: str, line_num: int, context: int = 30) -> str:
    """Read ±context lines around line_num; mark error line with '>>>'."""
    with open(src_path) as f:
        lines = f.readlines()

    total = len(lines)
    start = max(0, line_num - context - 1)
    end   = min(total, line_num + context)

    result_lines = []
    for i, ln in enumerate(lines[start:end], start=start + 1):
        marker = ">>>" if i == line_num else "   "
        result_lines.append(f"{marker} {i:5d}: {ln.rstrip()}")
    return "\n".join(result_lines)


# ── Phase 2: AI semantic analysis ─────────────────────────────────────────────
_PHASE2_SYSTEM = """\
You are an RTL DV (Design Verification) expert for AMD GPU chips.
Given a VCS simulation error and the surrounding C++ DV test code, analyze:
1. Which RTL signal(s) this code is checking (give full hierarchy paths like
   tb.substrate.bridge.rdl_mid0.CHIP_MID.nbif_ss0.foo.BAR)
2. What the chip_scope is (prefix up to and including CHIP_MID)
3. Your hypothesis about the root cause
4. A prioritized list of NPI queries to run

IMPORTANT RULES for NPI queries:
- NEVER use find_signal on large scopes (e.g. nbif_ss0) — it times out (>120s).
  Use handle_by_name with exact full signal paths instead.
- Start with handle_by_name to verify the signal exists, then trace_driver/trace_load.
- trace_driver / trace_load: depth 4-6.
- Provide at most 6 npi_queries total.

Return ONLY valid JSON with no extra text:
{
  "rtl_signals": ["<full hierarchy path>", ...],
  "chip_scope": "tb.substrate.bridge.rdl_mid0.CHIP_MID",
  "hypothesis": "<concise root-cause hypothesis>",
  "npi_queries": [
    {"cmd": "handle_by_name", "signal": "<full path>"},
    {"cmd": "trace_driver", "signal": "<full path>", "depth": 6},
    {"cmd": "trace_load",   "signal": "<full path>", "depth": 4}
  ]
}
"""


def phase2_analyze(error_string: str, src_path: str, line_num: int,
                   code_context: str) -> dict:
    _log("Phase2", "Calling Claude API for semantic analysis ...")
    user = (
        f"VCS Error: {error_string}\n"
        f"Source file: {src_path}:{line_num}\n\n"
        f"Code context (line {line_num} marked with '>>>'):\n"
        f"{code_context}"
    )
    result = _ai_call(_PHASE2_SYSTEM, user, max_tokens=2048)
    _log("Phase2", f"hypothesis: {result.get('hypothesis','?')}")
    _log("Phase2", f"rtl_signals: {result.get('rtl_signals',[])}")
    _log("Phase2", f"npi_queries: {len(result.get('npi_queries',[]))} planned")
    return result


# ── Phase 2.5: user confirmation checkpoint ───────────────────────────────────
def pause_for_confirmation(ai_response: dict, skip: bool = False,
                           tmp_file: str = "/tmp/debug_phase2_review.txt"):
    """Write AI analysis to tmp file, open gvim for review, then ask y/N."""
    with open(tmp_file, "w") as f:
        f.write("=== AI Analysis (Phase 2 Result) ===\n\n")
        f.write(f"Hypothesis:\n  {ai_response.get('hypothesis', '')}\n\n")
        f.write(f"chip_scope: {ai_response.get('chip_scope', '')}\n\n")
        f.write("RTL Signals:\n")
        for s in ai_response.get("rtl_signals", []):
            f.write(f"  {s}\n")
        f.write("\nPlanned NPI Queries:\n")
        for q in ai_response.get("npi_queries", []):
            f.write(f"  {json.dumps(q)}\n")
        f.write("\n--- Close this window to proceed to Phase 3 (NPI trace) ---\n")
        f.write("--- Or Ctrl+C in terminal to abort ---\n")

    if not skip:
        _log("Phase2.5", f"Opening gvim for review: {tmp_file}")
        try:
            subprocess.run(["gvim", "--nofork", tmp_file], check=False)
        except FileNotFoundError:
            _log("Phase2.5", "gvim not available; showing review in terminal:")
            with open(tmp_file) as fh:
                print(fh.read())

        ans = input("[Phase2.5] Proceed to Phase 3 (NPI trace)? [y/N]: ").strip().lower()
        if ans != "y":
            print("Aborted by user.")
            sys.exit(0)
    else:
        _log("Phase2.5", "Skipping confirmation (--skip-confirm).")
        with open(tmp_file) as fh:
            print(fh.read(), file=sys.stderr)

    _log("Phase2.5", "Confirmed. Proceeding to Phase 3 ...")


# ── Phase 3: NPI execution ────────────────────────────────────────────────────
def phase3_run_npi(srv: NpiServerClient, npi_queries: list) -> list:
    """Execute each query; return list of {query, result} dicts."""
    npi_results = []
    for i, q in enumerate(npi_queries, 1):
        _log("Phase3", f"Query {i}/{len(npi_queries)}: {json.dumps(q)}")
        try:
            result = srv.query(q, timeout=300.0)
        except Exception as e:
            result = {"error": str(e)}
        npi_results.append({"query": q, "result": result})
        _log("Phase3", f"  → keys: {list(result.keys())}")
    return npi_results


# ── Phase 4: AI verdict ───────────────────────────────────────────────────────
_PHASE4_SYSTEM = """\
You are an RTL DV (Design Verification) expert for AMD GPU chips.
You previously suggested NPI (RTL signal trace) queries. You now have the results.
Analyze the NPI trace data and determine the root cause of the VCS error.

NPI result interpretation guide:
- handle_by_name: {"found": true/false, "name": "...", "type": "..."}
- trace_driver:   {"drivers": [{"name": "...", "constant": bool, "value": "..."}, ...]}
- trace_load:     {"loads":   [{"name": "...", "type": "..."}, ...]}
- "constant": true means the signal is tied to a constant value (tie-0/tie-1)
- Signal not found (found: false) or empty lists are themselves evidence

IMPORTANT: Do NOT use find_signal on large scopes in next_npi_queries (it times out).
Use handle_by_name with exact full paths for new signals to check.

Return ONLY valid JSON with no extra text:
{
  "verdict": "PROVED" | "INCONCLUSIVE" | "NEED_MORE_TRACE",
  "root_cause": "<concise description>",
  "evidence": ["<evidence item 1>", "<evidence item 2>", ...],
  "next_npi_queries": []
}
"""


def phase4_verdict(error_string: str, phase2_result: dict,
                   npi_results: list, iteration: int) -> dict:
    _log("Phase4", f"Calling Claude API for verdict (iteration {iteration}) ...")
    user = (
        f"Original VCS Error: {error_string}\n\n"
        f"Phase 2 Analysis:\n"
        f"  hypothesis: {phase2_result.get('hypothesis','')}\n"
        f"  chip_scope: {phase2_result.get('chip_scope','')}\n\n"
        f"NPI Trace Results (iteration {iteration}):\n"
        f"{json.dumps(npi_results, indent=2)}"
    )
    result = _ai_call(_PHASE4_SYSTEM, user, max_tokens=2048, key_hint="verdict")
    _log("Phase4", f"verdict: {result.get('verdict','?')}")
    _log("Phase4", f"root_cause: {result.get('root_cause','?')}")
    return result


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="debug_from_vcs_error — AI-in-the-Loop Zero-Knowledge RTL Debug",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--runout", default="",
                    help="Runout directory (reads sim.ini for SO path and "
                         "simctrl__line_match_standard_error_regex for error)")
    ap.add_argument("--daidir", default=_DAIDIR_DEFAULT,
                    help="Path to vcs_sim_exe.daidir or build_size pointer")
    ap.add_argument("--error", default="",
                    help="VCS error string (overrides --runout auto-detection)")
    ap.add_argument("--so", default="",
                    help="Explicit path to testdll .so (overrides sim.ini lookup)")
    ap.add_argument("--context-lines", type=int, default=CONTEXT_LINES,
                    help="Lines of context around error line (default: 30)")
    ap.add_argument("--max-iter", type=int, default=MAX_ITER,
                    help="Max NPI→AI iterations (default: 5)")
    ap.add_argument("--skip-confirm", action="store_true",
                    help="Skip Phase 2.5 gvim confirmation")
    ap.add_argument("--out-dir", default=_RESULTS_DIR,
                    help="Directory for output JSON")
    args = ap.parse_args()

    # ── Get error string ──────────────────────────────────────────────────────
    error_string = args.error
    if not error_string:
        if not args.runout:
            ap.error("Provide --error or --runout")
        err_file = os.path.join(args.runout,
                                "simctrl__line_match_standard_error_regex")
        if not os.path.exists(err_file):
            err_file = os.path.join(args.runout, "simctrl_error_raw.log")
        with open(err_file) as f:
            error_string = f.read().strip().split("\n")[0]

    _log("Main", f"Error: {error_string[:120]}")

    # ── Phase 0: parse error ──────────────────────────────────────────────────
    try:
        cpp_name, line_num, func_name = parse_vcs_error(error_string)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    _log("Phase0", f"cpp={cpp_name!r}  line={line_num}  func={func_name!r}")

    # ── Phase 0: find SO ──────────────────────────────────────────────────────
    so_path = args.so
    if not so_path:
        if not args.runout:
            ap.error("Provide --so or --runout (to auto-detect SO from sim.ini)")
        try:
            so_path = find_so_from_runout(args.runout)
        except FileNotFoundError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(2)

    # ── Phase 0: resolve exact source file via addr2line ──────────────────────
    if not func_name:
        print(f"ERROR: No function name in error string; cannot use find_func_via_so.sh",
              file=sys.stderr)
        sys.exit(2)

    try:
        src_path = find_src_via_so(so_path, func_name)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    # ── Phase 1 ───────────────────────────────────────────────────────────────
    _log("Phase1", f"Reading {src_path}:{line_num} ±{args.context_lines} lines")
    try:
        code_context = read_with_highlight(src_path, line_num, args.context_lines)
    except OSError as e:
        print(f"ERROR reading source: {e}", file=sys.stderr)
        sys.exit(2)

    # ── Phase 2 ───────────────────────────────────────────────────────────────
    phase2_result = phase2_analyze(error_string, src_path, line_num, code_context)

    # ── Phase 2.5 ─────────────────────────────────────────────────────────────
    pause_for_confirmation(phase2_result, skip=args.skip_confirm)

    # ── Phase 3-4 loop ────────────────────────────────────────────────────────
    os.makedirs(args.out_dir, exist_ok=True)

    all_npi_results = []
    final_verdict = {}
    current_queries = phase2_result.get("npi_queries", [])

    _log("Main", f"Starting NPI server: {args.daidir}")
    with NpiServerClient(daidir=args.daidir, startup_timeout=240.0) as srv:
        for iteration in range(1, args.max_iter + 1):
            _log("Main", f"=== Iteration {iteration}/{args.max_iter} ===")

            if not current_queries:
                _log("Main", "No NPI queries — stopping loop.")
                break

            npi_results = phase3_run_npi(srv, current_queries)
            all_npi_results.extend(npi_results)

            verdict_result = phase4_verdict(
                error_string, phase2_result, npi_results, iteration
            )
            final_verdict = verdict_result
            verdict = verdict_result.get("verdict", "INCONCLUSIVE")

            if verdict in ("PROVED", "INCONCLUSIVE"):
                _log("Main", f"Stopping: verdict={verdict}")
                break

            current_queries = verdict_result.get("next_npi_queries", [])
            if not current_queries:
                _log("Main", "NEED_MORE_TRACE but no next_npi_queries — stopping.")
                break

    # ── Save results ──────────────────────────────────────────────────────────
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    sig_label = (phase2_result.get("rtl_signals") or ["unknown"])[0]
    sig_label = re.sub(r"[^\w]", "_", sig_label.split(".")[-1])[:30]
    out_path = os.path.join(args.out_dir, f"debug_{sig_label}_{ts}.json")

    report = {
        "timestamp": ts,
        "error_string": error_string,
        "src_path": src_path,
        "src_line": line_num,
        "so_path": so_path,
        "phase2_analysis": phase2_result,
        "all_npi_results": all_npi_results,
        "final_verdict": final_verdict,
    }
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)

    # ── Print summary ─────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("DEBUG SUMMARY")
    print("=" * 70)
    print(f"Error     : {error_string[:100]}")
    print(f"Source    : {src_path}:{line_num}")
    print(f"Hypothesis: {phase2_result.get('hypothesis','')}")
    print(f"Verdict   : {final_verdict.get('verdict','UNKNOWN')}")
    print(f"Root Cause: {final_verdict.get('root_cause','')}")
    if final_verdict.get("evidence"):
        print("Evidence  :")
        for e in final_verdict["evidence"]:
            print(f"  • {e}")
    print(f"\nFull report: {out_path}")
    print("=" * 70)

    verdict = final_verdict.get("verdict", "UNKNOWN")
    sys.exit(0 if verdict == "PROVED" else 1)


if __name__ == "__main__":
    main()
