"""ptz-guard: kembalikan kamera "pagar" ke posisi menghadap pagar kalau digeser (mis. lewat app Bardi).

Kamera (Tuya/Bardi, ONVIF di port 8000) tidak mendukung preset ONVIF dan tidak melaporkan posisi,
jadi posisi dibaca dari gambar: frame terbaru Frigate dicocokkan (ORB) dengan foto acuan siang/malam.

- Selisih kecil  -> geser sedikit demi sedikit sampai cocok (servo).
- Pagar tak terlihat -> ke batas mekanis (titik nol), putar ulang urutan hasil kalibrasi, lalu servo.

Setelah posisi berubah, ada masa tunggu (GRACE_SECONDS) supaya orang yang sedang melihat-lihat lewat
app sempat selesai. Semua kejadian dikabarkan ke grup WA lewat Hermes. Buat file PAUSE di folder ini
untuk menonaktifkan sementara.
"""
import asyncio
import logging
import os
import subprocess
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np
from onvif import ONVIFCamera

HERE = Path(__file__).resolve().parent
CAM_HOST = os.environ.get("PTZ_CAM_HOST", "192.168.18.102")
CAM_PORT = int(os.environ.get("PTZ_CAM_PORT", "8000"))
CAM_USER = os.environ["FRIGATE_CAM_USER"]
CAM_PASS = os.environ["FRIGATE_CAM_PASSWORD"]
FRIGATE = os.environ.get("FRIGATE_API", "http://127.0.0.1:5000")
CAMERA = os.environ.get("PTZ_CAMERA", "pagar")
CHECK_INTERVAL = int(os.environ.get("PTZ_CHECK_INTERVAL", "60"))
GRACE_SECONDS = int(os.environ.get("PTZ_GRACE_SECONDS", "120"))
FAIL_BACKOFF = int(os.environ.get("PTZ_FAIL_BACKOFF", "1800"))
WA_TARGET = os.environ.get("WA_TARGET", "")
HERMES = os.environ.get("HERMES_BIN", "/home/gitgitmiko/.hermes/hermes-agent/.hermes/bin/hermes")

# Ambang (gambar 640x360)
ALIGNED_PX = 25        # dianggap pada posisi kalau selisih <= ini
SERVO_TOL_PX = 8       # target akhir servo
MIN_INLIERS = 25       # minimal titik cocok agar hasil dipercaya
# Urutan dari titik nol (batas kiri + batas "tilt -") ke pagar, hasil kalibrasi 2026-11-21.
HOME_SEQ = {"tilt_steps": 3, "pan_steps": 8, "step_secs": 0.5}

log = logging.getLogger("ptz-guard")
REFS = {name: cv2.imread(str(HERE / "refs" / f"{name}.jpg"), cv2.IMREAD_GRAYSCALE) for name in ("night", "day")}
REFS = {k: cv2.resize(v, (640, 360)) for k, v in REFS.items() if v is not None}
ORB = cv2.ORB_create(2000)
MASK = np.ones((360, 640), np.uint8)
MASK[:30, :260] = 0    # abaikan teks jam di pojok kiri atas
_REF_FEATS = {k: ORB.detectAndCompute(v, MASK) for k, v in REFS.items()}


def notify(text):
    log.info("WA: %s", text)
    if not WA_TARGET:
        return
    try:
        subprocess.run([HERMES, "send", "--to", WA_TARGET, text], capture_output=True, timeout=90)
    except (OSError, subprocess.SubprocessError) as err:
        log.warning("hermes send gagal: %s", err)


def grab():
    data = urllib.request.urlopen(f"{FRIGATE}/api/{CAMERA}/latest.jpg?h=360", timeout=15).read()
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    return cv2.resize(img, (640, 360))


def measure(img):
    """(dx, dy, inliers) terhadap acuan yang paling cocok, atau None."""
    k2, d2 = ORB.detectAndCompute(img, MASK)
    best = None
    if d2 is None:
        return None
    for name, (k1, d1) in _REF_FEATS.items():
        if d1 is None:
            continue
        ms = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(d1, d2)
        if len(ms) < 10:
            continue
        p1 = np.float32([k1[m.queryIdx].pt for m in ms])
        p2 = np.float32([k2[m.trainIdx].pt for m in ms])
        M, inl = cv2.estimateAffinePartial2D(p1, p2, method=cv2.RANSAC, ransacReprojThreshold=4)
        if M is None:
            continue
        r = (float(M[0, 2]), float(M[1, 2]), int(inl.sum()))
        if best is None or r[2] > best[2]:
            best = r
    return best


