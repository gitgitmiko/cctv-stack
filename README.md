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
| `ptz-guard/` | `ptz-guard`: kembalikan kamera ke posisi menghadap pagar kalau digeser (lihat di bawah) |

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

## Penjaga posisi kamera (ptz-guard)

Kamera BARDI bisa diputar lewat app, dan kalau tidak dikembalikan, zona serta notifikasi pagar jadi
salah. `ptz-guard` (service user systemd) mengecek tiap 60 dtk dengan mencocokkan frame terbaru Frigate
(ORB + RANSAC) ke foto acuan siang dan malam di `ptz-guard/refs/` (tidak ikut repo).

- Kamera ini tidak mendukung preset ONVIF dan selalu melaporkan posisi 0,0, jadi posisi dibaca dari gambar.
- Kalau posisi berubah: WA ke grup, tunggu 2 menit (`PTZ_GRACE_SECONDS`), lalu kembalikan:
  - pagar masih terlihat → geser sedikit demi sedikit (ContinuousMove singkat) sampai selisih ≤ 8 px;
  - pagar tidak terlihat → ke batas mekanis (pan kiri + tilt "-"), putar ulang urutan kalibrasi
    (3 langkah tilt, 8 langkah pan @0,5 dtk), lalu geser halus. Sekitar 1 menit.
- Gagal → WA, coba lagi 30 menit kemudian. Buat file `ptz-guard/PAUSE` untuk menonaktifkan sementara.
- Membuat ulang acuan: simpan `latest.jpg?h=360` saat kamera tepat menghadap pagar ke `refs/night.jpg`
  (malam, inframerah) dan `refs/day.jpg` (siang).

```bash
cd ptz-guard && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
ln -s "$PWD/ptz-guard.service" ~/.config/systemd/user/ && systemctl --user enable --now ptz-guard
```
