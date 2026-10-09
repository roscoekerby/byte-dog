"""Authenticode signer of a file: embedded signature first, then the Windows
security catalogs (most System32 binaries are catalog-signed, not embedded).

signer_of(path) returns the organisation (O=) of the verified leaf signing
certificate, e.g. 'Microsoft Corporation', or '' when unsigned, untrusted or
unreadable. Never raises. No network: revocation checks are off and URL
retrieval is cache-only, so this is safe to call during a RAM thrash.
Results are cached per (path, mtime, size).
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
from ctypes import wintypes as w

_cache = {}
_cache_lock = threading.Lock()

WTD_UI_NONE = 2
WTD_REVOKE_NONE = 0
WTD_CHOICE_FILE = 1
WTD_CHOICE_CATALOG = 2
WTD_STATEACTION_VERIFY = 1
WTD_STATEACTION_CLOSE = 2
WTD_REVOCATION_CHECK_NONE = 0x10
WTD_CACHE_ONLY_URL_RETRIEVAL = 0x1000
CERT_NAME_ATTR_TYPE = 3
OID_ORGANIZATION = b'2.5.4.10'
GENERIC_READ = 0x80000000
FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
OPEN_EXISTING = 3
INVALID_HANDLE = ctypes.c_void_p(-1).value


class GUID(ctypes.Structure):
    _fields_ = [('Data1', w.DWORD), ('Data2', w.WORD), ('Data3', w.WORD),
                ('Data4', ctypes.c_ubyte * 8)]


GENERIC_VERIFY_V2 = GUID(0x00AAC56B, 0xCD44, 0x11D0,
                         (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))


class WINTRUST_FILE_INFO(ctypes.Structure):
    _fields_ = [('cbStruct', w.DWORD), ('pcwszFilePath', w.LPCWSTR),
                ('hFile', w.HANDLE), ('pgKnownSubject', ctypes.POINTER(GUID))]


class WINTRUST_CATALOG_INFO(ctypes.Structure):
    _fields_ = [('cbStruct', w.DWORD), ('dwCatalogVersion', w.DWORD),
                ('pcwszCatalogFilePath', w.LPCWSTR), ('pcwszMemberTag', w.LPCWSTR),
                ('pcwszMemberFilePath', w.LPCWSTR), ('hMemberFile', w.HANDLE),
                ('pbCalculatedFileHash', ctypes.POINTER(ctypes.c_ubyte)),
                ('cbCalculatedFileHash', w.DWORD), ('pcCatalogContext', ctypes.c_void_p),
                ('hCatAdmin', w.HANDLE)]


class WINTRUST_DATA(ctypes.Structure):
    _fields_ = [('cbStruct', w.DWORD), ('pPolicyCallbackData', ctypes.c_void_p),
                ('pSIPClientData', ctypes.c_void_p), ('dwUIChoice', w.DWORD),
                ('fdwRevocationChecks', w.DWORD), ('dwUnionChoice', w.DWORD),
                ('pInfo', ctypes.c_void_p), ('dwStateAction', w.DWORD),
                ('hWVTStateData', w.HANDLE), ('pwszURLReference', w.LPWSTR),
                ('dwProvFlags', w.DWORD), ('dwUIContext', w.DWORD),
                ('pSignatureSettings', ctypes.c_void_p)]


class CATALOG_INFO(ctypes.Structure):
    _fields_ = [('cbStruct', w.DWORD), ('wszCatalogFile', w.WCHAR * 260)]


class CRYPT_PROVIDER_CERT(ctypes.Structure):
    _fields_ = [('cbStruct', w.DWORD), ('pCert', ctypes.c_void_p)]


class CRYPT_PROVIDER_SGNR(ctypes.Structure):
    _fields_ = [('cbStruct', w.DWORD), ('sftVerifyAsOf', w.FILETIME),
                ('csCertChain', w.DWORD),
                ('pasCertChain', ctypes.POINTER(CRYPT_PROVIDER_CERT))]


_dlls = {}


def _api():
    if _dlls:
        return _dlls
    wt = ctypes.WinDLL('wintrust')
    c32 = ctypes.WinDLL('crypt32')
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    wt.WinVerifyTrust.argtypes = [w.HWND, ctypes.POINTER(GUID), ctypes.c_void_p]
    wt.WinVerifyTrust.restype = ctypes.c_long
    wt.WTHelperProvDataFromStateData.argtypes = [w.HANDLE]
    wt.WTHelperProvDataFromStateData.restype = ctypes.c_void_p
    wt.WTHelperGetProvSignerFromChain.argtypes = [ctypes.c_void_p, w.DWORD, w.BOOL, w.DWORD]
    wt.WTHelperGetProvSignerFromChain.restype = ctypes.POINTER(CRYPT_PROVIDER_SGNR)
    wt.CryptCATAdminAcquireContext2.argtypes = [ctypes.POINTER(w.HANDLE), ctypes.c_void_p,
                                                w.LPCWSTR, ctypes.c_void_p, w.DWORD]
    wt.CryptCATAdminAcquireContext2.restype = w.BOOL
    wt.CryptCATAdminCalcHashFromFileHandle2.argtypes = [
        w.HANDLE, w.HANDLE, ctypes.POINTER(w.DWORD), ctypes.c_void_p, w.DWORD]
    wt.CryptCATAdminCalcHashFromFileHandle2.restype = w.BOOL
    wt.CryptCATAdminEnumCatalogFromHash.argtypes = [
        w.HANDLE, ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(w.HANDLE)]
    wt.CryptCATAdminEnumCatalogFromHash.restype = w.HANDLE
    wt.CryptCATCatalogInfoFromContext.argtypes = [w.HANDLE, ctypes.POINTER(CATALOG_INFO), w.DWORD]
    wt.CryptCATCatalogInfoFromContext.restype = w.BOOL
    wt.CryptCATAdminReleaseCatalogContext.argtypes = [w.HANDLE, w.HANDLE, w.DWORD]
    wt.CryptCATAdminReleaseCatalogContext.restype = w.BOOL
    wt.CryptCATAdminReleaseContext.argtypes = [w.HANDLE, w.DWORD]
    wt.CryptCATAdminReleaseContext.restype = w.BOOL
    c32.CertGetNameStringW.argtypes = [ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_void_p,
                                       w.LPWSTR, w.DWORD]
    c32.CertGetNameStringW.restype = w.DWORD
    k32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                w.DWORD, w.DWORD, w.HANDLE]
    k32.CreateFileW.restype = w.HANDLE
    k32.CloseHandle.argtypes = [w.HANDLE]
    k32.CloseHandle.restype = w.BOOL
    _dlls.update(wt=wt, c32=c32, k32=k32)
    return _dlls


def _verify(union_choice: int, info) -> str:
    """WinVerifyTrust; on success return the leaf signer's organisation."""
    api = _api()
    data = WINTRUST_DATA(cbStruct=ctypes.sizeof(WINTRUST_DATA), dwUIChoice=WTD_UI_NONE,
                         fdwRevocationChecks=WTD_REVOKE_NONE, dwUnionChoice=union_choice,
                         pInfo=ctypes.cast(ctypes.pointer(info), ctypes.c_void_p),
                         dwStateAction=WTD_STATEACTION_VERIFY,
                         dwProvFlags=WTD_REVOCATION_CHECK_NONE | WTD_CACHE_ONLY_URL_RETRIEVAL)
    guid = GUID.from_buffer_copy(GENERIC_VERIFY_V2)
    try:
        if api['wt'].WinVerifyTrust(None, ctypes.byref(guid), ctypes.byref(data)) != 0:
            return ''
        prov = api['wt'].WTHelperProvDataFromStateData(data.hWVTStateData)
        if not prov:
            return ''
        sgnr = api['wt'].WTHelperGetProvSignerFromChain(prov, 0, False, 0)
        if not sgnr or not sgnr.contents.csCertChain:
            return ''
        cert = sgnr.contents.pasCertChain[0].pCert
        buf = ctypes.create_unicode_buffer(256)
        api['c32'].CertGetNameStringW(cert, CERT_NAME_ATTR_TYPE, 0,
                                      ctypes.c_char_p(OID_ORGANIZATION), buf, 256)
        return buf.value
    finally:
        data.dwStateAction = WTD_STATEACTION_CLOSE
        api['wt'].WinVerifyTrust(None, ctypes.byref(guid), ctypes.byref(data))


