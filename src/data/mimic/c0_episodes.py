"""C0: MIMIC continuous ICU episode mapping (v2.4.1, 阻断项 1 / R23 / R36 / R37).

两阶段 + zero-gap 序列核验 + 显式 final decision + 全局唯一 episode_id：
  edges_preliminary (transfer_sequence 实际生成 + 合法/异常路径类别)
  -> map_preliminary (仅 merged 延续，其余保守拆分)
  -> episode_merge_adjudications (仅 pending_review 边)
  -> map_final (显式 CASE；episode_id = 'MIMIC_<hadm_id>_<episode_seq>'；
     episode_mapping_version ∈ {main_tau0, sensitivity_tau30, sensitivity_tau60})
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_DATA_DIR = Path(__file__).resolve().parent.parent
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

import config  # noqa: E402
import utils   # noqa: E402


# ----------------------------------------------------------------
# Careunit classification (§2.1 C0 合法/异常路径类别)
# ----------------------------------------------------------------

def _careunit_class(careunit) -> str:
    if careunit is None or (isinstance(careunit, float) and pd.isna(careunit)):
        return "unknown"
    cu = str(careunit).strip().lower()
    if cu in ("unknown", ""):
        return "unknown"
    if "intensive care unit" in cu or "ccu" in cu:
        return "icu"
    if "emergency department" in cu:
        return "ed"
    return "ward"


def _classify_transfer_path(gap_minutes, left_unit, right_unit, seq_rows):
    """Classify one edge. seq_rows: list of transfer dicts in the open
    interval (prev_outtime, curr_intime), ordered by (intime, transfer_id).

    Returns (zero_gap_path_status, transfer_evidence, intervening_careunit,
             transfer_path_class, exclusion_reason, boundary_conflict).
    """
    left_cls = _careunit_class(left_unit)
    right_cls = _careunit_class(right_unit)

    if gap_minutes is None or pd.isna(gap_minutes):
        return ("not_applicable", "no_transfer_evidence", None,
                "first_stay", None, False)
    if gap_minutes < 0:
        return ("anomaly", "overlap", None, "overlapping_transfer_records",
                "overlap_records", False)

    classes = [_careunit_class(r["careunit"]) for r in seq_rows]
    non_icu = [r["careunit"] for r, c in zip(seq_rows, classes) if c not in ("icu",)]
    has_ed = any(c == "ed" for c in classes)
    has_unknown = any(c == "unknown" for c in classes)
    has_ward = any(c == "ward" for c in classes) or left_cls == "ward" or right_cls == "ward"

    # 内部转移占位判定：非 ICU、非 ED 的介入单元时长均 ≤ PLACEHOLDER_MAX_MIN，
    # 且左右边界均为 ICU → ICU_A → internal_transfer_placeholder → ICU_B（合法路径）
    def _dur_min(r):
        it, ot = r.get("intime"), r.get("outtime")
        if it is None or ot is None or pd.isna(it) or pd.isna(ot):
            return None
        return (ot - it).total_seconds() / 60.0
    non_icu_rows = [r for r, c in zip(seq_rows, classes)
                    if c not in ("icu", "ed", "unknown")]
    non_icu_durs = [_dur_min(r) for r in non_icu_rows]
    is_placeholder = (
        left_cls == "icu" and right_cls == "icu" and non_icu_rows
        and all(d is not None and d <= config.PLACEHOLDER_MAX_MIN
                for d in non_icu_durs)
        and not has_ed and not has_unknown
    )

    # boundary conflict: overlapping intervals among sequence rows
    conflict = False
    prev_out = None
    for r in seq_rows:
        it, ot = r.get("intime"), r.get("outtime")
        if prev_out is not None and it is not None and not pd.isna(it) \
                and not pd.isna(prev_out) and it < prev_out:
            conflict = True
            break
        if ot is not None and not pd.isna(ot):
            prev_out = ot

    if gap_minutes == 0:
        if len(seq_rows) == 0:
            return ("missing_boundary", "no_transfer_evidence", None,
                    "missing_left_boundary", "missing_boundary_evidence", False)
        if conflict:
            return ("anomaly", "direct_icu" if not non_icu else "via_ward",
                    "|".join(map(str, non_icu)) or None,
                    "multiple_conflicting_boundary_events",
                    "conflicting_boundary_events", True)
        if has_unknown:
            return ("anomaly", "direct_icu", "|".join(map(str, non_icu)) or None,
                    "unknown_careunit", "unknown_careunit_in_path", False)
        if has_ed:
            return ("anomaly", "via_ed", "|".join(map(str, non_icu)) or None,
                    "icu_to_ed_to_icu", "via_ed", False)
        if is_placeholder:
            return ("clean", "via_internal_placeholder",
                    "|".join(map(str, non_icu)) or None,
                    "icu_to_internal_placeholder_to_icu", None, False)
        if has_ward:
            return ("anomaly", "via_ward", "|".join(map(str, non_icu)) or None,
                    "icu_to_ward_to_icu", "via_ward", False)
        # 合法路径：ICU_A → ICU_B
        return ("clean", "direct_icu", None, "icu_to_icu", None, False)

    # gap > 0（仅敏感性阈值版本可能合并）
    if has_ed:
        return ("not_applicable", "via_ed", "|".join(map(str, non_icu)) or None,
                "icu_to_ed_to_icu", "via_ed", False)
    if is_placeholder:
        return ("not_applicable", "via_internal_placeholder",
                "|".join(map(str, non_icu)) or None,
                "icu_to_internal_placeholder_to_icu", None, False)
    if has_ward:
        return ("not_applicable", "via_ward", "|".join(map(str, non_icu)) or None,
                "icu_to_ward_to_icu", "via_ward", False)
    return ("not_applicable", "direct_icu" if seq_rows else "no_transfer_evidence",
            None, "icu_to_icu" if seq_rows else "no_transfer_rows", None, False)


def _edge_decision(gap_minutes, threshold_min, path_status, transfer_evidence):
    """preliminary_decision per §2.1 C0 三规则 + A.0。"""
    if gap_minutes is None or pd.isna(gap_minutes):
        return "split"
    if gap_minutes < 0:
        return "pending_review"
    if transfer_evidence in ("via_ward", "via_ed"):
        return "split"
    if gap_minutes == 0:
        if path_status == "clean":
            return "merged"
        if path_status == "missing_boundary":
            return "pending_review"     # Q1-9 用例④
        return "pending_review"         # anomaly
    # gap > 0
    if gap_minutes <= threshold_min and transfer_evidence in (
            "direct_icu", "no_transfer_evidence", "via_internal_placeholder"):
        return "merged"
    return "split"


# ----------------------------------------------------------------
# Stage 1: edges
# ----------------------------------------------------------------

def _build_base_edges(con) -> pd.DataFrame:
    utils.log_step("C0: build base edges with transfer_sequence")
    sql = """
    WITH s AS (
      SELECT subject_id, hadm_id, stay_id, intime, outtime,
             first_careunit, last_careunit,
             LAG(outtime) OVER (PARTITION BY hadm_id ORDER BY intime, stay_id)
               AS prev_outtime,
             LAG(stay_id)  OVER (PARTITION BY hadm_id ORDER BY intime, stay_id)
               AS prev_stay_id,
             LAG(last_careunit) OVER (PARTITION BY hadm_id ORDER BY intime, stay_id)
               AS prev_last_careunit
      FROM main.icustays
    ),
    g AS (
      SELECT *, EPOCH(intime - prev_outtime) / 60.0 AS gap_minutes
      FROM s WHERE prev_stay_id IS NOT NULL
    )
    SELECT g.hadm_id, g.prev_stay_id AS previous_stay_id, g.stay_id AS current_stay_id,
           g.gap_minutes, g.prev_outtime, g.intime,
           g.prev_last_careunit, g.first_careunit,
           (SELECT LIST({'transfer_id': t.transfer_id, 'eventtype': t.eventtype,
                         'careunit': t.careunit, 'intime': t.intime,
                         'outtime': t.outtime}
                        ORDER BY t.intime, t.transfer_id)
              FROM main.transfers t
             WHERE t.hadm_id = g.hadm_id
               AND t.intime < g.intime
               AND COALESCE(t.outtime, g.intime) > g.prev_outtime) AS transfer_sequence
    FROM g
    ORDER BY g.hadm_id, g.intime
    """
    df = con.execute(sql).fetchdf()
    print(f"  base edges: {len(df):,} rows")
    return df


def _classify_edges(df: pd.DataFrame) -> pd.DataFrame:
    utils.log_step("C0: classify zero-gap transfer paths")
    records = []
    for row in df.itertuples(index=False):
        seq = row.transfer_sequence
        if seq is None or (not isinstance(seq, (list, tuple, np.ndarray))
                           and pd.isna(seq)):
            seq_rows = []
        else:
            seq_rows = list(seq)
        for i, r in enumerate(seq_rows):
            r["relative_position"] = i + 1
        (path_status, evidence, intervening, path_class,
         exclusion, conflict) = _classify_transfer_path(
            row.gap_minutes, row.prev_last_careunit, row.first_careunit, seq_rows)
        left_tid = seq_rows[0]["transfer_id"] if seq_rows else None
        right_tid = seq_rows[-1]["transfer_id"] if seq_rows else None
        records.append({
            "hadm_id": row.hadm_id,
            "previous_stay_id": row.previous_stay_id,
            "current_stay_id": row.current_stay_id,
            "gap_minutes": row.gap_minutes,
            "overlap_flag": bool(row.gap_minutes is not None
                                 and not pd.isna(row.gap_minutes)
                                 and row.gap_minutes < 0),
            "transfer_sequence": json.dumps(
                [{k: (v.isoformat() if hasattr(v, "isoformat") and v is not None
                      and not (isinstance(v, float) and pd.isna(v)) else
                      (None if (v is None or (isinstance(v, float) and pd.isna(v))) else v))
                  for k, v in r.items()} for r in seq_rows],
                ensure_ascii=False),
            "zero_gap_path_status": path_status,
            "transfer_evidence": evidence,
            "intervening_careunit": intervening,
            "episode_transfer_path_class": path_class,
            "left_adjacent_transfer_id": left_tid,
            "right_adjacent_transfer_id": right_tid,
            "episode_merge_exclusion_reason": exclusion,
        })
    out = pd.DataFrame.from_records(records)
    return out


def _write_edges(edges: pd.DataFrame) -> pd.DataFrame:
    """Expand to the three threshold versions (分版本字段, §8 规范⑨)."""
    frames = []
    for version, tau in config.EPISODE_MERGE_THRESHOLDS.items():
        v = edges.copy()
        v["episode_merge_threshold_min"] = tau
        v["preliminary_decision"] = [
            _edge_decision(g, tau, ps, ev) for g, ps, ev in zip(
                v["gap_minutes"], v["zero_gap_path_status"],
                v["transfer_evidence"])
        ]
        v["episode_mapping_version"] = version
        frames.append(v)
    allv = pd.concat(frames, ignore_index=True)
    out_path = config.OUTPUT_DIRS["episodes"] / "mimic_icu_episode_edges_preliminary.parquet"
    utils.write_parquet(allv, out_path)
    print(f"  edges_preliminary: {len(allv):,} rows "
          f"({allv['episode_mapping_version'].nunique()} versions)")
    return allv


# ----------------------------------------------------------------
# Stage 2/3/4: preliminary map, adjudications, final map
# ----------------------------------------------------------------

def _stays_frame(con) -> pd.DataFrame:
    return con.execute("""
        SELECT subject_id, hadm_id, stay_id, intime, outtime,
               first_careunit, last_careunit
        FROM main.icustays
    """).fetchdf()


def _decision_with_adjudication(prelim_decision: str, adj_status, adj_final):
    """显式 final decision CASE（冻结；NULL ⇒ pipeline failure）。"""
    if prelim_decision == "pending_review":
        if adj_status == "adjudicated" and adj_final in ("merged", "split"):
            return adj_final, "adjudicated", False
        return "split", "unresolved_conservative_split", True
    if prelim_decision in ("merged", "split"):
        return prelim_decision, "clean", False
    raise ValueError(f"Illegal preliminary_decision: {prelim_decision!r} "
                     "(final_decision IS NULL => pipeline failure)")


def _build_maps_for_version(con, edges_v: pd.DataFrame, version: str,
                            tau: int, adjudications: pd.DataFrame,
                            stays: pd.DataFrame, stage: str) -> pd.DataFrame:
    key = ["hadm_id", "previous_stay_id", "current_stay_id"]
    ev = edges_v.copy()
    if stage == "final":
        adj = adjudications[["hadm_id", "previous_stay_id", "current_stay_id",
                             "adjudication_status", "final_decision"]]
        ev = ev.merge(adj, on=key, how="left")
        decisions = [
            _decision_with_adjudication(p, a, f) for p, a, f in zip(
                ev["preliminary_decision"], ev["adjudication_status"],
                ev["final_decision"])
        ]
        ev["edge_final_decision"] = [d[0] for d in decisions]
        ev["edge_mapping_status"] = [d[1] for d in decisions]
        ev["unresolved_conservative_split"] = [d[2] for d in decisions]
        decision_col = "edge_final_decision"
    else:
        ev["edge_final_decision"] = ev["preliminary_decision"].map(
            lambda d: "merged" if d == "merged" else "split")
        ev["edge_mapping_status"] = "preliminary"
        ev["unresolved_conservative_split"] = ev["preliminary_decision"].eq(
            "pending_review")
        decision_col = "edge_final_decision"

    m = stays.merge(
        ev[key + [decision_col, "edge_mapping_status",
                  "unresolved_conservative_split", "gap_minutes",
                  "overlap_flag", "transfer_evidence", "intervening_careunit",
                  "episode_transfer_path_class",
                  "episode_merge_exclusion_reason"]],
        left_on=["hadm_id", "stay_id"], right_on=["hadm_id", "current_stay_id"],
        how="left")
    m = m.sort_values(["hadm_id", "intime", "stay_id"]).reset_index(drop=True)
    m["is_merged_from_prev"] = m[decision_col].eq("merged").fillna(False)
    m["episode_seq"] = m.groupby("hadm_id")["is_merged_from_prev"] \
        .transform(lambda s: (~s).cumsum())
    m["stay_seq_in_episode"] = m.groupby(["hadm_id", "episode_seq"]) \
        .cumcount() + 1
    m["episode_id"] = "MIMIC_" + m["hadm_id"].astype(str) + "_" \
        + m["episode_seq"].astype(str)

    grp = m.groupby("episode_id", sort=False)
    ep_times = grp.agg(
        episode_intime_ts=("intime", "min"),
        episode_outtime_ts=("outtime", "max"),
        episode_has_null_stay_outtime=("outtime", lambda s: bool(s.isna().any())),
        episode_gap_max_min=("gap_minutes", lambda s: s.max(skipna=True)),
    ).reset_index()
    m = m.merge(ep_times, on="episode_id", how="left")
    m["episode_outtime_status"] = np.where(
        m["episode_has_null_stay_outtime"] | m["episode_outtime_ts"].isna(),
        "missing_or_open", "ok")
    m["episode_mapping_status"] = np.where(
        m["stay_seq_in_episode"] == 1, "episode_start",
        m["edge_mapping_status"].fillna("episode_start"))
    m["episode_merge_decision"] = np.where(
        m["stay_seq_in_episode"] == 1, "episode_start",
        m[decision_col].fillna("split"))
    m["episode_mapping_version"] = version
    m["episode_merge_threshold_min"] = tau

    cols = ["subject_id", "hadm_id", "episode_id", "episode_mapping_version",
            "stay_id", "stay_seq_in_episode", "episode_intime_ts",
            "episode_outtime_ts", "episode_outtime_status",
            "episode_has_null_stay_outtime", "gap_minutes",
            "episode_merge_decision", "episode_merge_exclusion_reason",
            "overlap_flag", "intervening_careunit", "transfer_evidence",
            "episode_mapping_status", "unresolved_conservative_split",
            "episode_merge_threshold_min", "episode_gap_max_min",
            "episode_transfer_path_class"]
    out = m[cols].rename(columns={"gap_minutes": "gap_minutes_from_prev_stay"})
    return out


def run_c0(con, versions=None):
    versions = versions or list(config.EPISODE_MERGE_THRESHOLDS.keys())
    out = config.OUTPUT_DIRS["episodes"]

    edges_base = _classify_edges(_build_base_edges(con))
    all_edges = _write_edges(edges_base)

    # 阶段③：adjudications —— 仅 pending_review 边进入（裁决范围冻结）
    pend = all_edges[all_edges["preliminary_decision"] == "pending_review"][
        ["hadm_id", "previous_stay_id", "current_stay_id",
         "episode_mapping_version"]].drop_duplicates()
    adj = pend.copy()
    adj["adjudication_status"] = "pending"
    adj["final_decision"] = None
    adj["adjudication_source"] = None
    utils.write_parquet(adj, out / "episode_merge_adjudications.parquet")
    print(f"  adjudications (pending edges): {len(adj):,} rows")

    stays = _stays_frame(con)
    prelim_frames, final_frames = [], []
    for version in versions:
        tau = config.EPISODE_MERGE_THRESHOLDS[version]
        ev = all_edges[all_edges["episode_mapping_version"] == version].copy()
        adj_v = adj[adj["episode_mapping_version"] == version].copy()
        prelim_frames.append(
            _build_maps_for_version(con, ev, version, tau, None, stays,
                                    stage="preliminary"))
        final_frames.append(
            _build_maps_for_version(con, ev, version, tau, adj_v, stays,
                                    stage="final"))

    prelim = pd.concat(prelim_frames, ignore_index=True)
    utils.write_parquet(prelim, out / "mimic_icu_episode_map_preliminary.parquet")
    print(f"  map_preliminary: {len(prelim):,} rows")

    final = pd.concat(final_frames, ignore_index=True)
    utils.write_parquet(final, out / "mimic_icu_episode_map_final.parquet")
    print(f"  map_final: {len(final):,} rows")

    # Q1-8: 唯一性（stay 级 final map）
    for version in versions:
        fv = final[final["episode_mapping_version"] == version]
        # ① 每 stay_id 恰好一个 episode_id
        assert fv.groupby("stay_id")["episode_id"].nunique().max() == 1, \
            f"stay_id in multiple episodes ({version})"
        # ② (episode_id, stay_id) 无重复行
        assert not fv.duplicated(["episode_id", "stay_id"]).any(), \
            f"duplicate (episode_id, stay_id) rows ({version})"
        # ③ episode_id 不跨 hadm（全局唯一命名空间）
        assert fv.groupby("episode_id")["hadm_id"].nunique().max() == 1, \
            f"episode_id spans hadm_id ({version})"
        # ④ final_decision 状态空间合法
        assert fv["episode_merge_decision"].isin(
            ["episode_start", "merged", "split"]).all(), \
            f"illegal final decision state ({version})"
    print("  Q1-8 uniqueness/state-space assertions passed")
    return len(final)
