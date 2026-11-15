"""In-memory provider for automated tests ONLY (STORAGE_BACKEND="memory").
Refused in production. Two named instances let tests prove that images route to
"uploadcare" and documents to "imagekit"."""
import hashlib
import uuid

from app.services.storage_providers.base import StorageError, StorageProvider, UploadResult


class MemoryProvider(StorageProvider):
    def __init__(self, name):
        self.name = name
        self.objects = {}

    def configured(self):
        return True

    def upload(self, data, filename, content_type, *, folder, tags=None):
        fid = uuid.uuid4().hex
        self.objects[fid] = (bytes(data), filename, content_type, folder)
        return UploadResult(fid, f"memory://{self.name}/{folder}/{fid}", len(data), content_type)

    def read(self, provider_file_id, file_url, *, content_type=""):
        try:
            return self.objects[provider_file_id][0]
        except KeyError:
            raise StorageError("Missing object")

    def delete(self, provider_file_id):
        self.objects.pop(provider_file_id, None)

    def signed_url(self, provider_file_id, file_url, *, expires_in=300):
        if self.name != "imagekit":
            return None
        sig = hashlib.sha1(f"{provider_file_id}{expires_in}".encode()).hexdigest()
        return f"https://signed.test/{provider_file_id}?ik-t={expires_in}&ik-s={sig}"
