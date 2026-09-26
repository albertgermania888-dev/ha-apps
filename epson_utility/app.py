import os
import json
import subprocess
import requests
from flask import Flask, render_template, jsonify

app = Flask(__name__)

OPTIONS_PATH = "/data/options.json"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
HA_API_URL = "http://supervisor/core/api/states/sensor.epson_print_result"

def get_ip():
    if os.path.exists(OPTIONS_PATH):
        with open(OPTIONS_PATH) as f:
            opts = json.load(f)
            return opts.get("printer_ip", "192.168.1.108")
    return "192.168.1.108"

def update_ha_sensor(state, message=""):
    if not SUPERVISOR_TOKEN:
        return
    headers = {
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "state": state,
        "attributes": {"message": message, "icon": "mdi:printer-check"}
    }
    try:
        requests.post(HA_API_URL, headers=headers, json=data, timeout=5)
    except Exception as e:
        print(f"[WARNING] Ошибка обновления сенсора HA: {e}")

@app.route("/")
def index():
    return render_template("index.html", ip=get_ip())

@app.route("/api/<action>", methods=["POST"])
def do_action(action):
    ip = get_ip()
    temp_file = "/tmp/epson_job.bin"
    
    print(f"[INFO] Запуск команды: {action} для принтера {ip}")

    # 1. Генерируем команды и сохраняем СТРОГО в файл
    # -q подавляет вывод лицензии, > /dev/null скрывает остальной текстовый мусор
    if action == "nozzle_check":
        esc_cmd = f"escputil --nozzle-check -q --raw-device={temp_file} > /dev/null 2>&1"
    elif action == "clean_head":
        esc_cmd = f"escputil --clean-head -q --raw-device={temp_file} > /dev/null 2>&1"
    else:
        return jsonify({"status": "ERROR", "msg": "Неизвестная команда"}), 400

    print(f"[INFO] Выполнение: {esc_cmd}")
    os.system(esc_cmd)

    # Проверяем, сформировался ли бинарный файл с заданием
    if not os.path.exists(temp_file) or os.path.getsize(temp_file) == 0:
        err_msg = "Файл задания не сформирован (возможно, принтер не поддерживается или утилита вернула ошибку)"
        print(f"[ERROR] {err_msg}")
        update_ha_sensor("ERROR", err_msg)
        return jsonify({"status": "ERROR", "msg": err_msg}), 500

    # 2. Отправляем чистый бинарный файл на принтер через netcat
    nc_cmd = f"nc -q 1 -w 10 {ip} 9100 < {temp_file}"
    print(f"[INFO] Отправка данных по сети: {nc_cmd}")
    
    result = subprocess.run(nc_cmd, shell=True, capture_output=True, text=True)

    # 3. Убираем за собой
    if os.path.exists(temp_file):
        os.remove(temp_file)
        print("[INFO] Временный файл задания удален")

    # Обработка результатов сети
    if result.returncode == 0:
        msg = f"Команда '{action}' успешно доставлена на принтер"
        print(f"[SUCCESS] {msg}")
        update_ha_sensor("SUCCESS", msg)
        return jsonify({"status": "SUCCESS", "msg": "Успешно отправлено на принтер"})
    else:
        err_msg = result.stderr.strip() if result.stderr else "Ошибка подключения к принтеру (выключен или недоступен)"
        print(f"[ERROR] Сетевая ошибка: {err_msg}")
        update_ha_sensor("ERROR", err_msg)
        return jsonify({"status": "ERROR", "msg": err_msg})

if __name__ == "__main__":
    # Явное включение небуферизованного вывода для Docker логов
    os.environ["PYTHONUNBUFFERED"] = "1"
    app.run(host="0.0.0.0", port=8099)
