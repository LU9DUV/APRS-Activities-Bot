import aprslib
import time
import logging
import logging.handlers
import sqlite3
import re
import os
import threading
import configparser
from datetime import datetime, timezone

# Rutas absolutas basadas en la ubicación del script, para que funcione
# sin importar desde qué directorio se lo ejecute (cron, systemd, etc.)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.ini")

# --- CONFIGURACIÓN DE LA ESTACIÓN (config.ini) ---
# El indicativo y la clave APRS-IS se leen de config.ini (sección [estacion]),
# que se edita desde el panel gráfico (pestaña Configuración). Los valores de
# abajo solo se usan si config.ini todavía no existe, para no romper una
# instalación anterior. Los cambios se aplican al reiniciar el bot.
_CALLSIGN_POR_DEFECTO = "LU9DUV-B"
_PASSCODE_POR_DEFECTO = "21413"


def _passcode_esperado(indicativo):
    """Clave APRS-IS que le corresponde a un indicativo (algoritmo estándar)."""
    base = indicativo.split("-")[0].upper()
    h = 0x73E2
    for i in range(0, len(base), 2):
        h ^= ord(base[i]) << 8
        if i + 1 < len(base):
            h ^= ord(base[i + 1])
    return h & 0x7FFF


def _cargar_configuracion():
    """
    Devuelve (indicativo, passcode, avisos). Si config.ini existe pero está
    incompleto o mal formado, el bot NO arranca (mejor que conectarse con
    datos erróneos). Los avisos se escriben al log más abajo, cuando ya está
    configurado.
    """
    if not os.path.exists(CONFIG_FILE):
        return _CALLSIGN_POR_DEFECTO, _PASSCODE_POR_DEFECTO, [(
            logging.WARNING,
            "⚠️ No existe config.ini: se usan el indicativo y la clave del script. "
            "Creá el archivo desde el panel (pestaña Configuración).")]

    cfg = configparser.ConfigParser()
    try:
        cfg.read(CONFIG_FILE, encoding="utf-8-sig")
    except configparser.Error as e:
        raise SystemExit(f"❌ config.ini no se pudo leer: {e}")

    indicativo = cfg.get("estacion", "indicativo", fallback="").strip().upper()
    passcode = cfg.get("estacion", "passcode", fallback="").strip()
    if not indicativo or not passcode:
        raise SystemExit("❌ config.ini: faltan 'indicativo' y/o 'passcode' en la sección [estacion].")
    if not re.fullmatch(r"[A-Z0-9]{3,7}(-[A-Z0-9]{1,2})?", indicativo):
        raise SystemExit(f"❌ config.ini: el indicativo '{indicativo}' no tiene un formato válido (ej: LU9DUV-B).")
    if not re.fullmatch(r"\d{1,6}", passcode):
        raise SystemExit("❌ config.ini: el passcode debe ser un número (ej: 21413).")

    avisos = [(logging.INFO, f"⚙️ Configuración leída de config.ini: indicativo {indicativo}.")]
    if passcode != str(_passcode_esperado(indicativo)):
        avisos.append((logging.WARNING,
                       "⚠️ El passcode de config.ini no coincide con el indicativo: el servidor APRS-IS "
                       "no va a aceptar los paquetes que envíe el bot."))
    return indicativo, passcode, avisos


CALLSIGN, PASSCODE, _AVISOS_CONFIG = _cargar_configuracion()
SERVER = "rotate.aprs2.net"
PORT = 14580
FRECUENCIA_MHZ = "144.390"  # Frecuencia de contacto del concurso, en MHz
MI_INDICATIVO = CALLSIGN.split('-')[0]  # Indicativo base del bot, sin SSID
BOLETIN_INTERVALO_SEGUNDOS = 3600  # Cada cuánto se difunden los boletines (1 hora)
MAX_BOLETINES_SIMULTANEOS = 9  # APRS solo tiene slots BLN1..BLN9 para boletines generales

CLAVES_FILE = os.path.join(BASE_DIR, "claves.txt")
DB_FILE = os.path.join(BASE_DIR, "concurso_aprs.db")
LOG_FILE = os.path.join(BASE_DIR, "bot_aprs.log")

