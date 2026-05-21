# Generating Verdi wave restore files from NPI investigations

After an NPI/FSDB investigation, you often need to hand the user a wave layout so they
can open Verdi and see the same signals you traced. There are two output formats; pick
the right one for the situation.

## .rc vs TCL — which to generate

**Default: `.rc`** (Verdi's native wave-restore format).
- Self-contained: includes the FSDB path, viewport, groups, signals
- Loaded via `verdi -ssf <file.rc>` at launch, OR `wvRestoreSignal <file.rc>` in a running session
- No dependence on whether the user already has a wave window open
- Verdi-internal path delimiter is `/`, and the file declares `-d /` so signal paths in the file MUST use `/` — convert from NPI's `.`-separated form

**Use TCL only when** you need to inject signals into an already-running Verdi session
where the user is mid-debug and you don't want to disturb their existing wave layout.
TCL has 3 footguns documented at the bottom of this file.

For `.rc` generation: refer to `signal.rc.template` in this directory and fill in the
groups + signal paths directly. Do not write a script — synthesize the file inline.

## MANDATORY: NPI-verify every signal path before writing it into a .rc or TCL file

The single biggest source of "signal not found" errors is using a *summary-form* signal
name from a prior subagent report instead of the actual elaborated net name. Subagent
truth tables abbreviate. The waveform doesn't.

Concrete examples seen in real debugging:
- subagent reported `L0AG0`, real net is `L0AG0_IOHUB_ctrl`
- subagent reported `postdiv.ds_state[3:0]`, real net is `postdiv.clk_state` (1 bit)
- subagent reported `postdiv.ds_clken`, real net is `postdiv.ds_clk_en`
- chip-body net `NBIF_AND_IOHUB__MP1_lclk_ds` (from BIA source) doesn't exist in the
  elaborated design — only the `_allow`-suffixed one does

**Verification protocol** before emitting any wave file:

1. For each candidate signal path, call `handle_by_name` against the design daidir.
   If `found: false`, the path is wrong — search for the real name with
   `internal_scope_handles()` / `net_handles()` of the parent scope.
2. For each verified path, call `pynpi.waveform.sig_by_name` against the FSDB. If it
   returns `None`, the signal exists in the design but was not dumped — note that in
   the file as a comment (don't include lines that will silently fail to add).

The cost of the verification round-trip is seconds; the cost of handing the user a
broken `.rc` is a frustrating bug-hunt that ends up back here.

## ASCII-only

Verdi cannot render non-ASCII characters in group names, signal labels, or comments.
Use `->` not `→`, `GC->DF` not `GC→DF`. No Chinese in identifiers.

## TCL injection footguns (only when you cannot use .rc)

If the user explicitly wants signals injected into a running Verdi session, use TCL.
Three things to know — all learned the hard way.

### 1. Path delimiter
Verdi's TCL `wvAddSignal` defaults to `/` as the hierarchical delimiter. If your
paths use `.` (NPI-style), you must pass `-delim "."` or every signal silently fails
to add.

### 2. Active wave window
Don't hardcode `$_nWave1` / `$_nWave2` — those Verdi-internal names depend on the
session's window history. The portable form is:
```tcl
set W [wvGetCurrentWindow -active]
wvAddSignal -win $W -delim "." <paths>
```
This matches the convention in `~/Scripts/thy_novas_common.tcl`.

### 3. `-group` argument requires explicit braces around each path
The argument format is `-group {"groupname" {path1} {path2} ...}`. TCL's `[list]`
auto-braces an item only when it contains TCL-special characters; plain dot-paths get
returned unbraced and Verdi then misparses the list. Use a small helper proc to force
braces on each path:
```tcl
proc grp {name paths} {
    global W
    set spec "\"$name\""
    foreach p $paths { append spec " {$p}" }
    eval wvAddSignal -win \$W -delim {.} -group {$spec}
}
grp "MyGroup" [list \
    "tb.dut.foo.sig_a" \
    "tb.dut.foo.sig_b"]
```

### 4. Signals with bit ranges
For vector signals, escape the brackets so TCL doesn't try to evaluate them as
commands: `tb.dut.bus\[31:0\]`. (Or wrap the whole path in `{}` braces — same effect.)
