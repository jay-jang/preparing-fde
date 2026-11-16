"""Offline verification of an RSA-SHA256-signed, versioned release manifest.

This intentionally uses an independently provisioned public key, not a key
shipped inside the untrusted bundle. It is a blob/package verification kernel,
not an OCI admission controller or an SBOM vulnerability scanner.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess


def verify_bundle(root, *, public_key, expected_release, now=None):
    root = Path(root).resolve(strict=True)
    public_key = Path(public_key).resolve(strict=True)
    if public_key.is_relative_to(root):
        raise ValueError('trust key must be provisioned outside the bundle')
    manifest = root / 'release.json'
    signature = root / 'release.sig'
    if manifest.is_symlink() or signature.is_symlink() or manifest.stat().st_size > 1_000_000:
        raise ValueError('unsafe manifest')
    # OpenSSL verifies the signature of the exact bytes, before parsing JSON.
    subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(public_key),
                    '-signature', str(signature), str(manifest)],
                   check=True, capture_output=True, timeout=10)
    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate manifest key')
            result[key] = value
        return result
    data = json.loads(manifest.read_text(encoding='utf-8'), object_pairs_hook=no_duplicates)
    if set(data) != {'schema_version', 'release_id', 'expires_at', 'files'}:
        raise ValueError('invalid manifest fields')
    if type(data['schema_version']) is not int or data['schema_version'] != 1:
        raise ValueError('unknown manifest version')
    if data['release_id'] != expected_release:
        raise ValueError('unexpected or replayed release')
    expiry = datetime.fromisoformat(data['expires_at'])
    if expiry.tzinfo is None or expiry <= (now or datetime.now(timezone.utc)):
        raise ValueError('expired release; check trusted internal time')
    if not isinstance(data['files'], list) or not 1 <= len(data['files']) <= 1000:
        raise ValueError('invalid artifact list')
    seen = set()
    for artifact in data['files']:
        if not isinstance(artifact, dict) or set(artifact) != {'name', 'sha256', 'bytes'}:
            raise ValueError('invalid artifact metadata')
        name = artifact['name']
        if not isinstance(name, str) or not name or '\\' in name:
            raise ValueError('unsafe artifact name')
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or str(relative) != name or name in seen:
            raise ValueError('unsafe or duplicate artifact path')
        seen.add(name)
        path = root / name
        # Reject symlinks in any component as well as escapes outside the root.
        if any(p.is_symlink() for p in [path, *path.parents] if p != root and p.is_relative_to(root)):
            raise ValueError('symlink artifact')
        if not path.resolve(strict=True).is_relative_to(root) or not path.is_file():
            raise ValueError('artifact outside bundle')
        if type(artifact['bytes']) is not int or artifact['bytes'] < 1 or path.stat().st_size != artifact['bytes']:
            raise ValueError('artifact size differs')
        if not isinstance(artifact['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', artifact['sha256']):
            raise ValueError('invalid hash')
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != artifact['sha256']:
            raise ValueError('artifact hash differs')
    return data