# --- LOGGING PERSISTENTE (consola + archivo, con rotación diaria) ---
# Se guardan 30 días de historial. Los timestamps son en UTC (igual que
# el resto del bot) para poder correlacionar sin ambigüedad con los
# horarios que ven los operadores en sus propios relojes UTC.
_formatter = logging.Formatter(
    fmt="%(asctime)s UTC [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_formatter.converter = time.gmtime  # fuerza UTC en vez de hora local

_handler_consola = logging.StreamHandler()
_handler_consola.setFormatter(_formatter)

_handler_archivo = logging.handlers.TimedRotatingFileHandler(
    LOG_FILE, when="midnight", backupCount=30, encoding="utf-8", utc=True
)
_handler_archivo.setFormatter(_formatter)

logging.basicConfig(level=logging.INFO, handlers=[_handler_consola, _handler_archivo])

for _nivel, _mensaje in _AVISOS_CONFIG:
    logging.log(_nivel, _mensaje)

AIS = None  # se inicializa en __main__

# --- LOCKS PARA MANEJO SEGURO DE CONTACTOS SIMULTÁNEOS ---
# Cada paquete recibido se procesa en su propio hilo (ver __main__), para
# que un contacto lento (ej: un envío de socket que tarda) no retrase a
# los demás. Estos locks protegen los pocos recursos que SÍ son
# compartidos entre hilos, para que sigan siendo seguros bajo concurrencia
# real:
_msgid_lock = threading.Lock()   # contador de ID de mensaje (y su archivo)
_claves_lock = threading.Lock()  # cache de claves.txt
_envio_lock = threading.Lock()   # escritura sobre el socket de AIS

# Se activa recién cuando la conexión con APRS-IS quedó establecida, para
# que el hilo de boletines periódicos no intente enviar nada antes de eso.
_conexion_establecida = threading.Event()

# --- GENERADOR DE ID ÚNICO PARA MENSAJES APRS ---
# La red APRS-IS y muchas estaciones descartan como "duplicado" cualquier
# paquete con el mismo origen+destino+texto que ya vieron poco antes.
# Para que respuestas repetitivas como "clave incorrecta" no se bloqueen,
# cada mensaje saliente lleva un {ID} distinto (spec APRS: alfanumérico,
# máximo 5 caracteres).
MSGID_FILE = os.path.join(BASE_DIR, "msgid_counter.txt")
_MSGID_MAX = 99999  # límite práctico dentro del máximo de 5 caracteres


def _leer_ultimo_msgid():
    """Lee el último contador usado desde disco, si existe."""
    try:
        with open(MSGID_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 0


def _guardar_ultimo_msgid(valor):
    try:
        with open(MSGID_FILE, "w", encoding="utf-8") as f:
            f.write(str(valor))
    except OSError as e:
        logging.warning(f"⚠️ No se pudo persistir el contador de msgid: {e}")


_msgid_actual = _leer_ultimo_msgid()


def generar_msg_id():
    """Genera un ID de mensaje único e incremental (1..99999, con wraparound).
    Protegido con lock: si dos contactos se procesan al mismo tiempo en
    hilos distintos, no deben poder generar el mismo ID."""
    global _msgid_actual
    with _msgid_lock:
        _msgid_actual = (_msgid_actual % _MSGID_MAX) + 1
        valor = _msgid_actual
        _guardar_ultimo_msgid(valor)
    return str(valor)


# --- CACHE DE CLAVES (para no leer el archivo en cada mensaje) ---
_claves_cache = {
    "mtime": None,
    "lineas": []
}


def _cargar_claves_si_hace_falta():
    """Recarga claves.txt solo si cambió desde la última lectura.
    Protegido con lock: evita que dos hilos recarguen el archivo al
    mismo tiempo y dejen la cache en un estado inconsistente."""
    with _claves_lock:
        if not os.path.exists(CLAVES_FILE):
            logging.error(f"❌ Archivo {CLAVES_FILE} no encontrado.")
            _claves_cache["lineas"] = []
            _claves_cache["mtime"] = None
            return

        mtime_actual = os.path.getmtime(CLAVES_FILE)
        if _claves_cache["mtime"] == mtime_actual:
            return  # no cambió, usamos la cache

        with open(CLAVES_FILE, "r", encoding="utf-8") as f:
            _claves_cache["lineas"] = f.readlines()
        _claves_cache["mtime"] = mtime_actual
        logging.info("🔄 claves.txt recargado (cambió el archivo).")


# --- INICIALIZACIÓN Y MIGRACIÓN DE BASE DE DATOS (SQLite) ---
def normalizar_licencia(indicativo):
    """
    Quita el SSID (ej: LU1ABC-7 -> LU1ABC) para que el mismo operador
    no pueda participar varias veces usando distintos SSID, y para que
    el ranking, las consultas de puntos y el ADIF muestren solo la
    licencia limpia.

    Excepción: si el indicativo usa "/" (notación portable o de
    prefijo DX, ej: LU9DUV/ZL, EA6/LU9DUV), esa barra NO es un SSID y
    no se debe tocar. Si además llegara a tener un SSID pegado al
    final (ej: EA6/LU9DUV-5), se le quita solo esa parte final.
    """
    indicativo = indicativo.strip().upper()

    if '/' in indicativo:
        partes = indicativo.split('/')
        partes[-1] = partes[-1].split('-')[0]
        return '/'.join(partes)

    return indicativo.split('-')[0]


def _migrar_licencias_con_ssid(conn):
    """
    Unifica registros viejos guardados con SSID incluido (ej: 'LU1ABC-7')
    con la licencia normalizada (ej: 'LU1ABC'), sumando sus puntos.
    Necesaria para bases de datos creadas antes de que se agregara la
    normalización de licencias.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT licencia, latitud, longitud, fecha_contacto_utc, puntos, ya_puntuo FROM participantes")
    filas = cursor.fetchall()

    if not any('-' in lic for lic, *_ in filas):
        return  # nada que migrar

    logging.info("🔧 Migrando base de datos: unificando licencias que tenían SSID.")

    agrupado = {}
    for lic, lat, lon, fecha, puntos, ya_puntuo in filas:
        lic_norm = normalizar_licencia(lic)
        grupo = agrupado.setdefault(lic_norm, {
            "lat": lat, "lon": lon, "fecha": fecha, "puntos": 0, "ya_puntuo": 0
        })
        grupo["puntos"] += puntos or 0
        grupo["ya_puntuo"] = 1 if (grupo["ya_puntuo"] or ya_puntuo) else 0
        # Nos quedamos con la fecha de contacto más reciente del grupo
        if fecha and (not grupo["fecha"] or fecha > grupo["fecha"]):
            grupo["fecha"] = fecha
            grupo["lat"] = lat
            grupo["lon"] = lon

    cursor.execute("DELETE FROM participantes")
    for lic_norm, datos in agrupado.items():
        cursor.execute("""
            INSERT INTO participantes (licencia, latitud, longitud, fecha_contacto_utc, puntos, ya_puntuo)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (lic_norm, datos["lat"], datos["lon"], datos["fecha"], datos["puntos"], datos["ya_puntuo"]))
    conn.commit()
    logging.info(f"✅ Migración completa: {len(filas)} registro(s) -> {len(agrupado)} licencia(s) única(s).")


def inicializar_db():
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        cursor = conn.cursor()

        # Modo WAL: permite que lecturas y escrituras concurrentes convivan
        # mejor (varios hilos consultando puntos/ranking mientras otro
        # está registrando un contacto), en vez de bloquearse todos entre sí.
        cursor.execute("PRAGMA journal_mode=WAL")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS participantes (
                licencia TEXT PRIMARY KEY,
                latitud REAL,
                longitud REAL,
                fecha_contacto_utc TEXT,
                puntos INTEGER DEFAULT 0,
                ya_puntuo INTEGER DEFAULT 0
            )
        """)

        # Tabla de puntaje POR CAMPAÑA: cada fila = "esta licencia ya
        # validó esta clave puntual". La clave primaria (licencia, clave)
        # es lo que permite que, si hay dos campañas activas con claves
        # distintas y el mismo rango de fechas, el operador pueda puntuar
        # una vez en CADA una, en vez de una sola vez en total.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS codigos_validados (
                licencia TEXT NOT NULL,
                clave TEXT NOT NULL,
                fecha_hora_utc TEXT,
                PRIMARY KEY (licencia, clave)
            )
        """)
        conn.commit()

        # Migración: si la tabla ya existía de una versión anterior del bot
        # (sin la columna ya_puntuo), la agregamos sin perder los datos.
        cursor.execute("PRAGMA table_info(participantes)")
        columnas_existentes = {fila[1] for fila in cursor.fetchall()}

        if "ya_puntuo" not in columnas_existentes:
            logging.info("🔧 Migrando base de datos: agregando columna 'ya_puntuo'.")
            cursor.execute("ALTER TABLE participantes ADD COLUMN ya_puntuo INTEGER DEFAULT 0")
            # A los que ya tenían puntos sumados con la versión vieja, los
            # marcamos como ya puntuados para no volver a sumarles.
            cursor.execute("UPDATE participantes SET ya_puntuo = 1 WHERE puntos > 0")
            conn.commit()

        # Migración: unificar licencias viejas que se guardaron con SSID.
        # Debe correr ANTES de la migración de puntaje legacy de abajo,
        # para que los registros en codigos_validados usen la licencia ya
        # normalizada (sin SSID) y no queden desalineados con participantes.
        _migrar_licencias_con_ssid(conn)

        # Migración: preservar el punto de quien ya había puntuado bajo el
        # modelo viejo (un único punto global, sin distinguir campaña).
        # No sabemos qué clave exacta usaron en su momento (ese dato no se
        # guardaba antes), así que se consume bajo una clave "legacy"
        # sintética: protege contra reprocesar ese punto viejo dos veces,
        # sin bloquear ninguna campaña nueva (que tendrá una clave distinta).
        cursor.execute("SELECT licencia FROM participantes WHERE ya_puntuo = 1")
        licencias_legacy = [fila[0] for fila in cursor.fetchall()]
        for lic in licencias_legacy:
            cursor.execute("""
                INSERT INTO codigos_validados (licencia, clave, fecha_hora_utc)
                VALUES (?, '__LEGACY__', ?)
                ON CONFLICT(licencia, clave) DO NOTHING
            """, (lic, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()

# --- LÓGICA DE CONTROL DE PALABRAS CLAVE ---

# Formato de cada línea de claves.txt (con título para los boletines):
#   "TÍTULO DE LA ACTIVIDAD", "CLAVE", DD/MM/YYYY, DD/MM/YYYY
PATRON_CAMPANA = re.compile(r'"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*([\d/]+)\s*,\s*([\d/]+)')


def parsear_campanas():
    """
    Parsea claves.txt (usando la misma cache de mtime que antes) y
    devuelve una lista de dicts: {titulo, clave, dt_inicio, dt_fin}.
    Es la única función que lee el archivo; tanto verificar_palabra_clave
    como el generador de boletines la reutilizan.
    """
    _cargar_claves_si_hace_falta()

    campanas = []
    for linea in _claves_cache["lineas"]:
        linea = linea.strip()
        if not linea:
            continue

        if linea.startswith("#"):
            continue  # línea de comentario: se ignora sin warning

        match = PATRON_CAMPANA.match(linea)
        if not match:
            logging.warning(f"⚠️ Línea de claves.txt con formato inválido, se ignora: {linea}")
            continue

        titulo, clave, f_inicio_str, f_fin_str = match.groups()
        try:
            dt_inicio = datetime.strptime(f_inicio_str.strip(), "%d/%m/%Y").replace(
                hour=0, minute=0, second=0, tzinfo=timezone.utc)
            dt_fin = datetime.strptime(f_fin_str.strip(), "%d/%m/%Y").replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc)
        except Exception as e:
            logging.error(f"Error procesando fechas en línea: {linea}. {e}")
            continue

        campanas.append({
            "titulo": titulo.strip(),
            "clave": clave.strip(),
            "dt_inicio": dt_inicio,
            "dt_fin": dt_fin,
        })

    return campanas


def obtener_campanas_activas():
    """Devuelve las campañas cuyo rango de fechas incluye el momento actual (UTC)."""
    ahora_utc = datetime.now(timezone.utc)
    return [c for c in parsear_campanas() if c["dt_inicio"] <= ahora_utc <= c["dt_fin"]]


def verificar_palabra_clave(mensaje_usuario):
    """
    Verifica el comando contra las campañas definidas en claves.txt.
    Retorna: ('VALIDA', clave_real) o ('EXPIRADA', None) o ('INCORRECTA', None)
    """
    ahora_utc = datetime.now(timezone.utc)
    comando_usuario = mensaje_usuario.strip().upper()
    clave_encontrada = False

    for campana in parsear_campanas():
        if campana["clave"].upper() == comando_usuario:
            clave_encontrada = True
            if campana["dt_inicio"] <= ahora_utc <= campana["dt_fin"]:
                return 'VALIDA', campana["clave"]

    if clave_encontrada:
        return 'EXPIRADA', None
    return 'INCORRECTA', None


# --- BOLETINES DE DIFUSIÓN (bulletins APRS) ---
def formatear_texto_boletin(campana):
    """
    Arma el texto del boletín para una campaña: nombre, clave, estación
    a la que hay que mandar el mensaje, y horario de validez. Se recorta
    el título si hace falta para entrar en el límite práctico de un
    mensaje APRS (~67 caracteres).
    """
    fecha_fin_str = campana["dt_fin"].strftime("%d/%m")
    sufijo = f": clave {campana['clave']} a {CALLSIGN} hasta {fecha_fin_str}"
    espacio_para_titulo = max(67 - len(sufijo), 0)
    titulo = campana["titulo"][:espacio_para_titulo]
    return f"{titulo}{sufijo}"


def enviar_boletin(numero_boletin, texto):
    """
    Envía un BOLETÍN APRS general (BLNn). A diferencia de un mensaje
    normal, un boletín NO lleva {ID} y no espera ACK: va dirigido a la
    "estación virtual" BLNn, que cualquier estación/software APRS puede
    mostrarle a su operador como anuncio, sin que nadie deba confirmarlo.
    """
    try:
        destinatario = f"BLN{numero_boletin}".ljust(9)
        paquete = f"{CALLSIGN}>APRS,TCPIP*::{destinatario}:{texto}\r\n"
        with _envio_lock:
            AIS.sendall(paquete)
        logging.info(f"📢 Boletín BLN{numero_boletin} enviado: {paquete.strip()}")
    except Exception as e:
        logging.error(f"❌ Error al enviar boletín BLN{numero_boletin}: {e}")


def enviar_boletines_actividades():
    """
    Difunde un boletín por cada campaña actualmente activa (hasta el
    máximo de slots BLN disponibles). Se llama periódicamente desde el
    hilo de boletines (ver __main__).
    """
    campanas_activas = obtener_campanas_activas()

    if not campanas_activas:
        logging.info("📢 No hay campañas activas en este momento; no se envían boletines.")
        return

    if len(campanas_activas) > MAX_BOLETINES_SIMULTANEOS:
        logging.warning(
            f"⚠️ Hay {len(campanas_activas)} campañas activas, pero solo hay "
            f"{MAX_BOLETINES_SIMULTANEOS} slots de boletín (BLN1-BLN{MAX_BOLETINES_SIMULTANEOS}). "
            f"Se anuncian las primeras {MAX_BOLETINES_SIMULTANEOS}."
        )

    for numero, campana in enumerate(campanas_activas[:MAX_BOLETINES_SIMULTANEOS], start=1):
        texto = formatear_texto_boletin(campana)
        enviar_boletin(numero, texto)


# --- GESTIÓN DE PARTICIPANTES (DB) ---
def registrar_o_actualizar_licencia(licencia, lat, lon):
    """
    Registra la licencia si es nueva. Devuelve el estado 'ya_puntuo' actual
    (0 o 1) para que el llamador decida si corresponde sumar un punto.

    El INSERT es atómico vía "ON CONFLICT DO NOTHING": si dos contactos de
    la MISMA licencia llegan casi en simultáneo (dos hilos distintos), no
    puede pasar que ambos vean "no existe" e intenten insertarla dos veces.
    """
    ahora_utc_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO participantes (licencia, latitud, longitud, fecha_contacto_utc, puntos, ya_puntuo)
            VALUES (?, ?, ?, ?, 0, 0)
            ON CONFLICT(licencia) DO NOTHING
        """, (licencia, lat, lon, ahora_utc_str))
        conn.commit()
        es_nueva = cursor.rowcount > 0

        cursor.execute("SELECT ya_puntuo FROM participantes WHERE licencia = ?", (licencia,))
        row = cursor.fetchone()

    if es_nueva:
        logging.info(f"💾 Nueva licencia registrada: {licencia} con 0 puntos.")
    else:
        logging.info(f"📖 Licencia conocida detectada: {licencia}.")

    return row[0] if row is not None else 0


def registrar_codigo_validado(licencia, clave):
    """
    Registra atómicamente que 'licencia' validó 'clave' (una campaña
    puntual, identificada por el texto exacto de la clave). Devuelve
    True si es la PRIMERA vez que esta licencia valida esta clave
    puntual (corresponde sumar punto), o False si ya la había validado
    antes (no se suma de nuevo, pero puede seguir puntuando en otras
    campañas con clave distinta).
    """
    ahora_utc_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO codigos_validados (licencia, clave, fecha_hora_utc)
            VALUES (?, ?, ?)
            ON CONFLICT(licencia, clave) DO NOTHING
        """, (licencia, clave, ahora_utc_str))
        conn.commit()
        return cursor.rowcount > 0


