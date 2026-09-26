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

# Универсальные имена очередей CUPS
PRINTER_DOC = "Epson_Doc"
PRINTER_RAW = "Epson_Raw"
CACHED_IP = None

device_state = {
    "status": "Инициализация...",
    "model": "Поиск модели...",
    "ip": "Поиск..."
}

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    return {"auto_check": False, "last_print": time.time()}

def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)

class PrinterListener:
    def __init__(self):
        self.found_ip = None
        self.found_model = "Epson (Неизвестная модель)"
    def remove_service(self, zeroconf, type, name): pass
    def add_service(self, zeroconf, type, name):
        # Универсальный поиск принтеров Epson
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
    config_ip = "auto"
    if os.path.exists(OPTIONS_PATH):
        with open(OPTIONS_PATH) as f:
            config_ip = json.load(f).get("printer_ip", "auto").strip()
            
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
            if subprocess.run(["ping", "-c", "1", "-W", "2", ip], stdout=subprocess.DEVNULL).returncode != 0:
                update_ha(HA_API_STATUS, "Выключен", "mdi:printer-off", "Принтер обесточен")
                device_state["status"] = "Выключен / Недоступен"
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

                st = load_state()
                if st.get("auto_check") and (time.time() - st.get("last_print", 0)) > 604800:
                    print("[INFO] Сработал авто-таймер (7 дней простоя). Печать проверки дюз...")
                    os.system(f"cancel -a {PRINTER_RAW} > /dev/null 2>&1")
                    subprocess.run(f"escputil --nozzle-check --printer-name {PRINTER_RAW}", shell=True)
                    st['last_print'] = time.time()
                    save_state(st)
        time.sleep(15)

@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/state", methods=["GET"])
def api_state():
    st = load_state()
    device_state["auto_check"] = st.get("auto_check", False)
    return jsonify(device_state)

@app.route("/api/config", methods=["POST"])
def api_config():
    st = load_state()
    st["auto_check"] = request.json.get("auto_check", False)
    save_state(st)
    return jsonify({"status": "SUCCESS"})

@app.route("/api/preview.jpg")
def api_preview_image():
    if os.path.exists("/tmp/preview.jpg"):
        return send_file("/tmp/preview.jpg", mimetype='image/jpeg')
    return "", 404

@app.route("/api/upload", methods=["POST"])
def api_upload():
    if 'file' not in request.files: return jsonify({"status": "ERROR"}), 400
    file = request.files['file']
    ext = file.filename.split('.')[-1].lower()
    
    for f in glob.glob("/tmp/print_job.*"): os.remove(f)
    if os.path.exists("/tmp/preview.jpg"): os.remove("/tmp/preview.jpg")

    filepath = f"/tmp/print_job.{ext}"
    file.save(filepath)

    if ext == 'pdf':
        os.system(f"gs -dFirstPage=1 -dLastPage=1 -sDEVICE=jpeg -r72 -dJPEGQ=70 -sOutputFile=/tmp/preview.jpg -q -dNOPAUSE -dBATCH '{filepath}'")
    elif ext in ['jpg', 'jpeg', 'png']:
        os.system(f"cp '{filepath}' /tmp/preview.jpg")
        
    return jsonify({"status": "SUCCESS", "preview": f"api/preview.jpg?t={time.time()}"})

def execute_print(filepath, data):
    copies = data.get("copies", "1")
    color = data.get("color", "color")
    media = data.get("media", "A4")
    quality = data.get("quality", "4")
    orientation = data.get("orientation", "3")
    pages = data.get("pages", "").strip()

    if color == "color":
        st = load_state()
        st['last_print'] = time.time()
        save_state(st)

    cmd = f"lp -d {PRINTER_DOC} -n {copies} -o media={media} -o print-color-mode={color} -o print-quality={quality} -o orientation-requested={orientation} -o fit-to-page"
    if pages: cmd += f" -o page-ranges={pages}"
    cmd += f" '{filepath}'"

    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if res.returncode == 0:
        update_ha(HA_API_CMD, "SUCCESS", "mdi:printer", "Документ отправлен в очередь")
        return {"status": "SUCCESS", "msg": "Файл передан в печать"}
    return {"status": "ERROR", "msg": f"Ошибка CUPS: {res.stderr}"}

# Печать файла из веб-интерфейса
@app.route("/api/print", methods=["POST"])
def api_print():
    files = glob.glob("/tmp/print_job.*")
    if not files: return jsonify({"status": "ERROR", "msg": "Файл не загружен"}), 400
    res = execute_print(files[0], request.json)
    return jsonify(res), (200 if res["status"] == "SUCCESS" else 500)

# НОВЫЙ МАРШРУТ: Печать локального файла из сервиса Home Assistant
@app.route("/api/print_local_file", methods=["POST"])
def api_print_local_file():
    data = request.json
    filepath = data.get("file")
    if not filepath or not os.path.exists(filepath):
        update_ha(HA_API_CMD, "ERROR", "mdi:alert", f"Файл не найден: {filepath}")
        return jsonify({"status": "ERROR", "msg": "Файл не найден"}), 404
        
    res = execute_print(filepath, data)
    return jsonify(res), (200 if res["status"] == "SUCCESS" else 500)

@app.route("/api/<action>", methods=["POST"])
def do_action(action):
    os.system(f"cancel -a {PRINTER_RAW} > /dev/null 2>&1")
    if action == "nozzle_check":
        cmd = f"escputil --nozzle-check --printer-name {PRINTER_RAW}"
    elif action == "clean_head":
        cmd = f"escputil --clean-head --printer-name {PRINTER_RAW}"
    else: return jsonify({"status": "ERROR"}), 400

    st = load_state()
    st['last_print'] = time.time()
    save_state(st)

    if subprocess.run(cmd, shell=True).returncode == 0:
        update_ha(HA_API_CMD, "SUCCESS", "mdi:printer-check", f"Команда {action} отправлена")
        return jsonify({"status": "SUCCESS", "msg": "Задание запущено"})
    return jsonify({"status": "ERROR", "msg": "Ошибка escputil"}), 500

if __name__ == "__main__":
    os.environ["PYTHONUNBUFFERED"] = "1"
    threading.Thread(target=background_poller, daemon=True).start()
    app.run(host="0.0.0.0", port=8099)
