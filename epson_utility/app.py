import os
import time
import json
import subprocess
import threading
import requests
from flask import Flask, render_template, jsonify, request
from werkzeug.utils import secure_filename
from zeroconf import Zeroconf, ServiceBrowser

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = '/tmp'

OPTIONS_PATH = "/data/options.json"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

HA_API_STATUS = "http://supervisor/core/api/states/sensor.epson_status"
HA_API_MAINT = "http://supervisor/core/api/states/sensor.epson_maintenance"
HA_API_CMD = "http://supervisor/core/api/states/sensor.epson_print_result"

PRINTER = "EpsonL3250"
CACHED_IP = None

device_state = {
    "status": "Инициализация...",
    "waste": "Неизвестно",
    "ip": "Поиск..."
}

class PrinterListener:
    def __init__(self):
        self.found_ip = None

    def remove_service(self, zeroconf, type, name):
        pass

    def add_service(self, zeroconf, type, name):
        # Ищем упоминания Epson в найденных сетевых сервисах
        if "epson" in name.lower() or "l3250" in name.lower():
            info = zeroconf.get_service_info(type, name)
            if info and info.parsed_addresses():
                self.found_ip = info.parsed_addresses()[0]

def discover_epson_printer():
    print("[INFO] Начинаем mDNS поиск принтера в сети...")
    try:
        zc = Zeroconf()
        listener = PrinterListener()
        # Прослушиваем стандартные протоколы печати
        ServiceBrowser(zc, "_ipp._tcp.local.", listener)
        ServiceBrowser(zc, "_printer._tcp.local.", listener)
        ServiceBrowser(zc, "_pdl-datastream._tcp.local.", listener)
        
        time.sleep(4) # Даем принтеру время на ответ
        zc.close()
        
        if listener.found_ip:
            return listener.found_ip
    except Exception as e:
        print(f"[WARNING] mDNS ошибка сканирования: {e}")
        
    return None

def get_ip():
    global CACHED_IP
    config_ip = "auto"
    if os.path.exists(OPTIONS_PATH):
        with open(OPTIONS_PATH) as f:
            opts = json.load(f)
            config_ip = opts.get("printer_ip", "auto").strip()
            
    # Ручной ввод приоритетнее автопоиска
    if config_ip.lower() != "auto" and config_ip != "":
        CACHED_IP = config_ip
        device_state["ip"] = CACHED_IP
        return CACHED_IP
        
    # Проверка закэшированного IP пингом
    if CACHED_IP:
        if subprocess.run(["ping", "-c", "1", "-W", "1", CACHED_IP], stdout=subprocess.DEVNULL).returncode == 0:
            device_state["ip"] = CACHED_IP
            return CACHED_IP
        else:
            print(f"[INFO] Принтер по адресу {CACHED_IP} не отвечает, сброс кэша...")
            CACHED_IP = None
            device_state["ip"] = "Поиск..."
            
    # Поиск устройства
    found_ip = discover_epson_printer()
    if found_ip:
        CACHED_IP = found_ip
        device_state["ip"] = CACHED_IP
        print(f"[INFO] Автоматически найден принтер: {CACHED_IP}")
        return CACHED_IP
        
    device_state["ip"] = "Не найден"
    return "127.0.0.1"

def update_ha(url, state, icon, msg=""):
    if not SUPERVISOR_TOKEN:
        return
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"}
    data = {"state": state, "attributes": {"message": msg, "icon": icon}}
    try:
        requests.post(url, headers=headers, json=data, timeout=5)
    except:
        pass

def setup_cups(ip):
    if ip == "127.0.0.1" or ip == "Не найден":
        return
        
    os.system("service cups start > /dev/null 2>&1")
    time.sleep(1)
    
    # Сверяем, куда сейчас указывает очередь. Если IP сменился — обновляем.
    check = subprocess.run(f"lpstat -v {PRINTER}", shell=True, capture_output=True, text=True)
    expected_uri = f"socket://{ip}:9100"
    
    if expected_uri not in check.stdout:
        print(f"[INFO] Обновление CUPS-очереди на адрес: {ip}")
        os.system(f"lpadmin -p {PRINTER} -v {expected_uri} -E")

