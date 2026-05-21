#!/usr/bin/env python3
"""
prove_hdl_path.py — 验证 uvm_hdl_force/uvm_hdl_deposit 路径在 RTL 中是否存在
===============================================================================
使用 npi_server 后端 (NpiServerClient)，与 npi_aurora/analyses/prove_hdl_path.py
功能等价，但不依赖 npi_aurora 的 socket 服务。

支持命令:
    check_signal  walk_path  scope_info  list_children  get_filelist

用法:
    python3 prove_hdl_path.py --dbdir /path/to/vcs_sim_exe.daidir \\
        --target "tb.substrate.bridge.rdl_mid0.CHIP_MID.<path>" \\
        --out proof_report.md

    # 或通过 rundir 自动发现 daidir:
    python3 prove_hdl_path.py --rundir /path/to/test_runout --target "<path>"

输出:
    proof_report.md  — 逐段路径验证表 + 父 scope 信息 + RTL 文件列表
    (--json 同时输出 proof_report.json)
"""

import argparse
import datetime
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from npi_server_client import NpiServerClient

# Arcadia MID 默认目标 (可被 --target 覆盖)
DEFAULT_TARGET = "tb.substrate.bridge.rdl_mid0.CHIP_MID.<target_path>"


# ── 核心分析 ──────────────────────────────────────────────────────────────────

def analyze_path(srv: NpiServerClient, target: str) -> dict:
    """逐段验证 HDL 路径，返回结构化分析结果。"""
    print(f"\n[1] 验证目标路径...")
    print(f"    {target}")

    walk = srv.query({"cmd": "walk_path", "path": target})
    exists = walk.get("broken_at") is None

    result = {
        "target": target,
        "exists_in_rtl": exists,
        "walk": walk,
        "parent_scope_info": {},
        "parent_children": [],
        "rtl_files": [],
    }

    valid_prefix = walk.get("valid_prefix", "")
    broken_at = walk.get("broken_at")

    if broken_at:
        print(f"\n[2] 路径在 '{broken_at}' 处断裂")
        print(f"    有效前缀: {valid_prefix}")

        if valid_prefix:
            print(f"\n[3] 获取父 scope 信息: {valid_prefix}")
            info = srv.query({"cmd": "scope_info", "scope": valid_prefix})
            result["parent_scope_info"] = info
            print(f"    模块类型: {info.get('def_name', 'N/A')}")
            fn = info.get("file_name", "N/A")
            ln = info.get("line_num", "?")
            print(f"    RTL 文件: {fn}:{ln}")

            print(f"\n[4] 列举 '{valid_prefix}' 的子层次...")
            ch_resp = srv.query({"cmd": "list_children", "scope": valid_prefix})
            children = ch_resp.get("children", [])
            result["parent_children"] = children
            print(f"    找到 {len(children)} 个子节点")
            for c in children[:20]:
                print(f"      - {c.get('name', '?')} ({c.get('type', '?')})")
            if len(children) > 20:
                print(f"      ... (共 {len(children)} 个)")

            print(f"\n[5] 获取 RTL 文件列表...")
            fl_resp = srv.query({"cmd": "get_filelist", "scope": valid_prefix})
            files = fl_resp.get("files", [])
            result["rtl_files"] = files
            print(f"    找到 {len(files)} 个 RTL 文件")
            for f in files[:10]:
                print(f"      {f}")
    else:
        print(f"\n[2] 路径完整存在于 RTL 中!")
        info = srv.query({"cmd": "scope_info", "scope": target})
        result["parent_scope_info"] = info
        fl_resp = srv.query({"cmd": "get_filelist", "scope": target})
        result["rtl_files"] = fl_resp.get("files", [])

    return result


# ── 报告生成 ──────────────────────────────────────────────────────────────────

