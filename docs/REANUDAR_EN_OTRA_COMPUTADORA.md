# Reanudar la restauración del TOSHIBA EXT en otra computadora

Esta guía corresponde al respaldo final identificado el 15 de octubre de 2026. Permite
continuar **en el mismo disco externo** desde otra laptop Windows o Linux. No copia la
descarga desde cero: `restore --resume` comprueba por SHA-256 cada archivo que ya está en
`Restaurado-TOSHIBA` y no descarga un objeto OCI cuando todos sus archivos están completos.
Puede volver a descargar **un objeto individual incompleto**. La bitácora no es un
checkpoint indispensable: el estado recuperable está en los archivos del disco.

## Antes de mover el disco

1. En la laptop actual, ejecuta `pausar_toshiba_windows.ps1`, ubicado en
   `C:\Users\ypalomino\OneDrive - ASCENSORES S.A\Documentos\temporal`.
2. Espera a que `restore-toshiba-status.json` indique `"state": "paused"` y confirma que
   no queda un proceso Python de restauración. **No desconectes el disco mientras esté
   en `running`**. Después, expulsa el disco de forma segura y apaga Windows por completo;
   evita hibernar si lo conectarás a Linux, pues NTFS podría montarse solo para lectura.
3. Nunca ejecutes dos restauraciones simultáneas contra el mismo destino.

El disco físico esperado es USB, serie `20210314020834F`, etiqueta `TOSHIBA EXT`, NTFS,
de aproximadamente 2 TB. La carpeta restaurada es `Restaurado-TOSHIBA` en la raíz de ese
volumen. **No cambies el nombre ni muevas esta carpeta**. No uses `INFORMACION YP` como
destino y no formatees el disco.

## Llevar el proyecto actualizado

Lleva a la otra computadora el paquete `upload-data-to-bucket-reanudable.zip` preparado en
`C:\Users\ypalomino\OneDrive - ASCENSORES S.A\Documentos\temporal`, **o clona la rama**
`feature/toshiba-resume-20261015` de
`https://github.com/obi10/upload-data-to-bucket`. No uses `main` para reanudar hasta que
los cambios se incorporen allí. El ZIP contiene el proyecto Python, esta guía y las
pruebas, pero **no** la clave privada, `config.restore.json`, la bitácora ni el catálogo
local. Comprueba que tu copia reconoce `--resume` con `run.py restore --help`.

Si prefieres Git en vez del ZIP, el comando es el mismo en Windows y Linux:

```text
git clone --branch feature/toshiba-resume-20261015 --single-branch https://github.com/obi10/upload-data-to-bucket.git restore-toshiba-repo
```

En las instrucciones siguientes, **salta `Expand-Archive` o `unzip` si ya clonaste** esa
rama y entra directamente en el directorio `restore-toshiba-repo`.

Guarda el proyecto en el disco **interno** de la nueva computadora, no dentro de
`Restaurado-TOSHIBA`. Python 3.10 o superior y `oci==2.187.0` son necesarios. Tener OCI CLI
configurado **no sustituye** al SDK Python. El perfil OCI `DEFAULT` debe poder leer el
bucket y su `key_file` debe apuntar a una clave existente en esa computadora. No pongas
la clave privada en el ZIP, en Git, en la bitácora ni en el disco restaurado.

En la raíz del proyecto crea `config.restore.json` con este contenido no secreto:

```json
{
  "profile": "DEFAULT",
  "region": "us-ashburn-1",
  "namespace": "idgnbtoi8dif",
  "bucket": "pc-desktop-qps9irc-disco-externo-copia-total",
  "prefix": "backups/toshiba",
  "runtime_dir": ".restore-state"
}
```

La instantánea que **debes fijar en cada sesión**, sin `--include-historical`, es:

```text
backups/toshiba/manifests/snapshots/toshiba-current-b9e83c8b03bb69477afee3aa3ead24133af3558fb483530549dfb5b50327911a.json.gz
```

### Windows: preparar el proyecto