def aligned(r):
    return bool(r) and r[2] >= MIN_INLIERS and abs(r[0]) <= ALIGNED_PX and abs(r[1]) <= ALIGNED_PX


class Ptz:
    async def __aenter__(self):
        wsdl = Path(__import__("onvif").__file__).parent / "wsdl"
        self.cam = ONVIFCamera(CAM_HOST, CAM_PORT, CAM_USER, CAM_PASS, wsdl_dir=str(wsdl))
        await self.cam.update_xaddrs()
        media = await self.cam.create_media_service()
        self.ptz = await self.cam.create_ptz_service()
        self.tok = (await media.GetProfiles())[0].token
        return self

    async def __aexit__(self, *exc):
        await self.cam.close()

    async def pulse(self, x, y, secs, rest=1.5):
        await self.ptz.ContinuousMove({"ProfileToken": self.tok, "Velocity": {"PanTilt": {"x": x, "y": y}}})
        await asyncio.sleep(secs)
        await self.ptz.Stop({"ProfileToken": self.tok, "PanTilt": True, "Zoom": True})
        await asyncio.sleep(rest)

    async def servo(self, max_steps=25):
        for _ in range(max_steps):
            await asyncio.sleep(3)
            r = measure(grab())
            if not r or r[2] < MIN_INLIERS:
                return False
            dx, dy, _ = r
            if abs(dx) <= SERVO_TOL_PX and abs(dy) <= SERVO_TOL_PX:
                return True
            if abs(dx) / 15 >= abs(dy) / 20:
                await self.pulse(0.2 if dx > 0 else -0.2, 0, min(0.8, max(0.1, abs(dx) / 150)), rest=0.5)
            else:
                await self.pulse(0, -0.2 if dy > 0 else 0.2, min(0.8, max(0.1, abs(dy) / 200)), rest=0.5)
        r = measure(grab())
        return aligned(r)

    async def home_and_replay(self):
        await self.pulse(-1.0, 0, 10)
        await self.pulse(0, -1.0, 5)
        for _ in range(HOME_SEQ["tilt_steps"]):
            await self.pulse(0, 1.0, HOME_SEQ["step_secs"])
        for _ in range(HOME_SEQ["pan_steps"]):
            await self.pulse(1.0, 0, HOME_SEQ["step_secs"])


async def recover():
    async with Ptz() as cam:
        r = measure(grab())
        near = r and r[2] >= 40 and abs(r[0]) < 250 and abs(r[1]) < 180
        if near and await cam.servo():
            return True
        await cam.home_and_replay()
        return await cam.servo()


async def loop():
    moved_since = None
    backoff_until = 0
    log.info("mulai; cek tiap %ss, masa tunggu %ss, acuan: %s", CHECK_INTERVAL, GRACE_SECONDS, ", ".join(REFS))
    while True:
        try:
            if (HERE / "PAUSE").exists() or time.time() < backoff_until:
                moved_since = None
            else:
                r = measure(grab())
                if aligned(r):
                    if moved_since:
                        log.info("kembali ke posisi tanpa campur tangan: %s", r)
                    moved_since = None
                else:
                    now = time.time()
                    if moved_since is None:
                        # Periksa sekali lagi supaya pergantian siang/malam tidak dikira digeser.
                        await asyncio.sleep(10)
                        r = measure(grab())
                        if not aligned(r):
                            moved_since = now
                            log.info("posisi berubah: %s", r)
                            notify(f"📹 Posisi kamera {CAMERA} berubah (mungkin digeser lewat app Bardi). "
                                   f"Akan dikembalikan otomatis dalam {GRACE_SECONDS // 60} menit.")
                    elif now - moved_since >= GRACE_SECONDS:
                        log.info("mengembalikan posisi…")
                        t0 = time.time()
                        ok = await recover()
                        moved_since = None
                        if ok:
                            notify(f"✅ Kamera {CAMERA} sudah kembali menghadap pagar ({time.time() - t0:.0f} dtk).")
                        else:
                            backoff_until = time.time() + FAIL_BACKOFF
                            notify(f"⚠️ Gagal mengembalikan posisi kamera {CAMERA}. Dicoba lagi {FAIL_BACKOFF // 60} menit lagi; "
                                   "cek manual lewat app Bardi.")
        except Exception as err:  # kamera/Frigate sesaat tidak bisa dihubungi
            log.warning("cek gagal: %s", err)
        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(loop())
