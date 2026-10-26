"""
corregir_adif.py
-----------------
Corrige archivos ADIF (.adi) generados por bot3.py que fueron creados ANTES
de que se agregaran los campos FREQ y STATION_CALLSIGN a cada registro QSO.

Qué hace:
- Busca líneas de registro (las que contienen "<EOR>") y les agrega los
  campos <FREQ> y <STATION_CALLSIGN> si no los tienen.
- Quita el SSID del campo <CALL> (ej: LU9DUV-5 -> LU9DUV), para que el
  log quede con la licencia limpia, igual que el resto del bot.
- Si un registro YA tiene esos campos correctos, no los toca ni los
  duplica (podés correr el script varias veces sin problema).
- Reordena los campos de cada registro en un orden prolijo y consistente
  (CALL, QSO_DATE, TIME_ON, FREQ, STATION_CALLSIGN, MODE, otros...).
- Antes de modificar un archivo, guarda una copia de seguridad con
  extensión ".bak" (solo la primera vez; no pisa un backup ya existente).

Uso:
    python corregir_adif.py                     -> corrige todos los .adi de la carpeta actual
    python corregir_adif.py 20260913.adi         -> corrige un archivo puntual
    python corregir_adif.py C:\\ruta\\a\\carpeta    -> corrige todos los .adi de esa carpeta
    python corregir_adif.py --frecuencia 144.390 --indicativo LU9DUV  (valores por defecto)
"""

import argparse
import os
import re
import shutil
import sys

# Valores por defecto: deben coincidir con la configuración de bot3.py
FRECUENCIA_MHZ_DEFAULT = "144.390"
INDICATIVO_DEFAULT = "LU9DUV"

# Orden prolijo en el que se van a escribir los campos conocidos.
ORDEN_PREFERIDO = ["CALL", "QSO_DATE", "TIME_ON", "FREQ", "STATION_CALLSIGN", "MODE"]

# Matchea campos ADIF del tipo <NOMBRE:LONGITUD>  (con o sin ":TIPO" opcional)
PATRON_CAMPO = re.compile(r"<([A-Za-z_]+):(\d+)(?::[A-Za-z])?>")


def parsear_campos(linea):
    """Devuelve un dict {NOMBRE_CAMPO: valor} a partir de una línea de registro ADIF."""
    campos = {}
    for match in PATRON_CAMPO.finditer(linea):
        nombre = match.group(1).upper()
        longitud = int(match.group(2))
        inicio = match.end()
        valor = linea[inicio:inicio + longitud]
        campos[nombre] = valor
    return campos


def reconstruir_registro(campos, frecuencia, indicativo):
    """
    Arma la línea de registro ADIF corregida:
    - Quita el SSID del campo CALL (ej: LU9DUV-5 -> LU9DUV).
    - Agrega FREQ y STATION_CALLSIGN si faltan.
    - Ordena los campos de forma prolija.
    """
    cambios = []

    if "CALL" in campos:
        call_original = campos["CALL"]
        call_normalizado = call_original.split("-")[0].strip().upper()
        if call_normalizado != call_original:
            campos["CALL"] = call_normalizado
            cambios.append(f"CALL ({call_original} -> {call_normalizado}, SSID quitado)")

    if "FREQ" not in campos:
        campos["FREQ"] = frecuencia
        cambios.append("FREQ")

    if "STATION_CALLSIGN" not in campos:
        campos["STATION_CALLSIGN"] = indicativo
        cambios.append("STATION_CALLSIGN")

    partes = []
    ya_incluidos = set()

    for nombre in ORDEN_PREFERIDO:
        if nombre in campos:
            valor = campos[nombre]
            partes.append(f"<{nombre}:{len(valor)}>{valor}")
            ya_incluidos.add(nombre)

    # Cualquier otro campo no contemplado en el orden preferido va al final
    for nombre, valor in campos.items():
        if nombre not in ya_incluidos:
            partes.append(f"<{nombre}:{len(valor)}>{valor}")

    partes.append("<EOR>")
    return " ".join(partes), cambios


def corregir_archivo(ruta, frecuencia, indicativo):
    """Corrige un único archivo .adi. Devuelve (registros_corregidos, registros_totales)."""
    with open(ruta, "r", encoding="utf-8") as f:
        lineas = f.readlines()

    lineas_nuevas = []
    registros_totales = 0
    registros_corregidos = 0

    for linea in lineas:
        linea_sin_salto = linea.rstrip("\n")

        if "<EOR>" in linea_sin_salto.upper() and "<CALL" in linea_sin_salto.upper():
            registros_totales += 1
            campos = parsear_campos(linea_sin_salto)
            nueva_linea, cambios = reconstruir_registro(campos, frecuencia, indicativo)

            if cambios:
                registros_corregidos += 1
                print(f"  ✏️  {campos.get('CALL', '?')}: {', '.join(cambios)}")

            lineas_nuevas.append(nueva_linea + "\n")
        else:
            # Encabezado, líneas en blanco u otro contenido: se dejan igual
            lineas_nuevas.append(linea)

    if registros_corregidos > 0:
        backup_path = ruta + ".bak"
        if not os.path.exists(backup_path):
            shutil.copy2(ruta, backup_path)
            print(f"  💾 Backup guardado en {backup_path}")

        with open(ruta, "w", encoding="utf-8") as f:
            f.writelines(lineas_nuevas)

    return registros_corregidos, registros_totales


def buscar_archivos_adi(ruta):
    """Si 'ruta' es un archivo .adi lo devuelve solo; si es carpeta, busca todos los .adi dentro."""
    if os.path.isfile(ruta):
        return [ruta]
    if os.path.isdir(ruta):
        return sorted(
            os.path.join(ruta, nombre)
            for nombre in os.listdir(ruta)
            if nombre.lower().endswith(".adi")
        )
    return []


def main():
    parser = argparse.ArgumentParser(
        description="Corrige archivos ADIF agregando FREQ y STATION_CALLSIGN a cada registro."
    )
    parser.add_argument(
        "ruta",
        nargs="?",
        default=".",
        help="Archivo .adi o carpeta a corregir (por defecto: carpeta actual)",
    )
    parser.add_argument(
        "--frecuencia",
        default=FRECUENCIA_MHZ_DEFAULT,
        help=f"Frecuencia en MHz a insertar (default: {FRECUENCIA_MHZ_DEFAULT})",
    )
    parser.add_argument(
        "--indicativo",
        default=INDICATIVO_DEFAULT,
        help=f"Indicativo de la estación (STATION_CALLSIGN) a insertar (default: {INDICATIVO_DEFAULT})",
    )
    args = parser.parse_args()

    archivos = buscar_archivos_adi(args.ruta)

    if not archivos:
        print(f"❌ No se encontraron archivos .adi en '{args.ruta}'.")
        sys.exit(1)

    print(f"🔍 Encontrados {len(archivos)} archivo(s) .adi para revisar.\n")

    total_corregidos = 0
    total_registros = 0

    for ruta in archivos:
        print(f"📄 {ruta}")
        corregidos, totales = corregir_archivo(ruta, args.frecuencia, args.indicativo)
        total_corregidos += corregidos
        total_registros += totales
        if corregidos == 0:
            print("  ✅ Ya estaba en formato correcto, no se modificó.")
        print()

    print(f"Listo. {total_corregidos} de {total_registros} registro(s) corregido(s) en total.")


if __name__ == "__main__":
    main()
