#!/usr/bin/env python3
import os
import sys
import re
import math
import json
from collections import defaultdict

# Directories (relative to this file)
CURRENT_DIR    = os.path.dirname(os.path.abspath(__file__))
PARSED_DIR     = os.path.join(CURRENT_DIR, 'parsednetlist')
SCOAP_OUT_DIR  = os.path.join(CURRENT_DIR, 'scoapout')

# Regex & constants
VECTOR_RE      = re.compile(r'^(\w+)\[(\d+):(\d+)\]$')
GATE_RE        = re.compile(r'^\s*(\w+)\s+out\(\s*([^)]+)\)\s+in\(\s*([^)]+)\)\s*$')
OUTPUT_PORT_NAMES = {'Z', 'ZN', 'Q', 'QN', 'Y', 'S', 'CO'}

def expand_vector(signal):
    m = VECTOR_RE.match(signal)
    if not m:
        return [signal]
    base, msb, lsb = m.group(1), int(m.group(2)), int(m.group(3))
    step = -1 if msb >= lsb else 1
    return [f"{base}[{i}]" for i in range(msb, lsb - step, step)]

def read_netlist(path):
    try:
        with open(path) as f:
            return [line.rstrip() for line in f if line.strip()]
    except FileNotFoundError:
        print(f"Error: cannot open '{path}'", file=sys.stderr)
        sys.exit(1)

def parse_sections(lines):
    inputs = []; outputs = []; gates = []
    section = None
    for lineno, line in enumerate(lines, start=1):
        if line.startswith('#'):
            if 'Primary Inputs' in line:    section = 'INPUT'
            elif 'Primary Outputs' in line: section = 'OUTPUT'
            elif 'Complete Paths' in line:  section = 'GATES'
            else:                           section = None
            continue
        if section == 'INPUT':
            for tok in line.split(): inputs.extend(expand_vector(tok))
        elif section == 'OUTPUT':
            for tok in line.split(): outputs.extend(expand_vector(tok))
        elif section == 'GATES':
            m = GATE_RE.match(line)
            if m:
                gtype = m.group(1)
                outs  = m.group(2).split()
                ins   = m.group(3).split()
                for o in outs:
                    gates.append((gtype, o, ins))
            else:
                print(f"[WARN] skipped @{lineno}: {line}", file=sys.stderr)
    # reconstruct fanouts for observability
    fanouts = defaultdict(list)
    for _, o, ins in gates:
        for i in ins: fanouts[i].append(o)
    fanout_list = list(fanouts.items())
    return inputs, outputs, fanout_list, gates

def extract_wires(inputs, outputs, gates):
    nets = set(inputs) | set(outputs)
    for _, o, ins in gates:
        nets.add(o); nets.update(ins)
    return sorted(nets)

def build_controllability(nets, inputs, gates):
    CC0 = {n:(1 if n in inputs else math.inf) for n in nets}
    CC1 = {n:(1 if n in inputs else math.inf) for n in nets}
    changed = True
    while changed:
        changed = False
        for gtype,o,ins in gates:
            c0 = [CC0[i] for i in ins]; c1 = [CC1[i] for i in ins]
            gt = gtype.upper()
            try:
                if 'NAND' in gt:
                    new0,new1 = 1+min(c0), 1+sum(c1)
                elif 'AND' in gt:
                    new0,new1 = 1+sum(c0), 1+min(c1)
                elif 'NOR' in gt:
                    new0,new1 = 1+sum(c1), 1+min(c0)
                elif 'OR' in gt:
                    new0,new1 = 1+min(c0), 1+sum(c1)
                elif 'XOR' in gt:
                    a0,b0 = c0; a1,b1 = c1
                    new0 = 1 + min(a0+b1, a1+b0)
                    new1 = 1 + min(a0+b0, a1+b1)
                elif 'INV' in gt or 'NOT' in gt:
                    new0,new1 = 1+c1[0], 1+c0[0]
                elif 'BUF' in gt:
                    new0,new1 = 1+c0[0], 1+c1[0]
                else:
                    new0,new1 = 1+c1[0], 1+c0[0]
            except:
                continue
            if new0 < CC0[o]:
                CC0[o] = new0; changed = True
            if new1 < CC1[o]:
                CC1[o] = new1; changed = True
    ctrl = {f"CC0_{n}":CC0[n] for n in nets}
    ctrl.update({f"CC1_{n}":CC1[n] for n in nets})
    return ctrl

