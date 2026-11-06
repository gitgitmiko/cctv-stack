#!/usr/bin/env python3
"""Frigate -> WhatsApp notifier: kirim pesan hanya saat pagar dibuka.

Frigate menjalankan model state classification `status_pagar` pada potongan gambar
daun pagar dan menerbitkan statusnya ("terbuka"/"tertutup") ke MQTT setelah 3
hasil berturut-turut sama. Notifier berlangganan topik itu dan mengirim foto +
link ke grup WhatsApp lewat `hermes send` setiap status berubah menjadi terbuka.
Berjalan sebagai systemd user service cctv-notifier (venv: .venv, butuh paho-mqtt).
"""
import json
import logging
import os
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt

FRIGATE = os.environ.get("FRIGATE_API", "http://127.0.0.1:5000")
PUBLIC_URL = os.environ.get("FRIGATE_PUBLIC_URL", "https://cctv.gitgitmiko.my.id")
WA_TARGET = os.environ["WA_TARGET"]  # whatsapp:<group-jid>@g.us
HERMES = os.environ.get("HERMES_BIN", "/home/gitgitmiko/.hermes/hermes-agent/.hermes/bin/hermes")
CAMERA = os.environ.get("GATE_CAMERA", "pagar")
MODEL = os.environ.get("GATE_MODEL", "status_pagar")
OPEN_STATE = os.environ.get("GATE_OPEN_STATE", "terbuka")
MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER") or os.environ.get("FRIGATE_MQTT_USER", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD") or os.environ.get("FRIGATE_MQTT_PASSWORD", "")
# Jeda minimal antar pesan, supaya pagar yang dibuka-tutup berkali-kali dalam waktu
# singkat tidak membanjiri grup (dan nomor WA tidak resmi tetap bervolume rendah).
COOLDOWN_SECONDS = float(os.environ.get("COOLDOWN_SECONDS", "60"))
# Tunggu sebentar sebelum memotret supaya orang/motor yang masuk ikut terlihat.
SNAPSHOT_DELAY = float(os.environ.get("SNAPSHOT_DELAY", "1.5"))
WORK_DIR = Path(os.environ.get("WORK_DIR", Path(__file__).parent / "tmp"))

TOPIC = f"frigate/{CAMERA}/classification/{MODEL}"
LABELS = {"person": "Orang", "car": "Mobil", "motorcycle": "Motor", "bicycle": "Sepeda", "cat": "Kucing", "dog": "Anjing"}

log = logging.getLogger("cctv-notifier")


def get_json(path):
    with urllib.request.urlopen(FRIGATE + path, timeout=10) as r:
        return json.load(r)


def download(path, dest, attempts=3):
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(FRIGATE + path, timeout=10) as r:
                if r.status == 200:
                    dest.write_bytes(r.read())
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False


def recent_context(opened_at):
    """Objek yang terdeteksi Frigate di sekitar waktu pagar dibuka, plus link review-nya bila ada."""
    objects, link = set(), f"{PUBLIC_URL}/#{CAMERA}"
    try:
        for e in get_json(f"/api/events?camera={CAMERA}&after={opened_at - 30}&limit=20"):
            objects.add(LABELS.get(e["label"], e["label"]))
        for r in get_json(f"/api/review?cameras={CAMERA}&after={opened_at - 60}&limit=10"):
            if r["start_time"] <= opened_at + 10 and (r["end_time"] is None or r["end_time"] >= opened_at - 10):
                link = f"{PUBLIC_URL}/review?id={r['id']}"
                break
    except (OSError, ValueError) as err:
        log.warning("gagal membaca event Frigate: %s", err)
    return sorted(objects), link


def send(text, image=None):
    body = f"{text} MEDIA:{image}" if image else text
    r = subprocess.run([HERMES, "send", "--to", WA_TARGET, body], capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        log.error("hermes send failed (%s): %s", r.returncode, (r.stderr or r.stdout).strip()[:500])
    return r.returncode == 0


class GateWatcher:
    def __init__(self):
        self.state = None
        self.last_sent = 0.0
        self.lock = threading.Lock()

    def on_connect(self, client, userdata, flags, reason_code, properties):
        log.info("mqtt connected (%s), subscribe %s", reason_code, TOPIC)
        client.subscribe(TOPIC, qos=1)

    def on_message(self, client, userdata, msg):
        state = msg.payload.decode(errors="replace").strip()
        if not state:  # pesan kosong = penghapusan retained di broker, bukan status
            return
        with self.lock:
            previous, self.state = self.state, state
        # Pesan retained = status lama yang tersimpan di broker, bukan kejadian baru.
        if msg.retain:
            log.info("status awal (retained): %s", state)
            return
        log.info("status pagar: %s -> %s", previous, state)
        if state == OPEN_STATE and previous != OPEN_STATE:
            # Kirim di thread terpisah: memotret + hermes send bisa makan beberapa detik,
            # sedangkan loop MQTT harus tetap jalan (keepalive).
            threading.Thread(target=self.notify_open, args=(time.time(),), daemon=True).start()

    def notify_open(self, opened_at):
        with self.lock:
            if opened_at - self.last_sent < COOLDOWN_SECONDS:
                log.info("pagar dibuka, dilewati (cooldown)")
                return
            self.last_sent = opened_at
        time.sleep(SNAPSHOT_DELAY)
        image = WORK_DIR / f"pagar-{int(opened_at)}.jpg"
        has_image = download(f"/api/{CAMERA}/latest.jpg?h=720", image)
        objects, link = recent_context(opened_at)
        when = datetime.fromtimestamp(opened_at).strftime("%d/%m/%Y %H:%M:%S")
        text = "🚪 *Pagar dibuka*\n" + (f"👀 {', '.join(objects)}\n" if objects else "") + f"🕐 {when} WIB\n🔗 {link}"
        if send(text, image if has_image else None):
            log.info("terkirim: pagar dibuka %s", when)
        image.unlink(missing_ok=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    watcher = GateWatcher()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="cctv-notifier")
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.on_connect = watcher.on_connect
    client.on_message = watcher.on_message
    client.reconnect_delay_set(min_delay=2, max_delay=60)
    log.info("menunggu status pagar di %s -> %s", TOPIC, WA_TARGET)
    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            client.loop_forever(retry_first_connection=True)
        except OSError as err:
            log.warning("mqtt tidak bisa dihubungi: %s", err)
            time.sleep(10)


if __name__ == "__main__":
    main()
