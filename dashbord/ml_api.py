"""
ml_api.py
──────────────────────────────────────────────────────────────────────────────
Smart Irrigation – ML Prediction Service
Runs as a separate Flask microservice on port 5001.
The main dashboard (app.py) calls it via HTTP.

Endpoints:
  POST /predict          → single prediction
  POST /predict/batch    → list of readings
  GET  /compare          → RF vs rule-based on a test sample
  GET  /health           → liveness + model metadata
  GET  /feature_importance → importances as JSON

Usage:
  python ml_api.py
  # then from app.py: requests.post("http://localhost:5001/predict", json={...})
──────────────────────────────────────────────────────────────────────────────
"""

import json
import numpy as np
import joblib
from flask import Flask, request, jsonify

# ── Load artefacts ──────────────────────────────────────────────────────────
MODEL_PATH = "../ML Model/Model/rf_valve_model.joblib"
META_PATH  = "../ML Model/Model/model_meta.json"

try:
    rf_model = joblib.load(MODEL_PATH)
    with open(META_PATH) as f:
        model_meta = json.load(f)
    FEATURES = model_meta["features"]
    print(f"[ML API] Model loaded — features: {FEATURES}")
except FileNotFoundError:
    raise RuntimeError("Model not found. Run train_model.ipynb first.")

# ── Gateway constants (mirrored from gateway.ino) ───────────────────────────
MOISTURE_DRY = 40.0
MOISTURE_WET = 70.0
VALVE_MAX    = 120
VALVE_MIN    = 10
VALVE_CLOSED = 0
HUM_HIGH     = 70.0
HUM_LOW      = 30.0
TEMP_HOT     = 33.0

app = Flask(__name__)


# ── Helper: build feature vector ────────────────────────────────────────────
def build_features(moisture: float, temperature: float,
                   humidity: float) -> list:
    """
    Derives all engineered features from raw sensor values.
    Features: ["moisture", "temperature", "humidity", "moisture_deficit", "hum_factor"]
    Must stay in sync with train_model.ipynb FEATURE_COLS.
    """
    moisture_deficit = max(0.0, MOISTURE_DRY - moisture)
    hum_factor       = (-1 if humidity > HUM_HIGH
                        else (1 if humidity < HUM_LOW else 0))

    return [
        moisture,          # moisture
        temperature,       # temperature
        humidity,          # humidity
        moisture_deficit,  # moisture_deficit
        hum_factor,        # hum_factor
    ]


# ── Helper: gateway rule replica (mirrors handleAutoIrrigation in gateway.ino)
def gateway_rule(moisture: float, humidity: float, temp: float) -> int:
    if moisture >= MOISTURE_WET:
        return VALVE_CLOSED
    elif moisture < MOISTURE_DRY:
        dry_ratio = 1.0 - (moisture / MOISTURE_DRY)
        angle_f   = dry_ratio * VALVE_MAX
        if humidity > HUM_HIGH:
            angle_f *= 0.7
        elif humidity < HUM_LOW:
            angle_f *= 1.2
        if temp > TEMP_HOT:
            angle_f *= 1.15
        return int(np.clip(angle_f, VALVE_MIN, VALVE_MAX))
    else:
        band_ratio = 1.0 - ((moisture - MOISTURE_DRY) / (MOISTURE_WET - MOISTURE_DRY))
        return int(np.clip(band_ratio * VALVE_MIN, 0, VALVE_MIN))


def angle_to_state(angle: float) -> str:
    a = int(round(angle))
    if a == 0:   return "CLOSED"
    if a < 10:   return "EASING"
    if a < 60:   return "PARTIAL"
    return "FULL_OPEN"


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "model":  MODEL_PATH,
        "meta":   model_meta,
    })


@app.route("/feature_importance", methods=["GET"])
def feature_importance():
    imps = dict(zip(FEATURES, rf_model.feature_importances_.tolist()))
    return jsonify({"importances": imps})


@app.route("/predict", methods=["POST"])
def predict():
    """
    Body (JSON):
        {
            "moisture":    55.3,     // %
            "temperature": 29.1,     // °C
            "humidity":    62.0      // %
        }

    Response:
        {
            "ml_angle":       45,
            "ml_state":       "PARTIAL",
            "rule_angle":     42,
            "rule_state":     "PARTIAL",
            "agreement":      true,
            "delta":          3,
            "recommendation": "IRRIGATE",
            "confidence":     0.91
        }
    """
    body = request.get_json(force=True, silent=True) or {}

    # ── Validate ────────────────────────────────────────────────────────────
    required = ("moisture", "temperature", "humidity")
    missing  = [k for k in required if k not in body]
    if missing:
        return jsonify({"error": f"Missing fields: {missing}"}), 400

    moisture    = float(body["moisture"])
    temperature = float(body["temperature"])
    humidity    = float(body["humidity"])

    # ── Predict ─────────────────────────────────────────────────────────────
    features   = build_features(moisture, temperature, humidity)
    ml_raw     = rf_model.predict([features])[0]
    ml_angle   = int(np.clip(round(ml_raw), 0, VALVE_MAX))

    rule_angle = gateway_rule(moisture, humidity, temperature)

    ml_state   = angle_to_state(ml_angle)
    rule_state = angle_to_state(rule_angle)
    delta      = abs(ml_angle - rule_angle)
    agreement  = (ml_state == rule_state)

    # Confidence: use std of per-tree predictions as uncertainty proxy
    tree_preds = np.array([t.predict([features])[0] for t in rf_model.estimators_])
    std_dev    = float(tree_preds.std())
    confidence = float(max(0.0, min(1.0, 1.0 - std_dev / VALVE_MAX)))

    recommendation = "IRRIGATE" if ml_angle >= VALVE_MIN else "NO_IRRIGATION"

    return jsonify({
        "ml_angle":       ml_angle,
        "ml_state":       ml_state,
        "rule_angle":     rule_angle,
        "rule_state":     rule_state,
        "agreement":      agreement,
        "delta":          delta,
        "recommendation": recommendation,
        "confidence":     round(confidence, 4),
        "input": {
            "moisture":    moisture,
            "temperature": temperature,
            "humidity":    humidity,
        }
    })


@app.route("/predict/batch", methods=["POST"])
def predict_batch():
    """
    Body: list of sensor dicts (same schema as /predict).
    Returns: list of prediction dicts.
    """
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, list):
        return jsonify({"error": "Expected a JSON array"}), 400

    results = []
    for item in body:
        moisture    = float(item.get("moisture",    50))
        temperature = float(item.get("temperature", 25))
        humidity    = float(item.get("humidity",    50))

        features   = build_features(moisture, temperature, humidity)
        ml_raw     = rf_model.predict([features])[0]
        ml_angle   = int(np.clip(round(ml_raw), 0, VALVE_MAX))
        rule_angle = gateway_rule(moisture, humidity, temperature)

        results.append({
            "ml_angle":   ml_angle,
            "ml_state":   angle_to_state(ml_angle),
            "rule_angle": rule_angle,
            "rule_state": angle_to_state(rule_angle),
            "agreement":  angle_to_state(ml_angle) == angle_to_state(rule_angle),
        })

    return jsonify(results)


if __name__ == "__main__":
    print("[ML API] Starting on http://0.0.0.0:5001")
    app.run(host="0.0.0.0", port=5001, debug=False)