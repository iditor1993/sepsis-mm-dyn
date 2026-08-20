"""P8: SC-common 变量终稿与单位统一（方案 §10）。"""
import pandas as pd

from lib import io, manifest


def run(cfg: dict) -> dict:
    root = io.data_root(cfg)
    out = io.artifact_dir(cfg, "p8_contracts")
    contract = pd.read_parquet(
        root / "contracts/sc_common_variable_contract_v2.parquet")
    core = list(cfg["sc_common_core"])
    extended = core + ["inr", "pt", "pao2"]
    mimic_only = ["nee_current"]

    a_layer = contract[contract["layer"] == "A"]["concept_name"].tolist()
    result = {
        "sc_common_core": core,
        "sc_common_extended": extended,
        "mimic_only_extra": mimic_only,
        "contract_layer_A": a_layer,
        "contract_status": "candidate_pending_c2_audit",
        "note": "A 层 18 通道为主外验输入；合同评级通过后升级 extended；"
                "禁止按模型效果反向调整（提取方案 §6）。",
    }
    pd.DataFrame({"channel": core}).assign(
        role="sc_common_core").to_parquet(
        out / "sc_common_channels_v1.parquet", index=False)
    manifest.register_artifact(cfg, "sc_common_channels_v1", "p8",
                               result, fitted_on=None)
    print(f"[P8] core channels: {len(core)}")
    return result
