"""冻结清单关闭辅助：更新 freeze_checklist.json 状态与证据。

用法（项目根目录）：
    python preprocess/freeze.py B-1 "证据说明"
    python preprocess/freeze.py --list
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FC = ROOT / "src/data/_output/_meta/freeze_checklist.json"
GROUPS = {"A": "A_protocol", "B": "B_timeline", "C": "C_leakage",
          "D": "D_labels", "E": "E_ecg"}


def load():
    return json.loads(FC.read_text(encoding="utf-8"))


def save(d):
    FC.write_text(json.dumps(d, indent=2, ensure_ascii=False),
                  encoding="utf-8")


def close(item: str, evidence: str):
    d = load()
    grp = GROUPS[item.split("-")[0]]
    assert item in d[grp], f"unknown item {item}"
    if d[grp][item] is True:
        print(f"{item} already closed")
        return
    d[grp][item] = True
    d.setdefault("closures", {})[item] = {
        "closed_at": datetime.now().isoformat(timespec="seconds"),
        "evidence": evidence,
    }
    save(d)
    n = sum(1 for g in GROUPS.values() for v in d[g].values() if v is True)
    print(f"closed {item}; total closed = {n}/31")


def show():
    d = load()
    for g in GROUPS.values():
        for k, v in d[g].items():
            print(f"{'[x]' if v is True else '[ ]'} {k}")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--list":
        show()
    elif len(sys.argv) >= 3:
        close(sys.argv[1], " ".join(sys.argv[2:]))
    else:
        print(__doc__)