def sumar_punto_a_licencia(licencia):
    """
    Suma 1 punto al total de la licencia. El control de "no sumar dos
    veces por la misma campaña" ya lo hace registrar_codigo_validado()
    antes de llamar a esta función, así que acá simplemente se suma.
    """
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE participantes
            SET puntos = puntos + 1, ya_puntuo = 1
            WHERE licencia = ?
        """, (licencia,))
        conn.commit()

    logging.info(f"⭐ Se sumó 1 punto a {licencia}.")


def obtener_puntos_licencia(licencia):
    """
    Devuelve la cantidad de puntos actuales de una licencia, o None si
    todavía no está registrada en la base de datos.
    """
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT puntos FROM participantes WHERE licencia = ?", (licencia,))
        row = cursor.fetchone()
    return row[0] if row is not None else None


def obtener_ranking_completo():
    """
    Devuelve una lista de tuplas (licencia, puntos, posicion) ordenada por
    posición ascendente. Las licencias empatadas en puntos comparten la
    misma posición (estándar de ranking: 1,1,3,4,...).
    """
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT licencia, puntos,
                   RANK() OVER (ORDER BY puntos DESC) AS posicion
            FROM participantes
            ORDER BY posicion ASC, licencia ASC
        """)
        return cursor.fetchall()


