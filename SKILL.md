---
name: npi_server
description: >
  NPI RTL connectivity, UPF/power-intent elaboration, and FSDB waveform value reading for any
  VCS-compiled chip design. Use this skill whenever the user wants to (a) trace RTL signal
  drivers or loads, check if a signal or scope exists in the VCS database, write NPI query
  scripts, run the AI-in-the-loop debug pipeline on a failing VCS test
  (debug_from_vcs_error.py), or investigate signal connectivity inside any RTL hierarchy;
  (b) audit/elaborate UPF — list power domains, dump connect_supply_net resolutions, verify
  a cell port resolves to the expected supply rail, check
  supply_set / isolation / level-shifter / retention strategies, query power_state_table
  legality. **UPF branch only when the sim is power-aware** (config name contains
  `_nlp` / `_lp` / `power_aware`, or build log has `vcs_lpmsg` / `-power=` flag, or an
  elaborated `*.upf` KDB exists); on plain RTL sim, X-prop on power-flavor signals
  (pwrbrk / pgaon / iso_ / pg_) comes from RTL driver issues (sync reset, missing
  asyncReset, unconnected wires) — do NOT invoke UPF queries; OR
  (c) read FSDB waveform values programmatically — open an FSDB via
  `pynpi.waveform.open()`, look up signals by hierarchy path with `sig_by_name`, iterate
  value transitions over a time window with `SigBasedHandle`, and extract value-at-time
  data (BinStr/Hex/Uint) without resorting to shell `fsdbextract` + VCD parse. The UPF
  queries go through `pynpi.upf` and the FSDB queries go through `pynpi.waveform` (see
  npi-api skill for both). Always prefer this skill over plain text grep when the question
  is about *what the tool actually elaborates* across multi-file UPF (golden RTL UPF →
  partition UPF → top UPF), since text grep cannot resolve aliases or cross-scope
  `connect_supply_net` chains; and prefer it over `fsdbextract` for waveform-value queries
  because NPI loads the FSDB once and answers many queries scoped by full hierarchy path.
  Also trigger when the user mentions NpiServerClient, npi_rtl_server, trace_driver,
  trace_load, handle_by_name, get_filelist, signal tracing against a `vcs_sim_exe.daidir`,
  pynpi.upf, get_power_domains, get_power_switches, supply_set, root_driver, cell-port supply
  check, UPF audit, power domain audit, connect_supply_net verification, isolation strategy
  check, retention strategy check, PST / power_state_table inspection, pynpi.waveform,
  FSDB read, FSDB waveform query, value-at-time query, signal toggle iteration, waveform
  value extraction, SigBasedHandle, sig_by_name, iter_start, programmatic FSDB read.
user-invocable: true
allowed-tools: Bash, Read, Glob, Grep, Agent
---

# NPI Server — RTL Signal Analysis Tool

## Overview

The npi_server is a persistent NPI (Novas Platform Interface) RTL analysis server that loads a VCS
simulation database (daidir) once and answers RTL signal queries instantly via JSON protocol.

It is **chip-agnostic** — point it at any `vcs_sim_exe.daidir` and it works. The examples below use
abstract placeholders like `<TB_TOP>` (your testbench top), `<CHIP_TOP>` (your DUT top), and
`<block>` (any sub-hierarchy). Replace them with the real names from your project.

**Skill files (this directory):**
| File | Purpose |
|---|---|
| `npi_rtl_server.py` | Server process (JSON stdin/stdout) |
| `npi_server_client.py` | `NpiServerClient` Python context manager + CLI |
| `debug_from_vcs_error.py` | AI-in-the-loop debug pipeline (Phase 0–4) |
| `prove_hdl_path.py` | Example one-shot prover script |
| `results/` | Output JSONs from past debug sessions |

