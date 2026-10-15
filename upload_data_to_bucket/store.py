from __future__ import annotations

import base64
import hashlib
import io
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import oci

from .catalog import Catalog
from .config import Settings
from .source_io import open_source, stat_source


def sha256_file(path: Path, chunk_size: int = 8 * 1024**2) -> str:
    digest = hashlib.sha256()
    with open_source(path) as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def md5_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data).digest()).decode("ascii")


class OciStore:
    def __init__(self, settings: Settings, catalog: Catalog):
        self.settings = settings
        self.catalog = catalog
        config = oci.config.from_file(profile_name=settings.profile)
        config["region"] = settings.region
        oci.config.validate_config(config)
        self.client = oci.object_storage.ObjectStorageClient(
            config,
            retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
            timeout=(15, 300),
        )
        self.namespace = settings.namespace
        self.bucket = settings.bucket

    def name(self, relative: str) -> str:
        return f"{self.settings.prefix.strip('/')}/{relative.lstrip('/')}"

    def head(self, object_name: str):
        try:
            return self.client.head_object(self.namespace, self.bucket, object_name)
        except oci.exceptions.ServiceError as error:
            if error.status == 404:
                return None
            raise

    def verified(self, object_name: str, sha256: str, size: int) -> bool:
        response = self.head(object_name)
        if response is None:
            return False
        headers = {key.lower(): value for key, value in response.headers.items()}
        return (
            int(headers.get("content-length", -1)) == size
            and headers.get("opc-meta-sha256", "").lower() == sha256.lower()
        )

    def upload_file(self, path: Path, object_name: str, sha256: str | None = None):
        path = path.absolute()
        size = stat_source(path).st_size
        sha256 = sha256 or sha256_file(path)
        if self.verified(object_name, sha256, size):
            return self.head(object_name).headers.get("etag")
        if self.head(object_name) is not None:
            raise RuntimeError(f"Colisión remota para {object_name}")
        if size <= self.settings.part_size:
            with open_source(path) as stream:
                data = stream.read()
            response = self.client.put_object(
                self.namespace, self.bucket, object_name, data,
                content_length=size,
                content_md5=md5_b64(data),
                opc_meta={"sha256": sha256, "size": str(size)},
                storage_tier="Standard",
            )
            if not self.verified(object_name, sha256, size):
                raise RuntimeError(f"OCI no confirmó {object_name}")
            return response.headers.get("etag")
        return self._multipart(path, object_name, sha256, size)

    def _multipart(self, path: Path, object_name: str, sha256: str, size: int):
        upload = self.catalog.get_upload(object_name)
        upload_id = upload["upload_id"] if upload and upload["sha256"] == sha256 else None
        remote_parts: dict[int, tuple[str, str]] = {}
        if upload_id:
            try:
                response = oci.pagination.list_call_get_all_results(
                    self.client.list_multipart_upload_parts,
                    self.namespace, self.bucket, object_name, upload_id,
                )
                for part in response.data:
                    remote_parts[int(part.part_number)] = (part.etag, getattr(part, "md5", "") or "")
            except oci.exceptions.ServiceError as error:
                if error.status not in (404, 409):
                    raise
                upload_id = None
        if not upload_id:
            details = oci.object_storage.models.CreateMultipartUploadDetails(
                object=object_name,
                metadata={"opc-meta-sha256": sha256, "opc-meta-size": str(size)},
                storage_tier="Standard",
            )
            upload_id = self.client.create_multipart_upload(
                self.namespace, self.bucket, details
            ).data.upload_id
            self.catalog.save_upload(object_name, str(path), sha256, size, upload_id)

        part_size = self.settings.part_size
        total_parts = math.ceil(size / part_size)

        def send(number: int):
            offset = (number - 1) * part_size
            length = min(part_size, size - offset)
            with open_source(path) as stream:
                stream.seek(offset)
                data = stream.read(length)
            if len(data) != length:
                raise OSError(f"Lectura incompleta de la parte {number}")
            checksum = md5_b64(data)
            result = self.client.upload_part(
                self.namespace, self.bucket, object_name, upload_id, number, data,
                content_length=length, content_md5=checksum,
            )
            etag = result.headers["etag"]
            self.catalog.save_part(object_name, number, etag, checksum)
            return number, etag, checksum

        missing = [number for number in range(1, total_parts + 1) if number not in remote_parts]
        if missing:
            with ThreadPoolExecutor(max_workers=self.settings.parallel_parts) as pool:
                futures = [pool.submit(send, number) for number in missing]
                for future in as_completed(futures):
                    number, etag, checksum = future.result()
                    remote_parts[number] = (etag, checksum)

        # Releer OCI evita confirmar una parte que solo exista en el estado local.
        response = oci.pagination.list_call_get_all_results(
            self.client.list_multipart_upload_parts,
            self.namespace, self.bucket, object_name, upload_id,
        )
        remote_parts = {int(part.part_number): (part.etag, getattr(part, "md5", "") or "") for part in response.data}
        if set(remote_parts) != set(range(1, total_parts + 1)):
            raise RuntimeError(f"Faltan partes remotas para {object_name}")
        parts = [
            oci.object_storage.models.CommitMultipartUploadPartDetails(
                part_num=number, etag=remote_parts[number][0]
            )
            for number in range(1, total_parts + 1)
        ]
        details = oci.object_storage.models.CommitMultipartUploadDetails(parts_to_commit=parts)
        result = self.client.commit_multipart_upload(
            self.namespace, self.bucket, object_name, upload_id, details
        )
        if not self.verified(object_name, sha256, size):
            raise RuntimeError(f"Verificación remota fallida para {object_name}")
        self.catalog.finish_upload(object_name)
        return result.headers.get("etag")

    def put_bytes(self, object_name: str, data: bytes, *, overwrite: bool = True):
        sha256 = hashlib.sha256(data).hexdigest()
        if not overwrite and self.verified(object_name, sha256, len(data)):
            return self.head(object_name).headers.get("etag")
        result = self.client.put_object(
            self.namespace, self.bucket, object_name, io.BytesIO(data),
            content_length=len(data), content_md5=md5_b64(data),
            opc_meta={"sha256": sha256, "size": str(len(data))},
            storage_tier="Standard",
        )
        if not self.verified(object_name, sha256, len(data)):
            raise RuntimeError(f"No se verificó {object_name}")
        return result.headers.get("etag")

    def get_bytes(self, object_name: str) -> bytes:
        response = self.client.get_object(self.namespace, self.bucket, object_name)
        return response.data.content

    def download(self, object_name: str, destination: Path,
                 on_chunk: Callable[[int], None] | None = None) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        response = self.client.get_object(self.namespace, self.bucket, object_name)
        downloaded = 0
        with destination.open("wb") as output:
            for chunk in response.data.raw.stream(8 * 1024**2, decode_content=False):
                output.write(chunk)
                downloaded += len(chunk)
                if on_chunk is not None:
                    on_chunk(downloaded)

    def list_objects(self, prefix: str):
        response = oci.pagination.list_call_get_all_results(
            self.client.list_objects, self.namespace, self.bucket, prefix=prefix
        )
        return response.data.objects

    def delete_prefix(self, prefix: str) -> int:
        objects = self.list_objects(prefix)
        for item in objects:
            self.client.delete_object(self.namespace, self.bucket, item.name)
        return len(objects)

    def abort_multipart_prefix(self, prefix: str) -> int:
        response = oci.pagination.list_call_get_all_results(
            self.client.list_multipart_uploads, self.namespace, self.bucket
        )
        uploads = [item for item in response.data if item.object.startswith(prefix)]
        for item in uploads:
            self.client.abort_multipart_upload(
                self.namespace, self.bucket, item.object, item.upload_id
            )
        return len(uploads)
