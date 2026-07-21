# subs:// queue — YouTube a video con subtítulos quemados

App web para descargar videos de YouTube con subtítulos en español
quemados (centrados, con outline legible) y el video a 10% menos de
brillo. Encolas varios links y cada uno se procesa mostrando su progreso.

## Cómo elige los subtítulos

1. Si el video tiene subtítulos manuales en español (`es`, `es-ES`, `es-419`), los usa.
2. Si no, usa los autogenerados en español (incluyendo los que YouTube
   traduce automáticamente cuando el video no está en español).
3. Si no hay ninguno disponible en español, el video se procesa sin
   subtítulos (sólo se le baja el brillo).

## Requisitos

- Python 3.9+
- `ffmpeg` instalado y en el PATH del sistema
  - macOS: `brew install ffmpeg`
  - Ubuntu/Debian: `sudo apt install ffmpeg`
  - Windows: descarga el build de https://ffmpeg.org/download.html y agrégalo al PATH

## Instalación

```bash
cd yt-subs-app
python -m venv venv
source venv/bin/activate   # en Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Ejecutar

```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```

Abre `http://localhost:8000` en el navegador. Pega el link, dale a
"convertir", y se agrega a la cola. El bloque de progreso aparece abajo
y cuando termina sale el botón de descargar.

## Notas técnicas / cosas que puedes ajustar

- `server.py` → función `run_burn`: ahí está el `force_style` de los
  subtítulos (fuente, tamaño, color, alineación `Alignment=2` = abajo
  centrado) y el filtro `eq=brightness=-0.10` para el brillo. Puedes
  subir `FontSize` o cambiar `MarginV` (qué tan pegados al borde
  inferior quedan) a tu gusto.
- El worker procesa **un video a la vez** (cola secuencial) para no
  saturar CPU/red. Si quieres concurrencia, se puede lanzar más de una
  tarea `worker_loop()` en el `startup_event`.
- Los videos terminados quedan en `downloads/`. Los archivos temporales
  (video crudo + subtítulos sin quemar) se borran automáticamente al
  terminar cada trabajo.
- La calidad de descarga está limitada a 1080p en el `format` de
  yt-dlp; puedes subirlo o quitarlo si quieres el máximo disponible.
- El progreso del "quemado" se calcula leyendo `-progress pipe:1` de
  ffmpeg contra la duración del video, así que la barra corresponde al
  tiempo real de encode.

## Producción

Para exponerlo fuera de tu máquina (por ejemplo en tu droplet de
DigitalOcean), corre uvicorn detrás de nginx o con `--host 0.0.0.0` y
abre el puerto, o mételo en un servicio systemd. No hay autenticación
en los endpoints — si lo vas a exponer públicamente, agrega algo de
auth antes.
