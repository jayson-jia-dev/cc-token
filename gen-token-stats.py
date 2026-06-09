#!/usr/bin/env python3
"""Generate token usage summary JSON for the current machine.
Output: machines/$MACHINE/token-stats.json in the claude-config repo.
Called by sync-claude.sh before commit.
"""

import json
import os
import glob
import socket
from datetime import datetime

CLAUDE_DIR = os.path.expanduser("~/.claude")
MACHINE = socket.gethostname().split(".")[0]
REPO_DIR = os.environ.get("CLAUDE_SYNC_REPO", os.path.join(CLAUDE_DIR, "claude-config"))
OUT_DIR = os.path.join(REPO_DIR, "machines", MACHINE)
OUT_FILE = os.path.join(OUT_DIR, "token-stats.json")

# IMPORTANT: keep in sync with PRICING / tier() in cc-token.5m.py
MODEL_PRICING = {
    "opus_new":  {"input": 5,    "output": 25, "cache_write": 10,    "cache_read": 0.50},
    "opus_old":  {"input": 15,   "output": 75, "cache_write": 18.75, "cache_read": 1.50},
    "sonnet":    {"input": 3,    "output": 15, "cache_write": 6,     "cache_read": 0.30},
    "haiku":     {"input": 1,    "output": 5,  "cache_write": 2,     "cache_read": 0.10},
}

def get_model_tier(model_name):
    m = model_name.lower()
    if "opus" in m:
        # Only legacy Opus (4.0/4.1) uses old pricing; all newer default to opus_new
        if "4-0" in model_name or "4-1" in model_name or "4.0" in m or "4.1" in m:
            return "opus_old"
        return "opus_new"
    elif "haiku" in m:
        return "haiku"
    return "sonnet"

def main():
    base = os.path.join(CLAUDE_DIR, "projects")
    if not os.path.isdir(base):
        return

    total_input = 0
    total_output = 0
    total_cache_write = 0
    total_cache_read = 0
    total_cost = 0.0
    session_count = 0
    min_date = None
    max_date = None
    model_breakdown = {}

    for projdir in glob.glob(os.path.join(base, "*")):
        if not os.path.isdir(projdir):
            continue
        for jf in glob.glob(os.path.join(projdir, "*.jsonl")):
            has_usage = False
            try:
                file_date = datetime.fromtimestamp(os.path.getmtime(jf)).strftime("%Y-%m-%d")
            except Exception:
                file_date = None

            try:
                with open(jf, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            d = json.loads(line)
                            if d.get("type") != "assistant":
                                continue
                            msg = d.get("message", {})
                            if not isinstance(msg, dict):
                                continue
                            usage = msg.get("usage")
                            if not usage:
                                continue

                            inp = usage.get("input_tokens", 0)
                            out = usage.get("output_tokens", 0)
                            cw = usage.get("cache_creation_input_tokens", 0)
                            cr = usage.get("cache_read_input_tokens", 0)

                            total_input += inp
                            total_output += out
                            total_cache_write += cw
                            total_cache_read += cr
                            has_usage = True

                            model = msg.get("model", "")
                            tier = get_model_tier(model)
                            p = MODEL_PRICING.get(tier, MODEL_PRICING["sonnet"])
                            msg_cost = (inp * p["input"] + out * p["output"] + cw * p["cache_write"] + cr * p["cache_read"]) / 1e6
                            total_cost += msg_cost

                            if model and model != "<synthetic>":
                                if model not in model_breakdown:
                                    model_breakdown[model] = {"msgs": 0, "tokens": 0, "cost": 0.0}
                                model_breakdown[model]["msgs"] += 1
                                model_breakdown[model]["tokens"] += inp + out + cw + cr
                                model_breakdown[model]["cost"] += msg_cost
                        except Exception:
                            pass

                if has_usage:
                    session_count += 1
                    if file_date:
                        if min_date is None or file_date < min_date:
                            min_date = file_date
                        if max_date is None or file_date > max_date:
                            max_date = file_date
            except Exception:
                pass

    # Round model_breakdown costs
    for m in model_breakdown:
        model_breakdown[m]["cost"] = round(model_breakdown[m]["cost"], 2)

    stats = {
        "machine": MACHINE,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "session_count": session_count,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cache_write_tokens": total_cache_write,
        "cache_read_tokens": total_cache_read,
        "total_cost": round(total_cost, 2),
        "date_range": {"min": min_date, "max": max_date},
        "model_breakdown": model_breakdown,
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_FILE, "w") as f:
        json.dump(stats, f, indent=2)

if __name__ == "__main__":
    main()
