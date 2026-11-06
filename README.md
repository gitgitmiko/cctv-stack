# cctv-stack

Stack CCTV rumah di `gitgit-server`: **Frigate** (NVR + deteksi objek), **Home Assistant**, **Mosquitto** (MQTT), dan **cctv-notifier** yang mengirim notifikasi ke grup WhatsApp **hanya saat pagar dibuka**.

> Repo ini publik. Rahasia (password kamera/MQTT/admin), database, log, model terlatih, dan dataset gambar tidak ikut di-commit — lihat `.gitignore` (daftar putih).

## Isi

| Path | Fungsi |
|---|---|
| `docker-compose.yml` | Mosquitto, Frigate (VAAPI + OpenVINO di Intel iGPU), Home Assistant |
| `.env.example` | Variabel rahasia yang dibaca compose/Frigate (`{FRIGATE_*}` di `config.yml`) |
| `frigate/config/config.yml` | Kamera `pagar` (BARDI, RTSP), zona, rekaman, dan model state classification `status_pagar` |
| `homeassistant/` | Konfigurasi dasar Home Assistant |
| `mosquitto/config/mosquitto.conf` | Broker MQTT, hanya localhost, wajib login |
| `notifier/` | `cctv-notifier`: dengar status pagar di MQTT → kirim foto + link lewat `hermes send` |

Rekaman 24 jam per jam ke `/mnt/cctv/<tanggal>/` dikerjakan terpisah oleh `cctv-recorder` (repo `dashboard_gitgit`).

## Menjalankan

```bash
cp .env.example .env                       # isi kredensial kamera dan akun MQTT
set -a; . ./.env; set +a
docker run --rm -v "$PWD/mosquitto/config:/mosquitto/config" eclipse-mosquitto:2 \
  mosquitto_passwd -c -b /mosquitto/config/passwd "$FRIGATE_MQTT_USER" "$FRIGATE_MQTT_PASSWORD"
docker compose up -d
```

Frigate butuh disk CCTV ter-mount di `/mnt/cctv` (container menolak start bila tidak ada).

## Notifikasi "pagar dibuka"

1. Frigate menjalankan model **`status_pagar`** (MobileNetV2 kecil, label `terbuka`/`tertutup`) pada potongan daun pagar `crop: [0.27, 0.12, 0.62, 0.86]` di stream deteksi 640×360. Model jalan saat ada gerakan di area itu (maks. ~1×/dtk) dan tiap 10 dtk; status sah setelah 3 hasil berturut-turut ≥ 0,8, lalu diterbitkan ke MQTT `frigate/pagar/classification/status_pagar`.
2. `notifier/notifier.py` berlangganan topik itu dan mengirim pesan saat status berubah menjadi `terbuka` (pesan *retained* saat start diabaikan; jeda antar pesan `COOLDOWN_SECONDS`).

Pasang notifier:

```bash
cd notifier
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp notifier.env.example notifier.env        # isi WA_TARGET (JID grup WhatsApp)
cp cctv-notifier.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now cctv-notifier
```

### Model & dataset

Model dilatih dari 1.109 potongan gambar rekaman (606 tertutup, 503 terbuka; siang, malam, orang/motor di balik pagar tertutup, pagar sedang dibuka/ditutup). Di server:

- dataset: `/mnt/cctv/frigate/clips/status_pagar/dataset/{terbuka,tertutup}/` (milik root → tambah gambar lewat `docker cp`)
- model: `frigate/config/model_cache/status_pagar/model.tflite`
- latih ulang (~4 menit, tanpa restart Frigate): `curl -X POST http://127.0.0.1:5000/api/classification/status_pagar/train`

**Kalau kamera diputar/digeser**, potongan `crop` tidak lagi tepat di pagar: gambar ulang crop-nya (UI Frigate → Classification, atau `config.yml`) lalu latih ulang dengan contoh dari sudut baru.