Abre PowerShell. Cambia la ruta del ZIP si lo copiaste a otro lugar:

```powershell
$zip = 'C:\ruta\al\upload-data-to-bucket-reanudable.zip'
$repo = Join-Path $env:USERPROFILE 'restore-toshiba-repo'
Expand-Archive -LiteralPath $zip -DestinationPath $repo
Set-Location $repo
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install 'oci==2.187.0'
& .\.venv\Scripts\python.exe .\run.py restore --help
```

Si `py` no existe, usa `python` o la ruta de una instalación Python 3.10+. Guarda el JSON
de configuración anterior como `config.restore.json` en `$repo`. No copies una ruta de
clave privada de la laptop original: comprueba que el perfil OCI local tenga la suya.

### Linux: preparar el proyecto

Usa una terminal y ajusta la ruta del ZIP a donde lo hayas copiado:

```bash
mkdir -p "$HOME/restore-toshiba-repo"
unzip /ruta/al/upload-data-to-bucket-reanudable.zip -d "$HOME/restore-toshiba-repo"
cd "$HOME/restore-toshiba-repo"
python3 -m venv .venv
.venv/bin/python -m pip install 'oci==2.187.0'
.venv/bin/python run.py restore --help
```

Guarda el JSON anterior como `config.restore.json` en esa carpeta. En Linux, el perfil
normalmente se lee de `~/.oci/config`; su `key_file` debe contener una ruta Linux válida.
Si `python3 -m venv` o el montaje NTFS no están disponibles, instala los componentes
correspondientes de tu distribución antes de continuar. No fuerces escritura sobre un
NTFS montado en modo solo lectura.

## Comprobar acceso OCI y el disco correcto

Desde cualquier sistema, con el perfil `DEFAULT` ya configurado, prueba la lectura de la
cabecera del manifiesto (no descarga los datos del respaldo):

```text
oci os object head --profile DEFAULT --region us-ashburn-1 --namespace-name idgnbtoi8dif --bucket-name pc-desktop-qps9irc-disco-externo-copia-total --name backups/toshiba/manifests/snapshots/toshiba-current-b9e83c8b03bb69477afee3aa3ead24133af3558fb483530549dfb5b50327911a.json.gz
```

En **Windows**, ejecuta este bloque desde PowerShell. Detecta la letra asignada al disco
por su serie física; no presupone que siga siendo `D:`:

```powershell
$disks = @(Get-Disk | Where-Object { $_.SerialNumber.Trim() -eq '20210314020834F' })
if ($disks.Count -ne 1 -or $disks[0].BusType -ne 'USB') { throw 'Disco físico no identificado de forma única' }
$volumes = @(Get-Partition -DiskNumber $disks[0].Number |
    Get-Volume | Where-Object { $_.FileSystemLabel -eq 'TOSHIBA EXT' -and $_.FileSystem -eq 'NTFS' })
if ($volumes.Count -ne 1 -or -not $volumes[0].DriveLetter) { throw 'Volumen TOSHIBA EXT no identificado' }
$letter = $volumes[0].DriveLetter
$destination = '\\?\' + $letter + ':\Restaurado-TOSHIBA'
if (-not (Test-Path -LiteralPath $destination)) { throw "Falta la carpeta: $destination" }
$volumes[0] | Select-Object DriveLetter,FileSystemLabel,FileSystem,SizeRemaining
Write-Output "Destino confirmado: $destination"
```

En **Linux**, primero identifica el dispositivo y su punto de montaje:

```bash
lsblk -o NAME,TRAN,MODEL,SERIAL,FSTYPE,LABEL,UUID,MOUNTPOINTS,SIZE
```

Confirma la serie `20210314020834F`, el volumen NTFS `TOSHIBA EXT` y el tamaño aproximado.
Si el adaptador USB no muestra la serie, **no elijas un disco solo por la etiqueta**:
identifícalo mediante su ruta física/UUID y detente si hay ambigüedad. Sustituye el montaje
del siguiente ejemplo por el que mostró `lsblk`:

