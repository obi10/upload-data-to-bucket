"""Read-only Windows backup access, scoped to the detected source volumes.

No ACL/ownership changes, no SeRestorePrivilege, no write handles. Other platforms
and commands keep ordinary file access. See Microsoft's CreateFileW and
GetFileInformationByHandleEx documentation.
"""
from __future__ import annotations

import ctypes
import json
import os
import stat
import struct
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

_backend = None


def _canonical(path):
    value = os.path.abspath(os.fspath(path))
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(os.path.normpath(value))


class WindowsBackupReader:
    def __init__(self, roots):
        if os.name != "nt":
            raise OSError("La lectura con privilegio de respaldo requiere Windows")
        from ctypes import wintypes as w
        self.roots = tuple(_canonical(root) for root in roots)
        if not self.roots:
            raise ValueError("Se requiere al menos una raíz de respaldo")
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.a = ctypes.WinDLL("advapi32", use_last_error=True)

        class Luid(ctypes.Structure):
            _fields_ = [("low", w.DWORD), ("high", w.LONG)]

        class Privilege(ctypes.Structure):
            _fields_ = [("luid", Luid), ("attributes", w.DWORD)]

        class TokenPrivileges(ctypes.Structure):
            _fields_ = [("count", w.DWORD), ("privileges", Privilege * 1)]

        class FileInfo(ctypes.Structure):
            _fields_ = [("attributes", w.DWORD), ("created", w.FILETIME),
                        ("accessed", w.FILETIME), ("modified", w.FILETIME),
                        ("volume", w.DWORD), ("size_high", w.DWORD),
                        ("size_low", w.DWORD), ("links", w.DWORD),
                        ("index_high", w.DWORD), ("index_low", w.DWORD)]

        self.TokenPrivileges = TokenPrivileges
        self.FileInfo = FileInfo
        self.token = w.HANDLE()
        self.previous = TokenPrivileges()
        self.k.GetCurrentProcess.restype = w.HANDLE
        self.k.CloseHandle.argtypes = [w.HANDLE]
        self.k.CloseHandle.restype = w.BOOL
        self.k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                       w.DWORD, w.DWORD, w.HANDLE]
        self.k.CreateFileW.restype = w.HANDLE
        self.k.GetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.POINTER(FileInfo)]
        self.k.GetFileInformationByHandle.restype = w.BOOL
        self.k.GetFileInformationByHandleEx.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.k.GetFileInformationByHandleEx.restype = w.BOOL
        self.a.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
        self.a.OpenProcessToken.restype = w.BOOL
        self.a.LookupPrivilegeValueW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.POINTER(Luid)]
        self.a.LookupPrivilegeValueW.restype = w.BOOL
        self.a.AdjustTokenPrivileges.argtypes = [w.HANDLE, w.BOOL, ctypes.POINTER(TokenPrivileges),
                                                w.DWORD, ctypes.POINTER(TokenPrivileges), ctypes.POINTER(w.DWORD)]
        self.a.AdjustTokenPrivileges.restype = w.BOOL

    def allows(self, path):
        value = _canonical(path)
        return any(value == root or value.startswith(root.rstrip("\\/") + os.sep) for root in self.roots)

    def enable(self):
        from ctypes import wintypes as w
        if not self.a.OpenProcessToken(self.k.GetCurrentProcess(), 0x20 | 0x8, ctypes.byref(self.token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            desired = self.TokenPrivileges()
            desired.count = 1
            if not self.a.LookupPrivilegeValueW(None, "SeBackupPrivilege", ctypes.byref(desired.privileges[0].luid)):
                raise ctypes.WinError(ctypes.get_last_error())
            desired.privileges[0].attributes = 2  # SE_PRIVILEGE_ENABLED
            returned = w.DWORD()
            ctypes.set_last_error(0)
            ok = self.a.AdjustTokenPrivileges(self.token, False, ctypes.byref(desired),
                                              ctypes.sizeof(self.previous), ctypes.byref(self.previous), ctypes.byref(returned))
            error = ctypes.get_last_error()
            if not ok or error:
                if error == 1300:  # ERROR_NOT_ALL_ASSIGNED: success BOOL is insufficient.
                    raise PermissionError("SeBackupPrivilege no está disponible; ejecutar la tarea con elevación")
                raise ctypes.WinError(error)
        except Exception:
            self.k.CloseHandle(self.token)
            self.token = None
            raise

    def close(self):
        if self.token:
            try:
                if self.previous.count:
                    if not self.a.AdjustTokenPrivileges(self.token, False, ctypes.byref(self.previous), 0, None, None):
                        raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self.k.CloseHandle(self.token)
                self.token = None

    def _error(self, path):
        error = ctypes.WinError(ctypes.get_last_error())
        error.filename = str(path)
        return error

    def _handle(self, path, access):
        if not self.allows(path):
            raise PermissionError(f"Fuera de las raíces autorizadas para respaldo: {path}")
        value = os.path.abspath(os.fspath(path))
        if not value.startswith("\\\\?\\"):
            value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
        # OPEN_EXISTING, BACKUP_SEMANTICS, OPEN_REPARSE_POINT; all sharing allowed.
        handle = self.k.CreateFileW(value, access, 7, None, 3, 0x02000000 | 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise self._error(path)
        return handle

    def _info(self, handle, path):
        info = self.FileInfo()
        if not self.k.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise self._error(path)
        return info

    def stat(self, path):
        handle = self._handle(path, 0x80)  # FILE_READ_ATTRIBUTES
        try:
            info = self._info(handle, path)
            mtime = (((info.modified.dwHighDateTime << 32) | info.modified.dwLowDateTime) - 116444736000000000) * 100
            mode = stat.S_IFLNK if info.attributes & 0x400 else stat.S_IFDIR if info.attributes & 0x10 else stat.S_IFREG
            return SimpleNamespace(st_size=(info.size_high << 32) | info.size_low,
                                   st_mtime_ns=mtime, st_mtime=mtime / 1_000_000_000,
                                   st_mode=mode | 0o444, st_file_attributes=info.attributes)
        finally:
            self.k.CloseHandle(handle)

    def open(self, path):
        import msvcrt
        handle = self._handle(path, 0x80000000)  # GENERIC_READ only
        try:
            info = self._info(handle, path)
            if info.attributes & (0x400 | 0x10):
                raise OSError(f"No se puede leer un directorio o reparse point como archivo: {path}")
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except Exception:
            self.k.CloseHandle(handle)
            raise
        # The CRT descriptor now owns the native handle.
        try:
            return os.fdopen(fd, "rb")
        except Exception:
            os.close(fd)
            raise

    def listdir(self, path):
        handle = self._handle(path, 0x81)  # FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES
        try:
            info = self._info(handle, path)
            if info.attributes & 0x400:
                raise OSError(f"Reparse point omitido: {path}")
            buffer = ctypes.create_string_buffer(65536)
            while True:
                if not self.k.GetFileInformationByHandleEx(handle, 14, buffer, len(buffer)):
                    if ctypes.get_last_error() == 18:  # ERROR_NO_MORE_FILES
                        return
                    raise self._error(path)
                raw = buffer.raw
                offset = 0
                while True:
                    # FILE_FULL_DIR_INFO: fixed fields through EaSize occupy 68 bytes.
                    next_offset = struct.unpack_from("<I", raw, offset)[0]
                    attributes, length = struct.unpack_from("<II", raw, offset + 56)
                    end = offset + 68 + length
                    if length % 2 or end > len(raw):
                        raise OSError("Respuesta inválida al enumerar el directorio")
                    name = raw[offset + 68:end].decode("utf-16-le")
                    if name not in (".", ".."):
                        if not name or "\\" in name or "/" in name:
                            raise OSError("Nombre inválido al enumerar el directorio")
                        yield name, attributes
                    if not next_offset:
                        break
                    if next_offset < 68 + length or next_offset % 8 or offset + next_offset + 68 > len(raw):
                        raise OSError("Offset inválido al enumerar el directorio")
                    offset += next_offset
        finally:
            self.k.CloseHandle(handle)

    def walk(self, root, onerror):
        pending = [Path(root)]
        while pending:
            current = pending.pop()
            try:
                entries = list(self.listdir(current))
            except OSError as error:
                error.filename = str(current)
                onerror(error)
                continue
            directories = [name for name, attributes in entries if attributes & 0x10]
            files = [name for name, attributes in entries if not attributes & 0x10]
            yield str(current), directories, files
            # Caller can remove reparse points/errors from the directory list.
            pending.extend(current / name for name in reversed(directories))


@contextmanager
def backup_access(roots, enabled=False, diagnostic_path=None):
    global _backend
    if not enabled:
        yield
        return
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "status": "error",
              "roots": [str(root) for root in roots]}
    reader = None
    previous = _backend
    try:
        try:
            reader = WindowsBackupReader(roots)
            reader.enable()
            _backend = reader
            report["status"] = "enabled"
        except Exception as error:
            report["error"] = str(error)
            raise
        finally:
            if diagnostic_path:
                Path(diagnostic_path).parent.mkdir(parents=True, exist_ok=True)
                with Path(diagnostic_path).open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(report, ensure_ascii=False) + "\n")
        yield
    finally:
        _backend = previous
        if reader is not None:
            reader.close()


def open_source(path):
    if _backend is not None and _backend.allows(path):
        return _backend.open(path)
    return Path(path).open("rb")


def stat_source(path):
    if _backend is not None and _backend.allows(path):
        return _backend.stat(path)
    return Path(path).lstat()


def walk_source(root, onerror):
    if _backend is not None and _backend.allows(root):
        yield from _backend.walk(root, onerror)
    else:
        yield from os.walk(root, topdown=True, followlinks=False, onerror=onerror)
