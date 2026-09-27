import os
import time
import json
import glob
import subprocess
import threading
import requests
import urllib3
from flask import Flask, render_template, jsonify, request, send_file
from werkzeug.utils import secure_filename
from zeroconf import Zeroconf, ServiceBrowser

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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

def send_notify(msg):
    opts = get_options()
    target_val = str(opts.get("notify_service", "")).strip()
    
    if not target_val: return False, 0, "Служба не указана"
    if not SUPERVISOR_TOKEN: return False, 0, "Нет токена"
        
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"}
    
    try:
        url1 = "http://supervisor/core/api/services/notify/send_message"
        res1 = requests.post(url1, headers=headers, json={"entity_id": target_val, "message": msg}, timeout=5)
        if res1.status_code in [200, 201]: return True, res1.status_code, "Успех"
    except: pass

    try:
        if "." in target_val:
            domain, service = target_val.split(".", 1)
            url2 = f"http://supervisor/core/api/services/{domain}/{service}"
            res2 = requests.post(url2, headers=headers, json={"message": msg}, timeout=5)
            if res2.status_code in [200, 201]: return True, res2.status_code, "Успех"
    except: pass
        
    try:
        url3 = "http://supervisor/core/api/services/telegram_bot/send_message"
        res3 = requests.post(url3, headers=headers, json={"message": msg}, timeout=5)
        if res3.status_code in [200, 201]: return True, res3.status_code, "Успех"
    except: pass
        
    return False, 400, "Ошибка отправки"

class PrinterListener:
    def __init__(self):
        self.found_ip = None
        self.found_model = "Epson (Неизвестная модель)"
    def remove_service(self, zeroconf, type, name): pass
    def update_service(self, zeroconf, type, name): pass
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

def check_printer_status(ip):
    if ip in ["127.0.0.1", "Не найден"]: return "Не найден", "Офлайн"
    
    urls_to_try = [
        f"https://{ip}/PRESENTATION/ADVANCED/COMMON/TOP",
        f"http://{ip}/PRESENTATION/ADVANCED/COMMON/TOP",
        f"https://{ip}/PRESENTATION/HTML/TOP/PRTINFO.HTML",
        f"http://{ip}/PRESENTATION/HTML/TOP/PRTINFO.HTML"
    ]
    
    html = ""
    for url in urls_to_try:
        try:
            res = requests.get(url, timeout=3, verify=False)
            if res.status_code == 200:
                html += res.text.lower() + " "
                if "printer status" in html or "статус" in html:
                    break
        except: pass
            
    if not html:
        return "Офлайн", "Принтер недоступен (Офлайн)"
        
    if any(err in html for err in ["error has occurred", "fatal error", "paper out", "jam", "ошибка", "замятие"]):
        return "Ошибка оборудования", "Ошибка (Проверьте принтер)"
        
    if any(busy in html for busy in ["busy.", "printing", "занят", "печать", "печатает"]):
        return "Занят", "Печатает (Занят)..."
        
    if any(ready in html for ready in ["available.", "ready", "idle", "доступен", "готов", "простаивает"]):
        return "Готов", "Готов (Простаивает)"
        
    return "Неизвестно", "Связь установлена (Статус скрыт)"

def monitor_job(ip, action_name):
    became_busy = False
    for _ in range(10):
        time.sleep(2)
        raw_state, _ = check_printer_status(ip)
        if raw_state == "Занят":
            became_busy = True
            break
        if raw_state == "Ошибка оборудования":
            update_ha(HA_API_CMD, "ERROR", "mdi:alert", "Сбой при старте")
            send_notify(f"❌ Сбой запуска '{action_name}'. Принтер выдал ошибку (нет бумаги или замятие).")
            return

    for _ in range(60):
        time.sleep(2)
        raw_state, _ = check_printer_status(ip)
        
        if raw_state == "Ошибка оборудования":
            update_ha(HA_API_CMD, "ERROR", "mdi:alert", "Сбой в процессе")
            send_notify(f"❌ Сбой во время '{action_name}'. Кончилась бумага или произошло замятие.")
            return
            
        if raw_state == "Готов":
            update_ha(HA_API_CMD, "SUCCESS", "mdi:printer-check", "Успешно")
            send_notify(f"✅ Успешно завершено: {action_name}.")
            return
            
    send_notify(f"⚠️ '{action_name}' отправлено, но результат неизвестен (таймаут мониторинга).")

def run_action_and_monitor(ip, cmd, action_name):
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if res.returncode != 0:
        err = res.stderr.strip() if res.stderr else "Сбой CUPS"
        send_notify(f"❌ Системная ошибка отправки '{action_name}':\n{err}")
        return
    monitor_job(ip, action_name)