**Configuration — set these per-project:**
- `NPI_SERVER_PATH` — directory containing `npi_rtl_server.py` (defaults to this skill's location)
- `NPI_DAIDIR` — path to your `*.daidir`. Can also be passed explicitly via `NpiServerClient(daidir=...)`.

Example values (**EXAMPLE — replace with your project's paths**):
```bash
# EXAMPLE only — replace <area>/<user>/<workspace>/<host>/<chip>/<config> with your project paths:
export NPI_SERVER_PATH=/proj/<area>/<user>/<workspace>/src/test/tools/ai_tools/dv_skills_for_each_domain/power/npi_server
export NPI_DAIDIR=/proj/<area>/<user>/<workspace>/out/<host>/<chip>/config/<config>/pub/sim/exec/vcs_sim_exe.daidir
```

---

## Filelist Cache — CHECK BEFORE STARTING ANY DAEMON

For "is file X part of this design?" / "list all `*.sv` under this design" /
"find files matching `<pattern>` in the elaborated design", **do NOT start NPI**.
A pre-built ground-truth filelist is cached at:

    ~/.cache/npi_filelist/<config_name>/filelist.txt

where `<config_name>` is the segment between `config/` and `/pub/sim/` in the
daidir path: `.../config/<CONFIG>/pub/sim/exec/vcs_sim_exe.daidir`. The cache
is the **NPI-elaborated** filelist (post dead-code-elimination) — strictly more
accurate than text greps over `verdi_with_vcs.f` / `Makefile`.

**Mandatory decision tree (run this verbatim, no improvising):**
```bash
DAIDIR=<the daidir or runout path the user gave>
CFG=$(echo "$DAIDIR" | sed -nE 's|.*/config/([^/]+)/pub/sim/.*|\1|p')
CACHE=~/.cache/npi_filelist/$CFG/filelist.txt
WARM=~/.claude/skills/npi_server/scripts/npi_filelist_warm.sh

if [[ -s $CACHE ]] && grep -q '^# status: *ok' "$CACHE"; then
    grep -E "<pattern>" "$CACHE"          # sub-second, AUTHORITATIVE
else
    nohup "$WARM" "$DAIDIR" >& /tmp/npi_warm.$CFG.log &  # fire-and-forget
    # this turn use the noisier text fallbacks:
    grep -oE '/[^[:space:]]+\.(sv|svh|v|vh|vhd)\b' \
        "$DAIDIR"/../../verdi_with_vcs.f "$DAIDIR"/../../Makefile | sort -u
    # synopsys_sim.setup 每行 `<lib> : <path>` 映射一个 worklib；source 不一定
    # 都在 top filelist (e.g. 预编进 worklib 的 die / IP)。Walk 全部 worklib
    # 的 AN.DB/make.vlogan (vlogan 自带 Makefile) 兜底。
    SETUP="$DAIDIR/../synopsys_sim.setup"
    [[ -f $SETUP ]] && awk '/^[A-Za-z_][A-Za-z0-9_]* *:/{print $3}' "$SETUP" | while read L; do
        [[ -f "$L/AN.DB/make.vlogan" ]] && grep -oE '/[^[:space:]]+\.s?v\b' "$L/AN.DB/make.vlogan"
    done | sort -u
fi
```

**Status checks** (any AI session can probe without starting NPI):
```bash
$WARM <daidir> --status   # ready / building / absent / failed
$WARM <daidir> --path     # print the cache file path
$WARM <daidir> --rebuild  # force re-warm
```

Cold build cost: **50–600 s** depending on design size (Aurora ~50 s, Arcadia ~600 s).
Warm grep: **<1 s**. **Never start `npi_rtl_server` for a filelist question
when this cache or its fallback exists.**

> ⚠️ **Scale limit**: `text.get_file_list()` does **NOT scale to multi-die top
> tree cosim** (e.g. MI450 MAAM-class designs: > 1 h cold build, 38 GB RSS,
> still didn't return). When `<daidir>/fsearch_partition_paths` shows multiple
> distinct parent `pub/sim/` paths, **don't try to build the cache** — fall back
> to grepping each parent's `Makefile` directly. **Generic source-discovery
> principle**: `synopsys_sim.setup` 每行 `<lib> : <path>` 映射一个 worklib；
> top sim 自己只编一部分 source (verdi_with_vcs.f / Makefile 列出的)，其余
> 在各预编 worklib。任一 worklib 的 `<path>/AN.DB/make.vlogan` 是 vlogan 自带
> 的结构化 Makefile，grep `\.s?v` 直接拿全部 source 绝对路径 (54k+ 量级 OK)。
> The cache works well for
> single-config or single-partitioned-parent layouts (Aurora, Arcadia mid).

---

## NPI Commands Reference

All queries are JSON objects sent to the server; all responses are JSON objects. In all examples,
treat strings like `<TB_TOP>.<CHIP_TOP>.<block>.<signal>` as placeholders for real paths in your
design (e.g. something like `top.cpu.alu.result`).

### handle_by_name — Check if a signal/scope exists
Use this as the **first step** before tracing. Always prefer this over `find_signal` on large scopes.
```json
{"cmd": "handle_by_name", "signal": "<TB_TOP>.<CHIP_TOP>.<block>.<signal>"}
```
Response: `{"found": true/false, "name": "...", "width": N, "src_file": "...", "src_line": N}`

### diagnose_hier — When `handle_by_name` returns False, call this instead of guessing
A `found: false` is **ambiguous** — the signal may genuinely not exist, OR a mid-path instance
was renamed by chip integration (e.g. IP-level `cip_nbif_t` becomes chip-level `cip_nbif_mid_t`
after die-suffix wrapping), OR the signal lives in encrypted RTL. **Do NOT guess alternate
hier names manually** — call this once:
```json
{"cmd": "diagnose_hier", "signal": "<the failing path>"}
```
Response:
```
{"found": false,
 "found_up_to": "<longest prefix that resolves>",
 "first_missing": "<the token where it broke>",
 "siblings_count": N, "siblings_sample": [...],
 "fuzzy_suggestions": ["<closest matches via difflib>"]}
```
Worked example (real arcadia trap): AI copies `cip_nbif_t.nbif_bifc_wrap...` from a fusedoc/
strap_param csv. `handle_by_name` returns `found:false`. `diagnose_hier` returns
`found_up_to=tb...CHIP_MID, first_missing=cip_nbif_t, fuzzy_suggestions=[cip_nbif_mid_t]`.
Fixed in one call instead of 5+ wrong guesses.

### ~~trace_driver / trace_load~~ — DEPRECATED, use eval instead

The built-in `trace_driver` and `trace_load` commands have been removed because they used
`is_pass_thr=False` with manual recursion, which is shallow and prone to fan-out explosion.

**Use eval with native NPI trace functions instead** — see eval section below for templates.

### get_src_file — Get RTL source file and line number
```json
{"cmd": "get_src_file", "signal": "<full path>"}
```
Response: `{"src_file": "...", "src_line": N}`
- For **module instances**: returns the module **definition** file, not the instantiation site.
  Uses `hdl.def_file()` with `hdl.file()` fallback.
- For **signals/nets**: returns the declaration file (e.g. `some_module.v:42`)

### get_filelist — List RTL source files under a scope
> ⚠️ **CURRENTLY BROKEN — handler returns `count=0` on real designs.** Root cause: `_load_hier_csv`
> uses `lang_l1.hier_tree_dump_csv` and parses it with `csv.DictReader` expecting a `SrcFilePath`
> column, but the API actually outputs an indented hierarchy tree with no header / no source-file
> column (verified across 7 Verdi versions and Synopsys-shipped demo daidir).
>
> **Use the filelist cache instead** — see `## Filelist Cache` at the top of this file.
> Text-file fallbacks (`verdi_with_vcs.f`, `Makefile`) remain valid when the cache is cold.

### hier_search — Search for signals matching a pattern (use carefully)
```json
{"cmd": "hier_search", "scope": "<TB_TOP>.<CHIP_TOP>", "pattern": "*<keyword>*", "depth": 3}
```
Response: `{"matches": ["...", ...]}`
⚠️ **Times out (>120s) on large scopes** — keep scope narrow, or use `handle_by_name` instead.

### find_signal — Find signal by name under a scope (avoid on large scopes)
```json
{"cmd": "find_signal", "scope": "<scope>", "name": "<signal_name>"}
```
⚠️ Same timeout risk as `hier_search`. Prefer `handle_by_name` with exact paths.

### get_module_ports — List ports of a module instance
```json
{"cmd": "get_module_ports", "scope": "<instance path>"}
```

### eval — Execute arbitrary pynpi code (preferred for complex queries)

⚠️ **MANDATORY: Before writing ANY eval code, invoke `/npi-api` skill first** to look up the
correct pynpi function signatures and handle methods. Do NOT guess method names — pynpi
methods have non-obvious names (e.g. `hdl.file()` not `hdl.file_name()`, `hdl.line_no()`
not `hdl.line_num()`). Getting them wrong inside `try/except` silently returns empty data.

```json
{"cmd": "eval", "code": "python code here", "timeout": 30}
```
Response: `{"result": <value of _result>}` or `{"error": "...", "traceback": "..."}`

The code runs in the loaded design context with `lang`, `npisys`, `sdb`, `json`, `os` available.
Set `_result` to the value you want returned (must be JSON-serializable).

**This is the most powerful command** — use it when the built-in commands are insufficient.

**Key handle methods (see `/npi-api` for full list):**
- `hdl.full_name()` / `hdl.name()` — signal/instance name
- `hdl.file()` / `hdl.line_no()` — instantiation/declaration source location
- `hdl.def_file()` / `hdl.def_line_no()` / `hdl.def_name()` — module definition source
- `lang.get_hdl_info(hdl)` — formatted info string: `"npiType, name, {file : line}"`
- `lang.expr_decompile(hdl)` — decompile to Verilog expression (e.g. `"'h0"` for constants)

#### Trace API comparison (choose the right one):

| API | Auto deep? | Output | Speed | Best for |
|-----|-----------|--------|-------|----------|
| `trace_driver_dump2(sig, buf, is_pass_thr=True)` | Yes | structured text with file:line | ~2s | human-readable full trace |
| `trace_driver_by_hdl2(hdl, True, None, opt)` | Yes | handle list | ~0.005s | programmatic analysis |
| `trace_driver_by_hdl2(hdl, False, None, opt)` | No (1 level) | handle list | ~0s | single-hop check |
| `trace_load_dump2(sig, buf, is_pass_thr=True)` | Yes | structured text | ~2s | downstream fan-out |
| `trace_load_by_hdl2(hdl, True, None, opt)` | Yes | handle list | fast | downstream programmatic |

**Rule: Always use `is_pass_thr=True`** (the default) for deep tracing. Never use
`is_pass_thr=False` with manual recursion — it's slower and explodes on combinational logic.

### UPF / power-intent audit (eval + `pynpi.upf`)

When the question is about UPF rather than RTL signals — "is this cell port wired to the right
supply?", "list every power_domain in this scope", "does the PST allow ALL_OFF for this cluster?",
"is there an isolation strategy at the boundary?" — the right tool is still this `eval` command,
just importing `pynpi.upf` inside the code.

The reason to use NPI here instead of grepping `.upf` files: real designs split UPF across
golden RTL UPF + partition wrappers + chip-top + generated copies. NPI **elaborates** all of
them and tells you what the tool actually sees — including resolved supply aliases, multi-scope
`connect_supply_net` chains, and cell-to-cell-abutment supplies that no text grep can follow.

**Prerequisite**: the `*.daidir` must have been built with VCS power compile flags (`-power=upfim`
+ `-upf <file>`). If `from pynpi import upf; upf.get_power_domains()` returns empty, the KDB
has no UPF — ask the user to point at a power-compiled daidir. See `npi-api` skill
`references/upf.md` "Daidir prerequisite" for the full story.

**Cell-port supply audit**: `pynpi.upf` doesn't reliably enumerate cell-port bindings. Use
`lang.trace_driver_dump2('<cell>.<PORT>', is_pass_thr=True)` and parse the first
`<2> source: <NET>, scope: <SCOPE>` line. Combine with `SupplySetHdl.ss_states()` for voltage.
See `npi-api` skill `references/upf.md` Pattern #3 + Worked Example B.

Quick example — resolve what a supply_net's root driver is:
```json
{"cmd": "eval", "code": "from pynpi import upf\nh = upf.handle_by_name('<scope>/<SUPPLY_NET>', None)\nrd = h.root_driver()\n_result = {'net': h.full_name(), 'root_driver': rd.full_name() if rd else None}", "timeout": 60}
```

#### Example: Deep trace with dump2 (human-readable, includes file:line)
```json
{"cmd": "eval", "code": "import io\nbuf = io.StringIO()\ntrc_opt = lang.TrcOption()\ntrc_opt.set_ignore_port_dir(True)\nlang.trace_driver_dump2('<TB_TOP>.<CHIP_TOP>.<block>.<signal>', buf, is_pass_thr=True, trc_opt=trc_opt)\n_result = buf.getvalue()"}
```

#### Example: Deep trace with by_hdl2 (fast, programmatic)
```json
{"cmd": "eval", "code": "hdl = lang.handle_by_name('<TB_TOP>.<CHIP_TOP>.<block>.<signal>', None)\ntrc_opt = lang.TrcOption()\ntrc_opt.set_ignore_port_dir(True)\nresults = lang.trace_driver_by_hdl2(hdl, True, None, trc_opt)\nout = []\nfor res in results:\n    stmt = lang.get_hdl_info(res.get_use_hdl())\n    sigs = []\n    for sig in res.get_sig_hdl_list():\n        fn = sig.full_name()\n        if fn is None: fn = sig.name()\n        if fn is None: fn = lang.expr_decompile(sig)\n        sigs.append({'name': fn, 'info': lang.get_hdl_info(sig)})\n    out.append({'stmt': stmt, 'sigs': sigs})\n_result = out"}
```

#### Example: Get module definition file
```json
{"cmd": "eval", "code": "hdl = lang.handle_by_name('<TB_TOP>.<CHIP_TOP>.<block_instance>', None)\n_result = {'def_file': hdl.def_file(), 'def_name': hdl.def_name(), 'inst_file': hdl.file(), 'inst_line': hdl.line_no()}"}
```

### FSDB waveform value reading (eval + `pynpi.waveform`)

When the question is about *signal values over time* — toggle order between two signals, value
of a net at a given timestamp, latency between an input transition and a downstream effect, or
whether a signal ever asserted in a given test — use `pynpi.waveform` inside `eval`. This is a
**third domain** alongside RTL tracing and UPF audit; the same `eval` channel handles all three.

The reason to use NPI here instead of shell `fsdbextract`+VCD parse: NPI loads the FSDB once and
answers many queries scoped by full hierarchy path, returning binary/hex/uint values per
transition with femtosecond timestamps. Shell extraction writes a giant VCD subset to disk and
re-parses it; pynpi.waveform reads the live FSDB directly and returns Python objects.

**Independence from daidir**: the npi server still needs to be started against some daidir, but
`waveform.open()` operates on any FSDB file path — the FSDB does not have to come from the same
test run as the daidir. Reading FSDB does not require the daidir to contain matching scopes.

**Cookbook**: see `npi-api` skill, `references/waveform.md` for the full API
(open / sig_by_name / SigBasedHandle.iter_start / VctFormat_e variants).

#### Example: dump every transition of one signal over a time window
```json
{"cmd": "eval", "code": "from pynpi import waveform\nfh = waveform.open('/path/to/dump.fsdb')\nsig = fh.sig_by_name('<TB_TOP>.<CHIP_TOP>.<block>.<signal>')\nsb = waveform.SigBasedHandle(); sb.add(sig)\nsb.iter_start(fh.min_time(), fh.max_time())\nout = []\nwhile True:\n    sid, t = sb.iter_next()\n    if sid == 0: break\n    out.append((t, sb.get_value(waveform.VctFormat_e.BinStrVal)))\nsb.iter_stop(); waveform.close(fh)\n_result = out", "timeout": 60}
```

#### Example: probe whether a signal is dumped at all
```json
{"cmd": "eval", "code": "from pynpi import waveform\nfh = waveform.open('/path/to/dump.fsdb')\nsig = fh.sig_by_name('<full.hierarchy.path>')\n_result = {'dumped': sig is not None, 'min_time': fh.min_time(), 'max_time': fh.max_time(), 'scale_unit': fh.scale_unit()}", "timeout": 30}
```

### ping — Health check
```json
{"cmd": "ping"}
```
Response: `{"pong": true}`

---

## Python Client Usage

```python
import os, sys
# Path to this skill's directory (override with NPI_SERVER_PATH if installed elsewhere)
sys.path.insert(0, os.environ.get("NPI_SERVER_PATH", "<path-to-npi_server-skill>"))
from npi_server_client import NpiServerClient

# Point at your project's daidir — read from env, or pass explicitly
DAIDIR = os.environ.get("NPI_DAIDIR", "<path-to-your-vcs_sim_exe.daidir>")

with NpiServerClient(daidir=DAIDIR, startup_timeout=240) as srv:
    # 1. Check signal exists
    r = srv.query({"cmd": "handle_by_name", "signal": "<TB_TOP>.<CHIP_TOP>.<block>.<signal>"})
    if r["found"]:
        print(f"Found: {r['name']}  src={r['src_file']}:{r['src_line']}")

    # 2. Trace drivers (use eval with dump2 for deep trace)
    r = srv.query({"cmd": "eval", "code": """
import io
buf = io.StringIO()
trc_opt = lang.TrcOption()
trc_opt.set_ignore_port_dir(True)
lang.trace_driver_dump2("<TB_TOP>.<CHIP_TOP>.<block>.<signal>", buf, is_pass_thr=True, trc_opt=trc_opt)
_result = buf.getvalue()
""", "timeout": 60}, timeout=120)
    print(r.get("result", r))
```

**Typical startup time:** 40–240s (daidir loading). Always use `startup_timeout=240`.

---

## Generating Verdi wave restore files (.rc) for the user

After tracing signals via NPI/FSDB, you'll often want to hand the user a Verdi wave layout
so they can open the same view in the GUI. The native format for this is a `.rc` file
loaded via `verdi -ssf <file.rc>` or `wvRestoreSignal <file.rc>` in a running session.

**Workflow (no script needed — synthesize the file directly):**
1. Verify every signal path with `handle_by_name` (design) and `pynpi.waveform.sig_by_name`
   (FSDB) before writing it into the file. Subagent reports use abbreviated names
   (e.g. `L0AG0` instead of the real `L0AG0_IOHUB_ctrl`); the wave file needs the actual
   elaborated net name or it silently drops the signal.
2. Convert NPI-style `.`-separated paths to `/`-separated (Verdi `.rc` declares `-d /`).
3. Refer to `references/signal.rc.template` and fill in the FSDB path + groups +
   converted signal paths. ASCII only.
4. Save next to the FSDB and tell the user to `verdi -ssf <path>` or source it.

For the **TCL injection alternative** (only when the user has a live Verdi session and
doesn't want their existing wave layout disturbed) and the 3 TCL footguns, see
`references/verdi_wave.md`.

---

## AI-in-the-Loop Debug Pipeline — debug_from_vcs_error.py

Automatically debugs a failing VCS test by:

| Phase | What happens |
|---|---|
| 0 | `runout/*.sim.ini` → `-testdll=*.so` → `find_func_via_so.sh` → `addr2line` → exact source file |
| 1 | Read source code ±30 lines around error line |
| 2 | Claude API → `{rtl_signals, chip_scope, hypothesis, npi_queries}` |
| 2.5 | gvim review (or `--skip-confirm` to bypass) |
| 3 | NPI executes suggested queries |
| 4 | Claude API verdict: `PROVED` / `INCONCLUSIVE` / `NEED_MORE_TRACE` |
| Loop | Repeat Phase 3–4 up to `--max-iter` times |

**CLI:**
```bash
cd $NPI_SERVER_PATH    # or this skill's directory

python3 debug_from_vcs_error.py \
    --runout <runout_dir> \
    --daidir <vcs_sim_exe.daidir> \
    [--error "<source_file>:<line>:<func> '<error_msg>'"] \
    [--skip-confirm] \
    [--max-iter 5]
```

**Required env vars** (typically set in your shell rc after sourcing):
```bash
ANTHROPIC_API_KEY=dummy
ANTHROPIC_BASE_URL=https://llm-api.amd.com/Anthropic
ANTHROPIC_CUSTOM_HEADERS=Ocp-Apim-Subscription-Key: $LLM_GATEWAY_KEY
```

**Tip:** The AI guesses RTL hierarchy names from the error message. For chips with non-obvious paths
or deep block hierarchies, **inject known scope hints into `--error`** (e.g. include the partial
hierarchy `<CHIP_TOP>.<known_block>` you already suspect). Without hints, the AI may give
`INCONCLUSIVE` after exhausting guesses; with hints it usually returns `PROVED` in one iteration.

---

## Gotchas

1. **Never use `find_signal` or `hier_search` on large scopes** (chip top, large IP roots, etc.) —
   times out after 120s+. Use `handle_by_name` with exact full paths.
2. **For static filelist queries, see `## Filelist Cache` at the top of this file.**
   The pre-built `~/.cache/npi_filelist/<config>/filelist.txt` is the authoritative
   ground-truth source list. Fall back to grepping `<daidir>/../../verdi_with_vcs.f`
   or `<daidir>/../../Makefile` only when the cache is absent or building. Reserve
   NPI for module → src file mapping (`lang.handle_by_name(...)` then `hdl.def_file()`)
   or `get_src_file` for a single specific signal — those are sub-ms point lookups.
3. **Phase 2 `max_tokens` must be ≥ 2048** in `debug_from_vcs_error.py` — 1024 causes JSON
   truncation for longer error strings.
