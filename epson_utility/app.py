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
        os.system(f"gs -dFirstPage=1 -dLastPage=1 -sDEVICE=jpeg -r72 -dJPEGQ=70 -sOutputFile=/tmp/preview.jpg -q -dNOPAUSE -dBATCH '{filepath}'")
    elif ext in ['jpg', 'jpeg', 'png']:
        os.system(f"cp '{filepath}' /tmp/preview.jpg")
        
    if orientation == "4" and os.path.exists("/tmp/preview.jpg"):
        os.system("mogrify -rotate -90 /tmp/preview.jpg")
        
    return True

@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/state", methods=["GET"])
def api_state():
    opts = get_options()
    st = load_state()
    
    device_state["auto_check"] = opts.get("auto_check_enabled", False)
    device_state["auto_check_days"] = int(opts.get("auto_check_days", 7))
    
    last_print = st.get("last_print", time.time())
    idle_seconds = time.time() - last_print
    device_state["idle_days"] = int(idle_seconds // 86400)
    
    return jsonify(device_state)

@app.route("/api/preview.jpg")
def api_preview_image():
    if os.path.exists("/tmp/preview.jpg"):
        return send_file("/tmp/preview.jpg", mimetype='image/jpeg')
    return "", 404

@app.route("/api/upload", methods=["POST"])
def api_upload():
    if 'file' not in request.files: return jsonify({"status": "ERROR"}), 400
    file = request.files['file']
    orientation = request.form.get("orientation", "3")
    ext = file.filename.split('.')[-1].lower()
    
    for f in glob.glob("/tmp/print_job.*"): os.remove(f)
    filepath = f"/tmp/print_job.{ext}"
    file.save(filepath)

    generate_preview(orientation)
    return jsonify({"status": "SUCCESS", "preview": f"api/preview.jpg?t={time.time()}"})

@app.route("/api/render_preview", methods=["POST"])
def api_render_preview():
    orientation = request.json.get("orientation", "3")
    if generate_preview(orientation):
        return jsonify({"status": "SUCCESS", "preview": f"api/preview.jpg?t={time.time()}"})
    return jsonify({"status": "ERROR"})

@app.route("/api/test_notify", methods=["POST"])
def api_test_notify():
    success, code, text = send_notify("Тестовое сообщение от Epson Smart Service!")
    if success:
        return jsonify({"status": "SUCCESS", "msg": f"Успех (HTTP {code}):\n{text}"})
    else:
        return jsonify({"status": "ERROR", "msg": f"Ошибка (HTTP {code}):\n{text}"})

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

@app.route("/api/print", methods=["POST"])
def api_print():
    files = glob.glob("/tmp/print_job.*")
    if not files: return jsonify({"status": "ERROR", "msg": "Файл не загружен"}), 400
    res = execute_print(files[0], request.json)
    return jsonify(res), (200 if res["status"] == "SUCCESS" else 500)

@app.route("/api/print_local_file", methods=["POST"])
def api_print_local_file():
    data = request.json
    filepath = data.get("file")
    if not filepath or not os.path.exists(filepath):
        return jsonify({"status": "ERROR", "msg": "Файл не найден"}), 404
    res = execute_print(filepath, data)
    return jsonify(res), (200 if res["status"] == "SUCCESS" else 500)

@app.route("/api/<action>", methods=["POST"])
def do_action(action):
    ip = get_ip_and_model()
    if ip == "127.0.0.1" or ip == "Не найден":
         return jsonify({"status": "ERROR", "msg": "Принтер не найден"}), 404

    os.system(f"cancel -a {PRINTER_RAW} > /dev/null 2>&1")
    if action == "nozzle_check":
        cmd = f"escputil --nozzle-check --printer-name {PRINTER_RAW}"
        action_name = "проверки дюз"
    elif action == "clean_head":
        cmd = f"escputil --clean-head --printer-name {PRINTER_RAW}"
        action_name = "прочистки головки"
    else: return jsonify({"status": "ERROR"}), 400

    st = load_state()
    st['last_print'] = time.time()
    save_state(st)

    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode == 0:
        update_ha(HA_API_CMD, "SUCCESS", "mdi:printer-check", f"Команда {action} отправлена")
        send_notify(f"✅ Ручной запуск {action_name} успешно отправлен на Epson")
        return jsonify({"status": "SUCCESS", "msg": "Задание запущено"})
    else:
        err = result.stderr.strip() if result.stderr else "Ошибка escputil"
        update_ha(HA_API_CMD, "ERROR", "mdi:alert", err)
        send_notify(f"❌ Ошибка {action_name} на Epson:\n{err}")
        return jsonify({"status": "ERROR", "msg": err}), 500

if __name__ == "__main__":
    os.environ["PYTHONUNBUFFERED"] = "1"
    threading.Thread(target=background_poller, daemon=True).start()
    app.run(host="0.0.0.0", port=8099)
