from django import template

from Wallet.asset_tracking import classify_asset_type
from Wallet.models import TrackedAsset


register = template.Library()

ASSET_TYPE_LABELS = {
    TrackedAsset.ASSET_TYPE_MAIN: 'Main asset',
    TrackedAsset.ASSET_TYPE_SUB: 'Sub asset',
    TrackedAsset.ASSET_TYPE_UNIQUE: 'Unique asset',
    TrackedAsset.ASSET_TYPE_MESSAGING: 'Messaging channel',
    TrackedAsset.ASSET_TYPE_QUALIFIER: 'Qualifier asset',
    TrackedAsset.ASSET_TYPE_SUB_QUALIFIER: 'Sub-qualifier asset',
    TrackedAsset.ASSET_TYPE_RESTRICTED: 'Restricted asset',
    TrackedAsset.ASSET_TYPE_ADMIN: 'Administrator asset',
}


@register.filter
def market_symbol(symbol):
    canonical = str(symbol or '').strip()
    if classify_asset_type(canonical) in {
        TrackedAsset.ASSET_TYPE_SUB,
        TrackedAsset.ASSET_TYPE_SUB_QUALIFIER,
    }:
        return canonical.rsplit('/', 1)[-1].strip('#$~!')
    return canonical


@register.filter
def asset_type_label(symbol):
    canonical = str(symbol or '').strip()
    if canonical.upper() == 'EVR':
        return 'Native coin'
    return ASSET_TYPE_LABELS.get(classify_asset_type(canonical), 'Native asset')


@register.filter
def asset_tooltip(symbol):
    canonical = str(symbol or '').strip()
    label = asset_type_label(canonical)
    return f'{label}: {canonical}' if canonical else label
