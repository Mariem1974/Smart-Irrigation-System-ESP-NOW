
#include <esp_now.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ESP32Servo.h>
#include <esp_task_wdt.h>

uint8_t nodeA_MAC[] = {0x44, 0x1D, 0x64, 0xF3, 0x6F, 0xFC};

const char *WIFI_SSID   = "Agrga";
const char *WIFI_PASS   = "270719819#";
const char *MQTT_BROKER = "192.168.1.8";
const int   MQTT_PORT   = 1883;
const char *MQTT_CLIENT = "ESP32_GW";

#define TOPIC_DATA   "irrigation/sensor/data"
#define TOPIC_BATCH  "irrigation/sensor/batch"
#define TOPIC_ACK    "irrigation/sensor/ack"
#define TOPIC_VALVE  "irrigation/valve/control"
#define TOPIC_LED    "irrigation/led/control"
#define TOPIC_SLEEP  "irrigation/sensor/sleep"
#define TOPIC_ALERT  "irrigation/sensor/alert"
#define TOPIC_ML_VALVE  "irrigation/ml/valve"
bool mlControlActive = false;

#define SERVO_PIN     18
#define LED_RED_PIN    2
#define BUZZER_PIN    15

#define MOISTURE_DRY   40.0f   
#define MOISTURE_WET   70.0f   

#define VALVE_MAX     120   
#define VALVE_MIN      10   
#define VALVE_CLOSED    0   


#define HUM_HIGH      70.0f   
#define HUM_LOW       30.0f   
#define TEMP_HOT      33.0f   

#define PKT_SENSOR_DATA  0x01
#define PKT_VALVE_CMD    0x02
#define PKT_ACK          0x03
#define PKT_SLEEP_CMD    0x04

typedef struct {
  uint8_t packetType;
  char    nodeID[10];
  float   temperature;
  float   humidity;
  float   soilMoisture;
  int     servoAngle;
  char    message[30];
} Packet;

#define BATCH_WINDOW_MS  10000UL
#define MAX_BATCH_SIZE       20

struct SensorReading {
  char  nodeID[10];
  float temp;
  float hum;
  float moisture;
};

SensorReading batchBuf[MAX_BATCH_SIZE];
int           batchCount     = 0;
unsigned long batchWindowEnd = 0;

Servo        irrigationValve;
bool         valveOpen    = false;
int          currentAngle = 0;

WiFiClient   wifiClient;
PubSubClient mqtt(wifiClient);

Packet        receivedPkt;
volatile bool newDataFlag = false;

void setAlertActuators(bool on) {
  digitalWrite(LED_RED_PIN, on ? HIGH : LOW);
  digitalWrite(BUZZER_PIN,  on ? HIGH : LOW);
}

void handleAutoIrrigation(float moisture, float humidity, float temp) {

  // ── If ML is in control, skip rule logic entirely ──────────────────────
  if (mlControlActive) {
    Serial.println("[MODE] ML control active – skipping rule logic");
    return;
  }

  // ── Rule-based logic (unchanged) ───────────────────────────────────────
  if (moisture >= MOISTURE_WET) {
    if (valveOpen) {
      valveOpen    = false;
      currentAngle = VALVE_CLOSED;
      irrigationValve.write(VALVE_CLOSED);
      mqtt.publish(TOPIC_ACK, "GATEWAY:VALVE_CLOSED:SOIL_WET");
      Serial.println("[ACTUATOR] Valve CLOSED (SOIL_WET) → 0°");
    }

  } else if (moisture < MOISTURE_DRY) {
    float dryRatio = 1.0f - (moisture / MOISTURE_DRY);
    float angle_f  = dryRatio * VALVE_MAX;

    if (humidity > HUM_HIGH) {
      angle_f *= 0.7f;
      Serial.printf("[DHT] High humidity (%.0f%%) → reducing angle by 30%%\n", humidity);
    } else if (humidity < HUM_LOW) {
      angle_f *= 1.2f;
      Serial.printf("[DHT] Low humidity (%.0f%%) → increasing angle by 20%%\n", humidity);
    }

    if (temp > TEMP_HOT) {
      angle_f *= 1.15f;
      Serial.printf("[DHT] High temp (%.1f C) → increasing angle by 15%%\n", temp);
    }

    int angle    = constrain((int)angle_f, VALVE_MIN, VALVE_MAX);
    valveOpen    = true;
    currentAngle = angle;
    irrigationValve.write(angle);

    char ack[100];
    snprintf(ack, sizeof(ack),
             "GATEWAY:VALVE_OPEN:ANGLE_%d:MOISTURE_%.0f:HUM_%.0f:TEMP_%.1f",
             angle, moisture, humidity, temp);
    mqtt.publish(TOPIC_ACK, ack);
    Serial.printf("[ACTUATOR] Valve OPEN at %d° (moisture %.0f%%, hum %.0f%%, temp %.1f C)\n",
                  angle, moisture, humidity, temp);

  } else {
    if (valveOpen) {
      float bandRatio = 1.0f - ((moisture - MOISTURE_DRY) /
                                 (MOISTURE_WET - MOISTURE_DRY));
      int angle    = constrain((int)(bandRatio * VALVE_MIN), 0, VALVE_MIN);
      currentAngle = angle;
      irrigationValve.write(angle);
      Serial.printf("[ACTUATOR] Hysteresis band – easing to %d° (moisture %.0f%%)\n",
                    angle, moisture);
    }
  }
}

