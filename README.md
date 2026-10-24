# Sistema de Adquisición de Datos ESP32 + BMI270 gestionado desde Raspberry Pi

Este repositorio contiene todos nuestros entregables para la Tarea 1 de TIC3.

## Contenido del Repositorio

* [esp32_firmware/](/esp32_firmware/): Proyecto de firmware en C para ESP-IDF:
  * [main.c](/esp32_firmware/main/main.c): Código principal para el ESP32-S3 con el driver I2C del sensor BMI270, modo secuencial y modo paralelo en doble núcleo con FreeRTOS (Core 0 para adquisición periódica y Core 1 para transmisión por sockets TCP/UDP en bloques JSON de 1 segundo).
  * [sdkconfig.defaults](/esp32_firmware/sdkconfig.defaults) y `CMakeLists.txt`: Configuración de compilación del proyecto (incluyendo la ampliación de pila a 16 KB para cargar el microcódigo del sensor).
* [raspberry_pi/](/raspberry_pi/): Servidor y aplicación gráfica para la Raspberry Pi:
  * [gui_server.py](/raspberry_pi/gui_server.py): Servidor multi-socket (TCP/UDP) e interfaz gráfica en Python (PyQt5 y PyQtGraph) para configurar remotamente los parámetros de adquisición, visualizar los 6 ejes en tiempo real y exportar las sesiones a CSV.
  * [requirements.txt](/raspberry_pi/requirements.txt): Librerías de Python requeridas (`PyQt5`, `pyqtgraph`, `numpy`).
* [sensores/](/sensores/): Directorio con los archivos CSV de las 3 sesiones de prueba adquiridas en el laboratorio:
  * `sesion_20261014_152423_odr100hz_range8g_parallel_TCP.csv`: **100 Hz**, rango **±8g**, modo **Paralelo (FreeRTOS)**, protocolo **TCP** (1000 muestras).
  * `sesion_20261014_152520_odr400hz_range4g_parallel_UDP.csv`: **400 Hz**, rango **±4g**, modo **Paralelo (FreeRTOS)**, protocolo **UDP** (2000 muestras).
  * `sesion_20261014_152558_odr1600hz_range16g_sequential_TCP.csv`: **1600 Hz**, rango **±16g**, modo **Secuencial**, protocolo **TCP** (5000 muestras).
* [wireshark/](/wireshark/): Capturas de tráfico de red registradas en la interfaz `wlan0` para comparar el comportamiento entre protocolos:
  * `wireshark_tcp.pcapng` (flujo con handshake SYN/ACK y confirmaciones) y `wireshark_udp.pcapng` (envío directo de datagramas sin conexión).
* [prompts.md](/prompts.md): Registro cronológico y tabla de trazabilidad de los prompts utilizados con IA durante todo el desarrollo del proyecto (Chat 1: Prompts 1 al 51; Chat 2: Prompts 1 al 22).

## Instrucciones de Instalación y Ejecución

### 1. Configuración de Red en Raspberry Pi
Configuramos la Raspberry Pi como punto de acceso Wi-Fi propio para aislar la red de telemetría:
* **SSID:** `TIC3_RED`
* **Contraseña:** `raspi123`
* **Canal:** 6 (2.4 GHz)
* **IP Servidor:** `10.42.0.1` (fija en modo Hotspot TIC3_RED)

### 2. Ejecución del Servidor y GUI
En la Raspberry Pi, conectar un monitor físico (mediante cable HDMI) con teclado y mouse como forma principal de visualizar y operar la interfaz gráfica en vivo.

Instalar las dependencias indicadas en `raspberry_pi/requirements.txt`:
```bash
sudo apt update
sudo apt install -y python3-pyqt5 python3-pyqtgraph python3-numpy
```
*(O mediante pip: `pip install -r raspberry_pi/requirements.txt`)*

Iniciar el servidor gráfico:
```bash
python3 raspberry_pi/gui_server.py
```

### 3. Compilación y Flasheo del Firmware en ESP32
Conectar el ESP32 por USB a la estación de trabajo y ejecutar con el entorno de ESP-IDF:
```bash
cd esp32_firmware
idf.py build flash monitor
```

### Integrantes:

* Oscar Ortiz Molina
* Joaquín Mardones Gallegos
* Gabriel Figueroa García
