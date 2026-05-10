"""
Smart Irrigation Dashboard - Flask Backend
Connects to real ESP32 Gateway via MQTT Broker
"""

from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response
import sqlite3
import hashlib
import json
import time
import threading
import queue
import os
from datetime import datetime

try:
    import requests as http_requests
    ML_API_AVAILABLE = True
except ImportError:
    ML_API_AVAILABLE = False
    print("[WARNING] requests not installed. Run: pip install requests")

try:
    import paho.mqtt.client as mqtt_lib
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    print("[WARNING] paho-mqtt not installed. Run: pip install paho-mqtt")

app = Flask(__name__)
app.secret_key = "irrigation_secret_2025"
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

DB_PATH     = "irrigation.db"
MQTT_BROKER = "192.168.1.8"
MQTT_PORT   = 1883

TOPIC_DATA     = "irrigation/sensor/data"
TOPIC_ALERT    = "irrigation/sensor/alert"
TOPIC_ACK      = "irrigation/sensor/ack"
TOPIC_VALVE    = "irrigation/valve/control"
TOPIC_SLEEP    = "irrigation/sensor/sleep"
TOPIC_BATCH    = "irrigation/sensor/batch"
TOPIC_ML_VALVE = "irrigation/ml/valve"

ML_API_URL  = "http://localhost:5001/predict"

# ML mode state (mirrors mlControlActive on the gateway)
ml_mode_active = False

# SSE clients
sse_clients = []
sse_lock    = threading.Lock()

def is_mobile_client():
    user_agent = request.headers.get("User-Agent", "").lower()
    mobile_tokens = ("android", "iphone", "ipad", "ipod", "mobile")
    return any(token in user_agent for token in mobile_tokens)

def home_endpoint():
    return "mobile" if is_mobile_client() else "dashboard"

