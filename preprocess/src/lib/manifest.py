"""Manifest / artifact registry helpers (方案 §9.2, §14)."""
import hashlib
import json
from datetime import datetime
from pathlib import Path

from lib import io


def content_hash_json(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, default=str,
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def register_artifact(cfg: dict, name: str, node: str, content: dict,
                      fitted_on: str = None, inputs: list = None) -> dict:
    """Register one artifact into p7_fitted/registry.json."""
    entry = {
        "name": name,
        "node": node,
        "fitted_on": fitted_on,
        "inputs": inputs or [],
        "content_hash": content_hash_json(content),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "code_version": cfg.get("_config_hash"),
    }
    reg_path = io.PROJECT_ROOT / cfg["paths"]["out_root"] \
        / "p7_fitted" / cfg["run_id"] / "registry.json"
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    if reg_path.exists():
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
    else:
        reg = {"schema_version": "registry_v1", "run_id": cfg["run_id"],
               "artifacts": []}
    reg["artifacts"] = [a for a in reg["artifacts"]
                        if a["name"] != name] + [entry]
    io.write_json(reg, reg_path)
    return entry
