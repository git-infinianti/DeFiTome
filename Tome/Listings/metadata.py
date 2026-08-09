import re
from copy import deepcopy

UNIQUE_METADATA_SCHEMA = 'defitome.unique-asset-metadata'
UNIQUE_METADATA_VERSION = 1

_IPFS_URI_RE = re.compile(r'^ipfs://(?P<cid>[^/\s]+)')


def extract_cid_from_uri(value):
    """Extract CID from an ipfs:// URI when present."""
    text = str(value or '').strip()
    if not text:
        return ''
    match = _IPFS_URI_RE.match(text)
    if match:
        return match.group('cid').strip()
    return ''


def normalize_unique_asset_metadata(asset_name, metadata_payload, source_cid=''):
    """Normalize arbitrary NFT metadata to a stable, versioned schema."""
    metadata = metadata_payload if isinstance(metadata_payload, dict) else {}

    name = str(metadata.get('name') or asset_name).strip() or asset_name
    description = str(metadata.get('description') or '').strip()
    image = str(metadata.get('image') or metadata.get('image_url') or '').strip()
    external_url = str(metadata.get('external_url') or '').strip()

    attributes_value = metadata.get('attributes', [])
    attributes = attributes_value if isinstance(attributes_value, list) else []

    normalized = {
        'schema': UNIQUE_METADATA_SCHEMA,
        'version': UNIQUE_METADATA_VERSION,
        'asset_name': str(asset_name),
        'source_ipfs_cid': str(source_cid or '').strip(),
        'name': name,
        'description': description,
        'image': image,
        'external_url': external_url,
        'attributes': deepcopy(attributes),
        'raw': deepcopy(metadata),
    }

    return normalized, UNIQUE_METADATA_VERSION


def _extract_metadata_attribute(attributes, trait_type):
    if not isinstance(attributes, list):
        return ''

    normalized_trait_type = str(trait_type or '').strip().lower()
    for attribute in attributes:
        if not isinstance(attribute, dict):
            continue
        attribute_trait_type = str(attribute.get('trait_type') or '').strip().lower()
        if attribute_trait_type == normalized_trait_type:
            return str(attribute.get('value') or '').strip()
    return ''


def validate_unique_asset_metadata(asset_name, metadata_payload, source_cid=''):
    """Validate that unique asset metadata was minted using DeFi Tome's schema."""
    if not isinstance(metadata_payload, dict):
        raise ValueError('Unique asset metadata must be a JSON object.')

    schema = str(metadata_payload.get('schema') or '').strip()
    if schema != UNIQUE_METADATA_SCHEMA:
        raise ValueError('Unique asset metadata schema is not supported.')

    try:
        version = int(metadata_payload.get('version'))
    except (TypeError, ValueError):
        raise ValueError('Unique asset metadata version is invalid.')

    if version != UNIQUE_METADATA_VERSION:
        raise ValueError('Unique asset metadata version is not supported.')

    normalized_asset_name = str(metadata_payload.get('asset_name') or '').strip().upper()
    expected_asset_name = str(asset_name or '').strip().upper()
    if normalized_asset_name != expected_asset_name:
        raise ValueError('Unique asset metadata asset name does not match the selected asset.')

    attributes = metadata_payload.get('attributes')
    root_asset = _extract_metadata_attribute(attributes, 'root_asset').upper()
    asset_tag = _extract_metadata_attribute(attributes, 'asset_tag')
    if not root_asset or not asset_tag:
        raise ValueError('Unique asset metadata is missing required mint attributes.')

    if f'{root_asset}#{asset_tag}' != expected_asset_name:
        raise ValueError('Unique asset metadata does not match the selected asset.')

    raw_metadata = metadata_payload.get('raw')
    if not isinstance(raw_metadata, dict):
        raise ValueError('Unique asset metadata must retain the original minted payload.')

    raw_schema = str(raw_metadata.get('schema') or '').strip()
    raw_version = raw_metadata.get('version')
    raw_asset_name = str(raw_metadata.get('asset_name') or '').strip().upper()
    if raw_schema != UNIQUE_METADATA_SCHEMA or str(raw_version) != str(UNIQUE_METADATA_VERSION) or raw_asset_name != expected_asset_name:
        raise ValueError('Unique asset metadata was not minted with the standardized DeFi Tome schema.')

    return normalize_unique_asset_metadata(expected_asset_name, metadata_payload, source_cid=source_cid)


def build_unique_metadata_payload(root_name, asset_tag, name, description, image):
    """Build a standardized metadata payload for newly minted unique assets."""
    asset_name = f"{str(root_name).strip().upper()}#{str(asset_tag).strip()}"
    draft = {
        'name': str(name or asset_name).strip() or asset_name,
        'description': str(description or '').strip(),
        'image': str(image or '').strip(),
        'attributes': [
            {'trait_type': 'root_asset', 'value': str(root_name).strip().upper()},
            {'trait_type': 'asset_tag', 'value': str(asset_tag).strip()},
        ],
    }
    return normalize_unique_asset_metadata(asset_name, draft)