def formatear_top5(filas_ranking):
    """Arma un texto compacto tipo 'TOP5: 1)LICENCIA-PUNTOS 2)...' """
    top5 = filas_ranking[:5]
    partes = [f"{posicion}){lic}-{puntos}" for lic, puntos, posicion in top5]
    return "TOP5: " + " ".join(partes)


# --- GENERADOR DE ARCHIVO ADIF ---
def guardar_en_adif(licencia):
    """Recibe la licencia YA normalizada (sin SSID, salvo notación
    portable con '/') para que el log ADIF quede limpio."""
    ahora_utc = datetime.now(timezone.utc)

    # Nombre del archivo basado en el día actual (ej: 20260908.adi)
    filename = os.path.join(BASE_DIR, ahora_utc.strftime("%Y%m%d.adi"))

    date_str = ahora_utc.strftime("%Y%m%d")
    time_str = ahora_utc.strftime("%H%M%S")

    # Formato estándar mínimo ADIF (incluye FREQ y STATION_CALLSIGN)
    record = (
        f"<CALL:{len(licencia)}>{licencia} "
        f"<QSO_DATE:{len(date_str)}>{date_str} "
        f"<TIME_ON:{len(time_str)}>{time_str} "
        f"<FREQ:{len(FRECUENCIA_MHZ)}>{FRECUENCIA_MHZ} "
        f"<STATION_CALLSIGN:{len(MI_INDICATIVO)}>{MI_INDICATIVO} "
        f"<MODE:3>PKT <EOR>\n"
    )

    # Escribir o crear si no existe
    archivo_nuevo = not os.path.exists(filename)
    try:
        with open(filename, "a", encoding="utf-8") as adif_file:
            if archivo_nuevo:
                adif_file.write("ADIF Export del Bot APRS Concursos\n<HEADER>\n\n")
            adif_file.write(record)
        logging.info(f"📝 Registro QSO ADIF guardado en {filename}")
    except OSError as e:
        logging.error(f"❌ No se pudo escribir el archivo ADIF {filename}: {e}")


