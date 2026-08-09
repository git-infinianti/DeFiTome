from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_http_methods, require_POST
import json
import mimetypes

from .address_metadata import (
    AddressMetadataTagError,
    AddressMetadataTagIssuanceError,
    discover_address_metadata_tags,
    issue_address_metadata_tag,
    list_controlled_addresses,
    verify_stored_address_metadata_tag,
)
from .models import AddressMetadataTag, IPFSUpload
from .kubo_api import KuboAPIUploader
from Tome.qr import build_qr_data_uri
from Wallet.rip10 import (
    RIP10ValidationError,
    build_address_name_tag,
    build_encryption_tag,
    build_generic_tag,
    build_identity_tag,
)


def _unpin_from_ipfs(ipfs_hash):
    """
    Unpin content from local IPFS node.
    
    Args:
        ipfs_hash: The IPFS hash to unpin
        
    Returns:
        tuple: (success: bool, error_message: str or None)
    """
    if not ipfs_hash:
        return True, None
    
    try:
        import ipfshttpclient
        client = ipfshttpclient.connect()
        client.pin.rm(ipfs_hash)
        return True, None
    except Exception as e:
        return False, str(e)


@login_required
def media_list(request):
    """Display all media uploaded by the user"""
    uploads = IPFSUpload.objects.filter(user=request.user).order_by('-created_at')
    context = {
        'uploads': uploads,
    }
    return render(request, 'media/list.html', context)


@login_required
@require_http_methods(["GET", "POST"])
def media_upload(request):
    """Upload new media to IPFS"""
    if request.method == 'POST':
        uploaded_file = request.FILES.get('file')
        
        if not uploaded_file:
            messages.error(request, 'Please select a file to upload.')
            return render(request, 'media/upload.html')
        
        # Validate file size (max 10MB)
        max_size = 10 * 1024 * 1024  # 10MB in bytes
        if uploaded_file.size > max_size:
            messages.error(request, 'File size exceeds 10MB limit.')
            return render(request, 'media/upload.html')
        
        try:
            upload_result = KuboAPIUploader().upload_fileobj(
                uploaded_file,
                file_name=uploaded_file.name,
                pin=True,
                cid_version=0,
            )
            IPFSUpload.objects.create(
                user=request.user,
                original_filename=upload_result.name,
                ipfs_hash=upload_result.cid,
            )

            messages.success(request, f'File uploaded successfully! IPFS CID: {upload_result.cid}')
            return render(
                request,
                'media/upload.html',
                {
                    'uploaded_cid': upload_result.cid,
                    'uploaded_cid_qr_data_uri': build_qr_data_uri(upload_result.cid),
                },
            )
        except Exception as e:
            messages.error(request, f'Error uploading file: {str(e)}')
            return render(request, 'media/upload.html')
    
    return render(request, 'media/upload.html')


@login_required
@require_http_methods(["GET", "POST"])
def media_edit(request, pk):
    """Show media metadata. Local file re-upload is disabled for IPFS-only storage."""
    upload = get_object_or_404(IPFSUpload, pk=pk, user=request.user)
    
    if request.method == 'POST':
        messages.info(request, 'Re-upload is disabled. Media files are stored on IPFS only.')
        return redirect('media_edit', pk=upload.pk)
    
    context = {
        'upload': upload,
    }
    return render(request, 'media/edit.html', context)


@login_required
@require_http_methods(["GET", "POST"])
def media_delete(request, pk):
    """Delete media and unpin from IPFS"""
    upload = get_object_or_404(IPFSUpload, pk=pk, user=request.user)
    
    if request.method == 'POST':
        # Get details before deletion
        ipfs_hash = upload.ipfs_hash
        file_name = upload.display_filename
        
        try:
            # Try to unpin from IPFS if hash exists
            success, error = _unpin_from_ipfs(ipfs_hash)
            
            # Delete the record
            upload.delete()
            
            # Show appropriate message
            if ipfs_hash and not success:
                messages.warning(request, f'File "{file_name}" deleted but unpin failed: {error}')
            else:
                messages.success(request, f'File "{file_name}" deleted successfully.')
            
        except Exception as e:
            messages.error(request, f'Error deleting file: {str(e)}')
        
        return redirect('media_list')
    
    context = {
        'upload': upload,
    }
    return render(request, 'media/delete.html', context)


@login_required
def media_preview(request, pk):
    """Render an inline preview page for a stored IPFS media item."""
    upload = get_object_or_404(IPFSUpload, pk=pk, user=request.user)

    context = {
        'upload': upload,
        'ipfs_uri_qr_data_uri': build_qr_data_uri(upload.ipfs_uri),
        'preview_type': 'unavailable',
        'preview_text': '',
        'preview_data_uri': '',
        'mime_type': '',
    }

    if not upload.ipfs_hash:
        return render(request, 'media/preview.html', context)

    try:
        content = KuboAPIUploader().download_bytes(upload.ipfs_hash, max_bytes=2_000_000)
    except Exception as exc:
        context['preview_text'] = f'Unable to load IPFS content: {str(exc)}'
        return render(request, 'media/preview.html', context)

    mime_type, _ = mimetypes.guess_type(upload.display_filename)
    mime_type = mime_type or 'application/octet-stream'
    context['mime_type'] = mime_type

    if mime_type.startswith('image/'):
        import base64

        encoded = base64.b64encode(content).decode('ascii')
        context['preview_type'] = 'image'
        context['preview_data_uri'] = f'data:{mime_type};base64,{encoded}'
        return render(request, 'media/preview.html', context)

    if mime_type.startswith('text/') or upload.display_filename.lower().endswith('.json'):
        context['preview_type'] = 'text'
        try:
            context['preview_text'] = content.decode('utf-8')
        except UnicodeDecodeError:
            context['preview_text'] = 'This file is not UTF-8 text and cannot be previewed as text.'
        return render(request, 'media/preview.html', context)

    context['preview_type'] = 'binary'
    context['preview_text'] = (
        'Binary file preview is not supported inline. '
        f'Use CID {upload.ipfs_hash} and filename {upload.display_filename} to fetch this file from IPFS.'
    )
    return render(request, 'media/preview.html', context)