def background_poller():
    time.sleep(5)
    while True:
        ip = get_ip_and_model()
        setup_cups(ip)
        
        raw_state, display_state = check_printer_status(ip)
        device_state["status"] = display_state
        
        if raw_state == "Ошибка оборудования": update_ha(HA_API_STATUS, "Ошибка", "mdi:alert")
        elif raw_state == "Занят": update_ha(HA_API_STATUS, "Печатает", "mdi:printer-3d-nozzle")
        elif raw_state == "Готов": update_ha(HA_API_STATUS, "Готов", "mdi:printer-check")

        opts = get_options()
        auto_enabled = opts.get("auto_check_enabled", False)
        auto_days = int(opts.get("auto_check_days", 7))
        st = load_state()
        
        if auto_enabled and (time.time() - st.get("last_print", 0)) > (auto_days * 86400):
            if raw_state == "Ошибка оборудования":
                send_notify(f"⚠️ Авто-проверка дюз отложена: принтер Epson в состоянии ошибки. Проверьте лоток.")
                st['last_print'] += 86400 
                save_state(st)
            elif raw_state == "Готов":
                os.system(f"cancel -a {PRINTER_RAW} > /dev/null 2>&1")
                cmd = f"escputil --nozzle-check --printer-name {PRINTER_RAW}"
                threading.Thread(target=run_action_and_monitor, args=(ip, cmd, "Авто-проверка дюз")).start()
                st['last_print'] = time.time()
                save_state(st)
        
        time.sleep(15)

def generate_preview(orientation):
    files = glob.glob("/tmp/print_job.*")
    if not files: return False
    filepath = files[0]
    ext = filepath.split('.')[-1].lower()
    if os.path.exists("/tmp/preview.jpg"): os.remove("/tmp/preview.jpg")

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
    ip = get_ip_and_model()
    raw_state, display_state = check_printer_status(ip)
    device_state["status"] = display_state
    
    opts = get_options()
    st = load_state()
    device_state["auto_check"] = opts.get("auto_check_enabled", False)
    device_state["auto_check_days"] = int(opts.get("auto_check_days", 7))
    device_state["idle_days"] = int((time.time() - st.get("last_print", time.time())) // 86400)
    return jsonify(device_state)

@app.route("/api/preview.jpg")
def api_preview_image():
    if os.path.exists("/tmp/preview.jpg"): return send_file("/tmp/preview.jpg", mimetype='image/jpeg')
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
    if generate_preview(orientation): return jsonify({"status": "SUCCESS", "preview": f"api/preview.jpg?t={time.time()}"})
    return jsonify({"status": "ERROR"})

@app.route("/api/test_notify", methods=["POST"])
def api_test_notify():
    success, code, text = send_notify("Тестовое сообщение от Epson Smart Service!")
    if success: return jsonify({"status": "SUCCESS", "msg": f"Успех (HTTP {code}):\n{text}"})
    else: return jsonify({"status": "ERROR", "msg": f"Ошибка (HTTP {code}):\n{text}"})

def execute_print(filepath, data):
    ip = get_ip_and_model()
    
    raw_state, _ = check_printer_status(ip)
    if raw_state == "Ошибка оборудования":
        return {"status": "ERROR", "msg": "Заблокировано: Ошибка принтера (проверьте лоток)"}
    if raw_state == "Не найден":
        return {"status": "ERROR", "msg": "Принтер не найден в сети"}

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

    threading.Thread(target=run_action_and_monitor, args=(ip, cmd, "Печать документа")).start()
    return {"status": "SUCCESS", "msg": "Отправлено на принтер. Ожидание завершения..."}

@app.route("/api/print", methods=["POST"])
def api_print():
    files = glob.glob("/tmp/print_job.*")
    if not files: return jsonify({"status": "ERROR", "msg": "Файл не загружен"}), 400
    res = execute_print(files[0], request.json)
    return jsonify(res), (200 if res["status"] == "SUCCESS" else 400)

@app.route("/api/print_local_file", methods=["POST"])
def api_print_local_file():
    data = request.json
    filepath = data.get("file")
    if not filepath or not os.path.exists(filepath):
        return jsonify({"status": "ERROR", "msg": "Файл не найден"}), 404
    res = execute_print(filepath, data)
    return jsonify(res), (200 if res["status"] == "SUCCESS" else 400)

@app.route("/api/<action>", methods=["POST"])
def do_action(action):
    ip = get_ip_and_model()
    
    raw_state, _ = check_printer_status(ip)
    if raw_state == "Ошибка оборудования":
        return jsonify({"status": "ERROR", "msg": "Заблокировано: Ошибка принтера (проверьте лоток)"}), 400
    if raw_state == "Не найден":
        return jsonify({"status": "ERROR", "msg": "Принтер не найден в сети"}), 404

    os.system(f"cancel -a {PRINTER_RAW} > /dev/null 2>&1")
    if action == "nozzle_check":
        cmd = f"escputil --nozzle-check --printer-name {PRINTER_RAW}"
        action_name = "Проверка дюз"
    elif action == "clean_head":
        cmd = f"escputil --clean-head --printer-name {PRINTER_RAW}"
        action_name = "Прочистка головки"
    else: return jsonify({"status": "ERROR"}), 400

    st = load_state()
    st['last_print'] = time.time()
    save_state(st)

    threading.Thread(target=run_action_and_monitor, args=(ip, cmd, action_name)).start()
    return jsonify({"status": "SUCCESS", "msg": "Отправлено на принтер. Ожидание завершения..."})

if __name__ == "__main__":
    os.environ["PYTHONUNBUFFERED"] = "1"
    threading.Thread(target=background_poller, daemon=True).start()
    app.run(host="0.0.0.0", port=8099)