# --- MANEJADORES DE COMANDOS DEL MENSAJE ---
def comando_puntos(remitente, licencia):
    """Comando 'PUNTOS': informa el puntaje actual del que consulta."""
    puntos_actuales = obtener_puntos_licencia(licencia)
    if puntos_actuales is None:
        respuesta = "Todavia no tenes puntos registrados en el concurso."
    else:
        respuesta = f"Tenes {puntos_actuales} punto(s) en el concurso."
    enviar_mensaje_aprs(remitente, respuesta)


def comando_ayuda(remitente):
    """Comando 'AYUDA': lista los comandos disponibles, sin revelar la clave."""
    respuesta = "Comandos: PUNTOS, RANKING. Hay clave secreta del concurso."
    enviar_mensaje_aprs(remitente, respuesta)


def comando_ranking(remitente, licencia):
    """
    Comando 'RANKING': muestra el top 5 y, si el que consulta no está
    entre ellos, un segundo mensaje aparte con su posición. Se manda en
    mensajes separados para no exceder el largo máximo práctico de un
    mensaje APRS (~67 caracteres).
    """
    filas_ranking = obtener_ranking_completo()

    if not filas_ranking:
        enviar_mensaje_aprs(remitente, "Todavia no hay participantes registrados.")
        return

    enviar_mensaje_aprs(remitente, formatear_top5(filas_ranking))

    top5_licencias = {lic for lic, _puntos, _pos in filas_ranking[:5]}
    if licencia not in top5_licencias:
        propia = next((f for f in filas_ranking if f[0] == licencia), None)
        if propia:
            _, puntos_propios, posicion_propia = propia
            respuesta_propia = f"Tu posicion: #{posicion_propia} con {puntos_propios} punto(s)."
            enviar_mensaje_aprs(remitente, respuesta_propia)


