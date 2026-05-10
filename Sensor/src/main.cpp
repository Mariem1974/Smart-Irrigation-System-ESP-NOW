
#include <esp_now.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <esp_wifi.h>
#include <esp_task_wdt.h>
#include <DHT.h>

void onDataSent(const uint8_t *mac_addr, esp_now_send_status_t status);
void onDataRecv(const uint8_t *mac, const uint8_t *data, int len);
void sendACK(const char *msg);

#define DHT_PIN        4
#define DHT_TYPE       DHT11
#define MOISTURE_PIN   34


#define MOISTURE_RAW_DRY   4095
#define MOISTURE_RAW_WET    900   

#define SEND_INTERVAL   5000UL
#define RETRY_GW_MS    30000UL
#define FAIL_THRESHOLD      8

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

const char *WIFI_SSID    = "Agrga";
const char *WIFI_PASS    = "270719819#";
const char *MQTT_BROKER  = "192.168.1.8";
const int   MQTT_PORT    = 1883;
const char *MQTT_CLIENT  = "ESP32_NodeA";

#define TOPIC_DATA   "irrigation/sensor/data"
#define TOPIC_ACK    "irrigation/sensor/ack"
#define TOPIC_SLEEP  "irrigation/sensor/sleep"

uint8_t gatewayMAC[] = {0x80, 0xF3, 0xDA, 0x63, 0x80, 0x84};

bool           useDirectWiFi      = false;
int            failCount          = 0;
bool           lastSentWasSensor  = false;
unsigned long  lastGWRetry        = 0;
volatile bool  needSwitchToWiFi   = false;
volatile bool  needSwitchToESPNOW = false;
uint8_t        savedChannel       = 1;
volatile bool  sleepFlag          = false;
volatile int   sleepSeconds       = 30;
unsigned long  lastSuccessTime    = 0;

DHT          dht(DHT_PIN, DHT_TYPE);
Packet       outPkt;
Packet       inPkt;
WiFiClient   wifiClient;
PubSubClient mqtt(wifiClient);


float readMoisture() {
  int raw = analogRead(MOISTURE_PIN);
  if (raw >= 4000) return -1;

  long sum = raw;
  for (int i = 0; i < 4; i++) {
    delay(10);
    sum += analogRead(MOISTURE_PIN);
  }
  raw = sum / 5;

  float pct = map(raw, MOISTURE_RAW_DRY, MOISTURE_RAW_WET, 0, 100);
  return constrain(pct, 0.0f, 100.0f);
}

float readDHT(float &hum) {
  float t = dht.readTemperature();
  float h = dht.readHumidity();
  if (isnan(t) || isnan(h) || t < 0 || t > 50 || h < 20 || h > 90) {
    hum = -1;
    return -1;
  }
  hum = h;
  return t;
}

void mqttCallback(char *topic, byte *payload, unsigned int length) {
  String msg = "";
  for (unsigned int i = 0; i < length; i++) msg += (char)payload[i];

  if (String(topic) == TOPIC_SLEEP) {
    int colonIdx = msg.indexOf(':');
    if (colonIdx > 0 && msg.substring(0, colonIdx) == "NODE_A") {
      sleepSeconds = msg.substring(colonIdx + 1).toInt();
      sleepFlag    = true;
    }
  }
}

void onDataSent(const uint8_t *mac_addr, esp_now_send_status_t status) {
  if (status == ESP_NOW_SEND_SUCCESS) {
    failCount       = 0;
    lastSuccessTime = millis();
  } else if (lastSentWasSensor) {
    failCount++;
    Serial.printf("[ESP-NOW] Send FAIL – failCount: %d/%d\n",
                  failCount, FAIL_THRESHOLD);
    if (failCount >= FAIL_THRESHOLD && !useDirectWiFi)
      needSwitchToWiFi = true;
  }
}

void onDataRecv(const uint8_t *mac, const uint8_t *data, int len) {
  if (len != sizeof(Packet)) return;
  memcpy(&inPkt, data, sizeof(inPkt));
  if (inPkt.packetType == PKT_SLEEP_CMD) {
    sleepSeconds = inPkt.servoAngle;
    sleepFlag    = true;
  }
}

void sendACK(const char *msg) {
  if (useDirectWiFi) {
    if (mqtt.connected()) mqtt.publish(TOPIC_ACK, msg);
    return;
  }
  Packet ack       = {};
  ack.packetType   = PKT_ACK;
  ack.temperature  = -1;
  ack.humidity     = -1;
  ack.soilMoisture = -1;
  ack.servoAngle   = 0;
  snprintf(ack.nodeID,  sizeof(ack.nodeID),  "NODE_A");
  snprintf(ack.message, sizeof(ack.message), "%s", msg);
  lastSentWasSensor = false;
  esp_now_send(gatewayMAC, (uint8_t *)&ack, sizeof(ack));
}

void connectMQTTDirect() {
  int tries = 0;
  while (!mqtt.connected() && tries++ < 5) {
    if (mqtt.connect(MQTT_CLIENT)) {
      mqtt.subscribe(TOPIC_SLEEP);
      mqtt.publish(TOPIC_ACK, "NODE_A:DIRECT_MODE");
    } else {
      delay(3000);
    }
  }
}

