"""
ui.py — MARK L (JARVIS) desktop HUD, built on PyQt6.

This file is the "face" of the app: a sci-fi holographic-style window with
an animated avatar orb, always-visible live panels (Reminders, Calendar, a
textured rotating Earth globe), a scrolling conversation log, and a handful
of full-screen overlays (setup, customization, remote pairing, the
"Global Ops" globe/telemetry page).

Important boundary: nothing in this file talks to Gemini or decides what
JARVIS should do. It only displays state and forwards user input back out
through callbacks — main.py's JarvisLive is the only thing that wires those
callbacks to real behavior (see JarvisUI's on_text_command / on_interrupt /
on_remote_clicked attributes, set from main.py after construction).

Threading rule that matters everywhere in this file: Qt widgets may only be
touched from the main/UI thread. Anything that does a network call, runs a
subprocess (AppleScript, etc.), or otherwise blocks, does that work on a
background thread and reports the result back via a pyqtSignal — never by
calling a widget method directly from that thread. Look for the
`_sig`/`*_sig` + `.connect(...)` pattern throughout as the recurring example
of this (e.g. LogWidget._sig, GlobalOpsOverlay._loc_sig / _weather_sig).

Rough map of what's in here, top to bottom:
  - C: the color palette / theme constants shared by every widget.
  - _SysMetrics: a background-polled CPU/RAM/GPU/temp snapshot, read by
    several widgets (the footer bars, the Global Ops gauges).
  - HudCanvas: the big animated avatar orb — the main visual focal point.
  - Small always-visible pieces: MetricBar, LogWidget, FileDropZone,
    _CameraPreview, RemindersPanel, CalendarPanel.
  - EarthGlobe / _EarthCanvas + friends (_ArcGauge, _MiniWorldMap,
    _BarEqualizer, _StatusGrid, _BarcodeStrip): the rotating globe widget
    and the decorative HUD chrome reused by the full-screen overlay.
  - GlobalOpsOverlay: the full-screen "expand" page reached from the
    globe panel — real CPU/mem/location/weather data, wrapped in
    broadcast-HUD-style decoration.
  - SetupOverlay, CustomizeOverlay, RemoteKeyOverlay, ClipboardPanel: other
    full-screen/floating overlays for onboarding, settings, phone pairing,
    and the copy-text quick-actions popup.
  - MainWindow: assembles all of the above into the actual window layout.
  - JarvisUI: the thin public interface main.py actually imports and
    drives (JarvisUI("face.png") in main()) — wraps MainWindow plus the
    QApplication event loop.
"""

from __future__ import annotations

import json
import math
import os
import platform
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

try:
    import numpy as np
except ImportError:
    np = None  # falls back to the procedural vector globe if unavailable

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}

from PyQt6.QtCore import (
    QEasingCurve, QMimeData, QObject, QPointF, QRectF, QSize, Qt,
    QTimer, QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QDragEnterEvent, QDropEvent, QFont,
    QFontDatabase, QImage, QKeySequence, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap, QPolygonF, QRadialGradient, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget, QProgressBar,
)

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR   = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"


def _read_full_config() -> dict:
    """Read api_keys.json config dict. Returns {} on any error."""
    try:
        return json.loads(API_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


_DEFAULT_W, _DEFAULT_H = 1180, 820
_MIN_W,     _MIN_H     = 980, 640
_LEFT_W  = 148
_RIGHT_W = 340

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


class C:
    """Shared color palette — every widget in this file pulls its colors
    from here rather than hardcoding hex values, so the whole HUD's theme
    (cyan JARVIS look) stays consistent and changeable in one place."""
    BG        = "#00060a"
    PANEL     = "#010d14"
    PANEL2    = "#010f18"
    BORDER    = "#0d3347"
    BORDER_B  = "#1a5c7a"
    BORDER_A  = "#0f4060"
    PRI       = "#00d4ff"
    PRI_DIM   = "#007a99"
    PRI_GHO   = "#001f2e"
    ACC       = "#ff6b00"
    ACC2      = "#ffcc00"
    GREEN     = "#00ff88"
    GREEN_D   = "#00aa55"
    RED       = "#ff3355"
    MUTED_C   = "#ff3366"
    TEXT      = "#8ffcff"
    TEXT_DIM  = "#3a8a9a"
    TEXT_MED  = "#5ab8cc"
    WHITE     = "#d8f8ff"
    DARK      = "#000d14"
    BAR_BG    = "#011520"


# Ana renge (accent) bağlı anahtarlar — durum renkleri (ACC, GREEN, RED…) sabit kalır
_HUE_LINKED = (
    "BG", "PANEL", "PANEL2", "BORDER", "BORDER_B", "BORDER_A",
    "PRI", "PRI_DIM", "PRI_GHO", "TEXT", "TEXT_DIM", "TEXT_MED",
    "WHITE", "DARK", "BAR_BG",
)
_PALETTE_DEFAULTS: dict[str, str] = {k: getattr(C, k) for k in _HUE_LINKED}

DEFAULT_UI_COLOR = _PALETTE_DEFAULTS["PRI"]


def apply_ui_accent(accent_hex: str) -> bool:
    """
    Seçilen accent rengine göre tüm turkuaz-ailesi paleti yeniden türetir
    (hue kaydırma — parlaklık/doygunluk oranları korunur, tasarım bozulmaz).
    Boyanan öğeler (HUD, dalga formu, metrikler) bir sonraki karede yeni
    rengi alır; stylesheet tabanlı paneller yeniden kurulduklarında alır.
    """
    import colorsys

    accent_hex = (accent_hex or "").strip().lower()
    if not (accent_hex.startswith("#") and len(accent_hex) == 7):
        return False
    try:
        int(accent_hex[1:], 16)
    except ValueError:
        return False

    def _hsv(h: str) -> tuple[float, float, float]:
        r = int(h[1:3], 16) / 255
        g = int(h[3:5], 16) / 255
        b = int(h[5:7], 16) / 255
        return colorsys.rgb_to_hsv(r, g, b)

    base_h            = _hsv(_PALETTE_DEFAULTS["PRI"])[0]
    acc_h, acc_s, _av = _hsv(accent_hex)
    dh   = acc_h - base_h
    grey = acc_s < 0.08   # griye yakın accent → tüm tema desaturize edilir

    for key, hex0 in _PALETTE_DEFAULTS.items():
        h, s, v = _hsv(hex0)
        if grey:
            s *= 0.15
        r, g, b = colorsys.hsv_to_rgb((h + dh) % 1.0, s, v)
        setattr(C, key, "#{:02x}{:02x}{:02x}".format(
            int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5)))
    return True


def current_palette() -> dict[str, str]:
    """C sınıfındaki accent'e bağlı renklerin anlık kopyası."""
    return {k: getattr(C, k) for k in _HUE_LINKED}


def retheme_all_widgets(old: dict[str, str], new: dict[str, str]) -> None:
    """
    CANLI tam tema değişimi. Uygulamadaki HER widget'ın stylesheet'inde eski
    palet renklerini yenileriyle değiştirir ve yeniden çizdirir. Böylece renk
    değişimi yalnızca boyanan öğelerde değil, panel/buton/kenarlık dahil tüm
    arayüzde ANINDA uygulanır — yeniden başlatma gerekmez.
    """
    mapping = {old[k].lower(): new[k].lower()
               for k in old if old[k].lower() != new.get(k, old[k]).lower()}
    if not mapping:
        return
    app = QApplication.instance()
    if app is None:
        return
    for w in app.allWidgets():
        try:
            ss = w.styleSheet()
            if ss:
                s2 = ss
                for o, n in mapping.items():
                    if o in s2:
                        s2 = s2.replace(o, n)
                if s2 != ss:
                    w.setStyleSheet(s2)
            w.update()
        except Exception:
            pass


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h); c.setAlpha(a); return c


_MONO_FAMILIES = ["SF Mono", "JetBrains Mono", "Menlo", "Cascadia Mono",
                  "Consolas", "DejaVu Sans Mono", "Courier New"]
_UI_FAMILIES   = ["SF Pro Display", "Inter", "Segoe UI Variable", "Segoe UI",
                  "Helvetica Neue", "Noto Sans", "DejaVu Sans", "Arial"]


def _hud_font(size: int, weight=None, mono: bool = True) -> QFont:
    """Modern HUD font: a clean monospace stack (SF Mono / Menlo / Consolas)
    with sensible fallbacks, so every platform gets a crisp, current look."""
    f = QFont()
    f.setFamilies(_MONO_FAMILIES if mono else _UI_FAMILIES)
    f.setPointSize(max(6, int(size)))
    if weight is not None:
        f.setWeight(weight)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


def _ui_font(size: int, weight=None) -> QFont:
    return _hud_font(size, weight, mono=False)


# ── Windows GPU via NVML DLL (no subprocess, no console window) ──────────────
_nvml_lib: object = None   # cached ctypes DLL
_nvml_ok:  object = None   # None=untested, True=works, False=unavailable


def _nvml_gpu_windows() -> float:
    """Return NVIDIA GPU utilisation % using nvml.dll directly — zero subprocess."""
    global _nvml_lib, _nvml_ok
    if _nvml_ok is False:
        return -1.0
    try:
        import ctypes

        class _Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        if _nvml_lib is None:
            for dll_name in ("nvml", r"C:\Windows\System32\nvml.dll"):
                try:
                    lib = ctypes.WinDLL(dll_name)
                    lib.nvmlInit_v2()
                    _nvml_lib = lib
                    break
                except Exception:
                    continue

        if _nvml_lib is None:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            _nvml_ok = True
            return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)

        dev = ctypes.c_void_p()
        _nvml_lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
        util = _Util()
        _nvml_lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(util))
        _nvml_ok = True
        return float(util.gpu)
    except Exception:
        _nvml_ok = False
        return -1.0


class _SysMetrics:
    """Background-polled system telemetry (CPU/mem/net/GPU/temp), shared by
    every widget that displays it (footer bars, Global Ops gauges) so each
    one doesn't poll psutil/NVML independently. Runs its own daemon thread
    (_loop) and just exposes plain attributes — callers read a snapshot via
    .snapshot(), they don't get pushed updates."""
    def __init__(self):
        self.cpu  = 0.0
        self.mem  = 0.0
        self.net  = 0.0   
        self.gpu  = -1.0  
        self.tmp  = -1.0  
        self._lock = threading.Lock()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        self._running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(1.5)

    def _update(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent

        nc  = psutil.net_io_counters()
        now = time.time()
        dt  = now - self._last_net_t
        if dt > 0:
            sent = (nc.bytes_sent - self._last_net.bytes_sent) / dt
            recv = (nc.bytes_recv - self._last_net.bytes_recv) / dt
            net  = (sent + recv) / (1024 * 1024)
        else:
            net = 0.0
        self._last_net   = nc
        self._last_net_t = now

        gpu = self._get_gpu()

        tmp = self._get_temp()

        with self._lock:
            self.cpu = cpu
            self.mem = mem
            self.net = net
            self.gpu = gpu
            self.tmp = tmp

    def _get_gpu(self) -> float:
        # pynvml — subprocess-free, works on all platforms if installed
        try:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)
        except Exception:
            pass

        # Windows: nvml.dll via ctypes (already cached in _nvml_gpu_windows)
        if _OS == "Windows":
            return _nvml_gpu_windows()

        # Linux / macOS: libnvidia-ml shared lib via ctypes
        try:
            import ctypes
            _lib = "libnvidia-ml.so.1" if _OS == "Linux" else "libnvidia-ml.dylib"

            class _Util(ctypes.Structure):
                _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

            nv = ctypes.CDLL(_lib)
            nv.nvmlInit_v2()
            dev = ctypes.c_void_p()
            nv.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
            u = _Util()
            nv.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u))
            return float(u.gpu)
        except Exception:
            pass

        return -1.0   # N/A — zero subprocess on all platforms

    def _get_temp(self) -> float:
        # psutil — works on Linux; occasionally Windows with driver support
        try:
            temps = psutil.sensors_temperatures()
            for name in ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                         "cpu-thermal", "zenpower", "it8688"]:
                if name in temps and temps[name]:
                    return temps[name][0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass

        # Windows: wmi module (pure Python COM, zero subprocess)
        if _OS == "Windows":
            try:
                import wmi  # type: ignore
                w = wmi.WMI(namespace="root/wmi")
                tz = w.MSAcpi_ThermalZoneTemperature()
                if tz:
                    return (tz[0].CurrentTemperature / 10.0) - 273.15
            except Exception:
                pass

        return -1.0   # N/A — zero subprocess on all platforms

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu": self.cpu,
                "mem": self.mem,
                "net": self.net,
                "gpu": self.gpu,
                "tmp": self.tmp,
            }


_metrics = _SysMetrics()

class HudCanvas(QWidget):
    """
    The big animated avatar orb — the main visual focal point of the HUD.
    Entirely hand-painted with QPainter in paintEvent (no images except the
    optional circular face photo): pulsing halo, scan lines, orbiting rings,
    idle particles, all driven by a single QTimer tick (~60fps) in _step.

    Reflects JARVIS's current state (LISTENING / THINKING / SPEAKING /
    SLEEPING, set via self.state) with different colors/animation energy —
    set from MainWindow via JarvisUI.set_state(), which main.py calls
    whenever JarvisLive's own state changes. This widget has no idea what
    Gemini is doing; it only knows the state string it was told.
    """
    def __init__(self, face_path: str, assistant_name: str = "J.A.R.V.I.S", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.muted    = False
        self.speaking = False
        self.state    = "INITIALISING"
        self._assistant_name = assistant_name

        self._tick       = 0
        self._scale      = 1.0
        self._tgt_scale  = 1.0
        self._halo       = 55.0
        self._tgt_halo   = 55.0
        self._last_t     = time.time()
        self._scan       = 0.0
        self._scan2      = 180.0
        self._rings      = [0.0, 120.0, 240.0]
        self._pulses: list[float] = [0.0, 50.0, 100.0]
        self._blink      = True
        self._blink_tick = 0
        self._particles: list[list[float]] = []
        self._face_px: QPixmap | None = None
        self._load_face(face_path)

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(16)

    def _load_face(self, path: str):
        try:
            from PIL import Image, ImageDraw
            import io
            img = Image.open(path).convert("RGBA")
            sz  = min(img.size)
            img = img.resize((sz, sz), Image.LANCZOS)
            mk  = Image.new("L", (sz, sz), 0)
            ImageDraw.Draw(mk).ellipse((2, 2, sz - 2, sz - 2), fill=255)
            img.putalpha(mk)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap(); px.loadFromData(buf.getvalue())
            self._face_px = px
        except Exception:
            self._face_px = None

    def _step(self):
        self._tick += 1
        now = time.time()
        if now - self._last_t > (0.12 if self.speaking else 0.5):
            if self.speaking:
                self._tgt_scale = random.uniform(1.06, 1.14)
                self._tgt_halo  = random.uniform(145, 190)
            elif self.muted:
                self._tgt_scale = random.uniform(0.998, 1.002)
                self._tgt_halo  = random.uniform(15, 28)
            else:
                self._tgt_scale = random.uniform(1.001, 1.008)
                self._tgt_halo  = random.uniform(48, 68)
            self._last_t = now

        sp = 0.38 if self.speaking else 0.15
        self._scale += (self._tgt_scale - self._scale) * sp
        self._halo  += (self._tgt_halo  - self._halo)  * sp

        speeds = [1.3, -0.9, 2.0] if self.speaking else [0.55, -0.35, 0.9]
        for i, spd in enumerate(speeds):
            self._rings[i] = (self._rings[i] + spd) % 360

        self._scan  = (self._scan  + (3.0 if self.speaking else 1.3)) % 360
        self._scan2 = (self._scan2 + (-2.0 if self.speaking else -0.75)) % 360

        _ah = max(120, self.height() - 74)
        fw  = min(self.width(), _ah) * 0.9
        lim = fw * 0.5
        spd = 4.2 if self.speaking else 2.0
        self._pulses = [r + spd for r in self._pulses if r + spd < lim]
        if len(self._pulses) < 3 and random.random() < (0.07 if self.speaking else 0.025):
            self._pulses.append(fw * 0.16)

        if self.speaking and random.random() < 0.28:
            cx, cy = self.width() / 2, _ah / 2 + 6
            ang = random.uniform(0, 2 * math.pi)
            r_s = fw * 0.2
            self._particles.append([
                cx + math.cos(ang) * r_s, cy + math.sin(ang) * r_s,
                math.cos(ang) * random.uniform(0.9, 2.4),
                math.sin(ang) * random.uniform(0.9, 2.4) - 0.4, 1.0,
            ])
        self._particles = [
            [p[0]+p[2], p[1]+p[3], p[2]*0.97, p[3]*0.97, p[4]-0.028]
            for p in self._particles if p[4] > 0
        ]

        self._blink_tick += 1
        if self._blink_tick >= 38:
            self._blink = not self._blink
            self._blink_tick = 0
        self.update()

    # ── Modern "arc reactor" renderer ────────────────────────────────────
    def _state_color(self) -> str:
        if self.muted:
            return C.MUTED_C
        if self.speaking:
            return C.PRI
        if self.state in ("THINKING", "PROCESSING"):
            return C.ACC2
        if self.state == "LISTENING":
            return C.GREEN
        return C.PRI

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        status_h = 74
        avail_h = max(120, H - status_h)
        fw = min(W, avail_h) * 0.9
        cx, cy = W / 2, avail_h / 2 + 6
        sc = self._state_color()
        base = C.MUTED_C if self.muted else C.PRI
        t = self._tick

        # background: soft vignette + dot grid
        bg = QRadialGradient(QPointF(cx, cy), max(W, H) * 0.75)
        bg.setColorAt(0.0, qcol(C.PRI_GHO))
        bg.setColorAt(0.55, qcol(C.BG))
        bg.setColorAt(1.0, qcol(C.BG))
        p.fillRect(self.rect(), QBrush(bg))
        p.setPen(QPen(qcol(C.BORDER, 110), 1))
        for x in range(12, W, 28):
            for y in range(12, H, 28):
                p.drawPoint(x, y)

        def ring_rect(r):
            return QRectF(cx - r, cy - r, r * 2, r * 2)

        level = max(0.0, min(1.0, (self._halo - 15) / 175))   # 0..1 energy
        r_core = fw * 0.16 * self._scale

        # outer ticked dial (slow rotation)
        R0 = fw * 0.5
        p.save(); p.translate(cx, cy); p.rotate(self._rings[0] * 0.25)
        for deg in range(0, 360, 3):
            major = deg % 30 == 0
            ln = 10 if major else 4
            p.setPen(QPen(qcol(base, 170 if major else 70), 1.4 if major else 1))
            rad = math.radians(deg)
            p.drawLine(QPointF(R0 * math.cos(rad), R0 * math.sin(rad)),
                       QPointF((R0 - ln) * math.cos(rad), (R0 - ln) * math.sin(rad)))
        p.restore()

        # thin full circle + sweeping scanner arcs
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(qcol(base, 60), 1))
        p.drawEllipse(ring_rect(fw * 0.465))
        sweep = QPen(qcol(sc, 230), 3); sweep.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(sweep)
        p.drawArc(ring_rect(fw * 0.465), int(self._scan * 16), int((60 + 40 * level) * 16))
        p.setPen(QPen(qcol(C.ACC, 150), 2))
        p.drawArc(ring_rect(fw * 0.445), int(self._scan2 * 16), int(38 * 16))

        # segmented ring (24 blocks) — segments light up with energy
        r_seg = fw * 0.40
        segs = 24
        pen = QPen(qcol(base, 90), fw * 0.028); pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        for i in range(segs):
            a0 = self._rings[1] + i * (360 / segs)
            lit = (math.sin(t * 0.05 + i * 0.7) + 1) / 2
            alpha = int(40 + 150 * lit * (0.35 + 0.65 * level))
            pen.setColor(qcol(sc if lit > 0.6 else base, alpha))
            p.setPen(pen)
            p.drawArc(ring_rect(r_seg), int(a0 * 16), int((360 / segs - 4) * 16))

        # counter-rotating arcs
        for idx, (fr, wdt, arc_l, gap, alpha) in enumerate(
            [(0.345, 2, 100, 20, 200), (0.315, 1.2, 40, 50, 140)]):
            rr = fw * fr
            ang = self._rings[2 if idx == 0 else 0] * (1 if idx == 0 else -1.4)
            pn = QPen(qcol(base, alpha), wdt); pn.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pn)
            a = ang
            while a < ang + 360:
                p.drawArc(ring_rect(rr), int(a * 16), int(arc_l * 16))
                a += arc_l + gap

        # radial audio spectrum around the core
        n = 72
        r_in = fw * 0.215
        for i in range(n):
            ang = i * (2 * math.pi / n) - math.pi / 2
            if self.muted:
                amp = 0.04
            elif self.speaking:
                amp = 0.25 + 0.75 * abs(math.sin(t * 0.21 + i * 1.3)) * random.uniform(0.55, 1.0)
            elif self.state in ("THINKING", "PROCESSING"):
                amp = 0.15 + 0.35 * max(0.0, math.sin(t * 0.12 - i * 0.35))
            else:
                amp = 0.10 + 0.10 * (math.sin(t * 0.06 + i * 0.5) + 1)
            ln = fw * 0.075 * amp
            pn = QPen(qcol(sc, int(90 + 160 * amp)), 2.2); pn.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pn)
            p.drawLine(QPointF(cx + r_in * math.cos(ang), cy + r_in * math.sin(ang)),
                       QPointF(cx + (r_in + ln) * math.cos(ang), cy + (r_in + ln) * math.sin(ang)))

        # thinking: orbiting satellites
        if self.state in ("THINKING", "PROCESSING") and not self.muted:
            for k in range(3):
                a = math.radians(t * 3.2 + k * 120)
                rr = fw * 0.29
                p.setPen(Qt.PenStyle.NoPen); p.setBrush(QBrush(qcol(C.ACC2, 230)))
                p.drawEllipse(QPointF(cx + rr * math.cos(a), cy + rr * math.sin(a)), 3.5, 3.5)

        # pulse waves
        lim = fw * 0.5
        for pr in self._pulses:
            if pr < r_core:
                continue
            a = max(0, int(140 * (1.0 - pr / lim)))
            p.setPen(QPen(qcol(sc, a), 1.2)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(ring_rect(pr))

        # core glow
        glow = QRadialGradient(QPointF(cx, cy), r_core * 2.4)
        glow.setColorAt(0.0, qcol("#ffffff", int(170 + 80 * level)))
        glow.setColorAt(0.18, qcol(sc, int(200 + 55 * level)))
        glow.setColorAt(0.45, qcol(sc, int(60 + 70 * level)))
        glow.setColorAt(1.0, qcol(sc, 0))
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QBrush(glow))
        p.drawEllipse(ring_rect(r_core * 2.4))

        # core body
        if self._face_px:
            fsz = int(r_core * 2)
            path = QPainterPath(); path.addEllipse(ring_rect(r_core))
            p.save(); p.setClipPath(path)
            p.drawPixmap(int(cx - r_core), int(cy - r_core),
                         self._face_px.scaled(fsz, fsz, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                              Qt.TransformationMode.SmoothTransformation))
            p.restore()
        else:
            body = QRadialGradient(QPointF(cx, cy), r_core)
            body.setColorAt(0.0, qcol(C.DARK, 120))
            body.setColorAt(0.85, qcol(C.PANEL, 235))
            body.setColorAt(1.0, qcol(sc, 255))
            p.setBrush(QBrush(body)); p.setPen(QPen(qcol(sc, 230), 2))
            p.drawEllipse(ring_rect(r_core))
            # reactor triangle
            tri = QPolygonF()
            rot = math.radians(self._rings[0] * 0.3)
            for k in range(3):
                a = rot + k * 2 * math.pi / 3 - math.pi / 2
                tri.append(QPointF(cx + r_core * 0.72 * math.cos(a), cy + r_core * 0.72 * math.sin(a)))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(qcol(sc, 150), 1.4))
            p.drawPolygon(tri)
            p.setPen(QPen(qcol(C.WHITE, 235), 1))
            f = _hud_font(max(8, int(r_core * 0.22)), QFont.Weight.Bold)
            f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 125)
            p.setFont(f)
            p.drawText(ring_rect(r_core), Qt.AlignmentFlag.AlignCenter, self._assistant_name)

        # speaking particles
        for pt in self._particles:
            a = max(0, min(255, int(pt[4] * 255)))
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(QBrush(qcol(sc, a)))
            p.drawEllipse(QPointF(pt[0], pt[1]), 2.0, 2.0)

        # corner brackets (inside the widget, never clipped)
        bl, m = 22, 10
        p.setPen(QPen(qcol(base, 150), 2))
        for bx, by, dx, dy in [(m, m, 1, 1), (W - m, m, -1, 1), (m, H - m, 1, -1), (W - m, H - m, -1, -1)]:
            p.drawLine(QPointF(bx, by), QPointF(bx + dx * bl, by))
            p.drawLine(QPointF(bx, by), QPointF(bx, by + dy * bl))

        # status pill
        labels = {"THINKING": "THINKING", "PROCESSING": "PROCESSING", "LISTENING": "LISTENING"}
        if self.muted:
            txt = "MUTED"
        elif self.speaking:
            txt = "SPEAKING"
        else:
            txt = labels.get(self.state, self.state)
        f = _hud_font(10, QFont.Weight.Bold)
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 130)
        p.setFont(f)
        tw = p.fontMetrics().horizontalAdvance(txt) + 46
        py = avail_h + 6
        pill = QRectF(cx - tw / 2, py, tw, 28)
        p.setPen(QPen(qcol(sc, 170), 1)); p.setBrush(QBrush(qcol(sc, 26)))
        p.drawRoundedRect(pill, 14, 14)
        dot_a = 255 if self._blink or self.speaking else 110
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QBrush(qcol(sc, dot_a)))
        p.drawEllipse(QPointF(pill.left() + 17, pill.center().y()), 4, 4)
        p.setPen(QPen(qcol(sc), 1))
        p.drawText(pill.adjusted(22, 0, 0, 0), Qt.AlignmentFlag.AlignCenter, txt)

        # hint line
        p.setFont(_hud_font(8))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        hint = "Speak naturally  ·  Ctrl+K commands  ·  Esc interrupt  ·  F4 mute"
        if p.fontMetrics().horizontalAdvance(hint) > W - 60:
            hint = "Ctrl+K commands  ·  Esc  ·  F4"
        p.drawText(QRectF(0, py + 34, W, 18), Qt.AlignmentFlag.AlignCenter, hint)
        p.end()


