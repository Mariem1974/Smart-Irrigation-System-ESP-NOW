# 🌱 Smart Irrigation System using ESP-NOW

> An IoT-based smart irrigation system that combines ESP32, ESP-NOW, and Machine Learning to optimize water usage in agriculture.

---

# Project Overview

This project automates irrigation decisions using real-time environmental data collected from smart sensor nodes.
---

# ✨ Features

- 🌱 Real-time soil moisture monitoring
- 🌡️ Temperature & humidity sensing
- 📶 ESP-NOW wireless communication
- 🤖 AI-based irrigation prediction
- 🚰 Automatic servo valve control
- 💧 Water-saving smart farming solution

---

## 🏗️ System Architecture

```
┌─────────────────┐        ESP-NOW          ┌─────────────────────────────┐
│   Node A        │ ─────────────────────►  │   Gateway ESP32             │
│   ESP32         │                         │                             │
│  • Soil sensor  │                         │  • Rule-based valve control │
│  • DHT (T + H)  │                         │  • Servo motor (0°–120°)    │
└─────────────────┘                         │  • MQTT publisher           │
                                            └────────────┬────────────────┘
                                                         │ MQTT
                                                         ▼
                                            ┌─────────────────────────────┐
                                            │   Mosquitto Broker          │
                                            │   192.168.1.6:1883          │
                                            └────────────┬────────────────┘
                                                         │
                                                         ▼
                                            ┌─────────────────────────────┐
                                            │   app.py  (port 5000)       │
                                            │  • Stores data in SQLite    │
                                            │  • Serves dashboard (SSE)   │
                                            │  • Calls ML API             │
                                            │  • Publishes ML valve angle │
                                            └────────────┬────────────────┘
                                                         │ HTTP localhost
                                                         ▼
                                            ┌─────────────────────────────┐
                                            │   ml_api.py  (port 5001)    │
                                            │  • Random Forest model      │
                                            │  • Returns ml_angle +       │
                                            │    rule_angle for display   │
                                            └─────────────────────────────┘
```


##  ML Model

**Algorithm:** Random Forest Regressor  
**Target:** Servo valve angle (0° – 120°)  
**Features:**
- 🌱 Soil Moisture
- 🌡️ Temperature
- 💧 Humidity
- 📉 Moisture Deficit

## 📊 Model Performance

| Metric | Value |
|---|---|
| MAE | ~0.49° |
| R² Score | ~0.9987 |
| CV R² | ~0.9987 |

✅ High prediction accuracy  
✅ Stable model performance  
✅ Strong prediction correlation

---

## 🚀 Installation & Setup

### 1. Prerequisites

- Python 3.10+
- Mosquitto MQTT broker running on `192.168.1.6:1883`
- PlatformIO (for flashing ESP32 nodes)

### 2. Install Python dependencies

```bash
cd dashbord
pip install -r requirements.txt
```

### 3. Train the model (first time)

Open and run all cells in:
```
ML Model/train_model.ipynb
```
This generates:
```
ML Model/Model/rf_valve_model.joblib
ML Model/Model/model_meta.json
```

### 4. Flash the ESP32 nodes

```bash
# Flash Gateway
cd Bridge
pio run --target upload

# Flash Node A
cd Sensor
pio run --target upload
```

### 5. Start the application

Open **two terminals** inside `dashbord/`:

```bash
# Terminal 1 — ML microservice
python ml_api.py
# → Listening on http://0.0.0.0:5001

# Terminal 2 — Dashboard
python app.py
# → Listening on http://0.0.0.0:5000
```

### 6. Open the dashboard

```
http://localhost:5000
```

Login with:
```
Username: admin
Password: admin123
```

---

# 🎯 Project Goals

- 💧 Reduce water consumption
- 🌱 Improve irrigation efficiency
- 🤖 Enable smart farming automation
- 📡 Integrate IoT with AI
- 📈 Build a scalable agriculture solution

---

#  Advanced AI Feature

## Continuous Learning from Real Data

The system can retrain the ML model using real sensor readings collected after deployment.

###  Retraining Process
Sensors → SQLite Database → Retraining Script → Updated ML Model

###  Benefits
- Improves prediction accuracy
- Learns from real environmental conditions
- Adapts over time automatically

---

#  Future Improvements
- ☀️ Solar-powered system
- 🤖 Advanced AI models
- 🌍 Remote monitoring support

---


# 📄 License

This project was developed for educational and research purposes.
