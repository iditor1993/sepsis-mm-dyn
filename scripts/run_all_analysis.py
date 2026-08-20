"""SEPSIS-MM-DYN 剩余分析一键顺序执行（总入口）。

按依赖顺序依次执行：
  1. DeepHit 竞争风险（quick=1 seed / full=5 seeds）
  2. 敏感性矩阵：ECG 时效 48h → 72h → SOFA carryforward
  3. ECG global_train_stats 归一化（缓存重建 + 重训）
  4. ECG inductive SSL（预训练 + 微调）
  5. eICU 外部验证（仅推理，需 MIMIC 权重已存在）

VSCode 用法：改 CONFIG → ▶ Run；或终端 python scripts/run_all_analysis.py
全程日志打印到终端并落盘 src/models/runs/all_analysis.log
各阶段失败不中断后续（记录到末尾汇总）。
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "src" / "models" / "runs" / "all_analysis.log"
LOG.parent.mkdir(parents=True, exist_ok=True)

# ============================ CONFIG（只改这里） ============================
DEEPHIT_MODE = "full"        # quick(1 seed) / full(5 seeds) / skip
DO_FRESHNESS = True           # ECG 时效 48h/72h（各 ~2-4h）
DO_SOFA_CF = True             # SOFA carryforward 交互（分钟级）
DO_GLOBALNORM = True          # ECG global 归一化（缓存+重训，过夜级）
DO_SSL = True                 # inductive SSL（预训练+微调，最大单项，1-2天）
DO_EICU = True                # eICU 外验（推理，分钟~小时级）
# ===========================================================================

STEPS = [
    ("DeepHit 竞争风险", "scripts/run_deephit.py", ["--mode", None]),
    ("敏感性 48h", "scripts/run_sensitivity.py", ["--mode", "freshness_48h"]),
    ("敏感性 72h", "scripts/run_sensitivity.py", ["--mode", "freshness_72h"]),
    ("SOFA carryforward", "scripts/run_sensitivity.py",
     ["--mode", "sofa_carryforward"]),
    ("ECG global 归一化", "scripts/run_ecg_globalnorm.py",
     ["--mode", "full"]),
    ("ECG inductive SSL", "scripts/run_ssl_inductive.py", ["--mode", "full"]),
    ("eICU 外部验证", "scripts/run_eicu_external.py", []),
]


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    log(f"===== 一键分析开始（日志 {LOG}）=====")
    enabled = {
        "DeepHit 竞争风险": DEEPHIT_MODE != "skip",
        "敏感性 48h": DO_FRESHNESS,
        "敏感性 72h": DO_FRESHNESS,
        "SOFA carryforward": DO_SOFA_CF,
        "ECG global 归一化": DO_GLOBALNORM,
        "ECG inductive SSL": DO_SSL,
        "eICU 外部验证": DO_EICU,
    }
    results = []
    for name, script, args in STEPS:
        if not enabled.get(name, True):
            log(f"跳过：{name}")
            continue
        cmd = [sys.executable, str(ROOT / script)] + \
              [a if a is not None else DEEPHIT_MODE for a in args]
        log(f"--- 开始：{name}（{' '.join(cmd)}）---")
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=str(ROOT), capture_output=False)
            ok = (p.returncode == 0)
            results.append((name, "OK" if ok else f"FAIL(rc={p.returncode})",
                            time.time() - t0))
            log(f"完成：{name} rc={p.returncode} 耗时 {time.time()-t0:.0f}s")
        except Exception as e:
            results.append((name, f"ERROR({e})", time.time() - t0))
            log(f"异常：{name}: {e}")
    log("===== 全部阶段结束，汇总 =====")
    for name, st, dur in results:
        log(f"  {name}: {st}（{dur/3600:.1f}h）")
    print("\n结果目录：src/models/runs/{deephit,sensitivity,eicu_external}")


if __name__ == "__main__":
    main()
