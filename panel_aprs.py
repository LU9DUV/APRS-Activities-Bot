#!/usr/bin/env python3
"""
panel_aprs.py - Panel gráfico para el bot APRS de concursos (bot3_corregido.py)

Se ejecuta APARTE del bot, en la misma carpeta (o apuntando a ella):
    python panel_aprs.py
    python panel_aprs.py C:\\ruta\\a\\la\\carpeta\\del\\bot

No toca el proceso del bot: lee concurso_aprs.db (solo lectura), edita
claves.txt (el bot lo recarga solo cuando cambia) y config.ini (señal
distintiva y clave APRS-IS, que el bot lee al iniciar) y muestra bot_aprs.log
y los archivos .adi. Los horarios se muestran en UTC (como el bot) o en la
hora local de la PC, según lo elegido en la pestaña Configuración.

Solo usa la biblioteca estándar (tkinter, sqlite3, csv, re).
"""

import configparser
import csv
import io
import os
import re
import shutil
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

CLAVES_NAME = "claves.txt"
CONFIG_NAME = "config.ini"
DB_NAME = "concurso_aprs.db"
LOG_NAME = "bot_aprs.log"
BOT_GLOB = "bot3*.py"  # tolera copias como "bot3_corregido (2).py"
LEGACY_KEY = "__LEGACY__"  # marca de puntos del modelo anterior (ver bot)
LIMITE_APRS = 67  # largo práctico de un mensaje/boletín APRS

# Mismo formato que el bot: "TÍTULO", "CLAVE", DD/MM/YYYY, DD/MM/YYYY
PATRON_CAMPANA = re.compile(r'"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*([\d/]+)\s*,\s*([\d/]+)')
PATRON_DESACTIVADA = re.compile(r"#\s*" + PATRON_CAMPANA.pattern)
PATRON_NIVEL = re.compile(r"\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]")
PATRON_CAMPO_ADIF = re.compile(r"<([A-Za-z_]+):(\d+)(?::[A-Za-z])?>")
PATRON_MARCA_LOG = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC")
PATRON_INDICATIVO = re.compile(r"[A-Z0-9]{3,7}(-[A-Z0-9]{1,2})?")


def ahora_utc():
    return datetime.now(timezone.utc)


def parsear_fecha(texto, fin_del_dia=False):
    """DD/MM/YYYY -> datetime UTC (inicio 00:00:00 o fin 23:59:59, como el bot)."""
    d = datetime.strptime(texto.strip(), "%d/%m/%Y")
    if fin_del_dia:
        d = d.replace(hour=23, minute=59, second=59)
    return d.replace(tzinfo=timezone.utc)


def hace_cuanto(dt):
    seg = max(0, int((ahora_utc() - dt).total_seconds()))
    if seg < 60:
        return f"hace {seg} s"
    if seg < 3600:
        return f"hace {seg // 60} min"
    if seg < 86400:
        return f"hace {seg // 3600} h {(seg % 3600) // 60} min"
    return f"hace {seg // 86400} d"


def reemplazar_atomico(tmp, destino):
    """os.replace con reintentos: en Windows falla si otro programa (el bot) tiene el archivo abierto justo ahora."""
    for intento in range(5):
        try:
            os.replace(tmp, destino)  # el bot nunca ve un archivo a medias
            return
        except PermissionError:
            if intento == 4:
                raise
            time.sleep(0.25)


class Zona:
    """Cómo se muestran los horarios en el panel: UTC (como el bot) u hora local de esta PC."""

    def __init__(self, modo="utc"):
        self.modo = modo if modo in ("utc", "local") else "utc"

    @property
    def local(self):
        return self.modo == "local"

    def convertir(self, dt):
        """datetime con zona (UTC) -> datetime en la zona elegida."""
        return dt.astimezone() if self.local else dt

    def ahora(self):
        return self.convertir(ahora_utc())

    def etiqueta(self, ref=None):
        """'UTC' o, en hora local, el desplazamiento (ej: 'UTC-03:00')."""
        if not self.local:
            return "UTC"
        d = self.convertir(ref or ahora_utc()).strftime("%z") or "+0000"
        return f"UTC{d[:3]}:{d[3:5]}"

    def marca(self, texto_utc):
        """'YYYY-MM-DD HH:MM:SS' (UTC) -> el mismo formato en la zona elegida."""
        dt = _parsear_marca(texto_utc)
        return self.convertir(dt).strftime("%Y-%m-%d %H:%M:%S") if (dt and self.local) else texto_utc