void addToBatch(const char *nodeID, float temp, float hum, float moisture) {
  if (batchCount >= MAX_BATCH_SIZE) return;
  strncpy(batchBuf[batchCount].nodeID, nodeID, 9);
  batchBuf[batchCount].nodeID[9] = '\0';
  batchBuf[batchCount].temp      = temp;
  batchBuf[batchCount].hum       = hum;
  batchBuf[batchCount].moisture  = moisture;
  batchCount++;
}

int compressNodeReadings(const char *targetNode,
                         uint8_t *outBuf, int maxLen) {
  int16_t temps[MAX_BATCH_SIZE];
  int16_t hums[MAX_BATCH_SIZE];
  int16_t mois[MAX_BATCH_SIZE];
  int count = 0;
  for (int i = 0; i < batchCount; i++) {
    if (strcmp(batchBuf[i].nodeID, targetNode) == 0) {
      temps[count] = (int16_t)(batchBuf[i].temp     * 10);
      hums[count]  = (int16_t)(batchBuf[i].hum      * 10);
      mois[count]  = (int16_t)(batchBuf[i].moisture * 10);
      count++;
    }
  }
  if (count == 0) return 0;

  int pos = 0;
  outBuf[pos++] = (uint8_t)count;
  outBuf[pos++] = (temps[0] >> 8) & 0xFF;
  outBuf[pos++] =  temps[0]       & 0xFF;
  outBuf[pos++] = (hums[0]  >> 8) & 0xFF;
  outBuf[pos++] =  hums[0]        & 0xFF;
  outBuf[pos++] = (mois[0]  >> 8) & 0xFF;
  outBuf[pos++] =  mois[0]        & 0xFF;
  for (int i = 1; i < count && pos + 3 <= maxLen; i++) {
    outBuf[pos++] = (uint8_t)(int8_t)constrain(temps[i]-temps[i-1], -127, 127);
    outBuf[pos++] = (uint8_t)(int8_t)constrain(hums[i] -hums[i-1],  -127, 127);
    outBuf[pos++] = (uint8_t)(int8_t)constrain(mois[i] -mois[i-1],  -127, 127);
  }
  return pos;
}

String bytesToHex(const uint8_t *buf, int len) {
  String hex = "";
  hex.reserve(len * 2);
  for (int i = 0; i < len; i++) {
    if (buf[i] < 0x10) hex += "0";
    hex += String(buf[i], HEX);
  }
  return hex;
}

void flushBatch(PubSubClient &mqttClient) {
  if (batchCount == 0) return;
  char nodes[5][10];
  int  nodeCount = 0;
  for (int i = 0; i < batchCount; i++) {
    bool found = false;
    for (int j = 0; j < nodeCount; j++) {
      if (strcmp(nodes[j], batchBuf[i].nodeID) == 0) { found = true; break; }
    }
    if (!found && nodeCount < 5) {
      strncpy(nodes[nodeCount], batchBuf[i].nodeID, 9);
      nodes[nodeCount][9] = '\0';
      nodeCount++;
    }
  }
  String payload = "CMPV1|";
  payload += batchCount;
  uint8_t compBuf[80];
  for (int n = 0; n < nodeCount; n++) {
    int len = compressNodeReadings(nodes[n], compBuf, sizeof(compBuf));
    if (len > 0) {
      payload += "|";
      payload += nodes[n];
      payload += ":";
      payload += bytesToHex(compBuf, len);
    }
  }
  if (mqttClient.publish(TOPIC_BATCH, payload.c_str()))
    Serial.printf("[BATCH] Published %d readings\n", batchCount);
  batchCount = 0;
}

void onDataRecv(const uint8_t *mac, const uint8_t *data, int len) {
  if (len != sizeof(Packet)) return;
  memcpy(&receivedPkt, data, sizeof(receivedPkt));
  newDataFlag = true;
}

void onDataSent(const uint8_t *mac_addr, esp_now_send_status_t status) {
  Serial.println(status == ESP_NOW_SEND_SUCCESS
                   ? "[ESP-NOW] Sent OK"
                   : "[ESP-NOW] Send FAIL");
}