def _embedded_signer(path: str) -> str:
    info = WINTRUST_FILE_INFO(cbStruct=ctypes.sizeof(WINTRUST_FILE_INFO), pcwszFilePath=path)
    return _verify(WTD_CHOICE_FILE, info)


def _catalog_signer(path: str) -> str:
    api = _api()
    wt, k32 = api['wt'], api['k32']
    hfile = k32.CreateFileW(path, GENERIC_READ, FILE_SHARE_ALL, None, OPEN_EXISTING, 0, None)
    if not hfile or hfile == INVALID_HANDLE:
        return ''
    try:
        for alg in ('SHA256', 'SHA1'):
            admin = w.HANDLE()
            if not wt.CryptCATAdminAcquireContext2(ctypes.byref(admin), None, alg, None, 0):
                continue
            try:
                signer = _catalog_signer_with(wt, admin, hfile, path)
                if signer:
                    return signer
            finally:
                wt.CryptCATAdminReleaseContext(admin, 0)
        return ''
    finally:
        k32.CloseHandle(hfile)


def _catalog_signer_with(wt, admin, hfile, path) -> str:
    size = w.DWORD(0)
    wt.CryptCATAdminCalcHashFromFileHandle2(admin, hfile, ctypes.byref(size), None, 0)
    if not size.value:
        return ''
    digest = (ctypes.c_ubyte * size.value)()
    if not wt.CryptCATAdminCalcHashFromFileHandle2(admin, hfile, ctypes.byref(size), digest, 0):
        return ''
    catinfo = wt.CryptCATAdminEnumCatalogFromHash(admin, digest, size.value, 0, None)
    if not catinfo:
        return ''
    try:
        ci = CATALOG_INFO(cbStruct=ctypes.sizeof(CATALOG_INFO))
        if not wt.CryptCATCatalogInfoFromContext(catinfo, ctypes.byref(ci), 0):
            return ''
        tag = ''.join(f'{b:02X}' for b in digest)
        info = WINTRUST_CATALOG_INFO(
            cbStruct=ctypes.sizeof(WINTRUST_CATALOG_INFO), pcwszCatalogFilePath=ci.wszCatalogFile,
            pcwszMemberTag=tag, pcwszMemberFilePath=path, hMemberFile=hfile,
            pbCalculatedFileHash=digest, cbCalculatedFileHash=size.value, hCatAdmin=admin)
        return _verify(WTD_CHOICE_CATALOG, info)
    finally:
        wt.CryptCATAdminReleaseCatalogContext(admin, catinfo, 0)


def signer_of(path: str) -> str:
    if sys.platform != 'win32' or not path:
        return ''
    try:
        st = os.stat(path)
    except OSError:
        return ''
    key = (os.path.normcase(path), st.st_mtime_ns, st.st_size)
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    try:
        signer = _embedded_signer(path) or _catalog_signer(path)
    except Exception:
        signer = ''
    with _cache_lock:
        _cache[key] = signer
    return signer