@app.after_request
def disable_cache_for_development(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

def push_sse_event(event_type, data):
    msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with sse_lock:
        dead = []
        for q in sse_clients:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            sse_clients.remove(q)

# MQTT
mqtt_connected = False
mqtt_client    = None

def on_mqtt_connect(client, userdata, flags, rc):
    global mqtt_connected
    if rc == 0:
        mqtt_connected = True
        print(f"[MQTT] Connected to {MQTT_BROKER}:{MQTT_PORT}")
        client.subscribe(TOPIC_DATA)
        client.subscribe(TOPIC_ALERT)
        client.subscribe(TOPIC_ACK)
        client.subscribe(TOPIC_BATCH)
    else:
        mqtt_connected = False
        print(f"[MQTT] Failed rc={rc}")

def on_mqtt_disconnect(client, userdata, rc):
    global mqtt_connected
    mqtt_connected = False
    if rc != 0:
        print(f"[MQTT] Unexpected disconnect rc={rc}, will auto-reconnect...")
    else:
        print("[MQTT] Clean disconnect")

def call_ml_api(moisture, temperature, humidity):
    """
    Calls ml_api.py /predict endpoint.
    Returns prediction dict or None if the service is unavailable.
    """
    if not ML_API_AVAILABLE:
        return None
    try:
        resp = http_requests.post(ML_API_URL, json={
            "moisture":    moisture,
            "temperature": temperature,
            "humidity":    humidity,
        }, timeout=2)
        return resp.json()
    except Exception as e:
        print(f"[ML API] Unavailable: {e}")
        return None

def on_mqtt_message(client, userdata, msg):
    global ml_mode_active

    topic   = msg.topic
    payload = msg.payload.decode("utf-8", errors="ignore")
    print(f"[MQTT] {topic}: {payload[:120]}")

    if topic == TOPIC_DATA:
        try:
            data     = json.loads(payload)
            node     = data.get("node", "NODE_A")
            temp     = float(data.get("temp",     -1))
            hum      = float(data.get("hum",      -1))
            moisture = float(data.get("moisture", -1))
            valve    = data.get("valve", "CLOSED")
            angle    = int(data.get("valve_angle", 0))

            if moisture < 0:
                return

            # ── Store in SQLite ───────────────────────────────────────────
            conn = get_db()
            conn.execute(
                "INSERT INTO sensor_readings (node_id, soil_moisture, temperature, air_humidity) VALUES (?,?,?,?)",
                (node, moisture, temp, hum)
            )
            conn.execute(
                "INSERT INTO valve_state (is_open, servo_angle, changed_by) VALUES (?,?,?)",
                (1 if valve == "OPEN" else 0, angle, "ESP32_AUTO")
            )
            conn.commit()
            conn.close()

            # ── Call ML API ───────────────────────────────────────────────
            ml_result = call_ml_api(moisture, temp, hum)
            ml_angle  = None
            rule_angle = None

            if ml_result:
                ml_angle   = ml_result.get("ml_angle")
                rule_angle = ml_result.get("rule_angle")

                # If ML mode is active, push the angle to the gateway
                if ml_mode_active and mqtt_client and mqtt_connected:
                    mqtt_client.publish(TOPIC_ML_VALVE, str(ml_angle))
                    print(f"[ML] Published angle {ml_angle}° to gateway")

            # ── Push to dashboard via SSE ─────────────────────────────────
            sse_payload = {
                "node": node, "temp": temp, "hum": hum,
                "moisture": moisture, "valve": valve, "angle": angle,
                "ts": datetime.now().strftime("%H:%M:%S"),
                "ml_angle":   ml_angle,
                "rule_angle": rule_angle,
                "ml_active":  ml_mode_active,
            }
            push_sse_event("sensor_update", sse_payload)

        except Exception as e:
            print(f"[MQTT] Parse error: {e}")

    elif topic == TOPIC_ALERT:
        parts = payload.split(":")
        alert_data = {
            "raw":    payload,
            "node":   parts[1] if len(parts) > 1 else "?",
            "type":   parts[2] if len(parts) > 2 else "UNKNOWN",
            "detail": parts[3] if len(parts) > 3 else "",
            "ts":     datetime.now().strftime("%H:%M:%S")
        }
        conn = get_db()
        conn.execute(
            "INSERT INTO alerts (node_id, alert_type, detail, raw_message) VALUES (?,?,?,?)",
            (alert_data["node"], alert_data["type"], alert_data["detail"], payload)
        )
        conn.commit()
        conn.close()
        push_sse_event("alert", alert_data)

    elif topic == TOPIC_ACK:
        push_sse_event("ack", {
            "message": payload,
            "ts": datetime.now().strftime("%H:%M:%S")
        })

def start_mqtt():
    global mqtt_client
    if not MQTT_AVAILABLE:
        print("[MQTT] paho-mqtt not available")
        return

    client_id = f"Flask_Dashboard_{os.getpid()}"
    if hasattr(mqtt_lib, "CallbackAPIVersion"):
        mqtt_client = mqtt_lib.Client(
            mqtt_lib.CallbackAPIVersion.VERSION1,
            client_id=client_id
        )
    else:
        mqtt_client = mqtt_lib.Client(client_id=client_id)
    mqtt_client.on_connect    = on_mqtt_connect
    mqtt_client.on_disconnect = on_mqtt_disconnect
    mqtt_client.on_message    = on_mqtt_message
    mqtt_client.reconnect_delay_set(min_delay=5, max_delay=60)

    def _run():
        while True:
            try:
                mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
                mqtt_client.loop_forever(retry_first_connection=True)
            except Exception as e:
                print(f"[MQTT] Error: {e} — retrying in 15s")
                time.sleep(15)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    print("[MQTT] Background thread started")

# Database
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c    = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        full_name TEXT,
        role TEXT DEFAULT "viewer",
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS sensor_readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id TEXT NOT NULL,
        soil_moisture REAL,
        temperature REAL,
        air_humidity REAL,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS valve_state (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        is_open INTEGER DEFAULT 0,
        servo_angle INTEGER DEFAULT 0,
        changed_by TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS irrigation_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id TEXT,
        triggered_by TEXT DEFAULT "AUTO",
        duration_seconds INTEGER,
        water_used_liters REAL,
        ml_confidence REAL,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id TEXT,
        alert_type TEXT,
        detail TEXT,
        raw_message TEXT,
        acknowledged INTEGER DEFAULT 0,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    team = [
        ("admin",  "admin123",  "Admin User",             "admin"),
        ("shahd",  "shahd123",  "Shahd Ahmed Mahmoud",    "developer"),
        ("mariam", "mariam123", "Mariam Mohammed Ahmed",  "ml_engineer"),
        ("mariem", "mariem123", "Mariem Maher Mohammed",  "hardware"),
        ("rawan",  "rawan123",  "Rawan Gamal Abdullah",   "architect"),
    ]
    for u, p, n, r in team:
        hp = hashlib.sha256(p.encode()).hexdigest()
        try:
            c.execute("INSERT INTO users (username,password,full_name,role) VALUES (?,?,?,?)",
                      (u, hp, n, r))
        except sqlite3.IntegrityError:
            pass

    conn.commit()
    conn.close()
    print("[DB] Ready")

# Auth routes
@app.route("/")
def index():
    return redirect(url_for(home_endpoint()) if "user" in session else url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        hashed   = hashlib.sha256(password.encode()).hexdigest()
        conn     = get_db()
        user     = conn.execute(
            "SELECT * FROM users WHERE username=? AND password=? AND role=?",
            (username, hashed, "admin")
        ).fetchone()
        conn.close()
        if user:
            session["user"] = dict(user)
            return redirect(url_for(home_endpoint()))
        error = "Invalid credentials or insufficient permissions."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("dashboard.html", user=session["user"])

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# SSE stream
@app.route("/api/stream")
@login_required
def sse_stream():
    q = queue.Queue(maxsize=50)
    with sse_lock:
        sse_clients.append(q)

    def generate():
        yield "event: connected\ndata: {\"status\":\"ok\"}\n\n"
        try:
            while True:
                try:
                    msg = q.get(timeout=20)
                    yield msg
                except queue.Empty:
                    yield "event: heartbeat\ndata: {}\n\n"
        except GeneratorExit:
            with sse_lock:
                if q in sse_clients:
                    sse_clients.remove(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        }
    )

# Sensor APIs
@app.route("/api/sensors/latest")
@login_required
def api_sensors_latest():
    conn   = get_db()
    nodes  = ["NODE_A"]
    result = {}
    for node in nodes:
        row = conn.execute(
            "SELECT * FROM sensor_readings WHERE node_id=? ORDER BY timestamp DESC LIMIT 1",
            (node,)
        ).fetchone()
        if row:
            result[node] = dict(row)
    conn.close()
    return jsonify(result)

@app.route("/api/sensors/history/<node_id>")
@login_required
def api_sensor_history(node_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT soil_moisture, temperature, air_humidity, timestamp "
        "FROM sensor_readings WHERE node_id=? ORDER BY timestamp DESC LIMIT 24",
        (node_id,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in reversed(rows)])

# Valve API
@app.route("/api/valve", methods=["GET"])
@login_required
def api_valve_get():
    conn = get_db()
    row  = conn.execute(
        "SELECT * FROM valve_state ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return jsonify(dict(row) if row else {"is_open": 0, "servo_angle": 0})

@app.route("/api/valve", methods=["POST"])
@login_required
def api_valve_set():
    data    = request.json or {}
    user    = session["user"]["username"]
    angle   = 0
    command = ""

    if "angle" in data:
        angle   = max(0, min(120, int(data["angle"])))
        command = str(angle)
    elif data.get("open"):
        command = "OPEN"
        angle   = 120
    else:
        command = "CLOSED"
        angle   = 0

    if mqtt_client and mqtt_connected:
        mqtt_client.publish(TOPIC_VALVE, command)
        print(f"[API] Valve → {command} by {user}")

    is_open = 1 if command not in ("CLOSED", "0") else 0
    conn    = get_db()
    conn.execute(
        "INSERT INTO valve_state (is_open, servo_angle, changed_by) VALUES (?,?,?)",
        (is_open, angle, user)
    )
    conn.commit()
    conn.close()

    return jsonify({
        "status":    "ok",
        "command":   command,
        "mqtt_sent": mqtt_connected
    })

# ML mode API
@app.route("/api/ml/mode", methods=["POST"])
@login_required
def api_ml_mode():
    """
    Switch the gateway between rule-based and ML control.
    Body: { "mode": "ML" } or { "mode": "RULE" }
    """
    global ml_mode_active
    data = request.json or {}
    mode = data.get("mode", "RULE").upper()

    if mode == "ML":
        ml_mode_active = True
        if mqtt_client and mqtt_connected:
            mqtt_client.publish(TOPIC_VALVE, "MODE_ML")
        print("[ML] Switched to ML control")
    else:
        ml_mode_active = False
        if mqtt_client and mqtt_connected:
            mqtt_client.publish(TOPIC_VALVE, "MODE_RULE")
        print("[ML] Switched to rule-based control")

    push_sse_event("ml_mode", {"ml_active": ml_mode_active})
    return jsonify({"status": "ok", "ml_active": ml_mode_active})

@app.route("/api/ml/mode", methods=["GET"])
@login_required
def api_ml_mode_get():
    """Returns the current ML mode state."""
    return jsonify({"ml_active": ml_mode_active})

@app.route("/api/ml/predict", methods=["POST"])
@login_required
def api_ml_predict():
    """
    Manual one-off prediction from the dashboard.
    Body: { "moisture": 45, "temperature": 30, "humidity": 60 }
    """
    data = request.json or {}
    result = call_ml_api(
        float(data.get("moisture",    50)),
        float(data.get("temperature", 25)),
        float(data.get("humidity",    50)),
    )
    if result is None:
        return jsonify({"error": "ML API unavailable"}), 503
    return jsonify(result)

# Sleep API
@app.route("/api/sleep", methods=["POST"])
@login_required
def api_sleep():
    data    = request.json or {}
    node    = data.get("node", "NODE_A")
    seconds = int(data.get("seconds", 60))
    command = f"{node}:{seconds}"
    if mqtt_client and mqtt_connected:
        mqtt_client.publish(TOPIC_SLEEP, command)
    return jsonify({"status": "ok", "command": command, "mqtt_sent": mqtt_connected})

# Alerts API
@app.route("/api/alerts")
@login_required
def api_alerts():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM alerts ORDER BY timestamp DESC LIMIT 20"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/alerts/<int:alert_id>/ack", methods=["POST"])
@login_required
def api_alert_ack(alert_id):
    conn = get_db()
    conn.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (alert_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

# Stats API
@app.route("/api/stats")
@login_required
def api_stats():
    conn         = get_db()
    total_reads  = conn.execute("SELECT COUNT(*) as c FROM sensor_readings").fetchone()["c"]
    total_alerts = conn.execute(
        "SELECT COUNT(*) as c FROM alerts WHERE acknowledged=0"
    ).fetchone()["c"]
    valve_row    = conn.execute(
        "SELECT * FROM valve_state ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return jsonify({
        "total_readings": total_reads,
        "unacked_alerts": total_alerts,
        "mqtt_connected": mqtt_connected,
        "valve_open":     bool(valve_row["is_open"]) if valve_row else False,
        "valve_angle":    valve_row["servo_angle"]   if valve_row else 0,
        "ml_active":      ml_mode_active,
    })

@app.route("/api/mqtt/status")
@login_required
def api_mqtt_status():
    return jsonify({
        "connected": mqtt_connected,
        "broker":    MQTT_BROKER,
        "port":      MQTT_PORT
    })

@app.route("/api/users")
@login_required
def api_users():
    if session["user"]["role"] != "admin":
        return jsonify({"error": "Forbidden"}), 403
    conn = get_db()
    rows = conn.execute(
        "SELECT id, username, full_name, role, created_at FROM users"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/mobile")
def mobile():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("mobile.html", user=session["user"])

if __name__ == "__main__":
    init_db()
    start_mqtt()
    app.run(host="0.0.0.0", debug=True, port=5000, threaded=True, use_reloader=False)