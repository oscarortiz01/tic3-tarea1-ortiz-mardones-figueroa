#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
TIC3 — Sistema de Adquisición ESP32 + BMI270 gestionado desde Raspberry Pi
Servidor de Sockets (TCP/UDP) + "Flat Telemetry Dashboard" (PyQt5 + PyQtGraph)
===============================================================================
Requisitos funcionales cubiertos:
  - RF1: Adquisición y visualización en tiempo real de los 3 ejes de aceleración
         (X, Y, Z) y giroscopio desde el sensor Bosch BMI270.
  - RF2 / RF3: Selector de modo Secuencial (bloqueante) vs Paralelo (tareas
         concurrentes FreeRTOS en Core 0 y Core 1).
  - RF4: Conectividad Wi-Fi (diseñado para escuchar en todas las interfaces 0.0.0.0,
         compatible con Hotspot TIC3_RED 10.42.0.1 y Wi-Fi de laboratorio).
  - RF5: Servidor de sockets residente siempre en la Raspberry Pi; el ESP32 es cliente.
  - RF6: Protocolo de transporte seleccionable en caliente (TCP por defecto o UDP)
         sin reescribir código.
  - RF7: Configuración remota de frecuencia de muestreo (ODR), rango/sensibilidad
         del BMI270 (±2g, ±4g, ±8g, ±16g), cantidad de muestras y modo de lectura.
  - RF8: GUI interactiva con gráficas limpias en tiempo real y guardado automático
         de sesiones completas en la carpeta 'sensores/'.
  - RF9: Tráfico observable y diferenciable en Wireshark (TCP vs UDP).

Requisitos no funcionales de calidad y robustez:
  - Arquitectura modular desacoplada en tres capas bien definidas:
      1. Capa de datos    -> AcquisitionStore (buffer circular thread-safe),
                             SessionCSVWriter (escritura formateada y auditada).
      2. Capa de red      -> TIC3MultiServer (sockets TCP/UDP en hilos dedicados,
                             select no bloqueante, SO_REUSEADDR y TCP keepalive).
      3. Capa de GUI      -> TelemetryDashboard (PyQt5 + PyQtGraph con refresco
                             por QTimer a ~25 FPS, sin saturar la CPU de la RPi).
  - Tolerancia a desconexiones: El servidor reacciona ante reinicios o pérdidas de señal
    del ESP32 sin bloquearse ni caerse, aceptando reconexiones inmediatamente.
  - Formato estricto en sensores/: Cada muestra registra timestamp, sample_id, t_us,
    ejes físicos (X, Y, Z de aceleración y giroscopio), ODR, rango, modo y protocolo.
