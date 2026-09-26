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

## Restaurar desde macOS

Instala Python y OCI SDK, copia la configuración con rutas de macOS y ejecuta:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install 'oci==2.187.0'
python run.py --config config-mac.json restore \
  --manifest 'OBJECT_NAME_DEL_MANIFIESTO.json.gz' \
  --destination '/Volumes/Destino/restaurado'
```

## Seguridad y eliminación local

Los objetos se verifican por tamaño y SHA-256. La carpeta local
La carpeta indicada por `partial_root` se elimina únicamente cuando todos sus
archivos están asociados a objetos confirmados y el manifiesto remoto fue publicado.
Los archivos del TOSHIBA nunca se modifican ni eliminan.

## Tarea programada

`windows\programar_respaldo_oci_20260925.ps1` registra una ejecución oculta a las 09:00,
con inicio posterior si el equipo estaba apagado y doce reintentos cada treinta minutos.

`windows\programar_reintento_oci_20260928.ps1` registra el reintento del lunes 28 de
septiembre de 2026 desde las 09:00, cada treinta minutos durante seis horas. Si el respaldo
ya está completo, las activaciones posteriores terminan inmediatamente sin volver a subir
ni volver a notificar.