def procesar_logica_concurso(remitente, licencia, mensaje_texto):
    """
    Comportamiento por defecto: evalúa el mensaje como intento de clave
    del concurso (VALIDA / EXPIRADA / INCORRECTA).

    El puntaje se controla POR CAMPAÑA (por clave puntual), no de forma
    global: si hay dos campañas activas con claves distintas, la misma
    licencia puede puntuar una vez en cada una.
    """
    resultado, clave_valida = verificar_palabra_clave(mensaje_texto)
    hora_utc_actual = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    if resultado == 'VALIDA':
        es_primera_vez_en_esta_campana = registrar_codigo_validado(licencia, clave_valida)

        if not es_primera_vez_en_esta_campana:
            # Esta licencia ya había validado ESTA clave puntual antes;
            # se le confirma pero no se le vuelve a sumar ni a loguear en ADIF.
            respuesta = "Código ya validado, gracias por el QSO"
            enviar_mensaje_aprs(remitente, respuesta)
        else:
            respuesta = f"plz confirm QRZ LOTW {FRECUENCIA_MHZ} PKT [{hora_utc_actual}]"
            enviar_mensaje_aprs(remitente, respuesta)

            # Registrar QSO en ADIF del día y sumar punto (solo la primera vez en esta campaña)
            guardar_en_adif(licencia)
            sumar_punto_a_licencia(licencia)

    elif resultado == 'EXPIRADA':
        respuesta = "Clave expirada, mejor suerte la proxima"
        enviar_mensaje_aprs(remitente, respuesta)

    else:
        respuesta = "clave incorrecta"
        enviar_mensaje_aprs(remitente, respuesta)


