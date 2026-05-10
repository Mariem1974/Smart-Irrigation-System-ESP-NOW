"""
retrain_on_real_data.py
──────────────────────────────────────────────────────────────────────────────
Run this periodically after deployment to improve the model using real
sensor readings stored in SQLite instead of the simulated dataset.

Usage:
    python retrain_on_real_data.py
    python retrain_on_real_data.py --db path/to/irrigation.db
    python retrain_on_real_data.py --min-samples 200

Everything else (RF settings, evaluation, saving) is identical to train_model.ipynb.
──────────────────────────────────────────────────────────────────────────────
"""

import argparse
import json
import sqlite3
import sys

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble        import RandomForestRegressor
from sklearn.metrics         import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import train_test_split, cross_val_score

# ── Paths ────────────────────────────────────────────────────────────────────
DB_PATH    = "irrigation.db"  
MODEL_PATH = "../ML Model/Model/rf_valve_model.joblib"
META_PATH  = "../ML Model/Model/model_meta.json"

# ── Must match train_model.ipynb exactly ─────────────────────────────────────
MOISTURE_DRY = 40.0
TEMP_HOT     = 33.0
HUM_HIGH     = 70.0
HUM_LOW      = 30.0
VALVE_MAX    = 120
FEATURE_COLS = [
    "moisture", "temperature", "humidity",
    "moisture_deficit", "hum_factor",
]
TARGET_COL = "valve_angle"


def load_from_sqlite(db_path: str) -> pd.DataFrame:
    """
    Pull sensor readings and the valve angle that followed each one
    from the SQLite database written by app.py.
    """
    conn = sqlite3.connect(db_path)

    df = pd.read_sql_query("""
        SELECT
            s.soil_moisture                    AS moisture,
            s.temperature                      AS temperature,
            s.air_humidity                     AS humidity,
            v.servo_angle                      AS valve_angle
        FROM sensor_readings s
        JOIN valve_state v
          ON v.id = (
              SELECT id FROM valve_state
              WHERE timestamp >= s.timestamp
              ORDER BY timestamp ASC
              LIMIT 1
          )
        WHERE s.soil_moisture >= 0
          AND s.temperature   BETWEEN 5  AND 50
          AND s.air_humidity  BETWEEN 10 AND 95
          AND v.servo_angle   BETWEEN 0  AND 120
        ORDER BY s.timestamp
    """, conn)

    conn.close()
    return df


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """Same feature engineering as train_model.ipynb — must stay in sync."""
    df = df.copy()
    df["moisture_deficit"] = (MOISTURE_DRY - df["moisture"]).clip(lower=0)
    df["hum_factor"]       = df["humidity"].apply(
        lambda h: -1 if h > HUM_HIGH else (1 if h < HUM_LOW else 0)
    )
    return df


def regression_report(name: str, y_true, y_pred) -> dict:
    mae  = mean_absolute_error(y_true, y_pred)
    rmse = np.sqrt(mean_squared_error(y_true, y_pred))
    r2   = r2_score(y_true, y_pred)
    print(f"\n{'─'*50}")
    print(f" {name}")
    print(f"{'─'*50}")
    print(f"  MAE  : {mae:.3f}°")
    print(f"  RMSE : {rmse:.3f}°")
    print(f"  R²   : {r2:.4f}")
    return {"mae": round(mae, 4), "rmse": round(rmse, 4), "r2": round(r2, 4)}


