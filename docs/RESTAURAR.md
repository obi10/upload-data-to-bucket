# Restaurar los archivos de OCI en otro disco duro externo

Esta guía reconstruye carpetas, nombres originales, archivos duplicados y carpetas vacías
en un disco de destino distinto. El comando `restore` lee los objetos de OCI y escribe en
el destino; no necesita conectar el TOSHIBA original ni modifica el bucket.

Necesitas el proyecto completo (`run.py` y `upload_data_to_bucket/`), Python 3.10 o
superior, OCI Python SDK y acceso al bucket. OCI CLI ayuda a consultar los manifiestos,
pero no reemplaza al programa: los TAR almacenan contenidos por SHA-256, no por su ruta
original. Las instrucciones siguientes se ejecutan desde la carpeta del repositorio.

## 1. Preparar el disco y el acceso

1. Conecta el disco de destino y confirma su ruta y permisos de escritura.
2. Elige una **carpeta nueva**, por ejemplo `/Volumes/MI_DISCO/Restaurado-TOSHIBA`
   en macOS o `E:\Restaurado-TOSHIBA` en Windows. Sustituye estos ejemplos por tu destino.
3. Comprueba el espacio libre. El tamaño restaurado incluye duplicados, de modo que puede
   superar el tamaño almacenado en OCI. Además, la restauración descarga un objeto a la vez
   en `.oci-restore-cache`, dentro del destino, y usa archivos temporales `.restoring`.
   Deja margen para el objeto más grande y los archivos temporales; un objeto individual
   puede ser mucho mayor de 4 GiB. No basta con reservar espacio para un TAR de 4 GiB.
4. En esa computadora, configura el perfil OCI `DEFAULT` con credenciales autorizadas para
   listar y leer objetos del bucket. Si OCI CLI ya funciona, el SDK usa el mismo perfil en
   `~/.oci/config` (macOS) o `%USERPROFILE%\.oci\config` (Windows). La ruta de la clave
   privada de ese perfil debe existir en esa computadora. No publiques claves ni credenciales.

Para comprobar espacio y rutas:

```bash
# macOS
ls /Volumes
df -h "/Volumes/MI_DISCO"
```

```powershell
# Windows
Get-Volume | Select-Object DriveLetter, FileSystemLabel, SizeRemaining, Size
```

## 2. Descargar el proyecto e instalar el SDK

Si el repositorio es privado, autentica Git con una cuenta que tenga acceso.

### macOS

```bash
git clone https://github.com/obi10/upload-data-to-bucket.git
cd upload-data-to-bucket
python3 -m venv .venv
.venv/bin/python -m pip install 'oci==2.187.0'
cp config.restore.example.json config.restore.json
```

### Windows (PowerShell)

```powershell
git clone https://github.com/obi10/upload-data-to-bucket.git
Set-Location upload-data-to-bucket
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install 'oci==2.187.0'
Copy-Item config.restore.example.json config.restore.json
```

Si Windows no tiene el lanzador `py`, sustituye `py -3` por la ruta de Python instalado.
En el equipo original se puede utilizar `C:\o\Scripts\python.exe`. Para restaurar y ver
el resultado, usa `python.exe`, no `pythonw.exe`.

Si ya tienes el repositorio, actualízalo con `git pull --ff-only` desde esa carpeta.

## 3. Completar la configuración de restauración

Edita `config.restore.json` y sustituye los valores `YOUR_...` por los del respaldo:

```json
{
  "profile": "DEFAULT",
  "region": "us-ashburn-1",
  "namespace": "YOUR_OBJECT_STORAGE_NAMESPACE",
  "bucket": "YOUR_BUCKET_NAME",
  "prefix": "backups/toshiba",
  "runtime_dir": ".restore-state"
}
```

Puedes consultar namespace, bucket y prefijo en el `config.json` del equipo original o
en OCI. No confundas el **nombre del bucket** con su OCID. No es necesario configurar
`partial_root`, el número de serie del TOSHIBA ni Tailscale para restaurar.

`runtime_dir` crea un catálogo local auxiliar; la restauración reconstruye sus datos desde
los manifiestos remotos. No necesitas copiar el catálogo SQLite original cuando el
manifiesto de la instantánea que quieres recuperar ya está publicado.
`config.restore.json`, `.restore-state/` y `.venv/` están excluidos de Git.

## 4. Comprobar qué instantáneas están disponibles

Con OCI CLI instalado, ejecuta este comando en cualquiera de los dos sistemas, sustituyendo
namespace y bucket por los mismos valores del JSON:

```text
oci os object list --profile DEFAULT --region us-ashburn-1 --namespace-name YOUR_OBJECT_STORAGE_NAMESPACE --bucket-name YOUR_BUCKET_NAME --prefix backups/toshiba/manifests/ --all --query "data.objects[*].name"
```

- `backups/toshiba/manifests/latest.json` es el índice usado para restaurar automáticamente
  el respaldo final. La versión actual solo lo publica al completar el inventario sin errores.
- `backups/toshiba/manifests/snapshots/*.json.gz` son instantáneas con las rutas originales.
  Copia el **nombre completo del objeto**, incluido el prefijo, al usar `--manifest`.
- Los JSON de `manifests/bundles/` describen contenidos TAR, no árboles de rutas. **No los
  pases a `--manifest`** ni intentes inferir los nombres originales de los hashes.

