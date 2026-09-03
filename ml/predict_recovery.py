"""
ML Phase A, step 3: prediction wrapper.

Loads the trained model once and exposes predict_recovery_probability() for
the candidate-action engine (backend/candidate_actions.py) to call. This is
the ONLY place model inference happens — decision/policy logic never touches
sklearn directly, keeping the ML layer swappable and the policy layer
deterministic and independent of it.
"""

import json
from functools import lru_cache

import joblib
import pandas as pd
from config import MODEL_METADATA_PATH, MODEL_PATH, MODEL_SCALER_PATH


@lru_cache(maxsize=1)
def _load():
    model = joblib.load(MODEL_PATH)
    metadata = json.loads(MODEL_METADATA_PATH.read_text())
    scaler = None
    if metadata.get("uses_scaled_features"):
        scaler = joblib.load(MODEL_SCALER_PATH)
    return model, metadata, scaler


def predict_recovery_probability(
    *, action: str, amount: float, attempt_count: int, hour: int, dow: int,
    failure_code: str = "no_failure_code", record_type: str = "transaction", days_overdue: int = -1,
    hours_since_last_attempt: float = -1, is_repeat_failure: int | None = None,
) -> float:
    """Returns P(recovery) for one candidate action in one context. 0-1 float.

    hours_since_last_attempt: -1 sentinel if there's no prior attempt.
    is_repeat_failure: defaults to derived-from-attempt_count (>=1) if not
    passed explicitly — callers may still pass it explicitly for clarity.
    """
    model, metadata, scaler = _load()

    if is_repeat_failure is None:
        is_repeat_failure = int(attempt_count >= 1)

    row = pd.DataFrame([{
        "failure_code": failure_code, "action": action, "record_type": record_type,
        "amount": amount, "attempt_count": attempt_count, "hour": hour, "dow": dow,
        "days_overdue": days_overdue, "hours_since_last_attempt": hours_since_last_attempt,
        "is_repeat_failure": is_repeat_failure,
    }])
    row["failure_action"] = row["failure_code"].astype(str) + "::" + row["action"].astype(str)

    X = pd.get_dummies(row, columns=metadata["categorical_columns"])
    # align to the exact training-time column set — any dummy the model saw in
    # training but that didn't appear for this single row must be added as 0,
    # and column order must match exactly what the model was fit on.
    for col in metadata["feature_columns"]:
        if col not in X.columns:
            X[col] = 0
    X = X[metadata["feature_columns"]]

    if scaler is not None:
        X[metadata["numeric_columns"]] = scaler.transform(X[metadata["numeric_columns"]])

    proba = model.predict_proba(X)[0, 1]
    return float(proba)


if __name__ == "__main__":
    # quick sanity check mirroring the spec's own worked example
    for action in ["RETRY_NOW", "RETRY_LATER"]:
        p = predict_recovery_probability(
            action=action, amount=10000, attempt_count=0, hour=14, dow=2,
            failure_code="bank_timeout",
        )
        print(f"bank_timeout + {action}: P(recovery) = {p:.2f}, expected value = ₹{10000*p:,.0f}")

    for action in ["RETRY_NOW", "RETRY_LATER"]:
        p = predict_recovery_probability(
            action=action, amount=10000, attempt_count=0, hour=14, dow=2,
            failure_code="insufficient_funds",
        )
        print(f"insufficient_funds + {action}: P(recovery) = {p:.2f}, expected value = ₹{10000*p:,.0f}")