===============================================================================
"""

import os
import sys
import json
import time
import socket
import select
import threading
import subprocess
from datetime import datetime, timedelta

import numpy as np

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt, pyqtSignal
import pyqtgraph as pg

# =============================================================================
# CONSTANTES Y CONFIGURACIÓN DEL SISTEMA
# =============================================================================
DEFAULT_PORT = 8080
GUI_UPDATE_MS = 40            # Refresco de gráficas: 25 FPS (desacoplado de la red)
IP_REFRESH_MS = 3000          # Período de refresco de la IP en la interfaz
PLOT_CAPACITY = 8192          # Buffer circular de graficado
PLOT_WINDOW_S = 3.0           # Ventana temporal visible en segundos
UDP_GRACE_MS = 600            # Espera de datagramas UDP rezagados al finalizar
MAX_SAMPLES = 1_000_000

ODR_OPTIONS = ["12.5 Hz", "25 Hz", "50 Hz", "100 Hz", "200 Hz", "400 Hz", "800 Hz", "1600 Hz"]
RANGE_OPTIONS = ["±2g", "±4g", "±8g", "±16g"]

# Resolución portable de la carpeta de guardado (compatible con repo raíz y con Desktop)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
parent_sensores = os.path.join(os.path.dirname(SCRIPT_DIR), "sensores")
SAVE_DIR = parent_sensores if os.path.isdir(parent_sensores) else os.path.join(SCRIPT_DIR, "sensores")
os.makedirs(SAVE_DIR, exist_ok=True)

# Paleta "Flat Telemetry" (Tema Oscuro Moderno Catppuccin Mocha / Grafana)
C = {
    "bg": "#11111B",          # Fondo principal de la ventana
    "card": "#181825",        # Fondo de tarjetas y paneles
    "border": "#27273A",      # Bordes sutiles modernos
    "input": "#232334",       # Fondo de inputs y botones secundarios
    "text": "#CDD6F4",        # Texto principal de alto contraste
    "subtext": "#A6ADC8",     # Texto secundario y etiquetas
    "muted": "#585B70",       # Elementos deshabilitados
    "accent": "#89B4FA",      # Color de acento (azul claro)
    "green": "#10B981",       # Verde esmeralda (adquisición activa / éxito)
    "red": "#EF4444",         # Rojo vibrante (detener adquisición)
    "yellow": "#FBBF24",      # Ámbar / amarillo (escuchando / espera)
    "x": "#38BDF8",           # Eje X: Cyan
    "y": "#F43F5E",           # Eje Y: Coral / Magenta
    "z": "#FBBF24",           # Eje Z: Ámbar
}
MONO_FONT = "DejaVu Sans Mono"


def get_local_ip():
    """
    Detecta la dirección IP activa de la Raspberry Pi de forma dinámica y no invasiva.
    Prioriza la IP del Hotspot 'TIC3_RED' (10.42.0.1) si está activo, o la IP Wi-Fi
    asignada por el router del laboratorio, sin alterar la configuración de red.
    """
    try:
        res = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=1)
        ips = [ip for ip in res.stdout.split() if ":" not in ip and not ip.startswith("127.")]
        if "10.42.0.1" in ips:
            return "10.42.0.1"
        if ips:
            return ips[0]
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# =============================================================================
# CAPA 1: DATOS Y ALMACENAMIENTO (DATA LAYER)
# =============================================================================
class SessionCSVWriter:
    """
    Gestor de persistencia en disco de las sesiones de telemetría en 'sensores/'.
    Cumple el Requisito No Funcional: cada fila contiene timestamp, modo de lectura,
    frecuencia de muestreo (ODR), rango/sensibilidad del BMI270, sample_id, t_us,
    aceleraciones en los 3 ejes (X, Y, Z) y velocidad angular (X, Y, Z).
    """

    HEADER = (
        "timestamp,sample_id,t_us,acc_x_ms2,acc_y_ms2,acc_z_ms2,"
        "gyr_x_rads,gyr_y_rads,gyr_z_rads,odr_hz,range_g,mode,protocol,target_samples\n"
    )

    def __init__(self, save_dir, odr, acc_range, mode, protocol, target_samples):
        self.odr = odr
        self.acc_range = acc_range
        self.mode = mode
        self.protocol = protocol
        self.target_samples = target_samples
        os.makedirs(save_dir, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.filename = f"sesion_{ts}_odr{odr}hz_range{acc_range}g_{mode}_{protocol}.csv"
        self.filepath = os.path.join(save_dir, self.filename)
        self.samples_written = 0

        self.file = open(self.filepath, "w", encoding="utf-8")
        self.file.write(self.HEADER)
        self.file.flush()
        self._suffix = f",{odr},{acc_range},{mode},{protocol},{target_samples}\n"
        self.t0_wall = None
        self.t0_us = None

    def write_rows(self, rows):
        """Escribe un lote de muestras en disco. rows: ndarray (N, 8) -> [id, t_us, ax, ay, az, gx, gy, gz]."""
        if self.file is None or len(rows) == 0:
            return
        if self.t0_wall is None:
            self.t0_wall = datetime.now()
            self.t0_us = int(rows[0][1])

        lines = []
        for r in rows:
            dt_us = int(r[1]) - self.t0_us
            if 0 <= dt_us < 86400 * 1_000_000:
                ts_str = (self.t0_wall + timedelta(microseconds=dt_us)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            else:
                ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            lines.append(
                f"{ts_str},{int(r[0])},{int(r[1])},{r[2]:.4f},{r[3]:.4f},{r[4]:.4f},"
                f"{r[5]:.4f},{r[6]:.4f},{r[7]:.4f}{self._suffix}"
            )
        self.file.write("".join(lines))
        self.file.flush()
        self.samples_written += len(rows)

    def close(self):
        """Cierra el archivo de forma segura y devuelve el total de muestras guardadas."""
        if self.file:
            self.file.flush()
            self.file.close()
            self.file = None
        return self.samples_written


class AcquisitionStore:
    """
    Buffer circular thread-safe que conecta los hilos de red con la interfaz gráfica.
    - Deduplica paquetes mediante máscara booleana sobre los sample_id generados por el ESP32.
    - Desenvuelve el contador de microsegundos t_us (uint32) para evitar discontinuidades.
    - Calcula la frecuencia real de muestreo (tasa en Hz) directamente con los timestamps
      del hardware del ESP32, eliminando el jitter inducido por la red Wi-Fi.
    - Proporciona instantáneas (snapshot) a la GUI mediante NumPy sin copias innecesarias.
    """

    def __init__(self, capacity=PLOT_CAPACITY):
        self.lock = threading.Lock()
        self.capacity = capacity
        self.reset(0, 100)

    def reset(self, target, odr):
        with self.lock:
            self.target = int(target)
            self.odr = float(odr)
            self.buf = np.zeros((self.capacity, 7), dtype=np.float64)  # [t_rel_s, ax, ay, az, gx, gy, gz]
            self.widx = 0
            self.count = 0
            self.version = 0
            self.received = 0
            self.duplicates = 0
            self.blocks = set()
            self.seen = bytearray(self.target)
            self.id_min = self.id_max = None
            self.t_at_min = self.t_at_max = None
            self.t0 = None
            self._wrap_off = 0
            self._last_raw = None

    def _unwrap_t(self, raw):
        """Desenrolla el desbordamiento de uint32 de t_us (ocurre cada ~71.5 minutos)."""
        prev = raw[0] if self._last_raw is None else self._last_raw
        seq = np.concatenate(([prev], raw))
        wraps = np.cumsum(np.diff(seq) < -(1 << 31)).astype(np.int64) * (1 << 32)
        t = raw + self._wrap_off + wraps
        self._wrap_off += int(wraps[-1])
        self._last_raw = int(raw[-1])
        return t

    def ingest(self, block_id, arr):
        """
        Ingesta un bloque de muestras recibido por TCP o UDP.
        Devuelve (filas_validas_guardables, meta_alcanzada).
        """
        with self.lock:
            self.blocks.add(int(block_id))
            ids = arr[:, 0].astype(np.int64)
            valid = (ids >= 0) & (ids < self.target)
            arr, ids = arr[valid], ids[valid]

            if len(ids):
                seen_np = np.frombuffer(self.seen, dtype=np.uint8)
                fresh = seen_np[ids] == 0
                _, first_idx = np.unique(ids, return_index=True)
                in_block_first = np.zeros(len(ids), dtype=bool)
                in_block_first[first_idx] = True
                keep = fresh & in_block_first
                self.duplicates += int(len(ids) - keep.sum())
                arr, ids = arr[keep], ids[keep]

            room = max(0, self.target - self.received)
            if len(ids) > room:
                arr, ids = arr[:room], ids[:room]

            if len(ids) == 0:
                return arr, self.received >= self.target > 0

            np.frombuffer(self.seen, dtype=np.uint8)[ids] = 1
            self.received += len(ids)

            t_us = self._unwrap_t(arr[:, 1].astype(np.int64))
            if self.t0 is None:
                self.t0 = int(t_us[0])

            i_lo, i_hi = int(np.argmin(ids)), int(np.argmax(ids))
            if self.id_min is None or ids[i_lo] < self.id_min:
                self.id_min, self.t_at_min = int(ids[i_lo]), int(t_us[i_lo])
            if self.id_max is None or ids[i_hi] > self.id_max:
                self.id_max, self.t_at_max = int(ids[i_hi]), int(t_us[i_hi])

            # Inserción en buffer circular para gráficas
            rows = np.empty((len(ids), 7))
            rows[:, 0] = (t_us - self.t0) / 1e6
            rows[:, 1:] = arr[:, 2:8]
            n = len(rows)
            if n >= self.capacity:
                rows = rows[-self.capacity:]
                n = self.capacity

            end = self.widx + n
            if end <= self.capacity:
                self.buf[self.widx:end] = rows
            else:
                k = self.capacity - self.widx
                self.buf[self.widx:] = rows[:k]
                self.buf[:n - k] = rows[k:]

            self.widx = end % self.capacity
            self.count = min(self.capacity, self.count + n)
            self.version += 1
            return arr, self.received >= self.target

    def snapshot(self, n):
        """Retorna las últimas n muestras ordenadas temporalmente y métricas de adquisición."""
        with self.lock:
            n = min(n, self.count)
            idx = (np.arange(self.widx - n, self.widx)) % self.capacity
            data = self.buf[idx].copy()
            rate = 0.0
            if self.id_max is not None and self.id_max > self.id_min:
                dt = self.t_at_max - self.t_at_min
                if dt > 0:
                    rate = (self.id_max - self.id_min) * 1e6 / dt
            span = (self.id_max - self.id_min + 1) if self.id_max is not None else 0
            stats = {
                "received": self.received,
                "target": self.target,
                "blocks": len(self.blocks),
                "rate": rate,
                "lost": max(0, span - self.received),
                "span": span,
                "duplicates": self.duplicates,
                "version": self.version,
            }
        return data, stats


# =============================================================================
# CAPA 2: COMUNICACIÓN Y RED (NETWORK LAYER)
# =============================================================================
class ServerSignals(QtCore.QObject):
    """Canal de señales Qt para comunicación segura entre hilos de red y la GUI."""
    status = pyqtSignal(str, str)
    goal_reached = pyqtSignal()


class TIC3MultiServer:
    """
    Servidor concurrente de sockets (TCP y UDP) enlazado a 0.0.0.0:<puerto>.
    - Utiliza SO_REUSEADDR en ambos sockets para permitir reanudación inmediata del puerto.
    - Hilos de recepción independientes que previenen cualquier congelamiento de la GUI.
    - Manejo no bloqueante mediante select() para tolerar desconexiones y reinicios del ESP32.
    - Soporta conmutación transparente en caliente entre TCP y UDP.
    """

    def __init__(self, port, store, signals):
        self.port = port
        self.store = store
        self.signals = signals
        self.running = False
        self.srv_sock = None
        self.client_sock = None
        self.client_addr = None
        self.client_lock = threading.Lock()
        self.session_lock = threading.Lock()
        self.session_active = False
        self.csv_writer = None
        self.protocol = "TCP"
        self._goal_emitted = False
        self.udp_sock = None
        self.udp_stop = threading.Event()
        self.udp_thread = None
        self.tcp_thread = None

    def start(self):
        """Inicia el servidor TCP de control y escucha en 0.0.0.0."""
        self.srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv_sock.bind(("0.0.0.0", self.port))
        self.srv_sock.listen(2)
        self.running = True
        self.tcp_thread = threading.Thread(target=self._tcp_loop, name="tcp_srv", daemon=True)
        self.tcp_thread.start()
        self.signals.status.emit("ESPERANDO_CLIENTE", f"Escuchando en 0.0.0.0:{self.port}")

    def stop(self):
        """Detiene de forma limpia los sockets del servidor y libera recursos."""
        self.running = False
        self.stop_acquisition(send_stop=False)
        self._drop_client(notify=False)
        if self.srv_sock:
            try:
                self.srv_sock.close()
            except OSError:
                pass
            self.srv_sock = None
        if self.tcp_thread and self.tcp_thread is not threading.current_thread():
            self.tcp_thread.join(timeout=1.5)
            self.tcp_thread = None

    @property
    def connected(self):
        return self.client_sock is not None

    def _tcp_loop(self):
        rxbuf = b""
        while self.running:
            with self.client_lock:
                cli = self.client_sock
            rlist = [self.srv_sock] + ([cli] if cli else [])
            try:
                readable, _, _ = select.select(rlist, [], [], 0.5)
            except (OSError, ValueError):
                if not self.running:
                    break
                time.sleep(0.05)
                continue

            if self.srv_sock in readable:
                try:
                    sock, addr = self.srv_sock.accept()
                except OSError:
                    continue
                self._configure_client(sock)
                if self.client_sock:
                    self._drop_client(notify=False)
                with self.client_lock:
                    self.client_sock, self.client_addr = sock, addr
                rxbuf = b""
                self.signals.status.emit("CLIENTE_CONECTADO", f"{addr[0]}:{addr[1]}")
                continue

            if cli is not None and cli in readable:
                try:
                    data = cli.recv(65536)
                except OSError:
                    data = b""
                if not data:
                    self._drop_client(notify=True)
                    rxbuf = b""
                    continue
                rxbuf += data
                if b"\n" in rxbuf:
                    *lines, rxbuf = rxbuf.split(b"\n")
                    for line in lines:
                        if line.strip():
                            self._process_line(line)
                if len(rxbuf) > 8 * 1024 * 1024:
                    rxbuf = b""

    @staticmethod
    def _configure_client(sock):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for opt, val in (("TCP_KEEPIDLE", 5), ("TCP_KEEPINTVL", 2), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, opt):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)

    def _drop_client(self, notify):
        with self.client_lock:
            cli, self.client_sock, self.client_addr = self.client_sock, None, None
        if cli:
            try:
                cli.close()
            except OSError:
                pass
            if notify and self.running:
                self.signals.status.emit("CLIENTE_PERDIDO", f"ESP32 desconectado. Escuchando en puerto {self.port}")

    def send_json(self, obj):
        with self.client_lock:
            cli = self.client_sock
        if not cli:
            raise RuntimeError("No hay ningún ESP32 conectado al servidor.")
        cli.sendall((json.dumps(obj) + "\n").encode("utf-8"))

    def _start_udp(self):
        self._stop_udp()
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        self.udp_sock.bind(("0.0.0.0", self.port))
        self.udp_sock.settimeout(0.3)
        self.udp_stop.clear()
        self.udp_thread = threading.Thread(target=self._udp_loop, args=(self.udp_sock,),
                                           name="udp_rx", daemon=True)
        self.udp_thread.start()

    def _udp_loop(self, sock):
        while not self.udp_stop.is_set():
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            for line in data.split(b"\n"):
                if line.strip():
                    self._process_line(line)

    def _stop_udp(self):
        self.udp_stop.set()
        if self.udp_sock:
            try:
                self.udp_sock.close()
            except OSError:
                pass
            self.udp_sock = None
        if self.udp_thread and self.udp_thread is not threading.current_thread():
            self.udp_thread.join(timeout=1.0)
            self.udp_thread = None

    def _process_line(self, raw):
        try:
            msg = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return

        if msg.get("type") == "block":
            self._handle_block(msg)
            return

        st = msg.get("status")
        if st == "ready":
            self.signals.status.emit("CLIENTE_LISTO", "ESP32 listo para recibir configuración")
        elif st == "started":
            self.signals.status.emit("ADQUIRIENDO", "ESP32 confirmó inicio de adquisición")
        elif st == "stopped":
            self.signals.status.emit("ESP_DETENIDO", "ESP32 confirmó detención")
        elif st == "idle" or msg.get("message") == "session_completed":
            self.signals.status.emit("SESION_TERMINADA", "ESP32 completó la sesión")

    def _handle_block(self, msg):
        samples = msg.get("samples") or []
        if not samples:
            return
        try:
            arr = np.asarray(samples, dtype=np.float64)
            if arr.ndim != 2:
                raise ValueError
        except ValueError:
            arr = np.asarray([s for s in samples if len(s) in (4, 5, 7, 8) and len(s) == len(samples[0])],
                             dtype=np.float64)
            if arr.ndim != 2 or len(arr) == 0:
                return

        if arr.shape[1] == 4:
            # [id, ax, ay, az] -> insert t_us=0, gyr=[0, 0, 0]
            arr = np.insert(arr, 1, 0.0, axis=1)
            arr = np.pad(arr, ((0, 0), (0, 3)), mode='constant')
        elif arr.shape[1] == 5:
            # [id, t_us, ax, ay, az] -> pad gyr=[0, 0, 0]
            arr = np.pad(arr, ((0, 0), (0, 3)), mode='constant')
        elif arr.shape[1] == 7:
            # [id, ax, ay, az, gx, gy, gz] -> insert t_us=0
            arr = np.insert(arr, 1, 0.0, axis=1)
        elif arr.shape[1] != 8:
            return

        with self.session_lock:
            if not self.session_active:
                return
            accepted, goal = self.store.ingest(msg.get("block", 0), arr)
            if self.csv_writer and len(accepted):
                self.csv_writer.write_rows(accepted)
            emit_goal = goal and not self._goal_emitted
            if emit_goal:
                self._goal_emitted = True

        if emit_goal:
            self.signals.goal_reached.emit()

    def start_acquisition(self, odr, acc_range, samples, protocol, mode):
        """Inicia una sesión de adquisición configurando el sensor y abriendo el CSV."""
        if not self.connected:
            raise RuntimeError("No hay ningún ESP32 conectado al servidor.")
        with self.session_lock:
            if self.csv_writer:
                self.csv_writer.close()
            self.store.reset(samples, odr if odr >= 25 else 12.5)
            self.csv_writer = SessionCSVWriter(SAVE_DIR, odr, acc_range, mode, protocol, samples)
            self.protocol = protocol
            self._goal_emitted = False
            self.session_active = True

        try:
            if protocol == "UDP":
                self._start_udp()
            else:
                self._stop_udp()

            self.send_json({
                "cmd": "start",
                "odr": odr,
                "range": acc_range,
                "samples": samples,
                "protocol": protocol,
                "mode": mode,
                "port": self.port
            })
        except Exception:
            path, _ = self.stop_acquisition(send_stop=False)
            if path and os.path.exists(path):
                os.remove(path)
            raise
        return self.csv_writer.filepath

    def stop_acquisition(self, send_stop=True):
        """Detiene la sesión activa, cierra el archivo CSV y notifica al sensor."""
        with self.session_lock:
            was_active = self.session_active
            self.session_active = False
            path, count = "", 0
            if self.csv_writer:
                path = self.csv_writer.filepath
                count = self.csv_writer.close()
                self.csv_writer = None

        if send_stop and was_active and self.connected:
            try:
                self.send_json({"cmd": "stop"})
            except (OSError, RuntimeError):
                pass

        self._stop_udp()
        return path, count


# =============================================================================
# CAPA 3: INTERFAZ GRÁFICA FLAT TELEMETRY (GUI LAYER)
# =============================================================================
STYLESHEET = f"""
* {{
    font-family: 'DejaVu Sans', 'Segoe UI', sans-serif;
    font-size: 10pt;
    color: {C['text']};
}}
QMainWindow, QWidget#root {{
    background: {C['bg']};
}}
QScrollArea, QScrollArea > QWidget > QWidget {{
    background: transparent;
    border: none;
}}
QFrame#header {{
    background: {C['card']};
    border-bottom: 1px solid {C['border']};
}}
QLabel#title {{
    font-size: 14pt;
    font-weight: bold;
    color: #FFFFFF;
    letter-spacing: 0.5px;
}}
QLabel#subtitle {{
    font-size: 8.5pt;
    color: {C['subtext']};
}}
QFrame#card {{
    background: {C['card']};
    border: 1px solid {C['border']};
    border-radius: 8px;
}}
QLabel {{
    background: transparent;
    border: none;
}}
QLabel#sectionTitle {{
    color: {C['accent']};
    font-size: 8.5pt;
    font-weight: bold;
}}
QLabel#fieldLabel {{
    color: {C['subtext']};
    font-size: 8.5pt;
}}
QFrame#pill {{
    background: {C['input']};
    border: 1px solid {C['border']};
    border-radius: 13px;
}}
QLabel#pillText {{
    font-size: 9pt;
    font-weight: bold;
}}
QComboBox, QLineEdit {{
    background: {C['input']};
    border: 1px solid {C['border']};
    border-radius: 6px;
    padding: 4px 6px;
    min-height: 20px;
    color: #FFFFFF;
    font-size: 9.5pt;
    font-weight: bold;
    selection-background-color: {C['accent']};
    selection-color: {C['bg']};
}}
QComboBox:hover, QLineEdit:hover {{
    border: 1px solid #3B3B55;
}}
QComboBox:focus, QLineEdit:focus, QComboBox:on {{
    border: 1px solid {C['accent']};
}}
QComboBox:disabled, QLineEdit:disabled {{
    color: {C['muted']};
    background: #1C1C2B;
    border: 1px solid {C['border']};
}}
QComboBox::drop-down {{
    border: none;
    width: 16px;
}}
QComboBox::down-arrow {{
    image: none;
    width: 0;
    height: 0;
    margin-right: 4px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {C['accent']};
}}
QComboBox QAbstractItemView {{
    background: {C['input']};
    color: {C['text']};
    border: 1px solid {C['accent']};
    selection-background-color: {C['accent']};
    selection-color: {C['bg']};
    outline: 0;
    padding: 4px;
}}
QPushButton {{
    background: {C['input']};
    border: 1px solid {C['border']};
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 9.5pt;
    font-weight: bold;
    color: {C['text']};
}}
QPushButton:hover {{
    border: 1px solid {C['accent']};
}}
QPushButton:disabled {{
    background: {C['border']};
    color: {C['muted']};
    border: 1px solid {C['border']};
}}
QPushButton#btnStart {{
    background: {C['green']};
    color: #FFFFFF;
    border: none;
    font-size: 10.5pt;
    font-weight: bold;
    padding: 8px;
    border-radius: 6px;
}}
QPushButton#btnStart:hover {{
    background: #059669;
}}
QPushButton#btnStart:disabled {{
    background: {C['border']};
    color: {C['muted']};
}}
QPushButton#btnStop {{
    background: {C['red']};
    color: #FFFFFF;
    border: none;
    font-size: 10.5pt;
    font-weight: bold;
    padding: 8px;
    border-radius: 6px;
}}
QPushButton#btnStop:hover {{
    background: #DC2626;
}}
QPushButton#btnStop:disabled {{
    background: {C['border']};
    color: {C['muted']};
    border: 1px solid {C['border']};
}}
QProgressBar {{
    background: {C['input']};
    border: 1px solid {C['border']};
    border-radius: 6px;
    min-height: 18px;
    text-align: center;
    color: #FFFFFF;
    font-weight: bold;
    font-size: 8pt;
}}
QProgressBar::chunk {{
    border-radius: 5px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {C['accent']}, stop:1 {C['green']});
}}
QFrame#kpi {{
    background: {C['input']};
    border: 1px solid {C['border']};
    border-radius: 8px;
}}
QLabel#kpiValue {{
    font-family: '{MONO_FONT}', monospace;
    font-size: 16pt;
    font-weight: bold;
    color: #FFFFFF;
}}
QLabel#kpiLabel {{
    font-size: 7.5pt;
    font-weight: bold;
    color: {C['subtext']};
}}
QLabel#fileBox {{
    background: {C['bg']};
    border: 1px solid {C['border']};
    border-radius: 6px;
    padding: 5px 6px;
    font-family: '{MONO_FONT}', monospace;
    font-size: 7.5pt;
    color: {C['subtext']};
}}
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
}}
QScrollBar::handle:vertical {{
    background: {C['border']};
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
}}
"""


def make_card(title):
    """Genera una tarjeta plana con título en mayúsculas y acento moderno."""
    card = QtWidgets.QFrame(objectName="card")
    lay = QtWidgets.QVBoxLayout(card)
    lay.setContentsMargins(10, 5, 10, 6)
    lay.setSpacing(4)
    lbl = QtWidgets.QLabel(title.upper(), objectName="sectionTitle")
    f = lbl.font()
    f.setLetterSpacing(QtGui.QFont.AbsoluteSpacing, 1.2)
    lbl.setFont(f)
    lay.addWidget(lbl)
    return card, lay


def field(label, widget):
    box = QtWidgets.QVBoxLayout()
    box.setSpacing(2)
    box.addWidget(QtWidgets.QLabel(label, objectName="fieldLabel"))
    box.addWidget(widget)
    return box


class StatusPill(QtWidgets.QFrame):
    """Píldora de estado con indicador luminoso circular o ícono."""

    def __init__(self, text, color, dot_color=None):
        super().__init__(objectName="pill")
        self.setSizePolicy(QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Fixed)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 12, 4)
        lay.setSpacing(7)
        self.dot = QtWidgets.QLabel()
        self.dot.setFixedSize(10, 10)
        self.text = QtWidgets.QLabel(text, objectName="pillText")
        self.text.setSizePolicy(QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Preferred)
        lay.addWidget(self.dot)
        lay.addWidget(self.text)
        self.set(text, color, dot_color)

    def set(self, text, color, dot_color=None, icon=None):
        txt = f"{icon}  {text}" if icon else text
        self.text.setText(txt)
        self.text.setStyleSheet(f"color: {color}; font-weight: bold;")
        dc = dot_color or color
        self.dot.setVisible(icon is None)
        self.dot.setStyleSheet(
            f"background: {dc}; border-radius: 5px; min-width: 10px; max-width: 10px; min-height: 10px; max-height: 10px;"
        )
        self.text.adjustSize()
        self.updateGeometry()
        p = self.parentWidget()
        if p and p.layout():
            p.layout().activate()


class KpiCard(QtWidgets.QFrame):
    """Mini-tarjeta KPI con número grande y legible para proyector."""

    def __init__(self, label, color="#FFFFFF"):
        super().__init__(objectName="kpi")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(1)
        self.value = QtWidgets.QLabel("0", objectName="kpiValue", alignment=Qt.AlignCenter)
        self.value.setStyleSheet(f"color: {color};")
        self.label = QtWidgets.QLabel(label.upper(), objectName="kpiLabel", alignment=Qt.AlignCenter)
        lay.addWidget(self.value)
        lay.addWidget(self.label)

    def set(self, text):
        self.value.setText(text)


def style_plot(plot, title, y_label, x_label="Tiempo (s)", show_bottom_label=True):
    """Estiliza una gráfica PyQtGraph para integrarse perfectamente al dashboard."""
    plot.setBackground(C["card"])
    pi = plot.getPlotItem()
    pi.setTitle(f"<span style='color:{C['text']}; font-size:11pt; font-weight:bold'>{title}</span>")
    pi.showGrid(x=True, y=True, alpha=0.15)
    for ax_name in ("left", "bottom"):
        ax = pi.getAxis(ax_name)
        ax.setPen(pg.mkPen(C["border"]))
        ax.setTextPen(pg.mkPen(C["subtext"]))
    pi.setLabel("left", f"<span style='color:{C['subtext']}; font-size:9.5pt; font-weight:bold'>{y_label}</span>")
    if show_bottom_label:
        pi.setLabel("bottom", f"<span style='color:{C['subtext']}; font-size:9.5pt; font-weight:bold'>{x_label}</span>")
    pi.setClipToView(True)
    pi.setDownsampling(auto=True, mode="peak")
    pi.setMenuEnabled(False)
    pi.addLegend(offset=(-10, 10), labelTextColor=C["text"],
                 brush=pg.mkBrush(17, 17, 27, 210), pen=pg.mkPen(C["border"]))
    return pi


class TelemetryDashboard(QtWidgets.QMainWindow):
    """
    Ventana Principal: Dashboard Flat Telemetry para visualización de señales X, Y, Z.
    Integra controles de red, parámetros de sesión, indicadores KPI y gráficas en tiempo real.
    """

    def __init__(self, port=DEFAULT_PORT):
        super().__init__()
        self.setWindowTitle("TIC3 — Telemetría ESP32 + BMI270 (Raspberry Pi)")
        self.resize(1360, 700)
        self.setMinimumSize(1020, 600)

        self.store = AcquisitionStore()
        self.signals = ServerSignals()
        self.signals.status.connect(self._on_status)
        self.signals.goal_reached.connect(lambda: self._finalize("goal"))
        self.server = None
        self.port = port
        self.local_ip = get_local_ip()
        self.acquiring = False
        self.session_cfg = {}
        self.last_csv = ""
        self._last_version = -1
        self._tick = 0

        self._build_ui()
        self._start_server(port)

        self.plot_timer = QtCore.QTimer(self, interval=GUI_UPDATE_MS, timeout=self._refresh_plots)
        self.plot_timer.start()
        self.ip_timer = QtCore.QTimer(self, interval=IP_REFRESH_MS, timeout=self._refresh_ip)
        self.ip_timer.start()

    def _build_ui(self):
        root = QtWidgets.QWidget(objectName="root")
        self.setCentralWidget(root)
        outer = QtWidgets.QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())

        body = QtWidgets.QHBoxLayout()
        body.setContentsMargins(8, 8, 8, 8)
        body.setSpacing(8)
        outer.addLayout(body, 1)

        # Panel izquierdo con ancho cómodo para prevenir cortes
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedWidth(430)
        panel = QtWidgets.QWidget()
        panel.setMinimumWidth(400)
        scroll.setWidget(panel)
        self._build_side_panel(panel)
        self.scroll_area = scroll
        QtCore.QTimer.singleShot(50, lambda: scroll.verticalScrollBar().setValue(0))

        body.addWidget(scroll)
        body.addWidget(self._build_plots_panel(), 1)

    def _build_header(self):
        header = QtWidgets.QFrame(objectName="header")
        lay = QtWidgets.QHBoxLayout(header)
        lay.setContentsMargins(14, 6, 14, 6)
        lay.setSpacing(8)

        titles = QtWidgets.QVBoxLayout()
        titles.setSpacing(1)
        titles.addWidget(QtWidgets.QLabel("TIC3 · Telemetría Remota ESP32 + BMI270", objectName="title"))
        titles.addWidget(QtWidgets.QLabel(
            "Servidor TCP/UDP en Raspberry Pi · Acelerómetro + Giroscopio 6 ejes · I2C", objectName="subtitle"))
        lay.addLayout(titles)
        lay.addStretch(1)

        self.pill_status = StatusPill(f"Escuchando en puerto {self.port}", C["yellow"], dot_color=C["yellow"])
        self.pill_esp = StatusPill("ESP32 desconectado", C["muted"], dot_color=C["muted"])
        self.pill_ip = StatusPill(f"IP {self.local_ip} : {self.port}", C["accent"], dot_color=C["accent"])

        for p in (self.pill_status, self.pill_esp, self.pill_ip):
            p.setSizePolicy(QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Fixed)
            lay.addWidget(p)
        return header

    def _build_side_panel(self, panel):
        lay = QtWidgets.QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(6)

        # Notificación toast no invasiva
        self.toast = QtWidgets.QLabel(wordWrap=True)
        self.toast.setMinimumHeight(32)
        lay.addWidget(self.toast)
        self._notify("Iniciando servidor...", "info")

        # Tarjeta 1: Red y puerto
        card_net, cl_net = make_card("Red · Puerto del Servidor")
        row_port = QtWidgets.QHBoxLayout()
        row_port.setSpacing(6)
        self.ent_port = QtWidgets.QLineEdit(str(self.port))
        self.ent_port.setValidator(QtGui.QIntValidator(1024, 65535, self))
        self.btn_port = QtWidgets.QPushButton("Aplicar puerto")
        self.btn_port.clicked.connect(self._on_change_port)
        self.ent_port.returnPressed.connect(self._on_change_port)
        row_port.addWidget(self.ent_port, 1)
        row_port.addWidget(self.btn_port)
        cl_net.addLayout(row_port)
        lay.addWidget(card_net)

        # Tarjeta 2: Parámetros de sesión
        card_params, cl_params = make_card("Parámetros de Sesión")
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(5)

        self.cb_protocol = QtWidgets.QComboBox()
        self.cb_protocol.addItems(["TCP (defecto)", "UDP"])

        self.cb_mode = QtWidgets.QComboBox()
        self.cb_mode.addItems(["Paralelo (FreeRTOS)", "Secuencial"])

        self.cb_odr = QtWidgets.QComboBox()
        self.cb_odr.addItems(ODR_OPTIONS)
        self.cb_odr.setCurrentText("100 Hz")

        self.cb_range = QtWidgets.QComboBox()
        self.cb_range.addItems(RANGE_OPTIONS)
        self.cb_range.setCurrentText("±8g")

        self.ent_samples = QtWidgets.QLineEdit("1000")
        self.ent_samples.setValidator(QtGui.QIntValidator(1, MAX_SAMPLES, self))

        grid.addLayout(field("Protocolo (RF6)", self.cb_protocol), 0, 0)
        grid.addLayout(field("Modo (RF2 / RF3)", self.cb_mode), 0, 1)
        grid.addLayout(field("Frecuencia ODR", self.cb_odr), 1, 0)
        grid.addLayout(field("Rango / Sensibilidad", self.cb_range), 1, 1)
        grid.addLayout(field("Cantidad de muestras", self.ent_samples), 2, 0, 1, 2)
        grid.setColumnStretch(0, 4)
        grid.setColumnStretch(1, 5)
        cl_params.addLayout(grid)
        lay.addWidget(card_params)

        self.param_widgets = [
            self.cb_protocol, self.cb_mode, self.cb_odr, self.cb_range,
            self.ent_samples, self.ent_port, self.btn_port
        ]

        # Tarjeta 3: Control de adquisición
        card_ctrl, cl_ctrl = make_card("Control de Adquisición")
        cl_ctrl.setSpacing(6)
        self.btn_start = QtWidgets.QPushButton("▶  INICIAR ADQUISICIÓN", objectName="btnStart")
        self.btn_stop = QtWidgets.QPushButton("■  DETENER", objectName="btnStop")
        for b in (self.btn_start, self.btn_stop):
            b.setCursor(Qt.PointingHandCursor)
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_stop.setEnabled(False)
        cl_ctrl.addWidget(self.btn_start)
        cl_ctrl.addWidget(self.btn_stop)
        lay.addWidget(card_ctrl)

        # Tarjeta 4: Estadísticas en vivo y KPI
        card_stats, cl_stats = make_card("Estadísticas en Vivo")
        cl_stats.setSpacing(5)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setFormat("0 / 1000 muestras  (0%)")
        cl_stats.addWidget(self.progress)

        kpis = QtWidgets.QHBoxLayout()
        kpis.setSpacing(6)
        self.kpi_samples = KpiCard("Muestras", "#FFFFFF")
        self.kpi_rate = KpiCard("Tasa Real (Hz)", C["green"])
        self.kpi_blocks = KpiCard("Bloques (1 s)", C["accent"])
        for k in (self.kpi_samples, self.kpi_rate, self.kpi_blocks):
            kpis.addWidget(k, 1)
        cl_stats.addLayout(kpis)

        self.lbl_quality = QtWidgets.QLabel("Pérdidas: 0 (0.0 %) · Duplicados: 0", objectName="fieldLabel")
        cl_stats.addWidget(self.lbl_quality)
        cl_stats.addWidget(QtWidgets.QLabel("Último CSV guardado", objectName="fieldLabel"))

        self.lbl_file = QtWidgets.QLabel("—", objectName="fileBox", wordWrap=True)
        self.lbl_file.setTextInteractionFlags(Qt.TextSelectableByMouse)
        cl_stats.addWidget(self.lbl_file)

        btn_folder = QtWidgets.QPushButton("Abrir carpeta sensores")
        btn_folder.clicked.connect(self._on_open_folder)
        cl_stats.addWidget(btn_folder)
        lay.addWidget(card_stats)

        lay.addStretch(1)

    def _build_plots_panel(self):
        """Panel derecho con las 2 gráficas de señales temporales (X, Y, Z) integradas directamente."""
        card = QtWidgets.QFrame(objectName="card")
        lay = QtWidgets.QVBoxLayout(card)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(12)

        pg.setConfigOptions(antialias=False, background=C["card"], foreground=C["subtext"])

        self.pw_acc = pg.PlotWidget()
        self.pw_gyr = pg.PlotWidget()

        style_plot(self.pw_acc, "Aceleración Lineal (m/s²)", "m/s²", show_bottom_label=False)
        style_plot(self.pw_gyr, "Velocidad Angular (rad/s)", "rad/s", show_bottom_label=True)

        self.pw_gyr.setXLink(self.pw_acc)

        self.curves_acc = [
            self.pw_acc.plot(pen=pg.mkPen(C[a], width=2), name=f"Acc {a.upper()}")
            for a in ("x", "y", "z")
        ]
        self.curves_gyr = [
            self.pw_gyr.plot(pen=pg.mkPen(C[a], width=2), name=f"Gyr {a.upper()}")
            for a in ("x", "y", "z")
        ]

        lay.addWidget(self.pw_acc, 1)
        lay.addWidget(self.pw_gyr, 1)

        # Objeto de compatibilidad por si scripts de prueba antiguos consultan win.tabs
        class _TabsCompat:
            def setCurrentIndex(self, _): pass
            def currentIndex(self): return 0
        self.tabs = _TabsCompat()

        return card

    # ---------------------------------------------------------------- notificaciones
    def _notify(self, message, kind="info"):
        palette = {
            "info": ("ℹ", "#1E283D", C["accent"]),
            "success": ("✔", "#0F2E24", C["green"]),
            "warning": ("■", "#3B3420", C["yellow"]),
            "error": ("✖", "#3D1F28", C["red"]),
        }
        icon, bg, fg = palette.get(kind, palette["info"])
        self.toast.setText(f"{icon}  {message}")
        self.toast.setStyleSheet(
            f"background:{bg}; color:{fg}; border:1px solid {fg}; border-radius:8px;"
            f"padding:6px 10px; font-size:8.5pt; font-weight:bold;"
        )

    def _refresh_ip(self):
        ip = get_local_ip()
        if ip != self.local_ip:
            self.local_ip = ip
            self._notify(f"IP activa de la Raspberry Pi: {ip}", "info")
        self.pill_ip.set(f"IP {self.local_ip} : {self.port}", C["accent"], dot_color=C["accent"])

    # ---------------------------------------------------------------- ciclo de red
    def _start_server(self, port):
        if self.server:
            self.server.stop()
            self.server = None
        self.port = port
        try:
            srv = TIC3MultiServer(port, self.store, self.signals)
            srv.start()
            self.server = srv
        except OSError as e:
            self.pill_status.set(f"Error puerto {port}", C["red"], dot_color=C["red"])
            self._notify(f"No se pudo enlazar el puerto {port}: {e.strerror or e}", "error")

        self.pill_esp.set("ESP32 desconectado", C["muted"], dot_color=C["muted"])
        self._refresh_ip()

    def _on_change_port(self):
        text = self.ent_port.text().strip()
        if not text.isdigit() or not (1024 <= int(text) <= 65535):
            self._notify("Puerto inválido: utilice un valor entre 1024 y 65535.", "error")
            return
        new_port = int(text)
        if self.server and new_port == self.server.port:
            self._notify(f"El servidor ya está escuchando en el puerto {new_port}.", "info")
            return

        moved = False
        if self.server and self.server.connected:
            try:
                self.server.send_json({"cmd": "set_port", "port": new_port})
                moved = True
            except (OSError, RuntimeError):
                pass

        self._start_server(new_port)
        if self.server:
            msg = f"Servidor reiniciado en puerto {new_port}."
            if moved:
                msg += " ESP32 notificado para reconexión automática."
            self._notify(msg, "info")

    def _on_status(self, code, message):
        if code == "ESPERANDO_CLIENTE":
            self.pill_status.set(f"Escuchando en puerto {self.port}", C["yellow"], dot_color=C["yellow"])
            self._notify(f"Servidor listo en 0.0.0.0:{self.port}. Esperando conexión del ESP32...", "warning")
        elif code in ("CLIENTE_CONECTADO", "CLIENTE_LISTO"):
            if code == "CLIENTE_CONECTADO":
                ip_cli = message.split(":")[0]
                self.pill_esp.set(f"ESP32 {ip_cli}", C["green"], dot_color=C["green"])
            if not self.acquiring:
                self.pill_status.set("ESP32 conectado", C["accent"], dot_color=C["accent"])
                self._notify(f"ESP32 conectado ({message}). Listo para adquisición.", "success")
        elif code == "CLIENTE_PERDIDO":
            self.pill_esp.set("ESP32 desconectado", C["muted"], dot_color=C["muted"])
            self.pill_status.set(f"Escuchando en puerto {self.port}", C["yellow"], dot_color=C["yellow"])
            if self.acquiring:
                self._finalize("lost")
            else:
                self._notify(message, "warning")
        elif code == "ADQUIRIENDO":
            self.pill_status.set("Adquiriendo datos...", C["green"], dot_color=C["green"])
        elif code == "SESION_TERMINADA" and self.acquiring:
            delay = UDP_GRACE_MS if self.session_cfg.get("protocol") == "UDP" else 150
            QtCore.QTimer.singleShot(delay, lambda: self._finalize("esp_done"))

    # ---------------------------------------------------------------- control de sesión
    def _read_config(self):
        odr_txt = self.cb_odr.currentText().replace(" Hz", "")
        odr = 12 if odr_txt == "12.5" else int(odr_txt)
        rng = int(self.cb_range.currentText().replace("±", "").replace("g", ""))
        txt = self.ent_samples.text().strip()
        if not txt.isdigit() or not (1 <= int(txt) <= MAX_SAMPLES):
            raise ValueError(f"Cantidad de muestras inválida (1 – {MAX_SAMPLES}).")
        proto = "UDP" if self.cb_protocol.currentText().startswith("UDP") else "TCP"
        mode = "parallel" if self.cb_mode.currentText().startswith("Paralelo") else "sequential"
        return {"odr": odr, "range": rng, "samples": int(txt), "protocol": proto, "mode": mode}

    def _set_acquiring(self, active):
        self.acquiring = active
        self.btn_start.setEnabled(not active)
        self.btn_stop.setEnabled(active)
        for w in self.param_widgets:
            w.setEnabled(not active)

    def _on_start(self):
        if not self.server:
            self._notify("El servidor no está activo. Verifique el puerto.", "error")
            return
        try:
            cfg = self._read_config()
            path = self.server.start_acquisition(cfg["odr"], cfg["range"], cfg["samples"],
                                                 cfg["protocol"], cfg["mode"])
        except (ValueError, RuntimeError, OSError) as e:
            self._notify(f"No se pudo iniciar: {e}", "error")
            return

        self.session_cfg = cfg
        self._last_version = -1
        for c in self.curves_acc + self.curves_gyr:
            c.setData([], [])

        self.progress.setRange(0, cfg["samples"])
        self.progress.setValue(0)
        self.progress.setFormat(f"0 / {cfg['samples']} muestras  (0%)")
        self.lbl_file.setText(os.path.basename(path) + "  (escribiendo...)")
        self._set_acquiring(True)
        self.pill_status.set("Adquiriendo datos...", C["green"], dot_color=C["green"])

        mode_txt = "Paralelo" if cfg["mode"] == "parallel" else "Secuencial"
        self._notify(f"Adquiriendo {cfg['samples']} muestras · {self.cb_odr.currentText()} · "
                     f"±{cfg['range']}g · {cfg['protocol']} · {mode_txt}", "info")

    def _on_stop(self):
        if self.acquiring:
            self._finalize("manual")

    def _finalize(self, reason):
        if not self.acquiring or not self.server:
            return
        self._set_acquiring(False)
        path, count = self.server.stop_acquisition(send_stop=(reason in ("manual", "goal")))
        self._refresh_plots(force=True)
        _, st = self.store.snapshot(0)
        fname = os.path.basename(path) if path else "—"
        self.last_csv = path
        self.lbl_file.setText(fname)
        target = self.session_cfg.get("samples", count)

        if reason == "goal" or count >= target:
            self.pill_status.set("Meta alcanzada — CSV guardado", C["green"], icon="✅")
            self._notify(f"Meta de {count} muestras alcanzada. Archivo: {fname}", "success")
        elif reason == "esp_done":
            self.pill_status.set("Sesión finalizada — CSV guardado", C["yellow"], icon="✅")
            self._notify(f"ESP32 finalizó: {count}/{target} muestras ({st['lost']} pérdidas). "
                         f"Archivo: {fname}", "warning")
        elif reason == "lost":
            self.pill_status.set(f"Escuchando en puerto {self.port}", C["yellow"], dot_color=C["yellow"])
            self._notify(f"ESP32 desconectado. CSV parcial ({count} muestras): {fname}", "error")
        else:
            self.pill_status.set("Detenido — CSV guardado", C["yellow"], icon="⏹")
            self._notify(f"Sesión detenida manualmente ({count} muestras). Archivo: {fname}", "warning")

    def _on_open_folder(self):
        os.makedirs(SAVE_DIR, exist_ok=True)
        try:
            subprocess.Popen(["xdg-open", SAVE_DIR])
        except OSError:
            pass
        self._notify(f"Carpeta de datos: {SAVE_DIR}", "info")

    # ---------------------------------------------------------------- renderizado (25 FPS)
    def _refresh_plots(self, force=False):
        """Refresca las gráficas y las tarjetas KPI a 25 FPS sin saturar la CPU."""
        self._tick += 1
        odr = self.store.odr or 100
        n_window = int(min(PLOT_CAPACITY, max(200, odr * PLOT_WINDOW_S)))
        data, st = self.store.snapshot(n_window)

        if st["version"] == self._last_version and not force:
            return
        self._last_version = st["version"]

        # Actualización de estadísticas y barra de progreso
        target_val = max(1, st["target"])
        self.progress.setMaximum(target_val)
        self.progress.setValue(st["received"])
        pct = 100.0 * st["received"] / target_val if st["target"] else 0
        self.progress.setFormat(f"{st['received']} / {st['target']} muestras  ({pct:.0f}%)")

        self.kpi_samples.set(f"{st['received']}")
        self.kpi_rate.set(f"{st['rate']:.1f}" if st["rate"] > 0 else "—")
        self.kpi_blocks.set(f"{st['blocks']}")

        loss_pct = 100.0 * st["lost"] / st["span"] if st["span"] else 0.0
        self.lbl_quality.setText(f"Pérdidas: {st['lost']} ({loss_pct:.1f} %) · Duplicados: {st['duplicates']}")

        if len(data) < 2:
            return

        t = data[:, 0]
        for i, c in enumerate(self.curves_acc):
            c.setData(t, data[:, 1 + i])
        for i, c in enumerate(self.curves_gyr):
            c.setData(t, data[:, 4 + i])

    def closeEvent(self, event):
        self.plot_timer.stop()
        self.ip_timer.stop()
        if self.server:
            self.server.stop()
        event.accept()


# =============================================================================
# PUNTO DE ENTRADA
# =============================================================================
def build_app(argv=None):
    QtWidgets.QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QtWidgets.QApplication(argv if argv is not None else sys.argv)
    app.setStyle("Fusion")
    pal = QtGui.QPalette()
    for role, color in (
        (QtGui.QPalette.Window, C["bg"]),
        (QtGui.QPalette.Base, C["input"]),
        (QtGui.QPalette.Text, C["text"]),
        (QtGui.QPalette.WindowText, C["text"]),
        (QtGui.QPalette.Button, C["input"]),
        (QtGui.QPalette.ButtonText, C["text"]),
        (QtGui.QPalette.Highlight, C["accent"]),
        (QtGui.QPalette.HighlightedText, C["bg"])
    ):
        pal.setColor(role, QtGui.QColor(color))
    app.setPalette(pal)
    app.setStyleSheet(STYLESHEET)
    return app


def main():
    port = DEFAULT_PORT
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    app = build_app()
    win = TelemetryDashboard(port)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
