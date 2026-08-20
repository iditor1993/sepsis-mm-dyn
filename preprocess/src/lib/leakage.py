"""Leakage assertions (方案 §12.1, 附录 A.4)."""


def assert_train_fitted(artifact: dict):
    assert artifact.get("fitted_on") == "train", \
        f"拟合工件非训练集来源: {artifact.get('name', '?')}"


def assert_split_purity(sample_df, subject_col="subject_key",
                        set_col="set_name"):
    dup = sample_df.groupby(subject_col)[set_col].nunique()
    assert (dup == 1).all(), \
        f"存在跨集合患者: {int((dup > 1).sum())} subjects"


def assert_ecg_leakage_free(modality_index,
                            avail_col="ecg_available_time_assumed",
                            lm_col="t_landmark_ts"):
    bad = modality_index[
        modality_index["ecg_selected_for_model"]
        & (modality_index[avail_col] > modality_index[lm_col])]
    assert len(bad) == 0, f"ECG 可用时间晚于 landmark: {len(bad)} rows"


def assert_mask_nan_policy(x, m):
    """X_seq 缺失位置必须 mask=0 且值为 0（方案 §12.2）。"""
    import numpy as np
    assert not np.isnan(x).any(), "X_seq 含 NaN"
    assert (x[~m] == 0).all(), "mask=0 位置值非 0"