**Situación comprobada el 5 de octubre de 2026:** había una instantánea parcial
`partial-20260924-...json.gz` publicada y el manifiesto final aún no existía. Esa instantánea
permite recuperar la primera copia (62.238 archivos, unos 339,07 GB decimales / 315,78 GiB
de datos lógicos). No incluye necesariamente todo lo que se subió posteriormente.
Vuelve a listar los manifiestos: esta situación puede cambiar con los reintentos.

Un objeto de datos puede estar subido antes de que se publique su instantánea. Si falta el
manifiesto que relaciona esos contenidos con sus rutas, **no se puede restaurar ese conjunto
completo con este comando todavía**. Conserva el catálogo del equipo original hasta publicar
la instantánea correspondiente. Un 404 para `latest.json` no demuestra que el bucket esté vacío.

## 5. Restaurar

### A. Respaldo final: cuando existe `latest.json`

macOS:

```bash
.venv/bin/python run.py --config config.restore.json restore \
  --destination "/Volumes/MI_DISCO/Restaurado-TOSHIBA"
```

Windows:

```powershell
& .\.venv\Scripts\python.exe run.py --config config.restore.json restore `
  --destination 'E:\Restaurado-TOSHIBA'
```

El programa lee `latest.json` y restaura sus `current_manifests`.

### B. Instantánea parcial: no requiere `latest.json`

Sustituye `NOMBRE_COMPLETO_DEL_MANIFIESTO.json.gz` por el nombre obtenido en el paso 4.
No uses un asterisco, una URL del navegador ni el nombre de un TAR.

macOS:

```bash
.venv/bin/python run.py --config config.restore.json restore \
  --manifest "backups/toshiba/manifests/snapshots/NOMBRE_COMPLETO_DEL_MANIFIESTO.json.gz" \
  --destination "/Volumes/MI_DISCO/Restaurado-Parcial"
```

Windows:

```powershell
& .\.venv\Scripts\python.exe run.py --config config.restore.json restore `
  --manifest 'backups/toshiba/manifests/snapshots/NOMBRE_COMPLETO_DEL_MANIFIESTO.json.gz' `
  --destination 'E:\Restaurado-Parcial'
```

Este comando recupera exclusivamente lo descrito por esa instantánea. Si contiene entradas
de error, el resultado informa cuántas quedaron registradas; esas rutas no se reconstruyen.

### C. Recuperar versiones históricas

Para preservar versiones de la misma ruta, restaura cada instantánea histórica con
`--manifest` en una **carpeta de destino distinta**. Así puedes comparar el histórico y
el respaldo actual sin reemplazar archivos.

Es posible repetir `--manifest` para combinar instantáneas en un destino: en rutas repetidas
gana **el último manifiesto indicado**. Para priorizar la versión actual, indica primero el
histórico y después el actual. Con el comportamiento presente de `--include-historical`,
los históricos se agregan después de los actuales y pueden prevalecer; para controlar esa
precedencia usa manifiestos explícitos o carpetas separadas.

## 6. Interrupciones, archivos existentes y comprobación

Deja conectado el destino y mantén el proceso activo hasta recibir el resumen. Una excepción
o una desconexión significa que esa ejecución no terminó.

Por defecto, el programa se detiene si encuentra un archivo ya existente, para evitar
reemplazarlo. Para volver a ejecutar una restauración interrumpida **en la misma carpeta
dedicada al respaldo**, repite el mismo comando añadiendo `--overwrite`:

```bash
# Ejemplo macOS, para el respaldo final
.venv/bin/python run.py --config config.restore.json restore \
  --destination "/Volumes/MI_DISCO/Restaurado-TOSHIBA" --overwrite
```

```powershell
# Ejemplo Windows, para el respaldo final
& .\.venv\Scripts\python.exe run.py --config config.restore.json restore `
  --destination 'E:\Restaurado-TOSHIBA' --overwrite
```

Si estabas restaurando una instantánea parcial, conserva también su argumento `--manifest`.
`--overwrite` reemplaza archivos coincidentes: no lo uses sobre una carpeta con cambios que
quieras conservar. Esta restauración vuelve a procesar archivos y puede descargar objetos
otra vez; no ofrece la reanudación por partes del comando de subida `backup`.

Durante la restauración se calcula el SHA-256 de cada objeto descargado y de cada archivo
reconstruido. Un fallo de checksum detiene el comando. Al terminar verás un JSON como:

```json
{"archivos_restaurados": 62238, "errores_registrados": 0}
```

El número depende de la instantánea elegida. `errores_registrados: 0` significa que no había
entradas de error en esos manifiestos; **no demuestra por sí solo que se haya respaldado todo
el TOSHIBA**, especialmente si elegiste una copia parcial. Revisa además que se haya usado
la instantánea esperada y abre algunos archivos importantes del destino.

El comando `verify` depende del catálogo local de subidas; ejecutarlo en una computadora nueva
con un catálogo vacío puede devolver cero objetos. No lo uses como comprobación de una
restauración completa. La validación SHA-256 se realiza dentro de `restore`.

Se reconstruyen archivos, directorios vacíos y fechas de modificación de archivos cuando
figuran en el manifiesto. No es una imagen de disco arrancable ni restaura ACL, propietarios
o enlaces especiales. Si el sistema de archivos de destino no admite alguna ruta, nombre
o longitud, la operación puede detenerse y deberá elegirse un destino compatible.
