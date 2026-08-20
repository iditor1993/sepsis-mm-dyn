"""SEPSIS-MM-DYN Data Extraction Pipeline CLI (v2.4.1 rewrite).

Usage:
    python main.py mimic                    # Full MIMIC pipeline
    python main.py mimic --step c0          # Single step (c0/cohort/landmarks/
                                            #   labels/f1..f8/contracts/qa)
    python main.py eicu                     # Full eICU pipeline
    python main.py eicu --step c6a          # Single step (c6a/c6b/c7/c8/
                                            #   landmarks/labels/features/qa)
    python main.py all                      # Both pipelines
"""
import sys
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))


def print_usage():
    print(__doc__)


def _parse_step(args):
    if "--step" in args:
        idx = args.index("--step")
        if idx + 1 < len(args):
            return args[idx + 1]
    return None


def run_mimic(step=None):
    print("=" * 60)
    print("  MIMIC-IV Extraction Pipeline (v2.4.1 rewrite)")
    print("=" * 60)
    from mimic.pipeline import run_full_mimic_pipeline
    run_full_mimic_pipeline(step=step)


def run_eicu(step=None):
    print("=" * 60)
    print("  eICU-CRD Extraction Pipeline (v2.4.1 rewrite)")
    print("=" * 60)
    from eicu.pipeline import run_full_eicu_pipeline
    run_full_eicu_pipeline(step=step)


def run_all():
    run_mimic()
    run_eicu()


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print_usage()
    elif args[0] == "mimic":
        run_mimic(_parse_step(args))
    elif args[0] == "eicu":
        run_eicu(_parse_step(args))
    elif args[0] == "all":
        run_all()
    else:
        print(f"Unknown command: {args[0]}")
        print_usage()
