"""ImageKit provider - used for documents and other non-image attachments.

* Upload:  POST {IMAGEKIT_UPLOAD_URL} multipart, HTTP Basic auth (private key as
           username, empty password). Files are uploaded with isPrivateFile=true so
           they cannot be fetched without a signature.
* Delete:  DELETE {IMAGEKIT_API_URL}/files/{fileId} (Basic auth).
* Delivery: short-lived signed URLs. Signature = lowercase hex HMAC-SHA1, keyed with
           the private key, over "<path after the URL endpoint><expiry epoch>";
           the URL gets ?ik-t=<expiry>&ik-s=<signature>.

The private key is only ever used here, on the server.
"""
import hashlib
import hmac
import time
from urllib.parse import quote

import requests
from flask import current_app

from app.services.storage_providers.base import NotConfigured, StorageError, StorageProvider, UploadResult


class ImageKitProvider(StorageProvider):
    name = "imagekit"

    def _cfg(self, key):
        return current_app.config[key]

    def configured(self):
        return bool(self._cfg("IMAGEKIT_PRIVATE_KEY") and self._cfg("IMAGEKIT_URL_ENDPOINT"))

    def _require(self):
        if not self.configured():
            raise NotConfigured("Document storage (ImageKit) is not configured.")

    def _auth(self):
        return (self._cfg("IMAGEKIT_PRIVATE_KEY"), "")

    def upload(self, data, filename, content_type, *, folder, tags=None):
        self._require()
        form = {"fileName": filename, "useUniqueFileName": "true", "folder": folder, "isPrivateFile": "true"}
        if tags:
            form["tags"] = ",".join(tags)
        try:
            r = requests.post(self._cfg("IMAGEKIT_UPLOAD_URL"), data=form, auth=self._auth(),
                              files={"file": (filename, data, content_type)},
                              timeout=self._cfg("STORAGE_HTTP_TIMEOUT"))
            r.raise_for_status()
            body = r.json()
            return UploadResult(body["fileId"], body["url"], len(data), content_type)
        except (requests.RequestException, KeyError, ValueError) as e:
            current_app.logger.error("ImageKit upload failed: %s", e)
            raise StorageError("The document could not be uploaded. Please try again.")

    def signed_url(self, provider_file_id, file_url, *, expires_in=300):
        self._require()
        endpoint = self._cfg("IMAGEKIT_URL_ENDPOINT") + "/"
        if not file_url.startswith(endpoint):
            raise StorageError("Stored document reference does not match the configured ImageKit endpoint.")
        path = quote(file_url[len(endpoint):], safe="/%:-_.~")
        expiry = int(time.time()) + int(expires_in)
        sig = hmac.new(self._cfg("IMAGEKIT_PRIVATE_KEY").encode(), f"{path}{expiry}".encode(),
                       hashlib.sha1).hexdigest().lower()
        return f"{endpoint}{path}?ik-t={expiry}&ik-s={sig}"

    def read(self, provider_file_id, file_url, *, content_type=""):
        try:
            r = requests.get(self.signed_url(provider_file_id, file_url, expires_in=60),
                             timeout=self._cfg("STORAGE_HTTP_TIMEOUT"))
            r.raise_for_status()
            return r.content
        except requests.RequestException as e:
            current_app.logger.error("ImageKit read failed: %s", e)
            raise StorageError("The document could not be loaded.")

    def delete(self, provider_file_id):
        self._require()
        try:
            r = requests.delete(f"{self._cfg('IMAGEKIT_API_URL')}/files/{provider_file_id}", auth=self._auth(),
                                timeout=self._cfg("STORAGE_HTTP_TIMEOUT"))
            if r.status_code not in (200, 204, 404):
                r.raise_for_status()
        except requests.RequestException as e:
            current_app.logger.error("ImageKit delete failed: %s", e)
            raise StorageError("The document could not be removed from storage.")
