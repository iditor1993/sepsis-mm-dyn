"""剩余分析补跑脚本（eICU 外验 → SSL 微调 → ECG 归一化）。

按「最快/最有价值 → 最重」排序，一键顺序执行，失败隔离不中断后续：
  1. eICU 外部验证（仅推理，分钟~小时级）——三级验证体系的层级 2
  2. ECG inductive SSL 微调（~半小时级）——复用已预训练的 ssl_encoder.pt
  3. ECG global 归一化（缓存重建+重训，过夜级，最大）

VSCode 用法：打开本文件 → ▶ Run；或终端 python scripts/run_remaining.py
可单独跑某一项：python scripts/run_remaining.py --only eicu
日志：终端打印 + src/models/runs/remaining.log
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "src" / "models" / "runs" / "remaining.log"
LOG.parent.mkdir(parents=True, exist_ok=True)

# 顺序固定：快 → 慢
STEPS = [
    ("eicu",   "eICU 外部验证（推理）", "scripts/run_eicu_external.py", []),
    ("ssl",    "ECG inductive SSL 微调", "scripts/run_ssl_inductive.py",
     ["--mode", "finetune_only"]),
    ("globalnorm", "ECG global 归一化（缓存+重训）",
     "scripts/run_ecg_globalnorm.py", ["--mode", "full"]),
]


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=[s[0] for s in STEPS],
                    help="只跑某一项（eicu / ssl / globalnorm）")
    args = ap.parse_args()
    log("===== 补跑开始 =====")
    results = []
    for key, name, script, args_list in STEPS:
        if args.only and args.only != key:
            log(f"跳过：{name}")
            continue
        cmd = [sys.executable, str(ROOT / script)] + args_list
        log(f"--- 开始：{name} ---")
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=str(ROOT))
            ok = p.returncode == 0
            results.append((name, "OK" if ok else f"FAIL(rc={p.returncode})",
                            time.time() - t0))
            log(f"完成：{name} rc={p.returncode} 耗时 {time.time()-t0:.0f}s")
        except Exception as e:
            results.append((name, f"ERROR({e})", time.time() - t0))
            log(f"异常：{name}: {e}")
    log("===== 汇总 =====")
    for name, st, dur in results:
        log(f"  {name}: {st}（{dur/3600:.2f}h）")
    log("全部结束")
    print("\n结果目录：")
    print("  eICU 外验   → src/models/runs/eicu_external/REPORT.md")
    print("  SSL 微调    → src/models/runs/sensitivity/ssl_inductive/REPORT.md")
    print("  归一化      → src/models/runs/sensitivity/ecg_globalnorm/REPORT.md")


if __name__ == "__main__":
    main()