def main(db_path: str, min_samples: int):
    print("=" * 60)
    print("  Smart Irrigation – Retrain on Real Data")
    print("=" * 60)

    # 1. Load from SQLite ─────────────────────────────────────────────────────
    print(f"\n[DB] Reading from {db_path} …")
    try:
        df = load_from_sqlite(db_path)
    except Exception as e:
        print(f"[ERROR] Could not read database: {e}")
        sys.exit(1)

    print(f"[DB] {len(df)} valid rows found after filtering")

    if len(df) < min_samples:
        print(f"[ABORT] Need at least {min_samples} samples to retrain. "
              f"Only {len(df)} available — keep collecting data and try later.")
        sys.exit(0)

    # 2. Feature engineering ──────────────────────────────────────────────────
    df = engineer_features(df)

    # 3. Print quick data summary ─────────────────────────────────────────────
    print(f"\n[DATA] Valve angle distribution:")
    print(f"  CLOSED (0)      : {(df.valve_angle == 0).sum()}")
    print(f"  Hysteresis (1-9): {((df.valve_angle > 0) & (df.valve_angle < 10)).sum()}")
    print(f"  Open (10-120)   : {(df.valve_angle >= 10).sum()}")
    print(f"\n[DATA] Sensor ranges:")
    print(f"  Moisture  : {df.moisture.min():.1f}% – {df.moisture.max():.1f}%")
    print(f"  Temp      : {df.temperature.min():.1f}°C – {df.temperature.max():.1f}°C")
    print(f"  Humidity  : {df.humidity.min():.1f}% – {df.humidity.max():.1f}%")

    X = df[FEATURE_COLS]
    y = df[TARGET_COL]

    # 4. Train / test split ───────────────────────────────────────────────────
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42
    )
    print(f"\n[SPLIT] Train: {len(X_train)}  |  Test: {len(X_test)}")

    # 5. Train ────────────────────────────────────────────────────────────────
    rf = RandomForestRegressor(
        n_estimators     = 10,
        max_depth        = None,
        min_samples_leaf = 2,
        max_features     = "sqrt",
        n_jobs           = -1,
        random_state     = 42,
    )
    print("\n[TRAIN] Fitting Random Forest on real data …")
    rf.fit(X_train, y_train)

    # 6. Evaluate ─────────────────────────────────────────────────────────────
    preds   = rf.predict(X_test).clip(0, VALVE_MAX)
    metrics = regression_report("Random Forest – Test Set (real data)", y_test, preds)

    cv_scores = cross_val_score(rf, X, y, cv=5, scoring="r2", n_jobs=-1)
    print(f"\n[CV] 5-fold R²: {np.round(cv_scores, 4)}  |  mean: {cv_scores.mean():.4f}")

    # 7. Compare with old model ───────────────────────────────────────────────
    try:
        old_rf      = joblib.load(MODEL_PATH)
        old_preds   = old_rf.predict(X_test).clip(0, VALVE_MAX)
        old_metrics = regression_report("Old model on same test set", y_test, old_preds)

        print(f"\n{'─'*50}")
        print(f" Old vs New model")
        print(f"{'─'*50}")
        improved = metrics["r2"] >= old_metrics["r2"]
        print(f"  Old R²  : {old_metrics['r2']:.4f}")
        print(f"  New R²  : {metrics['r2']:.4f}")
        print(f"  Result  : {'✓ New model is better — saving' if improved else '✗ Old model was better — still saving (real data preferred)'}")
    except FileNotFoundError:
        print("[INFO] No existing model found — saving new model directly.")

    # 8. Save ─────────────────────────────────────────────────────────────────
    joblib.dump(rf, MODEL_PATH)
    print(f"\n[SAVE] Model → {MODEL_PATH}")

    meta = {
        "features":     FEATURE_COLS,
        "target":       TARGET_COL,
        "valve_min":    10,
        "valve_max":    VALVE_MAX,
        "n_estimators": 10,
        "data_source":  "real_sqlite",
        "n_samples":    len(df),
        "test_mae":     metrics["mae"],
        "test_r2":      metrics["r2"],
        "cv_r2_mean":   round(float(cv_scores.mean()), 4),
    }
    with open(META_PATH, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"[SAVE] Meta  → {META_PATH}")
    print("\n[DONE] Retrain complete. Restart ml_api.py to load the new model.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db",          default=DB_PATH, help="Path to irrigation.db")
    parser.add_argument("--min-samples", default=100, type=int,
                        help="Minimum rows needed before retraining (default 100)")
    args = parser.parse_args()
    main(args.db, args.min_samples)