def generate_report(result: dict, out_path: str) -> str:
    target = result["target"]
    walk = result["walk"]
    broken_at = walk.get("broken_at")
    valid_prefix = walk.get("valid_prefix", "")
    info = result["parent_scope_info"]
    children = result["parent_children"]
    files = result["rtl_files"]

    lines = []
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines += [
        "# NPI RTL 路径验证报告", "",
        f"生成时间: {ts}", "",
        "## 目标路径", "", "```", target, "```", "",
        "## 验证结论", "",
    ]

    if broken_at:
        lines.append(f"**结论: 路径在 `{broken_at}` 处断裂 — uvm_hdl_force 静默失败**")
        lines += ["", "## NPI 逐段验证", "", "| 路径段 | 结果 |", "|--------|------|"]
        segs = target.split(".")
        vsegs = valid_prefix.split(".") if valid_prefix else []
        for i, seg in enumerate(segs):
            if i < len(vsegs):
                lines.append(f"| `{seg}` | ✅ 存在 |")
            elif seg == broken_at:
                lines.append(f"| `{seg}` | ❌ **不存在** (断点) |")
            else:
                lines.append(f"| `{seg}` | ⬜ 未验证 |")

        lines += ["", f"## 有效前缀", "", "```", valid_prefix, "```", ""]

        if info:
            lines += [f"## 父 Scope 信息: `{valid_prefix}`", "",
                      "| 属性 | 值 |", "|------|----|"]
            for k, v in info.items():
                if k != "exists":
                    lines.append(f"| {k} | `{v}` |")
            lines.append("")

        lines += [f"## 父 Scope 子节点 ({len(children)} 个)", ""]
        if children:
            lines += ["| 名称 | 类型 | 完整路径 |", "|------|------|---------|"]
            for c in children[:30]:
                nm = c.get("name", "?")
                tp = c.get("type", "?")
                fn = c.get("full_name", "")
                marker = " **← 此处无目标实例**" if nm == broken_at else ""
                lines.append(f"| `{nm}` | {tp} | `{fn}` |{marker}")
            if len(children) > 30:
                lines.append("| ... | ... | ... |")
            lines.append("")
            names = [c.get("name", "") for c in children]
            if broken_at not in names:
                lines.append(f"> **确认**: `{broken_at}` 不在 `{valid_prefix}` 的子层次中。")
                lines.append("")

        lines += ["## RTL 文件列表", ""]
        if files:
            for f in files:
                lines.append(f"- `{f}`")
        else:
            lines.append("_NPI 未返回文件列表 (可能为加密黑盒)_")
        lines.append("")

        lines += [
            "## 常见原因", "",
            "- **ICL/TDR 模块**: DFT tap 寄存器不编译进 VCS SV 仿真。"
            " `scope_info` 的 `def_name` 含 `icl`/`tdr`/`tap` 即可确认。",
            "  修复: 向上找到非 ICL 的父模块，force 其对应 RTL net。",
            "- **层次改名**: RTL 版本间 instance 名称变化。",
            "- **加密块边界**: NPI 无法穿透，`get_filelist` 返回空。",
            "  修复: 改 force 包装器端口信号而非内部路径。",
            "",
        ]
    else:
        lines.append("**结论: 路径完整存在 — UVM_FATAL 另有根因 (检查 reset 序列、init 缺失等)**")

    report = "\n".join(lines)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n[✓] 报告已写入: {out_path}")
    return report


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="验证 uvm_hdl_force 路径的 RTL 存在性 (npi_server 后端)")
    ap.add_argument("--dbdir",  default="", help="VCS KDB daidir 路径")
    ap.add_argument("--rundir", default="", help="仿真运行目录 (自动发现 daidir)")
    ap.add_argument("--target", default=DEFAULT_TARGET, help="要验证的完整 HDL 路径")
    ap.add_argument("--out",    default="proof_report.md", help="输出报告文件名")
    ap.add_argument("--json",   action="store_true", help="同时输出 JSON 原始结果")
    args = ap.parse_args()

    print("=" * 60)
    print("NPI RTL 路径验证 — uvm_hdl_force 根因分析 (npi_server)")
    print("=" * 60)

    # --out 写入脚本同级目录（与 aurora 版本行为一致），除非给绝对路径
    if not os.path.isabs(args.out):
        out_path = os.path.join(_HERE, args.out)
    else:
        out_path = args.out

    print(f"\n启动 NPI server...")
    with NpiServerClient(daidir=args.dbdir) as srv:
        print("NPI server 就绪")
        result = analyze_path(srv, args.target)

        if args.json:
            json_path = out_path.replace(".md", ".json")
            with open(json_path, "w") as f:
                json.dump(result, f, indent=2, default=str)
            print(f"[✓] JSON: {json_path}")

        generate_report(result, out_path)

    print("\n" + "=" * 60)
    if result["exists_in_rtl"]:
        print("结论: 路径存在 — UVM_FATAL 另有根因")
    else:
        broken = result["walk"].get("broken_at", "?")
        print(f"结论: 路径在 '{broken}' 处不存在 — uvm_hdl_force 静默失败")
    print("=" * 60)


if __name__ == "__main__":
    main()