@login_required
def address_metadata_tag_list(request):
    """Display the signed-in user's locally recorded RIP-0010 tag attempts."""
    return render(request, 'media/address_metadata_tags.html', _address_metadata_context(request))


@login_required
@require_http_methods(["GET", "POST"])
def address_metadata_tag_create(request):
    """Create and broadcast a signed RIP-0010 address metadata tag."""
    try:
        controlled_addresses = list_controlled_addresses(request.user)
    except AddressMetadataTagError as exc:
        messages.error(request, str(exc))
        return redirect('portfolio')

    if request.method == 'POST':
        target_address = request.POST.get('target_address', '').strip()
        requested_tag_type = request.POST.get('tag_type', '').strip().upper()
        revision = request.POST.get('revision', '').strip()
        main_asset = request.POST.get('main_asset', '').strip()

        try:
            tag_type, tag_payload = _build_tag_payload(request, requested_tag_type, target_address)
            tag_record = issue_address_metadata_tag(
                user=request.user,
                main_asset=main_asset,
                tag_type=tag_type,
                target_address=target_address,
                tag_payload=tag_payload,
                revision=revision,
            )
        except AddressMetadataTagIssuanceError as exc:
            messages.error(request, str(exc))
            return redirect('address_metadata_tag_list')
        except (AddressMetadataTagError, RIP10ValidationError, ValueError) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f'Issued {tag_record.asset_name}. Transaction ID: {tag_record.transaction_id}',
            )
            return redirect('address_metadata_tag_list')

    return render(
        request,
        'media/address_metadata_tag_create.html',
        {
            'controlled_addresses': controlled_addresses,
        },
    )


@login_required
@require_http_methods(["GET"])
def address_metadata_tag_lookup(request):
    """Look up RIP-0010 tags currently held by a public address."""
    address = request.GET.get('address', '').strip()
    if not address:
        messages.error(request, 'Enter an address to look up its metadata tags.')
        return redirect('address_metadata_tag_list')

    try:
        lookup_results = discover_address_metadata_tags(address)
    except AddressMetadataTagError as exc:
        messages.error(request, str(exc))
        return redirect('address_metadata_tag_list')

    return render(
        request,
        'media/address_metadata_tags.html',
        _address_metadata_context(
            request,
            lookup_address=address,
            lookup_results=lookup_results,
        ),
    )


@login_required
@require_POST
def address_metadata_tag_verify(request, pk):
    """Revalidate a locally recorded tag's CID, metadata hash, and signature."""
    tag_record = get_object_or_404(AddressMetadataTag, pk=pk, user=request.user)
    verification = verify_stored_address_metadata_tag(tag_record)
    if verification.is_valid:
        messages.success(request, f'{tag_record.asset_name} metadata and signature verified.')
    else:
        details = verification.error or ' '.join(
            verification.validation.errors if verification.validation else ()
        )
        messages.error(request, f'{tag_record.asset_name} could not be verified. {details}')
    return redirect('address_metadata_tag_list')


def _address_metadata_context(request, *, lookup_address='', lookup_results=None):
    return {
        'tags': AddressMetadataTag.objects.filter(user=request.user),
        'lookup_address': lookup_address,
        'lookup_results': lookup_results,
    }


def _build_tag_payload(request, requested_tag_type, target_address):
    if requested_tag_type == 'ANT':
        address_name = request.POST.get('address_name', '').strip()
        address_name_mime = request.POST.get(
            'address_name_mime',
            'text/x-markdown; charset=UTF-8',
        ).strip()
        icon = request.POST.get('icon', '').strip()
        _validate_field_length(address_name, 256, 'Address name')
        _validate_field_length(address_name_mime, 128, 'Address-name MIME type')
        _validate_field_length(icon, 131_072, 'Icon')
        return 'ANT', build_address_name_tag(
            target_address,
            address_name,
            address_name_mime,
            icon or None,
        )

    if requested_tag_type == 'PGP':
        public_key = request.POST.get('pgp_public_key', '').strip()
        _validate_field_length(public_key, 65_536, 'PGP public key')
        return 'PGP', build_encryption_tag(target_address, public_key)

    if requested_tag_type == 'AIT':
        algorithm = request.POST.get('identity_algorithm', '').strip()
        identity_document = request.POST.get('identity_document', '').strip()
        _validate_field_length(algorithm, 128, 'Encryption algorithm')
        _validate_field_length(identity_document, 255, 'Identity-document CID')
        return 'AIT', build_identity_tag(target_address, algorithm, identity_document)

    if requested_tag_type == 'CUSTOM':
        custom_tag_type = request.POST.get('custom_tag_type', '').strip()
        raw_metadata = request.POST.get('custom_metadata', '').strip()
        _validate_field_length(raw_metadata, 65_536, 'Custom metadata')
        if not raw_metadata:
            raise ValueError('Custom metadata is required.')
        try:
            metadata = json.loads(raw_metadata)
        except json.JSONDecodeError as exc:
            raise ValueError('Custom metadata must be valid JSON.') from exc
        return custom_tag_type, build_generic_tag(target_address, custom_tag_type, metadata)

    raise ValueError('Choose a supported address metadata tag type.')


def _validate_field_length(value, maximum, field_name):
    if len(value) > maximum:
        raise ValueError(f'{field_name} cannot exceed {maximum} characters.')