def build_observability(nets, outputs, fanout_list, ctrl, gates):
    CO = {n:(1 if n in outputs else math.inf) for n in nets}
    changed = True
    while changed:
        changed = False
        for gtype,o,ins in gates:
            coo = CO[o]; gt = gtype.upper()
            try:
                if len(ins)==1:
                    v = coo+1
                    if v < CO[ins[0]]:
                        CO[ins[0]] = v; changed = True
                else:
                    i1,i2 = ins[0], ins[1]
                    c0_i1, c1_i1 = ctrl[f"CC0_{i1}"], ctrl[f"CC1_{i1}"]
                    c0_i2, c1_i2 = ctrl[f"CC0_{i2}"], ctrl[f"CC1_{i2}"]
                    if 'AND' in gt or 'NAND' in gt:
                        n1 = coo + c1_i2 + 1; n2 = coo + c1_i1 + 1
                    elif 'OR' in gt or 'NOR' in gt:
                        n1 = coo + c0_i2 + 1; n2 = coo + c0_i1 + 1
                    elif 'XOR' in gt or 'XNOR' in gt:
                        m = min(c0_i2 + c1_i2, c0_i1 + c1_i1)
                        n1 = n2 = coo + m + 1
                    else:
                        n1 = n2 = coo + 1
                    if n1 < CO[i1]:
                        CO[i1] = n1; changed = True
                    if n2 < CO[i2]:
                        CO[i2] = n2; changed = True
            except:
                continue
    return {f"CO_{n}": CO[n] for n in nets}

def write_scoap(ctrl, obs, filename):
    with open(filename, 'w') as f:
        f.write("--- SCOAP CONTROLLABILITY (CC0) ---\n")
        for k in sorted(ctrl):
            if k.startswith("CC0_"):
                f.write(f"{k}: {ctrl[k]}\n")
        f.write("\n--- SCOAP CONTROLLABILITY (CC1) ---\n")
        for k in sorted(ctrl):
            if k.startswith("CC1_"):
                f.write(f"{k}: {ctrl[k]}\n")
        f.write("\n--- SCOAP OBSERVABILITY (CO) ---\n")
        for k in sorted(obs):
            f.write(f"{k}: {obs[k]}\n")
    print(f"[✓] SCOAP results written to: {filename}")

def dump_json(ctrl, obs, inputs, outputs, gates, filename):
    from math import isinf
    data = {
        "primary_inputs": inputs,
        "primary_outputs": outputs,
        "metrics": []
    }
    for idx, (gtype, o, ins) in enumerate(gates):
        entry = {
            "gate": f"{gtype}_{idx}",
            "output": o,
            "inputs": ins,
            "cc0": ctrl.get(f"CC0_{o}", None),
            "cc1": ctrl.get(f"CC1_{o}", None),
            "co": obs.get(f"CO_{o}", None)
        }
        for key in ("cc0","cc1","co"):
            if entry[key] is None or (isinstance(entry[key], float) and math.isinf(entry[key])):
                entry[key] = "Infinity"
        data["metrics"].append(entry)
    with open(filename, 'w') as f:
        json.dump(data, f, indent=4)
    print(f"[✓] JSON SCOAP written to: {filename}")

def run(input_filename, output_filename, json_flag=False):
    input_path = os.path.join(PARSED_DIR, input_filename)
    os.makedirs(SCOAP_OUT_DIR, exist_ok=True)
    output_path_txt = os.path.join(SCOAP_OUT_DIR, output_filename)
    lines = read_netlist(input_path)
    inputs, outputs, fanout_list, gates = parse_sections(lines)
    nets = extract_wires(inputs, outputs, gates)
    ctrl = build_controllability(nets, inputs, gates)
    obs = build_observability(nets, outputs, fanout_list, ctrl, gates)
    write_scoap(ctrl, obs, output_path_txt)
    if json_flag:
        base, _ = os.path.splitext(output_filename)
        json_path = os.path.join(SCOAP_OUT_DIR, f"{base}.json")
        dump_json(ctrl, obs, inputs, outputs, gates, json_path)
    return output_path_txt

if __name__ == "__main__":
    # Simple CLI: python scoap.py input_parsed.txt output.txt [--json]
    args = sys.argv[1:]
    if len(args) < 2:
        print("Usage: python scoap.py <parsed_input.txt> <output.txt> [--json]", file=sys.stderr)
        sys.exit(1)
    inp, outp = args[0], args[1]
    json_flag = "--json" in args
    try:
        result = run(inp, outp, json_flag)
        sys.exit(0)
    except Exception as e:
        print(f"[✗] Error: {e}", file=sys.stderr)
        sys.exit(1)