class MetricBar(QWidget):
    """Small labeled horizontal progress bar (footer strip: CPU/RAM/etc.) —
    a dumb display widget, just call set_value(pct, text) to update it."""

    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0.0       # 0–100
        self._text  = "--"
        self.setFixedHeight(38)
        self.setMinimumWidth(80)

    def set_value(self, pct: float, text: str):
        self._value = max(0.0, min(100.0, pct))
        self._text  = text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        p.setBrush(QBrush(qcol(C.PANEL2)))
        p.setPen(QPen(qcol(C.BORDER_A), 1))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 4, 4)

        bar_h   = 4
        bar_y   = H - bar_h - 5
        bar_w   = W - 12
        bar_x   = 6
        fill_w  = int(bar_w * self._value / 100)

        p.setBrush(QBrush(qcol(C.BAR_BG)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2, 2)

        if self._value > 85:
            bar_col = qcol(C.RED)
        elif self._value > 65:
            bar_col = qcol(C.ACC)
        else:
            bar_col = qcol(self._color)

        if fill_w > 0:
            p.setBrush(QBrush(bar_col))
            p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2, 2)

        p.setFont(_hud_font(7, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(8, 5, 50, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

        p.setFont(_hud_font(9, QFont.Weight.Bold))
        p.setPen(QPen(bar_col if self._text != "--" else qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(0, 4, W - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._text)

class LogWidget(QTextEdit):
    """Scrolling conversation transcript, with a typewriter effect for new
    lines. append_log() is the thread-safe public entry point — it just
    emits _sig, which is connected to the actual (main-thread-only) text
    insertion logic, since main.py's background thread is what calls
    append_log() whenever JARVIS or the user says something."""
    _sig = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(_hud_font(9))
        self.setStyleSheet(f"""
            QTextEdit {{
                background: {C.PANEL};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 10px;
                padding: 6px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG};
                width: 8px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B};
                border-radius: 10px;
                min-height: 20px;
            }}
        """)
        self._queue: list[str] = []
        self._typing  = False
        self._text    = ""
        self._pos     = 0
        self._tag     = "sys"
        self._ai_name_lc = "jarvis"   # updated when assistant name changes
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._sig.connect(self._enqueue)

    def append_log(self, text: str):
        self._sig.emit(text)

    def _enqueue(self, text: str):
        self._queue.append(text)
        if not self._typing:
            self._next()

    def clear_log(self):
        self._queue.clear()
        self._tmr.stop()
        self._typing = False
        self.clear()

    def _next(self):
        if not self._queue:
            self._typing = False
            return
        self._typing = True
        raw = self._queue.pop(0)
        tl = raw.lower()
        _ai_pfx = f"{self._ai_name_lc}:"
        speaker = "SYSTEM"
        body = raw
        if tl.startswith("you:"):
            self._tag, speaker, body = "you", "YOU", raw[4:]
        elif tl.startswith(_ai_pfx):
            self._tag, speaker, body = "ai", self._ai_name_lc.upper(), raw[len(_ai_pfx):]
        elif tl.startswith("jarvis:"):
            self._tag, speaker, body = "ai", "JARVIS", raw[7:]
        elif tl.startswith("file:"):
            self._tag, speaker, body = "file", "FILE", raw[5:]
        elif re.match(r"^(err|error)\b", tl) or re.search(r"\b(error|failed|exception)\b", tl):
            self._tag, speaker = "err", "ALERT"
        else:
            self._tag = "sys"
            if tl.startswith("sys:"):
                body = raw[4:]
        self._text = body.strip()
        self._pos = 0
        # speaker header line — dim, small caps, timestamp
        cur = self.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        if not self.document().isEmpty():
            cur.insertBlock()
        hf = cur.charFormat()
        hf.setForeground(QBrush(qcol(self._color(), 150)))
        hf.setFontPointSize(7)
        hf.setFontWeight(QFont.Weight.Bold)
        cur.insertText(f"{speaker}  ·  {time.strftime('%H:%M')}\n", hf)
        self.setTextCursor(cur)
        # long replies type faster so the log never lags behind speech
        self._chunk = max(1, len(self._text) // 90)
        self._tmr.start(8)

    def _color(self) -> QColor:
        return {
            "you":  qcol(C.WHITE),
            "ai":   qcol(C.PRI),
            "err":  qcol(C.RED),
            "file": qcol(C.GREEN),
            "sys":  qcol(C.ACC2),
        }.get(self._tag, qcol(C.TEXT))

    def _step(self):
        if self._pos < len(self._text):
            chunk = self._text[self._pos:self._pos + self._chunk]
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            fmt = cur.charFormat()
            fmt.setForeground(QBrush(self._color()))
            fmt.setFontPointSize(9)
            fmt.setFontWeight(QFont.Weight.Normal)
            cur.insertText(chunk, fmt)
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            self._pos += len(chunk)
        else:
            self._tmr.stop()
            self.ensureCursorVisible()
            QTimer.singleShot(20, self._next)

_FILE_ICONS = {
    "image":   ("🖼", "#00d4ff"), "video":   ("🎬", "#ff6b00"),
    "audio":   ("🎵", "#cc44ff"), "pdf":     ("📄", "#ff4444"),
    "word":    ("📝", "#4488ff"), "excel":   ("📊", "#44bb44"),
    "code":    ("💻", "#ffcc00"), "archive": ("📦", "#ff8844"),
    "pptx":    ("📊", "#ff6622"), "text":    ("📃", "#aaaaaa"),
    "data":    ("🔧", "#88ddff"), "unknown": ("📎", "#888888"),
}
_EXT_TO_CAT = {
    **dict.fromkeys(["jpg","jpeg","png","gif","webp","bmp","tiff","svg","ico"], "image"),
    **dict.fromkeys(["mp4","avi","mov","mkv","wmv","flv","webm","m4v"],         "video"),
    **dict.fromkeys(["mp3","wav","ogg","m4a","aac","flac","wma","opus"],        "audio"),
    **dict.fromkeys(["pdf"],                                                     "pdf"),
    **dict.fromkeys(["doc","docx"],                                              "word"),
    **dict.fromkeys(["xls","xlsx","ods"],                                        "excel"),
    **dict.fromkeys(["ppt","pptx"],                                              "pptx"),
    **dict.fromkeys(["py","js","ts","jsx","tsx","html","css","java","c","cpp",
                     "cs","go","rs","rb","php","swift","kt","sh","sql","lua"],   "code"),
    **dict.fromkeys(["zip","rar","tar","gz","7z","bz2","xz"],                   "archive"),
    **dict.fromkeys(["txt","md","rst","log"],                                    "text"),
    **dict.fromkeys(["csv","tsv","json","xml"],                                  "data"),
}

def _file_category(path: Path) -> str:
    return _EXT_TO_CAT.get(path.suffix.lower().lstrip("."), "unknown")

def _fmt_size(size: int) -> str:
    if   size < 1024:    return f"{size} B"
    elif size < 1024**2: return f"{size/1024:.1f} KB"
    elif size < 1024**3: return f"{size/1024**2:.1f} MB"
    else:                return f"{size/1024**3:.1f} GB"


class FileDropZone(QWidget):
    """Drag-and-drop target for attaching a file to the conversation (fed
    to file_processor / code_helper tools) — animated dashed border via
    _DropCanvas, emits file_selected(path) on drop or on click-to-browse."""
    file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(100)
        self._current_file: str | None = None
        self._hovering  = False
        self._drag_over = False
        self._dash_offset = 0.0
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._animate)
        self._anim_tmr.start(40)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._canvas = _DropCanvas(self)
        layout.addWidget(self._canvas)

    def _animate(self):
        self._dash_offset = (self._dash_offset + 0.8) % 20
        self._canvas.update()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True; self._canvas.update()

    def dragLeaveEvent(self, e):
        self._drag_over = False; self._canvas.update()

    def dropEvent(self, e: QDropEvent):
        self._drag_over = False
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if Path(path).is_file():
                self._set_file(path)
        self._canvas.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._browse()

    def enterEvent(self, e):
        self._hovering = True; self._canvas.update()

    def leaveEvent(self, e):
        self._hovering = False; self._canvas.update()

    def current_file(self) -> str | None:
        return self._current_file

    def clear_file(self):
        self._current_file = None; self._canvas.update()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a file for JARVIS", str(Path.home()),
            "All Files (*.*);;"
            "Images (*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx);;"
            "Data (*.csv *.xlsx *.json *.xml);;"
            "Code (*.py *.js *.ts *.html *.css *.java *.cpp *.go);;"
            "Audio (*.mp3 *.wav *.ogg *.m4a *.aac *.flac);;"
            "Video (*.mp4 *.avi *.mov *.mkv *.wmv *.webm);;"
            "Archives (*.zip *.rar *.tar *.gz *.7z)",
        )
        if path:
            self._set_file(path)

    def _set_file(self, path: str):
        self._current_file = path
        self._canvas.update()
        self.file_selected.emit(path)


class _DropCanvas(QWidget):
    """Just the painted contents of FileDropZone — split out so the parent
    widget can own drag/drop event handling while this owns paintEvent."""
    def __init__(self, zone: FileDropZone):
        super().__init__(zone)
        self._z = zone

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        z    = self._z
        W, H = self.width(), self.height()
        pad  = 6
        rect = QRectF(pad, pad, W - pad * 2, H - pad * 2)

        bg_col = qcol("#001a24" if z._drag_over else ("#001218" if z._hovering else C.PANEL))
        p.setBrush(QBrush(bg_col)); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   border_col = qcol(C.GREEN, 200)
        elif z._drag_over:    border_col = qcol(C.PRI, 230)
        elif z._hovering:     border_col = qcol(C.BORDER_B, 200)
        else:                 border_col = qcol(C.BORDER, 160)

        pen = QPen(border_col, 1.5, Qt.PenStyle.DashLine)
        pen.setDashOffset(z._dash_offset)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   self._paint_file(p, W, H)
        elif z._drag_over:    self._paint_drag_over(p, W, H)
        else:                 self._paint_idle(p, W, H, z._hovering)

    def _paint_idle(self, p, W, H, hover):
        cx, cy = W / 2, H / 2
        col = qcol(C.PRI_DIM if not hover else C.PRI)
        p.setPen(QPen(col, 2)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx, cy - 14), QPointF(cx, cy + 4))
        p.drawLine(QPointF(cx - 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx + 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx - 14, cy + 4), QPointF(cx + 14, cy + 4))
        p.setFont(_hud_font(8))
        p.setPen(QPen(qcol(C.PRI_DIM if not hover else C.TEXT), 1))
        p.drawText(QRectF(0, cy + 8, W, 16), Qt.AlignmentFlag.AlignCenter,
                   "Drop file here  or  Click to Browse")
        p.setFont(_hud_font(7))
        p.setPen(QPen(qcol("#1a4a5a"), 1))
        p.drawText(QRectF(0, cy + 24, W, 14), Qt.AlignmentFlag.AlignCenter,
                   "Images · Video · Audio · PDF · Docs · Code · Data")

    def _paint_drag_over(self, p, W, H):
        cx, cy = W / 2, H / 2
        p.setFont(_hud_font(20))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy - 24, W, 32), Qt.AlignmentFlag.AlignCenter, "⬇")
        p.setFont(_hud_font(8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy + 12, W, 16), Qt.AlignmentFlag.AlignCenter, "Release to load")

    def _paint_file(self, p, W, H):
        path = Path(self._z._current_file)
        cat  = _file_category(path)
        icon, icon_col = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size_str = _fmt_size(path.stat().st_size)
        ext_str  = path.suffix.upper().lstrip(".") or "FILE"

        block_x, block_w = 10, 60
        p.setFont(QFont("Segoe UI Emoji", 22) if _OS == "Windows" else QFont("Arial", 22))
        p.setPen(QPen(qcol(icon_col), 1))
        p.drawText(QRectF(block_x, 0, block_w, H), Qt.AlignmentFlag.AlignCenter, icon)

        tx = block_x + block_w + 6
        tw = W - tx - 38

        p.setFont(_hud_font(8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.WHITE), 1))
        name = path.name if len(path.name) <= 34 else path.name[:31] + "..."
        p.drawText(QRectF(tx, H * 0.18, tw, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        p.setFont(_hud_font(7))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(tx, H * 0.18 + 18, tw, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"{ext_str}  ·  {size_str}")

        p.setFont(_hud_font(6))
        p.setPen(QPen(qcol("#1e5c6a"), 1))
        par = str(path.parent)
        if len(par) > 42: par = "…" + par[-41:]
        p.drawText(QRectF(tx, H * 0.18 + 34, tw, 12),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, par)

        p.setFont(_hud_font(9, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.RED, 180), 1))
        p.drawText(QRectF(W - 34, 0, 28, H), Qt.AlignmentFlag.AlignCenter, "✕")

    def mousePressEvent(self, e):
        z = self._z
        if z._current_file and e.pos().x() > self.width() - 34:
            z.clear_file()
        else:
            z.mousePressEvent(e)


class _CameraPreview(QWidget):
    """Floating overlay that briefly shows what the camera captured."""

    _W, _H = 244, 188

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            _CameraPreview {{
                background: rgba(0, 6, 10, 242);
                border: 1px solid {C.PRI};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 5, 6, 6)
        lay.setSpacing(4)

        hdr = QHBoxLayout()
        title = QLabel("◈  VISUAL INPUT")
        title.setFont(_hud_font(7, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(title)
        hdr.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(16, 16)
        close_btn.setFont(_hud_font(8))
        close_btn.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent; border: none;"
        )
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.hide)
        hdr.addWidget(close_btn)
        lay.addLayout(hdr)

        self._img_lbl = QLabel()
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setStyleSheet("background: transparent;")
        lay.addWidget(self._img_lbl)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

        self.hide()

    def show_frame(self, img_bytes: bytes) -> None:
        px = QPixmap()
        px.loadFromData(img_bytes)
        if not px.isNull():
            max_w = self._W - 12
            scaled = px.scaled(
                max_w, 160,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._img_lbl.setPixmap(scaled)
            self._img_lbl.setFixedSize(scaled.width(), scaled.height())
            self.adjustSize()
        self.show()
        self.raise_()
        self._timer.start(6_000)   # auto-dismiss after 6 s


class RemindersPanel(QWidget):
    """Always-visible live section listing open Reminders (task_manager)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 10px;"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 8)
        lay.setSpacing(4)

        hdr = QLabel("☑ REMINDERS")
        hdr.setFont(_hud_font(7, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent; "
                           f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 4px;")
        lay.addWidget(hdr)

        self._list = QListWidget()
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setWordWrap(True)
        self._list.setFont(_hud_font(8))
        self._list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._list.setStyleSheet(f"""
            QListWidget {{
                background: transparent; color: {C.TEXT}; border: none;
            }}
            QListWidget::item {{ padding: 3px 2px; border-bottom: 1px solid {C.BORDER}; }}
            QScrollBar:vertical {{ background: {C.BG}; width: 6px; border: none; }}
            QScrollBar::handle:vertical {{ background: {C.BORDER_B}; border-radius: 3px; min-height: 16px; }}
        """)
        lay.addWidget(self._list, stretch=1)
        self.set_tasks([])

    def set_tasks(self, tasks: list[dict]):
        self._list.clear()
        if not tasks:
            item = QListWidgetItem("No open reminders.")
            item.setForeground(QColor(C.TEXT_DIM))
            self._list.addItem(item)
            return
        for t in tasks:
            due = f"  —  {t['due']}" if t.get("due") else ""
            item = QListWidgetItem(f"• {t['title']}{due}")
            item.setForeground(QColor(C.TEXT if not t.get("due") else C.ACC2))
            self._list.addItem(item)


class CalendarPanel(QWidget):
    """Always-visible live section listing upcoming Calendar events."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 10px;"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 8)
        lay.setSpacing(4)

        hdr = QLabel("▤ CALENDAR")
        hdr.setFont(_hud_font(7, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent; "
                           f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 4px;")
        lay.addWidget(hdr)

        self._list = QListWidget()
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setWordWrap(True)
        self._list.setFont(_hud_font(8))
        self._list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._list.setStyleSheet(f"""
            QListWidget {{
                background: transparent; color: {C.TEXT}; border: none;
            }}
            QListWidget::item {{ padding: 3px 2px; border-bottom: 1px solid {C.BORDER}; }}
            QScrollBar:vertical {{ background: {C.BG}; width: 6px; border: none; }}
            QScrollBar::handle:vertical {{ background: {C.BORDER_B}; border-radius: 3px; min-height: 16px; }}
        """)
        lay.addWidget(self._list, stretch=1)
        self.set_events([])

    def set_events(self, events: list[dict]):
        self._list.clear()
        if not events:
            item = QListWidgetItem("No upcoming events.")
            item.setForeground(QColor(C.TEXT_DIM))
            self._list.addItem(item)
            return
        for ev in events:
            when = ev["start"].strftime("%a %b %d, %I:%M %p").replace(" 0", " ")
            item = QListWidgetItem(f"• {ev['title']}\n   {when}")
            item.setForeground(QColor(C.ACC2))
            self._list.addItem(item)


class EarthGlobe(QWidget):
    """
    Always-visible section: a slowly rotating wireframe globe (holographic
    style, matching the HUD's other sci-fi vector graphics in HudCanvas)
    with a glowing marker at the user's location. The marker naturally
    appears/disappears as the globe rotates it in and out of view — no
    click/drag interaction here, this is ambient decoration + a location
    readout, not a data browser (that's what Reminders/Calendar are for).

    Location is resolved once via set_location() (called from a background
    thread — see MainWindow._fetch_today_slow — since the IP-geolocation
    fallback in actions/location.py is a network call).
    """

    _TILT_DEG = 23.4  # nod to Earth's real axial tilt — purely aesthetic,
                       # makes both latitude and longitude lines read as
                       # curved ellipses instead of flat lines/circles

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 10px;"
        )
        self.setMinimumSize(140, 140)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 8)
        lay.setSpacing(4)

        hdr_row = QHBoxLayout()
        hdr = QLabel("◈ LOCATION")
        hdr.setFont(_hud_font(7, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr_row.addWidget(hdr)
        hdr_row.addStretch()

        expand_btn = QPushButton("⛶ EXPAND")
        expand_btn.setFixedHeight(16)
        expand_btn.setFont(_hud_font(6, QFont.Weight.Bold))
        expand_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        expand_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 6px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}
        """)
        self._on_expand = None
        expand_btn.clicked.connect(lambda: self._on_expand() if self._on_expand else None)
        hdr_row.addWidget(expand_btn)

        hdr_wrap = QWidget()
        hdr_wrap.setLayout(hdr_row)
        hdr_wrap.setStyleSheet(f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 4px;")
        lay.addWidget(hdr_wrap)

        self._canvas = _EarthCanvas(theme="cyan")
        lay.addWidget(self._canvas, stretch=1)

    def set_location(self, lat: float | None, lon: float | None, city: str = ""):
        self._canvas.set_location(lat, lon, city)

    def set_expand_callback(self, cb):
        """cb() — called when the ⛶ EXPAND button is clicked."""
        self._on_expand = cb


_EARTH_TEXTURE_CACHE: dict = {}


def _load_earth_texture():
    """
    Loads a user-supplied equirectangular Earth image for photorealistic
    globe texture-mapping (grayscale — the sphere is lit/tinted per-theme
    at render time, see _EarthCanvas). Looked up from, in order:
      1. "earth_texture_path" in config/api_keys.json, if set
      2. assets/earth_texture.jpg next to this file

    Returns a 2D numpy uint8 array, or None if numpy/PIL aren't available,
    no image is configured, or it can't be read — callers should treat None
    as "fall back to the procedural vector globe", not an error. A free
    public-domain option is NASA's "Blue Marble" imagery (search
    visibleearth.nasa.gov or Wikimedia Commons) — any equirectangular world
    map image works. Cached in-process; restart JARVIS after replacing the
    file.
    """
    if "result" in _EARTH_TEXTURE_CACHE:
        return _EARTH_TEXTURE_CACHE["result"]

    result = None
    try:
        if np is None:
            raise RuntimeError("numpy not installed")
        from PIL import Image

        cfg = _read_full_config()
        raw_path = (cfg.get("earth_texture_path") or "").strip()
        path = Path(raw_path).expanduser() if raw_path else (BASE_DIR / "assets" / "earth_texture.jpg")

        if path.exists():
            img = Image.open(path).convert("L")
            # Downsized for a fast per-frame texture lookup — plenty of
            # detail for a HUD-sized globe, keeps the array small.
            img = img.resize((720, 360), Image.LANCZOS)
            result = np.asarray(img, dtype=np.uint8)
        else:
            print(f"[EarthGlobe] No earth texture found at {path} — using procedural globe.")
    except Exception as e:
        print(f"[EarthGlobe] Texture load skipped: {e}")
        result = None

    _EARTH_TEXTURE_CACHE["result"] = result
    return result


# Rough, stylized continent silhouettes (lat, lon) — hand-approximated, not
# real coastline data (no accurate public-domain geo dataset was fetched for
# this). At the small size this globe renders at, they only need to read as
# "landmasses" to break up the shaded sphere, not be geographically precise.
_CONTINENTS: list[list[tuple[float, float]]] = [
    # North America
    [(70, -160), (70, -90), (55, -60), (45, -52), (25, -80), (15, -95),
     (20, -105), (32, -117), (48, -125), (60, -140), (70, -160)],
    # South America
    [(12, -77), (5, -60), (-5, -35), (-20, -40), (-35, -58), (-55, -68),
     (-45, -75), (-20, -80), (-2, -80), (12, -77)],
    # Africa
    [(37, -10), (32, 10), (15, 25), (0, 35), (-15, 40), (-35, 20),
     (-30, 15), (-20, 12), (0, 9), (15, -15), (30, -10), (37, -10)],
    # Eurasia
    [(70, 30), (70, 90), (60, 140), (45, 140), (35, 120), (20, 100),
     (10, 80), (15, 60), (25, 50), (35, 35), (45, 25), (55, 10),
     (60, 5), (65, 20), (70, 30)],
    # Australia
    [(-12, 130), (-12, 145), (-25, 153), (-38, 145), (-35, 130),
     (-20, 113), (-12, 130)],
]


class _EarthCanvas(QWidget):
    """
    The actual painted globe — kept separate from EarthGlobe's header/frame
    chrome so EarthGlobe can wrap it like the other live panels.

    Composition modeled on a "broadcast HUD" globe reference (shaded 3D
    sphere, lat/lon grid, radar rings, a tilted orbit ring, and glowing
    location markers with leader lines to text labels). theme="cyan" (the
    default, used by the small always-visible live panel) recolors it into
    JARVIS's existing palette; theme="mono" (used by the full-screen Global
    Ops page) matches the reference's grayscale sphere + red markers
    literally.
    """

    _TILT_DEG = 23.4  # nod to Earth's real axial tilt — purely aesthetic,
                       # makes both latitude and longitude lines read as
                       # curved ellipses instead of flat lines/circles

    def __init__(self, parent=None, theme: str = "cyan"):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        if theme == "mono":
            self._c_bg      = "#000000"
            self._c_line    = "#c9d3d6"
            self._c_hi      = "#f2f5f6"
            self._c_mid     = "#8a9296"
            self._c_edge    = "#000000"
            self._c_land    = "#9aa2a5"
            self._c_marker  = C.MUTED_C  # reuse the app's existing red
            self._c_label   = C.MUTED_C
            self._tex_tint  = (1.0, 1.0, 1.0)   # literal grayscale, like the reference
        else:
            self._c_bg      = C.DARK
            self._c_line    = C.PRI
            self._c_hi      = C.TEXT
            self._c_mid     = C.PRI_DIM
            self._c_edge    = C.DARK
            self._c_land    = C.PRI_DIM
            self._c_marker  = C.ACC2
            self._c_label   = C.ACC2
            self._tex_tint  = (0.55, 0.92, 1.0)  # cyan-tinted, to match the rest of the HUD

        self._theta   = 0.0   # current rotation angle, degrees
        self._speed   = 0.35  # degrees/tick — slow ambient spin
        self._pulse   = 0.0
        self._radar   = 0.0
        self._markers: list[dict] = []   # [{"lat","lon","label"}, ...]

        # Real-texture rendering (see _load_earth_texture / _ensure_sphere_maps)
        # — None means "no image supplied", so paintEvent falls back to the
        # procedural gradient+polygon globe below.
        self._texture = _load_earth_texture()
        self._maps_key = None
        self._mask = self._lat_map = self._facing_lon_map = self._shade = None

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(33)  # ~30fps — plenty smooth for a slow ambient spin

    def set_location(self, lat, lon, city: str = ""):
        """Backward-compat single-marker convenience wrapper."""
        if lat is None or lon is None:
            self.set_markers([])
        else:
            self.set_markers([{"lat": lat, "lon": lon, "label": city or "YOUR LOCATION"}])

    def set_markers(self, markers: list[dict]):
        self._markers = markers or []
        self.update()

    def _step(self):
        self._theta = (self._theta + self._speed) % 360
        self._pulse = (self._pulse + 3.0) % 360
        self._radar = (self._radar + 0.6) % 360
        self.update()

    @staticmethod
    def _project(lat_deg: float, lon_deg: float, theta_deg: float, tilt_deg: float):
        """Unit-sphere point after spin (theta, about the vertical axis) and
        a fixed viewing tilt — returns (x, y, z), z>0 meaning facing the viewer."""
        lat = math.radians(lat_deg)
        lon = math.radians(lon_deg + theta_deg)
        x = math.cos(lat) * math.sin(lon)
        y = math.sin(lat)
        z = math.cos(lat) * math.cos(lon)
        t = math.radians(tilt_deg)
        y2 = y * math.cos(t) - z * math.sin(t)
        z2 = y * math.sin(t) + z * math.cos(t)
        return x, y2, z2

    def _draw_polyline(self, p: QPainter, pts, closed: bool, boost: float = 1.0):
        n = len(pts)
        if n < 2:
            return
        for i in range(n - (0 if closed else 1)):
            x1, y1, z1 = pts[i]
            x2, y2, z2 = pts[(i + 1) % n]
            front = (z1 + z2) / 2 > 0
            a = max(0, min(255, int((150 if front else 25) * boost)))
            p.setPen(QPen(qcol(self._c_line, a), 1.1 if front else 0.8))
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    def _ensure_sphere_maps(self, W: int, H: int, cx: float, cy: float, R: float):
        """
        Precomputes, once per widget size (cached — cheap to call every
        frame, only recomputes on resize), the per-pixel (lat, "current-
        facing" longitude, lighting) for every pixel inside the sphere's
        circle. These don't depend on the current rotation angle — only
        which *texture* longitude ends up at a given facing-longitude does
        — so the per-frame cost in paintEvent is just a texture lookup, not
        re-deriving this geometry. See the class docstring's design notes.
        """
        key = (int(W), int(H), round(R, 2))
        if self._maps_key == key:
            return
        self._maps_key = key

        ys, xs = np.mgrid[0:int(H), 0:int(W)]
        x  = (xs - cx) / R
        y2 = -(ys - cy) / R  # screen-space y after tilt (sy = cy - y2*R)
        r2 = x * x + y2 * y2
        mask = r2 <= 1.0
        z2 = np.sqrt(np.clip(1.0 - np.clip(r2, 0, 1), 0, 1))  # front hemisphere

        t = math.radians(self._TILT_DEG)
        ct, st = math.cos(t), math.sin(t)
        # invert the fixed viewing tilt to recover pre-tilt (y, z)
        y = y2 * ct + z2 * st
        z = -y2 * st + z2 * ct

        lat = np.degrees(np.arcsin(np.clip(y, -1, 1)))
        facing_lon = np.degrees(np.arctan2(x, z))

        # Fixed light direction in screen space (upper-left-ish) — this is
        # theta-independent too, since the sun doesn't rotate with the globe.
        lx, ly, lz = -0.35, 0.55, 0.75
        n = math.sqrt(lx * lx + ly * ly + lz * lz)
        lx, ly, lz = lx / n, ly / n, lz / n
        shade = x * lx + y2 * ly + z2 * lz
        shade = np.clip(shade, 0.14, 1.0)  # ambient floor so the dark side isn't pure black

        self._mask, self._lat_map, self._facing_lon_map, self._shade = mask, lat, facing_lon, shade

    def _render_texture_image(self, W: int, H: int) -> QImage:
        """Builds this frame's texture-mapped sphere as a QImage — the only
        per-frame cost is a rotated texture lookup + shading multiply, both
        vectorized via numpy."""
        tex_h, tex_w = self._texture.shape
        # facing_lon_map recovers (lon_deg + theta) per pixel; subtracting
        # theta recovers lon_deg itself. Standard equirectangular textures
        # put longitude 0 (Greenwich) at the HORIZONTAL CENTER of the image
        # (column tex_w/2), not at column 0 (which is the antimeridian,
        # ~180°) — so an extra +180 shift is needed before scaling to pixel
        # columns. Without it, every point is sampled from the opposite
        # side of the map from where it actually is (this was the bug where
        # a Florida marker rendered over Asia).
        lon = (self._facing_lon_map - self._theta + 180.0) % 360.0
        tx = (lon / 360.0 * tex_w).astype(np.int32) % tex_w
        ty = ((90.0 - self._lat_map) / 180.0 * tex_h).astype(np.int32)
        np.clip(ty, 0, tex_h - 1, out=ty)

        gray = self._texture[ty, tx].astype(np.float32)
        lit  = gray * self._shade

        rgba = np.zeros((H, W, 4), dtype=np.uint8)
        r, g, b = self._tex_tint
        rgba[..., 0] = np.clip(lit * r, 0, 255).astype(np.uint8)
        rgba[..., 1] = np.clip(lit * g, 0, 255).astype(np.uint8)
        rgba[..., 2] = np.clip(lit * b, 0, 255).astype(np.uint8)
        rgba[..., 3] = np.where(self._mask, 255, 0).astype(np.uint8)
        rgba = np.ascontiguousarray(rgba)

        # QImage copies from the bytes buffer here (Format_RGBA8888 expects
        # a plain bytes-like object), so there's no lifetime issue keeping
        # the numpy array itself alive after this method returns.
        return QImage(rgba.tobytes(), W, H, W * 4, QImage.Format.Format_RGBA8888)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), qcol(self._c_bg))

        W, H = self.width(), self.height()
        cx, cy = W / 2, H / 2
        R = min(W, H) * 0.36
        if R < 4:
            return

        theta, tilt = self._theta, self._TILT_DEG

        # ── outer radar rings (radar-scan dressing, echoes HudCanvas's
        # own pulse-ring style so this panel still feels like the same HUD) ──
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i, r_mul in enumerate((1.18, 1.38, 1.58)):
            rr = R * r_mul
            phase = (self._radar + i * 60) % 360
            a = max(0, int(70 * (0.4 + 0.6 * abs(math.sin(math.radians(phase))))))
            pen = QPen(qcol(self._c_line, a), 1.0)
            pen.setStyle(Qt.PenStyle.DotLine)
            p.setPen(pen)
            p.drawEllipse(QRectF(cx - rr, cy - rr, rr * 2, rr * 2))

        # ── tilted dashed orbit ring ──────────────────────────────────────
        p.save()
        p.translate(cx, cy)
        p.rotate(-18)
        orbit_pen = QPen(qcol(self._c_line, 70), 1.0)
        orbit_pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(orbit_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        orbit_w, orbit_h = R * 2.05, R * 0.9
        p.drawEllipse(QRectF(-orbit_w / 2, -orbit_h / 2, orbit_w, orbit_h))
        p.restore()

        # ── sphere body: real texture-mapped photo if the user supplied one
        # (actions/location-style config, see _load_earth_texture), else the
        # procedural gradient+polygon look ──────────────────────────────────
        if self._texture is not None and np is not None:
            self._ensure_sphere_maps(W, H, cx, cy, R)
            img = self._render_texture_image(int(W), int(H))
            p.drawImage(0, 0, img)
        else:
            grad = QRadialGradient(QPointF(cx - R * 0.35, cy - R * 0.35), R * 1.6)
            grad.setColorAt(0.0, qcol(self._c_hi, 90 if self._c_bg == "#000000" else 60))
            grad.setColorAt(0.45, qcol(self._c_mid, 75 if self._c_bg == "#000000" else 55))
            grad.setColorAt(1.0, qcol(self._c_edge, 235))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(grad))
            p.drawEllipse(QRectF(cx - R, cy - R, R * 2, R * 2))

        # clip everything "on" the sphere (continents + grid) to its disc
        clip = QPainterPath()
        clip.addEllipse(QRectF(cx - R, cy - R, R * 2, R * 2))
        p.setClipPath(clip)

        # ── continents (rough silhouettes, front-facing only) — only drawn
        # for the procedural fallback; a real texture already shows them ──
        if self._texture is None or np is None:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(qcol(self._c_land, 100)))
            for shape in _CONTINENTS:
                pts = [self._project(lat_deg, lon_deg, theta, tilt) for lat_deg, lon_deg in shape]
                if sum(z for _, _, z in pts) / len(pts) <= 0.05:
                    continue
                poly = QPolygonF([QPointF(cx + x * R, cy - y * R) for x, y, z in pts])
                p.drawPolygon(poly)

        # ── lat/lon grid — kept on top even over the real texture, for the
        # same HUD scan-grid-over-satellite-photo look as the reference ────
        for lat_deg in range(-60, 61, 30):
            pts = []
            for lon_deg in range(0, 361, 8):
                x, y, z = self._project(lat_deg, lon_deg, theta, tilt)
                pts.append((cx + x * R, cy - y * R, z))
            self._draw_polyline(p, pts, closed=True)

        for lon_deg in range(0, 360, 30):
            pts = []
            for lat_deg in range(-90, 91, 6):
                x, y, z = self._project(lat_deg, lon_deg, theta, tilt)
                pts.append((cx + x * R, cy - y * R, z))
            self._draw_polyline(p, pts, closed=False)

        pts = []
        for lon_deg in range(0, 361, 6):
            x, y, z = self._project(0, lon_deg, theta, tilt)
            pts.append((cx + x * R, cy - y * R, z))
        self._draw_polyline(p, pts, closed=True, boost=1.4)

        p.setClipping(False)

        # outer limb (crisp sphere silhouette on top of everything else)
        p.setPen(QPen(qcol(self._c_line, 130), 1.3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(cx - R, cy - R, R * 2, R * 2))

        # ── glowing markers + leader lines + labels ───────────────────────
        # only drawn while the globe's current rotation faces each one
        # toward the viewer — they naturally rotate away and back each cycle
        for idx, marker in enumerate(self._markers):
            lat, lon = marker.get("lat"), marker.get("lon")
            if lat is None or lon is None:
                continue
            x, y, z = self._project(lat, lon, theta, tilt)
            if z <= 0.03:
                continue
            mx, my = cx + x * R, cy - y * R
            near = min(1.0, z + 0.3)

            glow_r = 3.0 + 4.0 * (0.5 + 0.5 * math.sin(math.radians(self._pulse + idx * 90)))
            p.setPen(Qt.PenStyle.NoPen)
            for i in range(5):
                rr = glow_r * (2.2 - i * 0.35)
                a  = max(0, int(140 * near * (1 - i / 5)))
                p.setBrush(QBrush(qcol(self._c_marker, a)))
                p.drawEllipse(QPointF(mx, my), rr, rr)
            p.setBrush(QBrush(qcol("#ffffff", int(255 * near))))
            p.drawEllipse(QPointF(mx, my), 2.4, 2.4)

            # leader line + label, callout-style like the reference
            side = 1 if mx >= cx else -1
            up = -1 if idx % 2 == 0 else 1
            elbow = (mx + side * 16, my + up * 14)
            label_pos = (elbow[0] + side * 6, elbow[1])
            line_pen = QPen(qcol(self._c_label, int(200 * near)), 1.0)
            p.setPen(line_pen)
            p.drawLine(QPointF(mx, my), QPointF(*elbow))
            p.drawLine(QPointF(*elbow), QPointF(label_pos[0], label_pos[1]))

            label = (marker.get("label") or "").upper()
            p.setFont(_hud_font(7, QFont.Weight.Bold))
            text_w = 90
            text_x = label_pos[0] if side > 0 else label_pos[0] - text_w
            p.drawText(
                QRectF(text_x, label_pos[1] - 9, text_w, 14),
                Qt.AlignmentFlag.AlignLeft if side > 0 else Qt.AlignmentFlag.AlignRight,
                label,
            )

        if not self._markers:
            p.setPen(QPen(qcol(C.TEXT_DIM, 160)))
            p.setFont(_hud_font(7))
            p.drawText(QRectF(0, H - 16, W, 14), Qt.AlignmentFlag.AlignCenter, "LOCATING…")


def _theme_colors(theme: str) -> dict:
    """Shared palette lookup for the small decorative HUD widgets below."""
    if theme == "mono":
        return {"line": "#c9d3d6", "dim": "#5c6467", "accent": C.MUTED_C, "text": "#e8ecec"}
    return {"line": C.PRI, "dim": C.PRI_DIM, "accent": C.ACC2, "text": C.TEXT}


class _ArcGauge(QWidget):
    """
    Small circular percentage dial (270° sweep), styled like the reference's
    CPU/MEM/GPU/TMP-style readouts — driven by real system metrics via
    set_value(), not decorative fake numbers.
    """

    def __init__(self, label: str, theme: str = "mono", parent=None):
        super().__init__(parent)
        self._label = label
        self._value = 0.0
        self._c = _theme_colors(theme)
        self.setMinimumSize(64, 64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_value(self, value: float):
        self._value = max(0.0, min(100.0, value))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        cx, cy = W / 2, H / 2
        r = min(W, H) * 0.38
        rect = QRectF(cx - r, cy - r, r * 2, r * 2)

        start_a, span_a = 225, -270  # Qt angle units: 1/16 degree, applied below
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(qcol(self._c["dim"], 130), 3))
        p.drawArc(rect, start_a * 16, span_a * 16)

        val_span = int(span_a * (self._value / 100.0))
        p.setPen(QPen(qcol(self._c["accent"], 235), 3))
        p.drawArc(rect, start_a * 16, val_span * 16)

        p.setPen(QPen(qcol(self._c["text"], 255)))
        p.setFont(_hud_font(int(r * 0.5), QFont.Weight.Bold))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"{int(self._value)}")

        p.setPen(QPen(qcol(self._c["dim"], 220)))
        p.setFont(_hud_font(7))
        p.drawText(QRectF(0, H - 14, W, 12), Qt.AlignmentFlag.AlignCenter, self._label)


class _MiniWorldMap(QWidget):
    """
    Small flat (equirectangular) world map with a pulsing target reticle at
    a given lat/lon — the reference's country-outline-with-crosshair panel,
    generalized to a full world map (since JARVIS doesn't have per-country
    border data) and driven by the user's real resolved location.
    """

    def __init__(self, theme: str = "mono", parent=None):
        super().__init__(parent)
        self._c = _theme_colors(theme)
        self._lat: float | None = None
        self._lon: float | None = None
        self._label = ""
        self._pulse = 0.0
        self.setMinimumSize(90, 60)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(60)

    def set_target(self, lat, lon, label: str = ""):
        self._lat, self._lon, self._label = lat, lon, label
        self.update()

    def _step(self):
        self._pulse = (self._pulse + 4.0) % 360
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        p.fillRect(self.rect(), qcol("#000000" if self._c["accent"] == C.MUTED_C else C.DARK))

        def to_xy(lat, lon):
            return (lon + 180) / 360 * W, (90 - lat) / 180 * H

        # grid
        p.setPen(QPen(qcol(self._c["dim"], 60), 0.6))
        for lon in range(-180, 181, 30):
            x, _ = to_xy(0, lon)
            p.drawLine(QPointF(x, 0), QPointF(x, H))
        for lat in range(-60, 61, 30):
            _, y = to_xy(lat, 0)
            p.drawLine(QPointF(0, y), QPointF(W, y))

        # continents
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(qcol(self._c["line"], 70)))
        for shape in _CONTINENTS:
            poly = QPolygonF([QPointF(*to_xy(lat, lon)) for lat, lon in shape])
            p.drawPolygon(poly)

        p.setPen(QPen(qcol(self._c["dim"], 160), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(0.5, 0.5, W - 1, H - 1))

        if self._lat is not None and self._lon is not None:
            x, y = to_xy(self._lat, self._lon)
            rr = 3.0 + 3.0 * (0.5 + 0.5 * math.sin(math.radians(self._pulse)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(qcol(self._c["accent"], 90)))
            p.drawEllipse(QPointF(x, y), rr * 1.8, rr * 1.8)
            p.setBrush(QBrush(qcol(self._c["accent"], 255)))
            p.drawEllipse(QPointF(x, y), 2.2, 2.2)
            if self._label:
                p.setPen(QPen(qcol(self._c["accent"], 230)))
                p.setFont(_hud_font(6, QFont.Weight.Bold))
                p.drawText(QRectF(0, H - 11, W, 10), Qt.AlignmentFlag.AlignCenter,
                           self._label.upper())


class _BarEqualizer(QWidget):
    """Ambient animated bar-spectrum strip — pure visual dressing (no real
    data source fits this shape), matching the reference's bottom bar chart."""

    def __init__(self, bars: int = 48, theme: str = "mono", parent=None):
        super().__init__(parent)
        self._c = _theme_colors(theme)
        self._n = bars
        self._heights = [random.uniform(0.1, 0.4) for _ in range(bars)]
        self._targets  = list(self._heights)
        self._accent_idx = set(random.sample(range(bars), max(1, bars // 8)))
        self.setMinimumHeight(24)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(90)

    def _step(self):
        for i in range(self._n):
            if abs(self._heights[i] - self._targets[i]) < 0.02:
                self._targets[i] = random.uniform(0.05, 1.0)
            self._heights[i] += (self._targets[i] - self._heights[i]) * 0.35
        if random.random() < 0.1:
            self._accent_idx = set(random.sample(range(self._n), max(1, self._n // 8)))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        W, H = self.width(), self.height()
        gap = 2
        bw = max(1.0, (W - gap * (self._n - 1)) / self._n)
        for i, h in enumerate(self._heights):
            x = i * (bw + gap)
            bh = h * H
            color = self._c["accent"] if i in self._accent_idx else self._c["line"]
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(qcol(color, 200 if i in self._accent_idx else 130)))
            p.drawRect(QRectF(x, H - bh, bw, bh))


class _StatusGrid(QWidget):
    """Ambient animated status-block grid — decorative texture (a handful of
    cells randomly flash) matching the reference's small pixel/heatmap panel."""

    def __init__(self, cols: int = 10, rows: int = 4, theme: str = "mono", parent=None):
        super().__init__(parent)
        self._c = _theme_colors(theme)
        self._cols, self._rows = cols, rows
        self._active: set[tuple[int, int]] = set()
        self.setMinimumSize(80, 24)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(400)

    def _step(self):
        n = self._cols * self._rows
        self._active = {
            (random.randrange(self._cols), random.randrange(self._rows))
            for _ in range(max(1, n // 12))
        }
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        W, H = self.width(), self.height()
        gap = 2
        cw = (W - gap * (self._cols - 1)) / self._cols
        ch = (H - gap * (self._rows - 1)) / self._rows
        p.setPen(Qt.PenStyle.NoPen)
        for cx in range(self._cols):
            for cy in range(self._rows):
                active = (cx, cy) in self._active
                color = self._c["accent"] if active else self._c["dim"]
                p.setBrush(QBrush(qcol(color, 210 if active else 90)))
                p.drawRect(QRectF(cx * (cw + gap), cy * (ch + gap), cw, ch))


class _BarcodeStrip(QWidget):
    """Static-ish vertical barcode/signal-strip texture — decorative, a
    couple of stripes shift to the accent color occasionally."""

    def __init__(self, bars: int = 22, theme: str = "mono", parent=None):
        super().__init__(parent)
        self._c = _theme_colors(theme)
        self._n = bars
        self._widths = [random.uniform(0.3, 1.0) for _ in range(bars)]
        self._accent_idx = set(random.sample(range(bars), max(1, bars // 6)))
        self.setMinimumHeight(20)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(1400)

    def _step(self):
        self._accent_idx = set(random.sample(range(self._n), max(1, self._n // 6)))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        W, H = self.width(), self.height()
        gap = 1.5
        bw = max(1.0, (W - gap * (self._n - 1)) / self._n)
        p.setPen(Qt.PenStyle.NoPen)
        for i, wfrac in enumerate(self._widths):
            x = i * (bw + gap)
            h = wfrac * H
            color = self._c["accent"] if i in self._accent_idx else self._c["line"]
            p.setBrush(QBrush(qcol(color, 200 if i in self._accent_idx else 110)))
            p.drawRect(QRectF(x, H - h, bw, h))


class GlobalOpsOverlay(QWidget):
    """
    Full-screen "Global Ops" page — a grayscale/red broadcast-HUD dashboard
    styled after a reference the user shared, reached via the ⛶ EXPAND
    button on the small always-visible Location panel. This recreates the
    reference's *composition* (shaded globe with grid, side gauges, mini
    world map with a target reticle, bar-equalizer, status grid) rather
    than pixel-cloning that specific commercial stock template — the gauges
    are wired to JARVIS's real CPU/MEM/GPU/temperature readings and the map/
    globe markers to the user's real resolved location, while the bar
    equalizer / status grid / barcode strip are ambient decoration (nothing
    in the source reference maps to real JARVIS data for those).

    Shown as a full-window overlay over the HUD, like the existing Remote
    Control / Customize panels, with a close button (or Esc) to return to
    the normal dashboard.
    """

    closed  = pyqtSignal()
    _loc_sig = pyqtSignal(object)
    _weather_sig = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background: #000000;")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 16)
        root.setSpacing(10)

        # ── header ──────────────────────────────────────────────────────
        hdr = QHBoxLayout()
        tri = QLabel("▲")
        tri.setFont(_hud_font(13, QFont.Weight.Bold))
        tri.setStyleSheet(f"color: {C.MUTED_C}; background: transparent;")
        hdr.addWidget(tri)
        hdr.addStretch()

        banner = QLabel("GLOBAL OPERATIONS")
        banner.setFont(_hud_font(12, QFont.Weight.Bold))
        banner.setStyleSheet(f"""
            color: {C.MUTED_C}; background: #140000;
            border: 1px solid {C.MUTED_C}; border-radius: 8px;
            padding: 6px 22px;
        """)
        hdr.addWidget(banner)
        hdr.addStretch()

        close_btn = QPushButton("✕  CLOSE")
        close_btn.setFixedHeight(28)
        close_btn.setFont(_hud_font(8, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton {
                background: transparent; color: #9aa2a5;
                border: 1px solid #3a4144; border-radius: 8px; padding: 0 10px;
            }
            QPushButton:hover { color: #e8ecec; border-color: #6a7276; }
        """)
        close_btn.clicked.connect(self._do_close)
        hdr.addWidget(close_btn)
        root.addLayout(hdr)

        sub = QLabel("REAL-TIME SYSTEM & LOCATION STATUS")
        sub.setFont(_hud_font(7))
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setStyleSheet("color: #5c6467; background: transparent; letter-spacing: 2px;")
        root.addWidget(sub)

        # ── main body: left gauges/map | globe | right gauges/status ────
        body = QHBoxLayout()
        body.setSpacing(14)

        left = QVBoxLayout(); left.setSpacing(8)
        self._map = _MiniWorldMap(theme="mono")
        self._map.setFixedHeight(110)
        left.addWidget(self._map)

        self._weather_lbl = QLabel("WEATHER: —")
        self._weather_lbl.setFont(_hud_font(7, QFont.Weight.Bold))
        self._weather_lbl.setStyleSheet(f"color: {C.MUTED_C}; background: transparent; letter-spacing: 1px;")
        self._weather_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._weather_lbl.setWordWrap(True)
        left.addWidget(self._weather_lbl)

        gauge_row1 = QHBoxLayout()
        self._cpu_gauge = _ArcGauge("CPU", theme="mono")
        self._mem_gauge = _ArcGauge("MEM", theme="mono")
        gauge_row1.addWidget(self._cpu_gauge)
        gauge_row1.addWidget(self._mem_gauge)
        left.addLayout(gauge_row1)

        self._left_barcode = _BarcodeStrip(theme="mono")
        left.addWidget(self._left_barcode)
        left.addStretch()

        left_wrap = QWidget()
        left_wrap.setLayout(left)
        left_wrap.setFixedWidth(200)
        body.addWidget(left_wrap)

        self._globe = _EarthCanvas(theme="mono")
        body.addWidget(self._globe, stretch=1)

        right = QVBoxLayout(); right.setSpacing(8)
        gauge_row2 = QHBoxLayout()
        self._gpu_gauge = _ArcGauge("GPU", theme="mono")
        self._tmp_gauge = _ArcGauge("TMP", theme="mono")
        gauge_row2.addWidget(self._gpu_gauge)
        gauge_row2.addWidget(self._tmp_gauge)
        right.addLayout(gauge_row2)

        self._status_grid = _StatusGrid(theme="mono")
        self._status_grid.setFixedHeight(70)
        right.addWidget(self._status_grid)

        self._right_barcode = _BarcodeStrip(theme="mono")
        right.addWidget(self._right_barcode)
        right.addStretch()

        right_wrap = QWidget()
        right_wrap.setLayout(right)
        right_wrap.setFixedWidth(200)
        body.addWidget(right_wrap)

        root.addLayout(body, stretch=1)

        # ── bottom bar equalizer ──────────────────────────────────────────
        self._eq = _BarEqualizer(bars=64, theme="mono")
        self._eq.setFixedHeight(46)
        root.addWidget(self._eq)

        # ── live data ─────────────────────────────────────────────────────
        self._last_loc: dict | None = None
        self._loc_sig.connect(self._apply_location)
        self._weather_sig.connect(self._apply_weather)

        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self._refresh_metrics)
        self._metric_tmr.start(1500)
        self._refresh_metrics()

        self._loc_tmr = QTimer(self)
        self._loc_tmr.timeout.connect(self._refresh_location)
        self._loc_tmr.start(30000)
        QTimer.singleShot(50, self._refresh_location)

        self._weather_tmr = QTimer(self)
        self._weather_tmr.timeout.connect(self._refresh_weather)
        self._weather_tmr.start(600000)  # 10 min — matches actions.weather's own cache TTL

    def _refresh_metrics(self):
        snap = _metrics.snapshot()
        self._cpu_gauge.set_value(snap["cpu"])
        self._mem_gauge.set_value(snap["mem"])
        self._gpu_gauge.set_value(max(0.0, snap["gpu"]))
        tmp = snap["tmp"]
        self._tmp_gauge.set_value(min(100.0, max(0.0, tmp)) if tmp >= 0 else 0.0)

    def _refresh_location(self):
        """Kicks off a background fetch (actions.location does a network
        call on first resolution) — never touch widgets off the UI thread,
        hence the _loc_sig round-trip instead of updating directly."""
        def _fetch():
            try:
                from actions.location import get_location
                loc = get_location()
            except Exception:
                loc = None
            self._loc_sig.emit(loc)
        threading.Thread(target=_fetch, daemon=True).start()

    def _apply_location(self, loc):
        """Slot (main thread) — applies a location result from _refresh_location."""
        markers = [{"lat": 0.0, "lon": 0.0, "label": "GMT"}]
        if loc:
            markers.insert(0, {
                "lat": loc["lat"], "lon": loc["lon"],
                "label": loc.get("city") or "YOUR LOCATION",
            })
            self._map.set_target(loc["lat"], loc["lon"], loc.get("city") or "")
            new_loc = self._last_loc is None or (
                round(loc["lat"], 2) != round(self._last_loc.get("lat", 0.0), 2)
                or round(loc["lon"], 2) != round(self._last_loc.get("lon", 0.0), 2)
            )
            self._last_loc = loc
            if new_loc:
                self._refresh_weather()
        self._globe.set_markers(markers)

    def _refresh_weather(self):
        """Kicks off a background weather fetch for the last resolved
        location — same off-thread/signal pattern as _refresh_location."""
        loc = self._last_loc
        if not loc:
            return

        def _fetch():
            try:
                from actions.weather import get_current_weather
                w = get_current_weather(loc["lat"], loc["lon"])
            except Exception:
                w = None
            self._weather_sig.emit(w)
        threading.Thread(target=_fetch, daemon=True).start()

    def _apply_weather(self, w):
        """Slot (main thread) — applies a weather result from _refresh_weather."""
        if w:
            city = (self._last_loc or {}).get("city") or "LOCAL"
            self._weather_lbl.setText(
                f"{city.upper()}  {w['temp_f']:.0f}°F  {w['condition'].upper()}  ·  WIND {w['wind_mph']:.0f} MPH"
            )
        else:
            self._weather_lbl.setText("WEATHER UNAVAILABLE")

    def _do_close(self):
        self.hide()
        self.closed.emit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._do_close()
        else:
            super().keyPressEvent(event)


class SetupOverlay(QWidget):
    """First-run overlay — asks for a Gemini API key (and re-shown later if
    the key turns out to be invalid, via JarvisUI.prompt_reconfig()). Emits
    done(api_key, ...) once the user submits a key that gets accepted."""
    done = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SetupOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)

        detected = {"darwin": "mac", "windows": "windows"}.get(
            _OS.lower(), "linux"
        )
        self._sel_os = detected

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 22, 30, 22)
        layout.setSpacing(8)

        def _lbl(txt, font_size=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(_hud_font(font_size,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        layout.addWidget(_lbl("◈  INITIALISATION REQUIRED", 13, True))
        layout.addWidget(_lbl("Configure J.A.R.V.I.S. before first boot.", 9, color=C.PRI_DIM))
        layout.addSpacing(6)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep)
        layout.addSpacing(4)

        layout.addWidget(_lbl("GEMINI API KEY", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("AIza…")
        self._key_input.setFont(_hud_font(10))
        self._key_input.setFixedHeight(32)
        self._key_input.setStyleSheet(f"""
            QLineEdit {{
                background: #000d12; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 8px; padding: 4px 8px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        layout.addWidget(self._key_input)
        layout.addSpacing(12)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep2)
        layout.addSpacing(4)

        layout.addWidget(_lbl("OPERATING SYSTEM", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        det_name = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}[detected]
        layout.addWidget(_lbl(f"Auto-detected: {det_name}", 8, color=C.ACC2,
                               align=Qt.AlignmentFlag.AlignLeft))

        os_row = QHBoxLayout(); os_row.setSpacing(6)
        self._os_btns: dict[str, QPushButton] = {}
        for key, label in [("windows","⊞  Windows"),("mac","  macOS"),("linux","🐧  Linux")]:
            btn = QPushButton(label)
            btn.setFont(_hud_font(9, QFont.Weight.Bold))
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self._sel(k))
            os_row.addWidget(btn)
            self._os_btns[key] = btn
        layout.addLayout(os_row)
        self._sel(detected)
        layout.addSpacing(12)

        init_btn = QPushButton("▸  INITIALISE SYSTEMS")
        init_btn.setFont(_hud_font(10, QFont.Weight.Bold))
        init_btn.setFixedHeight(36)
        init_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        init_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 8px;
            }}
            QPushButton:hover {{
                background: {C.PRI_GHO}; border: 1px solid {C.PRI};
            }}
        """)
        init_btn.clicked.connect(self._submit)
        layout.addWidget(init_btn)

    def _sel(self, key: str):
        self._sel_os = key
        pal = {"windows":(C.PRI,"#001a22"),"mac":(C.ACC2,"#1a1400"),"linux":(C.GREEN,"#001a0d")}
        for k, btn in self._os_btns.items():
            if k == key:
                fg, bg = pal[k]
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {fg}; color: {bg};
                        border: none; border-radius: 8px; font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #000d12; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 8px;
                    }}
                    QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
                """)

    def _submit(self):
        key = self._key_input.text().strip()
        if not key:
            self._key_input.setStyleSheet(
                self._key_input.styleSheet() +
                f" QLineEdit {{ border: 1px solid {C.RED}; }}"
            )
            return
        self.done.emit(key, self._sel_os)


class HueWheel(QWidget):
    """
    Dairesel renk seçici. Kullanıcı tutamacı (küçük beyaz daire) çarkın
    çevresinde sürükleyerek TÜM renk tonları arasından seçim yapar.
    Merkezdeki dolu daire seçilen rengin canlı önizlemesidir.
    """

    hue_picked    = pyqtSignal(str)   # sürükleme sırasında (canlı)
    hue_committed = pyqtSignal(str)   # tutamaç bırakıldığında

    _RING = 16   # halka kalınlığı (px)

    def __init__(self, initial_hex: str = DEFAULT_UI_COLOR, parent=None):
        super().__init__(parent)
        self.setFixedSize(148, 148)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hue  = 0.53
        self._drag = False
        self.set_color(initial_hex)

    # ── API ──────────────────────────────────────────────────────────────────
    def color(self) -> str:
        return QColor.fromHsvF(self._hue, 1.0, 1.0).name()

    def set_color(self, hex_str: str):
        c = QColor((hex_str or "").strip())
        if c.isValid() and c.hsvHueF() >= 0:
            self._hue = c.hsvHueF()
            self.update()

    # ── geometri yardımcıları ────────────────────────────────────────────────
    def _ring_rect(self) -> QRectF:
        m = self._RING / 2 + 3
        return QRectF(self.rect()).adjusted(m, m, -m, -m)

    def _hue_from_pos(self, pos: QPointF) -> float:
        c  = QRectF(self.rect()).center()
        dx = pos.x() - c.x()
        dy = c.y() - pos.y()          # ekran y'si aşağı — matematiksel eksene çevir
        ang = math.atan2(dy, dx)      # [-π, π], saat yönünün tersi
        return (ang / (2 * math.pi)) % 1.0

    # ── çizim ────────────────────────────────────────────────────────────────
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect   = self._ring_rect()
        center = rect.center()

        grad = QConicalGradient(center, 0)
        for i in range(0, 361, 20):
            grad.setColorAt(i / 360.0, QColor.fromHsvF((i % 360) / 360.0, 1.0, 1.0))
        p.setPen(QPen(QBrush(grad), self._RING))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(rect)

        # merkez önizleme dairesi
        preview = QColor.fromHsvF(self._hue, 1.0, 1.0)
        inner   = rect.adjusted(30, 30, -30, -30)
        p.setPen(QPen(qcol(C.BORDER_B), 1))
        p.setBrush(QBrush(preview))
        p.drawEllipse(inner)

        # sürüklenen tutamaç
        r   = rect.width() / 2
        ang = self._hue * 2 * math.pi
        hx  = center.x() + r * math.cos(ang)
        hy  = center.y() - r * math.sin(ang)
        p.setPen(QPen(QColor("#00060a"), 2))
        p.setBrush(QBrush(QColor("#ffffff")))
        p.drawEllipse(QPointF(hx, hy), 7.5, 7.5)

    # ── fare ─────────────────────────────────────────────────────────────────
    def mousePressEvent(self, e):
        self._drag = True
        self._hue  = self._hue_from_pos(e.position())
        self.update()
        self.hue_picked.emit(self.color())

    def mouseMoveEvent(self, e):
        if self._drag:
            self._hue = self._hue_from_pos(e.position())
            self.update()
            self.hue_picked.emit(self.color())

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = False
            self.hue_committed.emit(self.color())


class CustomizeOverlay(QWidget):
    """Floating overlay — change assistant name, user name and UI colour."""

    saved = pyqtSignal(str, str, str)   # assistant_name, user_name, ui_color
    _OW, _OH = 400, 500

    def __init__(self, assistant_name="JARVIS", user_name="",
                 ui_color=DEFAULT_UI_COLOR, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            CustomizeOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 18, 24, 18)
        lay.setSpacing(8)

        def _lbl(txt, fs=9, bold=False, color=C.PRI, align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt); w.setAlignment(align)
            w.setFont(_hud_font(fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        _fs = (f"QLineEdit {{ background: #000d12; color: {C.TEXT}; "
               f"border: 1px solid {C.BORDER}; border-radius: 8px; padding: 4px 8px; }}"
               f"QLineEdit:focus {{ border: 1px solid {C.PRI}; }}")

        lay.addWidget(_lbl("⚙  CUSTOMISE ASSISTANT", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        lay.addWidget(_lbl("ASSISTANT NAME", 8, color=C.TEXT_DIM,
                            align=Qt.AlignmentFlag.AlignLeft))
        self._name_input = QLineEdit(assistant_name)
        self._name_input.setFont(_hud_font(10))
        self._name_input.setFixedHeight(32)
        self._name_input.setStyleSheet(_fs)
        lay.addWidget(self._name_input)

        lay.addSpacing(4)
        lay.addWidget(_lbl("YOUR NAME  (leave blank for default sir / efendim)", 8,
                            color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        self._user_input = QLineEdit(user_name)
        self._user_input.setPlaceholderText("e.g.  Tony   (leave blank for auto)")
        self._user_input.setFont(_hud_font(10))
        self._user_input.setFixedHeight(32)
        self._user_input.setStyleSheet(_fs)
        lay.addWidget(self._user_input)

        # ── UI colour — renk çarkı ───────────────────────────────────────────
        lay.addSpacing(4)
        clr_hdr = QHBoxLayout()
        clr_hdr.addWidget(_lbl("UI COLOUR  —  drag the handle", 8,
                               color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        clr_hdr.addStretch()
        df_btn = QPushButton("DEFAULT")
        df_btn.setFixedSize(64, 20)
        df_btn.setFont(_hud_font(7, QFont.Weight.Bold))
        df_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        df_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 8px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        df_btn.clicked.connect(lambda: self._set_color(DEFAULT_UI_COLOR))
        clr_hdr.addWidget(df_btn)
        lay.addLayout(clr_hdr)

        self._initial_color = (ui_color or DEFAULT_UI_COLOR).strip().lower()
        self._sel_color     = self._initial_color
        self.on_preview     = None   # callable(hex) — canlı önizleme; MainWindow bağlar

        self._wheel = HueWheel(self._sel_color)
        wheel_row = QHBoxLayout()
        wheel_row.addStretch(); wheel_row.addWidget(self._wheel); wheel_row.addStretch()
        lay.addLayout(wheel_row)
        self._wheel.hue_picked.connect(self._on_wheel_pick)
        self._wheel.hue_committed.connect(self._on_wheel_commit)

        self._hex_input = QLineEdit(self._sel_color)
        self._hex_input.setPlaceholderText("#00d4ff   (custom hex colour)")
        self._hex_input.setFont(_hud_font(10))
        self._hex_input.setFixedHeight(28)
        self._hex_input.setStyleSheet(_fs)
        self._hex_input.textEdited.connect(self._on_hex_edited)
        lay.addWidget(self._hex_input)

        lay.addSpacing(6)
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)

        save_btn = QPushButton("▸  APPLY CHANGES")
        save_btn.setFixedHeight(34)
        save_btn.setFont(_hud_font(9, QFont.Weight.Bold))
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 8px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        cancel_btn = QPushButton("CANCEL")
        cancel_btn.setFixedHeight(34)
        cancel_btn.setFont(_hud_font(9))
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 8px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        cancel_btn.clicked.connect(self._cancel)
        btn_row.addWidget(cancel_btn)
        lay.addLayout(btn_row)

    # ── renk akışı ───────────────────────────────────────────────────────────
    def _set_color(self, hx: str, update_wheel: bool = True, preview: bool = True):
        """Seçili rengi günceller; hex kutusu + çark senkron kalır, tema canlı önizlenir."""
        self._sel_color = hx.strip().lower()
        self._hex_input.blockSignals(True)
        self._hex_input.setText(self._sel_color)
        self._hex_input.blockSignals(False)
        if update_wheel:
            self._wheel.set_color(self._sel_color)
        if preview and self.on_preview:
            self.on_preview(self._sel_color)

    def _on_wheel_pick(self, hx: str):
        # Sürükleme sırasında: hex kutusunu güncelle, temayı henüz uygulama
        self._sel_color = hx
        self._hex_input.blockSignals(True)
        self._hex_input.setText(hx)
        self._hex_input.blockSignals(False)

    def _on_wheel_commit(self, hx: str):
        # Tutamaç bırakıldı → tüm arayüzü canlı önizle
        self._set_color(hx, update_wheel=False)

    def _on_hex_edited(self, text: str):
        t = text.strip().lower()
        if t.startswith("#") and len(t) == 7:
            try:
                int(t[1:], 16)
            except ValueError:
                return
            self._set_color(t, update_wheel=True, preview=True)

    def _cancel(self):
        # Önizleme uygulandıysa açılıştaki renge geri dön
        if self.on_preview and self._sel_color != self._initial_color:
            self.on_preview(self._initial_color)
        self.hide()

    def _save(self):
        name = self._name_input.text().strip() or "JARVIS"
        user = self._user_input.text().strip()
        self.saved.emit(name, user, self._sel_color or DEFAULT_UI_COLOR)
        self.hide()


class ClipboardPanel(QWidget):
    """Floating panel shown when text is copied — offers quick Jarvis actions."""

    action_requested = pyqtSignal(str)
    _W, _H = 326, 112

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ClipboardPanel {{
                background: rgba(0, 8, 14, 248);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)
        self._clip_text = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 7)
        lay.setSpacing(4)

        hdr = QHBoxLayout(); hdr.setSpacing(4)
        icon_lbl = QLabel("◈  CLIPBOARD DETECTED")
        icon_lbl.setFont(_hud_font(7, QFont.Weight.Bold))
        icon_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
        hdr.addWidget(icon_lbl); hdr.addStretch()
        x_btn = QPushButton("✕")
        x_btn.setFixedSize(16, 16)
        x_btn.setFont(_hud_font(8))
        x_btn.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        x_btn.clicked.connect(self.hide)
        hdr.addWidget(x_btn)
        lay.addLayout(hdr)

        self._preview = QLabel()
        self._preview.setFont(_hud_font(8))
        self._preview.setStyleSheet(f"""
            color: {C.TEXT}; background: {C.PANEL2};
            border: 1px solid {C.BORDER}; border-radius: 8px; padding: 4px 6px;
        """)
        self._preview.setWordWrap(False)
        self._preview.setFixedHeight(28)
        lay.addWidget(self._preview)

        btn_row = QHBoxLayout(); btn_row.setSpacing(4)
        _bs = (f"QPushButton {{ background: {C.PANEL2}; color: {C.TEXT_MED}; "
               f"border: 1px solid {C.BORDER}; border-radius: 6px; }}"
               f"QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}")
        for label, cmd_fmt in [
            ("TRANSLATE", "Translate this text to English: {text}"),
            ("SUMMARISE", "Summarise this: {text}"),
            ("EXPLAIN",   "Explain this: {text}"),
            ("FIX",       "Fix grammar and spelling: {text}"),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(22)
            b.setFont(_hud_font(7, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(_bs)
            b.clicked.connect(lambda _, c=cmd_fmt: self._trigger(c))
            btn_row.addWidget(b)
        lay.addLayout(btn_row)

        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self.hide)
        self.hide()

    def _trigger(self, cmd_fmt: str):
        if self._clip_text:
            self.action_requested.emit(cmd_fmt.format(text=self._clip_text[:800]))
        self.hide()

    def show_clipboard(self, text: str):
        self._clip_text = text
        preview = text[:58].replace('\n', ' ')
        if len(text) > 58:
            preview += "…"
        self._preview.setText(f'"{preview}"')
        self.show(); self.raise_()
        self._dismiss_timer.start(8000)


class RemoteKeyOverlay(QWidget):
    """Floating overlay — QR code for instant phone pairing + manual key fallback."""

    closed = pyqtSignal()

    _OW, _OH = 400, 465

    def __init__(self, url: str, key: str, auto_login_url: str = "",
                 manual_url: str = "", expiry_secs: int = 600, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            RemoteKeyOverlay {{
                background: rgba(0, 4, 12, 0.95);
                border: 1px solid {C.BORDER_B};
                border-radius: 14px;
            }}
        """)
        self._expiry          = time.time() + expiry_secs
        self._on_new_key      = None
        self._auto_login_url  = auto_login_url
        self._manual_url      = manual_url or url

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 16, 24, 16)
        lay.setSpacing(5)

        def _lbl(txt, fs=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(_hud_font(fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            w.setWordWrap(True)
            return w

        lay.addWidget(_lbl("◈  REMOTE ACCESS", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep)

        # ── QR code ───────────────────────────────────────────────────────────
        self._qr_label = QLabel()
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setFixedSize(176, 176)
        self._qr_label.setStyleSheet(
            "background: white; border-radius: 10px; padding: 4px;"
        )
        qr_row = QHBoxLayout()
        qr_row.addStretch()
        qr_row.addWidget(self._qr_label)
        qr_row.addStretch()
        lay.addLayout(qr_row)

        self._update_qr(auto_login_url)

        lay.addWidget(_lbl("Scan with phone camera to connect instantly", 8, color=C.TEXT_DIM))

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_lbl("Or enter manually:", 7, color=C.TEXT_DIM,
                           align=Qt.AlignmentFlag.AlignLeft))

        self._url_lbl = QLabel(self._manual_url)
        self._url_lbl.setFont(_hud_font(8))
        self._url_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        self._url_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._url_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._url_lbl)

        self._key_lbl = QLabel(key)
        self._key_lbl.setFont(_hud_font(28, QFont.Weight.Bold))
        self._key_lbl.setStyleSheet(f"""
            color: {C.ACC};
            background: {C.PANEL2};
            border: 1px solid {C.BORDER_B};
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 10px;
        """)
        self._key_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._key_lbl)

        self._timer_lbl = QLabel()
        self._timer_lbl.setFont(_hud_font(8))
        self._timer_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._timer_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._timer_lbl)

        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        new_btn = QPushButton("NEW KEY")
        new_btn.setFixedHeight(32)
        new_btn.setFont(_hud_font(8, QFont.Weight.Bold))
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 5px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        new_btn.clicked.connect(self._refresh_key)
        btn_row.addWidget(new_btn)

        close_btn = QPushButton("DISMISS")
        close_btn.setFixedHeight(32)
        close_btn.setFont(_hud_font(8, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self._do_close)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

        self._ctimer = QTimer(self)
        self._ctimer.timeout.connect(self._tick)
        self._ctimer.start(1000)
        self._tick()

    def set_new_key_callback(self, fn) -> None:
        self._on_new_key = fn

    def _update_qr(self, url: str) -> None:
        if not url:
            self._qr_label.setText("—")
            return
        try:
            import qrcode as _qrmod
            from io import BytesIO
            qr = _qrmod.QRCode(
                box_size=5, border=2,
                error_correction=_qrmod.constants.ERROR_CORRECT_M,
            )
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap()
            px.loadFromData(buf.getvalue())
            self._qr_label.setPixmap(
                px.scaled(170, 170,
                          Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
            )
        except ImportError:
            self._qr_label.setText("pip install\nqrcode[pil]")
            self._qr_label.setFont(_hud_font(8))
            self._qr_label.setStyleSheet(
                "color: #888; background: white; border-radius: 10px; padding: 4px;"
            )
        except Exception:
            self._qr_label.setText(url[:28])
            self._qr_label.setFont(_hud_font(7))
            self._qr_label.setStyleSheet(
                f"color: {C.PRI}; background: white; border-radius: 10px; padding: 4px;"
            )

    def _tick(self):
        remaining = max(0, int(self._expiry - time.time()))
        m, s = divmod(remaining, 60)
        self._timer_lbl.setText(f"Key expires in  {m:02d}:{s:02d}")
        if remaining == 0:
            self._do_close()

    def mark_connected(self) -> None:
        """Call from any thread when a phone successfully connects."""
        self._ctimer.stop()
        self._key_lbl.setText("CONNECTED")
        self._key_lbl.setStyleSheet(f"""
            color: {C.GREEN};
            background: rgba(34,197,94,0.08);
            border: 2px solid rgba(34,197,94,0.4);
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 4px;
        """)
        self._qr_label.setText("✓")
        self._qr_label.setFont(_hud_font(54, QFont.Weight.Bold))
        self._qr_label.setStyleSheet(
            "color: #00ff88; background: #001a0d; border-radius: 10px;"
        )
        self._timer_lbl.setText("Phone connected — JARVIS ready")
        self._timer_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")

    def _refresh_key(self):
        if self._on_new_key:
            result = self._on_new_key()
            if result:
                url    = result[0]
                key    = result[1]
                auto   = result[2] if len(result) >= 3 else ""
                manual = result[3] if len(result) >= 4 else url
                self._manual_url     = manual or url
                self._url_lbl.setText(self._manual_url)
                self._key_lbl.setText(key)
                self._auto_login_url = auto
                self._update_qr(auto or url)
                self._expiry = time.time() + 600
                self._key_lbl.setStyleSheet(f"""
                    color: {C.ACC};
                    background: {C.PANEL2};
                    border: 1px solid {C.BORDER_B};
                    border-radius: 8px;
                    padding: 6px 4px;
                    letter-spacing: 10px;
                """)
                self._timer_lbl.setStyleSheet(
                    f"color: {C.TEXT_MED}; background: transparent;"
                )
                self._ctimer.start(1000)
                self._tick()

    def _do_close(self):
        self._ctimer.stop()
        self.hide()
        self.closed.emit()


class CommandInput(QLineEdit):
    """Command box with shell-style history (↑/↓), persisted between runs,
    plus an inline autocompleter for common requests."""
    _HIST_FILE = CONFIG_DIR / "command_history.json"
    SUGGESTIONS = [
        "What's the weather today?", "What's on my calendar today?",
        "Read my unread emails", "Set a reminder in 30 minutes to ",
        "Start a 25 minute focus timer", "Open Safari", "Open Spotify",
        "Take a screenshot and describe my screen", "Summarize this file",
        "Turn the volume up", "Turn the volume down", "Search the web for ",
        "Give me my morning brief", "What's my system status?",
        "Open God's Eye View", "In God's Eye, fly to ", "In God's Eye, show live flights",
        "In God's Eye, track the ISS", "Switch God's Eye to thermal view",
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hist: list[str] = []
        self._idx = 0
        self._draft = ""
        try:
            self._hist = json.loads(self._HIST_FILE.read_text(encoding="utf-8"))[-200:]
        except Exception:
            self._hist = []
        self._idx = len(self._hist)
        from PyQt6.QtWidgets import QCompleter
        from PyQt6.QtCore import QStringListModel
        self._model = QStringListModel(self._pool())
        comp = QCompleter(self._model, self)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        comp.setFilterMode(Qt.MatchFlag.MatchContains)
        comp.setMaxVisibleItems(7)
        comp.popup().setStyleSheet(f"""
            QListView {{ background: {C.PANEL2}; color: {C.TEXT};
                border: 1px solid {C.PRI_DIM}; border-radius: 8px; padding: 4px;
                outline: none; }}
            QListView::item {{ padding: 5px 8px; border-radius: 6px; }}
            QListView::item:selected {{ background: {C.PRI_GHO}; color: {C.WHITE}; }}
        """)
        self.setCompleter(comp)

    def _pool(self) -> list[str]:
        seen, out = set(), []
        for s in list(reversed(self._hist)) + self.SUGGESTIONS:
            k = s.lower()
            if k not in seen:
                seen.add(k); out.append(s)
        return out

    def remember(self, txt: str):
        if self._hist and self._hist[-1] == txt:
            self._idx = len(self._hist); return
        self._hist.append(txt)
        self._hist = self._hist[-200:]
        self._idx = len(self._hist)
        self._model.setStringList(self._pool())
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            self._HIST_FILE.write_text(json.dumps(self._hist), encoding="utf-8")
        except Exception:
            pass

    def keyPressEvent(self, e):
        popup = self.completer().popup() if self.completer() else None
        if popup is not None and popup.isVisible():
            return super().keyPressEvent(e)
        if e.key() == Qt.Key.Key_Up and self._hist:
            if self._idx == len(self._hist):
                self._draft = self.text()
            self._idx = max(0, self._idx - 1)
            self.setText(self._hist[self._idx]); return
        if e.key() == Qt.Key.Key_Down and self._hist:
            self._idx = min(len(self._hist), self._idx + 1)
            self.setText(self._draft if self._idx == len(self._hist) else self._hist[self._idx])
            return
        super().keyPressEvent(e)


class Toast(QLabel):
    """Small auto-fading notification pill shown at the top of the window."""
    def __init__(self, parent):
        super().__init__(parent)
        self.setFont(_hud_font(9, QFont.Weight.Bold))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._tmr = QTimer(self); self._tmr.setSingleShot(True)
        self._tmr.timeout.connect(self.hide)
        self.hide()

    def show_msg(self, text: str, color: str | None = None, ms: int = 2200):
        col = color or C.PRI
        self.setStyleSheet(f"""
            QLabel {{ background: {C.PANEL2}; color: {col};
                border: 1px solid {col}; border-radius: 16px; padding: 7px 18px; }}
        """)
        self.setText(text)
        self.adjustSize()
        par = self.parentWidget()
        self.move((par.width() - self.width()) // 2, 70)
        self.raise_(); self.show()
        self._tmr.start(ms)


class CommandPalette(QWidget):
    """Ctrl+K / F1 command palette: fuzzy-search quick actions and common
    requests, plus a cheat-sheet of keyboard shortcuts."""
    def __init__(self, parent, actions: list[tuple[str, str, object]]):
        super().__init__(parent)
        self._actions = actions   # (title, hint, callable)
        self.setStyleSheet("background: transparent;")
        self._card = QFrame(self)
        self._card.setStyleSheet(f"""
            QFrame#card {{ background: {C.PANEL}; border: 1px solid {C.PRI_DIM};
                border-radius: 16px; }}
        """)
        self._card.setObjectName("card")
        v = QVBoxLayout(self._card); v.setContentsMargins(16, 16, 16, 12); v.setSpacing(10)
        title = QLabel("COMMAND CENTER")
        title.setFont(_hud_font(9, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent; border: none; letter-spacing: 2px;")
        v.addWidget(title)
        self._q = QLineEdit()
        self._q.setPlaceholderText("Search actions or type anything to ask…")
        self._q.setFont(_hud_font(11))
        self._q.setFixedHeight(40)
        self._q.setStyleSheet(f"""
            QLineEdit {{ background: {C.DARK}; color: {C.WHITE};
                border: 1px solid {C.PRI}; border-radius: 10px; padding: 4px 12px; }}
        """)
        self._q.textChanged.connect(self._filter)
        self._q.returnPressed.connect(self._run_current)
        self._q.installEventFilter(self)
        v.addWidget(self._q)
        self._list = QListWidget()
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setWordWrap(True)
        self._list.setFont(_hud_font(10))
        self._list.setStyleSheet(f"""
            QListWidget {{ background: transparent; color: {C.TEXT}; border: none; outline: none; }}
            QListWidget::item {{ padding: 8px 10px; border-radius: 8px; }}
            QListWidget::item:selected {{ background: {C.PRI_GHO}; color: {C.WHITE}; }}
            QListWidget::item:hover {{ background: {C.PRI_GHO}; }}
            QScrollBar:vertical {{ background: transparent; width: 6px; border: none; }}
            QScrollBar::handle:vertical {{ background: {C.BORDER_B}; border-radius: 3px; min-height: 24px; }}
            QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; border: none; }}
            QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
        """)
        self._list.itemActivated.connect(lambda _i: self._run_current())
        self._list.itemClicked.connect(lambda _i: self._run_current())
        v.addWidget(self._list, stretch=1)
        keys = QLabel("↑↓ navigate   ⏎ run   Esc close   ·   Ctrl+K palette   F4 mute   "
                      "F11 fullscreen   Ctrl+L clear log   ↑ in input = history")
        keys.setWordWrap(True)
        keys.setFont(_hud_font(8))
        keys.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        v.addWidget(keys)
        self.hide()

    def paintEvent(self, _):
        p = QPainter(self); p.fillRect(self.rect(), QColor(0, 0, 0, 150)); p.end()

    def mousePressEvent(self, e):
        if not self._card.geometry().contains(e.position().toPoint()):
            self.close_palette()

    def open(self):
        par = self.parentWidget()
        self.setGeometry(0, 0, par.width(), par.height())
        cw, ch = min(620, par.width() - 80), min(460, par.height() - 120)
        self._card.setGeometry((par.width() - cw) // 2, 90, cw, ch)
        self._q.clear(); self._filter("")
        self.raise_(); self.show(); self._q.setFocus()

    def close_palette(self):
        self.hide()

    def _filter(self, text: str):
        self._list.clear()
        t = text.strip().lower()
        for title, hint, fn in self._actions:
            if not t or all(w in (title + " " + hint).lower() for w in t.split()):
                it = QListWidgetItem(f"{title:<36}{hint}")
                it.setData(Qt.ItemDataRole.UserRole, fn)
                self._list.addItem(it)
        if t:
            it = QListWidgetItem(f"Ask: “{text.strip()}”")
            it.setData(Qt.ItemDataRole.UserRole, ("ask", text.strip()))
            self._list.addItem(it)
        if self._list.count():
            self._list.setCurrentRow(0)

    def eventFilter(self, obj, e):
        from PyQt6.QtCore import QEvent
        if obj is self._q and e.type() == QEvent.Type.KeyPress:
            k = e.key()
            if k in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                n = self._list.count()
                if n:
                    r = self._list.currentRow() + (1 if k == Qt.Key.Key_Down else -1)
                    self._list.setCurrentRow(max(0, min(n - 1, r)))
                return True
            if k == Qt.Key.Key_Escape:
                self.close_palette(); return True
        return super().eventFilter(obj, e)

    ask_requested = pyqtSignal(str)

    def _run_current(self):
        it = self._list.currentItem()
        if it is None:
            return
        fn = it.data(Qt.ItemDataRole.UserRole)
        self.close_palette()
        if isinstance(fn, tuple) and fn and fn[0] == "ask":
            self.ask_requested.emit(fn[1])
        elif isinstance(fn, str):
            self.ask_requested.emit(fn)
        elif callable(fn):
            QTimer.singleShot(0, fn)


class MainWindow(QMainWindow):
    """
    Assembles every widget above into the actual window: the avatar orb,
    the always-visible live panels row (Reminders/Calendar/Earth globe),
    the conversation log, footer metric bars, and the various overlays
    (setup/customize/remote/clipboard/Global Ops), wiring their show/hide
    and button clicks together.

    All the `*_sig` signals declared here exist for one reason: main.py's
    JarvisLive lives on a background thread, but these widgets can only be
    touched from the Qt main thread. So instead of calling e.g.
    self._log.append(...) directly, JarvisLive calls a plain method here
    (write_log, set_state, show_content, ...) which just emits the matching
    signal; the signal is connected to the real (main-thread) handler
    method, and Qt takes care of marshaling the call across threads safely.
    """
    _log_sig        = pyqtSignal(str)
    _state_sig      = pyqtSignal(str)
    _content_sig    = pyqtSignal(str, str)   # (title, text) — thread-safe content display
    _reconfig_sig   = pyqtSignal()           # trigger setup overlay from any thread
    _camera_sig     = pyqtSignal(bytes)      # show camera frame preview (small overlay)
    _cam_stream_sig = pyqtSignal(bool)       # True=start live stream, False=stop
    _cam_frame_sig  = pyqtSignal(bytes)      # live camera frame → HUD area
    _clipboard_sig  = pyqtSignal(str)        # clipboard text changed (thread-safe)
    _today_sig      = pyqtSignal(dict)       # slow-fetched TODAY panel data (thread-safe)

    def __init__(self, face_path: str):
        super().__init__()
        self._face_path = face_path

        # Load customization from config
        _cfg = _read_full_config()
        self._assistant_name: str = (_cfg.get("assistant_name") or "JARVIS").strip()
        _display = self._assistant_name.upper()

        # Kayıtlı UI rengini panel/stylesheet'ler kurulmadan ÖNCE uygula
        _ui_color = (_cfg.get("ui_color") or "").strip()
        if _ui_color and _ui_color.lower() != DEFAULT_UI_COLOR:
            apply_ui_accent(_ui_color)

        self.setWindowTitle(f"{_display} — MARK L")
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)

        screen = QApplication.primaryScreen().availableGeometry()
        # center on the actual screen (respects multi-monitor offsets / menu bar)
        self.resize(min(_DEFAULT_W, screen.width()), min(_DEFAULT_H, screen.height()))
        self.move(
            screen.x() + max(0, (screen.width()  - self.width())  // 2),
            screen.y() + max(0, (screen.height() - self.height()) // 2),
        )

        self.on_text_command   = None
        self.on_remote_clicked = None   # callable: () -> (url, key) | None
        self.on_interrupt      = None   # callable: () -> None — stop JARVIS mid-speech
        self._muted            = False
        self._current_file: str | None = None
        self._remote_overlay: RemoteKeyOverlay | None = None
        self._customize_overlay: CustomizeOverlay | None = None
        self._global_ops_overlay: GlobalOpsOverlay | None = None

        central = QWidget()
        central.setStyleSheet(f"background: {C.BG};")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._left_panel = self._build_left_panel()
        body.addWidget(self._left_panel, stretch=0)

        # Center column: HUD + always-visible live panels + resizable content
        # panel, all sharing one vertical QSplitter
        self.hud = HudCanvas(face_path, _display)
        self.hud.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._content_panel = self._build_content_panel()
        self._live_panels   = self._build_live_panels()

        # Live camera container — replaces HUD when camera stream is active
        _cam_cont = QWidget()
        _cam_cont.setStyleSheet("background: #000308;")
        _cam_v = QVBoxLayout(_cam_cont)
        _cam_v.setContentsMargins(0, 0, 0, 0)
        _cam_v.setSpacing(0)
        _cam_hdr = QHBoxLayout()
        _cam_hdr.setContentsMargins(8, 5, 8, 5)
        _cam_title = QLabel("◈  CAMERA FEED")
        _cam_title.setFont(_hud_font(8, QFont.Weight.Bold))
        _cam_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        _cam_hdr.addWidget(_cam_title)
        _cam_hdr.addStretch()
        _cam_x = QPushButton("✕  CLOSE")
        _cam_x.setFont(_hud_font(8, QFont.Weight.Bold))
        _cam_x.setCursor(Qt.CursorShape.PointingHandCursor)
        _cam_x.setStyleSheet(f"""
            QPushButton {{
                color: {C.TEXT_DIM}; background: transparent;
                border: none; padding: 2px 6px;
            }}
            QPushButton:hover {{ color: {C.PRI}; }}
        """)
        _cam_x.clicked.connect(self.stop_camera_stream)
        _cam_hdr.addWidget(_cam_x)
        _cam_v.addLayout(_cam_hdr)
        self._cam_live_lbl = QLabel()
        self._cam_live_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cam_live_lbl.setStyleSheet("background: transparent;")
        self._cam_live_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        _cam_v.addWidget(self._cam_live_lbl, stretch=1)

        # Stack: 0 = animated HUD, 1 = live camera
        self._hud_cam_stack = QStackedWidget()
        self._hud_cam_stack.addWidget(self.hud)
        self._hud_cam_stack.addWidget(_cam_cont)

        self._center_split = QSplitter(Qt.Orientation.Vertical)
        self._center_split.setStyleSheet(f"""
            QSplitter::handle {{
                background: {C.BORDER};
                height: 4px;
            }}
            QSplitter::handle:hover {{
                background: {C.PRI_DIM};
            }}
        """)
        self._center_split.addWidget(self._hud_cam_stack)
        self._center_split.addWidget(self._live_panels)
        self._center_split.addWidget(self._content_panel)
        self._center_split.setStretchFactor(0, 3)
        self._center_split.setStretchFactor(1, 2)
        self._center_split.setStretchFactor(2, 1)
        self._center_split.setCollapsible(0, False)
        body.addWidget(self._center_split, stretch=5)
        QTimer.singleShot(0, lambda: self._center_split.setSizes([440, 260, 0]))

        self._right_panel = self._build_right_panel()
        body.addWidget(self._right_panel, stretch=0)

        root.addLayout(body, stretch=1)
        root.addWidget(self._build_footer())

        # Quick-access drawer (floating overlay, built after central widget layout is done)
        self._quick_drawer = self._build_quick_drawer()
        self._update_autostart_btn(self._check_autostart())
        from memory.config_manager import get_brief_enabled as _gbe
        self._update_brief_btn(_gbe())

        self._clock_tmr = QTimer(self)
        self._clock_tmr.timeout.connect(self._tick_clock)
        self._clock_tmr.start(1000)
        self._tick_clock()

        # Metrik güncelleme timer'ı
        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self._update_metrics)
        self._metric_tmr.start(2000)
        self._update_metrics()

        # TODAY panel: slow fields (calendar/email/tasks) refreshed periodically
        # off the UI thread — see _fetch_today_slow / _update_today_panel.
        self._today_tmr = QTimer(self)
        self._today_tmr.timeout.connect(self._spawn_today_fetch)
        self._today_tmr.start(60000)
        QTimer.singleShot(1500, self._spawn_today_fetch)

        self._log_sig.connect(self._log.append_log)
        self._state_sig.connect(self._apply_state)
        self._content_sig.connect(self._show_content)
        self._reconfig_sig.connect(self._show_setup)
        self._camera_sig.connect(self._show_camera_frame)
        self._cam_stream_sig.connect(self._on_cam_stream)
        self._cam_frame_sig.connect(self._on_cam_frame)
        self._clipboard_sig.connect(self._show_clipboard_panel)
        self._today_sig.connect(self._update_today_panel)
        self._cam_stop = threading.Event()

        # Camera preview overlay (child of central widget, positioned in resizeEvent)
        self._cam_preview = _CameraPreview(self.centralWidget())

        # Clipboard panel (child of central widget, bottom-center)
        self._clipboard_panel = ClipboardPanel(self.centralWidget())
        self._clipboard_panel.action_requested.connect(self._on_clipboard_action)
        QApplication.clipboard().dataChanged.connect(self._on_clipboard_changed)

        self._overlay: SetupOverlay | None = None
        self._ready = self._check_config()
        if not self._ready:
            self._show_setup()

        sc_mute = QShortcut(QKeySequence("F4"), self)
        sc_mute.activated.connect(self._toggle_mute)
        sc_full = QShortcut(QKeySequence("F11"), self)
        sc_full.activated.connect(self._toggle_fullscreen)
        sc_intr = QShortcut(QKeySequence("Escape"), self)
        sc_intr.activated.connect(self._do_interrupt)

        # ── Modern UX: toast, command palette, extra shortcuts ─────────
        self._toast = Toast(self.centralWidget())
        self._palette = CommandPalette(self.centralWidget(), [
            ("Toggle microphone", "F4", self._toggle_mute),
            ("Interrupt / stop speaking", "Esc", self._do_interrupt),
            ("Toggle fullscreen", "F11", self._toggle_fullscreen),
            ("Clear conversation log", "Ctrl+L", self._clear_log),
            ("Focus command input", "Ctrl+I", self._focus_input),
            ("Open Global Ops globe", "telemetry", self._open_global_ops),
            ("Customize name & color", "settings", self._open_customize),
            ("Connect phone remote", "pairing", self._open_remote),
            ("Upload a file", "attach", lambda: self._drop_zone._browse()),
            ("God's Eye View — open globe", "Ctrl+G", self._open_gods_eye),
            ("God's Eye — show live flights", "gods eye", "In God's Eye, show live flights"),
            ("God's Eye — satellites & ISS", "gods eye", "In God's Eye, show satellites and when the ISS passes next"),
            ("God's Eye — earthquakes", "gods eye", "In God's Eye, show recent earthquakes and zoom out to the globe"),
            ("God's Eye — night vision", "gods eye", "Switch God's Eye to night vision"),
            ("God's Eye — close", "gods eye", "Close God's Eye View"),
            ("Morning brief", "ask", "Give me my morning brief"),
            ("Today's weather", "ask", "What's the weather today?"),
            ("Today's calendar", "ask", "What's on my calendar today?"),
            ("Unread emails", "ask", "Read my unread emails"),
            ("Focus timer (25 min)", "ask", "Start a 25 minute focus timer"),
            ("Describe my screen", "ask", "Take a screenshot and describe my screen"),
            ("System status", "ask", "What's my system status?"),
        ])
        self._palette.ask_requested.connect(self._send_text)
        for seq, fn in (("Ctrl+K", self._palette.open), ("F1", self._palette.open),
                        ("Ctrl+L", self._clear_log), ("Ctrl+I", self._focus_input),
                        ("Ctrl+,", self._open_customize), ("Ctrl+G", self._open_gods_eye)):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
            sc.activated.connect(fn)
        QTimer.singleShot(400, self._focus_input)

    def _show_camera_frame(self, img_bytes: bytes):
        """Slot — display camera preview overlay (main thread)."""
        self._cam_preview.show_frame(img_bytes)
        cw = self.centralWidget()
        pw = _CameraPreview._W
        ph = self._cam_preview.height()
        self._cam_preview.setGeometry(
            cw.width() - _RIGHT_W - pw - 12,
            cw.height() - ph - 28,
            pw, ph,
        )

    # --- Live camera stream in HUD area ------------------------------------
    def _on_cam_stream(self, start: bool) -> None:
        if start:
            self._hud_cam_stack.setCurrentIndex(1)
        else:
            self._hud_cam_stack.setCurrentIndex(0)
            self._cam_live_lbl.clear()

    def _on_cam_frame(self, data: bytes) -> None:
        px = QPixmap()
        px.loadFromData(data)
        if not px.isNull():
            w, h = self._cam_live_lbl.width(), self._cam_live_lbl.height()
            if w > 1 and h > 1:
                self._cam_live_lbl.setPixmap(
                    px.scaled(w, h,
                              Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
                )

    def start_camera_stream(self) -> None:
        self._cam_stop.clear()
        self._cam_stream_sig.emit(True)
        t = threading.Thread(target=self._cam_loop, daemon=True, name="cam-stream")
        t.start()

    def _cam_loop(self) -> None:
        try:
            import cv2
            # Reuse camera index detected by screen_processor (cached in api_keys.json)
            cam_idx = 0
            try:
                import json as _j
                cfg = _j.loads((CONFIG_DIR / "api_keys.json").read_text())
                cam_idx = int(cfg.get("camera_index", 0))
            except Exception:
                pass
            try:
                backend = cv2.CAP_DSHOW if _OS == "Windows" else cv2.CAP_ANY
            except AttributeError:
                backend = 0
            cap = cv2.VideoCapture(cam_idx, backend)
            if not cap.isOpened():
                cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                return
            # warm-up frames
            for _ in range(5):
                cap.read()
            while not self._cam_stop.wait(0.033) and cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
                    self._cam_frame_sig.emit(buf.tobytes())
            cap.release()
        except Exception as e:
            print(f"[Camera] Stream error: {e}")
        finally:
            self._cam_stream_sig.emit(False)

    def stop_camera_stream(self) -> None:
        self._cam_stop.set()

    # ------------------------------------------------------------------
    # Icon generation — arc-reactor style, rendered with Pillow
    # ------------------------------------------------------------------
    @staticmethod
    def _build_jarvis_icon(out_path: Path) -> bool:
        """
        Render a JARVIS arc-reactor icon at 4× resolution and downsample
        for crisp results at all sizes. Saves a multi-res .ico to out_path.
        Returns True on success.
        """
        try:
            import math
            import PIL.Image
            import PIL.ImageDraw
            import PIL.ImageFilter
        except ImportError:
            return False

        CYAN   = (0, 212, 255)
        DIM    = (0, 100, 140)
        DARK   = (0, 6, 10)
        GLOW   = (0, 160, 200)
        WHITE  = (220, 240, 255)

        def _render(sz: int) -> PIL.Image.Image:
            S  = sz * 4                     # draw at 4× then downscale
            img = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            d   = PIL.ImageDraw.Draw(img)
            cx = cy = S // 2

            # ── filled background circle ──────────────────────────────────
            R = S // 2 - 2
            d.ellipse([cx-R, cy-R, cx+R, cy+R], fill=(*DARK, 255))

            # ── outer border ring ─────────────────────────────────────────
            lw = max(2, S // 40)
            d.ellipse([cx-R, cy-R, cx+R, cy+R],
                      outline=(*CYAN, 220), width=lw)

            # ── mid decorative ring ───────────────────────────────────────
            R2 = int(R * 0.72)
            d.ellipse([cx-R2, cy-R2, cx+R2, cy+R2],
                      outline=(*DIM, 180), width=max(1, lw // 2))

            # ── 6 radial spokes (hex bolt) ────────────────────────────────
            R_inner = int(R * 0.30)
            R_outer = int(R * 0.62)
            spoke_w = max(1, S // 80)
            for i in range(6):
                angle = math.radians(i * 60 - 30)
                x1 = cx + int(R_inner * math.cos(angle))
                y1 = cy + int(R_inner * math.sin(angle))
                x2 = cx + int(R_outer * math.cos(angle))
                y2 = cy + int(R_outer * math.sin(angle))
                d.line([x1, y1, x2, y2], fill=(*GLOW, 200), width=spoke_w)

            # ── 6 tick marks on outer ring ────────────────────────────────
            for i in range(6):
                angle = math.radians(i * 60)
                for dr in range(lw * 2):
                    rx = (R - lw - dr)
                    d.point(
                        [cx + int(rx * math.cos(angle)),
                         cy + int(rx * math.sin(angle))],
                        fill=(*WHITE, 220),
                    )

            # ── inner glowing ring ────────────────────────────────────────
            Ri = int(R * 0.26)
            d.ellipse([cx-Ri, cy-Ri, cx+Ri, cy+Ri],
                      outline=(*CYAN, 255), width=max(2, lw))

            # ── bright glow soft blur applied before core ─────────────────
            # (draw a slightly larger cyan circle on a separate layer)
            glow_layer = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            gd = PIL.ImageDraw.Draw(glow_layer)
            Rc = int(R * 0.13)
            gd.ellipse([cx-Rc*2, cy-Rc*2, cx+Rc*2, cy+Rc*2],
                       fill=(*CYAN, 110))
            glow_layer = glow_layer.filter(PIL.ImageFilter.GaussianBlur(S // 14))
            img = PIL.Image.alpha_composite(img, glow_layer)
            d   = PIL.ImageDraw.Draw(img)

            # ── core dot ──────────────────────────────────────────────────
            d.ellipse([cx-Rc, cy-Rc, cx+Rc, cy+Rc], fill=(*WHITE, 255))

            # ── downscale to target size ──────────────────────────────────
            return img.resize((sz, sz), PIL.Image.LANCZOS)

        try:
            sizes  = [256, 128, 64, 48, 32, 16]
            frames = [_render(s) for s in sizes]
            frames[0].save(
                out_path,
                format="ICO",
                append_images=frames[1:],
                sizes=[(s, s) for s in sizes],
            )
            return True
        except Exception as e:
            print(f"[Shortcut] ⚠️  Icon generation failed: {e}")
            return False

    @staticmethod
    def _create_lnk_windows(lnk: str, target: str, args: str,
                             work_dir: str, icon_loc: str) -> None:
        """
        Create a Windows .lnk shortcut WITHOUT launching PowerShell or cmd.
        Tries win32com (pywin32) first; falls back to wscript.exe + VBScript.
        wscript.exe is a GUI-mode host — it never opens a console window.
        """
        # ── Option 1: pywin32 (pure Python COM, zero subprocess) ──────────
        try:
            from win32com.client import Dispatch   # type: ignore
            sh = Dispatch("WScript.Shell")
            sc = sh.CreateShortCut(lnk)
            sc.TargetPath       = target
            sc.Arguments        = f'"{args}"'
            sc.WorkingDirectory = work_dir
            sc.Description      = "J.A.R.V.I.S AI Assistant"
            sc.IconLocation     = icon_loc
            sc.save()
            return
        except ImportError:
            pass

        # ── Option 2: wscript.exe + VBScript (always available on Windows,
        #    GUI-mode executable — never opens a console window) ────────────
        vbs = "\n".join([
            'Set ws = CreateObject("WScript.Shell")',
            f'Set sc = ws.CreateShortcut("{lnk}")',
            f'sc.TargetPath = "{target}"',
            f'sc.Arguments = Chr(34) & "{args}" & Chr(34)',
            f'sc.WorkingDirectory = "{work_dir}"',
            'sc.Description = "J.A.R.V.I.S AI Assistant"',
            f'sc.IconLocation = "{icon_loc}"',
            'sc.Save',
        ])
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".vbs")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(vbs)
            proc = subprocess.Popen(
                ["wscript.exe", "/nologo", tmp],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
            )
            proc.wait(timeout=10)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    @staticmethod
    def _get_desktop_dir() -> Path:
        """
        Resolve the user's REAL desktop directory instead of assuming
        ~/Desktop, which breaks when:
          • OneDrive "Known Folder Move" relocates the desktop
            (C:/Users/x/OneDrive/Desktop) — very common on Win 10/11;
          • the XDG desktop is localized on Linux (~/Masaüstü,
            ~/Schreibtisch, ~/Bureau, …).
        Falls back to ~/Desktop only as a last resort.
        """
        home = Path.home()
        _os = platform.system()

        if _os == "Windows":
            # ── 1) SHGetKnownFolderPath(FOLDERID_Desktop) — the canonical
            #       answer; follows OneDrive redirection. No dependencies. ──
            try:
                import ctypes
                from ctypes import wintypes

                class _GUID(ctypes.Structure):
                    _fields_ = [("Data1", wintypes.DWORD),
                                ("Data2", wintypes.WORD),
                                ("Data3", wintypes.WORD),
                                ("Data4", ctypes.c_ubyte * 8)]

                # FOLDERID_Desktop {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
                fid = _GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                            (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9,
                                                 0x9A, 0x87, 0xC6, 0x41))
                buf = ctypes.c_wchar_p()
                if ctypes.windll.shell32.SHGetKnownFolderPath(
                        ctypes.byref(fid), 0, None, ctypes.byref(buf)) == 0:
                    p = Path(buf.value)
                    ctypes.windll.ole32.CoTaskMemFree(buf)
                    if p.is_dir():
                        return p
            except Exception:
                pass

            # ── 2) Registry: User Shell Folders (may contain %VARS%) ──────
            try:
                import winreg
                with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion"
                        r"\Explorer\User Shell Folders") as key:
                    val, _t = winreg.QueryValueEx(key, "Desktop")
                p = Path(os.path.expandvars(val))
                if p.is_dir():
                    return p
            except Exception:
                pass

        elif _os == "Linux":
            # ── xdg-user-dir honours localized names (~/Masaüstü, …) ──────
            try:
                out = subprocess.run(["xdg-user-dir", "DESKTOP"],
                                     capture_output=True, text=True, timeout=5)
                p = Path(out.stdout.strip())
                if out.stdout.strip() and p != home and p.is_dir():
                    return p
            except Exception:
                pass
            try:
                cfg = home / ".config" / "user-dirs.dirs"
                for line in cfg.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("XDG_DESKTOP_DIR"):
                        val = line.split("=", 1)[1].strip().strip('"')
                        p = Path(val.replace("$HOME", str(home)))
                        if p != home and p.is_dir():
                            return p
            except Exception:
                pass

        # macOS: ~/Desktop is always the real path (localization is
        # display-only). Everything else lands here as a last resort.
        return home / "Desktop"

    def _create_desktop_shortcut(self):
        """
        Create a desktop shortcut on Windows / macOS / Linux.
        Never opens a terminal, console, or PowerShell window on any platform.
        """
        import stat as _stat
        script  = Path(__file__).resolve().parent / "main.py"
        python  = Path(sys.executable)
        desktop = self._get_desktop_dir()

        # Arc-reactor icon (.ico — also exported as .png for Linux/macOS)
        ico_path = Path(__file__).resolve().parent / "config" / "jarvis.ico"
        if not ico_path.exists():
            self._build_jarvis_icon(ico_path)

        try:
            _os = platform.system()

            # ── Windows ───────────────────────────────────────────────────────
            if _os == "Windows":
                pythonw  = python.parent / "pythonw.exe"
                target   = str(pythonw if pythonw.exists() else python)
                lnk      = str(desktop / "J.A.R.V.I.S.lnk")
                icon_loc = str(ico_path) if ico_path.exists() else f"{target},0"
                self._create_lnk_windows(lnk, target, str(script),
                                         str(script.parent), icon_loc)

            # ── macOS — proper .app bundle (no Terminal window) ───────────────
            elif _os == "Darwin":
                app     = desktop / "J.A.R.V.I.S.app"
                mac_dir = app / "Contents" / "MacOS"
                res_dir = app / "Contents" / "Resources"
                mac_dir.mkdir(parents=True, exist_ok=True)
                res_dir.mkdir(exist_ok=True)

                # Launcher executable (bash — runs as background process,
                # macOS does NOT open Terminal for executables inside .app bundles)
                launcher = mac_dir / "JARVIS"
                launcher.write_text(
                    "#!/usr/bin/env bash\n"
                    f'cd "{script.parent}"\n'
                    f'exec "{python}" "{script}"\n'
                )
                launcher.chmod(launcher.stat().st_mode
                               | _stat.S_IEXEC | _stat.S_IXGRP | _stat.S_IXOTH)

                # Info.plist — must declare usage descriptions for mic/camera,
                # otherwise macOS silently blocks access with no permission
                # prompt at all (unlike Terminal.app, which already declares these).
                (app / "Contents" / "Info.plist").write_text(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                    '<plist version="1.0"><dict>\n'
                    '  <key>CFBundleExecutable</key><string>JARVIS</string>\n'
                    '  <key>CFBundleIdentifier</key>'
                    '<string>com.jarvis.assistant</string>\n'
                    '  <key>CFBundleName</key><string>J.A.R.V.I.S</string>\n'
                    '  <key>CFBundlePackageType</key><string>APPL</string>\n'
                    '  <key>CFBundleVersion</key><string>1.0</string>\n'
                    '  <key>NSMicrophoneUsageDescription</key>'
                    '<string>J.A.R.V.I.S needs microphone access for voice conversation.</string>\n'
                    '  <key>NSCameraUsageDescription</key>'
                    '<string>J.A.R.V.I.S needs camera access for webcam vision.</string>\n'
                    '</dict></plist>\n'
                )

                # Optional: copy icon as .icns (skip silently if Pillow is missing)
                try:
                    import PIL.Image
                    icns = res_dir / "AppIcon.icns"
                    PIL.Image.open(ico_path).save(icns, format="ICNS")
                    # Inject icon reference into plist
                    plist = app / "Contents" / "Info.plist"
                    txt = plist.read_text()
                    plist.write_text(
                        txt.replace(
                            '</dict></plist>',
                            '  <key>CFBundleIconFile</key>'
                            '<string>AppIcon</string>\n</dict></plist>\n',
                        )
                    )
                except Exception:
                    pass  # icon is optional

            # ── Linux — .desktop file (Terminal=false, no console) ────────────
            else:
                # Export .ico → .png for better desktop integration
                png_path = ico_path.with_suffix(".png")
                if not png_path.exists() and ico_path.exists():
                    try:
                        import PIL.Image
                        PIL.Image.open(ico_path).resize(
                            (256, 256), PIL.Image.LANCZOS
                        ).save(png_path, format="PNG")
                    except Exception:
                        png_path = ico_path  # fallback to .ico

                icon_line = f"Icon={png_path}\n" if png_path.exists() else ""
                desk = desktop / "J.A.R.V.I.S.desktop"
                desk.write_text(
                    "[Desktop Entry]\n"
                    "Name=J.A.R.V.I.S\n"
                    f"Exec={python} {script}\n"
                    f"Path={script.parent}\n"
                    "Type=Application\n"
                    "Terminal=false\n"
                    "Categories=Utility;\n"
                    + icon_line
                )
                desk.chmod(desk.stat().st_mode | 0o755)

            self._log.append_log("SYS: Desktop shortcut created.")
        except Exception as e:
            self._log.append_log(f"ERR: Shortcut failed — {e}")

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cw = self.centralWidget()
        if self._overlay and self._overlay.isVisible():
            ow, oh = 460, 390
            self._overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._remote_overlay and self._remote_overlay.isVisible():
            ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
            self._remote_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._customize_overlay and self._customize_overlay.isVisible():
            ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
            self._customize_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._global_ops_overlay and self._global_ops_overlay.isVisible():
            self._global_ops_overlay.setGeometry(0, 0, cw.width(), cw.height())
        # Camera preview — bottom-right corner of the center/HUD area
        pw = _CameraPreview._W
        ph = self._cam_preview.height() or _CameraPreview._H
        self._cam_preview.setGeometry(
            cw.width() - _RIGHT_W - pw - 12,
            cw.height() - ph - 28,
            pw, ph,
        )
        # Clipboard panel — bottom-center
        if hasattr(self, '_clipboard_panel') and self._clipboard_panel.isVisible():
            self._position_clipboard_panel()
        if hasattr(self, '_palette') and self._palette.isVisible():
            self._palette.open()
        # Quick drawer — reposition if open
        if hasattr(self, '_quick_drawer') and self._quick_drawer.isVisible():
            self._position_quick_drawer()

    def _update_metrics(self):
        snap = _metrics.snapshot()

        # CPU
        cpu = snap["cpu"]
        self._bar_cpu.set_value(cpu, f"{cpu:.0f}%")

        # MEM
        mem = snap["mem"]
        self._bar_mem.set_value(mem, f"{mem:.0f}%")

        # NET
        net = snap["net"]
        if net < 1.0:
            net_str = f"{net*1024:.0f}KB/s"
        else:
            net_str = f"{net:.1f}MB/s"
        net_pct = min(100, net * 10)  # 10 MB/s = %100
        self._bar_net.set_value(net_pct, net_str)

        # GPU
        gpu = snap["gpu"]
        if gpu >= 0:
            self._bar_gpu.set_value(gpu, f"{gpu:.0f}%")
        else:
            self._bar_gpu.set_value(0, "N/A")

        # TMP
        tmp = snap["tmp"]
        if tmp >= 0:
            tmp_pct = min(100, (tmp / 100) * 100)
            self._bar_tmp.set_value(tmp_pct, f"{tmp:.0f}°C")
        else:
            self._bar_tmp.set_value(0, "N/A")

        try:
            boot_t  = psutil.boot_time()
            elapsed = time.time() - boot_t
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            self._uptime_lbl.setText(f"UP  {h:02d}:{m:02d}")
        except Exception:
            self._uptime_lbl.setText("UP  --:--")

        try:
            proc_count = len(psutil.pids())
            self._proc_lbl.setText(f"PROC  {proc_count}")
        except Exception:
            self._proc_lbl.setText("PROC  --")

        # Fast, local-only reads (small JSON file only) — safe inline on the
        # UI thread, unlike the calendar/email/task fetches which involve
        # AppleScript subprocesses or network calls (see _fetch_today_slow).
        try:
            from actions.focus_timer import get_ui_status as _focus_status
            fstat = _focus_status()
            if fstat.get("active"):
                self._focus_lbl.setText(f"◷  {fstat['label']} — {fstat['remaining']}")
            else:
                self._focus_lbl.setText("◷  No active session")
        except Exception:
            pass

        try:
            from actions.habit_tracker import get_top_streaks as _top_streaks
            streaks = _top_streaks(limit=3)
            if streaks:
                summary = ", ".join(f"{name} {n}d" for name, n in streaks)
                self._habits_lbl.setText(f"♦  {summary}")
            else:
                self._habits_lbl.setText("♦  No habits tracked")
        except Exception:
            pass

    def _fetch_today_slow(self):
        """
        Background-thread fetch for everything that requires a subprocess or
        network call: the TODAY summary strip (next event / unread / open
        task count) plus the full data behind the always-visible Reminders
        and Calendar panels, plus the user's location for the Earth globe.
        Never call this on the UI thread — it emits _today_sig with the
        results instead of touching widgets directly, since Qt widgets may
        only be touched from the main thread. The calendar/task fetches are
        shared between the summary strip and their full-list panels so we
        don't hit AppleScript twice for the same data. Location resolution
        is cached in-process by actions.location, so repeating this call
        every 60s doesn't repeat the network lookup after the first success
        (or failure).
        """
        data = {
            "next_event": "No upcoming events", "unread": None, "open_tasks": None,
            "events": [], "tasks": [],
            "lat": None, "lon": None, "city": "",
        }

        try:
            from datetime import datetime, timedelta
            from actions.calendar_control import find_events_structured
            events = find_events_structured(
                datetime.now(), datetime.now() + timedelta(days=7), max_results=10
            )
            data["events"] = events
            if events:
                ev = events[0]
                when = ev["start"].strftime("%a %I:%M %p").replace(" 0", " ")
                data["next_event"] = f"{ev['title']} — {when}"
        except Exception as e:
            print(f"[UI] Calendar fetch skipped: {e}")

        try:
            from actions.email_control import get_unread_count
            data["unread"] = get_unread_count()
        except Exception as e:
            print(f"[UI] Email fetch skipped: {e}")

        try:
            from actions.task_manager import get_open_tasks_structured
            tasks = get_open_tasks_structured(max_results=25)
            data["tasks"] = tasks
            data["open_tasks"] = len(tasks)
        except Exception as e:
            print(f"[UI] Reminders fetch skipped: {e}")

        try:
            from actions.location import get_location
            loc = get_location()
            if loc:
                data["lat"]  = loc.get("lat")
                data["lon"]  = loc.get("lon")
                data["city"] = loc.get("city", "")
        except Exception as e:
            print(f"[UI] Location fetch skipped: {e}")

        self._today_sig.emit(data)

    def _update_today_panel(self, data: dict):
        """Slot (main thread) — applies results from _fetch_today_slow."""
        self._next_event_lbl.setText(f"▤  {data.get('next_event', 'No upcoming events')}")

        unread = data.get("unread")
        self._email_lbl.setText(f"✉  {unread} unread" if unread is not None else "✉  -- unread")

        open_tasks = data.get("open_tasks")
        self._tasks_lbl.setText(f"☑  {open_tasks} open" if open_tasks is not None else "☑  -- open")

        self._reminders_panel.set_tasks(data.get("tasks", []))
        self._calendar_panel.set_events(data.get("events", []))
        self._earth_globe.set_location(
            data.get("lat"), data.get("lon"), data.get("city", ""),
        )

    def _spawn_today_fetch(self):
        threading.Thread(target=self._fetch_today_slow, daemon=True).start()

    def _build_header(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(54)
        w.setStyleSheet(f"background: {C.DARK}; border-bottom: 1px solid {C.BORDER_B};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 0, 16, 0)

        def _badge(txt, color=C.TEXT_MED):
            l = QLabel(txt)
            l.setFont(_hud_font(8))
            l.setStyleSheet(f"color: {color}; background: transparent;")
            return l

        lay.addWidget(_badge("MARK L", C.PRI_DIM))
        lay.addSpacing(8)
        self._drawer_btn = QPushButton("⚙")
        self._drawer_btn.setFixedSize(26, 26)
        self._drawer_btn.setFont(_hud_font(11))
        self._drawer_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._drawer_btn.setToolTip("Settings & Controls")
        self._drawer_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 10px;
            }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.PRI_DIM}; }}
            QPushButton:checked {{ color: {C.PRI}; border-color: {C.PRI}; background: {C.PRI_GHO}; }}
        """)
        self._drawer_btn.setCheckable(True)
        self._drawer_btn.clicked.connect(self._toggle_drawer)
        lay.addWidget(self._drawer_btn)
        lay.addSpacing(6)
        self._gev_btn = QPushButton("◉  GOD'S EYE")
        self._gev_btn.setFixedHeight(26)
        self._gev_btn.setFont(_hud_font(8, QFont.Weight.Bold))
        self._gev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._gev_btn.setToolTip("Open God's Eye View — or just say \"Open God's Eye\"")
        self._gev_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 13px; padding: 0 12px; }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
        """)
        self._gev_btn.clicked.connect(self._open_gods_eye)
        lay.addWidget(self._gev_btn)
        lay.addStretch()

        mid = QVBoxLayout(); mid.setSpacing(1)
        _disp = self._assistant_name.upper()
        self._title_lbl = QLabel(_disp)
        self._title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_lbl.setFont(_hud_font(17, QFont.Weight.Bold))
        self._title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        mid.addWidget(self._title_lbl)
        _sub_text = ("Just A Rather Very Intelligent System"
                     if _disp in ("JARVIS", "J.A.R.V.I.S")
                     else "Personal AI Assistant")
        self._sub_lbl = QLabel(_sub_text)
        self._sub_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sub_lbl.setFont(_hud_font(7))
        self._sub_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        mid.addWidget(self._sub_lbl)
        lay.addLayout(mid)
        lay.addStretch()

        right_col = QVBoxLayout(); right_col.setSpacing(2)
        self._clock_lbl = QLabel("00:00:00")
        self._clock_lbl.setFont(_hud_font(14, QFont.Weight.Bold))
        self._clock_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._clock_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._clock_lbl)
        self._date_lbl = QLabel("")
        self._date_lbl.setFont(_hud_font(7))
        self._date_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._date_lbl)
        lay.addLayout(right_col)
        return w

    def _tick_clock(self):
        self._clock_lbl.setText(time.strftime("%H:%M:%S"))
        self._date_lbl.setText(time.strftime("%a %d %b %Y"))

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_LEFT_W)
        w.setStyleSheet(f"background: {C.DARK}; border-right: 1px solid {C.BORDER};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 10, 8, 10)
        lay.setSpacing(6)

        hdr = QLabel("◈ SYS MONITOR")
        hdr.setFont(_hud_font(7, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent; "
                          f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 4px;")
        lay.addWidget(hdr)
        lay.addSpacing(2)

        self._bar_cpu = MetricBar("CPU", C.PRI)
        self._bar_mem = MetricBar("MEM", C.ACC2)
        self._bar_net = MetricBar("NET", C.GREEN)
        self._bar_gpu = MetricBar("GPU", C.ACC)
        self._bar_tmp = MetricBar("TMP", "#ff6688")

        for bar in [self._bar_cpu, self._bar_mem, self._bar_net,
                    self._bar_gpu, self._bar_tmp]:
            lay.addWidget(bar)

        lay.addSpacing(4)

        info_panel = QWidget()
        info_panel.setStyleSheet(
            f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 10px;"
        )
        ip_lay = QVBoxLayout(info_panel)
        ip_lay.setContentsMargins(6, 5, 6, 5)
        ip_lay.setSpacing(3)

        self._uptime_lbl = QLabel("UP  --:--")
        self._uptime_lbl.setFont(_hud_font(8, QFont.Weight.Bold))
        self._uptime_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent; border: none;")
        ip_lay.addWidget(self._uptime_lbl)

        self._proc_lbl = QLabel("PROC  --")
        self._proc_lbl.setFont(_hud_font(8))
        self._proc_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
        ip_lay.addWidget(self._proc_lbl)

        os_name = {"Windows": "WIN", "Darwin": "macOS", "Linux": "LINUX"}.get(_OS, _OS.upper())
        os_lbl = QLabel(f"OS  {os_name}")
        os_lbl.setFont(_hud_font(8))
        os_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent; border: none;")
        ip_lay.addWidget(os_lbl)

        lay.addWidget(info_panel)
        lay.addSpacing(4)

        lay.addSpacing(4)

        today_hdr = QLabel("◈ TODAY")
        today_hdr.setFont(_hud_font(7, QFont.Weight.Bold))
        today_hdr.setStyleSheet(f"color: {C.PRI}; background: transparent; "
                                 f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 4px;")
        lay.addWidget(today_hdr)

        today_panel = QWidget()
        today_panel.setStyleSheet(
            f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 10px;"
        )
        tp_lay = QVBoxLayout(today_panel)
        tp_lay.setContentsMargins(6, 5, 6, 5)
        tp_lay.setSpacing(3)

        def _today_row(prefix: str, default: str, color: str) -> QLabel:
            lbl = QLabel(f"{prefix}  {default}")
            lbl.setFont(_hud_font(8))
            lbl.setStyleSheet(f"color: {color}; background: transparent; border: none;")
            lbl.setWordWrap(True)
            tp_lay.addWidget(lbl)
            return lbl

        self._focus_lbl      = _today_row("◷", "No active session", C.GREEN)
        self._next_event_lbl = _today_row("▤", "No upcoming events", C.ACC2)
        self._email_lbl      = _today_row("✉", "-- unread", C.TEXT_MED)
        self._tasks_lbl      = _today_row("☑", "-- open", C.TEXT_MED)
        self._habits_lbl     = _today_row("♦", "No habits tracked", "#ff6688")

        lay.addWidget(today_panel)
        lay.addStretch()

        return w

    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_RIGHT_W)
        w.setStyleSheet(f"background: {C.DARK}; border-left: 1px solid {C.BORDER};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        def _sec(txt):
            l = QLabel(f"▸ {txt}")
            l.setFont(_hud_font(7, QFont.Weight.Bold))
            l.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            return l

        lay.addWidget(_sec("ACTIVITY LOG"))
        self._log = LogWidget()
        lay.addWidget(self._log, stretch=1)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        lay.addWidget(_sec("FILE UPLOAD"))
        self._drop_zone = FileDropZone()
        self._drop_zone.file_selected.connect(self._on_file_selected)
        lay.addWidget(self._drop_zone)

        self._file_hint = QLabel("No file loaded — drop or click above to upload")
        self._file_hint.setFont(_hud_font(7))
        self._file_hint.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._file_hint.setWordWrap(True)
        lay.addWidget(self._file_hint)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_sec("COMMAND INPUT"))
        lay.addLayout(self._build_input_row())

        self._interrupt_btn = QPushButton("✋  INTERRUPT  [ESC]")
        self._interrupt_btn.setFixedHeight(34)
        self._interrupt_btn.setFont(_hud_font(8, QFont.Weight.Bold))
        self._interrupt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._interrupt_btn.setStyleSheet(f"""
            QPushButton {{
                background: #140008; color: {C.MUTED_C};
                border: 1px solid {C.MUTED_C}; border-radius: 8px;
            }}
            QPushButton:hover {{
                background: #200010; border: 1px solid #ff6688;
            }}
            QPushButton:pressed {{
                background: #300018;
            }}
        """)
        self._interrupt_btn.clicked.connect(self._do_interrupt)
        lay.addWidget(self._interrupt_btn)

        self._mute_btn = QPushButton("🎙  MICROPHONE ACTIVE")
        self._mute_btn.setFixedHeight(30)
        self._mute_btn.setFont(_hud_font(8, QFont.Weight.Bold))
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._style_mute_btn()
        lay.addWidget(self._mute_btn)

        return w

    def _build_quick_drawer(self) -> QWidget:
        """Floating overlay panel shown when the ⚙ header button is toggled."""
        _BTN_STYLE_PRI = f"""
            QPushButton {{
                background: #00091a; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 8px;
                text-align: left; padding: 0 8px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
        """
        _BTN_STYLE_DIM = f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 8px;
                text-align: left; padding: 0 8px;
            }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}
        """

        w = QWidget(self.centralWidget())
        w.setObjectName("QuickDrawer")
        w.setStyleSheet(f"""
            QWidget#QuickDrawer {{
                background: {C.DARK};
                border: 1px solid {C.BORDER_B};
                border-top: none;
                border-radius: 0 0 6px 6px;
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(5)

        hdr = QLabel("◈ CONTROLS")
        hdr.setFont(_hud_font(7, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent; "
                          f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 4px;")
        lay.addWidget(hdr)

        remote_btn = QPushButton("◉  REMOTE CONTROL")
        remote_btn.setFixedHeight(30)
        remote_btn.setFont(_hud_font(8, QFont.Weight.Bold))
        remote_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remote_btn.setStyleSheet(_BTN_STYLE_PRI)
        remote_btn.clicked.connect(self._open_remote)
        lay.addWidget(remote_btn)

        fs_btn = QPushButton("⛶  FULLSCREEN  [F11]")
        fs_btn.setFixedHeight(26)
        fs_btn.setFont(_hud_font(7))
        fs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        fs_btn.setStyleSheet(_BTN_STYLE_DIM)
        fs_btn.clicked.connect(self._toggle_fullscreen)
        lay.addWidget(fs_btn)

        sc_btn = QPushButton("⊞  CREATE DESKTOP SHORTCUT")
        sc_btn.setFixedHeight(26)
        sc_btn.setFont(_hud_font(7))
        sc_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        sc_btn.setStyleSheet(_BTN_STYLE_DIM)
        sc_btn.clicked.connect(self._create_desktop_shortcut)
        lay.addWidget(sc_btn)

        self._autostart_btn = QPushButton("◉  AUTO-START: OFF")
        self._autostart_btn.setFixedHeight(26)
        self._autostart_btn.setFont(_hud_font(7))
        self._autostart_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._autostart_btn.clicked.connect(self._toggle_autostart)
        lay.addWidget(self._autostart_btn)

        cust_btn = QPushButton("⚙  CUSTOMISE ASSISTANT")
        cust_btn.setFixedHeight(26)
        cust_btn.setFont(_hud_font(7))
        cust_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cust_btn.setStyleSheet(_BTN_STYLE_DIM)
        cust_btn.clicked.connect(self._open_customize)
        lay.addWidget(cust_btn)

        self._brief_btn = QPushButton()
        self._brief_btn.setFixedHeight(26)
        self._brief_btn.setFont(_hud_font(7))
        self._brief_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._brief_btn.clicked.connect(self._toggle_brief)
        lay.addWidget(self._brief_btn)

        w.adjustSize()
        return w

    def _toggle_drawer(self, checked: bool):
        if checked:
            self._position_quick_drawer()
            self._quick_drawer.show()
            self._quick_drawer.raise_()
        else:
            self._quick_drawer.hide()

    def _position_quick_drawer(self):
        if not hasattr(self, '_quick_drawer'):
            return
        _W = 220
        self._quick_drawer.setFixedWidth(_W)
        self._quick_drawer.adjustSize()
        self._quick_drawer.setGeometry(12, 54, _W, self._quick_drawer.sizeHint().height())

    def _build_input_row(self) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(5)
        self._input = CommandInput()
        self._input.setPlaceholderText("Ask anything…   (↑ history · Ctrl+K)")
        self._input.setFont(_hud_font(9))
        self._input.setFixedHeight(30)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: #000d14; color: {C.WHITE};
                border: 1px solid {C.BORDER}; border-radius: 8px; padding: 3px 7px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input)

        send = QPushButton("▸")
        send.setFixedSize(30, 30)
        send.setFont(_hud_font(11, QFont.Weight.Bold))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 8px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        send.clicked.connect(self._send)
        row.addWidget(send)
        return row

    def _build_live_panels(self) -> QWidget:
        """
        Always-visible row of three live sections: Reminders, Calendar, and
        a rotating Earth globe marking the user's location — populated/
        refreshed by _fetch_today_slow / _update_today_panel alongside the
        TODAY summary strip in the left panel.
        """
        w = QWidget()
        w.setObjectName("LivePanels")
        w.setStyleSheet(f"QWidget#LivePanels {{ background: {C.BG}; }}")

        lay = QHBoxLayout(w)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(8)

        self._reminders_panel = RemindersPanel()
        self._calendar_panel  = CalendarPanel()
        self._earth_globe     = EarthGlobe()
        self._earth_globe.set_expand_callback(self._open_global_ops)

        lay.addWidget(self._reminders_panel, stretch=1)
        lay.addWidget(self._calendar_panel, stretch=1)
        lay.addWidget(self._earth_globe, stretch=2)

        return w

    def _open_global_ops(self):
        """Opens the full-screen Global Ops page (⛶ EXPAND on the Location
        panel) — a bigger, grayscale/red version of the globe with system
        gauges and a mini world map, styled after the reference the user
        shared. Sized to fill the whole central widget, like a full-window
        overlay."""
        if self._global_ops_overlay and self._global_ops_overlay.isVisible():
            self._global_ops_overlay.raise_()
            return
        cw = self.centralWidget()
        ov = GlobalOpsOverlay(parent=cw)
        ov.setGeometry(0, 0, cw.width(), cw.height())
        ov.closed.connect(lambda: setattr(self, "_global_ops_overlay", None))
        ov.show()
        ov.raise_()
        ov.setFocus()
        self._global_ops_overlay = ov

    def _build_content_panel(self) -> QWidget:
        """
        Collapsible panel below the HUD — shows search results, news, briefings.
        Hidden by default; appears when show_content() is called.
        """
        w = QWidget()
        w.setObjectName("ContentPanel")
        w.setStyleSheet(f"""
            QWidget#ContentPanel {{
                background: {C.PANEL};
                border-top: 1px solid {C.BORDER_B};
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 7, 12, 8)
        lay.setSpacing(5)

        # ── header row ───────────────────────────────────────────────────────
        hdr = QHBoxLayout(); hdr.setSpacing(6)

        dot = QLabel("◈")
        dot.setFont(_hud_font(9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(dot)

        self._content_title_lbl = QLabel("BRIEFING")
        self._content_title_lbl.setFont(_hud_font(8, QFont.Weight.Bold))
        self._content_title_lbl.setStyleSheet(
            f"color: {C.PRI}; background: transparent; letter-spacing: 1px;"
        )
        hdr.addWidget(self._content_title_lbl)
        hdr.addStretch()

        self._content_ts_lbl = QLabel("")
        self._content_ts_lbl.setFont(_hud_font(7))
        self._content_ts_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._content_ts_lbl)

        dismiss = QPushButton("DISMISS  ✕")
        dismiss.setFont(_hud_font(7))
        dismiss.setFixedHeight(18)
        dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        dismiss.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 6px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        dismiss.clicked.connect(w.hide)
        hdr.addWidget(dismiss)
        lay.addLayout(hdr)

        # ── separator ─────────────────────────────────────────────────────────
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); lay.addWidget(sep)

        # ── text display ──────────────────────────────────────────────────────
        self._content_display = QTextEdit()
        self._content_display.setReadOnly(True)
        self._content_display.setFont(_hud_font(8))
        self._content_display.setMinimumHeight(60)
        self._content_display.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._content_display.setStyleSheet(f"""
            QTextEdit {{
                background: {C.DARK};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 8px;
                padding: 6px 8px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG}; width: 6px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B}; border-radius: 8px; min-height: 16px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; border: none;
            }}
        """)
        lay.addWidget(self._content_display)

        return w

    def _show_content(self, title: str, text: str):
        """Slot — runs on Qt main thread. Updates and shows the content panel."""
        import time as _time
        self._content_title_lbl.setText(title.upper()[:48])
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        self._content_display.setPlainText(text)
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start
        )
        first_show = not self._content_panel.isVisible()
        self._content_panel.show()
        if first_show:
            total = self._center_split.height()
            self._center_split.setSizes([max(total - 220, 120), 220])

    def _build_footer(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(22)
        w.setStyleSheet(f"background: {C.DARK}; border-top: 1px solid {C.BORDER};")
        lay = QHBoxLayout(w); lay.setContentsMargins(14, 0, 14, 0)

        def _fl(txt, color=C.TEXT_MED):
            l = QLabel(txt); l.setFont(_hud_font(7))
            l.setStyleSheet(f"color: {color}; background: transparent;")
            return l

        lay.addWidget(_fl("[F4] Mute  ·  [F11] Fullscreen"))
        lay.addStretch()
        lay.addWidget(_fl("MARK L  ·  by Joshua Khooba", C.PRI_DIM))
        return w

    def _on_file_selected(self, path: str):
        self._current_file = path
        p    = Path(path)
        cat  = _file_category(p)
        icon, _ = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size = _fmt_size(p.stat().st_size)
        self._file_hint.setText(f"{icon}  {p.name}  ·  {size}  ·  Tell {self._assistant_name} what to do with it")
        self._log.append_log(f"FILE: {p.name} ({size}) loaded")
        if self.on_text_command:
            msg = (
                f"[FILE_UPLOADED] path={path} | name={p.name} | "
                f"type={p.suffix.lstrip('.')} | size={size} | "
                f"Briefly tell the user you can see the file '{p.name}' "
                f"({size}) has been uploaded and ask what they'd like to do with it."
            )
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def notify_phone_connected(self) -> None:
        if self._remote_overlay and self._remote_overlay.isVisible():
            self._remote_overlay.mark_connected()

    def _open_remote(self):
        if not self.on_remote_clicked:
            self._log.append_log("SYS: Dashboard not running — remote unavailable.")
            return
        result = self.on_remote_clicked()
        if not result:
            self._log.append_log("SYS: Could not generate remote key.")
            return
        url    = result[0]
        key    = result[1]
        auto   = result[2] if len(result) >= 3 else ""
        manual = result[3] if len(result) >= 4 else url
        if self._remote_overlay:
            self._remote_overlay._do_close()
        cw  = self.centralWidget()
        ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
        ov  = RemoteKeyOverlay(url, key, auto_login_url=auto, manual_url=manual,
                               expiry_secs=600, parent=cw)
        ov.set_new_key_callback(self.on_remote_clicked)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.closed.connect(lambda: setattr(self, '_remote_overlay', None))
        ov.show()
        self._remote_overlay = ov
        self._log.append_log(f"SYS: Remote key generated — manual: {manual or url}")

    # ── Auto-start ──────────────────────────────────────────────────────────────

    def _check_autostart(self) -> bool:
        """Returns True if auto-start is currently registered on this OS."""
        try:
            if _OS == "Windows":
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
                try:
                    winreg.QueryValueEx(key, "JARVIS_AI")
                    return True
                except FileNotFoundError:
                    return False
                finally:
                    winreg.CloseKey(key)
            elif _OS == "Darwin":
                return (Path.home() / "Library" / "LaunchAgents"
                        / "com.jarvis.assistant.plist").exists()
            else:
                return (Path.home() / ".config" / "autostart" / "jarvis.desktop").exists()
        except Exception:
            return False

    def _toggle_autostart(self):
        currently_on = self._check_autostart()
        try:
            script = str(Path(__file__).resolve().parent / "main.py")
            if _OS == "Windows":
                import winreg
                reg = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_ALL_ACCESS)
                if currently_on:
                    winreg.DeleteValue(reg, "JARVIS_AI")
                else:
                    pythonw = Path(sys.executable).parent / "pythonw.exe"
                    exe = str(pythonw if pythonw.exists() else sys.executable)
                    winreg.SetValueEx(reg, "JARVIS_AI", 0, winreg.REG_SZ,
                                      f'"{exe}" "{script}"')
                winreg.CloseKey(reg)
            elif _OS == "Darwin":
                plist_dir = Path.home() / "Library" / "LaunchAgents"
                plist_dir.mkdir(parents=True, exist_ok=True)
                plist = plist_dir / "com.jarvis.assistant.plist"
                if currently_on:
                    plist.unlink(missing_ok=True)
                else:
                    plist.write_text(
                        '<?xml version="1.0" encoding="UTF-8"?>\n'
                        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                        '<plist version="1.0"><dict>\n'
                        '  <key>Label</key><string>com.jarvis.assistant</string>\n'
                        '  <key>ProgramArguments</key><array>\n'
                        f'    <string>{sys.executable}</string>\n'
                        f'    <string>{script}</string>\n'
                        '  </array>\n'
                        '  <key>RunAtLoad</key><true/>\n'
                        '</dict></plist>\n'
                    )
            else:
                desk_dir = Path.home() / ".config" / "autostart"
                desk_dir.mkdir(parents=True, exist_ok=True)
                desk = desk_dir / "jarvis.desktop"
                if currently_on:
                    desk.unlink(missing_ok=True)
                else:
                    desk.write_text(
                        "[Desktop Entry]\n"
                        f"Name={self._assistant_name}\n"
                        f"Exec={sys.executable} {script}\n"
                        "Type=Application\nTerminal=false\n"
                        "X-GNOME-Autostart-enabled=true\n"
                    )
            enabled = not currently_on
            self._update_autostart_btn(enabled)
            self._log.append_log(
                f"SYS: Auto-start {'enabled' if enabled else 'disabled'}.")
        except Exception as e:
            self._log.append_log(f"ERR: Auto-start failed — {e}")

    def _update_autostart_btn(self, enabled: bool):
        if not hasattr(self, '_autostart_btn'):
            return
        if enabled:
            self._autostart_btn.setText("◉  AUTO-START: ON")
            self._autostart_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 8px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            self._autostart_btn.setText("◉  AUTO-START: OFF")
            self._autostart_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 8px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    def _toggle_brief(self):
        from memory.config_manager import get_brief_enabled, save_brief_enabled
        new_val = not get_brief_enabled()
        save_brief_enabled(new_val)
        self._update_brief_btn(new_val)

    def _update_brief_btn(self, enabled: bool):
        if not hasattr(self, '_brief_btn'):
            return
        if enabled:
            self._brief_btn.setText("☀  MORNING BRIEF: ON")
            self._brief_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 8px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            self._brief_btn.setText("☀  MORNING BRIEF: OFF")
            self._brief_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 8px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    # ── Customization ────────────────────────────────────────────────────────────

    def _open_customize(self):
        cfg = _read_full_config()
        if self._customize_overlay:
            self._customize_overlay.hide()
        cw = self.centralWidget()
        ov = CustomizeOverlay(
            cfg.get("assistant_name", "JARVIS") or "JARVIS",
            cfg.get("user_name", ""),
            cfg.get("ui_color", "") or DEFAULT_UI_COLOR,
            parent=cw,
        )
        ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
        oh = min(oh, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.on_preview = self._preview_ui_color
        ov.saved.connect(self._apply_name_update)
        ov.show()
        self._customize_overlay = ov

    def _preview_ui_color(self, hex_color: str):
        """Canlı önizleme — tüm arayüzü yeni renge boyar (config'e YAZMAZ)."""
        old = current_palette()
        if apply_ui_accent(hex_color):
            retheme_all_widgets(old, current_palette())

    def _apply_name_update(self, name: str, user_name: str, ui_color: str = ""):
        """Update all name/theme-dependent UI elements and persist to config."""
        self._assistant_name = name.strip() or "JARVIS"
        display = self._assistant_name.upper()
        self.setWindowTitle(f"{display} — MARK L")
        self._title_lbl.setText(display)
        if display in ("JARVIS", "J.A.R.V.I.S"):
            self._sub_lbl.setText("Just A Rather Very Intelligent System")
        else:
            self._sub_lbl.setText("Personal AI Assistant")
        self._log._ai_name_lc = self._assistant_name.lower()
        self.hud._assistant_name = display

        color_changed = False
        if ui_color:
            old = current_palette()
            if apply_ui_accent(ui_color):
                # Tüm arayüzü (paneller, butonlar, kenarlıklar, HUD) canlı boya
                retheme_all_widgets(old, current_palette())
                color_changed = old["PRI"] != C.PRI

        try:
            data = _read_full_config()
            data["assistant_name"] = self._assistant_name
            data["user_name"] = user_name.strip()
            if ui_color:
                data["ui_color"] = ui_color.strip().lower()
            API_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
            self._log.append_log(f"SYS: Identity updated — {display}")
            if color_changed:
                self._log.append_log(f"SYS: UI colour applied — {ui_color}")
        except Exception as e:
            self._log.append_log(f"ERR: Config save failed — {e}")

    # ── Clipboard intelligence ───────────────────────────────────────────────────

    def _on_clipboard_changed(self):
        try:
            text = QApplication.clipboard().text().strip()
            if len(text) >= 10:
                self._clipboard_sig.emit(text)
        except Exception:
            pass

    def _show_clipboard_panel(self, text: str):
        self._clipboard_panel.show_clipboard(text)
        self._position_clipboard_panel()

    def _position_clipboard_panel(self):
        cw = self.centralWidget()
        pw = ClipboardPanel._W
        ph = self._clipboard_panel.sizeHint().height() or ClipboardPanel._H
        x = (cw.width() - pw) // 2
        y = cw.height() - ph - 6
        self._clipboard_panel.setGeometry(x, y, pw, ph)
        self._clipboard_panel.raise_()

    def _on_clipboard_action(self, cmd: str):
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(cmd,), daemon=True).start()

    # ────────────────────────────────────────────────────────────────────────────

    def _do_interrupt(self):
        if hasattr(self, "_palette") and self._palette.isVisible():
            self._palette.close_palette(); return
        if self.on_interrupt:
            self.on_interrupt()

    def _toggle_mute(self):
        self._muted = not self._muted
        self.hud.muted = self._muted
        self._style_mute_btn()
        if self._muted:
            self._apply_state("MUTED")
            self._log.append_log("SYS: Microphone muted.")
            if hasattr(self, "_toast"): self._toast.show_msg("Microphone muted  ·  F4 to resume", C.MUTED_C)
        else:
            self._apply_state("LISTENING")
            self._log.append_log("SYS: Microphone active.")
            if hasattr(self, "_toast"): self._toast.show_msg("Microphone live", C.GREEN)

    def _style_mute_btn(self):
        if self._muted:
            self._mute_btn.setText("🔇  MICROPHONE MUTED")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #140006; color: {C.MUTED_C};
                    border: 1px solid {C.MUTED_C}; border-radius: 8px;
                }}
            """)
        else:
            self._mute_btn.setText("🎙  MICROPHONE ACTIVE")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #00140a; color: {C.GREEN};
                    border: 1px solid {C.GREEN}; border-radius: 8px;
                }}
                QPushButton:hover {{ background: #001f10; }}
            """)

    def _open_gods_eye(self):
        self._toast.show_msg("Launching God's Eye View…")
        def _run():
            try:
                from actions.gods_eye import launch
                self._log.append_log(f"SYS: {launch()}")
            except Exception as e:
                self._log.append_log(f"SYS: God's Eye error: {e}")
        threading.Thread(target=_run, daemon=True).start()

    def _focus_input(self):
        self._input.setFocus(); self._input.selectAll()

    def _clear_log(self):
        self._log.clear_log()
        self._toast.show_msg("Conversation log cleared")

    def _send_text(self, txt: str):
        self._input.setText(txt)
        self._send()

    def _send(self):
        txt = self._input.text().strip()
        if not txt: return
        self._input.clear()
        self._input.remember(txt)
        self._log.append_log(f"You: {txt}")
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(txt,), daemon=True).start()

    def _apply_state(self, state: str):
        self.hud.state    = state
        self.hud.speaking = (state == "SPEAKING")

    def _check_config(self) -> bool:
        if not API_FILE.exists(): return False
        try:
            d = json.loads(API_FILE.read_text(encoding="utf-8"))
            return bool(d.get("gemini_api_key")) and bool(d.get("os_system"))
        except Exception:
            return False

    def _show_setup(self):
        ov = SetupOverlay(self.centralWidget())
        cw = self.centralWidget()
        ow, oh = 460, 390
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.done.connect(self._on_setup_done)
        ov.show()
        self._overlay = ov

    def _on_setup_done(self, key: str, os_name: str):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        API_FILE.write_text(
            json.dumps({"gemini_api_key": key, "os_system": os_name}, indent=4),
            encoding="utf-8",
        )
        self._ready = True
        if self._overlay:
            self._overlay.hide()
            self._overlay = None
        self._apply_state("LISTENING")
        self._assistant_name = _read_full_config().get("assistant_name", "JARVIS") or "JARVIS"
        self._log.append_log(f"SYS: Initialised. OS={os_name.upper()}. {self._assistant_name} online.")

class _RootShim:
    """Tiny adapter so main.py's ui.root.mainloop() call (a holdover-style
    API) can drive a real QApplication.exec() underneath without main.py
    needing to know it's talking to Qt specifically."""
    def __init__(self, app: QApplication):
        self._app = app
    def mainloop(self):
        self._app.exec()
    def protocol(self, *_):
        pass


class JarvisUI:
    """
    The public interface this whole file exposes to main.py — everything
    else in ui.py is an implementation detail behind this class. main.py
    does `ui = JarvisUI("face.png")`, then `JarvisLive(ui)` wires its own
    callbacks onto `ui.on_text_command` / `ui.on_remote_clicked` /
    `ui.on_interrupt`, and drives the rest through plain method calls like
    ui.write_log(...) / ui.set_state(...) / ui.show_content(...) — each of
    which is safe to call from any thread (see MainWindow's docstring for
    why: they just emit signals under the hood).
    """
    def __init__(self, face_path: str, size=None):
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyle("Fusion")
        self._win = MainWindow(face_path)
        self._win.show()
        self.root = _RootShim(self._app)

    @property
    def muted(self) -> bool:
        return self._win._muted

    @muted.setter
    def muted(self, v: bool):
        if v != self._win._muted:
            self._win._toggle_mute()

    @property
    def current_file(self) -> str | None:
        return self._win._drop_zone.current_file()

    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._win.on_text_command = cb

    @property
    def on_remote_clicked(self):
        return self._win.on_remote_clicked

    @on_remote_clicked.setter
    def on_remote_clicked(self, cb):
        self._win.on_remote_clicked = cb

    @property
    def on_interrupt(self):
        return self._win.on_interrupt

    @on_interrupt.setter
    def on_interrupt(self, cb):
        self._win.on_interrupt = cb

    def notify_phone_connected(self) -> None:
        self._win.notify_phone_connected()

    def set_state(self, state: str):
        self._win._state_sig.emit(state)

    def write_log(self, text: str):
        self._win._log_sig.emit(text)

    def wait_for_api_key(self):
        while not self._win._ready:
            time.sleep(0.1)

    def show_content(self, title: str, text: str):
        """Thread-safe: display content in the panel below the HUD."""
        self._win._content_sig.emit(title[:48], text[:4000])

    def prompt_reconfig(self):
        """Thread-safe: show the API key setup overlay (e.g. after an auth error)."""
        self._win._ready = False
        self._win._reconfig_sig.emit()

    def show_camera_frame(self, img_bytes: bytes):
        """Thread-safe: show a webcam frame in the small overlay (screen captures)."""
        self._win._camera_sig.emit(img_bytes)

    def start_camera_stream(self) -> None:
        """Thread-safe: start live camera feed in the full HUD area."""
        self._win.start_camera_stream()

    def stop_camera_stream(self) -> None:
        """Thread-safe: stop the live camera feed."""
        self._win.stop_camera_stream()

    @property
    def assistant_name(self) -> str:
        return self._win._assistant_name

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")