```bash
DEST='/media/usuario/TOSHIBA EXT/Restaurado-TOSHIBA'
findmnt -T "$DEST" -o SOURCE,FSTYPE,TARGET,OPTIONS
test -d "$DEST" && test -w "$DEST" || { echo 'Destino ausente o sin escritura'; exit 1; }
df -h "$DEST"
```

La salida de `findmnt` debe mostrar el volumen esperado con opción `rw` (lectura/escritura).
La unidad debe tener espacio para los archivos que faltan, el objeto OCI más grande
(aproximadamente 12,6 GB) y los temporales `.restoring`.

## Reanudar sin reemplazar archivos

Ejecuta desde la raíz del proyecto. En **Windows**, conserva las variables `$destination`
y `$letter` obtenidas en la comprobación anterior:

```powershell
$manifest = 'backups/toshiba/manifests/snapshots/toshiba-current-b9e83c8b03bb69477afee3aa3ead24133af3558fb483530549dfb5b50327911a.json.gz'
if (Test-Path -LiteralPath '.\.restore-state\restore.pause') {
    Remove-Item -LiteralPath '.\.restore-state\restore.pause'
}
& .\.venv\Scripts\python.exe .\run.py --config .\config.restore.json restore `
    --destination $destination --manifest $manifest --resume `
    --progress-file .\.restore-state\restore-progress.jsonl `
    --pause-file .\.restore-state\restore.pause
```

En **Linux**, conserva `DEST` después de identificar el montaje:

```bash
MANIFEST='backups/toshiba/manifests/snapshots/toshiba-current-b9e83c8b03bb69477afee3aa3ead24133af3558fb483530549dfb5b50327911a.json.gz'
rm -f -- .restore-state/restore.pause
.venv/bin/python run.py --config config.restore.json restore \
    --destination "$DEST" --manifest "$MANIFEST" --resume \
    --progress-file .restore-state/restore-progress.jsonl \
    --pause-file .restore-state/restore.pause
```

Deja abierta la terminal del comando hasta pausar o terminar. Al principio puede tardar en
verificar archivos existentes sin aumentar el número de archivos del disco. La bitácora
JSONL anota `file_verified_existing`, `file_restored`, `object_skipped`,
`object_download_progress` y otros eventos. **No uses `--overwrite`** para continuar.
Si un archivo existente no coincide con el manifiesto, el programa se detiene sin tocarlo;
investiga esa diferencia antes de reintentar.

## Pausar y trasladar de nuevo el disco

Desde **otra terminal en la misma carpeta del proyecto**:

```powershell
# Windows
New-Item -ItemType File -Path '.\.restore-state\restore.pause' -Force | Out-Null
```

```bash
# Linux
touch .restore-state/restore.pause
```

Espera a que el comando principal salga con `"estado": "pausada"` y código 4. La pausa
se comprueba entre archivos y fragmentos de descarga; una operación de red bloqueada o
la copia de un archivo grande puede tardar. Solo entonces expulsa el disco. Si se apaga de
golpe, los archivos finales verificados se conservan, pero el objeto activo incompleto
podría tener que descargarse otra vez. En el siguiente equipo repite las comprobaciones
del disco y el comando `--resume` con **el mismo manifiesto**.

La bitácora ubicada en `.restore-state` pertenece a cada computadora. Puedes copiarla
para auditoría, pero no hace falta para reanudar. La supervisión automática configurada en
la laptop original no ve la ejecución de otra computadora.

## Confirmar el resultado

Al terminar, el JSON del comando debe indicar `"archivos_restaurados": 101002` y
`"errores_registrados": 0`; `archivos_restaurados` suma los archivos nuevos y los
existentes que se verificaron. La carpeta `.oci-restore-cache` debería desaparecer.
La restauración verifica SHA-256 de cada objeto descargado y archivo reconstruido.
Comprueba también la identidad del disco y abre una muestra de archivos. No uses el
comando `verify` como comprobación en la nueva computadora: depende del catálogo de
subida del equipo original.
