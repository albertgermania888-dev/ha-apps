import os
import sys
import time
import json
import glob
import signal
import subprocess
import threading
from datetime import datetime
import requests
import urllib3
from flask import Flask, render_template, jsonify, request, send_file
from zeroconf import Zeroconf, ServiceBrowser
from waitress import serve

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Корректное завершение контейнера по сигналу Docker (убирает ошибку exit code 137 в логах)
def handle_sigterm(*args):
    os._exit(0)

signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = '/tmp'

OPTIONS_PATH = "/data/options.json"
STATE_PATH = "/data/epson_state.json"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

HA_API_STATUS = "http://supervisor/core/api/states/sensor.epson_status"
HA_API_CMD = "http://supervisor/core/api/states/sensor.epson_print_result"
HA_API_IDLE = "http://supervisor/core/api/states/sensor.epson_idle_days"

PRINTER_DOC = "Epson_Doc"
PRINTER_RAW = "Epson_Raw"
CACHED_IP = None

STATE_LOCK = threading.Lock()
JOB_LOCK = threading.Lock()
STATUS_LOCK = threading.Lock()

IS_MONITORING_JOB = False
LAST_STATUS_CHECK = 0
CACHED_RAW_STATE = "Неизвестно"

device_state = {
    "status": "Инициализация...",
    "model": "Поиск модели...",
    "ip": "Поиск..."
}

def get_options():
    if os.path.exists(OPTIONS_PATH):
        try:
            with open(OPTIONS_PATH, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def load_state():
    with STATE_LOCK:
        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH, "r") as f:
                    data = json.load(f)
                if isinstance(data, dict) and "last_print" in data:
                    return data
            except Exception as e:
                print(f"[WARN] Ошибка чтения {STATE_PATH}, создаем заново: {e}")

        # Если файла нет — создаем и СРАЗУ записываем на диск, чтобы таймер начал отсчет
        initial_state = {
            "last_print": time.time(),
            "last_error_notify": 0
        }
        try:
            with open(STATE_PATH, "w") as f:
                json.dump(initial_state, f)
        except Exception as e:
            print(f"[ERROR] Не удалось записать {STATE_PATH}: {e}")
        return initial_state

def save_state(state):
    with STATE_LOCK:
        try:
            with open(STATE_PATH, "w") as f:
                json.dump(state, f)
        except Exception as e:
            print(f"[ERROR] Не удалось сохранить {STATE_PATH}: {e}")

def mark_print_success():
    st = load_state()
    st["last_print"] = time.time()
    st["last_error_notify"] = 0
    save_state(st)

def send_notify(msg):
    opts = get_options()
    target_val = str(opts.get("notify_service", "")).strip()

    if not target_val:
        return False, 0, "Служба notify_service не указана в Конфигурации"
    if not SUPERVISOR_TOKEN:
        return False, 0, "Нет SUPERVISOR_TOKEN"

    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"}
    errors = []

    # Вариант 1: Служба notify.send_message (для новых интеграций-сущностей)
    try:
        url1 = "http://supervisor/core/api/services/notify/send_message"
        res1 = requests.post(url1, headers=headers, json={"entity_id": target_val, "message": msg}, timeout=5)
        if res1.status_code in [200, 201]:
            return True, res1.status_code, "Успех (notify.send_message)"
        errors.append(f"V1 ({res1.status_code}): {res1.text.strip()}")
    except Exception as e:
        errors.append(f"V1: {e}")

    # Вариант 2: Прямой вызов классической службы (например, notify.telegram_bot_...)
    try:
        domain, service = target_val.split(".", 1) if "." in target_val else ("notify", target_val)
        url2 = f"http://supervisor/core/api/services/{domain}/{service}"
        res2 = requests.post(url2, headers=headers, json={"message": msg}, timeout=5)
        if res2.status_code in [200, 201]:
            return True, res2.status_code, f"Успех ({domain}.{service})"
        errors.append(f"V2 ({res2.status_code}): {res2.text.strip()}")
    except Exception as e:
        errors.append(f"V2: {e}")

    # Вариант 3: Глобальный вызов telegram_bot.send_message
    try:
        url3 = "http://supervisor/core/api/services/telegram_bot/send_message"
        res3 = requests.post(url3, headers=headers, json={"message": msg}, timeout=5)
        if res3.status_code in [200, 201]:
            return True, res3.status_code, "Успех (telegram_bot.send_message)"
        errors.append(f"V3 ({res3.status_code}): {res3.text.strip()}")
    except Exception as e:
        errors.append(f"V3: {e}")

    return False, 400, "Все методы отклонены:\n" + "\n".join(errors)

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
    except Exception:
        pass
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

