"""
ML Phase A, step 2: train recovery probability model.

Trains a Logistic Regression baseline and a Random Forest, compares them
honestly on held-out data, and saves the better one. No leakage: only
pre-action features (failure_code, action, amount, attempt_count, hour, dow,
days_overdue, record_type) are used — never anything derived from the outcome.

Split: stratified random 80/20. NOTE on methodology: a chronological split
was considered (per common ML best-practice for production systems), but this
dataset has no meaningful time ordering — every row is an independently drawn
synthetic case, not a real event stream — so a chronological split would be
theater, not a real leakage safeguard. The actual leakage safeguard here is
feature selection (pre-action fields only), which is enforced above.
"""

import json

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score, brier_score_loss, confusion_matrix,
    precision_score, recall_score, roc_auc_score,
)
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler

FEATURE_COLUMNS_CATEGORICAL = ["failure_action", "record_type"]
# hours_since_last_attempt and is_repeat_failure are the Phase 3 additions —
# see ml/generate_training_data.py module docstring for why they're genuine
# signal and not constants-in-disguise.
FEATURE_COLUMNS_NUMERIC = [
    "amount", "attempt_count", "hour", "dow", "days_overdue",
    "hours_since_last_attempt", "is_repeat_failure",
]


def build_features(df: pd.DataFrame):
    df = df.copy()
    # Explicit interaction term: plain additive dummies for failure_code and
    # action separately cannot represent "bank_timeout prefers RETRY_NOW but
    # insufficient_funds prefers RETRY_LATER" — the model would blend the two
    # into one overall per-action main effect and get failure-specific
    # rankings backwards (verified this empirically before adding this fix).
    # Combining them into one categorical column lets the model learn each
    # (failure_code, action) pair's own coefficient.
    df["failure_action"] = df["failure_code"].astype(str) + "::" + df["action"].astype(str)
    X = pd.get_dummies(df[FEATURE_COLUMNS_CATEGORICAL + FEATURE_COLUMNS_NUMERIC],
                        columns=FEATURE_COLUMNS_CATEGORICAL)
    return X


def evaluate(name, model, X_test, y_test):
    proba = model.predict_proba(X_test)[:, 1]
    preds = (proba >= 0.5).astype(int)
    metrics = {
        "roc_auc": round(roc_auc_score(y_test, proba), 4),
        "pr_auc": round(average_precision_score(y_test, proba), 4),
        "precision": round(precision_score(y_test, preds, zero_division=0), 4),
        "recall": round(recall_score(y_test, preds, zero_division=0), 4),
        "brier_score": round(brier_score_loss(y_test, proba), 4),  # lower = better calibrated
    }
    cm = confusion_matrix(y_test, preds).tolist()
    print(f"\n{name}")
    print(f"  ROC-AUC:  {metrics['roc_auc']}")
    print(f"  PR-AUC:   {metrics['pr_auc']}")
    print(f"  Precision:{metrics['precision']}   Recall: {metrics['recall']}")
    print(f"  Brier:    {metrics['brier_score']} (lower = better calibrated)")
    print(f"  Confusion matrix [[TN,FP],[FN,TP]]: {cm}")
    return metrics, cm


def calibration_table(proba, y_test, n_bins: int = 10) -> list[dict]:
    """Bins predictions into deciles and compares mean predicted probability
    vs actual outcome rate in each bin. This is more convincing evidence for
    the EV-based decision logic than ROC-AUC alone, since EV math (amount x
    P(success)) only makes sense if the predicted probabilities are actually
    calibrated, not just well-ranked."""
    df = pd.DataFrame({"proba": proba, "outcome": y_test.values})
    df["bin"] = pd.qcut(df["proba"], q=n_bins, duplicates="drop")
    grouped = df.groupby("bin", observed=True).agg(
        predicted_mean=("proba", "mean"),
        actual_rate=("outcome", "mean"),
        n=("outcome", "count"),
    ).reset_index(drop=True)
    return [
        {
            "predicted_mean": round(float(row.predicted_mean), 4),
            "actual_rate": round(float(row.actual_rate), 4),
            "n": int(row.n),
        }
        for row in grouped.itertuples()
    ]


