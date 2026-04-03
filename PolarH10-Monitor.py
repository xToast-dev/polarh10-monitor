#!/usr/bin/env python3
"""
Polar H10 Heart Rate + ECG Monitor
────────────────────────────────────
Minimalistisches schwebendes Fenster für KDE Wayland.
Zeigt BPM + HRV (RMSSD) oben und scrollendes ECG-Signal unten.

Abhängigkeiten:
    pip install bleak PyQt6

Starten:
    python polar_h10_monitor.py [Optionen]

Optionen:
    --address ADDR          BLE MAC-Adresse       (Standard: 24:AC:AC:16:C6:D0)
    --reconnect-delay SECS  Sekunden bis Reconnect (Standard: 3)
    --opacity FLOAT         Fenster-Transparenz   (Standard: 0.92)
    --font-size INT         Schriftgröße BPM      (Standard: 48)
    --width INT             Fensterbreite px      (Standard: 300)
    --height INT            Fensterhöhe px        (Standard: 175)
    --color HEX             Akzentfarbe           (Standard: #00e676)
    --bg-color HEX          Hintergrundfarbe      (Standard: #0a0a0c)
    --bg-alpha INT          Hintergrund-Alpha     (Standard: 235)
    --ecg-speed FLOAT       Papiergeschwindigkeit mm/s (Standard: 25.0)
    --ecg-dpi INT           Bildschirm-DPI für mm/s-Umrechnung (Standard: 96)
    --notch {0,50,60}       Netzbrumm-Notch Hz    (Standard: 50)
    --no-r-peaks            R-Zacken-Punkte deaktivieren
    --verbose / -v          Verbose Konsolen-Output
    --no-stay-on-top        Kein Always-on-top
"""

import argparse
import asyncio
import math
import struct
import sys
import time as _time
import threading
from collections import deque
from datetime import datetime

from bleak import BleakClient, BleakError

from PyQt6.QtCore import Qt, pyqtSignal, QObject, QTimer, QPointF, QRectF
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QBrush, QPainterPath
from PyQt6.QtWidgets import QApplication, QWidget

# ── Konstanten ────────────────────────────────────────────────────────────────

HR_CHAR_UUID    = "00002a37-0000-1000-8000-00805f9b34fb"
PMD_CONTROL     = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA        = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"
ECG_START_CMD   = bytearray([0x02, 0x00, 0x00, 0x01, 0x82, 0x00,
                               0x01, 0x01, 0x0E, 0x00])
ECG_SAMPLE_RATE = 130  # Hz

DEFAULTS = dict(
    address         = "24:AC:AC:16:C6:D0",
    reconnect_delay = 3,
    opacity         = 0.92,
    font_size       = 48,
    width           = 300,
    height          = 175,
    color           = "#00e676",
    bg_color        = "#0a0a0c",
    bg_alpha        = 235,
    ecg_speed       = 25.0,   # mm/s – klinischer Standard
    ecg_dpi         = 96,
    notch           = 50,
    verbose         = False,
    no_stay_on_top  = False,
    no_r_peaks      = True,
)

# ── Logging ───────────────────────────────────────────────────────────────────

_verbose = False

def log(msg: str):
    if _verbose:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[{ts}] {msg}", flush=True)

def log_always(msg: str):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] {msg}", flush=True)

# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def hex_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def bpm_color(bpm: int | None) -> QColor:
    """
    Gibt eine Farbe je nach Herzfrequenz zurück:
      <60 bpm  → kühles Blau    (Ruhe / Bradykardie)
      60–90    → Grün           (Normal)
      90–120   → Gelb-Orange    (leichte Belastung)
      120–150  → Orange         (mittlere Belastung)
      >150     → Rot            (hohe Belastung)
    Weicher Übergang per linearer Interpolation.
    """
    if bpm is None:
        return QColor(60, 62, 68)

    # Stützpunkte: (bpm, R, G, B)
    stops = [
        (40,  100, 160, 255),   # kühles Blau
        (50,   80, 200, 120),   # normales Grün
        (70,    0, 230, 118),   # lebhaftes Grün
        (80, 255, 200,   0),   # Gelb-Orange
        (90, 255, 100,   0),   # Orange
        (110, 255,  30,  30),   # Rot
    ]

    b = float(bpm)
    if b <= stops[0][0]:
        return QColor(*stops[0][1:])
    if b >= stops[-1][0]:
        return QColor(*stops[-1][1:])

    for i in range(len(stops) - 1):
        lo_bpm, lo_r, lo_g, lo_b = stops[i]
        hi_bpm, hi_r, hi_g, hi_b = stops[i+1]
        if lo_bpm <= b <= hi_bpm:
            t = (b - lo_bpm) / (hi_bpm - lo_bpm)
            return QColor(
                int(lo_r + t * (hi_r - lo_r)),
                int(lo_g + t * (hi_g - lo_g)),
                int(lo_b + t * (hi_b - lo_b)),
            )
    return QColor(60, 62, 68)