def update_ha(url, state, icon, extra_attrs=None):
    if SUPERVISOR_TOKEN:
        headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"}
        attrs = {"icon": icon}
        if extra_attrs:
            attrs.update(extra_attrs)
        data = {"state": state, "attributes": attrs}
        try:
            requests.post(url, headers=headers, json=data, timeout=5)
        except Exception:
            pass

def setup_cups(ip):
    if ip in ["127.0.0.1", "Не найден"]:
        return
    os.system("service cups start > /dev/null 2>&1")
    out_doc = subprocess.run(f"lpstat -v {PRINTER_DOC}", shell=True, capture_output=True, text=True).stdout
    if f"ipp://{ip}" not in out_doc:
        os.system(f"lpadmin -p {PRINTER_DOC} -v ipp://{ip}/ipp/print -m everywhere -E > /dev/null 2>&1")
    out_raw = subprocess.run(f"lpstat -v {PRINTER_RAW}", shell=True, capture_output=True, text=True).stdout
    if f"socket://{ip}" not in out_raw:
        os.system(f"lpadmin -p {PRINTER_RAW} -v socket://{ip}:9100 -E > /dev/null 2>&1")

def check_printer_status(ip, force=False):
    global LAST_STATUS_CHECK, CACHED_RAW_STATE
    if ip in ["127.0.0.1", "Не найден"]:
        device_state["status"] = "Устройство не найдено"
        CACHED_RAW_STATE = "Не найден"
        return "Не найден", "Устройство не найдено"

    with STATUS_LOCK:
        # Защита от перегрузки веб-сервера принтера частыми запросами (кэш на 2.5 секунды)
        if not force and (time.time() - LAST_STATUS_CHECK) < 2.5:
            return CACHED_RAW_STATE, device_state["status"]

        urls_to_try = [
            f"https://{ip}/PRESENTATION/ADVANCED/COMMON/TOP",
            f"http://{ip}/PRESENTATION/ADVANCED/COMMON/TOP",
            f"https://{ip}/PRESENTATION/HTML/TOP/PRTINFO.HTML",
            f"http://{ip}/PRESENTATION/HTML/TOP/PRTINFO.HTML"
        ]

        html = ""
        for url in urls_to_try:
            try:
                res = requests.get(url, timeout=2.5, verify=False)
                if res.status_code == 200:
                    html = res.text.lower()
                    if "printer status" in html or "статус" in html:
                        break
            except Exception:
                pass

        LAST_STATUS_CHECK = time.time()

        if not html:
            CACHED_RAW_STATE = "Офлайн"
            device_state["status"] = "Принтер недоступен (Офлайн)"
        elif any(err in html for err in ["error has occurred", "fatal error", "paper out", "jam", "произошла ошибка", "замятие"]):
            CACHED_RAW_STATE = "Ошибка оборудования"
            device_state["status"] = "Ошибка (Проверьте принтер)"
        elif any(busy in html for busy in ["busy.", "printing", "занят.", "печатает"]):
            CACHED_RAW_STATE = "Занят"
            device_state["status"] = "Печатает (Занят)..."
        elif any(ready in html for ready in ["available.", "ready", "idle", "доступен.", "готов"]):
            CACHED_RAW_STATE = "Готов"
            device_state["status"] = "Готов (Простаивает)"
        else:
            CACHED_RAW_STATE = "Неизвестно"
            device_state["status"] = "Связь установлена"

        return CACHED_RAW_STATE, device_state["status"]

def monitor_job(ip, action_name, reset_timer=True):
    global IS_MONITORING_JOB
    try:
        # Этап 1: Ждем до 20 секунд, пока принтер примет задание и перейдет в Busy (или выдаст ошибку)
        for _ in range(10):
            time.sleep(2)
            raw_state, _ = check_printer_status(ip, force=True)
            if raw_state == "Занят":
                break
            if raw_state == "Ошибка оборудования":
                update_ha(HA_API_CMD, "ERROR", "mdi:alert", {"message": f"Сбой при старте: {action_name}"})
                send_notify(f"❌ Сбой запуска «{action_name}». Принтер выдал ошибку (нет бумаги или замятие).")
                return

        # Этап 2: Ждем завершения физической печати (до 180 секунд)
        for _ in range(90):
            time.sleep(2)
            raw_state, _ = check_printer_status(ip, force=True)

            if raw_state == "Ошибка оборудования":
                update_ha(HA_API_CMD, "ERROR", "mdi:alert", {"message": f"Ошибка во время: {action_name}"})
                send_notify(f"❌ Сбой во время «{action_name}». Кончилась бумага или произошло замятие.")
                return

            if raw_state == "Готов":
                if reset_timer:
                    mark_print_success()
                update_ha(HA_API_CMD, "SUCCESS", "mdi:printer-check", {"message": f"Успешно: {action_name}"})
                send_notify(f"✅ Успешно завершено: {action_name}.")
                return

        send_notify(f"⚠️ «{action_name}» отправлено, но результат неизвестен (таймаут ожидания ответа от принтера).")
    finally:
        with JOB_LOCK:
            IS_MONITORING_JOB = False