# --- PROCESADOR DE PAQUETES ---
def procesar_paquete(packet):
    # aprslib.consumer(..., raw=False) siempre entrega un dict ya parseado
    # (o directamente no invoca el callback si no pudo parsear el paquete),
    # así que solo contemplamos ese caso.
    if not isinstance(packet, dict):
        return

    mensaje_bruto = packet.get('text', '')
    destinatario = str(packet.get('addresse', '')).strip()
    remitente = str(packet.get('from', '')).strip()

    # Extraer coordenadas si vienen en el paquete
    lat = packet.get('latitude', 0.0)
    lon = packet.get('longitude', 0.0)

    if (not mensaje_bruto or mensaje_bruto == "") and 'raw' in packet:
        raw_line = packet['raw']
        mensaje_bruto = extraer_texto_manualmente(raw_line)

    destinatario = str(destinatario).strip()
    remitente = str(remitente).strip()
    mensaje_bruto = str(mensaje_bruto).strip()

    if not remitente:
        return

    if destinatario != CALLSIGN and destinatario != MI_INDICATIVO:
        return

    # --- IGNORAR PAQUETES DE CONFIRMACIÓN (ACK / REJ) ---
    # Cuando el bot manda un mensaje con {ID}, la estación del otro lado
    # suele responder automáticamente con "ackID" o "rejID". Estos NO son
    # comandos ni intentos de clave: si se los llega a tratar como texto
    # normal, el bot respondería "clave incorrecta" con un nuevo {ID},
    # eso dispararía un nuevo ack, y así se genera un loop infinito.
    es_ack_o_rej = (
        packet.get('response') in ('ack', 'rej')
        or re.match(r'^(ack|rej)\d{1,5}$', mensaje_bruto, re.IGNORECASE)
    )
    if es_ack_o_rej:
        logging.info(f"↩️ ACK/REJ recibido de {remitente} ('{mensaje_bruto}'). Se ignora, no se responde.")
        return

    msg_id = ""
    mensaje_texto = mensaje_bruto
    if "{" in mensaje_bruto:
        partes = mensaje_bruto.split("{")
        mensaje_texto = partes[0].strip()
        msg_id = partes[1].strip()

    logging.info(f"📩 Mensaje de {remitente}: '{mensaje_texto}' (ID: {msg_id}) [hilo: {threading.current_thread().name}]")

    # 1. ENVIAR EL ACK DE INMEDIATO
    if msg_id:
        enviar_ack(remitente, msg_id)

    # Normalizamos el indicativo (sin SSID) para identificar al operador
    # de forma única, sin importar desde qué SSID transmita.
    licencia = normalizar_licencia(remitente)

    # 2. LOGICA DEL CONCURSO (Paso 1: Controlar y/o registrar en Base de Datos)
    registrar_o_actualizar_licencia(licencia, lat, lon)

    # 3. DESPACHO SEGÚN EL CONTENIDO DEL MENSAJE.
    # Los comandos especiales (PUNTOS/AYUDA/RANKING) no cuentan como
    # intento de clave; cualquier otro texto cae en el caso por defecto,
    # que es la lógica normal del concurso.
    comando = mensaje_texto.strip().upper()
    match comando:
        case "PUNTOS":
            comando_puntos(remitente, licencia)
        case "AYUDA":
            comando_ayuda(remitente)
        case "RANKING":
            comando_ranking(remitente, licencia)
        case _:
            procesar_logica_concurso(remitente, licencia, mensaje_texto)


def extraer_texto_manualmente(raw_str):
    try:
        if "::" in raw_str:
            parte_mensaje = raw_str.split("::", 1)[1]
            if ":" in parte_mensaje:
                return parte_mensaje.split(":", 1)[1]
    except Exception:
        pass
    return ""


def enviar_ack(destinatario, msg_id):
    """Envío protegido con lock: evita que dos hilos escriban al mismo
    tiempo sobre el socket de AIS y se mezclen los paquetes salientes."""
    try:
        destinatario_formateado = destinatario.ljust(9)
        paquete_ack = f"{CALLSIGN}>APRS,TCPIP*::{destinatario_formateado}:ack{msg_id}\r\n"
        with _envio_lock:
            AIS.sendall(paquete_ack)
        logging.info(f"✅ ACK enviado a la red: {paquete_ack.strip()}")
    except Exception as e:
        logging.error(f"❌ Error al enviar ACK a {destinatario}: {e}")


