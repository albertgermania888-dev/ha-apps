import os
import time
import json
import glob
import subprocess
import threading
import requests
from flask import Flask, render_template, jsonify, request, send_file
from werkzeug.utils import secure_filename
from zeroconf import Zeroconf, ServiceBrowser

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = '/tmp'

OPTIONS_PATH = "/data/options.json"
STATE_PATH = "/data/epson_state.json"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

HA_API_STATUS = "http://supervisor/core/api/states/sensor.epson_status"
HA_API_CMD = "http://supervisor/core/api/states/sensor.epson_print_result"

PRINTER_DOC = "Epson_Doc"
PRINTER_RAW = "Epson_Raw"
CACHED_IP = None

device_state = {
    "status": "Инициализация...",
    "model": "Поиск модели...",
    "ip": "Поиск..."
}

def get_options():
    if os.path.exists(OPTIONS_PATH):
        with open(OPTIONS_PATH, "r") as f:
            return json.load(f)
    return {}

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    return {"last_print": time.time()}

def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)

# --- БРОНЕБОЙНАЯ ФУНКЦИЯ УВЕДОМЛЕНИЙ (4 уровня защиты) ---
def send_notify(msg):
    opts = get_options()
    target_val = str(opts.get("notify_service", "")).strip()
    
    if not target_val:
        return False, 0, "Служба не указана во вкладке Конфигурация"
    if not SUPERVISOR_TOKEN:
        return False, 0, "Нет токена доступа к API (нужна переустановка аддона)"
        
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"}
    errors = []
    
    # Метод 1: Современный notify.send_message
    try:
        url1 = "http://supervisor/core/api/services/notify/send_message"
        res1 = requests.post(url1, headers=headers, json={"entity_id": target_val, "message": msg}, timeout=5)
        if res1.status_code in [200, 201]: return True, res1.status_code, "Успех (notify.send_message)"
        errors.append(f"V1: {res1.status_code} {res1.text.strip()}")
    except Exception as e: errors.append(f"V1 Ошибка: {e}")

    # Метод 2: Прямой вызов службы (как было в старых версиях HA)
    try:
        if "." in target_val:
            domain, service = target_val.split(".", 1)
            url2 = f"http://supervisor/core/api/services/{domain}/{service}"
            res2 = requests.post(url2, headers=headers, json={"message": msg}, timeout=5)
            if res2.status_code in [200, 201]: return True, res2.status_code, f"Успех (прямой вызов {domain}.{service})"
            errors.append(f"V2: {res2.status_code} {res2.text.strip()}")
    except Exception as e: errors.append(f"V2 Ошибка: {e}")
        
    # Метод 3: Универсальный вызов Telegram
    try:
        url3 = "http://supervisor/core/api/services/telegram_bot/send_message"
        res3 = requests.post(url3, headers=headers, json={"message": msg}, timeout=5)
        if res3.status_code in [200, 201]: return True, res3.status_code, "Успех (telegram_bot.send_message)"
        errors.append(f"V3: {res3.status_code} {res3.text.strip()}")
    except Exception as e: errors.append(f"V3 Ошибка: {e}")
        
    # Метод 4: Глобальный бродкаст уведомлений HA
    try:
        url4 = "http://supervisor/core/api/services/notify/notify"
        res4 = requests.post(url4, headers=headers, json={"message": msg}, timeout=5)
        if res4.status_code in [200, 201]: return True, res4.status_code, "Успех (notify.notify)"
        errors.append(f"V4: {res4.status_code} {res4.text.strip()}")
    except Exception as e: errors.append(f"V4 Ошибка: {e}")

    return False, 400, "Все методы отклонены HA:\n" + "\n".join(errors)

class PrinterListener:
    def __init__(self):
        self.found_ip = None
        self.found_model = "Epson (Неизвестная модель)"
    def remove_service(self, zeroconf, type, name): pass
    def add_service(self, zeroconf, type, name):
        if "epson" in name.lower() or "printer" in name.lower():
            info = zeroconf.get_service_info(type, name)
            if info and info.parsed_addresses():
                self.found_ip = info.parsed_addresses()[0]
            if info:
                for k, v in info.properties.items():
                    if v and k.decode('utf-8', 'ignore').lower() in ['ty', 'product']:
                        self.found_model = v.decode('utf-8', 'ignore')

def discover_printer():
    try:
        zc = Zeroconf()
        listener = PrinterListener()
        ServiceBrowser(zc, "_ipp._tcp.local.", listener)
        time.sleep(3)
        zc.close()
        if listener.found_ip:
            return listener.found_ip, listener.found_model
    except: pass
    return None, "Epson (Офлайн)"

def get_ip_and_model():
    global CACHED_IP
    opts = get_options()
    config_ip = str(opts.get("printer_ip", "auto")).strip()
            
    if config_ip.lower() != "auto" and config_ip != "":
        CACHED_IP = config_ip
        device_state["ip"] = CACHED_IP
        if device_state["model"] == "Поиск модели...":
            _, m = discover_printer()
            device_state["model"] = m if m != "Epson (Офлайн)" else "Epson (Ручной IP)"
        return CACHED_IP

    if CACHED_IP and subprocess.run(["ping", "-c", "1", "-W", "1", CACHED_IP], stdout=subprocess.DEVNULL).returncode == 0:
        return CACHED_IP
            
    ip, model = discover_printer()
    if ip:
        CACHED_IP, device_state["ip"], device_state["model"] = ip, ip, model
        return ip
        
    if CACHED_IP:
        device_state["ip"] = CACHED_IP
        return CACHED_IP
        
    device_state["ip"] = "Не найден"
    return "127.0.0.1"

