from django.db import models
from django.contrib.auth.models import User
from .kubo_api import KuboAPIUploader

# Create your models here.
class IPFSUpload(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ipfs_uploads')
    # Do NOT instantiate the IPFS storage at import time; some environments
    # won't have the IPFS daemon available and importing the storage
    # implementation can raise/attempt network calls. Use a plain FileField
    # and perform IPFS operations lazily at runtime.
    file_stored_on_ipfs = models.FileField(blank=True, null=True)
    original_filename = models.CharField(max_length=255, blank=True)
    ipfs_hash = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        name = self.original_filename or getattr(self.file_stored_on_ipfs, 'name', '') or ''
        return f"IPFSUpload(file_name={name}, ipfs_hash={self.ipfs_hash})"

    @property
    def display_filename(self):
        return self.original_filename or getattr(self.file_stored_on_ipfs, 'name', '') or 'Unnamed File'

    @property
    def ipfs_uri(self):
        if not self.ipfs_hash:
            return ''
        if self.original_filename:
            return f'ipfs://{self.ipfs_hash}/{self.original_filename}'
        return f'ipfs://{self.ipfs_hash}'

    def upload_to_ipfs(self):
        """Upload the current file to Kubo `/api/v0/add` and save the CID.

        Returns the resulting CID on success or None on failure.
        """
        if not self.file_stored_on_ipfs:
            return None

        try:
            uploader = KuboAPIUploader()
            result = uploader.upload_fileobj(
                self.file_stored_on_ipfs.file,
                file_name=self.file_stored_on_ipfs.name,
                pin=True,
            )
            self.ipfs_hash = result.cid
            self.save(update_fields=['file_stored_on_ipfs', 'ipfs_hash'])
            return self.ipfs_hash
        except Exception:
            return None


class AddressMetadataTag(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        BROADCAST = 'broadcast', 'Broadcast'
        FAILED = 'failed', 'Failed before broadcast'
        BROADCAST_UNKNOWN = 'broadcast_unknown', 'Broadcast outcome unknown'

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='address_metadata_tags')
    target_address = models.CharField(max_length=128)
    funding_address = models.CharField(max_length=128, blank=True)
    main_asset = models.CharField(max_length=10)
    tag_type = models.CharField(max_length=3)
    revision = models.CharField(max_length=7, blank=True)
    asset_name = models.CharField(max_length=30, db_index=True)
    metadata = models.JSONField(default=dict)
    ipfs_cid = models.CharField(max_length=255, blank=True)
    transaction_id = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(blank=True)
    signature_verified = models.BooleanField(null=True)
    last_verified_at = models.DateTimeField(blank=True, null=True)
    verification_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('user', '-created_at')),
            models.Index(fields=('target_address', 'asset_name')),
        ]

    def __str__(self):
        return f"AddressMetadataTag(asset_name={self.asset_name}, status={self.status})"