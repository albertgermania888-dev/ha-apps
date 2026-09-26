import os, json, subprocess
import requests
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# Получаем настройки аддона
OPTIONS_PATH = "/data/options.json"
if os.path.exists(OPTIONS_PATH):
    with open(OPTIONS_PATH) as f:
        options = json.load(f)
else:
    options = {"printer_ip": "192.168.1.108"}

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
# Сенсор, который скрипт сам создаст и будет обновлять в HA
HA_API_URL = "http://supervisor/core/api/states/sensor.epson_print_result"

def update_ha_sensor(state, message=""):
    """Прямая отправка результата в Home Assistant минуя логи"""
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
    requests.post(HA_API_URL, headers=headers, json=data)

@app.route("/")
def index():
    # Главная страница Web UI
    return render_template("index.html", ip=options.get("printer_ip"))

@app.route("/api/<action>", methods=["POST"])
def do_action(action):
    ip = options.get("printer_ip")
    
    if action == "nozzle_check":
        cmd = ["escputil", "--nozzle-check", f"--raw-device=net:{ip}:9100"]
    elif action == "clean_head":
        cmd = ["escputil", "--clean-head", f"--raw-device=net:{ip}:9100"]
    else:
        return jsonify({"status": "ERROR", "msg": "Неизвестная команда"}), 400

    try:
        # Запуск утилиты с ожиданием результата
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if result.returncode == 0:
            update_ha_sensor("SUCCESS", f"Команда {action} выполнена успешно")
            return jsonify({"status": "SUCCESS", "msg": "Успешно"})
        else:
            update_ha_sensor("ERROR", result.stderr)
            return jsonify({"status": "ERROR", "msg": result.stderr})
    except Exception as e:
        update_ha_sensor("ERROR", str(e))
        return jsonify({"status": "ERROR", "msg": str(e)})

if __name__ == "__main__":
    # Запуск сервера на порту для Ingress
    app.run(host="0.0.0.0", port=8099)

