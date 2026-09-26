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
HA_API_CMD = "http://supervisor/core/api/states/sensor.epson_print_result"

PRINTER_DOC = "EpsonL3250"
PRINTER_RAW = "EpsonRAW"
CACHED_IP = None

device_state = {
    "status": "Инициализация...",
    "model": "Поиск модели...",
    "ip": "Поиск..."
}

class PrinterListener:
    def __init__(self):
        self.found_ip = None
        self.found_model = "Epson (Неизвестная модель)"

    def remove_service(self, zeroconf, type, name):
        pass

    def add_service(self, zeroconf, type, name):
        if "epson" in name.lower() or "l32" in name.lower() or "printer" in name.lower():
            info = zeroconf.get_service_info(type, name)
            if info:
                addresses = info.parsed_addresses()
                if addresses:
                    self.found_ip = addresses[0]
                for k, v in info.properties.items():
                    if v is not None:
                        key = k.decode('utf-8', 'ignore').lower()
                        val = v.decode('utf-8', 'ignore')
                        if key in ['ty', 'product', 'usb_mfg']:
                            self.found_model = val

def discover_printer():
    try:
        zc = Zeroconf()
        listener = PrinterListener()
        ServiceBrowser(zc, "_ipp._tcp.local.", listener)
        time.sleep(3)
        zc.close()
        if listener.found_ip:
            return listener.found_ip, listener.found_model
    except:
        pass
    return None, "Epson (Офлайн)"

def get_ip_and_model():
    global CACHED_IP
    config_ip = "auto"
    if os.path.exists(OPTIONS_PATH):
        with open(OPTIONS_PATH) as f:
            opts = json.load(f)
            config_ip = opts.get("printer_ip", "auto").strip()
            
    if config_ip.lower() != "auto" and config_ip != "":
        CACHED_IP = config_ip
        device_state["ip"] = CACHED_IP
        if device_state["model"] == "Поиск модели...":
            _, m = discover_printer()
            device_state["model"] = m if m != "Epson (Офлайн)" else "Epson (Ручной IP)"
        return CACHED_IP

    if CACHED_IP:
        if subprocess.run(["ping", "-c", "1", "-W", "1", CACHED_IP], stdout=subprocess.DEVNULL).returncode == 0:
            return CACHED_IP
        else:
            CACHED_IP = None
            
    ip, model = discover_printer()
    if ip:
        CACHED_IP = ip
        device_state["ip"] = ip
        device_state["model"] = model
        return ip
        
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
    
    # 1. Очередь IPP Everywhere для печати документов с настройками (цвет, формат)
    check_doc = subprocess.run(f"lpstat -v {PRINTER_DOC}", shell=True, capture_output=True, text=True)
    if f"ipp://{ip}" not in check_doc.stdout:
        os.system(f"lpadmin -p {PRINTER_DOC} -v ipp://{ip}/ipp/print -m everywhere -E")
        
    # 2. RAW-очередь для сервисных кодов escputil (дюзы, прочистка)
    check_raw = subprocess.run(f"lpstat -v {PRINTER_RAW}", shell=True, capture_output=True, text=True)
    if f"socket://{ip}" not in check_raw.stdout:
        os.system(f"lpadmin -p {PRINTER_RAW} -v socket://{ip}:9100 -E")

def background_poller():
    time.sleep(5)
    while True:
        ip = get_ip_and_model()
        setup_cups(ip)
        
        if ip == "127.0.0.1" or ip == "Не найден":
            update_ha(HA_API_STATUS, "Не найден", "mdi:printer-search", "Автопоиск не удался")
            device_state["status"] = "Устройство не найдено"
        else:
            if subprocess.run(["ping", "-c", "1", "-W", "2", ip], stdout=subprocess.DEVNULL).returncode != 0:
                update_ha(HA_API_STATUS, "Выключен", "mdi:printer-off", "Принтер обесточен")
                device_state["status"] = "Выключен / Недоступен"
            else:
                stat = subprocess.run(f"lpstat -p {PRINTER_DOC}", shell=True, capture_output=True, text=True).stdout.lower()
                if "media-empty" in stat or "бумага" in stat or "out of paper" in stat:
                    status_text = "Нет бумаги / Замятие"
                    update_ha(HA_API_STATUS, "Нет бумаги", "mdi:tray-alert", "Проверьте лоток")
                elif "printing" in stat or "now printing" in stat:
                    status_text = "Печатает..."
                    update_ha(HA_API_STATUS, "Печатает", "mdi:printer-3d-nozzle", "В процессе...")
                else:
                    status_text = "Готов (Простаивает)"
                    update_ha(HA_API_STATUS, "Готов", "mdi:printer-check", "Ожидает заданий")
                
                device_state["status"] = status_text
        
        time.sleep(15)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/state", methods=["GET"])
def api_state():
    return jsonify(device_state)

@app.route("/api/upload_print", methods=["POST"])
def api_upload_print():
    ip = get_ip_and_model()
    if ip == "127.0.0.1" or ip == "Не найден":
         return jsonify({"status": "ERROR", "msg": "Принтер не найден"}), 404
         
    if 'file' not in request.files:
        return jsonify({"status": "ERROR", "msg": "Файл не выбран"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "ERROR", "msg": "Пустой файл"}), 400
    
    # Считываем настройки печати из веб-формы
    copies = request.form.get("copies", "1")
    color = request.form.get("color", "color")
    media = request.form.get("media", "A4")
    
    try:
        copies = int(copies)
    except:
        copies = 1

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    
    # Формируем системную команду с подстановкой параметров
    cmd = f"lp -d {PRINTER_DOC} -n {copies} -o media={media} -o print-color-mode={color} -o fit-to-page '{filepath}'"
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    
    if res.returncode == 0:
        update_ha(HA_API_CMD, "SUCCESS", "mdi:printer", f"Файл '{filename}' отправлен")
        return jsonify({"status": "SUCCESS", "msg": f"Файл '{filename}' передан в печать"})
    else:
        return jsonify({"status": "ERROR", "msg": f"Ошибка CUPS: {res.stderr}"}), 500

@app.route("/api/<action>", methods=["POST"])
def do_action(action):
    ip = get_ip_and_model()
    if ip == "127.0.0.1" or ip == "Не найден":
         return jsonify({"status": "ERROR", "msg": "Принтер не найден"}), 404

    os.system(f"cancel -a {PRINTER_RAW} > /dev/null 2>&1")
    
    if action == "nozzle_check":
        cmd = f"escputil --nozzle-check --printer-name {PRINTER_RAW}"
    elif action == "clean_head":
        cmd = f"escputil --clean-head --printer-name {PRINTER_RAW}"
    else:
        return jsonify({"status": "ERROR", "msg": "Неизвестная команда"}), 400

    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode == 0:
        update_ha(HA_API_CMD, "SUCCESS", "mdi:printer-check", f"Сервисная команда отправлена")
        return jsonify({"status": "SUCCESS", "msg": f"Задание запущено"})
    else:
        err = result.stderr.strip() if result.stderr else "Ошибка escputil"
        update_ha(HA_API_CMD, "ERROR", "mdi:alert", err)
        return jsonify({"status": "ERROR", "msg": err}), 500

if __name__ == "__main__":
    os.environ["PYTHONUNBUFFERED"] = "1"
    threading.Thread(target=background_poller, daemon=True).start()
    app.run(host="0.0.0.0", port=8099)