def update_ha(url, state, icon, msg=""):
    if SUPERVISOR_TOKEN:
        headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"}
        data = {"state": state, "attributes": {"message": msg, "icon": icon}}
        try: requests.post(url, headers=headers, json=data, timeout=5)
        except: pass

def setup_cups(ip):
    if ip in ["127.0.0.1", "Не найден"]: return
    os.system("service cups start > /dev/null 2>&1")
    time.sleep(1)
    if f"ipp://{ip}" not in subprocess.run(f"lpstat -v {PRINTER_DOC}", shell=True, capture_output=True, text=True).stdout:
        os.system(f"lpadmin -p {PRINTER_DOC} -v ipp://{ip}/ipp/print -m everywhere -E")
    if f"socket://{ip}" not in subprocess.run(f"lpstat -v {PRINTER_RAW}", shell=True, capture_output=True, text=True).stdout:
        os.system(f"lpadmin -p {PRINTER_RAW} -v socket://{ip}:9100 -E")

def background_poller():
    time.sleep(5)
    while True:
        ip = get_ip_and_model()
        setup_cups(ip)
        
        if ip in ["127.0.0.1", "Не найден"]:
            update_ha(HA_API_STATUS, "Не найден", "mdi:printer-search", "Автопоиск не удался")
            device_state["status"] = "Устройство не найдено"
        else:
            stat = subprocess.run(f"lpstat -p {PRINTER_DOC}", shell=True, capture_output=True, text=True).stdout.lower()
            if "media-empty" in stat or "out of paper" in stat:
                status_text = "Нет бумаги / Замятие"
                update_ha(HA_API_STATUS, "Нет бумаги", "mdi:tray-alert")
            elif "printing" in stat:
                status_text = "Печатает..."
                update_ha(HA_API_STATUS, "Печатает", "mdi:printer-3d-nozzle")
            else:
                status_text = "Готов (Простаивает)"
                update_ha(HA_API_STATUS, "Готов", "mdi:printer-check")
            device_state["status"] = status_text

            opts = get_options()
            auto_enabled = opts.get("auto_check_enabled", False)
            auto_days = int(opts.get("auto_check_days", 7))
            
            st = load_state()
            if auto_enabled and (time.time() - st.get("last_print", 0)) > (auto_days * 86400):
                os.system(f"cancel -a {PRINTER_RAW} > /dev/null 2>&1")
                res = subprocess.run(f"escputil --nozzle-check --printer-name {PRINTER_RAW}", shell=True, capture_output=True, text=True)
                
                if res.returncode == 0:
                    send_notify("✅ Автоматическая проверка дюз на Epson успешно завершена.")
                else:
                    err = res.stderr.strip() if res.stderr else "Сбой escputil"
                    send_notify(f"❌ Ошибка авто-проверки дюз на Epson:\n{err}")
                    
                st['last_print'] = time.time()
                save_state(st)
        
        time.sleep(15)

def generate_preview(orientation):
    files = glob.glob("/tmp/print_job.*")
    if not files: return False
    filepath = files[0]
    ext = filepath.split('.')[-1].lower()
    
    if os.path.exists("/tmp/preview.jpg"):
        os.remove("/tmp/preview.jpg")

    if ext == 'pdf':
        os.system(f"gs -dFirstСудя по предоставленным данным, проблема заключается в том, что вашему аддону не хватает прав для доступа к внутреннему API Home Assistant.

*   **Блокировка доступа Supervisor-ом**: В логе `logs-19.txt` множество раз фиксируется предупреждение `[supervisor.api.proxy] Not permitted API access: db7d2e44_epson_utility`[span_4](start_span)[span_4](end_span). Это означает, что механизм безопасности Home Assistant Supervisor принудительно отклоняет запросы от вашего аддона "Epson Smart Service[span_5](start_span)"[span_5](end_span).
*   **Ошибка HTTP 400**: На скриншоте `1790523170270.jpeg` интерфейс выдает ошибку `Ошибка (HTTP 400): 400: Bad Request` в ответ на попытку выполнить действие[span_6](start_span)[span_6](end_span). Эта ошибка возвращается самим Home Assistant, поскольку веб-интерфейс пытается отправить запрос, который Supervisor блокирует из-за отсутствия разрешений[span_7](start_span)[span_7](end_span).
*   **Успешные вызовы в прошлом**: В логах Flask-сервера `logs-16.txt` видно, что ранее (в 13:08) запрос `POST /api/nozzle_check` отрабатывал успешно с кодом 200, однако более поздние `POST`-запросы (например, `POST /api/test_notify`) по времени совпадают с ошибками доступа `Not permitted API access` в логах Supervisor[span_8](start_span)[span_8](end_span)[span_9](start_span)[span_9](end_span).
*   **Процесс разработки**: Лог `logs-19.txt` показывает, что вы активно дорабатываете аддон и последовательно собирали версии с 1.0.2 до 1.0.5 из репозитория `https://github.com/albertgermania888-dev/ha-apps`[span_10](start_span)[span_10](end_span). 

**Как исправить:**
Поскольку это кастомный аддон, вам необходимо явно запросить доступ к Home Assistant API в его конфигурационном файле (`config.yaml` или `config.json`). 

Добавьте в конфигурацию аддона следующий параметр:
```yaml
hassapi: true
