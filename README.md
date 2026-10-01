# Bot de seguimiento de precios para Telegram

Busca productos en Amazon.es, PcComponentes, MediaMarkt y FNAC mediante Google
Shopping (a través de SerpApi). Elige el resultado exacto que quieres vigilar; el bot
comprueba su oferta cada 12 horas y avisa solo cuando detecta un cambio de precio.
Los seguimientos se guardan en SQLite y sobreviven a los reinicios.

## Requisitos

- Python 3.10 o posterior.
- Un bot/token creado con [@BotFather](https://t.me/BotFather).
- Una cuenta y clave de API de [SerpApi](https://serpapi.com/). Las consultas de
  búsqueda y las revisiones periódicas consumen cuota de la API.

No existe una API pública común de esas tiendas; la disponibilidad y precisión
dependen de los resultados de Google Shopping/SerpApi. Si una tienda deja de
aparecer entre las ofertas, el bot conserva el último precio y avisa una vez
hasta que pueda comprobarlo de nuevo. El primer chequeo periódico ocurre doce
horas después de iniciar el bot.

## Instalación

En PowerShell, desde esta carpeta:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edita `.env` y sustituye los valores de ejemplo por el token de Telegram y la
clave de SerpApi. No compartas ni publiques esas claves. Luego carga las
variables en la sesión de PowerShell:

```powershell
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
        Set-Item -Path "Env:$($matches[1])" -Value $matches[2]
    }
}
py bot.py
```

La base de datos `pricebot.sqlite3` se crea al iniciar. Para guardarla en otra
ruta, configura `DATABASE_PATH`.

## Uso

- `/buscar nombre del producto` — busca productos; pulsa **Seguir** en cada
  resultado que quieras vigilar. **Ver** abre la oferta.
- `/seguimiento` — muestra el ID y precio actual de cada seguimiento.
- `/quitar ID` — deja de vigilar el producto con ese ID.
- `/ayuda` — muestra los comandos.

El bot debe permanecer ejecutándose para realizar las revisiones. Ejecuta las
pruebas unitarias con `py -m unittest discover -s tests`.

### Render

Si se despliega como **Web Service**, usa `python bot.py` como comando de
inicio. El bot escucha automáticamente el puerto `PORT` que asigna Render y
responde `ok` en `/health`, mientras mantiene el polling de Telegram.

Si aparece un error inesperado, el manejador del bot registra el traceback
completo en la consola para facilitar el diagnóstico. Los tokens configurados
se ocultan en ese registro; comparte el traceback sin publicar credenciales.