# ── Notch-Filter (IIR 2. Ordnung, Bilinear-Transform) ────────────────────────

class NotchFilter:
    """Schmalbandiger IIR-Notch-Filter. Q=30 → ~3 Hz Kerbe, kein QRS-Einfluss."""

    def __init__(self, freq_hz: float, fs: float = ECG_SAMPLE_RATE, Q: float = 30.0):
        self._active = freq_hz > 0
        if not self._active:
            return
        w0     = 2 * math.pi * freq_hz / fs
        alpha  = math.sin(w0) / (2 * Q)
        cos_w0 = math.cos(w0)
        a0     = 1 + alpha
        self._b = (1.0/a0, -2*cos_w0/a0, 1.0/a0)
        self._a = (1.0,    -2*cos_w0/a0, (1-alpha)/a0)
        self._z = [0.0, 0.0]
        log_always(f"Notch-Filter: {freq_hz} Hz  Q={Q}  "
                   f"b={[round(x,6) for x in self._b]}  "
                   f"a={[round(x,6) for x in self._a]}")

    def process(self, x: float) -> float:
        if not self._active:
            return x
        b0, b1, b2 = self._b
        _,  a1, a2 = self._a
        y          = b0*x + self._z[0]
        self._z[0] = b1*x - a1*y + self._z[1]
        self._z[1] = b2*x - a2*y
        return y

# ── HRV / RMSSD ───────────────────────────────────────────────────────────────

class HrvCalculator:
    def __init__(self, window: int = 20):
        self._rr: deque[float] = deque(maxlen=window)

    def add_rr(self, rr_ms_list: list[int]):
        for rr in rr_ms_list:
            if 300 < rr < 2000:
                self._rr.append(float(rr))

    @property
    def rmssd(self) -> float | None:
        if len(self._rr) < 4:
            return None
        rr    = list(self._rr)
        diffs = [(rr[i+1] - rr[i])**2 for i in range(len(rr)-1)]
        return math.sqrt(sum(diffs) / len(diffs))

# ── R-Zacken-Detektor (Pan-Tompkins-ähnlich) ─────────────────────────────────

class RDetector:
    REFRACTORY  = 34      # Samples (~260 ms bei 130 Hz)
    WIN         = 7       # Halbfenster für lokales Maximum
    MAX_HIST    = 130     # gleitendes Maximum über ~1 s
    THRESHOLD_K = 0.55

    def __init__(self):
        self._buf        : deque[float] = deque(maxlen=self.WIN*2+1)
        self._max_hist   : deque[float] = deque(maxlen=self.MAX_HIST)
        self._since_last : int          = self.REFRACTORY
        self._adaptive   : float        = 500.0

    def push(self, v: float) -> bool:
        self._buf.append(v)
        self._max_hist.append(abs(v))
        self._since_last += 1
        self._adaptive = max(self._max_hist) * self.THRESHOLD_K
        if len(self._buf) < self.WIN*2+1 or self._since_last < self.REFRACTORY:
            return False
        center = self._buf[self.WIN]
        if center < self._adaptive or center != max(self._buf):
            return False
        self._since_last = 0
        return True

# ── ECG Frame Parsing ─────────────────────────────────────────────────────────

def parse_ecg_frame(data: bytes) -> list[int]:
    if len(data) < 11 or data[0] != 0x00:
        return []
    raw     = data[10:]
    samples = [int.from_bytes(raw[i:i+3], "little", signed=True)
               for i in range(0, len(raw)-2, 3)]
    if _verbose and samples:
        vals = "  ".join(f"{v:>7d}" for v in samples)
        log(f"ECG {len(samples):>2} samples │ "
            f"min={min(samples):>7d}  max={max(samples):>7d}  µV\n"
            f"          [{vals}]")
    return samples

