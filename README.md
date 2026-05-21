# NPI RTL Analysis Persistent Server

General-purpose NPI-based RTL analysis server for VCS-compiled designs.
Loads VCS daidir **once**, serves unlimited JSON queries — no repeated `load_design()`.

## Files

| File | Purpose |
|------|---------|
| `npi_rtl_server.py` | Persistent server (JSON stdin/stdout) |
| `npi_server_client.py` | Python client (subprocess context manager + CLI) |
| `prove_hdl_path.py` | Example one-shot prover script |
| `debug_from_vcs_error.py` | AI-in-the-loop debug pipeline |
| `results/` | Output JSON proof reports |

## Quick Start

```tcsh
# Set VERDI_HOME (usually already set in your DV flow)
setenv VERDI_HOME /tool/cbar/apps/verdi/2025.06-SP2-2

# Interactive server (manual JSON queries):
python3 npi_rtl_server.py --daidir /path/to/vcs_sim_exe.daidir

# Or pipe a query:
echo '{"cmd":"ping"}' | python3 npi_rtl_server.py --daidir /path/to/vcs_sim_exe.daidir
```

## Commands

| Command | Parameters | Returns |
|---------|-----------|---------|
| `ping` | — | `{"status":"pong"}` |
| `find_signal` | `scope`, `pattern` | `{"signals": [{name, width, src_file, src_line}]}` |
| `trace_driver` | `signal`, `depth` (default 5) | `{"drivers": [{name, depth, constant?}]}` |
| `trace_load` | `signal`, `depth` (default 5) | `{"loads": [{name, depth}]}` |
| `get_src_file` | `signal` | `{src_file, src_line}` |
| `get_filelist` | `scope` (opt), `use_csv` (default true) | `{"files": [...], "count": N}` |
| `hier_search` | `module`, `scope` (opt), `max_results` | `{"instances": [{path, src_file, src_line}]}` |
| `quit` | — | `{"status":"quit"}` |

## Python API

In all examples below, `<TB_TOP>` / `<CHIP_TOP>` / `<block>` are placeholders for paths in your
own design (e.g. `top.cpu.alu`).

```python
from npi_server_client import NpiServerClient

with NpiServerClient(daidir="/path/to/vcs_sim_exe.daidir") as srv:
    # Find all signals matching a pattern
    r = srv.query({"cmd": "find_signal",
                   "scope": "<TB_TOP>.<CHIP_TOP>.<block>",
                   "pattern": "<keyword>*"})

    # Trace drivers of a signal
    drv = srv.query({"cmd": "trace_driver",
                     "signal": r["signals"][0]["name"],
                     "depth": 5})

    # Find all instances of a module
    insts = srv.query({"cmd": "hier_search",
                       "module": "<module_name>",
                       "scope": "<TB_TOP>.<CHIP_TOP>"})

    # Get RTL filelist for a scope
    files = srv.query({"cmd": "get_filelist",
                       "scope": "<TB_TOP>.<CHIP_TOP>.<block>",
                       "use_csv": True})
```

## Performance Notes

| Operation | Typical Time | Notes |
|-----------|-------------|-------|
| Design load (`npisys.load_design`) | 60–180 s | Done once; all subsequent queries free |
| `find_signal` (narrow scope) | < 1 s | Fast with specific scope |
| `find_signal` (chip-wide `*`) | 30–120 s | Avoid; use narrow scope |
| `trace_driver` / `trace_load` (depth=5) | 1–10 s | Depends on fan-in/fan-out |
| `get_filelist` via CSV (scope=module) | 5–30 s | Cached after first call |
| `hier_search` (uses CSV cache) | < 1 s | Second call is instant |

**Key design choice**: `hier_tree_dump_csv` is called once per scope and cached in memory.
Subsequent `hier_search` and `get_filelist` calls on the same scope are O(1).

## Git Tags

| Tag | Contents |
|-----|---------|
| `v1-server-skeleton` | Server with ping/find_signal/trace_driver/trace_load/get_src_file |
| `v2-client-helper` | NpiServerClient subprocess context manager + CLI |
| `v3-proof-script` | First example prover script (3-query RTL proof pattern) |
| `v4-hier-search-filelist` | hier_search + improved get_filelist via hier_tree_dump_csv |
| `v5-docs-perf` | README + performance analysis |
