"""SEPSIS-MM-DYN preprocessing pipeline CLI (方案 v1.1).

Usage:
    python -m src.run             # 全量 P0→P10（MIMIC）
    python -m src.run --step p2   # 单步
    python -m src.run --from p5   # 从某步继续
"""
import argparse
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from lib import io  # noqa: E402

STEPS = ["p0", "p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8", "p9",
         "p10", "eicu"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", choices=STEPS)
    ap.add_argument("--from", dest="from_step", choices=STEPS)
    args = ap.parse_args()
    cfg = io.load_config()

    if args.step:
        todo = [args.step]
    elif args.from_step:
        todo = STEPS[STEPS.index(args.from_step):]
    else:
        todo = STEPS

    for step in todo:
        print("=" * 60)
        print(f"  {step.upper()}  run_id={cfg['run_id']}")
        print("=" * 60)
        if step == "p0":
            from nodes import p0_env
            p0_env.run(cfg)
        elif step == "p1":
            from nodes import p1_validate
            p1_validate.run(cfg)
        elif step == "p2":
            from nodes import p2_tensorize
            p2_tensorize.run(cfg)
        elif step == "p3":
            from nodes import p3_static
            p3_static.run(cfg)
        elif step == "p4":
            from nodes import p4_samples
            p4_samples.run(cfg)
        elif step == "p5":
            from nodes import p5_ecg
            p5_ecg.run(cfg)
        elif step == "p6":
            from nodes import p6_modality
            p6_modality.run(cfg)
        elif step == "p7":
            from nodes import p7_fit
            p7_fit.run(cfg)
        elif step == "p8":
            from nodes import p8_contracts
            p8_contracts.run(cfg)
        elif step == "p9":
            from nodes import p9_package
            p9_package.run(cfg)
        elif step == "p10":
            from nodes import p10_qa
            p10_qa.run(cfg)
        elif step == "eicu":
            from nodes import eicu_branch
            eicu_branch.run_all(cfg)
    print("preprocess pipeline done")


if __name__ == "__main__":
    main()
