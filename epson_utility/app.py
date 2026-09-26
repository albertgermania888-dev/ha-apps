import os
import time
import json
import subprocess
import requests
from flask import Flask, render_template, jsonify

app = Flask(__name__)

OPTIONS_PATH = "/data/options.json"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
HA_API_URL = "http://supervisor/core/api/states/sensor.epson_print_result"
PRINTER = "EpsonL3250"

def get_ip():
    if os.path.exists(OPTIONS_PATH):
        with open(OPTIONS_PATH) as f:
            opts = json.load(f)
            return opts.get("printer_ip", "192.168.1.108")
    return "192.168.1.108"

def update_ha(state, msg):
    if not SUPERVISOR_TOKEN:
        return
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"}
    data = {"state": state, "attributes": {"message": msg, "icon": "mdi:printer-check"}}
    try:
        requests.post(HA_API_URL, headers=headers, json=data, timeout=5)
    except Exception as e:
        print(f"[WARNING] Ошибка связи с Home Assistant: {e}")

def setup_cups(ip):
    # Запускаем фоновую службу CUPS внутри контейнера
    os.system("service cups start > /dev/null 2>&1")
    time.sleep(2)
    
    check = subprocess.run(f"lpstat -p {PRINTER}", shell=True, capture_output=True, text=True)
    if PRINTER not in check.stdout:
        print(f"[INFO] Создание RAW-очереди CUPS для принтера {PRINTER}...")
        # Подключаем сетевой сокет, чтобы CUPS пробрасывал сырые данные от escputil
        os.system(f"lpadmin -p {PRINTER} -v socket://{ip}:9100 -E")

@app.route("/")
def index():
    return render_template("index.html", ip=get_ip())

@app.route("/api/<action>", methods=["POST"])
def do_action(action):
    ip = get_ip()
    
    # Быстрая проверка, включен ли принтер физически
    if subprocess.run(["ping", "-c", "2", "-W", "2", ip], stdout=subprocess.DEVNULL).returncode != 0:
        err = "Принтер недоступен в сети (выключен)"
        update_ha("ERROR", err)
        return jsonify({"status": "ERROR", "msg": err}), 500

    setup_cups(ip)
    
    # Очистка очереди от старых/зависших заданий
    os.system(f"cancel -a {PRINTER} > /dev/null 2>&1")

    # Формируем команду строго по документации escputil через CUPS
    print(f"[INFO] Запуск escputil для команды: {action}")
    if action == "nozzle_check":
        cmd = f"escputil --nozzle-check --printer-name {PRINTER}"
    elif action == "clean_head":
        cmd = f"escputil --clean-head --printer-name {PRINTER}"
    else:
        return jsonify({"status": "ERROR", "msg": "Неизвестная команда"}), 400

    # escputil поставит задачу в CUPS
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    
    if result.returncode != 0:
        err = result.stderr.strip() if result.stderr else "Внутренняя ошибка escputil"
        print(f"[ERROR] {err}")
        update_ha("ERROR", err)
        return jsonify({"status": "ERROR", "msg": err}), 500

    print("[INFO] Задание в очереди. Ожидание ответа от принтера...")
    
    # Мониторинг очереди CUPS на ошибки механики (нет бумаги/замятие)
    for _ in range(15):
        time.sleep(3)
        stat = subprocess.run(f"lpstat -p {PRINTER}", shell=True, capture_output=True, text=True).stdout.lower()
        
        if "media-empty" in stat or "out of paper" in stat or "бумага" in stat:
            err = "Нет бумаги в лотке принтера!"
            print(f"[ERROR] {err}")
            os.system(f"cancel -a {PRINTER}") 
            update_ha("ERROR", err)
            return jsonify({"status": "ERROR", "msg": err}), 500
            
        queue = subprocess.run("lpstat -W not-completed", shell=True, capture_output=True, text=True).stdout
        if PRINTER not in queue:
            msg = f"Задача '{action}' выполнена успешно"
            print(f"[SUCCESS] {msg}")
            update_ha("SUCCESS", msg)
            return jsonify({"status": "SUCCESS", "msg": "Успешно отправлено на печать"})
            
    err = "Таймаут (принтер думает слишком долго или зажевал бумагу)"
    os.system(f"cancel -a {PRINTER}")
    update_ha("ERROR", err)
    return jsonify({"status": "ERROR", "msg": err}), 500

if __name__ == "__main__":
    os.environ["PYTHONUNBUFFERED"] = "1"
    app.run(host="0.0.0.0", port=8099)