def run_action_and_monitor(ip, cmd, action_name, reset_timer=True):
    global IS_MONITORING_JOB
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if res.returncode != 0:
        with JOB_LOCK:
            IS_MONITORING_JOB = False
        err = res.stderr.strip() if res.stderr else "Сбой отправки в очередь CUPS"
        send_notify(f"❌ Системная ошибка отправки «{action_name}»:\n{err}")
        return
    monitor_job(ip, action_name, reset_timer)

def background_poller():
    global IS_MONITORING_JOB
    # Гарантируем создание файла состояния при старте
    load_state()
    time.sleep(3)

    while True:
        try:
            ip = get_ip_and_model()
            setup_cups(ip)

            raw_state, _ = check_printer_status(ip, force=False)

            if raw_state == "Ошибка оборудования":
                update_ha(HA_API_STATUS, "Ошибка", "mdi:alert")
            elif raw_state == "Занят":
                update_ha(HA_API_STATUS, "Печатает", "mdi:printer-3d-nozzle")
            elif raw_state == "Готов":
                update_ha(HA_API_STATUS, "Готов", "mdi:printer-check")
            else:
                update_ha(HA_API_STATUS, raw_state, "mdi:printer-wireless")

            opts = get_options()
            auto_enabled = bool(opts.get("auto_check_enabled", False))
            auto_days = int(opts.get("auto_check_days", 7))
            st = load_state()

            now = time.time()
            last_print = float(st.get("last_print", now))
            idle_seconds = max(0, now - last_print)
            idle_days = int(idle_seconds // 86400)

            update_ha(HA_API_IDLE, idle_days, "mdi:calendar-clock", {
                "unit_of_measurement": "дн.",
                "friendly_name": "Epson дней без печати",
                "last_print": datetime.fromtimestamp(last_print).strftime("%d.%m.%Y %H:%M")
            })

            if auto_enabled and idle_seconds >= (auto_days * 86400):
                with JOB_LOCK:
                    job_active = IS_MONITORING_JOB

                if not job_active:
                    if raw_state == "Ошибка оборудования":
                        last_err_notif = float(st.get("last_error_notify", 0))
                        # Напоминаем об ошибке не чаще 1 раза в сутки, не сбрасывая счетчик простоя
                        if (now - last_err_notif) >= 86400:
                            send_notify("⚠️ Авто-проверка дюз отложена: принтер Epson находится в состоянии ошибки. Проверьте лоток для бумаги.")
                            st["last_error_notify"] = now
                            save_state(st)
                    elif raw_state == "Готов":
                        with JOB_LOCK:
                            IS_MONITORING_JOB = True
                        os.system(f"cancel -a {PRINTER_RAW} > /dev/null 2>&1")
                        cmd = f"escputil --nozzle-check --printer-name {PRINTER_RAW}"
                        threading.Thread(
                            target=run_action_and_monitor,
                            args=(ip, cmd, "Авто-проверка дюз", True),
                            daemon=True
                        ).start()
        except Exception as e:
            print(f"[ERROR] Ошибка в фоновом цикле: {e}")

        time.sleep(15)

def generate_preview(orientation):
    files = glob.glob("/tmp/print_job.*")
    if not files:
        return False
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
def index():
    return render_template("index.html")

@app.route("/api/state", methods=["GET"])
def api_state():
    force_refresh = request.args.get("force") == "1"
    ip = get_ip_and_model()
    check_printer_status(ip, force=force_refresh)

    opts = get_options()
    st = load_state()

    now = time.time()
    last_print = float(st.get("last_print", now))
    idle_seconds = max(0, now - last_print)

    device_state["auto_check"] = bool(opts.get("auto_check_enabled", False))
    device_state["auto_check_days"] = int(opts.get("auto_check_days", 7))
    device_state["idle_days"] = int(idle_seconds // 86400)
    device_state["idle_hours"] = int((idle_seconds % 86400) // 3600)
    device_state["last_print_str"] = datetime.fromtimestamp(last_print).strftime("%d.%m %H:%M")

    return jsonify(device_state)

@app.route("/api/preview.jpg")
def api_preview_image():
    if os.path.exists("/tmp/preview.jpg"):
        return send_file("/tmp/preview.jpg", mimetype='image/jpeg')
    return "", 404

@app.route("/api/upload", methods=["POST"])
def api_upload():
    if 'file' not in request.files:
        return jsonify({"status": "ERROR"}), 400
    file = request.files['file']
    orientation = request.form.get("orientation", "3")
    ext = file.filename.split('.')[-1].lower()
    for f in glob.glob("/tmp/print_job.*"):
        os.remove(f)
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
    return jsonify({"status": "ERROR", "msg": f"Ошибка (HTTP {code}):\n{text}"})

def execute_print(filepath, data):
    global IS_MONITORING_JOB
    ip = get_ip_and_model()

    raw_state, _ = check_printer_status(ip, force=True)
    if raw_state == "Ошибка оборудования":
        return {"status": "ERROR", "msg": "Заблокировано: Ошибка принтера (проверьте лоток)"}
    if raw_state in ["Не найден", "Офлайн"]:
        return {"status": "ERROR", "msg": "Принтер недоступен в сети"}

    with JOB_LOCK:
        if IS_MONITORING_JOB:
            return {"status": "ERROR", "msg": "Принтер уже выполняет другое задание"}
        IS_MONITORING_JOB = True

    copies = data.get("copies", "1")
    color = data.get("color", "color")
    media = data.get("media", "A4")
    quality = data.get("quality", "4")
    orientation = data.get("orientation", "3")
    pages = data.get("pages", "").strip()

    cmd = f"lp -d {PRINTER_DOC} -n {copies} -o media={media} -o print-color-mode={color} -o print-quality={quality} -o orientation-requested={orientation} -o fit-to-page"
    if pages:
        cmd += f" -o page-ranges={pages}"
    cmd += f" '{filepath}'"

    # Сбрасываем таймер засыхания только если печать цветная и завершилась успешно
    reset_timer = (color == "color")
    threading.Thread(
        target=run_action_and_monitor,
        args=(ip, cmd, "Печать документа", reset_timer),
        daemon=True
    ).start()
    return {"status": "SUCCESS", "msg": "Отправлено на принтер. Ожидание завершения..."}

@app.route("/api/print", methods=["POST"])
def api_print():
    files = glob.glob("/tmp/print_job.*")
    if not files:
        return jsonify({"status": "ERROR", "msg": "Файл не загружен"}), 400
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
    global IS_MONITORING_JOB
    ip = get_ip_and_model()

    raw_state, _ = check_printer_status(ip, force=True)
    if raw_state == "Ошибка оборудования":
        return jsonify({"status": "ERROR", "msg": "Заблокировано: Ошибка принтера (проверьте лоток)"}), 400
    if raw_state in ["Не найден", "Офлайн"]:
        return jsonify({"status": "ERROR", "msg": "Принтер недоступен в сети"}), 404

    if action == "nozzle_check":
        cmd = f"escputil --nozzle-check --printer-name {PRINTER_RAW}"
        action_name = "Проверка дюз"
    elif action == "clean_head":
        cmd = f"escputil --clean-head --printer-name {PRINTER_RAW}"
        action_name = "Прочистка головки"
    else:
        return jsonify({"status": "ERROR", "msg": "Неизвестная команда"}), 400

    with JOB_LOCK:
        if IS_MONITORING_JOB:
            return jsonify({"status": "ERROR", "msg": "Принтер уже выполняет задание"}), 400
        IS_MONITORING_JOB = True

    os.system(f"cancel -a {PRINTER_RAW} > /dev/null 2>&1")
    threading.Thread(
        target=run_action_and_monitor,
        args=(ip, cmd, action_name, True),
        daemon=True
    ).start()
    return jsonify({"status": "SUCCESS", "msg": "Отправлено на принтер. Ожидание завершения..."})

if __name__ == "__main__":
    os.environ["PYTHONUNBUFFERED"] = "1"
    load_state()
    threading.Thread(target=background_poller, daemon=True).start()
    print("🚀 Epson Smart Service запущен на WSGI-сервере Waitress.")
    serve(app, host="0.0.0.0", port=8099, threads=6)
