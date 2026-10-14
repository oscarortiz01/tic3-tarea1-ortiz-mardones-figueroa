# Sistema de Adquisición de Datos ESP32 + BMI270 gestionado desde Raspberry Pi

Este repositorio contiene todos nuestros entregables para la Tarea 1 de TIC3.

## Contenido del Repositorio

* [main.c](/esp32_firmware/main/main.c): Firmware embebido en C (ESP-IDF) que desarrollamos para el ESP32-S3, con soporte para lectura I2C del sensor BMI270, procesamiento en doble núcleo (Core 0 para adquisición estricta y Core 1 para red en FreeRTOS) y transmisión por sockets TCP/UDP con empaquetado dinámico en bloques de 1 segundo.
* [gui_server.py](/raspberry_pi/gui_server.py): Servidor de red y aplicación gráfica (Frontend) que implementamos en Python utilizando PyQt5 y PyQtGraph para la configuración remota de parámetros (ODR, rango, protocolo, modo), recepción asíncrona de datos y graficación en tiempo real.
* [requirements.txt](/raspberry_pi/requirements.txt): Archivo de dependencias con las librerías necesarias para ejecutar el servidor gráfico en la Raspberry Pi.
* [sensores/](/sensores/): Directorio con los registros en formato CSV que adquirimos empíricamente en el laboratorio bajo distintas combinaciones de frecuencia de muestreo (100 Hz a 1600 Hz), sensibilidades (±2g, ±4g, ±8g, ±16g), modos (secuencial/paralelo) y protocolos de transporte (TCP/UDP).
* [wireshark/](/wireshark/): Capturas de tráfico de red en formato `.pcapng` que registramos durante las pruebas de transmisión para analizar la sobrecarga de cabeceras, handshakes y comportamiento temporal entre TCP y UDP.
* [prompts.md](/prompts.md): Registro cronológico y reflexivo de todos los prompts utilizados con herramientas de IA durante las etapas de desarrollo, depuración y optimización del proyecto (Chat 1: Driver I2C y red; Chat 2: FreeRTOS y Dashboard).

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

---

Integrantes:

• Oscar Ortiz Molina  
• Joaquín Mardones Gallegos  
• Gabriel Figueroa García  
