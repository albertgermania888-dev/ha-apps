import os
import json
import subprocess
import requests
from flask import Flask, render_template, jsonify

app = Flask(__name__)

# Пути и константы Home Assistant
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
        print(f"HA Sensor update error: {e}")

@app.route("/")
def index():
    return render_template("index.html", ip=get_ip())

@app.route("/api/<action>", methods=["POST"])
def do_action(action):
    ip = get_ip()

    # escputil генерирует ESC/P команды и выдает их в стандартный вывод (/dev/stdout)
    if action == "nozzle_check":
        esc_cmd = ["escputil", "--nozzle-check", "--raw-device=/dev/stdout"]
    elif action == "clean_head":
        esc_cmd = ["escputil", "--clean-head", "--raw-device=/dev/stdout"]
    else:
        return jsonify({"status": "ERROR", "msg": "Неизвестная команда"}), 400

    # netcat отправляет полученные данные на 9100 порт принтера (-q 1 = выйти через 1 сек после отправки)
    nc_cmd = ["nc", "-q", "1", ip, "9100"]

    try:
        # Связываем вывод первой команды с вводом второй (аналог pipe | в терминале)
        p1 = subprocess.Popen(esc_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p2 = subprocess.Popen(nc_cmd, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        # Разрешаем p1 завершиться, если p2 отвалится
        p1.stdout.close()
        
        # Ждем завершения отправки сети (p2)
        out, err = p2.communicate(timeout=45)
        # Получаем возможные ошибки от утилиты принтера
        p1_out, p1_err = p1.communicate(timeout=5)
        
        err_msg = err.decode('utf-8').strip()
        p1_err_msg = p1_err.decode('utf-8').strip()

        if p2.returncode == 0:
            update_ha_sensor("SUCCESS", f"Команда {action} успешно отправлена")
            return jsonify({"status": "SUCCESS", "msg": "Успешно отправлено на принтер"})
        else:
            # Если принтер выключен, nc вернет ошибку подключения
            final_err = err_msg if err_msg else (p1_err_msg if p1_err_msg else "Ошибка подключения к принтеру")
            update_ha_sensor("ERROR", final_err)
            return jsonify({"status": "ERROR", "msg": final_err})

    except subprocess.TimeoutExpired:
        # Защита от зависших процессов
        p2.kill()
        p1.kill()
        update_ha_sensor("ERROR", "Таймаут сетевого соединения")
        return jsonify({"status": "ERROR", "msg": "Принтер не отвечает (таймаут)"})
    except Exception as e:
        update_ha_sensor("ERROR", str(e))
        return jsonify({"status": "ERROR", "msg": str(e)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8099)
