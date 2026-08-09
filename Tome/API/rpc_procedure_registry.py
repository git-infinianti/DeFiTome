from API.rpc import evrmore_rpc


RPC_PROCEDURE_GROUPS = {
    'General': ['-'],
    'Addressindex': [
        'getaddressbalance',
        'getaddressdeltas',
        'getaddressmempool',
        'getaddresstxids',
        'getaddressutxos',
    ],
    'Assets': [
        'getassetdata',
        'getburnaddresses',
        'listaddressesbyasset',
        'listassetbalancesbyaddress',
        'listassets',
    ],
    'Blockchain': [
        'decodeblock',
        'getbestblockhash',
        'getblock',
        'getblockchaininfo',
        'getblockcount',
        'getblockhash',
        'getblockheader',
        'getchaintips',
        'getchaintxstats',
        'getdifficulty',
        'getmempoolancestors',
        'getmempooldescendants',
        'getmempoolentry',
        'getmempoolinfo',
        'getrawmempool',
        'getspentinfo',
        'gettxout',
        'gettxoutproof',
    ],
    'Control': [
        'help',
        'getnetworkhashps',
    ],
    'Rawtransactions': [
        'combinerawtransaction',
        'createrawtransaction',
        'decoderawtransaction',
        'decodescript',
        'getrawtransaction',
        'sendrawtransaction',
        'signrawtransaction',
        'testmempoolaccept',
    ],
    'Restricted assets': [
        'checkaddressrestriction',
        'checkaddresstag',
        'checkglobalrestriction',
        'getverifierstring',
        'isvalidverifierstring',
        'listaddressesfortag',
        'listaddressrestrictions',
        'listglobalrestrictions',
        'listtagsforaddress',
    ],
    'Util': [
        'estimatefee',
        'estimatesmartfee',
        'signmessagewithprivkey',
        'validateaddress',
    ],
    'Mining': [
        'verifymessage',
    ],
}


ALLOWED_RPC_PROCEDURES = {
    procedure
    for procedures in RPC_PROCEDURE_GROUPS.values()
    for procedure in procedures
    if procedure != '-'
}


def get_rpc_procedure_catalog():
    return [
        {
            'category': category,
            'procedures': procedures,
        }
        for category, procedures in RPC_PROCEDURE_GROUPS.items()
    ]


def is_allowed_rpc_procedure(procedure):
    return str(procedure or '').strip().lower() in ALLOWED_RPC_PROCEDURES


def execute_allowed_rpc_procedure(procedure, params=None):
    normalized = str(procedure or '').strip().lower()
    if normalized not in ALLOWED_RPC_PROCEDURES:
        raise ValueError(f'RPC procedure "{normalized}" is not allowed through this API.')

    method = getattr(evrmore_rpc.client, normalized, None)
    if method is None:
        raise ValueError(f'RPC procedure "{normalized}" is unavailable on the configured client.')

    normalized_params = params if isinstance(params, list) else []
    return method(*normalized_params)