def train():
    df = pd.read_csv("ml/training_data.csv", keep_default_na=False, na_values=[""])
    df["days_overdue"] = df["days_overdue"].fillna(-1)
    df["hours_since_last_attempt"] = df["hours_since_last_attempt"].fillna(-1)
    df["is_repeat_failure"] = df["is_repeat_failure"].fillna(0)

    X = build_features(df)
    y = df["outcome"]
    feature_columns = list(X.columns)

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )

    # Scale numeric columns for the Logistic Regression (RF is scale-invariant,
    # but sharing one scaled matrix keeps the pipeline simple and fixes LR's
    # convergence warning from amount's much larger magnitude vs attempt_count/hour).
    scaler = StandardScaler()
    X_train_scaled = X_train.copy()
    X_test_scaled = X_test.copy()
    X_train_scaled[FEATURE_COLUMNS_NUMERIC] = scaler.fit_transform(X_train[FEATURE_COLUMNS_NUMERIC])
    X_test_scaled[FEATURE_COLUMNS_NUMERIC] = scaler.transform(X_test[FEATURE_COLUMNS_NUMERIC])

    lr = LogisticRegression(max_iter=1000, class_weight="balanced")
    lr.fit(X_train_scaled, y_train)
    lr_metrics, lr_cm = evaluate("Logistic Regression (baseline)", lr, X_test_scaled, y_test)

    rf = RandomForestClassifier(n_estimators=200, max_depth=6, min_samples_leaf=15,
                                 random_state=42, class_weight="balanced")
    rf.fit(X_train, y_train)  # unscaled is fine for RF
    rf_metrics, rf_cm = evaluate("Random Forest", rf, X_test, y_test)

    # Select on ROC-AUC (standard for ranking candidate actions by probability);
    # ties/near-ties favor the simpler, more interpretable Logistic Regression.
    if rf_metrics["roc_auc"] > lr_metrics["roc_auc"] + 0.01:
        chosen_name, chosen_model, chosen_metrics = "random_forest", rf, rf_metrics
    else:
        chosen_name, chosen_model, chosen_metrics = "logistic_regression", lr, lr_metrics

    print(f"\n{'='*60}\nSELECTED MODEL: {chosen_name}\n{'='*60}")

    # Calibration check on the chosen model's test-set predictions.
    chosen_X_test = X_test_scaled if chosen_name == "logistic_regression" else X_test
    chosen_proba = chosen_model.predict_proba(chosen_X_test)[:, 1]
    calibration = calibration_table(chosen_proba, y_test)
    print("\nCalibration (predicted vs actual, by decile):")
    for row in calibration:
        print(f"  predicted={row['predicted_mean']:.3f}  actual={row['actual_rate']:.3f}  n={row['n']}")

    joblib.dump(chosen_model, "models/recovery_model.pkl")
    joblib.dump(scaler, "models/feature_scaler.pkl")
    metadata = {
        "model_type": chosen_name,
        "feature_columns": feature_columns,
        "categorical_columns": FEATURE_COLUMNS_CATEGORICAL,
        "numeric_columns": FEATURE_COLUMNS_NUMERIC,
        "uses_interaction_feature": "failure_action",
        "uses_scaled_features": chosen_name == "logistic_regression",
        "metrics": {"logistic_regression": lr_metrics, "random_forest": rf_metrics},
        "train_rows": len(X_train),
        "test_rows": len(X_test),
        "training_data_source": "ml/training_data.csv (synthetic, simulator-generated — see DATA_SOURCES.md)",
        "calibration_deciles": calibration,
    }
    with open("models/model_metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)

    print(f"\nSaved model to models/recovery_model.pkl")
    print(f"Saved metadata to models/model_metadata.json")


if __name__ == "__main__":
    train()