void sendNodePacket(uint8_t packetType, int value, const char *message) {
  Packet cmd = {};
  cmd.packetType = packetType;
  cmd.servoAngle = value;
  snprintf(cmd.nodeID,  sizeof(cmd.nodeID),  "GATEWAY");
  snprintf(cmd.message, sizeof(cmd.message), "%s", message);
  esp_now_send(nodeA_MAC, (uint8_t *)&cmd, sizeof(cmd));
}

void mqttCallback(char *topic, byte *payload, unsigned int length) {
  String msg = "";
  for (unsigned int i = 0; i < length; i++) msg += (char)payload[i];

  if (String(topic) == TOPIC_VALVE) {
    if (msg == "OPEN") {
      valveOpen    = true;
      currentAngle = VALVE_MAX;
      irrigationValve.write(VALVE_MAX);
      mqtt.publish(TOPIC_ACK, "GATEWAY:VALVE_OPEN:MANUAL_FULL");
      Serial.printf("[ACTUATOR] Manual full open → %d°\n", VALVE_MAX);
    } else if (msg == "CLOSED") {
      valveOpen    = false;
      currentAngle = VALVE_CLOSED;
      irrigationValve.write(VALVE_CLOSED);
      mqtt.publish(TOPIC_ACK, "GATEWAY:VALVE_CLOSED:MANUAL");
      Serial.println("[ACTUATOR] Manual close → 0°");
    } else {
      int angle    = constrain(msg.toInt(), 0, VALVE_MAX);
      valveOpen    = angle > 0;
      currentAngle = angle;
      irrigationValve.write(angle);
      char ack[64];
      snprintf(ack, sizeof(ack), "GATEWAY:VALVE_MANUAL:ANGLE_%d", angle);
      mqtt.publish(TOPIC_ACK, ack);
      Serial.printf("[ACTUATOR] Manual valve angle %d°\n", angle);
    }

  } else if (String(topic) == TOPIC_LED) {
    bool on = msg.toInt() > 0 || msg == "ON";
    setAlertActuators(on);
    mqtt.publish(TOPIC_ACK,
      on ? "GATEWAY:RED_LED_BUZZER_ON"
         : "GATEWAY:RED_LED_BUZZER_OFF");

  } else if (String(topic) == TOPIC_SLEEP) {
    int colonIdx = msg.indexOf(':');
    if (colonIdx > 0) {
      String target   = msg.substring(0, colonIdx);
      int    sleepSec = msg.substring(colonIdx + 1).toInt();
      if (target == "NODE_A")
        sendNodePacket(PKT_SLEEP_CMD, sleepSec, "SLEEP_CMD");
  } } else if (String(topic) == TOPIC_ML_VALVE) {
    mlControlActive = true;
    int angle    = constrain(msg.toInt(), 0, VALVE_MAX);
    valveOpen    = angle > 0;
    currentAngle = angle;
    irrigationValve.write(angle);
    char ack[64];
    snprintf(ack, sizeof(ack), "GATEWAY:ML_VALVE:ANGLE_%d", angle);
    mqtt.publish(TOPIC_ACK, ack);
    Serial.printf("[ML] Valve set to %d°\n", angle);

} else if (String(topic) == TOPIC_VALVE && msg == "MODE_RULE") {
    mlControlActive = false;
    mqtt.publish(TOPIC_ACK, "GATEWAY:MODE_RULE");
    Serial.println("[MODE] Switched to rule-based control");

} else if (String(topic) == TOPIC_VALVE && msg == "MODE_ML") {
    // ── Dashboard sends MODE_ML → switch to ML control ────────────────────
    mlControlActive = true;
    mqtt.publish(TOPIC_ACK, "GATEWAY:MODE_ML");
    Serial.println("[MODE] Switched to ML control");
}}

void connectWiFi() {
  Serial.print("[WiFi] Connecting");
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries++ < 40) {
    esp_task_wdt_reset();
    delay(500);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED)
    Serial.printf("\n[WiFi] OK – IP: %s\n", WiFi.localIP().toString().c_str());
  else
    Serial.println("\n[WiFi] FAILED");
}