def background_poller():
    time.sleep(5)
    
    while True:
        ip = get_ip()
        setup_cups(ip)
        
        if ip == "127.0.0.1" or ip == "Не найден":
            update_ha(HA_API_STATUS, "Не найден", "mdi:printer-search", "Автопоиск не удался")
            device_state["status"] = "Устройство не найдено"
        else:
            if subprocess.run(["ping", "-c", "1", "-W", "2", ip], stdout=subprocess.DEVNULL).returncode != 0:
                update_ha(HA_API_STATUS, "Выключен", "mdi:printer-off", "Принтер обесточен")
                device_state["status"] = "Выключен / Недоступен"
            else:
                stat = subprocess.run(f"lpstat -p {PRINTER}", shell=True, capture_output=True, text=True).stdout.lower()
                if "media-empty" in stat or "бумага" in stat or "out of paper" in stat:
                    status_text = "Нет бумаги / Замятие"
                    update_ha(HA_API_STATUS, "Нет бумаги", "mdi:tray-alert", "Проверьте лоток")
                elif "printing" in stat or "now printing" in stat:
                    status_text = "Печатает..."
                    update_ha(HA_API_STATUS, "Печатает", "mdi:printer-3d-nozzle", "В процессе...")
                elif "idle" in stat:
                    status_text = "Готов (Простаивает)"
                    update_ha(HA_API_STATUS, "Готов", "mdi:printer-check", "Ожидает заданий")
                else:
                    status_text = "В сети"
                    update_ha(HA_API_STATUS, "В сети", "mdi:printer", "Подключен")
                
                device_state["status"] = status_text

                maint = subprocess.run(f"escputil -s --printer-name {PRINTER}", shell=True, capture_output=True, text=True).stdout
                waste_val = device_state["waste"]
                for line in maint.split('\n'):
                    if "waste" in line.lower() or "pad" in line.lower():
                        waste_val = line.strip()
                device_state["waste"] = waste_val
                update_ha(HA_API_MAINT, waste_val, "mdi:water-opacity", "Износ впитывающей прокладки")
        
        time.sleep(15)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/state", methods=["GET"])
def api_state():
    return jsonify(device_state)

@app.route("/api/upload_print", methods=["POST"])
def api_upload_print():
    ip = get_ip()
    if ip == "127.0.0.1" or ip == "Не найден":
         return jsonify({"status": "ERROR", "msg": "Принтер не найден"}), 404
         
    if 'file' not in request.files:
        return jsonify({"status": "ERROR", "msg": "Нет файла в запросе"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "ERROR", "msg": "Файл не выбран"}), 400
    
    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    
    res = subprocess.run(f"lp -d {PRINTER} '{filepath}'", shell=True, capture_output=True, text=True)
    if res.returncode == 0:
        update_ha(HA_API_CMD, "SUCCESS", "mdi:printer", f"Файл '{filename}' отправлен")
        return jsonify({"status": "SUCCESS", "msg": f"Файл '{filename}' в очереди"})
    else:
        return jsonify({"status": "ERROR", "msg": f"Ошибка CUPS: {res.stderr}"}), 500

@app.route("/api/<action>", methods=["POST"])
def do_action(action):
    ip = get_ip()
    if ip == "127.0.0.1" or ip == "Не найден":
         return jsonify({"status": "ERROR", "msg": "Принтер не найден"}), 404

    os.system(f"cancel -a {PRINTER} > /dev/null 2>&1")
    
    if action == "nozzle_check":
        cmd = f"escputil --nozzle-check --printer-name {PRINTER}"
    elif action == "clean_head":
        cmd = f"escputil --clean-head --printer-name {PRINTER}"
    else:
        return jsonify({"status": "ERROR", "msg": "Неизвестная команда"}), 400

    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode == 0:
        update_ha(HA_API_CMD, "SUCCESS", "mdi:printer-check", f"Команда {action} отправлена")
        return jsonify({"status": "SUCCESS", "msg": f"Задание '{action}' отправлено"})
    else:
        err = result.stderr.strip() if result.stderr else "Ошибка escputil"
        update_ha(HA_API_CMD, "ERROR", "mdi:alert", err)
        return jsonify({"status": "ERROR", "msg": err}), 500

if __name__ == "__main__":
    os.environ["PYTHONUNBUFFERED"] = "1"
    threading.Thread(target=background_poller, daemon=True).start()
    app.run(host="0.0.0.0", port=8099)
