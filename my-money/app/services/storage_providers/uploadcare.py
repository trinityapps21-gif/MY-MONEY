"""Uploadcare provider - used for ALL image files.

* Upload:  POST {UPLOADCARE_UPLOAD_URL} (Upload API, multipart) with the public key
           and UPLOADCARE_STORE=1; the response maps the form field to the file UUID.
* Delete:  DELETE {UPLOADCARE_API_URL}/files/{uuid}/storage/ (REST API v0.7).
* Read:    server-side GET from the CDN; browsers never receive the CDN URL -
           Flask proxies images after checking group membership.

The secret key is only ever used here, on the server.
"""
import hashlib
import hmac
import time

import requests
from flask import current_app

from app.services.storage_providers.base import NotConfigured, StorageError, StorageProvider, UploadResult


class UploadcareProvider(StorageProvider):
    name = "uploadcare"

    def _cfg(self, key):
        return current_app.config[key]

    def configured(self):
        return bool(self._cfg("UPLOADCARE_PUBLIC_KEY") and self._cfg("UPLOADCARE_SECRET_KEY"))

    def _require(self):
        if not self.configured():
            raise NotConfigured("Image storage (Uploadcare) is not configured.")

    def _rest_headers(self):
        # Uploadcare.Simple sends the secret key from the server over HTTPS. Uploadcare's docs recommend the
        # signed "Uploadcare" scheme for production; swap it in here (single place) if you require that.
        return {
            "Accept": "application/vnd.uploadcare-v0.7+json",
            "Authorization": f"Uploadcare.Simple {self._cfg('UPLOADCARE_PUBLIC_KEY')}:{self._cfg('UPLOADCARE_SECRET_KEY')}",
        }

    def upload(self, data, filename, content_type, *, folder, tags=None):
        self._require()
        fields = {"UPLOADCARE_PUB_KEY": self._cfg("UPLOADCARE_PUBLIC_KEY"), "UPLOADCARE_STORE": "1"}
        if tags:
            fields["tags"] = ",".join(tags)
        if self._cfg("UPLOADCARE_SIGNED_UPLOADS"):
            expire = str(int(time.time()) + 600)
            fields["expire"] = expire
            fields["signature"] = hmac.new(self._cfg("UPLOADCARE_SECRET_KEY").encode(), expire.encode(),
                                           hashlib.sha256).hexdigest()
        try:
            r = requests.post(self._cfg("UPLOADCARE_UPLOAD_URL"), data=fields,
                              files={"file": (filename, data, content_type)},
                              timeout=self._cfg("STORAGE_HTTP_TIMEOUT"))
            r.raise_for_status()
            uuid = r.json()["file"]
        except (requests.RequestException, KeyError, ValueError) as e:
            current_app.logger.error("Uploadcare upload failed: %s", e)
            raise StorageError("The image could not be uploaded. Please try again.")
        return UploadResult(uuid, f"{self._cfg('UPLOADCARE_CDN_BASE')}/{uuid}/", len(data), content_type)

    def read(self, provider_file_id, file_url, *, content_type=""):
        try:
            r = requests.get(f"{self._cfg('UPLOADCARE_CDN_BASE')}/{provider_file_id}/",
                             timeout=self._cfg("STORAGE_HTTP_TIMEOUT"))
            r.raise_for_status()
            return r.content
        except requests.RequestException as e:
            current_app.logger.error("Uploadcare read failed: %s", e)
            raise StorageError("The image could not be loaded.")

    def delete(self, provider_file_id):
        self._require()
        try:
            r = requests.delete(f"{self._cfg('UPLOADCARE_API_URL')}/files/{provider_file_id}/storage/",
                                headers=self._rest_headers(), timeout=self._cfg("STORAGE_HTTP_TIMEOUT"))
            if r.status_code not in (200, 404):  # 404 = already gone
                r.raise_for_status()
        except requests.RequestException as e:
            current_app.logger.error("Uploadcare delete failed: %s", e)
            raise StorageError("The image could not be removed from storage.")