void connectMQTT() {
  int retries = 0;
  while (!mqtt.connected() && retries++ < 5) {
    if (mqtt.connect(MQTT_CLIENT)) {
      mqtt.subscribe(TOPIC_VALVE);
      mqtt.subscribe(TOPIC_LED);
      mqtt.subscribe(TOPIC_SLEEP);
      mqtt.subscribe(TOPIC_ML_VALVE);
      mqtt.publish(TOPIC_ACK, "GATEWAY:ONLINE");
      Serial.println("[MQTT] Connected");
    } else {
      Serial.printf("[MQTT] Failed rc=%d, retry %d/5\n",
                    mqtt.state(), retries);
      delay(3000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("GATEWAY");

  Serial.printf("[INFO] Gateway MAC: %s\n", WiFi.macAddress().c_str());

  pinMode(LED_RED_PIN, OUTPUT);
  pinMode(BUZZER_PIN,  OUTPUT);
  setAlertActuators(false);

  irrigationValve.attach(SERVO_PIN);
  irrigationValve.write(VALVE_CLOSED);
  currentAngle = VALVE_CLOSED;

  IPAddress local_IP(192, 168, 1, 100);
  IPAddress gateway_IP(192, 168, 1, 1);
  IPAddress subnet(255, 255, 255, 0);
  if (!WiFi.config(local_IP, gateway_IP, subnet))
    Serial.println("[WiFi] Static IP config failed – using DHCP");

  WiFi.mode(WIFI_STA);
  connectWiFi();

  mqtt.setServer(MQTT_BROKER, MQTT_PORT);
  mqtt.setBufferSize(1500);
  mqtt.setCallback(mqttCallback);
  mqtt.setKeepAlive(60);
  connectMQTT();

  if (esp_now_init() != ESP_OK) {
    Serial.println("[ESP-NOW] Init failed – restarting");
    ESP.restart();
  }
  esp_now_register_recv_cb(onDataRecv);
  esp_now_register_send_cb(onDataSent);

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, nodeA_MAC, 6);
  peer.channel = 0;
  peer.encrypt = false;
  esp_now_add_peer(&peer);

  batchWindowEnd = millis() + BATCH_WINDOW_MS;
  Serial.println("[GATEWAY] Ready\n");
}

unsigned long lastWiFiCheck = 0;
#define WIFI_CHK_INTV 15000UL

void loop() {
  if (millis() - lastWiFiCheck >= WIFI_CHK_INTV) {
    lastWiFiCheck = millis();
    if (WiFi.status() != WL_CONNECTED) connectWiFi();
  }

  if (!mqtt.connected()) connectMQTT();
  mqtt.loop();

  if (newDataFlag) {
    newDataFlag = false;

    if (receivedPkt.packetType == PKT_SENSOR_DATA) {
      float t = receivedPkt.temperature;
      float h = receivedPkt.humidity;
      float m = receivedPkt.soilMoisture;

      if (m < 0) {
        Serial.println("[GATEWAY] Invalid moisture – ignoring packet");

      } else {

        handleAutoIrrigation(m, h, t);

        char payload[200];
        snprintf(payload, sizeof(payload),
          "{\"node\":\"%s\",\"temp\":%.1f,\"hum\":%.1f,"
          "\"moisture\":%.1f,\"valve\":\"%s\",\"valve_angle\":%d}",
          receivedPkt.nodeID, t, h, m,
          valveOpen ? "OPEN" : "CLOSED",
          currentAngle);
        mqtt.publish(TOPIC_DATA, payload);
        mqtt.publish(TOPIC_ACK, "NODE_A:DATA_OK");
        Serial.printf("[MQTT] %s\n", payload);

        addToBatch(receivedPkt.nodeID, t, h, m);

        bool alertActive = false;

        if (m < 20.0f) {
          char alert[80];
          snprintf(alert, sizeof(alert),
                   "ALERT:%s:DRY_SOIL:MOISTURE_%.1f",
                   receivedPkt.nodeID, m);
          mqtt.publish(TOPIC_ALERT, alert);
          Serial.printf("[ALERT] %s\n", alert);
          alertActive = true;
        } else if (m > 90.0f) {
          char alert[80];
          snprintf(alert, sizeof(alert),
                   "ALERT:%s:FLOODING:MOISTURE_%.1f",
                   receivedPkt.nodeID, m);
          mqtt.publish(TOPIC_ALERT, alert);
          Serial.printf("[ALERT] %s\n", alert);
          alertActive = true;
        }

        if (t > 35.0f) {
          char alert[80];
          snprintf(alert, sizeof(alert),
                   "ALERT:%s:HIGH_TEMP_%.1f",
                   receivedPkt.nodeID, t);
          mqtt.publish(TOPIC_ALERT, alert);
          Serial.printf("[ALERT] %s\n", alert);
          alertActive = true;
        }

        
        setAlertActuators(alertActive);

      } 

    } else if (receivedPkt.packetType == PKT_ACK) {
      char ack[64];
      snprintf(ack, sizeof(ack), "%s:%s",
               receivedPkt.nodeID, receivedPkt.message);
      mqtt.publish(TOPIC_ACK, ack);
    }
  }

  if (millis() >= batchWindowEnd) {
    flushBatch(mqtt);
    batchWindowEnd = millis() + BATCH_WINDOW_MS;
  }
}