void switchToWiFi() {
  useDirectWiFi = true;
  failCount     = 0;
  lastGWRetry   = millis();
  esp_now_deinit();
  delay(300);
  WiFi.mode(WIFI_OFF);
  delay(500);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries++ < 40) {
    esp_task_wdt_reset();
    delay(500);
  }
  if (WiFi.status() == WL_CONNECTED) {
    mqtt.setServer(MQTT_BROKER, MQTT_PORT);
    mqtt.setBufferSize(1500);
    mqtt.setCallback(mqttCallback);
    connectMQTTDirect();
  } else {
    useDirectWiFi = false;
  }
}

void switchToESPNOW() {
  if (mqtt.connected()) mqtt.disconnect();
  WiFi.disconnect();
  delay(300);
  WiFi.mode(WIFI_STA);
  delay(200);
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_channel(savedChannel, WIFI_SECOND_CHAN_NONE);
  esp_wifi_set_promiscuous(false);
  if (esp_now_init() != ESP_OK) {
    Serial.println("[ESP-NOW] Init failed, staying in WiFi mode");
    switchToWiFi();
    return;
  }
  esp_now_register_send_cb(onDataSent);
  esp_now_register_recv_cb(onDataRecv);
  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, gatewayMAC, 6);
  peer.channel = 0;
  peer.encrypt = false;
  esp_now_add_peer(&peer);
  useDirectWiFi   = false;
  failCount       = 0;
  lastSuccessTime = millis();
  delay(300);
}

void publishDirect(float temp, float hum, float moisture) {
  if (!mqtt.connected()) connectMQTTDirect();
  if (!mqtt.connected()) return;
  mqtt.loop();
  char payload[140];
  snprintf(payload, sizeof(payload),
    "{\"node\":\"NODE_A\",\"temp\":%.1f,\"hum\":%.1f,\"moisture\":%.1f,\"mode\":\"direct\"}",
    temp, hum, moisture);
  mqtt.publish(TOPIC_DATA, payload);
}

void enterDeepSleep(int seconds) {
  char msg[30];
  snprintf(msg, sizeof(msg), "NODE_A:SLEEPING_%ds", seconds);
  sendACK(msg);
  delay(500);
  esp_sleep_enable_timer_wakeup((uint64_t)seconds * 1000000ULL);
  esp_deep_sleep_start();
}

void setup() {
  Serial.begin(115200);
  dht.begin();
  delay(1000);
  WiFi.setTxPower(WIFI_POWER_19_5dBm);

  Serial.println("NODE A");
  pinMode(MOISTURE_PIN, INPUT);


  Serial.printf("[CALIB] Raw moisture ADC: %d\n", analogRead(MOISTURE_PIN));

  if (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_TIMER)
    Serial.println("[SLEEP] Woke from deep sleep");

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries++ < 20) {
    esp_task_wdt_reset();
    delay(500);
  }
  savedChannel = (WiFi.status() == WL_CONNECTED) ? WiFi.channel() : 1;
  Serial.printf("[WIFI] Channel: %d\n", savedChannel);
  WiFi.disconnect();
  delay(200);

  esp_wifi_set_promiscuous(true);
  esp_wifi_set_channel(savedChannel, WIFI_SECOND_CHAN_NONE);
  esp_wifi_set_promiscuous(false);

  if (esp_now_init() != ESP_OK) {
    Serial.println("[ESP-NOW] Init failed – restarting");
    ESP.restart();
  }
  esp_now_register_send_cb(onDataSent);
  esp_now_register_recv_cb(onDataRecv);

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, gatewayMAC, 6);
  peer.channel = 0;
  peer.encrypt = false;
  esp_now_add_peer(&peer);

  mqtt.setServer(MQTT_BROKER, MQTT_PORT);
  mqtt.setBufferSize(1500);
  mqtt.setCallback(mqttCallback);

  lastSuccessTime = millis();
  Serial.println("[NODE A] Ready – ESP-NOW mode\n");
}

unsigned long lastSend = 0;

void loop() {
  if (needSwitchToWiFi) {
    needSwitchToWiFi = false;
    switchToWiFi();
  }
  if (needSwitchToESPNOW) {
    needSwitchToESPNOW = false;
    switchToESPNOW();
  }
  if (sleepFlag) {
    sleepFlag = false;
    enterDeepSleep(sleepSeconds);
  }

  if (useDirectWiFi) {
    mqtt.loop();
    if (millis() - lastGWRetry >= RETRY_GW_MS) {
      lastGWRetry        = millis();
      needSwitchToESPNOW = true;
    }
  }

  if (millis() - lastSend >= SEND_INTERVAL) {
    lastSend = millis();

    float hum      = -1;
    float temp     = readDHT(hum);
    float moisture = readMoisture();

    if (temp < 0 || hum < 0) {
      Serial.println("[DHT11] Bad reading – skipping send");

    } else if (moisture < 0) {
      Serial.println("[MOISTURE] Sensor not in soil – skipping send");

    } else {
      Serial.printf("[SENSORS] Temp:%.1f C | Hum:%.1f%% | Moisture:%.1f%%\n",
                    temp, hum, moisture);

      if (useDirectWiFi) {
        publishDirect(temp, hum, moisture);
      } else {
        outPkt              = {};
        outPkt.packetType   = PKT_SENSOR_DATA;
        outPkt.temperature  = temp;
        outPkt.humidity     = hum;
        outPkt.soilMoisture = moisture;
        outPkt.servoAngle   = 0;
        snprintf(outPkt.nodeID,  sizeof(outPkt.nodeID),  "NODE_A");
        snprintf(outPkt.message, sizeof(outPkt.message), "M:%.0f", moisture);
        lastSentWasSensor = true;
        esp_now_send(gatewayMAC, (uint8_t *)&outPkt, sizeof(outPkt));
      }
    }
  }
}