def enviar_mensaje_aprs(destinatario, texto):
    """
    Envía un mensaje APRS agregando siempre un {ID} único al final del
    texto, para que la red no lo descarte como duplicado si el contenido
    se repite (ej: "clave incorrecta" enviado varias veces).
    Envío protegido con lock por el mismo motivo que enviar_ack().
    """
    try:
        msg_id = generar_msg_id()
        destinatario_formateado = destinatario.ljust(9)
        texto_con_id = f"{texto}{{{msg_id}"
        paquete_mensaje = f"{CALLSIGN}>APRS,TCPIP*::{destinatario_formateado}:{texto_con_id}\r\n"
        with _envio_lock:
            AIS.sendall(paquete_mensaje)
        logging.info(f"📤 Respuesta enviada a la red: {paquete_mensaje.strip()}")
    except Exception as e:
        logging.error(f"❌ Error al enviar respuesta a {destinatario}: {e}")


# --- CONEXIÓN PRINCIPAL ---
def _procesar_paquete_en_hilo(packet):
    """
    Wrapper que se ejecuta en un hilo nuevo por cada paquete recibido.
    Así, cada contacto se procesa de forma independiente: si uno tarda
    (por ejemplo, una escritura lenta al socket o a la DB), no bloquea
    la lectura ni el procesamiento de los demás. Cualquier excepción se
    loguea acá mismo, porque una excepción dentro de un hilo secundario
    NO la ve el try/except del loop principal.
    """
    try:
        procesar_paquete(packet)
    except Exception:
        logging.exception("⚠️ Error procesando un paquete en un hilo secundario.")


def _hilo_boletines_periodicos():
    """
    Espera a que la conexión con APRS-IS quede establecida y, a partir
    de ahí, difunde los boletines de las actividades activas cada
    BOLETIN_INTERVALO_SEGUNDOS (por defecto, cada 1 hora). Corre en un
    hilo daemon aparte durante toda la vida del programa.
    """
    _conexion_establecida.wait()
    logging.info(
        f"📢 Hilo de boletines iniciado (cada {BOLETIN_INTERVALO_SEGUNDOS // 60} min)."
    )
    while True:
        try:
            enviar_boletines_actividades()
        except Exception:
            logging.exception("⚠️ Error enviando boletines periódicos.")
        time.sleep(BOLETIN_INTERVALO_SEGUNDOS)


def _cerrar_conexion_ais():
    """
    Cierra el socket de AIS de forma defensiva antes de reintentar
    connect(). Necesario porque, si la conexión se corta de forma
    abrupta del lado del servidor (ej: "conexión forzada a cerrar por
    el host remoto"), aprslib puede dejar internamente un socket roto
    (file descriptor -1) que hace fallar el próximo select() con un
    ValueError en vez de una excepción propia de aprslib. Cerrar
    explícitamente antes de reconectar evita arrastrar ese estado.
    """
    try:
        AIS.close()
    except Exception:
        pass  # si ya estaba roto/cerrado, no importa: igual vamos a reconectar


if __name__ == "__main__":
    inicializar_db()

    AIS = aprslib.IS(CALLSIGN, passwd=PASSCODE, host=SERVER, port=PORT)

    AIS.set_filter(f"d/{MI_INDICATIVO}*")

    logging.info(f"🤖 Iniciando Bot {CALLSIGN} Concursero...")
    logging.info(f"📝 Log persistente en: {LOG_FILE}")

    threading.Thread(target=_hilo_boletines_periodicos, daemon=True).start()

    while True:
        try:
            AIS.connect()
            logging.info("🔗 Conexión de red establecida. Esperando tramas de concurso...")
            _conexion_establecida.set()  # habilita el envío de boletines
            # Cada paquete se despacha a un hilo propio (daemon=True para
            # que no impidan cerrar el programa) y se sigue leyendo la
            # red de inmediato, sin esperar a que termine de procesarse.
            AIS.consumer(
                lambda packet: threading.Thread(
                    target=_procesar_paquete_en_hilo, args=(packet,), daemon=True
                ).start(),
                raw=False,
            )
        except (aprslib.exceptions.ConnectionDrop, aprslib.exceptions.LoginError) as e:
            logging.warning(f"⚠️ Error de conexión conocido ({e}). Reintentando en 10 segundos...")
            _cerrar_conexion_ais()
            time.sleep(10)
        except KeyboardInterrupt:
            logging.info("🛑 Bot detenido por el usuario.")
            break
        except Exception as e:
            # Cualquier otro error inesperado (timeout, socket, parseo interno, etc.)
            # no debe tirar abajo el bot: se loguea y se reintenta.
            logging.exception(f"⚠️ Error inesperado en el loop principal: {e}")
            _cerrar_conexion_ais()
            logging.info("Reintentando en 10 segundos...")
            time.sleep(10)
