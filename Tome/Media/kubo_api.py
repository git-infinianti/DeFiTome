from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass
from typing import BinaryIO

from django.conf import settings
import httpx


@dataclass(frozen=True)
class KuboUploadResult:
    """Structured result returned by Kubo `/api/v0/add`."""

    name: str
    cid: str
    size: int


class KuboAPIUploader:
    """Small client object for uploading content to a Kubo node."""

    def __init__(self, api_base_url: str | None = None, timeout: float = 30.0):
        default_url = getattr(settings, "IPFS_STORAGE_API_URL", "http://127.0.0.1:5001/api/v0/")
        self.api_base_url = (api_base_url or default_url).rstrip("/") + "/"
        self.timeout = timeout

    def upload_path(
        self,
        file_path: str,
        *,
        pin: bool = True,
        wrap_with_directory: bool = False,
        cid_version: int | None = None,
        hash_algorithm: str | None = None,
    ) -> KuboUploadResult:
        """Upload a local file path to Kubo and return the resulting CID."""
        filename = os.path.basename(file_path)
        with open(file_path, "rb") as file_obj:
            return self.upload_fileobj(
                file_obj,
                file_name=filename,
                pin=pin,
                wrap_with_directory=wrap_with_directory,
                cid_version=cid_version,
                hash_algorithm=hash_algorithm,
            )

    def upload_bytes(
        self,
        data: bytes,
        *,
        file_name: str,
        pin: bool = True,
        wrap_with_directory: bool = False,
        cid_version: int | None = None,
        hash_algorithm: str | None = None,
    ) -> KuboUploadResult:
        """Upload raw bytes with a provided virtual file name."""
        return self.upload_fileobj(
            io.BytesIO(data),
            file_name=file_name,
            pin=pin,
            wrap_with_directory=wrap_with_directory,
            cid_version=cid_version,
            hash_algorithm=hash_algorithm,
        )

    def upload_fileobj(
        self,
        file_obj: BinaryIO,
        *,
        file_name: str,
        pin: bool = True,
        wrap_with_directory: bool = False,
        cid_version: int | None = None,
        hash_algorithm: str | None = None,
    ) -> KuboUploadResult:
        """Upload any binary file-like object supported by IPFS/Kubo."""
        params = {
            "pin": str(pin).lower(),
            "wrap-with-directory": str(wrap_with_directory).lower(),
        }

        if cid_version is not None:
            params["cid-version"] = str(cid_version)
        if hash_algorithm:
            params["hash"] = hash_algorithm

        if hasattr(file_obj, "seek"):
            file_obj.seek(0)

        files = {"file": (file_name, file_obj, "application/octet-stream")}

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.api_base_url}add", params=params, files=files)
            response.raise_for_status()

        # Kubo may stream multiple JSON lines. The last line is the final object.
        payload = self._parse_add_response(response.text)

        cid = payload.get("Hash")
        name = payload.get("Name", file_name)
        size_raw = payload.get("Size", 0)

        if not cid:
            raise ValueError(f"Kubo add response is missing Hash: {payload}")

        try:
            size = int(size_raw)
        except (TypeError, ValueError):
            size = 0

        return KuboUploadResult(name=name, cid=cid, size=size)

    def download_bytes(self, cid: str, *, max_bytes: int = 1_048_576) -> bytes:
        """Retrieve bounded content from the configured Kubo node's `/api/v0/cat`."""
        normalized_cid = self._normalize_cid(cid)
        if max_bytes <= 0:
            raise ValueError("max_bytes must be greater than zero.")

        content = bytearray()
        with httpx.Client(timeout=self.timeout) as client:
            with client.stream(
                "POST",
                f"{self.api_base_url}cat",
                params={"arg": normalized_cid},
            ) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise ValueError(f"Kubo content exceeds the {max_bytes}-byte limit.")

        return bytes(content)

    def download_json(self, cid: str, *, max_bytes: int = 1_048_576) -> dict:
        """Retrieve a JSON object from Kubo, rejecting non-object JSON payloads."""
        content = self.download_bytes(cid, max_bytes=max_bytes)
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Kubo content is not valid UTF-8 JSON.") from exc

        if not isinstance(payload, dict):
            raise ValueError("Kubo metadata must be a JSON object.")
        return payload

    @staticmethod
    def _normalize_cid(cid: str) -> str:
        normalized = str(cid or "").strip()
        if not normalized:
            raise ValueError("An IPFS CID is required.")
        if len(normalized) > 255 or any(character.isspace() for character in normalized):
            raise ValueError("IPFS CIDs must be non-whitespace strings up to 255 characters.")
        return normalized

    @staticmethod
    def _parse_add_response(response_text: str) -> dict:
        """Parse Kubo newline-delimited JSON output from `/api/v0/add`."""
        lines = [line.strip() for line in response_text.splitlines() if line.strip()]
        if not lines:
            raise ValueError("Kubo add response was empty")

        for line in reversed(lines):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

        raise ValueError(f"Unable to parse Kubo add response: {response_text}")


class PublicIPFSGatewayResolver:
    """Bounded, read-only resolver that falls back across configured public gateways."""

    def __init__(self, gateway_urls=None, timeout: float = 30.0):
        configured_urls = gateway_urls or getattr(settings, 'IPFS_PUBLIC_GATEWAY_URLS', [])
        self.gateway_urls = [str(url).strip().rstrip('/') + '/' for url in configured_urls if str(url).strip()]
        if not self.gateway_urls:
            raise ValueError('At least one public IPFS gateway URL must be configured.')
        self.timeout = timeout

    def download_bytes(self, cid: str, *, max_bytes: int = 1_048_576) -> bytes:
        normalized_cid = KuboAPIUploader._normalize_cid(cid)
        if max_bytes <= 0:
            raise ValueError('max_bytes must be greater than zero.')

        failures = []
        for gateway_url in self.gateway_urls:
            content = bytearray()
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                    with client.stream('GET', f'{gateway_url}{normalized_cid}') as response:
                        response.raise_for_status()
                        for chunk in response.iter_bytes():
                            content.extend(chunk)
                            if len(content) > max_bytes:
                                raise ValueError(f'IPFS content exceeds the {max_bytes}-byte limit.')
                return bytes(content)
            except (httpx.HTTPError, ValueError) as exc:
                failures.append(f'{gateway_url}: {exc}')
        raise ValueError(f'IPFS payload could not be resolved from configured gateways: {failures}')

    def download_json(self, cid: str, *, max_bytes: int = 1_048_576) -> dict:
        content = self.download_bytes(cid, max_bytes=max_bytes)
        try:
            payload = json.loads(content.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError('IPFS gateway content is not valid UTF-8 JSON.') from exc
        if not isinstance(payload, dict):
            raise ValueError('IPFS gateway payload must be a JSON object.')
        return payload
