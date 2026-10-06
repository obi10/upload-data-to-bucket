# Respaldo TOSHIBA a OCI

Respaldo deduplicado, reanudable y verificable de `TOSHIBA EXTERNAL_USB USB Device`
hacia OCI Object Storage. No depende de la letra ni de la etiqueta del volumen y nunca
elimina archivos del disco externo.

## Operación

El programa importa primero la copia parcial de Music, agrupa archivos pequeños en TAR
sin compresión, sube archivos grandes mediante multipartes y registra todo en un catálogo
SQLite. Cada contenido se identifica por SHA-256, por lo que dos rutas idénticas ocupan un
solo objeto. Una carga interrumpida continúa con las partes que falten.

Copia `config.example.json` como `config.json` y completa el namespace, bucket, rutas y
perfil. `config.json` no se publica en Git porque identifica el entorno operativo.

```powershell
& 'C:\o\Scripts\python.exe' run.py --config config.json lab-test
& 'C:\o\Scripts\python.exe' run.py --config config.json backup
& 'C:\o\Scripts\python.exe' run.py --config config.json verify
& 'C:\o\Scripts\python.exe' run.py --config config.json restore --destination 'E:\Restaurado'
```

El estado se guarda en la ruta indicada por `runtime_dir`. Las
credenciales permanecen en el perfil `DEFAULT` de OCI y no forman parte del proyecto.

## Detección y diagnóstico en Windows

Configura `device_serial_number` con el número de serie físico del disco. Se consultan
`Get-Disk` y `Win32_DiskDrive`, unidos por número de disco, y se resuelven sus particiones
mediante Storage y asociaciones CIM. La letra y la etiqueta pueden cambiar. Si se configura
una serie, otro disco del mismo modelo no se acepta. Sin serie, se comparan ambos nombres
normalizando el sufijo de Windows `USB Device`; coincidencias ambiguas se rechazan.

Cada consulta agrega un registro a `runtime_dir/device-detection.jsonl` con fecha UTC,
identidades encontradas, errores de Windows, montajes y motivos de selección o rechazo.
Para comprobar la detección sin iniciar una carga a OCI:

```powershell
& 'C:\o\Scripts\python.exe' run.py --config config.json diagnose-device
```

Código 0: disco identificado y accesible. Código 3: no hay un volumen accesible que coincida;
esto no prueba que el disco estuviera desconectado. Los errores de consulta se registran
y provocan un fallo, sin confirmar el respaldo. Los discos del sistema se excluyen.

## Restaurar en otro disco externo

Consulta la **[guía de restauración para Windows y macOS](docs/RESTAURAR.md)**.
Incluye instalación, configuración OCI, selección del disco de destino, recuperación de
copias parciales, respaldo completo, reanudación y comprobación de resultados.
La plantilla [config.restore.example.json](config.restore.example.json) permite preparar
otra computadora sin copiar el catálogo del equipo original.

La restauración necesita el proyecto Python y los manifiestos de OCI para reconstruir
las rutas originales. Descargar los TAR del bucket con OCI CLI, por sí solo, no reconstruye
el árbol de archivos. Si todavía no existe `manifests/latest.json`, se debe seleccionar
explícitamente un manifiesto de instantánea; la guía explica esta situación.

## Seguridad y eliminación local

Los objetos se verifican por tamaño y SHA-256.
La carpeta indicada por `partial_root` se elimina únicamente cuando todos sus
archivos están asociados a objetos confirmados y el manifiesto remoto fue publicado.
Los archivos del TOSHIBA nunca se modifican ni eliminan.

## Tarea programada

La tarea `Upload_TOSHIBA_OCI_20261006_RETRY` ejecuta el respaldo el martes 6 de octubre
de 2026, a las 09:00, 09:30 y cada treinta minutos hasta las 14:30 (Lima), con doce
activaciones. Se conserva `pythonw.exe`, inicio cuando el equipo vuelva a estar disponible
y exclusión de instancias simultáneas; una ejecución activa puede continuar después de
las 14:30. Requiere iniciar sesión con el usuario configurado y conectar el TOSHIBA.

Los reintentos consultan únicamente los contenidos pendientes del inventario vigente.
Un archivo modificado o inaccesible al formar un TAR se registra en
`runtime_dir/upload-errors.jsonl` y se aplaza; los demás archivos estables se suben.
Un inventario posterior recoge la nueva versión y evita reintentar hashes antiguos sin
origen. Se reutiliza el SHA-256 del inventario anterior si tamaño y fecha de modificación
en nanosegundos coinciden; los contenidos nuevos se validan al empaquetarse.
En Windows se usan rutas extendidas para soportar nombres mayores de 260 caracteres.
Los errores de acceso siguen pendientes y no se cambian permisos del disco.
No se publica `manifests/latest.json` ni se marca el respaldo completo si quedan errores.

`windows\programar_respaldo_oci_20260925.ps1` registra una ejecución oculta a las 09:00,
con inicio posterior si el equipo estaba apagado y doce reintentos cada treinta minutos.

`windows\programar_reintento_oci_20260928.ps1` registra el reintento del lunes 28 de
septiembre de 2026 desde las 09:00, cada treinta minutos durante seis horas. Si el respaldo
ya está completo, las activaciones posteriores terminan inmediatamente sin volver a subir
ni volver a notificar.