def _parsear_marca(texto_utc):
    try:
        return datetime.strptime(texto_utc, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _fecha_en_zona(texto_utc, zona):
    dt = _parsear_marca(texto_utc)
    return zona.convertir(dt).date() if dt else None


def leer_valores_bot(carpeta):
    """(señal, passcode) escritos como valores por defecto en el script del bot, o ('', '')."""
    scripts = sorted(Path(carpeta).glob(BOT_GLOB), key=lambda s: s.stat().st_mtime, reverse=True)
    for ruta in scripts:
        try:
            texto = ruta.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        s = re.search(r'^(?:_CALLSIGN_POR_DEFECTO|CALLSIGN)\s*=\s*"([^"]+)"', texto, re.M)
        c = re.search(r'^(?:_PASSCODE_POR_DEFECTO|PASSCODE)\s*=\s*"([^"]+)"', texto, re.M)
        if s:
            return s.group(1), (c.group(1) if c else "")
    return "", ""


def texto_boletin(titulo, clave, fin, callsign):
    """Replica formatear_texto_boletin() del bot. Devuelve (texto, recortado)."""
    sufijo = f": clave {clave} a {callsign} hasta {fin.strftime('%d/%m')}"
    espacio = max(LIMITE_APRS - len(sufijo), 0)
    return titulo[:espacio] + sufijo, len(titulo) > espacio


# --------------------------------------------------------------------------
# Campañas (claves.txt)
# --------------------------------------------------------------------------
class ArchivoCambio(Exception):
    """claves.txt cambió (por el usuario u otro programa) mientras se editaba."""


@dataclass
class Campana:
    titulo: str
    clave: str
    inicio: str  # DD/MM/YYYY
    fin: str     # DD/MM/YYYY
    habilitada: bool = True
    linea: int = -1   # índice de la línea en el archivo
    raw: str = ""     # texto original de la línea (para detectar cambios)

    def dt_inicio(self):
        return parsear_fecha(self.inicio)

    def dt_fin(self):
        return parsear_fecha(self.fin, fin_del_dia=True)

    def estado(self, ahora=None):
        if not self.habilitada:
            return "Desactivada"
        ahora = ahora or ahora_utc()
        try:
            if ahora < self.dt_inicio():
                return "Programada"
            if ahora > self.dt_fin():
                return "Vencida"
        except ValueError:
            return "Fecha inválida"
        return "Activa"

    def como_linea(self):
        base = f'"{self.titulo}", "{self.clave}", {self.inicio}, {self.fin}'
        return base if self.habilitada else "# " + base


class ClavesStore:
    """Lee y edita claves.txt conservando comentarios, líneas en blanco y saltos de línea."""

    def __init__(self, ruta):
        self.ruta = Path(ruta)

    def firma(self):
        try:
            st = self.ruta.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _leer(self):
        if not self.ruta.exists():
            return [], "\r\n" if os.name == "nt" else "\n"
        contenido = self.ruta.read_bytes().decode("utf-8-sig")  # tolera BOM de Notepad
        eol = "\r\n" if "\r\n" in contenido else "\n"
        return contenido.splitlines(), eol

    def _escribir(self, lineas, eol):
        if self.ruta.exists():
            backup = self.ruta.with_name(self.ruta.name + ".bak")
            if not backup.exists():
                shutil.copy2(self.ruta, backup)  # copia de la versión original, una sola vez
        tmp = self.ruta.with_name(self.ruta.name + ".tmp")
        tmp.write_bytes((eol.join(lineas) + (eol if lineas else "")).encode("utf-8"))
        reemplazar_atomico(tmp, self.ruta)

    def listar(self):
        lineas, _ = self._leer()
        campanas = []
        for i, linea in enumerate(lineas):
            txt = linea.strip()
            if not txt:
                continue
            if txt.startswith("#"):
                m, habilitada = PATRON_DESACTIVADA.match(txt), False
            else:
                m, habilitada = PATRON_CAMPANA.match(txt), True
            if m:
                t, c, ini, fin = m.groups()
                campanas.append(Campana(t.strip(), c.strip(), ini.strip(), fin.strip(), habilitada, i, linea))
        return campanas

    def invalidas(self):
        """[(nro_línea, texto)] de las líneas que el bot ignora por no cumplir el formato."""
        lineas, _ = self._leer()
        return [(i + 1, l.strip()) for i, l in enumerate(lineas)
                if l.strip() and not l.strip().startswith("#") and not PATRON_CAMPANA.match(l.strip())]

    def _verificar(self, lineas, campana):
        if not (0 <= campana.linea < len(lineas)) or lineas[campana.linea] != campana.raw:
            raise ArchivoCambio("claves.txt cambió mientras lo editabas. Recargá la lista y volvé a intentar.")

    def agregar(self, campana):
        lineas, eol = self._leer()
        lineas.append(campana.como_linea())
        self._escribir(lineas, eol)

    def actualizar(self, original, nueva):
        lineas, eol = self._leer()
        self._verificar(lineas, original)
        lineas[original.linea] = nueva.como_linea()
        self._escribir(lineas, eol)

    def eliminar(self, campana):
        lineas, eol = self._leer()
        self._verificar(lineas, campana)
        del lineas[campana.linea]
        self._escribir(lineas, eol)

    def cambiar_habilitada(self, campana, habilitada):
        nueva = Campana(campana.titulo, campana.clave, campana.inicio, campana.fin, habilitada)
        self.actualizar(campana, nueva)


def validar_campana(titulo, clave, inicio, fin, otras):
    """Devuelve (errores, advertencias). 'otras' = campañas habilitadas, sin la que se edita."""
    errores, avisos = [], []
    titulo, clave = titulo.strip(), clave.strip()
    if not titulo:
        errores.append("Falta el título.")
    if not clave:
        errores.append("Falta la clave.")
    for nombre, valor in (("título", titulo), ("clave", clave)):
        if '"' in valor:
            errores.append(f"El {nombre} no puede contener comillas dobles (\").")
    if "{" in clave:
        errores.append("La clave no puede contener '{': APRS lo usa para el número de mensaje y la clave nunca coincidiría.")
    dt_i = dt_f = None
    try:
        dt_i = parsear_fecha(inicio)
    except ValueError:
        errores.append("La fecha de inicio debe ser DD/MM/AAAA (por ejemplo 15/09/2026).")
    try:
        dt_f = parsear_fecha(fin, fin_del_dia=True)
    except ValueError:
        errores.append("La fecha de fin debe ser DD/MM/AAAA (por ejemplo 20/09/2026).")
    if dt_i and dt_f and dt_f < dt_i:
        errores.append("La fecha de fin es anterior a la de inicio.")
    if dt_i and dt_f and clave:
        for o in otras:
            try:
                solapa = o.dt_inicio() <= dt_f and dt_i <= o.dt_fin()
            except ValueError:
                continue
            if o.clave.upper() == clave.upper() and solapa:
                errores.append(f"Ya existe una campaña con la clave '{o.clave}' en fechas que se superponen ({o.inicio} a {o.fin}).")
    if len(clave) > 30:
        avisos.append("La clave es muy larga: a los operadores les costará escribirla en un mensaje APRS.")
    return errores, avisos


# --------------------------------------------------------------------------
# Configuración (config.ini)
# --------------------------------------------------------------------------
ZONAS = ("utc", "local")
ENCABEZADO_CONFIG = (
    "# Configuración del bot APRS de concursos.\n"
    "# [estacion]: la lee el bot al iniciar (reinicialo para aplicar los cambios).\n"
    "# [panel]: solo cambia cómo el panel muestra los horarios (utc | local).\n\n")


def passcode_para(indicativo):
    """Clave APRS-IS que le corresponde a un indicativo (algoritmo estándar; ignora el SSID)."""
    base = indicativo.split("-")[0].strip().upper()
    h = 0x73E2
    for i in range(0, len(base), 2):
        h ^= ord(base[i]) << 8
        if i + 1 < len(base):
            h ^= ord(base[i + 1])
    return str(h & 0x7FFF)


def validar_configuracion(indicativo, passcode):
    """Lista de errores (vacía si los datos sirven)."""
    errores = []
    ind, pas = indicativo.strip().upper(), passcode.strip()
    if not ind:
        errores.append("Falta la señal distintiva.")
    elif not PATRON_INDICATIVO.fullmatch(ind):
        errores.append("La señal distintiva no tiene un formato válido (ejemplo: LU9DUV-B).")
    if not pas:
        errores.append("Falta la clave APRS-IS (passcode).")
    elif not re.fullmatch(r"\d{1,6}", pas):
        errores.append("La clave APRS-IS debe ser un número (ejemplo: 21413).")
    return errores


@dataclass
class Configuracion:
    indicativo: str = ""
    passcode: str = ""
    zona: str = "utc"  # "utc" | "local"


class ConfigStore:
    """config.ini: [estacion] la usa el bot; [panel] solo el panel."""

    def __init__(self, ruta):
        self.ruta = Path(ruta)

    def existe(self):
        return self.ruta.exists()

    def firma(self):
        try:
            st = self.ruta.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _leer(self):
        cfg = configparser.ConfigParser(interpolation=None)
        try:
            cfg.read(self.ruta, encoding="utf-8-sig")
            return cfg, ""
        except (configparser.Error, OSError, UnicodeDecodeError) as e:
            return configparser.ConfigParser(interpolation=None), f"No se pudo leer config.ini ({e})"

    def cargar(self):
        """(Configuracion, error). Un archivo ausente no es un error: devuelve valores vacíos."""
        cfg, error = self._leer()
        zona = cfg.get("panel", "zona_horaria", fallback="utc").strip().lower()
        return Configuracion(cfg.get("estacion", "indicativo", fallback="").strip().upper(),
                             cfg.get("estacion", "passcode", fallback="").strip(),
                             zona if zona in ZONAS else "utc"), error

    def guardar(self, c):
        cfg, _ = self._leer()  # conserva secciones o claves extra que haya agregado el usuario
        for seccion in ("estacion", "panel"):
            if not cfg.has_section(seccion):
                cfg.add_section(seccion)
        cfg.set("estacion", "indicativo", c.indicativo)
        cfg.set("estacion", "passcode", c.passcode)
        cfg.set("panel", "zona_horaria", c.zona)
        buf = io.StringIO()
        cfg.write(buf)
        texto = ENCABEZADO_CONFIG + buf.getvalue()
        if os.name == "nt":
            texto = texto.replace("\n", "\r\n")
        tmp = self.ruta.with_name(self.ruta.name + ".tmp")
        tmp.write_bytes(texto.encode("utf-8"))
        reemplazar_atomico(tmp, self.ruta)


# --------------------------------------------------------------------------
# Estadísticas (concurso_aprs.db, solo lectura)
# --------------------------------------------------------------------------
class StatsDB:
    def __init__(self, ruta):
        self.ruta = Path(ruta)
        self.error = ""

    def existe(self):
        return self.ruta.exists()

    def limpiar(self):
        """Borra el último error; cada pantalla lo llama antes de consultar."""
        self.error = ""

    def firma(self):
        """Cambia cuando el bot escribe en la base (archivo principal y -wal)."""
        partes = []
        for sufijo in ("", "-wal"):
            try:
                st = Path(str(self.ruta) + sufijo).stat()
                partes.append((st.st_mtime_ns, st.st_size))
            except OSError:
                partes.append(None)
        return tuple(partes)

    def _fallo(self, mensaje):
        if not self.error:  # conserva el primer error de la tanda de consultas
            self.error = mensaje

    def _consulta(self, sql, params=()):
        """Ejecuta una consulta de solo lectura; ante cualquier problema devuelve []."""
        if not self.existe():
            self._fallo("Todavía no existe concurso_aprs.db (el bot la crea al iniciar).")
            return []
        try:
            uri = self.ruta.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                return conn.execute(sql, params).fetchall()
            finally:
                conn.close()  # en Windows 'with' no cierra el archivo
        except sqlite3.Error as e:
            self._fallo(f"No se pudo leer la base de datos: {e}")
            return []

    def resumen(self, zona=None):
        zona = zona or Zona()
        hoy = zona.ahora().date()
        desde = (ahora_utc() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        part = self._consulta("SELECT COUNT(*), COALESCE(SUM(puntos), 0) FROM participantes")
        val = self._consulta("SELECT COUNT(*) FROM codigos_validados WHERE clave != ?", (LEGACY_KEY,))
        # "hoy" depende de la zona elegida: se cuenta en Python sobre los últimos días
        marcas_val = self._consulta(
            "SELECT fecha_hora_utc FROM codigos_validados WHERE clave != ? AND fecha_hora_utc >= ?",
            (LEGACY_KEY, desde))
        marcas_part = self._consulta(
            "SELECT fecha_contacto_utc FROM participantes WHERE fecha_contacto_utc >= ?", (desde,))
        return {
            "participantes": part[0][0] if part else 0,
            "puntos": part[0][1] if part else 0,
            "validaciones": val[0][0] if val else 0,
            "validaciones_hoy": sum(1 for (m,) in marcas_val if _fecha_en_zona(m, zona) == hoy),
            "nuevos_hoy": sum(1 for (m,) in marcas_part if _fecha_en_zona(m, zona) == hoy),
        }

    def ranking(self, zona=None):
        """[(posicion, licencia, puntos, primer_contacto, campañas)], con empates compartiendo posición."""
        zona = zona or Zona()
        filas = self._consulta("SELECT licencia, puntos, fecha_contacto_utc FROM participantes")
        camp = defaultdict(list)
        for lic, clave in self._consulta("SELECT licencia, clave FROM codigos_validados ORDER BY fecha_hora_utc"):
            camp[lic].append("(histórico)" if clave == LEGACY_KEY else clave)
        filas.sort(key=lambda f: (-(f[1] or 0), f[0]))
        resultado, pos_actual, previo = [], 0, None
        for i, (lic, puntos, fecha) in enumerate(filas, start=1):
            if puntos != previo:
                pos_actual, previo = i, puntos
            resultado.append((pos_actual, lic, puntos or 0, zona.marca(fecha or ""), ", ".join(camp.get(lic, []))))
        return resultado

    def por_clave(self):
        """{clave: (validaciones, participantes distintos)}"""
        filas = self._consulta("SELECT clave, COUNT(*), COUNT(DISTINCT licencia) FROM codigos_validados GROUP BY clave")
        return {c: (n, p) for c, n, p in filas}

    def por_dia(self, metrica, dias=14, zona=None):
        """(etiquetas dd/mm, valores) de los últimos 'dias' días de la zona elegida;
        metrica: 'validaciones' o 'nuevos'."""
        zona = zona or Zona()
        hoy = zona.ahora().date()
        fechas = [hoy - timedelta(days=i) for i in range(dias - 1, -1, -1)]
        desde = (ahora_utc() - timedelta(days=dias + 1)).strftime("%Y-%m-%d %H:%M:%S")
        if metrica == "validaciones":
            filas = self._consulta(
                "SELECT fecha_hora_utc FROM codigos_validados WHERE clave != ? AND fecha_hora_utc >= ?",
                (LEGACY_KEY, desde))
        else:
            filas = self._consulta(
                "SELECT fecha_contacto_utc FROM participantes WHERE fecha_contacto_utc >= ?", (desde,))
        cuenta = Counter(_fecha_en_zona(m, zona) for (m,) in filas)
        return [f.strftime("%d/%m") for f in fechas], [cuenta.get(f, 0) for f in fechas]


def exportar_ranking_csv(ruta, filas, etiqueta_zona="UTC"):
    with open(ruta, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig: Excel lo abre bien
        w = csv.writer(f, delimiter=";")
        w.writerow(["Posición", "Licencia", "Puntos", f"Primer contacto ({etiqueta_zona})", "Campañas validadas"])
        w.writerows(filas)


# --------------------------------------------------------------------------
# Logs del bot y registros ADIF
# --------------------------------------------------------------------------
def listar_logs(carpeta):
    """Archivos de log del bot (el actual y los rotados), más nuevos primero."""
    base = Path(carpeta)
    archivos = [p for p in base.glob(LOG_NAME + "*") if p.is_file()]
    return sorted(archivos, key=lambda p: p.stat().st_mtime, reverse=True)


def leer_log(ruta, max_bytes=1_500_000):
    """Devuelve las líneas del log (solo el final si es muy grande) y si se recortó."""
    ruta = Path(ruta)
    tam = ruta.stat().st_size
    with open(ruta, "rb") as f:
        recortado = tam > max_bytes
        if recortado:
            f.seek(tam - max_bytes)
            f.readline()  # descarta la línea cortada a la mitad
        datos = f.read()
    return datos.decode("utf-8", errors="replace").splitlines(), recortado


def filtrar_log(lineas, nivel_minimo="Todos", texto=""):
    """Devuelve [(nivel, linea)]. Las líneas sin nivel (ej: traceback) heredan el de la anterior."""
    orden = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
    minimo = {"Todos": 0, "Warning o superior": 2, "Solo errores": 3}.get(nivel_minimo, 0)
    texto = texto.strip().lower()
    salida, actual = [], "INFO"
    for linea in lineas:
        m = PATRON_NIVEL.search(linea)
        if m:
            actual = m.group(1)
        if orden.get(actual, 1) < minimo:
            continue
        if texto and texto not in linea.lower():
            continue
        salida.append((actual, linea))
    return salida


def convertir_linea_log(linea, zona):
    """Reescribe la marca de hora del bot (UTC) a la zona elegida; las demás líneas quedan igual."""
    if not zona.local:
        return linea
    m = PATRON_MARCA_LOG.match(linea)
    dt = _parsear_marca(m.group(1)) if m else None
    if not dt:
        return linea
    return f"{zona.convertir(dt):%Y-%m-%d %H:%M:%S} {zona.etiqueta(dt)}{linea[m.end():]}"


def ultima_marca_log(ruta):
    """datetime UTC de la última línea fechada del log, o None."""
    try:
        lineas, _ = leer_log(ruta, max_bytes=20_000)
    except OSError:
        return None
    for linea in reversed(lineas):
        m = PATRON_MARCA_LOG.match(linea)
        if m:
            return _parsear_marca(m.group(1))
    return None


def listar_adif(carpeta):
    return sorted(Path(carpeta).glob("*.adi"), key=lambda p: p.name, reverse=True)


def leer_adif(ruta, zona=None):
    """[(fecha, hora, indicativo, frecuencia, modo, estacion)] de un archivo .adi.
    ADIF guarda fecha y hora en UTC; se muestran en la zona elegida."""
    zona = zona or Zona()
    registros = []
    with open(ruta, "r", encoding="utf-8", errors="replace") as f:
        for linea in f:
            if "<eor>" not in linea.lower():
                continue
            campos = {}
            for m in PATRON_CAMPO_ADIF.finditer(linea):
                campos[m.group(1).upper()] = linea[m.end():m.end() + int(m.group(2))]
            if "CALL" not in campos:
                continue
            fecha, hora = campos.get("QSO_DATE", ""), campos.get("TIME_ON", "")
            f_txt = f"{fecha[:4]}-{fecha[4:6]}-{fecha[6:8]}" if len(fecha) == 8 else fecha
            h_txt = f"{hora[:2]}:{hora[2:4]}:{hora[4:6]}" if len(hora) >= 6 else hora
            if zona.local and len(fecha) == 8 and len(hora) >= 6:
                dt = _parsear_marca(f"{f_txt} {h_txt}")
                if dt:
                    local = zona.convertir(dt)
                    f_txt, h_txt = local.strftime("%Y-%m-%d"), local.strftime("%H:%M:%S")
            registros.append((f_txt, h_txt, campos["CALL"], campos.get("FREQ", ""), campos.get("MODE", ""),
                              campos.get("STATION_CALLSIGN", "")))
    return registros


def abrir_carpeta(carpeta):
    """Abre la carpeta en el explorador del sistema."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(carpeta))  # noqa: E1101 (solo Windows)
        elif sys.platform == "darwin":
            os.system(f'open "{carpeta}"')
        else:
            os.system(f'xdg-open "{carpeta}"')
    except OSError:
        pass


# ==========================================================================
# Interfaz gráfica
# ==========================================================================
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

DEFAULT_DIR = Path(__file__).resolve().parent
ES_WINDOWS = sys.platform.startswith("win")

BG = "#ffffff"
TEXTO = "#1f2328"
GRIS = "#59636e"
LINEA = "#d1d9e0"
ACENTO = "#1f6feb"
VERDE = "#1a7f37"
AMBAR = "#9a6700"
ROJO = "#cf222e"

COLOR_ESTADO = {"Activa": VERDE, "Programada": ACENTO, "Vencida": GRIS,
                "Desactivada": GRIS, "Fecha inválida": ROJO}
ORDEN_ESTADO = {"Activa": 0, "Programada": 1, "Vencida": 2, "Desactivada": 3, "Fecha inválida": 4}

_fuentes = {}


def fuente(base="TkDefaultFont", size=None, weight=None):
    """Fuente derivada de una fuente con nombre de Tk (se guarda para que no la recoja el GC)."""
    clave = (base, size, weight)
    if clave not in _fuentes:
        f = tkfont.nametofont(base).copy()
        if size:
            f.configure(size=size)
        if weight:
            f.configure(weight=weight)
        _fuentes[clave] = f
    return _fuentes[clave]


def crear_tabla(master, columnas, estirar, altura=8):
    """Treeview con scrollbars y orden al hacer clic en el encabezado.
    columnas = [(id, titulo, ancho, ancla)]. Devuelve (marco, arbol)."""
    marco = ttk.Frame(master)
    arbol = ttk.Treeview(marco, columns=[c[0] for c in columnas], show="headings",
                         height=altura, selectmode="browse")
    arbol._orden_invertido = {}
    for ident, titulo, ancho, ancla in columnas:
        arbol.heading(ident, text=titulo, command=lambda i=ident, a=arbol: ordenar_tabla(a, i))
        arbol.column(ident, width=ancho, minwidth=50, anchor=ancla, stretch=(ident == estirar))
    vsb = ttk.Scrollbar(marco, orient="vertical", command=arbol.yview)
    hsb = ttk.Scrollbar(marco, orient="horizontal", command=arbol.xview)
    arbol.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    arbol.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    hsb.grid(row=1, column=0, sticky="ew")
    marco.rowconfigure(0, weight=1)
    marco.columnconfigure(0, weight=1)
    return marco, arbol


def ordenar_tabla(arbol, columna):
    invertir = not arbol._orden_invertido.get(columna, True)
    arbol._orden_invertido = {columna: invertir}

    def clave(iid):
        valor = arbol.set(iid, columna)
        try:
            return (0, float(valor))
        except ValueError:
            return (1, valor.lower())

    filas = sorted(arbol.get_children(""), key=clave, reverse=invertir)
    for pos, iid in enumerate(filas):
        arbol.move(iid, "", pos)


class PestanaBase(ttk.Frame):
    """Pestaña con refresco periódico: 'cada' ms, solo mientras está visible."""
    cada = 10_000
    ultimo = 0.0
    firma = None

    def __init__(self, app):
        super().__init__(app.notebook, padding=(16, 12))
        self.app = app

    def reset(self):
        self.firma = None

    def aplicar_zona(self):
        """Actualiza los títulos que mencionan la zona horaria (UTC / local)."""

    def refresh(self, forzar=False):
        raise NotImplementedError


class Grafico(tk.Canvas):
    """Gráfico de barras simple, sin dependencias externas."""

    def __init__(self, master):
        super().__init__(master, height=200, highlightthickness=0, background=BG)
        self.etiquetas, self.valores = [], []
        self.bind("<Configure>", lambda e: self.dibujar())

    def set_datos(self, etiquetas, valores):
        self.etiquetas, self.valores = list(etiquetas), list(valores)
        self.dibujar()

    def dibujar(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        n = len(self.valores)
        if w < 80 or h < 80 or n == 0:
            return
        izq, der, arriba, abajo = 36, 10, 14, 26
        ancho, alto = w - izq - der, h - arriba - abajo
        techo = max(4, -(-max(self.valores) // 4) * 4)  # múltiplo de 4: marcas enteras
        f_chica = fuente(size=8)
        for i in range(5):
            valor = techo * i // 4
            y = arriba + alto - alto * i / 4
            self.create_line(izq, y, w - der, y, fill=LINEA)
            self.create_text(izq - 6, y, text=str(valor), anchor="e", fill=GRIS, font=f_chica)
        slot = ancho / n
        paso = max(1, int(-(-n * 38 // ancho)))  # evita que se pisen las etiquetas del eje
        for i, (etq, val) in enumerate(zip(self.etiquetas, self.valores)):
            x0 = izq + i * slot + slot * 0.2
            x1 = izq + (i + 1) * slot - slot * 0.2
            y1 = arriba + alto
            y0 = y1 - alto * val / techo
            if val:
                self.create_rectangle(x0, y0, x1, y1, fill=ACENTO, outline="")
                if slot >= 22:
                    self.create_text((x0 + x1) / 2, y0 - 8, text=str(val), fill=TEXTO, font=f_chica)
            if i % paso == 0 or i == n - 1:
                self.create_text((x0 + x1) / 2, y1 + 12, text=etq, fill=GRIS, font=f_chica)


# --------------------------------------------------------------------------
class ResumenTab(PestanaBase):
    METRICAS = {"Validaciones de clave por día": "validaciones",
                "Participantes nuevos por día": "nuevos"}

    def __init__(self, app):
        super().__init__(app)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=2)
        self.rowconfigure(5, weight=3)

        self.aviso = tk.StringVar()
        ttk.Label(self, textvariable=self.aviso, foreground=AMBAR, wraplength=900).grid(row=0, column=0, sticky="w")

        cifras = ttk.Frame(self)
        cifras.grid(row=1, column=0, sticky="w", pady=(2, 12))
        self.cifras, self.leyendas = {}, {}
        definicion = [("participantes", "Participantes"), ("puntos", "Puntos otorgados"),
                      ("validaciones", "Claves validadas"), ("validaciones_hoy", "Validadas hoy (UTC)"),
                      ("nuevos_hoy", "Participantes nuevos hoy")]
        for i, (k, texto) in enumerate(definicion):
            bloque = ttk.Frame(cifras, padding=(0, 0, 32, 0))
            bloque.grid(row=0, column=i, sticky="w")
            valor = ttk.Label(bloque, text="0", font=fuente(size=20, weight="bold"))
            valor.grid(row=0, column=0, sticky="w")
            leyenda = ttk.Label(bloque, text=texto, foreground=GRIS)
            leyenda.grid(row=1, column=0, sticky="w")
            self.cifras[k] = valor
            self.leyendas[k] = leyenda

        ctrl = ttk.Frame(self)
        ctrl.grid(row=2, column=0, sticky="ew")
        self.metrica = tk.StringVar(value=next(iter(self.METRICAS)))
        self.dias = tk.StringVar(value="14 días")
        cb1 = ttk.Combobox(ctrl, textvariable=self.metrica, values=list(self.METRICAS), state="readonly", width=30)
        cb2 = ttk.Combobox(ctrl, textvariable=self.dias, values=["7 días", "14 días", "30 días"], state="readonly", width=9)
        cb1.pack(side="left")
        cb2.pack(side="left", padx=(8, 0))
        for cb in (cb1, cb2):
            cb.bind("<<ComboboxSelected>>", lambda e: self.refresh(forzar=True))

        self.grafico = Grafico(self)
        self.grafico.grid(row=3, column=0, sticky="nsew", pady=(6, 0))

        ttk.Label(self, text="Actividad por campaña", font=fuente(weight="bold")).grid(row=4, column=0, sticky="w", pady=(12, 4))
        marco, self.tabla = crear_tabla(self, [
            ("estado", "Estado", 100, "w"), ("campana", "Campaña", 280, "w"), ("clave", "Clave", 170, "w"),
            ("validaciones", "Validaciones", 100, "e"), ("participantes", "Participantes", 100, "e")],
            estirar="campana", altura=6)
        marco.grid(row=5, column=0, sticky="nsew")
        for e, c in COLOR_ESTADO.items():
            self.tabla.tag_configure(e, foreground=c)
        self.aplicar_zona()

    def aplicar_zona(self):
        z = self.app.zona.etiqueta()
        self.leyendas["validaciones_hoy"].configure(text=f"Validadas hoy ({z})")
        self.leyendas["nuevos_hoy"].configure(text=f"Participantes nuevos hoy ({z})")

    def refresh(self, forzar=False):
        db = self.app.db
        firma = (db.firma(), self.app.claves.firma(), self.metrica.get(), self.dias.get(), self.app.clave_dia())
        if firma == self.firma and not forzar:
            return
        self.firma = firma
        db.limpiar()

        r = db.resumen(self.app.zona)
        for k, etiqueta in self.cifras.items():
            etiqueta.configure(text=str(r[k]))

        dias = int(self.dias.get().split()[0])
        etq, val = db.por_dia(self.METRICAS[self.metrica.get()], dias, self.app.zona)
        self.grafico.set_datos(etq, val)

        por_clave = db.por_clave()
        filas, en_archivo = [], set()
        campanas = sorted(self.app.claves.listar(), key=lambda c: ORDEN_ESTADO.get(c.estado(), 9))
        for c in campanas:
            en_archivo.add(c.clave)
            n, p = por_clave.get(c.clave, (0, 0))
            filas.append((c.estado(), c.titulo, c.clave, n, p))
        for clave, (n, p) in por_clave.items():
            if clave == LEGACY_KEY:
                filas.append(("Histórico", "Puntos del modelo anterior", "—", n, p))
            elif clave not in en_archivo:
                filas.append(("Sin definir", "(ya no está en claves.txt)", clave, n, p))
        self.tabla.delete(*self.tabla.get_children())
        for f in filas:
            self.tabla.insert("", "end", values=f, tags=(f[0],))
        self.aviso.set(db.error)


# --------------------------------------------------------------------------
class RankingTab(PestanaBase):
    def __init__(self, app):
        super().__init__(app)
        self.filas = []
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        barra = ttk.Frame(self)
        barra.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(barra, text="Buscar licencia:").pack(side="left")
        self.buscar = tk.StringVar()
        ttk.Entry(barra, textvariable=self.buscar, width=20).pack(side="left", padx=(6, 16))
        self.buscar.trace_add("write", lambda *_: self._mostrar())
        self.conteo = ttk.Label(barra, foreground=GRIS)
        self.conteo.pack(side="left")
        ttk.Button(barra, text="Exportar a CSV…", command=self.exportar).pack(side="right")

        marco, self.arbol = crear_tabla(self, [
            ("pos", "Posición", 80, "center"), ("licencia", "Licencia", 140, "w"), ("puntos", "Puntos", 80, "e"),
            ("primero", "Primer contacto (UTC)", 160, "w"), ("campanas", "Campañas validadas", 380, "w")],
            estirar="campanas")
        marco.grid(row=1, column=0, sticky="nsew")
        self.aplicar_zona()

    def aplicar_zona(self):
        self.arbol.heading("primero", text=f"Primer contacto ({self.app.zona.etiqueta()})")

    def refresh(self, forzar=False):
        firma = (self.app.db.firma(), self.app.clave_dia())
        if firma == self.firma and not forzar:
            return
        self.firma = firma
        self.app.db.limpiar()
        self.filas = self.app.db.ranking(self.app.zona)
        self._mostrar()

    def _mostrar(self):
        q = self.buscar.get().strip().upper()
        filas = [f for f in self.filas if q in f[1].upper()]
        self.arbol.delete(*self.arbol.get_children())
        for f in filas:
            self.arbol.insert("", "end", values=f)
        self.conteo.configure(text=f"{len(filas)} de {len(self.filas)} participantes")

    def exportar(self):
        if not self.filas:
            messagebox.showinfo("Exportar ranking", "Todavía no hay participantes para exportar.", parent=self)
            return
        ruta = filedialog.asksaveasfilename(
            parent=self, defaultextension=".csv", filetypes=[("CSV (Excel)", "*.csv")],
            initialfile=f"ranking_{ahora_utc().strftime('%Y%m%d')}.csv")
        if not ruta:
            return
        try:
            exportar_ranking_csv(ruta, self.filas, self.app.zona.etiqueta())
        except OSError as e:
            messagebox.showerror("Exportar ranking", f"No se pudo guardar el archivo:\n{e}", parent=self)
            return
        messagebox.showinfo("Exportar ranking", f"Ranking guardado en:\n{ruta}", parent=self)


# --------------------------------------------------------------------------
class CampanaDialog(tk.Toplevel):
    """Diálogo modal para crear o modificar una campaña. Deja el resultado en self.resultado."""

    def __init__(self, master, titulo_ventana, campana, otras, callsign, zona=None):
        super().__init__(master)
        self.title(titulo_ventana)
        self.transient(master)
        self.resizable(False, False)
        self.configure(background=BG)
        self.resultado = None
        self.otras, self.callsign = otras, callsign
        self.zona = zona or Zona()

        marco = ttk.Frame(self, padding=18)
        marco.grid(row=0, column=0)
        self.v_titulo = tk.StringVar(value=campana.titulo)
        self.v_clave = tk.StringVar(value=campana.clave)
        self.v_inicio = tk.StringVar(value=campana.inicio)
        self.v_fin = tk.StringVar(value=campana.fin)
        self.v_activa = tk.BooleanVar(value=campana.habilitada)

        def fila(r, texto, var, ancho):
            ttk.Label(marco, text=texto).grid(row=r, column=0, sticky="w", pady=5, padx=(0, 14))
            e = ttk.Entry(marco, textvariable=var, width=ancho)
            e.grid(row=r, column=1, sticky="w", pady=5)
            return e

        e_titulo = fila(0, "Título de la actividad", self.v_titulo, 46)
        fila(1, "Clave que envían los operadores", self.v_clave, 28)
        fila(2, "Válida desde (UTC)", self.v_inicio, 14)
        fila(3, "Válida hasta (UTC)", self.v_fin, 14)
        self.nota_base = ("Fechas en formato DD/MM/AAAA. La campaña rige desde las 00:00 del primer día "
                          "hasta las 23:59 del último, en hora UTC. Las mayúsculas de la clave no importan.")
        self.l_nota = ttk.Label(marco, foreground=GRIS, wraplength=430, justify="left", text=self.nota_base)
        self.l_nota.grid(row=4, column=0, columnspan=2, sticky="w", pady=(2, 10))
        ttk.Checkbutton(marco, text="Campaña activada (si no, el bot la ignora)",
                        variable=self.v_activa).grid(row=5, column=0, columnspan=2, sticky="w")

        prev = ttk.LabelFrame(marco, text="Boletín que se difundirá", padding=10)
        prev.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.l_prev = ttk.Label(prev, wraplength=430, justify="left", font=fuente("TkFixedFont"))
        self.l_prev.grid(row=0, column=0, sticky="w")
        self.l_largo = ttk.Label(prev, foreground=GRIS)
        self.l_largo.grid(row=1, column=0, sticky="w", pady=(4, 0))

        botones = ttk.Frame(marco)
        botones.grid(row=7, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(botones, text="Cancelar", command=self.destroy).pack(side="right")
        ttk.Button(botones, text="Guardar", style="Acento.TButton", command=self._guardar).pack(side="right", padx=(0, 8))

        for v in (self.v_titulo, self.v_clave, self.v_inicio, self.v_fin):
            v.trace_add("write", lambda *_: self._vista_previa())
        self._vista_previa()
        self.bind("<Return>", lambda e: self._guardar())
        self.bind("<Escape>", lambda e: self.destroy())

        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        e_titulo.focus_set()
        try:
            self.wait_visibility()  # grab_set falla en algunos sistemas si la ventana aún no se mostró
            self.grab_set()
        except tk.TclError:
            pass

    def _nota_local(self):
        """En modo hora local, aclara a qué horario local equivale la vigencia (que el bot evalúa en UTC)."""
        extra = ""
        if self.zona.local:
            try:
                ini = self.zona.convertir(parsear_fecha(self.v_inicio.get()))
                fin = self.zona.convertir(parsear_fecha(self.v_fin.get(), fin_del_dia=True))
                extra = (f"\nEn hora local ({self.zona.etiqueta(ini)}) rige desde el {ini:%d/%m %H:%M} "
                         f"hasta el {fin:%d/%m %H:%M}.")
            except ValueError:
                pass
        self.l_nota.configure(text=self.nota_base + extra)

    def _vista_previa(self):
        self._nota_local()
        try:
            fin = parsear_fecha(self.v_fin.get())
        except ValueError:
            self.l_prev.configure(text="(completá una fecha de fin válida para ver el boletín)")
            self.l_largo.configure(text="", foreground=GRIS)
            return
        texto, recortado = texto_boletin(self.v_titulo.get().strip() or "Título", self.v_clave.get().strip() or "CLAVE",
                                         fin, self.callsign)
        self.l_prev.configure(text=texto)
        aviso = "  ·  el título se recorta para entrar en un mensaje APRS" if recortado else ""
        self.l_largo.configure(text=f"{len(texto)} de {LIMITE_APRS} caracteres{aviso}",
                               foreground=AMBAR if recortado else GRIS)

    def _guardar(self):
        errores, avisos = validar_campana(self.v_titulo.get(), self.v_clave.get(),
                                          self.v_inicio.get(), self.v_fin.get(), self.otras)
        if errores:
            messagebox.showerror("Revisá los datos", "\n".join("• " + e for e in errores), parent=self)
            return
        if avisos and not messagebox.askyesno("Revisá los datos", "\n".join("• " + a for a in avisos) +
                                              "\n\n¿Guardar de todos modos?", parent=self):
            return
        self.resultado = Campana(self.v_titulo.get().strip(), self.v_clave.get().strip(),
                                 self.v_inicio.get().strip(), self.v_fin.get().strip(), self.v_activa.get())
        self.destroy()


class CampanasTab(PestanaBase):
    def __init__(self, app):
        super().__init__(app)
        self.campanas = {}
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        barra = ttk.Frame(self)
        barra.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(barra, text="Nueva campaña…", style="Acento.TButton", command=self.nueva).pack(side="left")
        self.b_mod = ttk.Button(barra, text="Modificar…", command=self.modificar)
        self.b_mod.pack(side="left", padx=(8, 0))
        self.b_toggle = ttk.Button(barra, text="Desactivar", command=self.alternar)
        self.b_toggle.pack(side="left", padx=(8, 0))
        self.b_del = ttk.Button(barra, text="Eliminar", command=self.eliminar)
        self.b_del.pack(side="left", padx=(8, 0))
        ttk.Label(barra, foreground=GRIS, text="Los cambios se guardan en claves.txt y el bot los toma solo.").pack(side="right")

        marco, self.arbol = crear_tabla(self, [
            ("estado", "Estado", 100, "w"), ("titulo", "Título", 270, "w"), ("clave", "Clave", 180, "w"),
            ("inicio", "Desde (UTC)", 100, "center"), ("fin", "Hasta (UTC)", 100, "center"),
            ("val", "Validaciones", 100, "e")], estirar="titulo")
        marco.grid(row=1, column=0, sticky="nsew")
        for e, c in COLOR_ESTADO.items():
            self.arbol.tag_configure(e, foreground=c)
        self.vacio = ttk.Label(self, foreground=GRIS, text="Todavía no hay campañas. Creá la primera con «Nueva campaña…».")
        self.vacio.grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.aviso = ttk.Label(self, foreground=AMBAR, wraplength=900, justify="left")
        self.aviso.grid(row=3, column=0, sticky="w", pady=(8, 0))

        self.arbol.bind("<<TreeviewSelect>>", lambda e: self._botones())
        self.arbol.bind("<Double-1>", self._doble_clic)
        self.arbol.bind("<Return>", lambda e: self.modificar())
        self.arbol.bind("<Delete>", lambda e: self.eliminar())
        self._botones()

    # -- datos ------------------------------------------------------------
    def refresh(self, forzar=False):
        firma = (self.app.claves.firma(), self.app.db.firma(), self.app.clave_dia())
        if firma == self.firma and not forzar:
            return
        self.firma = firma
        previa = self._seleccionada()
        self.campanas = {str(c.linea): c for c in self.app.claves.listar()}
        por_clave = self.app.db.por_clave()
        self.arbol.delete(*self.arbol.get_children())
        for iid, c in self.campanas.items():
            estado = c.estado()
            self.arbol.insert("", "end", iid=iid, tags=(estado,),
                              values=(estado, c.titulo, c.clave, c.inicio, c.fin, por_clave.get(c.clave, (0, 0))[0]))
        if previa and str(previa.linea) in self.campanas:
            self.arbol.selection_set(str(previa.linea))
        if self.campanas:
            self.vacio.grid_remove()
        else:
            self.vacio.grid()
        invalidas = self.app.claves.invalidas()
        if invalidas:
            ejemplos = "; ".join(f"línea {n}: {t[:50]}" for n, t in invalidas[:3])
            self.aviso.configure(text=f"⚠ El bot ignora {len(invalidas)} línea(s) de claves.txt por no tener el formato "
                                      f'"TÍTULO", "CLAVE", DD/MM/AAAA, DD/MM/AAAA  ({ejemplos})')
        else:
            self.aviso.configure(text="")
        self._botones()

    def _seleccionada(self):
        sel = self.arbol.selection()
        return self.campanas.get(sel[0]) if sel else None

    def _validaciones(self, campana):
        return self.app.db.por_clave().get(campana.clave, (0, 0))[0]

    def _botones(self):
        c = self._seleccionada()
        estado = "normal" if c else "disabled"
        for b in (self.b_mod, self.b_toggle, self.b_del):
            b.configure(state=estado)
        self.b_toggle.configure(text="Activar" if c and not c.habilitada else "Desactivar")

    def _doble_clic(self, evento):
        if self.arbol.identify_row(evento.y):
            self.modificar()

    # -- acciones ---------------------------------------------------------
    def _guardar_cambio(self, accion, mensaje_ok):
        """Ejecuta una modificación de claves.txt mostrando errores de forma clara."""
        try:
            accion()
        except ArchivoCambio as e:
            messagebox.showwarning("claves.txt cambió", str(e), parent=self)
            self.refresh(forzar=True)
            return False
        except OSError as e:
            messagebox.showerror("No se pudo guardar", f"No se pudo escribir claves.txt:\n{e}", parent=self)
            return False
        self.app.mensaje(mensaje_ok)
        self.refresh(forzar=True)
        return True

    def nueva(self):
        hoy = ahora_utc()
        base = Campana("", "", hoy.strftime("%d/%m/%Y"), (hoy + timedelta(days=7)).strftime("%d/%m/%Y"))
        self._editar("Nueva campaña", base, None)

    def modificar(self):
        c = self._seleccionada()
        if c:
            self._editar("Modificar campaña", c, c)

    def _editar(self, titulo_ventana, base, original):
        otras = [c for c in self.app.claves.listar()
                 if c.habilitada and (original is None or c.linea != original.linea)]
        dlg = CampanaDialog(self.winfo_toplevel(), titulo_ventana, base, otras, self.app.callsign, self.app.zona)
        self.wait_window(dlg)
        nueva = dlg.resultado
        if nueva is None:
            return
        if original is not None and nueva.clave.upper() != original.clave.upper():
            n = self._validaciones(original)
            if n and not messagebox.askyesno(
                    "Cambio de clave",
                    f"Esta campaña ya tiene {n} validaciones registradas con la clave «{original.clave}».\n\n"
                    "Si cambiás la clave, quienes ya puntuaron podrían volver a puntuar con la nueva.\n\n"
                    "¿Guardar el cambio igual?", icon="warning", parent=self):
                return
        if original is None:
            self._guardar_cambio(lambda: self.app.claves.agregar(nueva), f"Campaña «{nueva.titulo}» creada.")
        else:
            self._guardar_cambio(lambda: self.app.claves.actualizar(original, nueva),
                                 f"Campaña «{nueva.titulo}» guardada.")

    def alternar(self):
        c = self._seleccionada()
        if c:
            self._guardar_cambio(lambda: self.app.claves.cambiar_habilitada(c, not c.habilitada),
                                 f"Campaña «{c.titulo}» {'desactivada' if c.habilitada else 'activada'}.")

    def eliminar(self):
        c = self._seleccionada()
        if not c:
            return
        n = self._validaciones(c)
        texto = f"¿Eliminar la campaña «{c.titulo}» (clave {c.clave})?"
        if n:
            texto += f"\n\nLas {n} validaciones ya registradas y los puntos de los participantes se conservan."
        texto += "\n\nSi solo querés pausarla, usá «Desactivar»."
        if messagebox.askyesno("Eliminar campaña", texto, icon="warning", parent=self):
            self._guardar_cambio(lambda: self.app.claves.eliminar(c), f"Campaña «{c.titulo}» eliminada.")


# --------------------------------------------------------------------------
class LogsTab(PestanaBase):
    cada = 2000
    NIVELES = ["Todos", "Warning o superior", "Solo errores"]

    def __init__(self, app):
        super().__init__(app)
        self._nombres = []
        self._debounce = None
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        barra = ttk.Frame(self)
        barra.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.archivo = tk.StringVar()
        self.nivel = tk.StringVar(value=self.NIVELES[0])
        self.buscar = tk.StringVar()
        self.seguir = tk.BooleanVar(value=True)
        self.cb_archivo = ttk.Combobox(barra, textvariable=self.archivo, state="readonly", width=24)
        cb_nivel = ttk.Combobox(barra, textvariable=self.nivel, values=self.NIVELES, state="readonly", width=18)
        self.cb_archivo.pack(side="left")
        cb_nivel.pack(side="left", padx=(8, 0))
        ttk.Label(barra, text="Buscar:").pack(side="left", padx=(14, 6))
        ttk.Entry(barra, textvariable=self.buscar, width=22).pack(side="left")
        ttk.Checkbutton(barra, text="Seguir en vivo", variable=self.seguir).pack(side="left", padx=(14, 0))
        ttk.Button(barra, text="Abrir carpeta", command=lambda: abrir_carpeta(self.app.carpeta)).pack(side="right")
        ttk.Button(barra, text="Ir al final", command=lambda: self.texto.see("end")).pack(side="right", padx=(0, 8))
        for cb in (self.cb_archivo, cb_nivel):
            cb.bind("<<ComboboxSelected>>", lambda e: self.refresh(forzar=True))
        self.buscar.trace_add("write", self._buscar_cambio)

        fm = fuente("TkFixedFont")
        if ES_WINDOWS:
            fm = fuente("TkFixedFont", size=9)
            fm.configure(family="Consolas")
        self.texto = tk.Text(self, wrap="none", state="disabled", font=fm, background=BG, foreground=TEXTO,
                             relief="flat", borderwidth=0, highlightthickness=1, highlightbackground=LINEA,
                             highlightcolor=LINEA, padx=8, pady=6)
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.texto.yview)
        hsb = ttk.Scrollbar(self, orient="horizontal", command=self.texto.xview)
        self.texto.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.texto.grid(row=1, column=0, sticky="nsew")
        vsb.grid(row=1, column=1, sticky="ns")
        hsb.grid(row=2, column=0, sticky="ew")
        self.texto.tag_configure("ERROR", foreground=ROJO)
        self.texto.tag_configure("CRITICAL", foreground=ROJO, font=fuente("TkFixedFont", weight="bold"))
        self.texto.tag_configure("WARNING", foreground=AMBAR)
        self.texto.tag_configure("DEBUG", foreground=GRIS)

        self.estado = ttk.Label(self, foreground=GRIS)
        self.estado.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def reset(self):
        super().reset()
        self._nombres = []

    def _buscar_cambio(self, *_):
        if self._debounce:
            self.after_cancel(self._debounce)
        self._debounce = self.after(300, lambda: self.refresh(forzar=True))

    def _poner(self, texto):
        self.texto.configure(state="normal")
        self.texto.delete("1.0", "end")
        self.texto.insert("end", texto)
        self.texto.configure(state="disabled")

    def refresh(self, forzar=False):
        if not self.seguir.get() and not forzar:
            return
        archivos = listar_logs(self.app.carpeta)
        nombres = [a.name for a in archivos]
        if nombres != self._nombres:
            self._nombres = nombres
            self.cb_archivo.configure(values=nombres)
            if self.archivo.get() not in nombres:
                self.archivo.set(nombres[0] if nombres else "")
        if not nombres:
            self._poner(f"Todavía no hay archivos {LOG_NAME} en esta carpeta.\n"
                        "El bot los crea al iniciar; si lo corrés desde otra carpeta, usá Archivo > Cambiar carpeta de datos.")
            self.estado.configure(text="")
            self.firma = None
            return
        ruta = self.app.carpeta / self.archivo.get()
        try:
            st = ruta.stat()
            firma = (str(ruta), st.st_size, st.st_mtime_ns, self.nivel.get(), self.buscar.get())
            if firma == self.firma and not forzar:
                return
            lineas, recortado = leer_log(ruta)
            lineas = [convertir_linea_log(l, self.app.zona) for l in lineas]
        except OSError as e:
            self.estado.configure(text=f"No se pudo leer el log: {e}")
            return
        al_final = self.texto.yview()[1] >= 0.999 or self.firma is None or (self.firma and self.firma[0] != str(ruta))
        self.firma = firma
        filtradas = filtrar_log(lineas, self.nivel.get(), self.buscar.get())
        self.texto.configure(state="normal")
        self.texto.delete("1.0", "end")
        for nivel, linea in filtradas:
            self.texto.insert("end", linea + "\n", nivel if nivel in ("ERROR", "CRITICAL", "WARNING", "DEBUG") else ())
        self.texto.configure(state="disabled")
        if al_final:
            self.texto.see("end")
        extra = " · se muestra solo el final del archivo" if recortado else ""
        self.estado.configure(text=f"{len(filtradas)} de {len(lineas)} líneas · actualizado {self.app.zona.ahora():%H:%M:%S} {self.app.zona.etiqueta()}{extra}")


# --------------------------------------------------------------------------
class QsoTab(PestanaBase):
    TODOS = "(todos los días)"

    def __init__(self, app):
        super().__init__(app)
        self._nombres = []
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        barra = ttk.Frame(self)
        barra.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.archivo = tk.StringVar(value=self.TODOS)
        self.cb = ttk.Combobox(barra, textvariable=self.archivo, state="readonly", width=24, values=[self.TODOS])
        self.cb.pack(side="left")
        self.cb.bind("<<ComboboxSelected>>", lambda e: self.refresh(forzar=True))
        self.conteo = ttk.Label(barra, foreground=GRIS)
        self.conteo.pack(side="left", padx=(14, 0))
        ttk.Button(barra, text="Abrir carpeta", command=lambda: abrir_carpeta(self.app.carpeta)).pack(side="right")

        marco, self.arbol = crear_tabla(self, [
            ("fecha", "Fecha (UTC)", 100, "w"), ("hora", "Hora (UTC)", 90, "w"), ("call", "Indicativo", 150, "w"),
            ("freq", "Frecuencia (MHz)", 120, "w"), ("modo", "Modo", 70, "w"), ("estacion", "Estación", 120, "w")],
            estirar="estacion")
        marco.grid(row=1, column=0, sticky="nsew")
        self.aplicar_zona()

    def aplicar_zona(self):
        z = self.app.zona.etiqueta()
        self.arbol.heading("fecha", text=f"Fecha ({z})")
        self.arbol.heading("hora", text=f"Hora ({z})")

    def reset(self):
        super().reset()
        self._nombres = []
        self.archivo.set(self.TODOS)

    def refresh(self, forzar=False):
        archivos = listar_adif(self.app.carpeta)
        nombres = [a.name for a in archivos]
        if nombres != self._nombres:
            self._nombres = nombres
            self.cb.configure(values=[self.TODOS] + nombres)
            if self.archivo.get() not in nombres + [self.TODOS]:
                self.archivo.set(self.TODOS)
        elegidos = archivos if self.archivo.get() == self.TODOS else [a for a in archivos if a.name == self.archivo.get()]
        firma = tuple((a.name, a.stat().st_mtime_ns, a.stat().st_size) for a in elegidos)
        if firma == self.firma and not forzar:
            return
        self.firma = firma
        filas = []
        for a in elegidos:
            try:
                filas.extend(leer_adif(a, self.app.zona))
            except OSError:
                continue
        filas.sort(reverse=True)  # más recientes primero
        self.arbol.delete(*self.arbol.get_children())
        for f in filas:
            self.arbol.insert("", "end", values=f)
        etiqueta = "contacto" if len(filas) == 1 else "contactos"
        self.conteo.configure(text=f"{len(filas)} {etiqueta}" if filas else "Todavía no hay contactos registrados en ADIF.")


# --------------------------------------------------------------------------
class ConfigTab(PestanaBase):
    """Señal distintiva y clave APRS-IS (las lee el bot) y zona horaria del panel."""
    cada = 3_600_000  # no necesita refresco periódico: se recarga al abrirla si el archivo cambió

    def __init__(self, app):
        super().__init__(app)
        self.cargada = None  # Configuracion tal como está guardada (o precargada del script)
        self.v_ind, self.v_pass = tk.StringVar(), tk.StringVar()
        self.v_mostrar = tk.BooleanVar(value=False)
        self.v_zona = tk.StringVar(value="utc")

        cont = ttk.Frame(self)
        cont.grid(row=0, column=0, sticky="nw")

        est = ttk.LabelFrame(cont, text="Estación", padding=16)
        est.grid(row=0, column=0, sticky="ew")
        ttk.Label(est, text="Señal distintiva").grid(row=0, column=0, sticky="w", padx=(0, 16), pady=(0, 4))
        ttk.Entry(est, textvariable=self.v_ind, width=18).grid(row=0, column=1, sticky="w", pady=(0, 4))
        ttk.Label(est, foreground=GRIS, wraplength=520, justify="left",
                  text="Con el SSID si lo usás (por ejemplo LU9DUV-B). Es la estación a la que los operadores "
                       "envían sus mensajes y la que figura en los boletines."
                  ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 14))
        ttk.Label(est, text="Clave APRS-IS (passcode)").grid(row=2, column=0, sticky="w", padx=(0, 16))
        fila = ttk.Frame(est)
        fila.grid(row=2, column=1, sticky="w")
        self.e_pass = ttk.Entry(fila, textvariable=self.v_pass, width=12, show="•")
        self.e_pass.pack(side="left")
        ttk.Checkbutton(fila, text="Mostrar", variable=self.v_mostrar,
                        command=self._mostrar_clave).pack(side="left", padx=(10, 0))
        ttk.Button(fila, text="Calcular desde la señal", command=self.calcular).pack(side="left", padx=(10, 0))
        self.l_clave = ttk.Label(est, wraplength=520, justify="left")
        self.l_clave.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(est, foreground=GRIS, wraplength=520, justify="left",
                  text="El bot lee la señal y la clave solo al iniciar: reinicialo para que tome los cambios."
                  ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))

        hor = ttk.LabelFrame(cont, text="Horarios en el panel", padding=16)
        hor.grid(row=1, column=0, sticky="ew", pady=(16, 0))
        ttk.Radiobutton(hor, text="UTC (igual que el bot, los logs y los archivos ADIF)",
                        variable=self.v_zona, value="utc").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(hor, text=f"Hora local de esta PC ({Zona('local').etiqueta()})",
                        variable=self.v_zona, value="local").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Label(hor, foreground=GRIS, wraplength=520, justify="left",
                  text="Solo cambia cómo se muestran los horarios en este panel (logs, contactos, ranking y "
                       "estadísticas por día). El bot y sus archivos siguen usando UTC, y las fechas de vigencia "
                       "de las campañas siempre se interpretan en UTC."
                  ).grid(row=2, column=0, sticky="w", pady=(10, 0))

        botones = ttk.Frame(cont)
        botones.grid(row=2, column=0, sticky="w", pady=(16, 0))
        ttk.Button(botones, text="Guardar", style="Acento.TButton", command=self.guardar).pack(side="left")
        ttk.Button(botones, text="Descartar cambios", command=self.descartar).pack(side="left", padx=(8, 0))
        self.l_estado = ttk.Label(botones, foreground=GRIS)
        self.l_estado.pack(side="left", padx=(16, 0))
        self.l_archivo = ttk.Label(cont, foreground=GRIS, wraplength=560, justify="left")
        self.l_archivo.grid(row=3, column=0, sticky="w", pady=(16, 0))

        for v in (self.v_ind, self.v_pass, self.v_zona):
            v.trace_add("write", self._al_editar)

    # -- estado del formulario ----------------------------------------------
    def _valores(self):
        return Configuracion(self.v_ind.get().strip().upper(), self.v_pass.get().strip(), self.v_zona.get())

    def _hay_cambios(self):
        return self.cargada is not None and self._valores() != self.cargada

    def _al_editar(self, *_):
        ind, pas = self.v_ind.get().strip().upper(), self.v_pass.get().strip()
        if not PATRON_INDICATIVO.fullmatch(ind) or not re.fullmatch(r"\d{1,6}", pas):
            self.l_clave.configure(text="", foreground=GRIS)
        elif pas == passcode_para(ind):
            self.l_clave.configure(text="✓ La clave coincide con la señal distintiva.", foreground=VERDE)
        else:
            self.l_clave.configure(
                text="⚠ La clave no coincide con la señal distintiva: el servidor APRS-IS no aceptaría "
                     "lo que transmita el bot.", foreground=AMBAR)
        self.l_estado.configure(text="Hay cambios sin guardar" if self._hay_cambios() else "")

    def _mostrar_clave(self):
        self.e_pass.configure(show="" if self.v_mostrar.get() else "•")

    def reset(self):
        super().reset()
        self.cargada = None

    def refresh(self, forzar=False):
        firma = self.app.config_store.firma()
        if self.cargada is not None and (firma == self.firma or self._hay_cambios()):
            return  # sin novedades, o hay ediciones sin guardar que no hay que pisar
        self.firma = firma
        self._cargar()

    def _cargar(self):
        store = self.app.config_store
        cfg, error = store.cargar()
        if store.existe():
            texto = f"Archivo: {store.ruta}"
        else:
            senal, clave = leer_valores_bot(self.app.carpeta)
            cfg.indicativo, cfg.passcode = cfg.indicativo or senal.upper(), cfg.passcode or clave
            texto = (f"Todavía no existe {store.ruta.name}: se muestran los valores que trae el script del bot. "
                     "Al guardar se crea el archivo en la carpeta de datos.")
        self.l_archivo.configure(text=(error + "\n" if error else "") + texto)
        self.cargada = cfg
        self.v_ind.set(cfg.indicativo)
        self.v_pass.set(cfg.passcode)
        self.v_zona.set(cfg.zona)
        self._al_editar()

    # -- acciones -------------------------------------------------------------
    def calcular(self):
        ind = self.v_ind.get().strip().upper()
        if not PATRON_INDICATIVO.fullmatch(ind):
            messagebox.showerror("Señal distintiva", "Escribí primero una señal distintiva válida "
                                 "(por ejemplo LU9DUV-B).", parent=self)
            return
        self.v_pass.set(passcode_para(ind))

    def descartar(self):
        self.cargada = None
        self.firma = None
        self.refresh(forzar=True)

    def guardar(self):
        nueva = self._valores()
        errores = validar_configuracion(nueva.indicativo, nueva.passcode)
        if errores:
            messagebox.showerror("Revisá los datos", "\n".join("• " + e for e in errores), parent=self)
            return
        if nueva.passcode != passcode_para(nueva.indicativo) and not messagebox.askyesno(
                "La clave no coincide",
                f"La clave {nueva.passcode} no es la que corresponde a {nueva.indicativo}.\n\n"
                "Con una clave incorrecta el servidor APRS-IS no acepta los paquetes que envía el bot "
                "(no podría responder a los operadores).\n\n¿Guardar igual?", icon="warning", parent=self):
            return
        anterior = self.cargada
        try:
            self.app.config_store.guardar(nueva)
        except OSError as e:
            messagebox.showerror("No se pudo guardar", f"No se pudo escribir config.ini:\n{e}", parent=self)
            return
        self.app.aplicar_config(nueva)  # recarga el formulario desde el archivo recién guardado
        texto = "Configuración guardada."
        if anterior is None or (anterior.indicativo, anterior.passcode) != (nueva.indicativo, nueva.passcode):
            texto += " Reiniciá el bot para que use la nueva señal y clave."
        self.l_estado.configure(text=texto)
        self.app.mensaje(texto)


# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self, carpeta):
        super().__init__()
        self.title("Panel del bot APRS")
        self.geometry("1060x720")
        self.minsize(900, 600)
        self.set_carpeta(carpeta)
        self._estilos()
        self._menu()

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self.tabs = []
        for clase, titulo in [(ResumenTab, "Resumen"), (RankingTab, "Ranking"), (CampanasTab, "Campañas"),
                              (LogsTab, "Log del bot"), (QsoTab, "Contactos (ADIF)"), (ConfigTab, "Configuración")]:
            tab = clase(self)
            self.notebook.add(tab, text=titulo)
            self.tabs.append(tab)

        pie = ttk.Frame(self, padding=(12, 6))
        pie.pack(fill="x")
        ttk.Separator(self).pack(fill="x", before=pie)
        self.v_izq, self.v_der = tk.StringVar(), tk.StringVar()
        ttk.Label(pie, textvariable=self.v_izq, foreground=GRIS).pack(side="left")
        self.l_der = ttk.Label(pie, textvariable=self.v_der, foreground=GRIS)
        self.l_der.pack(side="right")
        self._msg_id = None
        self._ultimo_pie = 0.0
        self._texto_izq()
        if self.cfg_error:
            self.mensaje(self.cfg_error)

        self.notebook.bind("<<NotebookTabChanged>>", lambda e: self.refrescar(forzar=True))
        self.bind("<F5>", lambda e: self.refrescar(forzar=True))
        self.after(200, self.tick)

    # -- configuración ----------------------------------------------------
    def set_carpeta(self, carpeta):
        self.carpeta = Path(carpeta).resolve()
        self.db = StatsDB(self.carpeta / DB_NAME)
        self.claves = ClavesStore(self.carpeta / CLAVES_NAME)
        self.config_store = ConfigStore(self.carpeta / CONFIG_NAME)
        self.cfg, self.cfg_error = self.config_store.cargar()
        self.zona = Zona(self.cfg.zona)
        self.callsign = self.cfg.indicativo or leer_valores_bot(self.carpeta)[0] or "INDICATIVO"
        self.title(f"Panel del bot APRS · {self.callsign}")

    def clave_dia(self):
        """Cambia a medianoche UTC y a medianoche de la zona elegida (para refrescar lo que depende del día)."""
        return ahora_utc().strftime("%Y-%m-%d") + self.zona.ahora().strftime("%Y-%m-%d")

    def aplicar_config(self, cfg):
        """Aplica una configuración recién guardada: zona horaria, título y señal de los boletines."""
        self.cfg = cfg
        self.zona = Zona(cfg.zona)
        self.callsign = cfg.indicativo or self.callsign
        self.title(f"Panel del bot APRS · {self.callsign}")
        for tab in self.tabs:
            tab.reset()
            tab.aplicar_zona()
        self.refrescar(forzar=True)

    def _estilos(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        self.configure(background=BG)
        if ES_WINDOWS:
            for nombre in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                tkfont.nametofont(nombre).configure(family="Segoe UI", size=10)
        s.configure(".", background=BG, foreground=TEXTO, bordercolor=LINEA, lightcolor=BG, darkcolor=BG)
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", padding=(16, 7), background="#eef1f4", foreground=GRIS, borderwidth=0)
        s.map("TNotebook.Tab", background=[("selected", BG)], foreground=[("selected", TEXTO)])
        s.configure("Treeview", background=BG, fieldbackground=BG, rowheight=26, borderwidth=1, bordercolor=LINEA)
        s.configure("Treeview.Heading", background="#f1f3f5", relief="flat", padding=(8, 5))
        s.map("Treeview", background=[("selected", "#dbeafe")], foreground=[("selected", TEXTO)])
        s.configure("TButton", padding=(12, 5))
        s.configure("Acento.TButton", background=ACENTO, foreground="#ffffff", borderwidth=0)
        s.map("Acento.TButton", background=[("active", "#1a5fd0"), ("disabled", "#9db8e6")],
              foreground=[("disabled", "#ffffff")])
        s.configure("TLabelframe", background=BG, bordercolor=LINEA)
        s.configure("TLabelframe.Label", background=BG, foreground=GRIS)

    def _menu(self):
        barra = tk.Menu(self)
        archivo = tk.Menu(barra, tearoff=False)
        archivo.add_command(label="Cambiar carpeta de datos…", command=self.cambiar_carpeta)
        archivo.add_command(label="Actualizar        F5", command=lambda: self.refrescar(forzar=True))
        archivo.add_separator()
        archivo.add_command(label="Salir", command=self.destroy)
        ayuda = tk.Menu(barra, tearoff=False)
        ayuda.add_command(label="Acerca del panel", command=self.acerca)
        barra.add_cascade(label="Archivo", menu=archivo)
        barra.add_cascade(label="Ayuda", menu=ayuda)
        self.configure(menu=barra)

    def cambiar_carpeta(self):
        nueva = filedialog.askdirectory(parent=self, initialdir=str(self.carpeta),
                                        title="Carpeta donde corre el bot (con claves.txt y concurso_aprs.db)")
        if nueva:
            self.set_carpeta(nueva)
            for tab in self.tabs:
                tab.reset()
                tab.aplicar_zona()
            self._texto_izq()
            self.refrescar(forzar=True)

    def acerca(self):
        messagebox.showinfo(
            "Acerca del panel",
            "Panel del bot APRS de concursos.\n\n"
            "Lee la base de datos y los logs del bot sin interferir con él, y edita claves.txt "
            "(el bot recarga el archivo automáticamente).\n\n"
            "Todos los horarios están en UTC.\n\n"
            f"Carpeta de datos:\n{self.carpeta}", parent=self)

    # -- refresco -----------------------------------------------------------
    def tick(self):
        try:
            self.refrescar()
        except Exception as e:  # el temporizador nunca debe morir por un error puntual
            self.mensaje(f"Error al actualizar: {e}")
        self.after(1000, self.tick)

    def refrescar(self, forzar=False):
        tab = self.nametowidget(self.notebook.select())
        ahora = time.monotonic()
        if forzar or ahora - tab.ultimo >= tab.cada / 1000:
            tab.ultimo = ahora
            tab.refresh(forzar=forzar)
        if forzar or ahora - self._ultimo_pie >= 5:
            self._ultimo_pie = ahora
            self._texto_der()

    def _texto_izq(self):
        self.v_izq.set(f"Carpeta de datos: {self.carpeta}")

    def mensaje(self, texto):
        """Muestra un aviso breve en el pie y luego vuelve a la carpeta."""
        self.v_izq.set(texto)
        if self._msg_id:
            self.after_cancel(self._msg_id)
        self._msg_id = self.after(6000, self._texto_izq)

    def _texto_der(self):
        logs = listar_logs(self.carpeta)
        marca = ultima_marca_log(logs[0]) if logs else None
        if marca is None:
            self.v_der.set("Sin registros del bot todavía")
            self.l_der.configure(foreground=GRIS)
            return
        antiguo = (ahora_utc() - marca) > timedelta(minutes=90)
        self.v_der.set(f"Último registro del bot: {hace_cuanto(marca)}" +
                       (" · ¿está corriendo?" if antiguo else ""))
        self.l_der.configure(foreground=AMBAR if antiguo else GRIS)


def main():
    carpeta = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DIR
    if not Path(carpeta).is_dir():
        print(f"No existe la carpeta: {carpeta}")
        sys.exit(1)
    if ES_WINDOWS:
        try:  # texto nítido en pantallas de alta densidad
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    App(carpeta).mainloop()


if __name__ == "__main__":
    main()