def parse_hr_full(data: bytes) -> tuple[int, list[int]]:
    flags, idx = data[0], 1
    if flags & 0x01:
        bpm = struct.unpack_from("<H", data, idx)[0]; idx += 2
    else:
        bpm = data[idx]; idx += 1
    if (flags >> 3) & 0x01:
        idx += 2
    rr = []
    if (flags >> 4) & 0x01:
        while idx + 1 < len(data):
            rr.append(int(struct.unpack_from("<H", data, idx)[0] * 1000 / 1024))
            idx += 2
    return bpm, rr

# ── BLE Worker ────────────────────────────────────────────────────────────────

class BleWorker(QObject):
    hr_updated     = pyqtSignal(int, list)
    ecg_samples    = pyqtSignal(list)
    status_changed = pyqtSignal(str)

    def __init__(self, address: str, reconnect_delay: int):
        super().__init__()
        self._address         = address
        self._reconnect_delay = reconnect_delay
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = True

    def start(self):
        threading.Thread(target=self._run_loop, daemon=True).start()

    def stop(self):
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect_loop())

    async def _connect_loop(self):
        while self._running:
            self.status_changed.emit("Verbinde …")
            log_always(f"Verbinde mit {self._address} …")
            try:
                async with BleakClient(self._address, timeout=12.0) as client:
                    log_always(f"Verbunden  MTU={client.mtu_size}")
                    if _verbose:
                        for svc in client.services:
                            log(f"  Svc {svc.uuid} ({svc.description})")
                            for ch in svc.characteristics:
                                log(f"    Chr {ch.uuid} {ch.properties}")
                    await client.start_notify(HR_CHAR_UUID, self._on_hr)
                    ctrl = await client.read_gatt_char(PMD_CONTROL)
                    log_always(f"PMD Features: {ctrl.hex()}")
                    await client.write_gatt_char(PMD_CONTROL, ECG_START_CMD,
                                                  response=True)
                    await client.start_notify(PMD_DATA, self._on_ecg)
                    log_always("ECG+HR Notify gestartet")
                    self.status_changed.emit("Verbunden")
                    while self._running and client.is_connected:
                        await asyncio.sleep(1)
                    await client.stop_notify(PMD_DATA)
                    await client.stop_notify(HR_CHAR_UUID)
            except BleakError as e:
                log_always(f"BleakError: {e}")
                self.status_changed.emit(f"BLE: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_always(f"Fehler: {e}")
                self.status_changed.emit(f"Fehler: {e}")
            if self._running:
                log_always(f"Reconnect in {self._reconnect_delay}s …")
                self.status_changed.emit("Reconnect …")
                await asyncio.sleep(self._reconnect_delay)

    def _on_hr(self, _h, data: bytearray):
        try:
            bpm, rr = parse_hr_full(bytes(data))
            log_always(f"HR  {bpm:>3} bpm  RR={rr}")
            self.hr_updated.emit(bpm, rr)
        except Exception as e:
            log_always(f"HR-Fehler: {e}")

    def _on_ecg(self, _h, data: bytearray):
        try:
            s = parse_ecg_frame(bytes(data))
            if s:
                self.ecg_samples.emit(s)
        except Exception as e:
            log_always(f"ECG-Fehler: {e}  raw={bytes(data).hex()}")

# ── MonitorWindow ─────────────────────────────────────────────────────────────
#
# ECG-Rendering:
#   Ring-Buffer mit den letzten N Samples (N = sichtbares Zeitfenster in Samples).
#   N wird aus --ecg-speed (mm/s), --ecg-dpi und der Fensterbreite berechnet,
#   sodass die Papiergeschwindigkeit klinisch korrekt ist.
#
#   paintEvent zeichnet alle Samples als Polyline von links → rechts.
#   Der neueste Sample liegt immer ganz rechts. Neue Samples verdrängen
#   alte aus der deque → automatisches Scrollen ohne Pixmap-Tricks.
#
#   Amplitude-Skala: links ein schmaler Streifen mit ±mV-Beschriftung.

class MonitorWindow(QWidget):
    """
    ECG-Widget mit EKG-Schreibkopf-Prinzip:

    Eine QPixmap (ecg_px) hält den gesamten sichtbaren ECG-Bereich.
    Ein Schreibkopf (needle_x) bewegt sich mit exakt px_per_s px/s von links
    nach rechts – angetrieben durch perf_counter() dt, nicht durch BLE-Batches.

    Neue Samples aus dem BLE-Buffer werden am Schreibkopf eingezeichnet sobald
    sie fällig sind (akkumulierter Schreibbedarf >= 1 px). So ist die Bewegung
    60fps-flüssig und das Signal pixel-genau.

    Vor dem Schreibkopf wird ein kleiner Streifen gelöscht (EKG-Papier-Effekt).
    """

    SCALE_W  = 28    # Breite der Amplitudenskala links
    GAP_PX   = 12    # Breite des Löschstreifens vor der Nadel

    def __init__(self, cfg: argparse.Namespace):
        super().__init__()
        self._cfg = cfg

        # ── BPM / HRV ─────────────────────────────────────────────────────────
        self._bpm: int | None   = None
        self._hrv: float | None = None
        self._hrv_disp: float   = 0.0
        self._hrv_calc          = HrvCalculator(window=20)
        self._status            = "Starte …"
        self._pulse: float      = 1.0

        # ── Geschwindigkeit (Mausrad-verstellbar) ──────────────────────────────
        self._speed_mm_s: float = cfg.ecg_speed   # aktuell eingestellte mm/s
        self._px_per_mm:  float = cfg.ecg_dpi / 25.4
        self._speed_label_alpha: float = 0.0       # 0=unsichtbar, 1=voll sichtbar
        self._update_speed(cfg.ecg_speed, log=True)

        # ── Schreibkopf-Zustand ────────────────────────────────────────────────
        self._ecg_px:    object = None   # QPixmap, lazy init
        self._needle_x:  float  = 0.0   # aktuelle Schreibposition (px, float)
        self._write_acc: float  = 0.0   # akkumulierte, noch nicht gezeichnete px
        self._last_y:    float | None = None  # letzter Y-Wert für Linienkontinuität

        # ── Eingehende Samples ────────────────────────────────────────────────
        self._pending: deque[float] = deque()

        # ── Filter + Detektor ─────────────────────────────────────────────────
        self._notch = NotchFilter(cfg.notch, ECG_SAMPLE_RATE)
        self._rdet  = RDetector()
        # R-Zacken: Liste von (x_px, y_px) direkt in Pixmap-Koordinaten
        self._r_marks: list[tuple[float, float]] = []

        # ── Amplitude ─────────────────────────────────────────────────────────
        self._amp_lo: float = -1000.0
        self._amp_hi: float =  1000.0

        # ── Timing ────────────────────────────────────────────────────────────
        self._last_tick_t  = _time.perf_counter()
        self._fps_frames   = 0
        self._fps_samples  = 0
        self._fps_t0       = _time.perf_counter()

        # ── Timer ─────────────────────────────────────────────────────────────
        self._timer = QTimer(self)
        self._timer.setInterval(16)   # ~60 fps
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self._setup_window()

    # ── Setup ─────────────────────────────────────────────────────────────────

    def _setup_window(self):
        cfg   = self._cfg
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if not cfg.no_stay_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowOpacity(cfg.opacity)
        self.setFixedSize(cfg.width, cfg.height)
        self.setWindowTitle("Polar H10")

    # ── Geschwindigkeit ändern ───────────────────────────────────────────────

    # Erlaubte mm/s-Stufen (klinische Standardwerte + sinnvolle Extras)
    _SPEED_STEPS = (6.25, 12.5, 25.0, 50.0, 100.0)

    def _update_speed(self, mm_s: float, log: bool = False):
        """Setzt neue Papiergeschwindigkeit und setzt Schreibkopf zurück."""
        self._speed_mm_s = mm_s
        self._px_per_s   = mm_s * self._px_per_mm
        self._px_per_smp = self._px_per_s / ECG_SAMPLE_RATE
        if log:
            log_always(f"ECG: {mm_s} mm/s  "
                       f"→ {self._px_per_s:.1f} px/s  "
                       f"→ {self._px_per_smp:.3f} px/sample")
        # Pixmap neu initialisieren damit alte Kurve bei falscher Skalierung verschwindet
        self._ecg_px    = None
        self._last_y    = None
        self._write_acc = 0.0
        self._needle_x  = 0.0
        self._speed_label_alpha = 1.0   # Label einblenden

    # ── Layout-Helfer ─────────────────────────────────────────────────────────

    def _ecg_area(self) -> tuple[int, int, int, int]:
        """Gibt (x, y, w, h) des ECG-Plotbereichs zurück."""
        H     = self.height()
        bpm_h = int(H * 0.46)
        x     = self.SCALE_W
        y     = bpm_h + 2
        w     = self.width() - self.SCALE_W - 2
        h     = H - y - 14
        return x, y, w, h

    # ── Amplitude ─────────────────────────────────────────────────────────────

    def _v_to_y(self, v: float, h: int) -> float:
        decay = 0.0003
        if v < self._amp_lo:
            self._amp_lo = v
        else:
            self._amp_lo += decay * 80
        if v > self._amp_hi:
            self._amp_hi = v
        else:
            self._amp_hi -= decay * 80
        mid  = (self._amp_lo + self._amp_hi) / 2
        span = max(self._amp_hi - self._amp_lo, 500.0)
        self._amp_lo = mid - span / 2
        self._amp_hi = mid + span / 2
        pad  = 4
        norm = max(0.02, min(0.98, (v - self._amp_lo) / (span or 1)))
        return pad + (h - 2*pad) * (1.0 - norm)

    # ── BLE Slots ─────────────────────────────────────────────────────────────

    def on_hr(self, bpm: int, rr: list):
        self._bpm = bpm
        self._pulse = 0.0
        self._hrv_calc.add_rr(rr)
        self._hrv = self._hrv_calc.rmssd
        if _verbose and self._hrv:
            log(f"HRV RMSSD={self._hrv:.1f} ms")

    def on_ecg(self, samples: list):
        for raw in samples:
            self._pending.append(self._notch.process(float(raw)))
        self._fps_samples += len(samples)

    def on_status(self, status: str):
        self._status = status
        if "Verbunden" not in status:
            self._bpm      = None
            self._hrv      = None
            self._pending.clear()
            self._rdet     = RDetector()
            self._last_y   = None
            self._r_marks  = []
            self._write_acc = 0.0
            if self._ecg_px:
                from PyQt6.QtGui import QPixmap as _QP
                self._ecg_px.fill(Qt.GlobalColor.transparent)
        self.update()

    # ── Tick ──────────────────────────────────────────────────────────────────

    def _tick(self):
        now = _time.perf_counter()
        dt  = min(now - self._last_tick_t, 0.05)
        self._last_tick_t = now

        # HRV-Interpolation
        target = self._hrv if self._hrv is not None else 0.0
        self._hrv_disp += (target - self._hrv_disp) * 0.08

        # Puls-Flash
        if self._pulse < 1.0:
            self._pulse = min(self._pulse + dt * 2.5, 1.0)

        # Speed-Label ausblenden (nach 2 s)
        if self._speed_label_alpha > 0.0:
            self._speed_label_alpha = max(0.0, self._speed_label_alpha - dt * 0.6)

        # ECG-Schreibkopf vorschieben und Samples einzeichnen
        if self._ecg_px is None:
            self._init_ecg_px()
        if self._ecg_px is not None:
            self._advance(dt)

        # Verbose FPS
        if _verbose:
            self._fps_frames += 1
            elapsed = now - self._fps_t0
            if elapsed >= 2.0:
                log_always(
                    f"PERF  render={self._fps_frames/elapsed:5.1f} fps  "
                    f"ble={self._fps_samples/elapsed:5.1f} smp/s  "
                    f"pending={len(self._pending)}"
                )
                self._fps_frames = self._fps_samples = 0
                self._fps_t0 = now

        self.update()

    # ── Pixmap-Initialisierung ────────────────────────────────────────────────

    def _init_ecg_px(self):
        from PyQt6.QtGui import QPixmap as _QP
        _, _, w, h   = self._ecg_area()
        if w <= 0 or h <= 0:
            return
        self._ecg_px = _QP(w, h)
        self._ecg_px.fill(Qt.GlobalColor.transparent)
        self._needle_x  = 0.0
        self._write_acc = 0.0
        self._last_y    = None
        self._r_marks   = []

    # ── Schreibkopf vorschieben ───────────────────────────────────────────────

    def _advance(self, dt: float):
        """Schreibkopf um dt * px_per_s px vorschieben und Samples einzeichnen."""
        _, _, ecg_w, ecg_h = self._ecg_area()
        accent  = QColor(*hex_rgb(self._cfg.color))

        px = QPainter(self._ecg_px)
        px.setRenderHint(QPainter.RenderHint.Antialiasing)

        pen = QPen(accent, 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        px.setPen(pen)

        # Pixel die dieser Frame zurückgelegt werden
        step = dt * self._px_per_s
        self._write_acc += step

        # Pro "fälligem" Pixel: einen Sample aus dem Buffer nehmen und zeichnen
        while self._write_acc >= 1.0 and self._pending:
            # Einen Sample konsumieren
            v = self._pending.popleft()
            y = self._v_to_y(v, ecg_h)

            # Schreibkopf um 1 px vorschieben
            self._needle_x += 1.0
            self._write_acc -= 1.0
            if self._needle_x >= ecg_w:
                self._needle_x = 0.0
                self._last_y   = None

            nx = self._needle_x

            # Löschstreifen vor dem Schreibkopf (EKG-Papier-Effekt)
            gap_start = int(nx + 1) % ecg_w
            gap_end   = min(gap_start + self.GAP_PX, ecg_w)
            px.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_Clear)
            px.fillRect(gap_start, 0, gap_end - gap_start,
                        ecg_h, Qt.GlobalColor.transparent)
            # Zweiter Teil falls Wrap-around
            overflow = (gap_start + self.GAP_PX) - ecg_w
            if overflow > 0:
                px.fillRect(0, 0, overflow, ecg_h, Qt.GlobalColor.transparent)
            px.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_SourceOver)
            px.setPen(pen)

            # Linie vom letzten Punkt zum aktuellen
            if self._last_y is not None:
                px.drawLine(QPointF(nx - 1.0, self._last_y),
                            QPointF(nx,       y))

            # R-Zacke?
            is_r = self._rdet.push(v)
            if is_r:
                log(f"R-Zacke  x={nx:.0f}  {v:.0f} µV")
            if is_r and not self._cfg.no_r_peaks:
                px.setPen(Qt.PenStyle.NoPen)
                px.setBrush(QBrush(QColor(
                    accent.red(), accent.green(), accent.blue(), 80)))
                px.drawEllipse(QPointF(nx, y), 4.5, 4.5)
                px.setBrush(QBrush(QColor(255, 255, 255, 230)))
                px.drawEllipse(QPointF(nx, y), 2.0, 2.0)
                px.setPen(pen)

            self._last_y = y

        px.end()

    # ── paintEvent ────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        cfg   = self._cfg
        W, H  = self.width(), self.height()
        accent = QColor(*hex_rgb(cfg.color))
        bg_r, bg_g, bg_b = hex_rgb(cfg.bg_color)

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        # ── Hintergrund ───────────────────────────────────────────────────────
        p.setBrush(QBrush(QColor(bg_r, bg_g, bg_b, cfg.bg_alpha)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(0, 0, W, H, 14, 14)

        bpm_h    = int(H * 0.46)
        ecg_x, ecg_y, ecg_w, ecg_h = self._ecg_area()

        # ── BPM ───────────────────────────────────────────────────────────────
        bpm_w    = int(W * 0.62)
        hrv_x    = bpm_w
        bpm_text = str(self._bpm) if self._bpm else "--"
        fb = QFont("Inter", cfg.font_size, QFont.Weight.Bold)
        fb.setStyleHint(QFont.StyleHint.SansSerif)
        fb.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, -1.5)
        p.setFont(fb)
        bc = bpm_color(self._bpm)
        p.setPen(QColor(0, 0, 0, 50))
        p.drawText(QRectF(1, 3, bpm_w, bpm_h-16),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   bpm_text)
        p.setPen(bc)
        p.drawText(QRectF(0, 0, bpm_w, bpm_h-16),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   bpm_text)
        fl = QFont("Inter", 8)
        fl.setStyleHint(QFont.StyleHint.SansSerif)
        fl.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.8)
        p.setFont(fl)
        p.setPen(QColor(90, 92, 100, 180))
        p.drawText(QRectF(0, bpm_h-20, bpm_w, 14),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   "BPM")

        # ── HRV ───────────────────────────────────────────────────────────────
        if self._hrv_disp > 1.0:
            alpha = min(255, int(255 * min(1.0, self._hrv_disp / 10.0)))
            fh = QFont("Inter", int(cfg.font_size * 0.46), QFont.Weight.Bold)
            fh.setStyleHint(QFont.StyleHint.SansSerif)
            p.setFont(fh)
            p.setPen(QColor(accent.red(), accent.green(), accent.blue(), alpha))
            p.drawText(QRectF(hrv_x, 4, W-hrv_x-4, bpm_h-22),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                       f"{self._hrv_disp:.0f}")
            fls = QFont("Inter", 7)
            fls.setStyleHint(QFont.StyleHint.SansSerif)
            p.setFont(fls)
            p.setPen(QColor(80, 82, 92, alpha))
            p.drawText(QRectF(hrv_x, bpm_h-22, W-hrv_x-4, 12),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                       "RMSSD")
            p.drawText(QRectF(hrv_x, bpm_h-12, W-hrv_x-4, 10),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                       "ms")
            p.setPen(QPen(QColor(255, 255, 255, 8), 1))
            p.drawLine(hrv_x, 10, hrv_x, bpm_h-6)

        # ── Trennlinie ────────────────────────────────────────────────────────
        p.setPen(QPen(QColor(255, 255, 255, 10), 1))
        p.drawLine(ecg_x, ecg_y, W-2, ecg_y)

        # ── Pulsierender Rand ─────────────────────────────────────────────────
        if self._pulse < 1.0:
            ease = 1.0 - self._pulse
            p.setPen(QPen(
                QColor(accent.red(), accent.green(), accent.blue(), int(160*ease**2)),
                0.8 + 2.2*ease))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(1, 1, W-2, H-2, 13, 13)

        # ── ECG-Pixmap ────────────────────────────────────────────────────────
        if self._ecg_px and not self._ecg_px.isNull():
            p.drawPixmap(ecg_x, ecg_y, self._ecg_px)

            # Schreibnadel (senkrechte Linie an needle_x)
            nx = ecg_x + int(self._needle_x)
            p.setPen(QPen(
                QColor(accent.red(), accent.green(), accent.blue(), 40), 1))
            p.drawLine(nx, ecg_y + 2, nx, ecg_y + ecg_h - 2)
        else:
            fs = QFont("Inter", 7)
            fs.setStyleHint(QFont.StyleHint.SansSerif)
            p.setFont(fs)
            p.setPen(QColor(55, 57, 65))
            p.drawText(QRectF(ecg_x, ecg_y, ecg_w, ecg_h),
                       Qt.AlignmentFlag.AlignCenter, "ECG …")

        # ── Amplitudenskala ───────────────────────────────────────────────────
        import math as _m
        amp_span_mv = (self._amp_hi - self._amp_lo) / 1000.0
        for step_mv in (0.1, 0.2, 0.5, 1.0, 2.0, 5.0):
            if amp_span_mv / step_mv <= 8:
                break
        fs2 = QFont("Inter", 5)
        fs2.setStyleHint(QFont.StyleHint.SansSerif)
        p.setFont(fs2)
        first_mv = _m.floor(self._amp_hi / 1000.0 / step_mv) * step_mv
        tick_mv  = first_mv
        while tick_mv >= self._amp_lo / 1000.0 - step_mv:
            tick_uv = tick_mv * 1000.0
            span    = max(self._amp_hi - self._amp_lo, 500.0)
            norm    = max(0.02, min(0.98, (tick_uv - self._amp_lo) / span))
            y_tick  = ecg_y + 4 + (ecg_h - 8) * (1.0 - norm)
            if ecg_y <= y_tick <= ecg_y + ecg_h:
                p.setPen(QPen(QColor(255, 255, 255, 10), 1,
                              Qt.PenStyle.DotLine))
                p.drawLine(QPointF(ecg_x, y_tick),
                           QPointF(ecg_x + ecg_w - 1, y_tick))
                label = "0" if tick_mv == 0 else (
                    f"{tick_mv:+.0f}" if abs(tick_mv) >= 1.0
                    else f"{tick_mv:+.1f}")
                p.setPen(QColor(100, 102, 112, 180))
                p.drawText(QRectF(0, y_tick-5, self.SCALE_W-3, 10),
                           Qt.AlignmentFlag.AlignRight |
                           Qt.AlignmentFlag.AlignVCenter, label)
            tick_mv -= step_mv
        p.setPen(QPen(QColor(255, 255, 255, 15), 1))
        p.drawLine(ecg_x, ecg_y+2, ecg_x, ecg_y+ecg_h-2)

        # ── Geschwindigkeits-Label ───────────────────────────────────────────
        if self._speed_label_alpha > 0.01:
            a_spd = int(255 * self._speed_label_alpha)
            f_spd = QFont("Inter", 8, QFont.Weight.Bold)
            f_spd.setStyleHint(QFont.StyleHint.SansSerif)
            p.setFont(f_spd)
            # Hintergrund-Pill
            spd_text = f"{self._speed_mm_s:g} mm/s"
            fm       = p.fontMetrics()
            tw       = fm.horizontalAdvance(spd_text) + 12
            th       = 16
            sx       = ecg_x + ecg_w - tw - 4
            sy       = ecg_y + 4
            p.setBrush(QBrush(QColor(0, 0, 0, int(140 * self._speed_label_alpha))))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRectF(sx, sy, tw, th), 4, 4)
            p.setPen(QColor(accent.red(), accent.green(), accent.blue(), a_spd))
            p.drawText(QRectF(sx, sy, tw, th),
                       Qt.AlignmentFlag.AlignCenter, spd_text)

        # ── Status ────────────────────────────────────────────────────────────
        fs3 = QFont("Inter", 6)
        fs3.setStyleHint(QFont.StyleHint.SansSerif)
        p.setFont(fs3)
        p.setPen(QColor(60, 62, 72, 200))
        p.drawText(QRectF(0, H-14, W, 12),
                   Qt.AlignmentFlag.AlignCenter, self._status)

    # ── Drag ──────────────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            h = self.windowHandle()
            if h:
                h.startSystemMove()

    def mouseDoubleClickEvent(self, _event):
        QApplication.quit()

    # ── Mausrad: Geschwindigkeit anpassen ─────────────────────────────────────

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta == 0:
            return
        steps  = self._SPEED_STEPS
        cur    = self._speed_mm_s
        # Nächste/Vorherige Stufe wählen
        if delta > 0:
            # schneller
            faster = [s for s in steps if s > cur]
            new_s  = faster[0] if faster else steps[-1]
        else:
            # langsamer
            slower = [s for s in steps if s < cur]
            new_s  = slower[-1] if slower else steps[0]
        if new_s != cur:
            self._update_speed(new_s, log=True)
            log_always(f"Geschwindigkeit: {new_s} mm/s")
        event.accept()

# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Polar H10 – BPM + HRV + ECG Monitor",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    D = DEFAULTS
    p.add_argument("--address",         default=D["address"])
    p.add_argument("--reconnect-delay", default=D["reconnect_delay"], type=int)
    p.add_argument("--opacity",         default=D["opacity"],         type=float)
    p.add_argument("--font-size",       default=D["font_size"],       type=int)
    p.add_argument("--width",           default=D["width"],           type=int)
    p.add_argument("--height",          default=D["height"],          type=int)
    p.add_argument("--color",           default=D["color"])
    p.add_argument("--bg-color",        default=D["bg_color"])
    p.add_argument("--bg-alpha",        default=D["bg_alpha"],        type=int)
    p.add_argument("--ecg-speed",       default=D["ecg_speed"],       type=float,
                   help="Papiergeschwindigkeit mm/s (25=klinisch, 50=schnell)")
    p.add_argument("--ecg-dpi",         default=D["ecg_dpi"],         type=int,
                   help="Bildschirm-DPI für mm/s-Umrechnung")
    p.add_argument("--notch",           default=D["notch"],           type=int,
                   choices=[0, 50, 60],
                   help="Netzbrumm-Notch Hz (0=aus)")
    p.add_argument("--no-r-peaks",      action="store_true",
                   help="R-Zacken-Punkte im ECG-Graph ausblenden")
    p.add_argument("--verbose", "-v",   action="store_true")
    p.add_argument("--no-stay-on-top",  action="store_true")
    return p.parse_args()

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    cfg = parse_args()
    global _verbose
    _verbose = cfg.verbose

    log_always("Polar H10 Monitor startet")
    log_always(f"Gerät:   {cfg.address}")
    log_always(f"Fenster: {cfg.width}×{cfg.height}  opacity={cfg.opacity}")
    log_always(f"ECG:     {cfg.ecg_speed} mm/s  Notch={cfg.notch} Hz")

    try:
        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(True)

        window = MonitorWindow(cfg)

        worker = BleWorker(cfg.address, cfg.reconnect_delay)
        worker.hr_updated.connect(window.on_hr)
        worker.ecg_samples.connect(window.on_ecg)
        worker.status_changed.connect(window.on_status)
        worker.start()

        window.show()
        code = app.exec()
        worker.stop()
        log_always("Beendet.")
        sys.exit(code)

    except AttributeError as e:
        import traceback
        print("\n" + "═"*60, file=sys.stderr)
        print("FEHLER: Fehlende Attribute in MonitorWindow.", file=sys.stderr)
        print(f"  {e}", file=sys.stderr)
        print("  Verwende die neueste Dateiversion komplett neu.", file=sys.stderr)
        print("═"*60, file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        sys.exit(1)

    except Exception as e:
        import traceback
        print(f"\nFATAL: {e}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
